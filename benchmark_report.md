# Model Sherpa Performance Benchmark

This benchmark is generated using **real historical data** from your local agent sessions (`state.json`). It measures the efficiency gains, latency reductions, and financial savings directly attributed to the Model Sherpa middleware layer.

---

## Executive Summary

Based on **65** monitored agent runs, Model Sherpa has intercepted and repaired fumbles in real-time, delivering the following cumulative performance metrics:

- **LLM API Calls Prevented**: **44 calls** (failures silently repaired)
- **Cumulative Context Saved**: **980,100 tokens** (redundant payload blocked)
- **Developer Time Saved**: **123.2 seconds** (2.05 minutes of execution latency)
- **Estimated Cost Reduction**: **$0.0788** (reduced token footprint)

---

## Detailed Metric Breakdown

| Metric | Without Model Sherpa | With Model Sherpa | Efficiency Gain / Savings |
| :--- | :---: | :---: | :---: |
| **Failed Tool Executions** | 44 | 0 | **100% Error Prevention** |
| **Average Turns to Goal** | 91.0 | 65 | **~30% Fewer Roundtrips** |
| **Redundant File Reads** | 11 | 0 | **100% Redundancy Damped** |
| **Context Window Overhead** | 980,100 tokens | 0 tokens | **980,100 tokens saved** |
| **API Latency Overhead** | 123.2s | 0.0s | **123.2s saved** |

---

## How it Works (Under the Hood)

### 1. Universal Argument Repair
When an LLM agent uses deprecated or hallucinated parameter names (e.g. calling `terminal(cmd=...)` instead of `terminal(command=...)`), Model Sherpa intercepts and silently rewrites the arguments.
* **Without Sherpa**: The registry rejects the call, returning a schema error. The LLM must process the error and re-generate the call, consuming another full API roundtrip.
* **With Sherpa**: The call is corrected in microseconds (< 1ms overhead) and executes successfully on the first attempt.

### 2. Smart Read Range Damping
If an agent attempts to read the same file content multiple times in a single turn, Model Sherpa blocks the tool execution and reminds the model that the content is already present in its context window.
* **Without Sherpa**: The tool returns the file contents, adding thousands of redundant tokens into the session context. Due to how LLM history accumulates, these duplicate tokens are re-sent on every subsequent turn, polluting the context and raising API costs.
* **With Sherpa**: The redundant read is blocked. The agent is nudged to read its own history, keeping the context clean.

### 3. Command Syntax Linting
Agents frequently paste shell prompts (e.g., `$ ls -la` or `cd /path && cmd`) into terminal tool arguments. Model Sherpa automatically strips prompts and extracts `cd` commands into the proper `workdir` parameter.
* **Without Sherpa**: The shell returns syntax errors or fails to preserve directories between calls, causing subsequent commands to execute in the wrong folder.
* **With Sherpa**: Commands execute successfully in the correct directory.
