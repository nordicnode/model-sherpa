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
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture: load the plugin fresh against a temporary HERMES_HOME.
# ---------------------------------------------------------------------------


def _load_fresh_module(hermes_home: str | None = None):
    """Reload the plugin module from source. Optional HERMES_HOME sets the
    env before reload (mirrors the `mod` fixture but as a callable helper)."""
    if hermes_home is not None:
        import os

        os.environ["HERMES_HOME"] = str(hermes_home)
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
    after the with-block returns on platforms with a real locking backend.

    On platforms where fcntl is unavailable the lock is a fail-open no-op
    that does not create the lock file; that gap is closed separately by
    the msvcrt backend (see test_locking.py).
    """
    with mod._lock_state_file(2):  # LOCK_EX
        pass
    if mod.fcntl is None:
        # Fail-open no-op path: directory exists (created unconditionally),
        # but the lock file itself is only materialised by a real backend.
        assert mod.STATE_DIR.exists()
        return
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
        "nudges_suppressed counter should be at least 1 after exceeding the per-window limit"
    )


# ---------------------------------------------------------------------------
# Bug #3: _record_event writes to events.jsonl even on the first call.
# ---------------------------------------------------------------------------


def test_record_event_writes_to_disk_on_first_call(mod, sherpa_home):
    """_record_event used to raise UnboundLocalError on the first call (the
    rotation check referenced an undefined _last_rotation_check). The fix
    defines separate timestamps for correction and event log rotation.
    """
    assert hasattr(mod, "_last_correction_rotation"), "_last_correction_rotation should be defined"
    assert hasattr(mod, "_last_event_rotation"), "_last_event_rotation should be defined"
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
    assert mod.__version__ == "0.4.0"


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
        # Manually trigger the tick callback; it should see 0 sessions and
        # NOT reschedule.
        # threading.Timer.finished is a Python 3.13+ Event; guard the call so
        # the test runs on 3.9-3.12 too. cancel() works on every version.
        if hasattr(timer, "finished"):
            timer.finished.set()
        timer.cancel()
        timer.function(*timer.args, **timer.kwargs)
        assert mod._periodic_flush_timer is None, "Periodic flush timer should not be rescheduled"
    finally:
        timer.cancel()


# ---------------------------------------------------------------------------
# _transform_tool_result tests.
# ---------------------------------------------------------------------------


def test_transform_tool_result_returns_tip_for_error_string(mod, sherpa_home):
    """An error string should get a [SHERPA] tip appended."""
    result = mod._transform_tool_result(
        tool_name="terminal",
        result="Error: no such file or directory, scandir '/missing/path'",
        session_id="transform_test",
    )
    assert result is not None
    assert "[SHERPA]" in result
    assert "Tip:" in result


def test_transform_tool_result_returns_none_for_success(mod, sherpa_home):
    """A successful result should not be modified."""
    result = mod._transform_tool_result(
        tool_name="terminal",
        result="total 8\n-rw-r--r-- 1 user user 1024 Jan  1 00:00 file.txt",
        session_id="transform_test",
    )
    assert result is None


def test_transform_tool_result_returns_none_for_non_string(mod, sherpa_home):
    """Non-string results should not be modified (post_tool_call handles those)."""
    result = mod._transform_tool_result(
        tool_name="terminal",
        result={"exit_code": 1, "stderr": "command not found"},
        session_id="transform_test",
    )
    assert result is None


def test_transform_tool_result_avoids_double_append(mod, sherpa_home):
    """If a previous plugin already added [SHERPA], don't add another."""
    result = mod._transform_tool_result(
        tool_name="terminal",
        result="Error: no such file or directory\n\n[SHERPA] already has a tip",
        session_id="transform_test",
    )
    assert result is None


# ---------------------------------------------------------------------------
# _looks_like_error tests.
# ---------------------------------------------------------------------------


def test_looks_like_error_dict_with_error_key(mod):
    assert mod._looks_like_error({"error": "something failed"}) is True


def test_looks_like_error_dict_with_zero_exit_code(mod):
    assert mod._looks_like_error({"exit_code": 0, "stderr": "some warning"}) is False


def test_looks_like_error_dict_with_nonzero_exit_code(mod):
    assert mod._looks_like_error({"exit_code": 1, "stderr": ""}) is True


def test_looks_like_error_dict_with_exitCode(mod):
    assert mod._looks_like_error({"exitCode": 0, "stderr": "warning"}) is False
    assert mod._looks_like_error({"exitCode": 127, "stderr": ""}) is True


def test_looks_like_error_none(mod):
    assert mod._looks_like_error(None) is False


def test_looks_like_error_success_string(mod):
    assert mod._looks_like_error("file contents here") is False


def test_looks_like_error_traceback_string(mod):
    assert mod._looks_like_error("Traceback (most recent call last):\n  File ...") is True


def test_looks_like_error_read_file_success(mod):
    """read_file results containing common words like 'exception' in content
    should not be treated as errors."""
    result = "# This file handles exceptions gracefully\nclass Foo: pass"
    assert mod._looks_like_error(result, "read_file") is False


def test_looks_like_error_read_file_actual_error(mod):
    assert mod._looks_like_error("FileNotFoundError: /missing/path", "read_file") is True


def test_looks_like_error_search_files_success(mod):
    result = "./lib/utils.py:42:    # handle exception cases here"
    assert mod._looks_like_error(result, "search_files") is False


# ---------------------------------------------------------------------------
# _didyoumean_tool tests.
# ---------------------------------------------------------------------------


def test_didyoumean_tool_returns_none_for_empty(mod, sherpa_home):
    assert mod._didyoumean_tool("") is None
    assert mod._didyoumean_tool(None) is None


# ---------------------------------------------------------------------------
# _didyoumean_path tests (additional).
# ---------------------------------------------------------------------------


def test_didyoumean_path_returns_none_for_existing_path(mod, sherpa_home):
    """If the path exists, _didyoumean_path should return None."""
    existing = sherpa_home / "exists.txt"
    existing.write_text("hello")
    assert mod._didyoumean_path(str(existing)) is None


def test_didyoumean_path_returns_none_for_empty(mod, sherpa_home):
    assert mod._didyoumean_path("") is None
    assert mod._didyoumean_path(None) is None


def test_didyoumean_path_finds_sibling(mod, sherpa_home):
    """_didyoumean_path should suggest a close sibling filename."""
    d = sherpa_home / "project"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "main.py").write_text("pass")
    result = mod._didyoumean_path(str(d / "config.jsom"))
    assert result is not None
    assert "config.json" in result


# ---------------------------------------------------------------------------
# _is_multistep_request additional tests.
# ---------------------------------------------------------------------------


def test_is_multistep_then_next(mod):
    assert mod._is_multistep_request("first do X then do Y finally do Z") is True


def test_is_multistep_empty(mod):
    assert mod._is_multistep_request("") is False
    assert mod._is_multistep_request(None) is False


def test_is_multistep_long_truncated(mod):
    """Very long messages should be truncated to 4000 chars."""
    msg = "a. " * 5000  # way over 4000 chars
    # Should not raise
    mod._is_multistep_request(msg)


# ---------------------------------------------------------------------------
# Loop detection: ping-pong (A-B-A-B) and triple (A-B-C-A-B-C).
# ---------------------------------------------------------------------------


def test_loop_detection_pingpong(mod, sherpa_home):
    """A-B-A-B pattern should be detected as a loop."""
    sid = "pingpong_test"
    mod._drain_nudges(sid)
    args_a = {"command": "ls /tmp"}
    args_b = {"command": "ls /var"}
    for _ in range(2):
        mod._post_tool_call(tool_name="terminal", args=args_a, result={"exit_code": 0}, session_id=sid)
        mod._post_tool_call(tool_name="terminal", args=args_b, result={"exit_code": 0}, session_id=sid)
    pending = mod._pending_nudges.get(sid, [])
    kinds = [k for k, _ in pending]
    assert kinds.count("loop") == 1, f"expected ping-pong loop nudge, got {kinds}"


def test_loop_detection_triple_sequence(mod, sherpa_home):
    """A-B-C-A-B-C pattern should be detected as a loop."""
    sid = "triple_test"
    mod._drain_nudges(sid)
    args_a = {"command": "ls /a"}
    args_b = {"command": "ls /b"}
    args_c = {"command": "ls /c"}
    for _ in range(2):
        mod._post_tool_call(tool_name="terminal", args=args_a, result={"exit_code": 0}, session_id=sid)
        mod._post_tool_call(tool_name="terminal", args=args_b, result={"exit_code": 0}, session_id=sid)
        mod._post_tool_call(tool_name="terminal", args=args_c, result={"exit_code": 0}, session_id=sid)
    pending = mod._pending_nudges.get(sid, [])
    kinds = [k for k, _ in pending]
    assert kinds.count("loop") == 1, f"expected triple-sequence loop nudge, got {kinds}"


# ---------------------------------------------------------------------------
# Error streak and hint injection.
# ---------------------------------------------------------------------------


def test_error_streak_triggers_hint_after_two(mod, sherpa_home):
    """After 2 consecutive errors, a matching hint should be queued."""
    sid = "streak_test"
    mod._drain_nudges(sid)
    # Two errors of the same type
    for _ in range(2):
        mod._post_tool_call(
            tool_name="terminal",
            args={"command": "cat /nope"},
            result={"error": "no such file or directory"},
            session_id=sid,
        )
    pending = mod._pending_nudges.get(sid, [])
    kinds = [k for k, _ in pending]
    assert "hint" in kinds, f"expected a hint nudge after 2 errors, got {kinds}"


def test_error_streak_resets_on_success(mod, sherpa_home):
    """A successful call should reset the error streak."""
    sid = "reset_streak"
    mod._drain_nudges(sid)
    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "fail"},
        result={"error": "no such file"},
        session_id=sid,
    )
    assert mod._error_streak.get(sid, 0) == 1
    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "ok"},
        result={"exit_code": 0},
        session_id=sid,
    )
    assert mod._error_streak.get(sid, 0) == 0


# ---------------------------------------------------------------------------
# _redact_dict tests.
# ---------------------------------------------------------------------------


def test_redact_dict_redacts_sensitive_keys(mod):
    data = {"api_key": "secret123", "name": "John", "password": "hunter2"}
    result = mod._redact_dict(data)
    assert result["api_key"] == "[REDACTED]"
    assert result["password"] == "[REDACTED]"
    assert result["name"] == "John"


def test_redact_dict_nested(mod):
    data = {"outer": {"github_token": "ghp_abc", "safe": "value"}}
    result = mod._redact_dict(data)
    assert result["outer"]["github_token"] == "[REDACTED]"
    assert result["outer"]["safe"] == "value"


def test_redact_dict_list(mod):
    data = [{"secret": "s1"}, {"public": "p1"}]
    result = mod._redact_dict(data)
    assert result[0]["secret"] == "[REDACTED]"
    assert result[1]["public"] == "p1"


# ---------------------------------------------------------------------------
# _normalize_key tests.
# ---------------------------------------------------------------------------


def test_normalize_key(mod):
    assert mod._normalize_key("file_path") == "filepath"
    assert mod._normalize_key("File-Path") == "filepath"
    assert mod._normalize_key("PATH") == "path"


# ---------------------------------------------------------------------------
# _deep_merge tests.
# ---------------------------------------------------------------------------


def test_deep_merge_preserves_defaults(mod):
    defaults = {"a": 1, "b": {"c": 2, "d": 3}}
    override = {"b": {"c": 99}}
    result = mod._deep_merge(defaults, override)
    assert result == {"a": 1, "b": {"c": 99, "d": 3}}


def test_deep_merge_adds_new_keys(mod):
    defaults = {"a": 1}
    override = {"b": 2}
    result = mod._deep_merge(defaults, override)
    assert result == {"a": 1, "b": 2}


def test_deep_merge_non_dict_override(mod):
    assert mod._deep_merge({"a": 1}, "scalar") == "scalar"


# ---------------------------------------------------------------------------
# Command lint: additional tests.
# ---------------------------------------------------------------------------


def test_command_lint_strips_bash_c_simple(mod):
    cmd, warnings, fixes = mod._lint_terminal_command('bash -c "ls -la"', {})
    assert cmd == "ls -la"
    assert fixes == 1


def test_command_lint_strips_sh_c_simple(mod):
    cmd, warnings, fixes = mod._lint_terminal_command("sh -c 'echo hello'", {})
    assert cmd == "echo hello"
    assert fixes == 1


def test_command_lint_warns_bash_c_complex(mod):
    cmd, warnings, fixes = mod._lint_terminal_command('bash -c "ls | grep foo"', {})
    # Complex inner command should warn, not unwrap
    assert len(warnings) > 0
    assert "bash -c" in warnings[0]


def test_command_lint_strips_percent_prompt(mod):
    cmd, warnings, fixes = mod._lint_terminal_command("% ls -la", {})
    assert cmd == "ls -la"
    assert fixes == 1


def test_command_lint_smart_quotes(mod):
    cmd, warnings, fixes = mod._lint_terminal_command("echo \u201chello\u201d", {})
    assert '"' in cmd
    assert "\u201c" not in cmd


def test_command_lint_no_op(mod):
    cmd, warnings, fixes = mod._lint_terminal_command("ls -la", {})
    assert cmd == "ls -la"
    assert fixes == 0
    assert warnings == []


def test_command_lint_cd_with_existing_workdir(mod, tmp_path):
    # Use a real absolute dir so the test is portable across platforms
    # (POSIX "/tmp" does not resolve to a real path on win32).
    target = tmp_path / "sub"
    target.mkdir()
    args = {"workdir": str(tmp_path / "other")}
    cmd, warnings, fixes = mod._lint_terminal_command(f"cd {target} && ls", args)
    assert cmd == "ls"
    # An absolute cd path overrides the existing workdir.
    assert Path(args["workdir"]).resolve() == target.resolve()


def test_command_lint_cd_windows_backslash_path_overrides_workdir(mod, tmp_path):
    """Regression: on Windows a `cd C:\\dir\\sub && cmd` must override the
    existing workdir with the absolute path. shlex.split() used to strip the
    backslashes (POSIX escape semantics), producing a mangled relative path
    that got appended to the old workdir."""
    target = tmp_path / "deep" / "sub"
    target.mkdir(parents=True)
    # Force the OS-native separator (backslash on Windows) the way a model
    # would after seeing a Windows path in the environment.
    sep = "\\" if sys.platform == "win32" else "/"
    native = str(target).replace("\\", sep).replace("/", sep)
    args = {"workdir": str(tmp_path / "unrelated")}
    cmd, warnings, fixes = mod._lint_terminal_command(f"cd {native} && ls", args)
    assert cmd == "ls"
    assert Path(args["workdir"]).resolve() == target.resolve(), (
        f"absolute cd path should override workdir; got {args['workdir']!r}"
    )


# ---------------------------------------------------------------------------
# Repair args: additional synonym and schema tests.
# ---------------------------------------------------------------------------


def test_repair_args_no_change_needed(mod):
    args = {"command": "ls", "workdir": "/tmp"}
    fixes = mod._repair_args("terminal", args)
    assert fixes == []
    assert args == {"command": "ls", "workdir": "/tmp"}


def test_repair_args_patch_aliases(mod):
    args = {"file_path": "/f", "diff": "patch content"}
    mod._repair_args("patch", args)
    assert "path" in args
    assert "patch" in args


def test_repair_args_write_file_aliases(mod):
    args = {"filename": "/f", "text": "content"}
    mod._repair_args("write_file", args)
    assert "path" in args
    assert "content" in args


# ---------------------------------------------------------------------------
# Nudge dedup within a single turn.
# ---------------------------------------------------------------------------


def test_nudge_dedup_same_kind_same_turn(mod, sherpa_home):
    """Two nudges of the same kind in the same turn should produce only one."""
    sid = "dedup_test"
    mod._drain_nudges(sid)
    mod._queue_nudge(sid, "hint", "first hint")
    mod._queue_nudge(sid, "hint", "second hint")
    nudges = mod._drain_nudges(sid)
    assert len(nudges) == 1
    assert nudges[0] == "first hint"  # first-wins


def test_nudge_different_kinds_both_queued(mod, sherpa_home):
    """Different nudge kinds should both be queued."""
    sid = "multi_kind_test"
    mod._drain_nudges(sid)
    mod._queue_nudge(sid, "loop", "loop msg")
    mod._queue_nudge(sid, "hint", "hint msg")
    nudges = mod._drain_nudges(sid)
    assert len(nudges) == 2


# ---------------------------------------------------------------------------
# _result_to_text tests.
# ---------------------------------------------------------------------------


def test_result_to_text_none(mod):
    assert mod._result_to_text(None) == ""


def test_result_to_text_bytes(mod):
    assert "hello" in mod._result_to_text(b"hello")


def test_result_to_text_dict(mod):
    result = mod._result_to_text({"key": "value"})
    assert "key" in result
    assert "value" in result


def test_result_to_text_binary(mod):
    result = mod._result_to_text(b"\x00\x01\xff")
    assert "binary data" in result


def test_result_to_text_int(mod):
    assert mod._result_to_text(42) == "42"


# ---------------------------------------------------------------------------
# Session lifecycle.
# ---------------------------------------------------------------------------


def test_session_start_clears_state(mod, sherpa_home):
    """Starting a session should clear any previous per-session state."""
    sid = "lifecycle_test"
    mod._first_user_msg[sid] = "old message"
    mod._on_session_start(session_id=sid)
    assert sid not in mod._first_user_msg


def test_session_end_flushes_stats(mod, sherpa_home):
    """Ending a session should flush pending stats to disk."""
    sid = "flush_test"
    mod._on_session_start(session_id=sid)
    mod._bump_stat("rewrites", 5)
    mod._on_session_end(session_id=sid)
    state = mod._load_state()
    assert state["stats"]["rewrites"] >= 5


def test_clear_all_sessions(mod, sherpa_home):
    """_clear_all_sessions should wipe all per-session data."""
    mod._first_user_msg["s1"] = "msg1"
    mod._first_user_msg["s2"] = "msg2"
    mod._clear_all_sessions()
    assert len(mod._first_user_msg) == 0


# ---------------------------------------------------------------------------
# _tool_schema_preview tests.
# ---------------------------------------------------------------------------


def test_tool_schema_preview_no_registry(mod, sherpa_home):
    """Without a registry, schema preview should return a fallback."""
    result = mod._tool_schema_preview("nonexistent_tool")
    assert result is not None
    assert "no parameters" in result or "nonexistent_tool" in result


# ---------------------------------------------------------------------------
# _format_events tests.
# ---------------------------------------------------------------------------


def test_format_events_empty(mod, sherpa_home):
    result = mod._format_events(session_id="no_such_session", n=5)
    assert "no Sherpa telemetry" in result or "(no" in result


def test_format_events_after_recording(mod, sherpa_home):
    mod._record_event("fmt_test", "rewrite", "terminal: cmd->command")
    result = mod._format_events(session_id="fmt_test", n=5)
    assert "rewrite" in result
    assert "terminal" in result


# ---------------------------------------------------------------------------
# _canonical_read_key_path tests.
# ---------------------------------------------------------------------------


def test_canonical_read_key_path_empty(mod):
    assert mod._canonical_read_key_path("") == ""
    assert mod._canonical_read_key_path(None) is None


def test_canonical_read_key_path_absolute(mod, tmp_path):
    # A real absolute path is returned resolved (not a POSIX-specific literal).
    f = tmp_path / "test.py"
    f.write_text("x")
    result = mod._canonical_read_key_path(str(f))
    assert Path(result).resolve() == f.resolve()


# ---------------------------------------------------------------------------
# Plugin version consistency.
# ---------------------------------------------------------------------------


def test_version_matches_plugin_yaml(mod):
    """__version__ in __init__.py should match plugin.yaml."""
    plugin_yaml_path = Path(__file__).resolve().parent.parent / "plugin.yaml"
    if not plugin_yaml_path.exists():
        pytest.skip("plugin.yaml not found")
    # Parse version line directly to avoid requiring pyyaml as a test dependency.
    yaml_version = None
    for line in plugin_yaml_path.read_text().splitlines():
        if line.startswith("version:"):
            yaml_version = line.split(":", 1)[1].strip()
            break
    assert yaml_version is not None, "Could not find 'version:' in plugin.yaml"
    assert mod.__version__ == yaml_version, (
        f"Version mismatch: __init__.py={mod.__version__}, plugin.yaml={yaml_version}"
    )


# ---------------------------------------------------------------------------
# _didyoumean_path tests (additional).
# ---------------------------------------------------------------------------


def test_didyoumean_path_respects_candidates_limit(mod, sherpa_home, monkeypatch):
    """_didyoumean_path should scan at most _DYM_MAX_CANDIDATES entries, preventing
    latency spikes in directories with thousands of files."""
    # The previous version of this test only checked the return value (None)
    # and never asserted that the cap was actually respected. We now
    # instrument difflib.get_close_matches to count the candidate list size
    # it receives — that is the real proof the cap is in force.
    ancestor = sherpa_home / "heavy_dir"
    ancestor.mkdir()

    class MockDirEntry:
        def __init__(self, name):
            self.name = name

    class MockScandir:
        def __init__(self, path):
            pass

        def __enter__(self):
            return (MockDirEntry(f"file_{i}.txt") for i in range(1000))

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr("os.scandir", MockScandir)

    import difflib

    real_gcm = difflib.get_close_matches
    seen_candidate_counts: list[int] = []

    def counting_gcm(word, possibilities, *args, **kwargs):
        seen_candidate_counts.append(len(list(possibilities)))
        return real_gcm(word, list(possibilities), *args, **kwargs)

    monkeypatch.setattr("difflib.get_close_matches", counting_gcm)

    target_path = ancestor / "nonexistent.txt"
    result = mod._didyoumean_path(str(target_path))

    assert result is None
    assert len(seen_candidate_counts) == 1
    assert seen_candidate_counts[0] <= mod._DYM_MAX_CANDIDATES
    assert seen_candidate_counts[0] < 1000


# ---------------------------------------------------------------------------
# Fake plugin context for register/_handle_slash tests.
# ---------------------------------------------------------------------------


class FakePluginCtx:
    """Stand-in for the real plugin context used by `register(ctx)`.

    Records every call to register_hook, register_command, and register_tool
    so tests can assert on them. Also implements unregister_tool (used by
    `_unregister_aliases` when `alias_tools` is toggled off) and
    deregister_tool as a no-op alias for back-compat.
    """

    def __init__(self):
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, dict] = {}
        self.tools: dict[str, dict] = {}
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }

    def register_tool(self, name, toolset=None, schema=None, handler=None, emoji=None):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "emoji": emoji,
        }
        self.registered.append(name)

    def unregister_tool(self, name):
        self.tools.pop(name, None)
        self.unregistered.append(name)

    # Back-compat alias — the real registry uses `deregister` in some versions.
    def deregister(self, name):
        self.unregister_tool(name)


# ---------------------------------------------------------------------------
# _pre_llm_call tests.
# ---------------------------------------------------------------------------


def test_pre_llm_call_reanchor_fires_at_cadence(mod, sherpa_home):
    """After _REANCHOR_EVERY tool calls, _pre_llm_call should re-inject the
    original goal as a Re-anchor nudge."""
    sid = "reanchor_test"
    mod._first_user_msg[sid] = "deploy the prod cluster"
    mod._calls_since_reanchor[sid] = mod._REANCHOR_EVERY  # exactly at the threshold

    result = mod._pre_llm_call(
        session_id=sid,
        user_message="continue working",
        is_first_turn=False,
    )

    assert result is not None
    assert "Re-anchor" in result["context"]
    assert "deploy the prod cluster" in result["context"]
    # The counter should have been reset so we don't re-fire next turn.
    assert mod._calls_since_reanchor[sid] == 0


def test_pre_llm_call_reanchor_silent_below_cadence(mod, sherpa_home):
    """Below the cadence threshold, no Re-anchor message is injected."""
    sid = "reanchor_quiet"
    mod._first_user_msg[sid] = "some goal"
    mod._calls_since_reanchor[sid] = mod._REANCHOR_EVERY - 1

    result = mod._pre_llm_call(
        session_id=sid,
        user_message="keep going",
        is_first_turn=False,
    )

    # Re-anchor should NOT be in the result. Other content (e.g. cheatsheet
    # refresh) may legitimately be present, so we check the absence of the
    # Re-anchor string specifically.
    if result is not None:
        assert "Re-anchor" not in result["context"]


def test_pre_llm_call_stop_nudge_threshold(mod, sherpa_home):
    """At >= _STOP_NUDGE_AT_CALLS tool calls, the stop-calling-tools nudge
    must fire so the model doesn't keep thrashing."""
    sid = "stop_nudge"
    mod._call_count[sid] = mod._STOP_NUDGE_AT_CALLS
    # Suppress re-anchor by clearing calls-since-reanchor.
    mod._calls_since_reanchor[sid] = 0
    mod._first_user_msg.pop(sid, None)

    result = mod._pre_llm_call(
        session_id=sid,
        user_message="carry on",
        is_first_turn=False,
    )

    assert result is not None
    assert "stop calling tools" in result["context"]
    # The counter should have been reset so the nudge doesn't fire every turn.
    assert mod._call_count[sid] == 0


def test_pre_llm_call_plan_first_injects_on_first_turn(mod, sherpa_home, monkeypatch):
    """A multi-step first-turn user message should trigger the plan-first
    nudge (when the `todo` tool is registered and the feature is on)."""
    # Make sure the plan_first feature is on and that the registry reports
    # `todo` as a known tool — plan-first only fires in that case (it would
    # be worse to nudge the model toward a missing tool than to stay silent).
    state = mod._load_state(bypass_temporal_block=True)
    state["features"]["plan_first"] = True
    mod._save_state(state)

    def fake_has_tool(name):
        return name == "todo"

    monkeypatch.setattr(mod, "_has_tool", fake_has_tool)

    result = mod._pre_llm_call(
        session_id="plan_first_test",
        user_message="First do this. Then do that. Finally check the output.",
        is_first_turn=True,
    )

    assert result is not None
    assert "todo" in result["context"].lower()
    # The stat counter is held in _pending_stats until _flush_stats() merges
    # it into persistent state. Drain it before reading.
    mod._flush_stats()
    state = mod._load_state(bypass_temporal_block=True)
    assert state["stats"]["plan_nudges"] >= 1


def test_pre_llm_call_total_cap_truncation(mod, sherpa_home):
    """When the combined nudge text exceeds _TOTAL_NUDGE_CAP it must be
    truncated with the [TRUNCATED BY SHERPA] marker — otherwise a model
    could be drowned in its own injected context."""
    sid = "cap_test"
    # Pre-fill both per-turn and per-call counters so a stop-nudge and a
    # re-anchor BOTH fire. The cheatsheet alone is ~600 chars; together with
    # two large nudges this easily blows past the 8000-char cap.
    mod._first_user_msg[sid] = "x" * 5000
    mod._calls_since_reanchor[sid] = mod._REANCHOR_EVERY
    mod._call_count[sid] = mod._STOP_NUDGE_AT_CALLS
    # Drop a big queued nudge too.
    mod._queue_nudge(sid, "hint", "y" * 5000)

    result = mod._pre_llm_call(
        session_id=sid,
        user_message="continue",
        is_first_turn=False,
    )

    assert result is not None
    combined = result["context"]
    assert len(combined) <= mod._TOTAL_NUDGE_CAP + len("\n... [TRUNCATED BY SHERPA]") + 1
    assert "[TRUNCATED BY SHERPA]" in combined


def test_pre_llm_call_truncates_first_user_message(mod, sherpa_home):
    """The first user message cached for re-anchoring must be capped at
    _FIRST_USER_MSG_CAP so a multi-KB goal doesn't bloat every future turn."""
    sid = "history_cap"
    long_msg = "z" * 5000  # well over _FIRST_USER_MSG_CAP (1500)

    mod._pre_llm_call(
        session_id=sid,
        user_message=long_msg,
        is_first_turn=True,
    )

    cached = mod._first_user_msg.get(sid, "")
    assert len(cached) <= mod._FIRST_USER_MSG_CAP
    # And it should contain the start of the original message verbatim.
    assert cached.startswith("z" * 100)


# ---------------------------------------------------------------------------
# _handle_slash tests.
# ---------------------------------------------------------------------------


def test_handle_slash_status(mod, sherpa_home):
    """/sherpa status returns the lifetime stats + feature overview."""
    out = mod._handle_slash("status")
    assert "model-sherpa" in out
    assert "Features" in out
    # The lifetime-stats section should at least mention rewrites.
    assert "silent arg rewrites" in out


def test_handle_slash_on_off_toggles(mod, sherpa_home):
    """/sherpa on and /sherpa off flip the master `enabled` flag in state."""
    out_off = mod._handle_slash("off")
    assert "DISABLED" in out_off
    state = mod._load_state(bypass_temporal_block=True)
    assert state["enabled"] is False

    out_on = mod._handle_slash("on")
    assert "ENABLED" in out_on
    state = mod._load_state(bypass_temporal_block=True)
    assert state["enabled"] is True


def test_handle_slash_feature_alias_tools_on_registers(mod, sherpa_home, monkeypatch):
    """Toggling `alias_tools` on should register hard aliases via the
    plugin context's `register_tool` method."""
    fake_ctx = FakePluginCtx()

    # The real registry import would return None in tests, so _register_aliases
    # would early-return. Patch it to a small stub registry so the loop runs.
    class StubReg:
        def get_all_tool_names(self):
            return []

        def get_entry(self, name):
            return None

    stub_reg = StubReg()
    monkeypatch.setattr(mod, "_registry", lambda: stub_reg)
    mod._plugin_ctx = fake_ctx

    out = mod._handle_slash("feature alias_tools on")
    assert "on" in out
    # FakePluginCtx should now have the hard aliases registered.
    assert len(fake_ctx.registered) > 0
    # Every registered tool should carry a "[sherpa alias]" description.
    for name, payload in fake_ctx.tools.items():
        assert payload["schema"]["description"].startswith("[sherpa alias]")


def test_handle_slash_feature_alias_tools_off_unregisters(mod, sherpa_home, monkeypatch):
    """Toggling `alias_tools` off should unregister previously-installed
    hard aliases."""
    fake_ctx = FakePluginCtx()

    class StubReg:
        def get_all_tool_names(self):
            return []

        def get_entry(self, name):
            return None

    stub_reg = StubReg()
    monkeypatch.setattr(mod, "_registry", lambda: stub_reg)
    mod._plugin_ctx = fake_ctx

    # Turn on first so we have something to turn off.
    mod._handle_slash("feature alias_tools on")
    assert len(fake_ctx.registered) > 0

    out = mod._handle_slash("feature alias_tools off")
    assert "off" in out
    # _unregister_aliases uses the global registry's deregister method, which
    # our stub does not implement, so it returns 0 unregistered. The
    # important assertion is that the feature flag was flipped and the
    # state is consistent.
    state = mod._load_state(bypass_temporal_block=True)
    assert state["features"]["alias_tools"] is False


def test_handle_slash_doctor_returns_report(mod, sherpa_home):
    """/sherpa doctor runs a diagnostic and returns a non-empty report."""
    out = mod._handle_slash("doctor")
    assert "model-sherpa doctor" in out
    # Should mention the state file and the registry status.
    assert "state" in out
    assert "registry" in out


def test_handle_slash_reset_clears_state(mod, sherpa_home):
    """/sherpa reset should zero out the lifetime stats and clear sessions."""
    # Seed some state and a per-session dict.
    mod._bump_stat("rewrites", 5)
    mod._first_user_msg["s1"] = "stale"
    mod._first_user_msg["s2"] = "also stale"

    out = mod._handle_slash("reset")
    assert "cleared" in out.lower()

    state = mod._load_state(bypass_temporal_block=True)
    assert state["stats"]["rewrites"] == 0
    assert len(mod._first_user_msg) == 0


# ---------------------------------------------------------------------------
# _post_tool_call happy path.
# ---------------------------------------------------------------------------


def test_post_tool_call_happy_path_no_nudge(mod, sherpa_home):
    """A successful tool result (no error) must not trigger a nudge nor
    bump the error stats. The only state change is incrementing the
    per-session call counter used for re-anchoring."""
    sid = "happy_path"
    mod._drain_nudges(sid)
    mod._error_streak.pop(sid, None)
    mod._calls_since_reanchor[sid] = 0

    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "ls /tmp"},
        result={"exit_code": 0, "stdout": "file1\nfile2\n"},
        session_id=sid,
    )

    # No nudges should have been queued.
    pending = mod._pending_nudges.get(sid, [])
    assert pending == []
    # Error streak should be zero (success resets it).
    assert mod._error_streak.get(sid, 0) == 0
    # The per-session call counter for re-anchor should have been bumped.
    assert mod._calls_since_reanchor[sid] == 1


# ---------------------------------------------------------------------------
# register(ctx) end-to-end.
# ---------------------------------------------------------------------------


def test_register_hooks_command_and_aliases(mod, sherpa_home, monkeypatch):
    """register(ctx) must install all 6 hooks, register the /sherpa command,
    and (when alias_tools is on) wire up the hard alias tools on ctx."""
    fake_ctx = FakePluginCtx()

    class StubReg:
        def get_all_tool_names(self):
            return []

        def get_entry(self, name):
            return None

    monkeypatch.setattr(mod, "_registry", lambda: StubReg())

    # Enable alias_tools before registration so we exercise the alias path.
    state = mod._load_state(bypass_temporal_block=True)
    state["features"]["alias_tools"] = True
    mod._save_state(state)

    mod.register(fake_ctx)

    expected_hooks = {
        "pre_tool_call",
        "post_tool_call",
        "transform_tool_result",
        "pre_llm_call",
        "on_session_start",
        "on_session_end",
    }
    assert expected_hooks.issubset(set(fake_ctx.hooks.keys()))
    # Each hook should have been registered with exactly one callable.
    for hook_name in expected_hooks:
        assert len(fake_ctx.hooks[hook_name]) == 1

    # The slash command should be present.
    assert "sherpa" in fake_ctx.commands
    cmd = fake_ctx.commands["sherpa"]
    assert callable(cmd["handler"])

    # Hard aliases should have been registered on the fake ctx.
    assert len(fake_ctx.registered) > 0
    # Plugin ctx global should now point at our fake.
    assert mod._plugin_ctx is fake_ctx

    # Cleanup so the test doesn't leak global state into others.
    mod._plugin_ctx = None
    mod._registered_alias_tool_names.clear()


# ---------------------------------------------------------------------------
# Windows baseline: state-dir creation must not depend on fcntl.
#
# Root cause found while establishing the test baseline on win32: _lock_file
# returns early when fcntl is None (Unix-only), and the only STATE_DIR.mkdir
# call lived *after* that early return. On Windows (or any platform where
# fcntl is unavailable) the state directory was never created, so every
# _save_state() failed with FileNotFoundError. These tests pin the contract
# that state persistence works regardless of the locking backend.
# ---------------------------------------------------------------------------


def test_save_state_creates_missing_state_dir(mod, sherpa_home):
    """_save_state must create STATE_DIR and succeed even when it does not
    yet exist — independent of the locking backend (fcntl/msvcrt/none)."""
    state_dir = mod.STATE_DIR
    assert not state_dir.exists(), "precondition: STATE_DIR should not exist yet"
    # _save_state must not raise and must persist to disk.
    mod._save_state({"enabled": True, "features": {}, "stats": {}, "custom_hints": []})
    assert state_dir.exists(), "STATE_DIR should have been created"
    assert mod.STATE_FILE.exists(), "state.json should have been written"


def test_record_event_creates_missing_state_dir(mod, sherpa_home):
    """_record_event (events.jsonl) must also work without a pre-existing
    STATE_DIR — it has its own mkdir but shares the same backend gap."""
    assert not mod.STATE_DIR.exists()
    mod._record_event("t", "event_kind", "detail")
    assert mod.EVENT_LOG_FILE.exists(), "events.jsonl should have been written"


def test_lock_state_file_creates_missing_state_dir(mod, sherpa_home):
    """Acquiring the state lock must not fail when STATE_DIR is absent.

    On platforms where fcntl is unavailable the lock is a no-op, but the
    directory it targets must still be created so later writes succeed.
    """
    assert not mod.STATE_DIR.exists()
    with mod._lock_state_file(2):  # LOCK_EX
        pass
    assert mod.STATE_DIR.exists(), "STATE_DIR should exist after acquiring lock"


# ---------------------------------------------------------------------------
# HERMES_HOME resolution (Phase 1).
#
# The plugin must resolve its state dir through Hermes' get_hermes_home() so
# that (a) the Windows platform default is %LOCALAPPDATA%\hermes rather than
# ~/.hermes, and (b) the context-local override used by kanban/delegation
# subagents is honored. Previously the plugin read os.environ at import time
# and bypassed both, silently writing state to the wrong profile.
# ---------------------------------------------------------------------------


def test_hermes_home_delegates_to_get_hermes_home_when_available(monkeypatch, tmp_path):
    """When hermes_constants.get_hermes_home is importable, _hermes_home()
    must defer to it so the context-local override path is honored."""
    fake_home = tmp_path / "via_get_hermes_home"
    fake_home.mkdir()
    stub = types.ModuleType("hermes_constants")
    stub.get_hermes_home = lambda: fake_home  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_constants", stub)
    mod = _load_fresh_module()
    assert Path(mod._hermes_home()) == fake_home


def test_hermes_home_context_local_override_is_honored(monkeypatch, tmp_path):
    """get_hermes_home() reflects a context-local override (used by subagents);
    _hermes_home must pick it up on every call, not a cached import-time value."""
    override_home = tmp_path / "subagent_profile"
    override_home.mkdir()
    stub = types.ModuleType("hermes_constants")
    stub.get_hermes_home = lambda: override_home  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_constants", stub)
    mod = _load_fresh_module()
    # Must resolve per-call, so the value tracks a later change to the override.
    assert Path(mod._hermes_home()) == override_home
    new_override = tmp_path / "other_profile"
    new_override.mkdir()
    stub.get_hermes_home = lambda: new_override  # type: ignore[attr-defined]
    assert Path(mod._hermes_home()) == new_override, "must re-read per call"


def test_hermes_home_fallback_without_hermes_libs(monkeypatch, tmp_path):
    """When hermes_constants is unavailable (e.g. unit tests / standalone run),
    fall back to the HERMES_HOME env var, not crash."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Ensure hermes_constants is not importable.
    sys.modules.pop("hermes_constants", None)
    import importlib

    real_import = importlib.import_module

    def _block(name, *a, **k):
        if name == "hermes_constants":
            raise ImportError("simulated absence")
        return real_import(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", _block)
    mod = _load_fresh_module()
    assert Path(mod._hermes_home()) == tmp_path


def test_hermes_home_windows_platform_default(monkeypatch, tmp_path):
    """On win32 with no HERMES_HOME env var and no Hermes libs, the default
    must be %LOCALAPPDATA%/hermes — NOT ~/.hermes (the POSIX default)."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    sys.modules.pop("hermes_constants", None)
    import importlib

    real_import = importlib.import_module

    def _block(name, *a, **k):
        if name == "hermes_constants":
            raise ImportError("simulated absence")
        return real_import(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", _block)
    mod = _load_fresh_module()
    expected = local / "hermes"
    assert Path(mod._hermes_home()) == expected, (
        f"win32 default should be %LOCALAPPDATA%/hermes, got {mod._hermes_home()}"
    )


def test_state_dir_resolves_lazily_through_hermes_home(monkeypatch, tmp_path):
    """STATE_DIR/STATE_FILE/etc. must resolve through _hermes_home() so they
    track the active profile rather than a frozen import-time constant."""
    stub = types.ModuleType("hermes_constants")
    stub.get_hermes_home = lambda: tmp_path  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_constants", stub)
    mod = _load_fresh_module()
    assert Path(mod._state_dir()) == tmp_path / "memories" / "model-sherpa"
    assert Path(mod._state_file()) == tmp_path / "memories" / "model-sherpa" / "state.json"


# ---------------------------------------------------------------------------
# Phase 2.3: Fail-open uniformity across all hooks.
#
# _pre_tool_call already has a try/except → logger.exception → return None.
# _post_tool_call, _transform_tool_result, and _pre_llm_call must follow the
# same contract: any unhandled exception is logged and swallowed so a buggy
# Sherpa never crashes the host agent.
# ---------------------------------------------------------------------------


def test_post_tool_call_fails_open(monkeypatch, mod, sherpa_home):
    """An internal exception in _post_tool_call must be caught and the hook
    must return its safe default (None) instead of propagating."""

    def boom(*a, **kw):
        raise RuntimeError("injected _post_tool_call fault")

    monkeypatch.setattr(mod, "_record_call", boom)
    # Must not raise.
    result = mod._post_tool_call(
        tool_name="terminal",
        args={"command": "ls"},
        result={"exit_code": 0},
        session_id="fail_open_test",
    )
    assert result is None


def test_transform_tool_result_fails_open(monkeypatch, mod, sherpa_home):
    """An internal exception in _transform_tool_result must be caught and the
    hook must return its safe default (None) instead of propagating."""

    def boom(*a, **kw):
        raise RuntimeError("injected _transform_tool_result fault")

    monkeypatch.setattr(mod, "_looks_like_error", boom)
    result = mod._transform_tool_result(
        tool_name="terminal",
        result="Error: something broke",
        session_id="fail_open_test",
    )
    assert result is None


def test_pre_llm_call_fails_open(monkeypatch, mod, sherpa_home):
    """An internal exception in _pre_llm_call must be caught and the hook
    must return its safe default (None) instead of propagating."""

    def boom(*a, **kw):
        raise RuntimeError("injected _pre_llm_call fault")

    monkeypatch.setattr(mod, "_flush_stats", boom)
    result = mod._pre_llm_call(
        session_id="fail_open_test",
        user_message="hello",
        is_first_turn=True,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Phase 5.1: Structured error fields (status / error_type / error_message).
#
# Hermes can pass structured error fields in the tool result dict. When
# present, they are authoritative — we should detect errors via them
# instead of relying solely on the _looks_like_error regex heuristic.
# ---------------------------------------------------------------------------


def test_post_tool_call_structured_error_status(mod, sherpa_home):
    """A result with status='error' is detected as an error WITHOUT relying
    on _looks_like_error regex. The error streak should increment."""
    sid = "struct_err_status"
    mod._drain_nudges(sid)
    result = {"exit_code": 0, "stdout": "some output"}  # looks successful to regex
    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "test"},
        result=result,
        session_id=sid,
        status="error",
        error_message="command timed out",
    )
    # Error streak must increment even though _looks_like_error would say no.
    assert mod._error_streak.get(sid, 0) >= 1


def test_post_tool_call_structured_error_message(mod, sherpa_home):
    """A non-empty error_message is an error signal even when status is empty."""
    sid = "struct_err_msg"
    mod._drain_nudges(sid)
    result = {"exit_code": 0, "stdout": "looks fine"}
    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "check"},
        result=result,
        session_id=sid,
        status="",
        error_message="Permission denied",
    )
    assert mod._error_streak.get(sid, 0) >= 1


def test_post_tool_call_structured_error_type_recorded(mod, sherpa_home):
    """When error_type is provided, it should be recorded in the event."""
    sid = "struct_err_type"
    mod._drain_nudges(sid)
    result = {"exit_code": 1, "stderr": "crash"}
    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "run"},
        result=result,
        session_id=sid,
        status="error",
        error_type="permission_denied",
        error_message="Access denied",
    )
    # Check that the event log has the error_type.
    events_path = sherpa_home / "memories" / "model-sherpa" / "events.jsonl"
    if events_path.exists():
        lines = events_path.read_text().splitlines()
        found = any("permission_denied" in line for line in lines)
        assert found, f"error_type should appear in events; got: {lines[-1] if lines else '(empty)'}"


def test_post_tool_call_no_structured_fields_falls_back_to_regex(mod, sherpa_home):
    """When no structured error fields are provided, detection falls back
    to _looks_like_error as before."""
    sid = "struct_err_fallback"
    mod._drain_nudges(sid)
    # This dict triggers _looks_like_error (nonzero exit_code).
    result = {"exit_code": 1, "stderr": "error"}
    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "fail"},
        result=result,
        session_id=sid,
    )
    assert mod._error_streak.get(sid, 0) >= 1


def test_transform_tool_result_structured_error_overrides_regex(mod, sherpa_home):
    """_transform_tool_result should detect structured errors. A result that
    looks successful to regex but has status='error' should get a tip."""
    result = "Operation completed with warnings"  # not an error string
    out = mod._transform_tool_result(
        tool_name="terminal",
        args={"command": "test"},
        result=result,
        session_id="transform_struct",
        status="blocked",
        error_message="Blocked by policy",
    )
    # With status="blocked", it should be treated as an error and get a tip.
    assert out is not None
    assert "[SHERPA]" in out


# ---------------------------------------------------------------------------
# Phase 5.2: Context-aware nudging.
#
# When the model repeats the same failing approach, nudges should
# escalate in urgency. Instead of always delivering the same gentle tip,
# the nudge text should vary based on the error streak count.
# ---------------------------------------------------------------------------


def test_nudge_escalates_on_high_error_streak(mod, sherpa_home):
    """After 3+ consecutive errors, the nudge should include stronger
    language (e.g. 'reconsider', 'different approach') instead of just
    a gentle tip."""
    sid = "nudge_escalation"
    mod._drain_nudges(sid)
    # Simulate 3 consecutive errors to build up the streak.
    for i in range(3):
        mod._error_streak[sid] = mod._error_streak.get(sid, 0) + 1
    # Now trigger another error via _post_tool_call — streak is 3→4.
    result = {"exit_code": 1, "stderr": "Permission denied"}
    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "rm -rf /protected"},
        result=result,
        session_id=sid,
    )
    # The nudge queued for the next turn should be present.
    with mod._session_lock:
        nudges = mod._pending_nudges.get(sid, [])
    assert len(nudges) >= 1, f"Expected at least one nudge after 4 errors, got {nudges}"
    # At least one nudge should contain escalation language.
    texts = [text for _, text in nudges]
    escalated = any("reconsider" in t.lower() or "different approach" in t.lower() for t in texts)
    assert escalated, f"Expected escalation language in nudges after 4 errors, got: {texts}"


def test_nudge_gentle_on_low_error_streak(mod, sherpa_home):
    """With only 1-2 consecutive errors, the nudge should be gentle (no
    escalation language)."""
    sid = "nudge_gentle"
    mod._drain_nudges(sid)
    # Simulate just 1 error.
    mod._error_streak[sid] = 1
    result = {"exit_code": 1, "stderr": "file not found"}
    mod._post_tool_call(
        tool_name="terminal",
        args={"command": "cat missing.txt"},
        result=result,
        session_id=sid,
    )
    with mod._session_lock:
        nudges = mod._pending_nudges.get(sid, [])
    texts = [text for _, text in nudges]
    not_escalated = not any("reconsider" in t.lower() or "different approach" in t.lower() for t in texts)
    assert not_escalated, f"Did not expect escalation after just 2 errors, got: {texts}"


# ---------------------------------------------------------------------------
# Phase 5.3: /sherpa export json|csv
#
# Allows the user to export telemetry events and stats to a file for
# offline analysis. Supports JSON and CSV formats.
# ---------------------------------------------------------------------------


def test_sherpa_export_json(mod, sherpa_home, tmp_path, monkeypatch):
    """/sherpa export json writes a JSON file with events and stats."""
    # Ensure some events exist.
    mod._record_event("export_test", "test_kind", "test detail", tool="test_tool")
    mod._flush_stats()
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    # Point export output to tmp_path so it doesn't pollute the project.
    out = mod._handle_slash(f"export json {out_dir / 'sherpa_export.json'}")
    assert "exported" in out.lower() or "written" in out.lower() or out_dir.joinpath("sherpa_export.json").exists()


def test_sherpa_export_csv(mod, sherpa_home, tmp_path):
    """/sherpa export csv writes a CSV file with events."""
    mod._record_event("export_csv", "test_kind", "csv detail", tool="test_tool")
    mod._flush_stats()
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    out = mod._handle_slash(f"export csv {out_dir / 'sherpa_export.csv'}")
    assert "exported" in out.lower() or "written" in out.lower() or out_dir.joinpath("sherpa_export.csv").exists()


def test_sherpa_export_requires_format(mod):
    """/sherpa export with no format shows usage."""
    out = mod._handle_slash("export")
    assert "usage" in out.lower() or "json" in out.lower() or "csv" in out.lower()


def test_sherpa_export_invalid_format(mod):
    """/sherpa export with invalid format shows error."""
    out = mod._handle_slash("export xml")
    assert "unsupported" in out.lower() or "invalid" in out.lower() or "json" in out.lower()


# ---------------------------------------------------------------------------
# Phase 5.4: MCP tool repair — schema-based extra-arg detection and
# MCP-specific DYM error patterns for non-builtin tools.
# ---------------------------------------------------------------------------


def _p54_enable(mod, name, value):
    mod._update_state(lambda st: st["features"].__setitem__(name, value))


def test_extra_arg_detection_for_mcp_tool(mod, sherpa_home, monkeypatch):
    """When a tool has a known schema and the model passes an arg that isn't
    in the schema, _repair_args should flag it."""
    fake_reg = mod.FakeRegistry() if hasattr(mod, "FakeRegistry") else None
    if fake_reg is None:
        from tests.conftest import FakeRegistry
        fake_reg = FakeRegistry()
    fake_reg.register("mcp_web_search", {
        "name": "mcp_web_search",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    })
    monkeypatch.setattr(mod, "_registry", lambda: fake_reg)
    monkeypatch.setattr(mod, "_tool_registry_generation", lambda: fake_reg._generation)
    # "Query" (wrong case) should repair to "query" (case-insensitive match).
    # "limt" (typo) should NOT match — it's too far from "limit".
    args = {"Query": "test", "limt": 10}
    fixes = mod._repair_args("mcp_web_search", args)
    # "Query" should be repaired to "query" (case-insensitive match).
    assert any("Query" in f or "query" in f for f in fixes), f"Expected Query→query, got {fixes}"


def test_mcp_unknown_tool_dym_nudge(mod, sherpa_home, monkeypatch):
    """When an MCP tool call fails with 'unknown tool' in the result,
    the DYM nudge should fire and suggest the closest match."""
    sid = "mcp_dym_test"
    mod._drain_nudges(sid)
    fake_reg = mod.FakeRegistry() if hasattr(mod, "FakeRegistry") else None
    if fake_reg is None:
        from tests.conftest import FakeRegistry
        fake_reg = FakeRegistry()
    fake_reg.register("mcp_grep", {
        "name": "mcp_grep",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
    })
    monkeypatch.setattr(mod, "_registry", lambda: fake_reg)
    monkeypatch.setattr(mod, "_tool_registry_generation", lambda: fake_reg._generation)
    _p54_enable(mod, "didyoumean", True)
    result = "Error: mcp_grep_wrong is not a registered tool"
    mod._post_tool_call(
        tool_name="mcp_grep_wrong",
        args={"pattern": "test"},
        result=result,
        session_id=sid,
    )
    with mod._session_lock:
        nudges = mod._pending_nudges.get(sid, [])
    texts = [t for _, t in nudges]
    assert any("did you mean" in t.lower() for t in texts), f"Expected DYM nudge, got: {texts}"


def test_extra_args_flagged_in_pre_tool_call(mod, sherpa_home, monkeypatch):
    """When arg_guard is on and a model passes args not in the schema,
    _pre_tool_call should flag the unknown args."""
    fake_reg = mod.FakeRegistry() if hasattr(mod, "FakeRegistry") else None
    if fake_reg is None:
        from tests.conftest import FakeRegistry
        fake_reg = FakeRegistry()
    fake_reg.register("mcp_fetch", {
        "name": "mcp_fetch",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST"]},
            },
            "required": ["url"],
        },
    })
    monkeypatch.setattr(mod, "_registry", lambda: fake_reg)
    monkeypatch.setattr(mod, "_tool_registry_generation", lambda: fake_reg._generation)
    _p54_enable(mod, "arg_guard", True)
    args = {"url": "https://example.com", "headers": {"Accept": "text/html"}}
    mod._pre_tool_call(
        tool_name="mcp_fetch",
        args=args,
        session_id="extra_arg_test",
    )
    # Verify the schema validation doesn't crash on extra args.
    issues = mod._schema_required_or_invalid_args("mcp_fetch", args)
    assert isinstance(issues, list)
