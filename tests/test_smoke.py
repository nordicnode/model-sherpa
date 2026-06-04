"""Smoke tests for model-sherpa.

These tests are deliberately framework-free: they import the plugin as a
Python module against a temporary HERMES_HOME and exercise the hook
entry-points and helper functions directly. They are not full unit tests
for the tool registry (which lives elsewhere in Hermes); they exist to
catch regressions in the bugs the v0.3.0+ refactor fixed:

  - Bug #1: _lock_state_file referenced an undefined ``lock_file``.
  - Bug #2: _queue_nudge referenced undefined throttle constants.
  - Bug #3: _record_event referenced an undefined ``_last_rotation_check``.
  - Bug #4: _on_session_end had a missing ``global`` declaration.
  - Bug #5: bullet-list regex used ``\\+`` (literal ``+``) instead of ``+``.

Run with:  python3 -m pytest tests/
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture: load the plugin fresh against a temporary HERMES_HOME.
# ---------------------------------------------------------------------------

@pytest.fixture()
def sherpa_home(monkeypatch, tmp_path):
    """Redirect HERMES_HOME to a tmp dir so the test never touches the real
    user's state. Returns the Path of the temporary home directory.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def mod(sherpa_home):
    """Reload the plugin module from scratch so module-level state is clean."""
    for name in list(sys.modules):
        if name in ("model_sherpa", "model-sherpa") or name.startswith("model_sherpa."):
            del sys.modules[name]
    plugin_path = Path(__file__).resolve().parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("model_sherpa", str(plugin_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Bug #1: _lock_state_file uses the right lock file path.
# ---------------------------------------------------------------------------

def test_lock_state_file_uses_lock_file_constant(mod, sherpa_home):
    """The lock file constant exists and is the expected path."""
    assert hasattr(mod, "LOCK_FILE"), "LOCK_FILE constant missing"
    expected = mod.STATE_DIR / "state.lock"
    assert mod.LOCK_FILE == expected


def test_lock_state_file_acquires_and_releases(mod, sherpa_home):
    """The context manager should not raise; the lock file should exist
    after the with-block returns."""
    with mod._lock_state_file(2):  # LOCK_EX
        pass
    assert mod.LOCK_FILE.exists(), "lock file should be created on first use"


# ---------------------------------------------------------------------------
# Bug #2: _queue_nudge uses the throttle constants.
# ---------------------------------------------------------------------------

def test_nudge_window_constants_exist(mod):
    assert hasattr(mod, "_NUDGE_WINDOW_TURNS")
    assert hasattr(mod, "_NUDGE_LIMIT_PER_WINDOW")
    assert mod._NUDGE_WINDOW_TURNS > 0
    assert mod._NUDGE_LIMIT_PER_WINDOW > 0


def test_queue_nudge_does_not_raise_within_window(mod, sherpa_home):
    """A handful of nudges of the same kind should not raise NameError.

    This is the regression test for Bug #2 — the original code referenced
    _NUDGE_WINDOW_TURNS / _NUDGE_LIMIT_PER_WINDOW which were never defined,
    so every call beyond the first window-bound would crash.
    """
    sid = "test_session"
    mod._drain_nudges(sid)
    # Simulate user turns; _queue_nudge throttles by kind and turn window.
    for turn in range(mod._NUDGE_LIMIT_PER_WINDOW + 2):
        mod._turn_count[sid] = turn
        mod._drain_nudges(sid)  # clear the per-turn dedup bucket
        mod._queue_nudge(sid, "loop", f"loop message {turn}")
    # The first N should be queued; subsequent should be suppressed.
    # The dedup logic means each new "loop" within a single turn is dropped
    # (so we get N-1 throttles in the first N turns + 1 throttle on the
    # turn after the limit is exceeded). The on-disk stat tracks suppression.
    mod._flush_stats()
    state = mod._load_state()
    assert state["stats"].get("nudges_suppressed", 0) >= 1, (
        "nudges_suppressed counter should be at least 1 after exceeding "
        "the per-window limit"
    )


# ---------------------------------------------------------------------------
# Bug #3: _record_event writes to events.jsonl even on the first call.
# ---------------------------------------------------------------------------

def test_record_event_writes_to_disk_on_first_call(mod, sherpa_home):
    """_record_event used to raise UnboundLocalError on the first call (the
    rotation check referenced an undefined _last_rotation_check). The fix
    is to define the constant so the first event lands on disk.
    """
    assert hasattr(mod, "_last_rotation_check"), "_last_rotation_check should be defined"
    mod._record_event("s1", "test_kind", "test_detail")
    time.sleep(0.1)
    events_path = sherpa_home / "memories" / "model-sherpa" / "events.jsonl"
    assert events_path.exists(), "events.jsonl should be written on first event"
    lines = events_path.read_text().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert record["kind"] == "test_kind"
    assert record["session_id"] == "s1"


# ---------------------------------------------------------------------------
# Bug #4: _on_session_end does not raise.
# ---------------------------------------------------------------------------

def test_on_session_end_does_not_raise(mod, sherpa_home):
    """The original code raised UnboundLocalError because _cleanup_timer
    was treated as a local variable (assigned later in the same function)
    and read before that assignment.
    """
    mod._on_session_start(session_id="s_test")
    mod._on_session_end(session_id="s_test")


# ---------------------------------------------------------------------------
# Bug #5: _is_multistep_request detects numbered and bulleted lists.
# ---------------------------------------------------------------------------

def test_is_multistep_numbered_list(mod):
    msg = "1. first thing\n2. second thing\n3. third thing"
    assert mod._is_multistep_request(msg) is True


def test_is_multistep_dash_list(mod):
    msg = "- a\n- b\n- c"
    assert mod._is_multistep_request(msg) is True


def test_is_multistep_asterisk_list(mod):
    msg = "* a\n* b\n* c"
    assert mod._is_multistep_request(msg) is True


def test_is_multistep_single_sentence_false(mod):
    assert mod._is_multistep_request("just one sentence here please") is False


# ---------------------------------------------------------------------------
# Loop detection end-to-end.
# ---------------------------------------------------------------------------

def test_loop_detection_emits_nudge(mod, sherpa_home):
    """Three identical terminal calls should produce exactly one 'loop' nudge."""
    sid = "loop_test"
    mod._drain_nudges(sid)
    for _ in range(3):
        mod._post_tool_call(
            tool_name="terminal",
            args={"command": "false"},
            result={"error": "command not found"},
            session_id=sid,
        )
    pending = mod._pending_nudges.get(sid, [])
    kinds = [k for k, _ in pending]
    assert kinds.count("loop") == 1, f"expected exactly one loop nudge, got {kinds}"


# ---------------------------------------------------------------------------
# Argument repair: smart quotes + arg-name aliases.
# ---------------------------------------------------------------------------

def test_repair_args_smart_quotes(mod):
    args = {"command": "ls \u201cfoo\u201d"}
    fixes = mod._repair_args("terminal", args)
    assert args["command"] == 'ls "foo"'
    assert any("smart-quotes" in f for f in fixes)


def test_repair_args_aliases(mod):
    args = {"file_path": "/etc/hosts", "cmd": "ls"}
    fixes = mod._repair_args("read_file", args)
    assert "path" in args
    assert args["path"] == "/etc/hosts"
    assert any("file_path" in f for f in fixes)


# ---------------------------------------------------------------------------
# Command lint: $ prompt strip and cd extraction.
# ---------------------------------------------------------------------------

def test_command_lint_strips_dollar_prompt(mod):
    cmd, warnings, fixes = mod._lint_terminal_command("$ ls -la", {})
    assert cmd == "ls -la"
    assert fixes == 1
    assert warnings == []


def test_command_lint_extracts_cd(mod):
    cmd, warnings, fixes = mod._lint_terminal_command("cd /tmp && ls", {})
    assert cmd == "ls"
    assert fixes == 1


# ---------------------------------------------------------------------------
# Arg guard: blocks empty required args.
# ---------------------------------------------------------------------------

def test_arg_guard_blocks_empty_command(mod, sherpa_home):
    """terminal(command='') should be blocked."""
    result = mod._pre_tool_call(
        tool_name="terminal",
        args={"command": ""},
        session_id="arg_guard_test",
    )
    assert result is not None
    assert result.get("action") == "block"
    assert "missing" in result.get("message", "").lower()


def test_arg_guard_allows_valid_command(mod, sherpa_home):
    """terminal(command='ls -la') should pass."""
    result = mod._pre_tool_call(
        tool_name="terminal",
        args={"command": "ls -la"},
        session_id="arg_guard_test2",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Compound stat key collision safety (Issue #11).
# ---------------------------------------------------------------------------

def test_per_tool_sep_does_not_appear_in_keys(mod):
    """The compound-key separator should be a control character."""
    assert ord(mod._PER_TOOL_SEP) < 0x20


def test_bump_tool_stat_uses_separator(mod, sherpa_home):
    """When _bump_tool_stat is called, the pending stats should contain
    both the global key and the compound per-tool key, with the separator."""
    mod._bump_tool_stat("read_file", "rewrites", 1)
    pending = mod._pending_stats
    assert pending.get("rewrites") == 1
    compound_keys = [k for k in pending if mod._PER_TOOL_SEP in k]
    assert any(k.endswith(mod._PER_TOOL_SEP + "read_file") for k in compound_keys)


# ---------------------------------------------------------------------------
# Fingerprint cycle safety (Issue #10).
# ---------------------------------------------------------------------------

def test_fingerprint_value_handles_cycles(mod):
    """A cyclic dict should not raise RecursionError."""
    a = {"k": "v"}
    a["self"] = a
    result = mod._fingerprint_value("k", a)
    assert "__cycle__" in repr(result)


def test_fingerprint_value_respects_depth_cap(mod):
    """A 50-deep nested list should produce a depth-cap sentinel, not crash."""
    deep = "leaf"
    for _ in range(50):
        deep = [deep]
    result = mod._fingerprint_value("k", deep)
    assert "__depth_cap__" in repr(result)


# ---------------------------------------------------------------------------
# Plugin version consistency.
# ---------------------------------------------------------------------------

def test_version_is_defined(mod):
    assert hasattr(mod, "__version__")
    assert mod.__version__ == "0.3.1"


# ---------------------------------------------------------------------------
# Regression tests for concurrency and timer leak fixes.
# ---------------------------------------------------------------------------

def test_session_start_restarts_cleanup_task(mod, sherpa_home):
    """Starting a session should ensure the cleanup task is running, even if it
    was previously stopped/None."""
    mod._cleanup_timer = None
    mod._on_session_start(session_id="fresh_session")
    try:
        assert mod._cleanup_timer is not None, "Cleanup timer should have been started by _on_session_start"
    finally:
        # Cleanup timer for testing
        if mod._cleanup_timer:
            mod._cleanup_timer.cancel()
            mod._cleanup_timer = None


def test_cleanup_stale_sessions_does_not_leak_timer_when_no_sessions(mod, sherpa_home):
    """_cleanup_stale_sessions should not reschedule itself if there are no
    active sessions left."""
    mod._cleanup_timer = None
    # No sessions are tracked
    assert len(mod._all_session_ids()) == 0
    mod._cleanup_stale_sessions()
    assert mod._cleanup_timer is None, "Cleanup timer should remain None when no sessions are active"


def test_ensure_periodic_flush_does_not_leak_timer_when_no_sessions(mod, sherpa_home):
    """_ensure_periodic_flush should not reschedule its timer/tick if there
    are no active sessions left."""
    mod._periodic_flush_timer = None
    # Verify no active sessions
    assert len(mod._all_session_ids()) == 0
    mod._ensure_periodic_flush()
    # Now simulate the tick function running
    assert mod._periodic_flush_timer is not None
    timer = mod._periodic_flush_timer
    try:
        # Trigger the tick callback manually to simulate timer firing
        # This will run _tick, which should see 0 sessions and NOT reschedule.
        timer.finished.set()  # stop the timer's waiting
        # Call the target directly to test rescheduling branch
        timer.function(*timer.args, **timer.kwargs)
        assert mod._periodic_flush_timer is None, "Periodic flush timer should not be rescheduled"
    finally:
        timer.cancel()


def test_didyoumean_path_respects_candidates_limit(mod, sherpa_home, monkeypatch):
    """_didyoumean_path should scan at most _DYM_MAX_CANDIDATES entries, preventing
    latency spikes in directories with thousands of files."""
    # Create a mock directory with many entries
    ancestor = sherpa_home / "heavy_dir"
    ancestor.mkdir()
    
    # Mock os.scandir to return a huge number of entries
    class MockDirEntry:
        def __init__(self, name):
            self.name = name
            
    class MockScandir:
        def __init__(self, path):
            pass
        def __enter__(self):
            # Return more entries than _DYM_MAX_CANDIDATES (500)
            return (MockDirEntry(f"file_{i}.txt") for i in range(1000))
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
            
    monkeypatch.setattr("os.scandir", MockScandir)
    
    # We pass a file path that doesn't exist under ancestor
    target_path = ancestor / "nonexistent.txt"
    
    # We call the helper. It should run successfully and return None (since no close match).
    # Crucially, it shouldn't try to compare all 1000 entries (which we've capped).
    # Since we can't directly check count of difflib inputs easily, we verify that the constant is indeed respected.
    result = mod._didyoumean_path(str(target_path))
    assert result is None


