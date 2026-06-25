"""Tests for the schema-driven (Pass-2) arg-repair engine and the recursive
schema validator (Phase 4 coverage gap).

These were the largest untested code paths in the review: Pass-2 synonym
repair (the bidirectional _ARG_SYNONYM_GROUPS engine) was dead code in the
old tests because no registry schema was supplied, and _schema_validate_value
had zero coverage.

The `registry_with_schemas` fixture (conftest.py) supplies an in-memory
FakeRegistry with realistic core-tool schemas; we monkeypatch the plugin's
_registry()/_tool_registry_generation() to return it so the schema-driven
branches execute.
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
def mod_with_registry(sherpa_home, monkeypatch, registry_with_schemas):
    """Fresh plugin module whose _registry() returns the FakeRegistry."""
    mod = _load_fresh_module()
    monkeypatch.setattr(mod, "_registry", lambda: registry_with_schemas)
    monkeypatch.setattr(mod, "_tool_registry_generation", lambda: registry_with_schemas._generation)
    # Clear the lru_caches so schema lookups consult the wired registry.
    mod._registered_tool_names_cached.cache_clear()
    mod._get_schema_cached.cache_clear()
    return mod, registry_with_schemas


# ---------------------------------------------------------------------------
# Pass-2 case-insensitive exact match.
# ---------------------------------------------------------------------------


def test_pass2_case_insensitive_repair(mod_with_registry):
    mod, _ = mod_with_registry
    args = {"Path": "/tmp/x"}
    fixes = mod._repair_args("read_file", args)
    assert "path" in args, "Path should be canonicalised to path"
    assert "Path" not in args
    assert any("Path" in f for f in fixes)


def test_pass2_normalized_fuzzy_match(mod_with_registry):
    """A key that differs only by separator/case normalizes to a schema prop."""
    mod, _ = mod_with_registry
    args = {"file-glob": "*.py"}
    mod._repair_args("search_files", args)
    assert "file_glob" in args, "file-glob should normalize to file_glob"
    assert "file-glob" not in args


def test_pass2_synonym_group_routes_to_schema_member(mod_with_registry):
    """A hallucinated synonym (e.g. `filepath`) routes to the schema's actual
    property name (`path`) via the synonym-group engine — bidirectional."""
    mod, _ = mod_with_registry
    # `filepath` is NOT in read_file's _ARG_ALIASES, and read_file's schema
    # uses `path`. The synonym group {path, file_path, filepath, ...} must
    # route filepath -> path.
    args = {"filepath": "/tmp/x"}
    fixes = mod._repair_args("read_file", args)
    assert "path" in args and args["path"] == "/tmp/x"
    assert "filepath" not in args
    assert any("filepath" in f for f in fixes)


def test_pass2_duplicate_synonym_dropped(mod_with_registry):
    """When both the hallucinated key and the canonical key are present, the
    hallucinated one is dropped (canonical wins) rather than overwriting."""
    mod, _ = mod_with_registry
    args = {"path": "/canonical", "filepath": "/bogus"}
    fixes = mod._repair_args("read_file", args)
    assert args["path"] == "/canonical", "canonical must win"
    assert "filepath" not in args
    assert any("dropped duplicate synonym" in f for f in fixes)


def test_pass2_content_synonym_group(mod_with_registry):
    """write_file schema uses `content`; `body`/`text`/`data` route to it."""
    mod, _ = mod_with_registry
    args = {"path": "/tmp/x", "body": "hello"}
    mod._repair_args("write_file", args)
    assert args.get("content") == "hello"
    assert "body" not in args


def test_pass2_no_schema_no_crash(mod):
    """Without a registry, Pass-2 is a no-op and must not raise."""
    args = {"filepath": "/tmp/x"}
    fixes = mod._repair_args("read_file", args)
    # No schema -> no Pass-2 repair; filepath stays (Pass-1 read_file aliases
    # DO map file_path/filepath, so it actually gets repaired by Pass 1 here).
    # The contract we pin: no crash, and the function returns a list.
    assert isinstance(fixes, list)


# ---------------------------------------------------------------------------
# Recursive schema validation (_schema_validate_value).
# ---------------------------------------------------------------------------


def test_schema_validate_value_type_mismatch(mod):
    issues = mod._schema_validate_value("offset", {"type": "integer"}, "abc")
    assert issues, "string where integer expected must be flagged"


def test_schema_validate_value_enum_violation(mod):
    issues = mod._schema_validate_value("status", {"type": "string", "enum": ["a", "b"]}, "c")
    assert issues, "a value outside the enum must be flagged"
    assert any("status" in i for i in issues)


def test_schema_validate_value_ok(mod):
    issues = mod._schema_validate_value("limit", {"type": "integer"}, 5)
    assert issues == []


def test_schema_validate_value_nested_required(mod):
    """A nested object missing a required child field is flagged."""
    prop = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "content": {"type": "string"}},
        "required": ["id", "content"],
    }
    value = {"id": "1"}  # missing content
    issues = mod._schema_validate_value("todo", prop, value)
    assert any("content" in i and "required" in i for i in issues), issues


def test_schema_validate_value_array_items(mod):
    """An array with a mistyped item is flagged per-index."""
    prop = {"type": "array", "items": {"type": "integer"}}
    issues = mod._schema_validate_value("nums", prop, [1, "two", 3])
    assert any("[1]" in i for i in issues), issues


def test_schema_validate_value_bool_not_int(mod):
    """JSON Schema treats bool as distinct from integer (Python bool is a subclass
    of int, but the validator must reject bool where integer is expected)."""
    issues = mod._schema_validate_value("n", {"type": "integer"}, True)
    assert issues, "bool must not satisfy integer type"


# ---------------------------------------------------------------------------
# _missing_required_args per tool (uses schema when available).
# ---------------------------------------------------------------------------


def test_missing_required_args_memory_invalid_action(mod_with_registry):
    mod, _ = mod_with_registry
    missing = mod._missing_required_args("memory", {"action": "frob", "target": "memory"})
    assert any("action" in m for m in missing)


def test_missing_required_args_memory_replace_needs_old_text(mod_with_registry):
    mod, _ = mod_with_registry
    missing = mod._missing_required_args(
        "memory", {"action": "replace", "target": "user", "content": "x"}
    )
    assert any("old_text" in m for m in missing)


def test_missing_required_args_todo_bad_status(mod_with_registry):
    mod, _ = mod_with_registry
    todos = [{"id": "1", "content": "do thing", "status": "done"}]
    missing = mod._missing_required_args("todo", {"todos": todos})
    assert any("status" in m for m in missing), "invalid status must be flagged"
