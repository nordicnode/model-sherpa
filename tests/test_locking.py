"""Tests for the cross-process/cross-thread locking backend (Phase 2).

The plugin must offer real mutual exclusion on every platform:
  - Unix:        fcntl.flock (LOCK_NB + retry)
  - Windows:     msvcrt.locking (LK_NBLCK + retry)
  - Neither:     fail-open no-op (single-process safety only)

These tests pin the mutual-exclusion contract without depending on which
backend is present at runtime — they monkeypatch the backend to the
platform's native one and assert that a second contender cannot enter while
the first holds the lock.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest


def _load_fresh_module():
    """Reload the plugin from source so module globals (e.g. fcntl/msvcrt) are
    evaluated fresh against the current sys.modules stubs."""
    for name in list(sys.modules):
        if name in ("model_sherpa", "model-sherpa") or name.startswith("model_sherpa."):
            del sys.modules[name]
    plugin_path = Path(__file__).resolve().parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("model_sherpa", str(plugin_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sherpa_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_lock_file_mutual_exclusion_with_real_backend(sherpa_home, tmp_path):
    """Under low contention a real locking backend provides mutual exclusion.

    NOTE on design: _lock_file is fail-open — after a bounded retry budget it
    yields without the lock rather than ever blocking the CLI indefinitely. So
    this test deliberately uses *low* contention (2 threads, short hold) so the
    contending thread's retries succeed within budget, proving the lock is real
    and not a no-op. (High-contention fail-open is exercised by
    test_lock_file_fail_open_when_no_backend.) A no-op lock would overlap even
    at low contention, which is what this guards against.
    """
    mod = _load_fresh_module()
    if mod.fcntl is None and mod.msvcrt is None:
        pytest.skip("no real locking backend on this platform")

    lock_path = tmp_path / "mu.lock"
    overlap_detected = threading.Event()
    in_critical = [0]
    guard = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()  # release both threads together to create one contention point
        for _ in range(10):
            with mod._lock_file(lock_path, mod.fcntl.LOCK_EX if mod.fcntl else 1):
                with guard:
                    in_critical[0] += 1
                    if in_critical[0] > 1:
                        overlap_detected.set()
                # Brief hold; well within the retry budget of the contender.
                time.sleep(0.001)
                with guard:
                    in_critical[0] -= 1
            time.sleep(0.001)  # give the other thread a turn

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not overlap_detected.is_set(), (
        "two threads entered the critical section simultaneously at low contention — the lock is effectively a no-op"
    )


def test_lock_file_creates_target_file_on_real_backend(sherpa_home, tmp_path):
    """Acquiring the lock must materialise the lock file when a real backend is
    used (closes the Windows gap where the no-op path left no lock file)."""
    mod = _load_fresh_module()
    if mod.fcntl is None and sys.platform != "win32":
        pytest.skip("no real locking backend on this platform")
    lock_path = tmp_path / "created.lock"
    with mod._lock_file(lock_path, mod.fcntl.LOCK_EX if mod.fcntl else 1):
        pass
    assert lock_path.exists(), "lock file should be created by the real backend"


def test_msvcrt_backend_used_on_windows_when_fcntl_absent(monkeypatch, sherpa_home, tmp_path):
    """When fcntl is None but msvcrt is available, the lock uses msvcrt and
    still provides mutual exclusion. Forces the Windows code path on any
    platform by stubbing fcntl=None and providing a real msvcrt if importable."""
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        pytest.skip("msvcrt not available on this platform")

    mod = _load_fresh_module()
    # Force the Windows branch.
    monkeypatch.setattr(mod, "fcntl", None, raising=False)

    lock_path = tmp_path / "msvcrt.lock"
    held = threading.Event()
    overlap = threading.Event()
    n = 30

    def worker():
        for _ in range(n):
            with mod._lock_file(lock_path, 1):
                if held.is_set():
                    overlap.set()
                held.set()
                held.clear()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not overlap.is_set(), "msvcrt backend must be mutually exclusive"
    assert lock_path.exists(), "msvcrt backend must create the lock file"


def test_lock_file_fail_open_when_no_backend(monkeypatch, sherpa_home, tmp_path):
    """When neither fcntl nor msvcrt is available, the lock fails open (no-op)
    rather than raising — the CLI must never hang."""
    mod = _load_fresh_module()
    monkeypatch.setattr(mod, "fcntl", None, raising=False)
    monkeypatch.setattr(mod, "msvcrt", None, raising=False)
    lock_path = tmp_path / "noop.lock"
    # Must not raise.
    with mod._lock_file(lock_path, 1):
        pass


# ---------------------------------------------------------------------------
# Task 2.2: _update_state must acquire the cross-process lock exactly once.
#
# Previously _update_state took LOCK_EX, then _load_state() re-acquired
# LOCK_SH on the same lock file. On a contested lock the inner acquire retried
# 10x (~100ms) then failed open — adding latency to every stat flush and
# (under contention) reading state without the lock the outer caller holds.
# ---------------------------------------------------------------------------


def test_update_state_acquires_lock_exactly_once(monkeypatch, sherpa_home):
    """_update_state must take the cross-process lock exactly once, not twice."""
    mod = _load_fresh_module()

    lock_calls = {"count": 0}
    real_lock = mod._lock_state_file

    @contextlib.contextmanager
    def counting_lock(mode):
        lock_calls["count"] += 1
        with real_lock(mode):
            yield

    monkeypatch.setattr(mod, "_lock_state_file", counting_lock)

    mod._update_state(lambda st: st.update({"enabled": True}))
    assert lock_calls["count"] == 1, (
        f"_update_state should acquire the state lock exactly once, got {lock_calls['count']}"
    )
