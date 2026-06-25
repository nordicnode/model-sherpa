"""Shared test fixtures for model-sherpa (Phase 4).

Provides:
  - sherpa_home: redirect HERMES_HOME to a tmp dir (also defined per-file for
    back-compat, but centralised here so all test modules share it).
  - mod: reload the plugin fresh against sherpa_home.
  - fake_registry / FakeRegistry: an in-memory registry with schemas so the
    schema-driven (Pass-2) arg repair, validation, and didyoumean paths can be
    exercised without the real Hermes registry.
  - FakePluginCtx: a minimal ctx with register_hook/register_command/
    register_tool for register()-driven tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "__init__.py"


@pytest.fixture()
def sherpa_home(monkeypatch, tmp_path):
    """Redirect HERMES_HOME to a tmp dir so tests never touch real user state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def mod(sherpa_home):
    """Reload the plugin module from source so module globals are clean."""
    for name in list(sys.modules):
        if name in ("model_sherpa", "model-sherpa") or name.startswith("model_sherpa."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("model_sherpa", str(PLUGIN_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_fresh():
    for name in list(sys.modules):
        if name in ("model_sherpa", "model-sherpa") or name.startswith("model_sherpa."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("model_sherpa", str(PLUGIN_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeEntry:
    def __init__(self, name: str, schema: Dict[str, Any]):
        self.name = name
        self.schema = schema


class FakeRegistry:
    """In-memory registry that mirrors the real tools.registry public API the
    plugin uses: get_all_tool_names, get_entry, get_schema, dispatch, deregister,
    and _generation."""

    def __init__(self):
        self._tools: Dict[str, _FakeEntry] = {}
        self._generation: int = 0
        self.dispatched: List[Dict[str, Any]] = []
        self.deregistered: List[str] = []

    def register(self, name: str, schema: Dict[str, Any]) -> None:
        self._tools[name] = _FakeEntry(name, schema)
        self._generation += 1

    def get_all_tool_names(self) -> List[str]:
        return sorted(self._tools.keys())

    def get_entry(self, name: str) -> Optional[_FakeEntry]:
        return self._tools.get(name)

    def get_schema(self, name: str) -> Optional[Dict[str, Any]]:
        e = self._tools.get(name)
        return e.schema if e else None

    def dispatch(self, name: str, args: Dict[str, Any], **kwargs: Any) -> Any:
        self.dispatched.append({"name": name, "args": dict(args), "kwargs": dict(kwargs)})
        return {"ok": True, "name": name, "args": dict(args)}

    def deregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            self.deregistered.append(name)
            self._generation += 1
            return True
        return False


class FakePluginCtx:
    def __init__(self):
        self.hooks: Dict[str, List] = {}
        self.commands: Dict[str, Dict[str, Any]] = {}
        self.registered: List[Dict[str, Any]] = []

    def register_hook(self, name: str, fn) -> None:
        self.hooks.setdefault(name, []).append(fn)

    def register_command(self, name: str, handler=None, description: str = "", args_hint: str = "") -> None:
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}

    def register_tool(
        self, name: str, toolset: str = "", schema=None, handler=None, emoji: str = "", **kw: Any
    ) -> None:
        self.registered.append({"name": name, "toolset": toolset, "schema": schema, "handler": handler, "emoji": emoji})


@pytest.fixture()
def fake_registry():
    return FakeRegistry()


@pytest.fixture()
def fake_ctx():
    return FakePluginCtx()


@pytest.fixture()
def registry_with_schemas(monkeypatch, fake_registry):
    """A FakeRegistry pre-populated with realistic schemas for the core tools,
    and installed as the plugin's registry so schema-driven paths execute.

    Yields (registry, module) after wiring monkeypatch.
    """
    schemas = {
        "read_file": {
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        "write_file": {
            "name": "write_file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        "terminal": {
            "name": "terminal",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "background": {"type": "boolean"},
                },
                "required": ["command"],
            },
        },
        "search_files": {
            "name": "search_files",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "target": {"type": "string", "enum": ["content", "files"]},
                    "file_glob": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
        "memory": {
            "name": "memory",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                    "target": {"type": "string", "enum": ["memory", "user"]},
                    "content": {"type": "string"},
                    "old_text": {"type": "string"},
                },
                "required": ["action", "target"],
            },
        },
        "todo": {
            "name": "todo",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    }
    for name, schema in schemas.items():
        fake_registry.register(name, schema)
    return fake_registry
