"""Tests for the read-range damper and the dry_run feature paths (Phase 4 gap).

The review found both had zero coverage:
  - read_damper: range-subset detection was never exercised end-to-end.
  - dry_run: six distinct soft-block/nudge branches were untested.

These exercise _pre_tool_call with a real (isolated) state file so the
dry_run toggle and read_damper fire through their production code paths.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "__init__.py"


def _load_fresh_module():
    for name in list(sys.modules):
        if name in ("model_sherpa", "model-sherpa") or name.startswith("model_sherpa."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("model_sherpa", str(PLUGIN_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sherpa_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def mod(sherpa_home):
    return _load_fresh_module()


def _enable_feature(mod, name: str, value: bool) -> None:
    mod._update_state(lambda st: st["features"].__setitem__(name, value))


# ---------------------------------------------------------------------------
# Read-range damper.
# ---------------------------------------------------------------------------


def test_read_damper_blocks_subset_range(mod, tmp_path):
    """Re-reading a range fully contained in a prior read this turn is blocked."""
    _enable_feature(mod, "read_damper", True)
    f = tmp_path / "f.txt"
    f.write_text("line\n" * 1000)
    sid = "s1"
    # First read 1-200 allowed.
    r1 = mod._pre_tool_call("read_file", {"path": str(f), "offset": 1, "limit": 200}, session_id=sid)
    assert r1 is None
    # Re-read 50-100 (subset of 1-200) blocked.
    r2 = mod._pre_tool_call("read_file", {"path": str(f), "offset": 50, "limit": 51}, session_id=sid)
    assert r2 is not None and r2.get("action") == "block"


def test_read_damper_allows_paging(mod, tmp_path):
    """Sequential paging (1-100 then 101-200) is NOT blocked — the second range
    is not a subset of the first."""
    _enable_feature(mod, "read_damper", True)
    f = tmp_path / "p.txt"
    f.write_text("x\n" * 500)
    sid = "s2"
    r1 = mod._pre_tool_call("read_file", {"path": str(f), "offset": 1, "limit": 100}, session_id=sid)
    r2 = mod._pre_tool_call("read_file", {"path": str(f), "offset": 101, "limit": 100}, session_id=sid)
    assert r1 is None and r2 is None, "paging forward must be allowed"


def test_read_damper_resets_per_turn(mod, tmp_path):
    """Read history is reset at the start of each turn (pre_llm_call)."""
    _enable_feature(mod, "read_damper", True)
    f = tmp_path / "t.txt"
    f.write_text("y\n" * 500)
    sid = "s3"
    mod._pre_tool_call("read_file", {"path": str(f), "offset": 1, "limit": 200}, session_id=sid)
    # New turn clears history.
    mod._pre_llm_call(session_id=sid, user_message="next", is_first_turn=False)
    r = mod._pre_tool_call("read_file", {"path": str(f), "offset": 50, "limit": 51}, session_id=sid)
    assert r is None, "after a new turn the same range should be allowed again"


def test_read_damper_off_allows_redundant(mod, tmp_path):
    _enable_feature(mod, "read_damper", False)
    f = tmp_path / "o.txt"
    f.write_text("z\n" * 500)
    sid = "s4"
    mod._pre_tool_call("read_file", {"path": str(f), "offset": 1, "limit": 200}, session_id=sid)
    r = mod._pre_tool_call("read_file", {"path": str(f), "offset": 50, "limit": 51}, session_id=sid)
    assert r is None


# ---------------------------------------------------------------------------
# dry_run feature paths.
# ---------------------------------------------------------------------------


def _enable_dry_run(mod):
    mod._update_state(lambda st: st["features"].__setitem__("dry_run", True))


def test_dry_run_rewrite_does_not_mutate_and_advises(mod):
    """In dry_run, a misnamed arg is reported as a nudge but the call is NOT
    blocked and the advisory stat increments."""
    _enable_dry_run(mod)
    sid = "d1"
    # terminal `cmd` is a misname for `command`.
    args = {"cmd": "ls"}
    r = mod._pre_tool_call("terminal", dict(args), session_id=sid)
    assert r is None, "dry_run must not block the call"
    # The original args must be unchanged (dry_run operates on a copy).
    assert "cmd" in args and "command" not in args
    # A dry_run nudge must have been queued.
    nudges = mod._drain_nudges(sid)
    assert any("dry-run" in n for n in nudges), nudges


def test_dry_run_arg_guard_soft_blocks(mod):
    """In dry_run, a missing-required-arg is reported as a would-block advisory
    and the call still proceeds (returns None)."""
    _enable_dry_run(mod)
    _enable_feature(mod, "arg_guard", True)
    sid = "d2"
    r = mod._pre_tool_call("terminal", {"command": ""}, session_id=sid)
    assert r is None, "dry_run arg_guard must soft-block (return None), not hard-block"
    nudges = mod._drain_nudges(sid)
    assert any("Would block" in n for n in nudges)


def test_dry_run_read_damper_soft_blocks(mod, tmp_path):
    """In dry_run, a redundant read is a would-block advisory, not a hard block."""
    _enable_dry_run(mod)
    _enable_feature(mod, "read_damper", True)
    f = tmp_path / "d.txt"
    f.write_text("a\n" * 500)
    sid = "d3"
    mod._pre_tool_call("read_file", {"path": str(f), "offset": 1, "limit": 200}, session_id=sid)
    r = mod._pre_tool_call("read_file", {"path": str(f), "offset": 50, "limit": 51}, session_id=sid)
    assert r is None
    nudges = mod._drain_nudges(sid)
    assert any("Would block" in n for n in nudges)


def test_dry_run_increments_dry_runs_stat(mod):
    """dry_run paths must increment the `dry_runs` stat, not the hard stat."""
    _enable_dry_run(mod)
    sid = "d4"
    mod._pre_tool_call("terminal", {"cmd": "ls"}, session_id=sid)
    mod._flush_stats()
    stats = mod._load_state()["stats"]
    assert stats.get("dry_runs", 0) >= 1


def test_dry_run_cmd_lint_advises(mod):
    """In dry_run, a terminal command lint is reported but not applied."""
    _enable_dry_run(mod)
    _enable_feature(mod, "command_lint", True)
    sid = "d5"
    args = {"command": "$ ls -la"}
    mod._pre_tool_call("terminal", args, session_id=sid)
    # Original command unchanged in dry_run.
    assert args["command"] == "$ ls -la"
    nudges = mod._drain_nudges(sid)
    assert any("dry-run" in n for n in nudges)
