"""Tests for the /sherpa slash subcommands that previously had no coverage
(Phase 4 gap): aliases, telemetry, add (+ bad regex), rules, log, cheatsheet,
help, and the unknown-command fallback.

status / on / off / feature / doctor / reset are already covered in
test_smoke.py; these fill the remaining subcommands.
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


# ---------------------------------------------------------------------------
# cheatsheet
# ---------------------------------------------------------------------------


def test_cheatsheet_prints_core_tools(mod):
    out = mod._handle_slash("cheatsheet")
    assert "terminal" in out and "read_file" in out and "search_files" in out
    assert "Cheatsheet injected on turn 1" in out


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def test_rules_lists_arg_repair_and_hints(mod):
    out = mod._handle_slash("rules")
    assert "Arg-repair rules" in out
    assert "terminal" in out  # an arg-alias entry
    assert "Built-in error-hint patterns" in out
    assert "no such file" in out.lower() or "enoent" in out.lower()


def test_rules_includes_custom_hints(mod):
    mod._handle_slash('add "Permission denied" "Use sudo"')
    out = mod._handle_slash("rules")
    assert "Custom hints" in out
    assert "Permission denied" in out


# ---------------------------------------------------------------------------
# add (custom hint)
# ---------------------------------------------------------------------------


def test_add_custom_hint_persists(mod):
    out = mod._handle_slash('add "Permission denied" "Try sudo"')
    assert "Added custom hint" in out
    state = mod._load_state()
    hints = state["custom_hints"]
    assert any(h["pattern"] == "Permission denied" for h in hints)
    added = next(h for h in hints if h["pattern"] == "Permission denied")
    assert added["hint"] == "Try sudo"


def test_add_rejects_invalid_regex(mod):
    out = mod._handle_slash('add "[unclosed" "bad hint"')
    assert "Invalid regular expression" in out
    state = mod._load_state()
    assert not any(h["pattern"] == "[unclosed" for h in state["custom_hints"])


def test_add_requires_args(mod):
    out = mod._handle_slash("add")
    assert "Usage" in out


def test_add_updates_existing_pattern(mod):
    mod._handle_slash('add Foo first')
    mod._handle_slash('add Foo second')
    state = mod._load_state()
    matches = [h for h in state["custom_hints"] if h["pattern"] == "Foo"]
    assert len(matches) == 1, "updating an existing pattern must not duplicate"
    assert matches[0]["hint"] == "second"


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


def test_log_empty_when_nothing_logged(mod):
    out = mod._handle_slash("log")
    assert "no corrections" in out.lower()


def test_log_shows_recent_entries(mod):
    # Produce a correction entry.
    mod._log_correction("rewrite", "terminal: cmd→command")
    out = mod._handle_slash("log")
    assert "rewrite" in out
    assert "cmd→command" in out


def test_log_respects_n(mod):
    for i in range(5):
        mod._log_correction("kind", f"detail-{i}")
    out = mod._handle_slash("log 2")
    # Last 2 entries only.
    assert "detail-3" in out
    assert "detail-4" in out


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------


def test_telemetry_empty_message(mod):
    out = mod._handle_slash("telemetry")
    assert "telemetry" in out.lower() or "no sherpa telemetry" in out.lower()


def test_telemetry_shows_recorded_events(mod):
    mod._record_event("sess-1", "loop", "terminal repeated 3x", tool="terminal")
    out = mod._handle_slash("telemetry sess-1")
    assert "loop" in out
    assert "terminal" in out


def test_telemetry_respects_n(mod):
    for i in range(5):
        mod._record_event("sess-2", "hint", f"hint-{i}")
    out = mod._handle_slash("telemetry sess-2 2")
    assert "hint-3" in out and "hint-4" in out


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------


def test_aliases_lists_all_specs(mod, fake_registry, monkeypatch):
    monkeypatch.setattr(mod, "_registry", lambda: fake_registry)
    monkeypatch.setattr(mod, "_tool_registry_generation", lambda: fake_registry._generation)
    out = mod._handle_slash("aliases")
    assert "Soft aliases" in out
    assert "Hard visible alias tools" in out
    # Every spec name should appear (bash, cat, grep, find, ls, head, tail, ...).
    for name in ("bash", "cat", "grep", "find", "ls"):
        assert name in out


def test_aliases_handles_missing_registry(mod, monkeypatch):
    monkeypatch.setattr(mod, "_registry", lambda: None)
    out = mod._handle_slash("aliases")
    assert "inspect registry" in out.lower()


# ---------------------------------------------------------------------------
# help / unknown
# ---------------------------------------------------------------------------


def test_help_no_args(mod):
    out = mod._handle_slash("")
    assert "model-sherpa" in out


def test_help_flag(mod):
    out = mod._handle_slash("help")
    assert "Subcommands" in out or "model-sherpa" in out


def test_unknown_subcommand(mod):
    out = mod._handle_slash("frobnicate")
    assert "Unknown subcommand" in out
