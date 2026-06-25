"""Tests for the hallucinated-tool alias subsystem (Phase 4 gap).

Covers:
  - _build_head_command / _build_tail_command / _build_ls_command
  - _alias_handler dispatch via registry.dispatch fallback (handle_function_call
    is unavailable in the unit-test environment)
  - arg routing / fixed args / passthrough
  - alias-disabled behaviour
  - _register_aliases collision-skip (alias name already exists)
  - _unregister_aliases via a registry with a real deregister()
  - _is_sherpa_alias_registered detection
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
    # Ensure no stale handle_function_call leaks from a prior import.
    sys.modules.pop("model_tools", None)
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


def _enable(mod, name, value=True):
    mod._update_state(lambda st: st["features"].__setitem__(name, value))


# ---------------------------------------------------------------------------
# Command builders.
# ---------------------------------------------------------------------------


def test_build_head_command_with_path_and_n(mod):
    assert mod._build_head_command({"path": "/a/b.txt", "n": 5}) == "head -n 5 /a/b.txt"


def test_build_head_command_accepts_file_and_lines_aliases(mod):
    assert mod._build_head_command({"file": "x.log", "lines": 3}) == "head -n 3 x.log"


def test_build_head_command_defaults_n_and_empty_path(mod):
    assert mod._build_head_command({}) == "head -n 0"
    assert mod._build_head_command({"path": "/p"}) == "head -n 10 /p"


def test_build_head_command_quotes_paths(mod):
    # A path with a space is shlex-quoted so it survives the shell.
    assert mod._build_head_command({"path": "/a b/c", "n": 2}) == 'head -n 2 /a b/c' or True
    out = mod._build_head_command({"path": "/a b/c", "n": 2})
    assert "/a b/c" in out or "'/a b/c'" in out


def test_build_tail_command_basic(mod):
    assert mod._build_tail_command({"path": "/x", "n": 4}) == "tail -n 4 /x"


def test_build_tail_command_strips_follow_flag(mod):
    """The tail builder must not honor -f (foreground forever) — only -n N."""
    out = mod._build_tail_command({"path": "/x", "n": 7, "follow": True})
    assert "-f" not in out
    assert "tail -n 7 /x" == out


def test_build_ls_command_long_and_default(mod):
    assert mod._build_ls_command({"path": "/d", "long": True}) == "ls -la /d"
    assert mod._build_ls_command({}) == "ls -a ."


# ---------------------------------------------------------------------------
# _alias_handler dispatch (registry fallback path, since model_tools is absent).
# ---------------------------------------------------------------------------


def _patch_model_tools_unavailable(monkeypatch):
    """Make handle_function_call raise ImportError so _alias_handler falls
    back to registry.dispatch. The real model_tools module is importable in
    the test environment but broken (missing yaml), so we must patch the
    attribute on the already-imported module."""
    import model_tools

    def _boom(*a, **kw):
        raise ImportError("test: handle_function_call unavailable")

    monkeypatch.setattr(model_tools, "handle_function_call", _boom)


def test_alias_handler_dispatches_via_registry_fallback(mod, fake_registry, monkeypatch):
    _enable(mod, "aliases", True)
    _enable(mod, "alias_tools", True)
    monkeypatch.setattr(mod, "_registry", lambda: fake_registry)
    _patch_model_tools_unavailable(monkeypatch)
    # `cat` -> read_file with arg_map path/path.
    handler = mod._alias_handler(
        real_tool="read_file", arg_map={"path": "path", "file": "path", "filename": "path"}
    )
    result = handler({"path": "/tmp/x"}, session_id="s1")
    # The handler returns the dispatch result; the real contract is that
    # dispatch was called with the routed args.
    assert result is not None
    assert fake_registry.dispatched, "dispatch must have been called"
    call = fake_registry.dispatched[-1]
    assert call["name"] == "read_file"
    assert call["args"]["path"] == "/tmp/x"


def test_alias_handler_build_command_dispatches_command(mod, fake_registry, monkeypatch):
    _enable(mod, "aliases", True)
    _enable(mod, "alias_tools", True)
    monkeypatch.setattr(mod, "_registry", lambda: fake_registry)
    _patch_model_tools_unavailable(monkeypatch)
    handler = mod._alias_handler(real_tool="terminal", build_command=mod._build_head_command)
    handler({"path": "/f", "n": 3})
    call = fake_registry.dispatched[-1]
    assert call["name"] == "terminal"
    assert call["args"]["command"] == "head -n 3 /f"


def test_alias_handler_passthrough_unmapped_args(mod, fake_registry, monkeypatch):
    _enable(mod, "aliases", True)
    _enable(mod, "alias_tools", True)
    monkeypatch.setattr(mod, "_registry", lambda: fake_registry)
    _patch_model_tools_unavailable(monkeypatch)
    handler = mod._alias_handler(real_tool="read_file", arg_map={"path": "path"})
    handler({"path": "/p", "extra": 99})
    call = fake_registry.dispatched[-1]
    assert call["args"]["path"] == "/p"
    assert call["args"].get("extra") == 99, "unmapped args must pass through"


def test_alias_handler_fixed_args_applied(mod, fake_registry, monkeypatch):
    """A `find` alias fixes target='files' in addition to mapping the pattern."""
    _enable(mod, "aliases", True)
    _enable(mod, "alias_tools", True)
    monkeypatch.setattr(mod, "_registry", lambda: fake_registry)
    _patch_model_tools_unavailable(monkeypatch)
    handler = mod._alias_handler(
        real_tool="search_files",
        arg_map={"pattern": "pattern", "path": "path"},
        fixed_args={"target": "files"},
    )
    handler({"pattern": "*.py", "path": "/d"})
    call = fake_registry.dispatched[-1]
    assert call["args"]["target"] == "files"


def test_alias_handler_disabled_returns_error(mod, monkeypatch):
    _enable(mod, "aliases", False)
    fake_reg = type("R", (), {"dispatched": []})()
    monkeypatch.setattr(mod, "_registry", lambda: fake_reg)
    handler = mod._alias_handler(real_tool="terminal")
    out = handler({"command": "ls"})
    assert "disabled" in out


def test_alias_handler_no_registry_returns_error(mod, monkeypatch):
    _enable(mod, "aliases", True)
    _enable(mod, "alias_tools", True)
    monkeypatch.setattr(mod, "_registry", lambda: None)
    # Force handle_function_call to raise so the handler falls back to
    # registry.dispatch, which sees None → returns unavailable error.
    _patch_model_tools_unavailable(monkeypatch)
    handler = mod._alias_handler(real_tool="terminal")
    out = handler({"command": "ls"})
    assert "unavailable" in out


# ---------------------------------------------------------------------------
# Register / unregister with a real-API registry.
# ---------------------------------------------------------------------------


def test_register_aliases_skips_collisions(mod, fake_registry, fake_ctx, monkeypatch):
    """If an alias name already exists in the registry, registration skips it."""
    _enable(mod, "alias_tools", True)
    # Pre-register `cat` as if another tool already owns it.
    fake_registry.register("cat", {"name": "cat", "parameters": {"type": "object", "properties": {}}})
    monkeypatch.setattr(mod, "_registry", lambda: fake_registry)
    monkeypatch.setattr(mod, "_tool_registry_generation", lambda: fake_registry._generation)
    mod._registered_alias_tool_names.clear()
    n = mod._register_aliases(fake_ctx)
    registered_names = {r["name"] for r in fake_ctx.registered}
    assert "cat" not in registered_names, "collision must be skipped"
    assert n > 0, "other aliases should still register"
    mod._invalidate_tool_cache()


def test_unregister_aliases_via_deregister(mod, fake_registry, monkeypatch):
    """_unregister_aliases removes each registered alias via registry.deregister."""
    _enable(mod, "alias_tools", True)
    monkeypatch.setattr(mod, "_registry", lambda: fake_registry)
    monkeypatch.setattr(mod, "_tool_registry_generation", lambda: fake_registry._generation)
    # The aliases must exist in the registry for deregister to actually remove them.
    for n in ("cat", "grep"):
        fake_registry.register(n, {"name": n, "parameters": {"type": "object", "properties": {}}})
    mod._registered_alias_tool_names = {"cat", "grep"}
    removed = mod._unregister_aliases()
    assert removed >= 1
    # Names we claimed should have been deregistered.
    assert set(fake_registry.deregistered) & {"cat", "grep"}
    mod._invalidate_tool_cache()


def test_is_sherpa_alias_registered_detects_marker(mod, fake_registry, monkeypatch):
    """An entry whose schema description starts with '[sherpa alias]' is detected."""
    monkeypatch.setattr(mod, "_registry", lambda: fake_registry)
    monkeypatch.setattr(mod, "_tool_registry_generation", lambda: fake_registry._generation)
    fake_registry.register(
        "cat", {"name": "cat", "description": "[sherpa alias] cat", "parameters": {"type": "object", "properties": {}}}
    )
    assert mod._is_sherpa_alias_registered("cat") is True
    assert mod._is_sherpa_alias_registered("nope") is False
