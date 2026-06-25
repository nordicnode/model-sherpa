# Model Sherpa

> **Guide-rails and sentinel layer for LLM agents — stop hallucinated tool calls, break looping behaviour, and keep secrets out of your context window.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Hermes: Plugin](https://img.shields.io/badge/Hermes-Plugin-orange.svg)](https://github.com/nousresearch/hermes)
[![CI Status](https://github.com/nordicnode/model-sherpa/actions/workflows/ci.yml/badge.svg)](https://github.com/nordicnode/model-sherpa/actions)
[![Tests: 194](https://img.shields.io/badge/tests-194%20passing-brightgreen.svg)](https://github.com/nordicnode/model-sherpa/actions)
[![Version: 0.4.0](https://img.shields.io/badge/version-0.4.0-blue.svg)](https://github.com/nordicnode/model-sherpa/releases)

**Quick Links:** [Install](#installation) · [Quick Start](#quick-start) · [Guide-Rails](#key-guide-rails) · [Slash Commands](#slash-commands) · [Configuration](#advanced-configuration) · [Benchmarking](#performance-and-benchmarking) · [Contributing](https://github.com/nordicnode/model-sherpa/blob/main/CONTRIBUTING.md)

---

## Why Model Sherpa?

LLM agents are powerful — and expensive when they go off the rails. When a model hallucinates tool parameters, repeats the same failing action in a loop, or leaks API keys into logs, you pay for it in wasted tokens, broken workflows, and security exposure.

Model Sherpa sits between the LLM and the Hermes tool registry as **transparent middleware**, silently fixing bad tool calls, breaking repetitive loops, and stripping secrets from logs — before they cost you.

| Without Sherpa | With Sherpa |
| :--- | :--- |
| Model passes curly-quoted `"path.txt"` → tool crashes | Smart-quote repair fixes it silently |
| Model loops `read_file` → `patch` → `read_file` forever | Loop detected → nudge injected → model pivots |
| `api_key=sk-abc123...` shows up in session logs | Redacted to `api_key=***REDACTED***` |
| Model sends `file_path` instead of `path` → schema error | Fuzzy normalization maps to the correct key |

---

## Installation

### Automated (via Hermes CLI)

```bash
hermes plugins install nordicnode/model-sherpa
```

### Manual

```bash
git clone https://github.com/nordicnode/model-sherpa.git ~/.hermes/plugins/model-sherpa
```

---

## Quick Start

```bash
# Install the plugin
hermes plugins install nordicnode/model-sherpa

# Restart Hermes, then run the diagnostic to check your tool registry
/sherpa doctor

# View your lifetime stats and feature states
/sherpa

# Toggle a specific guide-rail on or off
/sherpa feature read_damper off
```

That's it. Sherpa is zero-config by default — all guide-rails are active out of the box.

---

## Key Guide-Rails

### Arg Guard and Universal Repair

The **Arg Guard** is the first line of defense against model parameter fumbles:

- **Schema Validation**: Intercepts tool calls and validates them against their JSON schema before execution.
- **Smart-Quote Repair**: Automatically converts curly quotes (`"path.txt"`) to standard ASCII — a common LLM copy-paste error.
- **Fuzzy Normalization**: Maps hallucinatory keys (e.g., `file_path` or `file-path`) to the canonical schema key (`path`) automatically.
- **`patch` Specialist**: Dedicated repair logic for the `patch` tool, mapping `diff` or `original` to `patch_string`.

### Sequence Loop Detection (SLD)

Standard repetition counters miss the most expensive failure mode: **Rhythmic Thrashing**. Sherpa detects:

- **Simple Repeats**: $A \rightarrow A \rightarrow A$
- **Ping-Pong Loops**: $A \rightarrow B \rightarrow A \rightarrow B$
- **Complex Cycles**: $A \rightarrow B \rightarrow C \rightarrow A \rightarrow B \rightarrow C$

When detected, Sherpa injects a "Loop Nudge" into the model's context, forcing it to pivot strategy before it burns through the token budget.

### Smart Read Range Damping

Traditional dampers block the same file twice. Sherpa's **Read Damper** is range-aware:

- **Paging Support**: Allows the model to page through a file sequentially (e.g., Lines 1-100, then 101-200).
- **Subset Detection**: Blocks a read only if the requested lines are **already contained** within a previously read range in the same turn.

### Zero-Trust Telemetry

Sherpa is built for privacy-conscious environments:

- **Substring Redaction**: Masks `api_key`, `token`, `password`, and `secret` in all logs, even if they are part of a larger key (e.g., `github_token_v2`).
- **Binary Safeguards**: Coerces non-UTF8 tool results to a compact placeholder (`<binary data: N bytes>`) so binary payloads don't pollute the context window.

---

## Comparison with Alternatives

| Feature | Model Sherpa | Guardrails AI | NeMo Guardrails |
| :--- | :---: | :---: | :---: |
| Silent arg repair | ✅ | ❌ | ❌ |
| Loop detection (rhythmic SLD) | ✅ | ❌ | ❌ |
| Smart-quote repair | ✅ | ❌ | ❌ |
| Read-range damping | ✅ | ❌ | ❌ |
| Secret substring redaction | ✅ | ✅ (validators) | ✅ (rails) |
| Custom recovery hints | ✅ | ✅ (hub) | ✅ (Colang) |
| Zero-config install | ✅ | pip + config | pip + Colang |
| Framework-agnostic | ❌ Hermes-only | ✅ | ✅ |
| Structured data generation | ❌ | ✅ | ✅ |

**Bottom line:** If you're running Hermes Agent and want drop-in protection without writing config files or custom validators, Sherpa is purpose-built for that. If you need framework-agnostic structured output validation, Guardrails AI or NeMo Guardrails are the right choice.

---

## Slash Commands

| Command | Description |
| :--- | :--- |
| `/sherpa` | Comprehensive dashboard of lifetime stats and feature states. |
| `/sherpa feature <name> <on\|off>` | Real-time toggle for any guide-rail (e.g., `didyoumean`, `read_damper`). |
| `/sherpa doctor` | Diagnostic check on your tool registry to find schema inconsistencies. |
| `/sherpa telemetry` | View the most recent session events with full redaction. |
| `/sherpa add <pattern> <hint>` | Dynamically add new recovery hints without restarting. |
| `/sherpa export json` | Export lifetime stats as JSON. |
| `/sherpa export csv` | Export lifetime stats as CSV. |

---

## Advanced Configuration

### Custom Hints Engine

Extend Sherpa's recovery logic via the `custom_hints` array in `state.json`. These are matched using a high-performance, cached regex engine.

```json
{
  "custom_hints": [
    {
      "pattern": "Permission denied",
      "hint": "Tip: You are in a restricted directory. Try using your home folder or sudo."
    }
  ]
}
```

---

## System Architecture

Sherpa sits as **transparent middleware** between the LLM and the Hermes Tool Registry, intercepting every hook to apply heuristic and schema-based corrections:

1. **Pre-LLM Hook**: Injects nudges and cheatsheets into the context before the LLM generates a response.
2. **Tool Call Hook**: Intercepts outgoing tool calls to perform silent schema validation and argument repair.
3. **Result Hook / Post-Tool Hook**: Monitors tool results to detect looping patterns, apply error-pattern hints, and redact sensitive substrings.

---

## Performance and Benchmarking

Sherpa records every silent repair, block, and nudge in `state.json` and `events.jsonl`. You can project those recorded counts into estimated token / latency / cost savings with the bundled benchmark tool:

```bash
make benchmark
```

The report multiplies your real recorded counts by configurable per-incident estimates (token sizes, turn latency) defined in the `BENCHMARK_CONSTANTS` dict inside `benchmark.py`. **When no local session data exists yet, the report is generated from all-zero defaults and clearly flags this** with a `⚠ NO REAL DATA` banner rather than presenting fabricated numbers. Adjust the multipliers in `BENCHMARK_CONSTANTS` to match your model's real token costs.

### Engineering Performance

- **Cross-Platform Atomic Transactions**: Uses `fcntl` (Unix) or `msvcrt` (Windows) cross-process locking plus atomic `replace()` to keep state consistent across concurrent Hermes instances. (When neither backend is available, locking fails open rather than ever blocking the CLI.)
- **Minimal Latency**: Multi-level caching for state and regex patterns, plus a single-lock `_update_state` path (no double/triple flock), keeps per-turn overhead negligible on the hot path.
- **Startup Stability**: Non-blocking locks with a bounded retry budget ensure the CLI never hangs, even when the state file is contested.

---

## Development and Testing

This plugin includes a full test suite and a `Makefile` for local verification.

### Running Checks

```bash
# Run static analysis (linter)
make lint

# Run the pytest suite (194 tests)
make test

# Run both linter and tests in order
make check
```

### Test Coverage

Tests are located in `tests/` and run against an isolated temporary environment. They verify:

- Core integration hooks (`pre_llm_call`, `pre_tool_call`, `post_tool_call`, and others)
- Argument repair rules, fuzzy normalization, and smart-quote translation
- Sequence loop detection, cycle handling, and nudge injection
- Safety checks, required parameter enforcement, and read damping
- Core state locking, safe log rotation, and thread/timer cleanup
- Command linting, prompt sanitization, and path suggestions
- Plugin configuration consistency
- JSON and CSV export functionality

---

## License

MIT License. See [LICENSE](https://github.com/nordicnode/model-sherpa/blob/main/LICENSE) for the full text.

---

*If Model Sherpa saves you from a token-burning loop or a leaked API key, consider giving it a ⭐ — it helps other Hermes users find it.*
