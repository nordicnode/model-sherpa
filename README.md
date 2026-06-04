# Model Sherpa

> **The ultimate guide-rails and sentinel layer for LLM agents.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Hermes: Plugin](https://img.shields.io/badge/Hermes-Plugin-orange.svg)](https://github.com/nousresearch/hermes)

`model-sherpa` is a high-performance, production-grade safety and optimization layer for the Hermes Agent framework. It acts as an intelligent intermediary, monitoring the "hot path" of model-to-tool interactions to prevent hallucinatory loops, optimize context window usage, and enforce strict operational privacy.

---

## Installation

Install the plugin directly via the Hermes Agent CLI:

```bash
hermes plugins install nordicnode/model-sherpa
```

---

## System Architecture

Sherpa sits as a transparent middleware between the LLM and the Hermes Tool Registry. It intercepts every hook to apply heuristic and schema-based corrections.

The middleware coordinates interactions during several distinct phases:
1. **Pre-LLM Hook**: Injects nudges and cheatsheets into the context before the LLM generates a response.
2. **Tool Call Hook**: Intercepts outgoing tool calls to perform silent schema validation and argument repair.
3. **Result Hook / Post-Tool Hook**: Monitors tool results to detect looping patterns, apply error-pattern hints, and redact sensitive substrings.

---

## Key Guide-Rails

### Arg Guard and Universal Repair
The **Arg Guard** is the first line of defense against model parameter fumbles.
- **Schema Validation**: Intercepts tool calls and validates them against their JSON schema before execution.
- **Smart-Quote Repair**: Automatically converts curly quotes (`“path.txt”`) to standard ASCII, fixing a common LLM copy-paste error.
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
- **Binary Safeguards**: Automatically summarizes non-UTF8 data (`<binary data: 1.2MB>`) to prevent context window pollution.

---

## Slash Commands

| Command | Description |
| :--- | :--- |
| `/sherpa status` | Comprehensive dashboard of lifetime stats and feature states. |
| `/sherpa feature <name> <on/off>` | Real-time toggle for any guide-rail (e.g., `loop_detector`, `read_damper`). |
| `/sherpa doctor` | Runs a diagnostic check on your tool registry to find schema inconsistencies. |
| `/sherpa telemetry` | View the most recent session events with full redaction. |
| `/sherpa add hint <regex> <message>` | Dynamically add new recovery hints without restarting. |

---

## Advanced Configuration

### Custom Hints Engine
You can extend Sherpa's recovery logic via the `custom_hints` array in `state.json`. These are matched using a high-performance, cached regex engine.

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

## Engineering and Performance
- **Atomic Transactions**: Uses `fcntl` cross-process locking and atomic `replace()` to ensure state integrity across multiple Hermes instances.
- **Minimal Latency**: Implements multi-level caching for disk stats and regex patterns. Post-tool hooks are benchmarked to add < 1ms of overhead.
- **Startup Stability**: Uses non-blocking locks to ensure the CLI never hangs, even if the state file is contested.

---

## Development and Testing

This plugin includes a test suite and a `Makefile` for local verification.

### Running Checks

To run the static analysis and verification tests:

```bash
# Run static analysis (linter)
make lint

# Run the pytest suite
make test

# Run both linter and tests in order
make check
```

### Test Coverage

Tests are located in `tests/test_smoke.py` and run against an isolated temporary environment. They verify:
- Core integration hooks (`pre_llm_call`, `pre_tool_call`, `post_tool_call`, and others)
- Argument repair rules, fuzzy normalization, and smart-quote translation
- Sequence loop detection, cycle handling, and nudge injection
- Safety checks, required parameter enforcement, and read damping
- Core state locking, safe log rotation, and thread/timer cleanup
- Command linting, prompt sanitization, and path suggestions
- Plugin configuration consistency

---

## License
MIT License.
