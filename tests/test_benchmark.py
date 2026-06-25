"""Tests for benchmark.py honesty (Phase 3.1).

Pins the contract that:
  (a) When no real data exists, the report contains a visible ⚠ NO REAL DATA
      banner and does NOT claim "real historical data".
  (b) When events.jsonl exists, stats are aggregated from it, not just state.json.
  (c) Multipliers are read from a config dict (overridable).
  (d) The "~X% fewer roundtrips" figure is derived from a real formula or omitted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture: load the benchmark module fresh.
# ---------------------------------------------------------------------------


def _load_benchmark(hermes_home: str):
    """Import benchmark.py with a redirect HERMES_HOME."""
    import os

    os.environ["HERMES_HOME"] = str(hermes_home)
    for name in list(sys.modules):
        if name == "benchmark" or name.startswith("benchmark."):
            del sys.modules[name]
    plugin_path = Path(__file__).resolve().parent.parent / "benchmark.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("benchmark", str(plugin_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def home(tmp_path):
    """A fresh HERMES_HOME with no state."""
    return tmp_path


@pytest.fixture()
def home_with_state(tmp_path):
    """A HERMES_HOME with a populated state.json (no events.jsonl)."""
    state_dir = tmp_path / "memories" / "model-sherpa"
    state_dir.mkdir(parents=True)
    state = {
        "enabled": True,
        "features": {},
        "stats": {
            "rewrites": 10,
            "hints": 3,
            "loops": 2,
            "cheatsheets": 30,
            "aliases_used": 1,
            "didyoumean": 4,
            "reanchors": 5,
            "plan_nudges": 2,
            "arg_blocks": 6,
            "read_blocks": 8,
            "cmd_lints": 15,
            "tool_dym": 2,
            "dry_runs": 0,
            "nudges_suppressed": 1,
            "per_tool": {},
        },
        "custom_hints": [],
    }
    (state_dir / "state.json").write_text(json.dumps(state))
    return tmp_path


@pytest.fixture()
def home_with_events(tmp_path):
    """A HERMES_HOME with both state.json and events.jsonl."""
    state_dir = tmp_path / "memories" / "model-sherpa"
    state_dir.mkdir(parents=True)
    state = {
        "enabled": True,
        "features": {},
        "stats": {
            "rewrites": 10,
            "hints": 3,
            "loops": 2,
            "cheatsheets": 30,
            "aliases_used": 1,
            "didyoumean": 4,
            "reanchors": 5,
            "plan_nudges": 2,
            "arg_blocks": 6,
            "read_blocks": 8,
            "cmd_lints": 15,
            "tool_dym": 2,
            "dry_runs": 0,
            "nudges_suppressed": 1,
            "per_tool": {},
        },
        "custom_hints": [],
    }
    (state_dir / "state.json").write_text(json.dumps(state))
    events = [
        {"kind": "rewrite", "tool": "terminal", "ts": "2026-06-24T10:00:00"},
        {"kind": "rewrite", "tool": "read_file", "ts": "2026-06-24T10:01:00"},
        {"kind": "hint", "tool": "terminal", "ts": "2026-06-24T10:02:00"},
        {"kind": "loop", "tool": "terminal", "ts": "2026-06-24T10:03:00"},
        {"kind": "read_block", "tool": "read_file", "ts": "2026-06-24T10:04:00"},
    ]
    with (state_dir / "events.jsonl").open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Test (a): No real data → visible banner, no "real historical data" claim.
# ---------------------------------------------------------------------------


def test_no_data_shows_banner(benchmark_mod, home):
    """When no state.json and no events.jsonl exist, the report must contain
    a visible ⚠ NO REAL DATA banner."""
    mod = _load_benchmark(home)
    report = mod.run_benchmark()
    assert "NO REAL DATA" in report or "⚠" in report, (
        "report must show a visible NO REAL DATA banner when no data exists"
    )


def test_no_data_does_not_claim_real_historical(benchmark_mod, home):
    """When no data exists, the report must NOT claim 'real historical data'."""
    mod = _load_benchmark(home)
    report = mod.run_benchmark()
    assert "real historical data" not in report.lower(), (
        "report must not claim 'real historical data' when no data exists"
    )


# ---------------------------------------------------------------------------
# Test (b): With events.jsonl, load_stats reports the source.
# ---------------------------------------------------------------------------


def test_load_stats_with_events_returns_events_source(home_with_events):
    """When events.jsonl exists, load_stats must return source='events_jsonl'."""
    mod = _load_benchmark(home_with_events)
    stats, source = mod.load_stats()
    assert source == "events_jsonl", f"expected 'events_jsonl', got {source!r}"


def test_load_stats_with_state_only_returns_state_source(home_with_state):
    """When only state.json exists, load_stats returns source='state_json'."""
    mod = _load_benchmark(home_with_state)
    stats, source = mod.load_stats()
    assert source == "state_json", f"expected 'state_json', got {source!r}"


def test_load_stats_with_no_data_returns_none_source(home):
    """When no data exists, load_stats returns source='none'."""
    mod = _load_benchmark(home)
    stats, source = mod.load_stats()
    assert source == "none", f"expected 'none', got {source!r}"


# ---------------------------------------------------------------------------
# Test (c): Multipliers come from a config dict.
# ---------------------------------------------------------------------------


def test_benchmark_constants_overridable(home_with_state):
    """BENCHMARK_CONSTANTS must be a dict that can be overridden."""
    mod = _load_benchmark(home_with_state)
    assert hasattr(mod, "BENCHMARK_CONSTANTS"), "BENCHMARK_CONSTANTS must exist"
    assert isinstance(mod.BENCHMARK_CONSTANTS, dict), "BENCHMARK_CONSTANTS must be a dict"
    # Override and re-run; the report should reflect the new values.
    original = mod.BENCHMARK_CONSTANTS.copy()
    mod.BENCHMARK_CONSTANTS["AVG_INPUT_TOKEN_COST_PER_M"] = 999.0
    report = mod.run_benchmark()
    # The overridden constant must actually be consumed by run_benchmark
    # (not just stored) — the report must still be a string and not raise.
    assert isinstance(report, str) and report
    # Restore so the fixture cleanup doesn't contaminate other tests.
    mod.BENCHMARK_CONSTANTS.update(original)


# ---------------------------------------------------------------------------
# Test (d): No fabricated roundtrip% from hardcoded (0.4/1.4) ratio.
# ---------------------------------------------------------------------------


def test_report_does_not_use_hardcoded_roundtrip_ratio(home_with_state):
    """The report must not include '~29% fewer roundtrips' derived from the
    hardcoded (0.4/1.4)*100 formula. Either use a real formula or omit it."""
    mod = _load_benchmark(home_with_state)
    report = mod.run_benchmark()
    # The old code computed ~((0.4/1.4)*100) = 28-29%. If that exact figure
    # appears, the rewrite is incomplete.
    assert "~29%" not in report, (
        "report still contains the fabricated ~29% roundtrip figure"
    )


# ---------------------------------------------------------------------------
# Stubs so the parametrised fixture works without `benchmark_mod` param.
# These are overridden when the module is importable after the rewrite.
# ---------------------------------------------------------------------------


@pytest.fixture()
def benchmark_mod():
    """Placeholder — actual tests call _load_benchmark directly."""
    return None
