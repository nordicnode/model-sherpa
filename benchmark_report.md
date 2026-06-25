# Model Sherpa Performance Benchmark

> ⚠ **NO REAL DATA** — No local session data was found. All counters below are zero. Run a session with model-sherpa enabled to see real metrics.

---

Data source: **none**

---

## Executive Summary

Based on **10** monitored agent runs, Model Sherpa has intercepted and repaired fumbles in real-time, delivering the following cumulative performance metrics:

- **LLM API Calls Prevented**: **0 calls** (failures silently repaired)
- **Cumulative Context Saved**: **0 tokens** (redundant payload blocked)
- **Developer Time Saved**: **0.0 seconds** (0.00 minutes of execution latency)
- **Estimated Cost Reduction**: **$0.0000** (reduced token footprint)

---

## Detailed Metric Breakdown

| Metric | Count | Description |
| :--- | :---: | :--- |
| **Argument Rewrites** | 0 | Silent alias repair (e.g. cmd→command) |
| **Command Lints** | 0 | Shell prompt / cd extraction fixes |
| **Loop Detections** | 0 | Repeated-call patterns caught |
| **Error Hints** | 0 | Contextual error-pattern hints fired |
| **Read Damping** | 0 | Redundant file reads blocked |
| **Arg Guard Blocks** | 0 | Empty/missing required arg blocks |
| **Did-You-Mean** | 0 | File-closest-sibling suggestions |
| **Tool DYM** | 0 | Closest-registered-tool suggestions |
| **Re-anchors** | 0 | Original goal reinjected |
| **Plan Nudges** | 0 | Multi-step → todo nudges |

---

## How it Works (Under the Hood)

### 1. Universal Argument Repair
When an LLM agent uses deprecated or hallucinated parameter names (e.g. calling `terminal(cmd=...)` instead of `terminal(command=...)`), Model Sherpa intercepts and silently rewrites the arguments.
* **Without Sherpa**: The registry rejects the call, returning a schema error. The LLM must process the error and re-generate the call, consuming another full API roundtrip.
* **With Sherpa**: The call is corrected and executes successfully on the first attempt.

### 2. Smart Read Range Damping
If an agent attempts to read the same file content multiple times in a single turn, Model Sherpa blocks the tool execution and reminds the model that the content is already present in its context window.
* **Without Sherpa**: The tool returns the file contents, adding thousands of redundant tokens into the session context. Due to how LLM history accumulates, these duplicate tokens are re-sent on every subsequent turn, polluting the context and raising API costs.
* **With Sherpa**: The redundant read is blocked. The agent is nudged to read its own history, keeping the context clean.

### 3. Command Syntax Linting
Agents frequently paste shell prompts (e.g., `$ ls -la` or `cd /path && cmd`) into terminal tool arguments. Model Sherpa automatically strips prompts and extracts `cd` commands into the proper `workdir` parameter.
* **Without Sherpa**: The shell returns syntax errors or fails to preserve directories between calls, causing subsequent commands to execute in the wrong folder.
* **With Sherpa**: Commands execute successfully in the correct directory.
