"""
model-sherpa — guide-rails for all models inside Hermes.

Every model, regardless of size or capability, can fumble tool names,
repeat the same mistake, drown in the tool surface, and miss the
"investigate → act → verify" arc. This plugin gives them discreet,
cache-safe assistance uniformly.

Features (each toggleable via /sherpa feature <name> <on|off>):

  rewrites          Silent arg-name repair (always on when plugin is on)
                    terminal(cmd=…)           → terminal(command=…)
                    read_file(file_path=…)    → read_file(path=…)
                    search_files(query=…)     → search_files(pattern=…)
                    …and several more under _ARG_ALIASES.

  aliases           Soft-recover bash / shell / sh / exec / cat / head / tail /
                    grep / rg / egrep / find / ls by pointing unknown-tool
                    calls at the correct canonical Hermes tool.

  alias_tools       Opt-in hard aliases that register bash/cat/grep/etc. as
                    visible tools. Off by default to keep the tool surface
                    canonical and prompt-cache-friendly.

  dry_run           Report repairs/blocks/read damping as advisories without
                    mutating args or blocking tool calls.

  arg_guard         Block empty/missing required args (terminal command='',
                    read_file path='', etc.) and tell the model exactly what
                    field is missing, with the canonical schema preview.

  schema_on_demand  When arg_guard fires, append the tool's parameter list
                    so models can re-emit a correct call next turn.

  read_damper       Block the 4th read of the same file in one turn — the
                    content is already in context above.

  didyoumean        On read_file ENOENT, scan the parent directory and
                    propose the closest sibling filename via difflib.

  plan_first        On a multi-step first user message (≥3 sentences,
                    numbered list, or "then/next/finally" hints), nudge
                    the model to call `todo` before any tools.

  reanchor          Every 10 tool calls, re-inject the original user
                    message verbatim so models don't drift.

Always-on:

  • Per-turn nudges  (pre_llm_call → injected into USER msg, cache-safe)
      - First turn: a compact tool-name cheatsheet
      - Subsequent turns: only when pressure detected (loop, ≥2 errors,
        late iteration count, re-anchor cadence)

  • Error-pattern hints (post_tool_call → next-turn injection)
      - "No such file" / ENOENT, "command not found", "Permission denied",
        "timeout", "JSON decode error", "context length exceeded"

  • Loop detection (post_tool_call)
      - Same (tool, args-hash) ≥3 times in a row → next-turn STOP nudge

  • Stop-condition reminder (pre_llm_call)
      - At ≥15 tool calls without a final answer, inject "finish if you
        have the answer".

  • In-turn Tip footer (transform_tool_result)
      - On an errored tool result, append a one-line [SHERPA] Tip: so the
        model sees the hint immediately, not just next turn.

Slash commands:
  /sherpa                   show status (features, all stats)
  /sherpa on | off          master switch
  /sherpa feature <n> <v>   toggle one feature
  /sherpa cheatsheet        print the first-turn cheatsheet
  /sherpa aliases           list the hallucinated-tool aliases registered
  /sherpa telemetry [N]     show recent per-session Sherpa events
  /sherpa add <pat> <hint>  add a custom error-regex → hint mapping
  /sherpa rules             list current arg-repair + error-hint rules
  /sherpa doctor            diagnose registry, aliases, state, and logs
  /sherpa reset             clear runtime counters
  /sherpa log [N]           tail the corrections log

Design invariants:
  - No system-prompt mutation. Ever. (prompt cache must not break.)
  - All per-turn context is injected into the USER message only.
  - Hooks are fail-open: any exception is logged and swallowed.
  - State is per Hermes home directory via HERMES_HOME.
  - Soft aliases keep the model-facing tool surface canonical. Hard alias
    tools are visible and therefore opt-in via `alias_tools`.
"""

from __future__ import annotations

import contextlib
import copy
import difflib

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
import functools
import hashlib
import json
import logging
import os
import re
import shlex
import textwrap
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("model-sherpa")

# ---------------------------------------------------------------------------
# Paths & persistence
# ---------------------------------------------------------------------------

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
STATE_DIR = HERMES_HOME / "memories" / "model-sherpa"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.lock"  # fcntl flock target (Bug #1 fix)
LOG_FILE = STATE_DIR / "corrections.log"
EVENT_LOG_FILE = STATE_DIR / "events.jsonl"

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
# Above this length we stop scanning the full result text in the linear
# error/hint detectors (_match_error_hint, _looks_like_error) and instead
# sample both ends of the buffer. 8 KiB is comfortably larger than any
# realistic error message but small enough to keep regex passes O(1).
MAX_ERROR_OUTPUT_LENGTH = 8192
# Size of each end-sample (in characters) and the truncation length used
# when bounding user messages for the multi-step detector
# (_is_multistep_request). 4 KiB is enough to cover most real prompts and
# keeps the detector from being a quadratic hazard on accidentally-pasted
# multi-MB blobs.
MAX_TOOL_RESULT_LENGTH = 4000

__version__ = "0.3.1"  # keep in sync with plugin.yaml

_state_lock = threading.RLock()
# RLocks so log-rotation (which re-acquires the same lock inside _rotate_file)
# does not deadlock with the caller that already holds it.
_log_lock = threading.RLock()
_event_log_lock = threading.RLock()
_event_rotate_lock = threading.RLock()  # separate lock so event-rotation doesn't contend with correction-log writes

_last_stat_time = 0.0
_last_correction_rotation = 0.0  # Bug #3 fix: was referenced but never assigned
_last_event_rotation = 0.0  # Separate timestamp for event-log rotation to avoid coupling

# In-memory cache for _load_state. Hooks (pre_tool_call, post_tool_call,
# pre_llm_call) call _feature() many times per turn, which used to re-read,
# re-parse, and deep-merge state.json on every invocation. We cache the
# merged dict and invalidate when (a) _save_state writes new state, or
# (b) the on-disk file mtime/size changes (handles external edits/resets).
_state_cache: Optional[Dict[str, Any]] = None
_state_cache_sig: Optional[Tuple[float, int, int]] = None  # (mtime, size, inode)

_DEFAULT_STATE: Dict[str, Any] = {
    "enabled": True,
    "custom_hints": [],  # [{"pattern": "...", "hint": "..."}, ...]
    # Feature toggles for the v0.2 additions. All ship ON.
    "features": {
        "aliases": True,  # soft bash/cat/grep/find/etc. recovery hints
        "alias_tools": False,  # hard-register alias tools (visible; opt-in)
        "dry_run": False,  # report repairs/blocks without applying them
        "didyoumean": True,  # ENOENT → closest filename suggestion
        "reanchor": True,  # reinject original goal on loop / every 10
        "plan_first": True,  # multi-step prompts → nudge to use `todo`
        "arg_guard": True,  # block empty/bogus required args
        "schema_on_demand": True,  # tool-arg validation failure → show schema
        "read_damper": True,  # block 4th read of same path in one turn
        "command_lint": True,  # repair common terminal command mistakes
    },
    "stats": {
        "rewrites": 0,
        "hints": 0,
        "loops": 0,
        "cheatsheets": 0,
        "aliases_used": 0,
        "didyoumean": 0,
        "reanchors": 0,
        "plan_nudges": 0,
        "arg_blocks": 0,
        "read_blocks": 0,
        "cmd_lints": 0,
        "tool_dym": 0,
        "dry_runs": 0,
        "nudges_suppressed": 0,
        # per_tool[tool_name][stat_key] → count
        "per_tool": {},
    },
}


def _deep_merge(defaults: Any, override: Any) -> Any:
    """Recursive deep-merge that preserves nested dict defaults.

    Unlike dict.update(), nested dicts are merged key-by-key so that adding
    a new default sub-key in a future version doesn't get wiped out by a
    user state file that only set sibling keys.
    """
    if isinstance(defaults, dict) and isinstance(override, dict):
        out = dict(defaults)
        for k, v in override.items():
            if k in out:
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out
    return override


@contextlib.contextmanager
def _lock_file(lock_path: Path = LOCK_FILE, mode: int = 0):
    """Acquire a cross-process lock on a specific file path.

    Defaults to LOCK_FILE (the state-file lock) so existing call-sites that
    only need to serialize state.json access can call ``_lock_file(mode=...)``
    without specifying a path. Pass an explicit ``lock_path`` for other files
    (e.g. log rotation targets).

    Uses LOCK_NB with a retry loop so a contested lock does not block the CLI
    indefinitely, but still prevents race conditions by attempting to acquire
    the lock multiple times before failing open.
    """
    if fcntl is None:
        yield
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as f:
            acquired = False
            for _ in range(10):
                try:
                    fcntl.flock(f, mode | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.01)
            if not acquired:
                logger.debug(
                    "model-sherpa: lock acquisition on %s failed after retries, proceeding without lock", lock_path
                )
            yield
    except Exception as exc:
        logger.debug("model-sherpa: cross-process lock on %s unavailable: %s", lock_path, exc)
        yield


@contextlib.contextmanager
def _lock_state_file(mode: int):
    """Acquire a cross-process lock on state.lock (LOCK_SH or LOCK_EX).

    Thin shim over :func:`_lock_file` that defaults to ``LOCK_FILE``.
    Retained for backward compatibility with call-sites that pre-date the
    consolidation.
    """
    with _lock_file(LOCK_FILE, mode):
        yield


def _load_state(bypass_temporal_block: bool = False) -> Dict[str, Any]:
    global _state_cache, _state_cache_sig, _last_stat_time
    with _state_lock:
        now = time.time()
        # Fast path 1: If we have a cache and it was checked very recently, return it directly.
        # Reduced from 1.0s to 0.05s to eliminate the "blind spot" for multi-agent swarms.
        if not bypass_temporal_block and _state_cache is not None and (now - _last_stat_time) < 0.05:
            return copy.deepcopy(_state_cache)

        _last_stat_time = now
        # Fast path 2: return a deep copy of the cached merged state if the
        # on-disk file hasn't changed since we last loaded it. The copy
        # ensures callers can mutate freely (and then _save_state) without
        # corrupting the cache for everyone else.
        try:
            st = STATE_FILE.stat()
            # Include inode to catch atomic replaces even if mtime/size don't change.
            sig: Optional[Tuple[float, int, int]] = (st.st_mtime, st.st_size, getattr(st, "st_ino", 0))
        except FileNotFoundError:
            sig = None
        except Exception:
            sig = None

        if _state_cache is not None and sig == _state_cache_sig:
            return copy.deepcopy(_state_cache)

        with _lock_state_file(fcntl.LOCK_SH if fcntl else 0):
            if sig is None:
                merged = copy.deepcopy(_DEFAULT_STATE)
                _state_cache = merged
                _state_cache_sig = None
                return copy.deepcopy(merged)

            try:
                data = json.loads(STATE_FILE.read_text())
            except Exception:
                merged = copy.deepcopy(_DEFAULT_STATE)
                _state_cache = merged
                _state_cache_sig = sig
                return copy.deepcopy(merged)

        if isinstance(data, dict):
            # Removed: model/tier-specific behavior. Safeguards apply uniformly
            # to every model, so old persisted profile keys are ignored.
            data.pop("profile", None)
        # Deep-merge so nested dicts (features, stats) keep all defaults,
        # not just the ones the user happened to set.
        merged = _deep_merge(copy.deepcopy(_DEFAULT_STATE), data)
        _state_cache = merged
        _state_cache_sig = sig
        return copy.deepcopy(merged)


def _update_state(func: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Atomically load, modify, and save state with both thread and process locking."""
    with _state_lock, _lock_state_file(fcntl.LOCK_EX if fcntl else 0):
        # We bypass the temporal block to ensure we see the absolute latest
        # on-disk state before applying the mutation.
        state = _load_state(bypass_temporal_block=True)
        func(state)
        _save_state(state)
        return state


def _feature(name: str) -> bool:
    """True if the named feature is enabled (and the plugin itself is on)."""
    s = _load_state()
    if not s.get("enabled"):
        return False
    return bool((s.get("features") or {}).get(name, False))


def _save_state(state: Dict[str, Any]) -> None:
    global _state_cache, _state_cache_sig
    with _state_lock:
        # Update the persisted file using an atomic replace to avoid
        # corruption during power failure or crash.
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            with _lock_state_file(fcntl.LOCK_EX if fcntl else 0):
                with tmp.open("w") as f:
                    json.dump(state, f, indent=2, sort_keys=True)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                tmp.replace(STATE_FILE)
        finally:
            # Cleanup: on the success path tmp.replace() already moved the file,
            # so tmp.exists() is False. This only fires on failure (e.g. disk full,
            # crash between write and replace) to remove the orphaned temp file.
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
        # Refresh the in-memory cache so subsequent _load_state() calls
        # immediately see the new values without re-reading the file.
        try:
            st = STATE_FILE.stat()
            _state_cache = copy.deepcopy(state)
            _state_cache_sig = (st.st_mtime, st.st_size, getattr(st, "st_ino", 0))
        except Exception:
            # If stat fails, drop the cache so the next read re-loads.
            _state_cache = None
            _state_cache_sig = None


def _rotate_file(
    path: Path,
    max_size: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    lock: Optional[Union[threading.Lock, threading.RLock]] = None,
) -> None:
    """Atomic size-based log rotation (e.g. log.jsonl -> log.1.jsonl).

    Issue #8 fix: callers can pass a per-file lock so the event log's rotation
    doesn't contend with the corrections log's writes through the shared
    ``_log_lock``. The lock is optional for backward compatibility — without
    it, we fall back to ``_log_lock`` (the historical behavior).
    """
    effective_lock = lock if lock is not None else _log_lock
    try:
        with effective_lock:
            # Check size inside the lock to prevent concurrent double-rotation.
            if not path.exists() or path.stat().st_size < max_size:
                return
            # Use cross-process lock for log rotation to prevent different processes
            # from corrupting log files during simultaneous rotation.
            lock_path = path.with_suffix(path.suffix + ".lock")
            with _lock_file(lock_path, fcntl.LOCK_EX if fcntl else 0):
                # Re-verify existence and size after acquiring cross-process lock.
                if not path.exists() or path.stat().st_size < max_size:
                    return
                for i in range(backup_count - 1, 0, -1):
                    s = path.with_suffix(f".{i}{path.suffix}")
                    d = path.with_suffix(f".{i + 1}{path.suffix}")
                    if s.exists():
                        try:
                            s.replace(d)
                        except Exception as exc:
                            logger.debug("model-sherpa: log rotation rename failed: %s", exc)
                try:
                    path.replace(path.with_suffix(f".1{path.suffix}"))
                except Exception as exc:
                    logger.debug("model-sherpa: log rotation final replace failed: %s", exc)
    except Exception as exc:
        logger.debug("model-sherpa: log rotation for %s failed: %s", path, exc)


def _migrate_state() -> None:
    """Apply one-way cleanup for deprecated persisted keys."""
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        return
    if not isinstance(data, dict) or "profile" not in data:
        return
    data.pop("profile", None)
    _save_state(_deep_merge(json.loads(json.dumps(_DEFAULT_STATE)), data))


def _log_correction(kind: str, detail: str) -> None:
    """Append a timestamped entry to the corrections log."""
    global _last_log_entry, _last_correction_rotation
    now = time.time()
    with _log_lock:
        if _last_log_entry is not None:
            last_kind, last_detail, last_time = _last_log_entry
            if last_kind == kind and last_detail == detail and (now - last_time) < 30:
                return
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            if (now - _last_correction_rotation) > 10.0:
                _rotate_file(LOG_FILE)
                _last_correction_rotation = now
            with LOG_FILE.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{kind}] {detail}\n")
            _last_log_entry = (kind, detail, now)
        except Exception as exc:
            logger.debug("model-sherpa: failed to write correction log: %s", exc)


def _flush_stats_safely() -> None:
    """Wrapper that swallows I/O errors so the daemon timer thread never
    dumps an unhandled exception to stderr."""
    try:
        _flush_stats()
    except Exception as exc:
        logger.warning("model-sherpa: background stats flush failed: %s", exc)


def _ensure_periodic_flush() -> None:
    """Start a self-rescheduling periodic flush timer if one isn't already
    running.  Fires every _PERIODIC_FLUSH_INTERVAL seconds regardless of
    stat activity, so a gateway crash / SIGKILL loses at most one interval's
    worth of pending increments."""
    global _periodic_flush_timer
    with _timer_lock:
        if _periodic_flush_timer is not None:
            return  # already running

        def _tick():
            _flush_stats_safely()
            global _periodic_flush_timer
            with _timer_lock:
                with _session_lock:
                    sessions_left = _all_session_ids()
                if sessions_left:
                    _periodic_flush_timer = None
                    _ensure_periodic_flush()
                else:
                    _periodic_flush_timer = None

        _periodic_flush_timer = threading.Timer(_PERIODIC_FLUSH_INTERVAL, _tick)
        _periodic_flush_timer.daemon = True
        _periodic_flush_timer.start()


def _schedule_flush() -> None:
    """Start (or restart) a debounced background timer that flushes pending stats.

    Also ensures the periodic safety-net timer is running so stats are
    persisted at most every _PERIODIC_FLUSH_INTERVAL seconds even during
    active sessions that would otherwise starve the debounced timer.
    """
    global _flush_timer
    with _timer_lock:
        if _flush_timer is not None:
            _flush_timer.cancel()
        _flush_timer = threading.Timer(_FLUSH_DELAY, _flush_stats_safely)
        _flush_timer.daemon = True
        _flush_timer.start()
    _ensure_periodic_flush()


# Issue #11 fix: collision-proof compound key for per-tool stat buckets.
# The previous implementation used "stat:tool" and split on the first ":",
# which would misroute any stat key that legitimately contained a colon.
# A sentinel that cannot appear in user-defined stat names removes that risk.
_PER_TOOL_SEP = "\x1f"  # ASCII Unit Separator — forbidden in JSON keys


def _bump_stat(key: str, n: int = 1) -> None:
    """Increment a stat in memory; the change is persisted automatically."""
    with _stats_lock:
        _pending_stats[key] = _pending_stats.get(key, 0) + n
    _schedule_flush()


def _bump_tool_stat(tool_name: str, key: str, n: int = 1) -> None:
    """Increment both the global stat and the per-tool sub-stat.

    Issue #11 fix: the per-tool bucket is keyed by ``stat + _PER_TOOL_SEP + tool``
    instead of the previous ``stat:tool`` string. The US (0x1F) separator
    cannot appear in a JSON object key, so a future stat name with a colon
    (e.g. ``"foo:bar"``) cannot be misrouted.
    """
    with _stats_lock:
        _pending_stats[key] = _pending_stats.get(key, 0) + n
        compound = f"{key}{_PER_TOOL_SEP}{tool_name}"
        _pending_stats[compound] = _pending_stats.get(compound, 0) + n
    _schedule_flush()


def _flush_stats() -> None:
    """Merge in-memory pending stats into the persistent state file.

    Compound pending keys of the form ``stat<US>tool`` (from _bump_tool_stat)
    are routed into ``stats.per_tool[tool][stat]``. The US separator is an
    ASCII control character (0x1F) that cannot appear in a JSON object key.
    """
    with _stats_lock:
        if not _pending_stats:
            return
        pending = dict(_pending_stats)
        _pending_stats.clear()
    try:

        def update(s):
            for k, v in pending.items():
                if _PER_TOOL_SEP in k:
                    stat_key, tool = k.split(_PER_TOOL_SEP, 1)
                    s["stats"].setdefault("per_tool", {})
                    s["stats"]["per_tool"].setdefault(tool, {})
                    s["stats"]["per_tool"][tool][stat_key] = s["stats"]["per_tool"][tool].get(stat_key, 0) + v
                else:
                    s["stats"][k] = s["stats"].get(k, 0) + v

        _update_state(update)
    except Exception:
        with _stats_lock:
            for k, v in pending.items():
                _pending_stats[k] = _pending_stats.get(k, 0) + v
        raise


# ---------------------------------------------------------------------------
# Cheatsheet
# ---------------------------------------------------------------------------

CHEATSHEET = textwrap.dedent("""\
    [SHERPA] Core Hermes tools — use these names exactly:
      terminal(command=…)            ← NOT bash/shell/sh/exec
      read_file(path=…)              ← NOT cat/head/tail
      search_files(pattern=…)        ← NOT grep/rg/find/ls
      write_file(path=…, content=…)  patch(path=…, …) for edits
      todo(todos=[…])                plan multi-step work before tools
      skill_view(name=…)             load a saved procedure when relevant
      memory(action=…, target=…)     persist durable facts across sessions
    Loop: investigate → act → verify. Stop when you have the answer.
""").strip()


# ---------------------------------------------------------------------------
# Silent argument / tool-name repair
#   Each rule is (tool_predicate, args_predicate, rewriter, reason)
#   Rewriter mutates the args dict in place; returns the (possibly new)
#   tool name. Returning None means "no change".
# ---------------------------------------------------------------------------

# Tools agents commonly hallucinate the name of, mapped to the real tool.
# Note: pre_tool_call only fires for tools that EXIST in the registry, so
# the only meaningful tool-name aliases here are for tools that ARE
# registered (e.g. tools that legitimately exist by another name). The
# more impactful redirect happens in the post_tool_call "no such tool"
# error-pattern hint, which surfaces in the next turn.

# Common arg-name typos for the canonical Hermes tools.
_ARG_ALIASES: Dict[str, Dict[str, str]] = {
    "terminal": {
        "cmd": "command",
        "exec": "command",
        "shell": "command",
        "script": "command",
        "args": "command",
        # Real terminal_tool schema uses `workdir` for the per-call cwd.
        "cwd": "workdir",
        "dir": "workdir",
        "working_directory": "workdir",
        "timeout_seconds": "timeout",
        "timeout_s": "timeout",
        "background_run": "background",
        "bg": "background",
    },
    "read_file": {
        "file_path": "path",
        "filepath": "path",
        "filename": "path",
        "file": "path",
        "start_line": "offset",
        "limit_lines": "limit",
        "lines": "limit",
    },
    "write_file": {
        "file_path": "path",
        "filepath": "path",
        "filename": "path",
        "file": "path",
        "text": "content",
        "body": "content",
        "data": "content",
    },
    "patch": {
        "file_path": "path",
        "filepath": "path",
        "filename": "path",
        "file": "path",
        # patch-mode body: the live schema field is `patch`, not
        # `patch_string`. Map common hallucinations toward the canonical
        # name. (Note: `content` is intentionally NOT mapped here because
        # write_file uses `content`; mapping it would mis-route mixed-mode
        # mistakes.)
        "diff": "patch",
        "patch_string": "patch",
        "patch_content": "patch",
        # replace-mode fields
        "old": "old_string",
        "original": "old_string",
        "find": "old_string",
        "search": "old_string",
        "new": "new_string",
        "replacement": "new_string",
        "replace": "new_string",
    },
    "search_files": {
        "regex": "pattern",
        "query": "pattern",
        "search": "pattern",
        "directory": "path",
        "dir": "path",
        "root": "path",
        # Real search_files schema uses `file_glob`; there is no `include`
        # or `exclude` field. Remap common hallucinated names instead of
        # passing through fields the tool will silently ignore.
        "include": "file_glob",
        "include_pattern": "file_glob",
        "file_pattern": "file_glob",
        "glob": "file_glob",
    },
    "memory": {
        "operation": "action",
        "cmd": "action",
        "text": "content",
        "body": "content",
    },
    "todo": {
        "items": "todos",
        "tasks": "todos",
        "list": "todos",
    },
    "skill_view": {
        "skill": "name",
        "skill_name": "name",
    },
}


# Equivalence classes of argument names. Every member of a group is
# treated as a synonym; if the model passes one but the tool's actual
# schema uses a different member, _repair_args renames it.
#
# This is the bidirectional engine the per-tool _ARG_ALIASES table can't
# express: a hand-rolled wrong→right map can only rewrite in one
# direction (`file_path → path` for read_file). Some third-party tools
# may expect `file_path` while the model passes `path`. The synonym
# groups cover both directions automatically by consulting the tool's
# real schema at repair time.
_ARG_SYNONYM_GROUPS: List[frozenset] = [
    frozenset({"path", "file_path", "filepath", "filename", "file"}),
    frozenset({"content", "text", "body", "data"}),
    frozenset({"command", "cmd", "exec", "shell", "script"}),
    frozenset({"pattern", "regex", "query", "search"}),
    frozenset({"workdir", "working_directory", "cwd", "dir"}),
    frozenset({"timeout", "timeout_seconds", "timeout_s"}),
    frozenset({"background", "background_run", "bg"}),
    frozenset({"action", "operation", "verb"}),
    frozenset({"offset", "start", "start_line"}),
    frozenset({"limit", "lines", "limit_lines", "n", "count"}),
    frozenset({"file_glob", "glob", "include", "include_pattern", "file_pattern"}),
]


def _synonym_group_for(name: str) -> set:
    """Return the set of (lowercase) synonyms for *name*."""
    name_l = name.lower()
    for group in _ARG_SYNONYM_GROUPS:
        if any(g.lower() == name_l for g in group):
            return {g.lower() for g in group}
    return set()


def _schema_property_names(tool_name: str) -> set:
    """Return the set of declared parameter names for *tool_name*, or empty
    if the schema is unavailable."""
    schema = _get_schema(tool_name) or {}
    params = schema.get("parameters") if isinstance(schema, dict) else {}
    if not isinstance(params, dict):
        params = {}
    props = params.get("properties") or {}
    return set(props.keys()) if isinstance(props, dict) else set()


def _normalize_key(key: str) -> str:
    """Return a 'flattened' lowercase version of a key (no underscores or dashes)."""
    return key.lower().replace("_", "").replace("-", "")


def _repair_smart_quotes(value: Any) -> Tuple[Any, bool]:
    """Recursively translate curly quotes inside dicts/lists/strings.

    Returns (new_value, changed). Non-string scalars are returned unchanged.
    The recursion is bounded by _FINGERPRINT_MAX_DEPTH so cyclic / runaway
    structures cannot wedge the repair pass.
    """

    def _walk(v: Any, depth: int, seen: set) -> Tuple[Any, bool]:
        if depth >= _FINGERPRINT_MAX_DEPTH:
            return v, False
        if isinstance(v, str):
            t = v.translate(_SMART_QUOTE_MAP)
            return (t, t != v)
        if isinstance(v, dict):
            if id(v) in seen:
                return v, False
            seen = seen | {id(v)}
            changed = False
            for k, sub in list(v.items()):
                new_sub, c = _walk(sub, depth + 1, seen)
                if c:
                    v[k] = new_sub
                    changed = True
            return v, changed
        if isinstance(v, list):
            if id(v) in seen:
                return v, False
            seen = seen | {id(v)}
            changed = False
            for i, sub in enumerate(v):
                new_sub, c = _walk(sub, depth + 1, seen)
                if c:
                    v[i] = new_sub
                    changed = True
            return v, changed
        return v, False

    return _walk(value, 0, set())


def _repair_args(tool_name: str, args: Dict[str, Any]) -> List[str]:
    """Silently rename misnamed args and fix smart quotes; return list of fixes applied.

    Three passes:
      0) Universal Smart-Quote Repair: Fixes curly quotes anywhere in args,
         including nested lists/dicts (e.g. todos[].content).
      1) Hand-rolled per-tool overrides in _ARG_ALIASES.
      2) Schema-driven fuzzy repair: Matches by case-insensitivity, normalization
         (stripping _/-), and synonym groups.
    """
    fixes: List[str] = []

    # Pass 0 — Smart Quote Repair (recursive into nested dicts/lists so a
    # `todo(todos=[{"content": "“x”"}])` call gets fixed too). Nested
    # containers are mutated in place; immutable top-level strings need an
    # explicit write-back.
    for k, v in list(args.items()):
        new_v, changed = _repair_smart_quotes(v)
        if changed:
            if isinstance(v, str):
                args[k] = new_v
            fixes.append(f"smart-quotes in {k}")

    # Pass 1 — explicit table.
    aliases = _ARG_ALIASES.get(tool_name) or {}
    for wrong, right in aliases.items():
        if wrong in args and right not in args:
            args[right] = args.pop(wrong)
            fixes.append(f"{wrong}→{right}")

    # Pass 2 — schema-driven synonym repair.
    schema_props = _schema_property_names(tool_name)
    if schema_props:
        # Create maps for case-insensitive and normalized lookups
        schema_props_l = {p.lower(): p for p in schema_props}
        schema_props_n = {_normalize_key(p): p for p in schema_props}

        for key in list(args.keys()):
            key_l = key.lower()
            key_n = _normalize_key(key)

            # 1. Exact case-insensitive match
            if key_l in schema_props_l:
                actual_canonical = schema_props_l[key_l]
                if key != actual_canonical:
                    if actual_canonical in args:
                        args.pop(key, None)
                        fixes.append(f"dropped duplicate synonym {key}")
                    else:
                        args[actual_canonical] = args.pop(key)
                        fixes.append(f"{key}→{actual_canonical}")
                continue

            # 2. Fuzzy match (normalized)
            if key_n in schema_props_n:
                actual_canonical = schema_props_n[key_n]
                if actual_canonical in args:
                    args.pop(key, None)
                    fixes.append(f"dropped duplicate synonym {key}")
                else:
                    args[actual_canonical] = args.pop(key)
                    fixes.append(f"{key}→{actual_canonical}")
                continue

            group_l = _synonym_group_for(key)
            if not group_l:
                continue
            # Find which schema property is in the same group.
            target_candidates_l = sorted([p for p in group_l if p in schema_props_l])
            if not target_candidates_l:
                continue

            target_l = target_candidates_l[0]
            target = schema_props_l[target_l]

            if target in args:
                args.pop(key, None)
                fixes.append(f"dropped duplicate synonym {key}")
                continue
            args[target] = args.pop(key)
            fixes.append(f"{key}→{target}")

    return fixes


# ---------------------------------------------------------------------------
# Error-pattern → hint mapping
# ---------------------------------------------------------------------------

_ERROR_HINTS: List[Tuple[re.Pattern, str]] = [
    (
        re.compile(r"no such file|enoent|file not found", re.I),
        "Tip: the path didn't exist. Use `search_files` to locate it first, "
        "or run `terminal(command='ls <dir>')` to inspect what's actually there.",
    ),
    (
        re.compile(r"permission denied|eacces", re.I),
        "Tip: avoid system dirs. Write under `$HERMES_HOME` (e.g. "
        "`~/.hermes/tmp/`) or use `terminal(command='sudo …')` if you must.",
    ),
    (
        re.compile(r"patch: .*failed to apply", re.I),
        "Tip: `patch` is very sensitive to whitespace and line numbers. "
        "If you are having trouble, try using `write_file` to overwrite the "
        "entire file instead, especially if the file is small.",
    ),
    (
        re.compile(r"command not found|: not found", re.I),
        "Tip: that binary isn't on PATH. Try the Python equivalent inside "
        "`execute_code(language='python', …)` or install with the project's "
        "package manager first.",
    ),
    (
        re.compile(r"no such tool|unknown tool|tool .* not (?:found|available)", re.I),
        "Tip: that tool name isn't registered. Common mistakes: use "
        "`terminal` (not bash/shell), `read_file` (not cat), `search_files` "
        "(not grep/find), `write_file` (not echo >).",
    ),
    (
        re.compile(r"timeout|timed out", re.I),
        "Tip: split the work, or pass a larger `timeout` arg. For long jobs "
        "run with `background=true, notify_on_complete=true`.",
    ),
    (
        re.compile(r"json\.decoder\.JSONDecodeError|invalid json", re.I),
        "Tip: pass JSON as a real object, not a string. e.g. "
        '`{"key":"value"}` directly in the args, not `\'{"key":...}\'`.',
    ),
    (
        re.compile(r"context length|max(?:imum)? tokens|too many tokens", re.I),
        "Tip: read smaller chunks with `read_file(offset=…, limit=…)`, or narrow `search_files` with `include=…`.",
    ),
]


def _result_to_text(result: Any) -> str:
    """Coerce any tool result into a printable string for analysis."""
    if result is None:
        return ""
    if isinstance(result, bytes):
        try:
            return result.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary data: {len(result)} bytes>"
    if isinstance(result, (list, dict)):
        try:
            # Sort keys for consistent error matching in tests/logs
            return json.dumps(result, indent=2, sort_keys=True, default=str)
        except Exception:
            return str(result)
    return str(result)


# session_id -> list of compiled custom hints (protected by _hint_cache_lock)
_hint_cache_lock = threading.Lock()
_custom_hint_cache: List[Tuple[re.Pattern, str]] = []
_last_hint_cache_sig: Optional[str] = None


def _match_error_hint(result_text: str) -> Optional[str]:
    global _custom_hint_cache, _last_hint_cache_sig
    if not result_text:
        return None
    # Sample both ends of large outputs
    if len(result_text) > MAX_ERROR_OUTPUT_LENGTH:
        search_text = result_text[:MAX_TOOL_RESULT_LENGTH] + "\n...\n" + result_text[-MAX_TOOL_RESULT_LENGTH:]
    else:
        search_text = result_text

    for pat, hint in _ERROR_HINTS:
        if pat.search(search_text):
            return hint

    # Custom user-added hints with regex caching
    state = _load_state()
    hints = state.get("custom_hints", [])
    if not hints:
        return None

    # Re-compile hints only if the list in state has changed
    try:
        current_sig = hashlib.md5(json.dumps(hints, sort_keys=True).encode(), usedforsecurity=False).hexdigest()
    except Exception:
        current_sig = None

    if current_sig != _last_hint_cache_sig:
        with _hint_cache_lock:
            # Double-check after acquiring lock (another thread may have updated)
            if current_sig != _last_hint_cache_sig:
                new_cache = []
                for entry in hints:
                    if isinstance(entry, dict) and "pattern" in entry and "hint" in entry:
                        try:
                            new_cache.append((re.compile(entry["pattern"], re.I), entry["hint"]))
                        except Exception:
                            continue
                _custom_hint_cache = new_cache
                _last_hint_cache_sig = current_sig

    # Python's GIL protects atomic list reads on the fast path (cache hit);
    # the lock above already synchronizes the slow path (cache miss/rebuild).
    for pat, hint in _custom_hint_cache:
        if pat.search(search_text):
            return hint
    return None


def _looks_like_error(result: Any, tool_name: str = "") -> bool:
    if result is None:
        return False

    parsed = None
    if isinstance(result, dict):
        parsed = result
    elif isinstance(result, str):
        stripped = result.lstrip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except Exception:
                pass

    if isinstance(parsed, dict):
        if parsed.get("error"):
            return True
        # Honor an explicit successful exit code before scanning stderr,
        # because well-behaved CLIs (gcc warnings, ffmpeg banners, npm
        # peer-dep notices) write to stderr while still exiting 0. Treating
        # those as errors poisons the error-streak counter and triggers
        # spurious next-turn hints.
        if "exit_code" in parsed:
            if parsed["exit_code"] != 0:
                return True
            return False
        if "exitCode" in parsed:
            if parsed["exitCode"] != 0:
                return True
            return False
        stderr = parsed.get("stderr")
        if isinstance(stderr, str) and stderr.strip():
            return True

    # Coerce to string for signature checks
    result_text = _result_to_text(result)
    if not result_text:
        return False

    # If the tool is read_file or search_files, successful raw outputs must not be treated as errors
    if tool_name in {"read_file", "search_files"}:
        if isinstance(result, str):
            lower_text = result_text[:200].lower()
            if any(
                lower_text.startswith(p) for p in ("error:", "exception:", "permissionerror:", "filenotfounderror:")
            ):
                return True
            if "traceback (most recent call last)" in lower_text:
                return True
            return False

    # Sample both ends for pattern matching
    if len(result_text) > MAX_ERROR_OUTPUT_LENGTH:
        chunk = result_text[:MAX_TOOL_RESULT_LENGTH] + "\n...\n" + result_text[-MAX_TOOL_RESULT_LENGTH:]
    else:
        chunk = result_text
    chunk_l = chunk.lower()
    if any(
        k in chunk_l
        for k in (
            "traceback",
            "exception",
            "errno",
            "no such file",
            "permission denied",
            "command not found",
        )
    ):
        return True
    return False


def _canonical_read_key_path(path: str) -> str:
    """Best-effort canonical read path for per-turn duplicate detection."""
    if not path:
        return path
    try:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        return str(p.resolve(strict=False))
    except Exception:
        return path


# ---------------------------------------------------------------------------
# Loop / pressure tracking (in-memory, per session)
# ---------------------------------------------------------------------------

# session_id -> deque of (tool_name, args_hash)
_call_history: Dict[str, Deque[Tuple[str, str]]] = {}
# session_id -> (tool, hash) of last loop we nudged about
_last_notified_loop: Dict[str, Any] = {}
# session_id -> list of pending nudges to inject next pre_llm_call.
# Each entry is (kind, text). Kinds dedup within a single turn so e.g.
# repeating loop trips can't stack four near-identical "you called X with
# same args" variants when only the tool name changes.
_pending_nudges: Dict[str, List[Tuple[str, str]]] = {}
# session_id -> recent structured Sherpa events for diagnostics.
_session_events: Dict[str, Deque[Dict[str, Any]]] = {}
# session_id -> kind -> throttle bucket.
_nudge_throttle: Dict[str, Dict[str, Dict[str, int]]] = {}
# session_id -> count of tool calls observed
_call_count: Dict[str, int] = {}
# session_id -> consecutive errors
_error_streak: Dict[str, int] = {}

_LOOP_REPEATS = 3
_STOP_NUDGE_AT_CALLS = 15
_HISTORY_CAP = 16
_REANCHOR_EVERY = 10
_CHEATSHEET_EVERY_TURNS = 25  # re-inject cheatsheet every N user turns
_EVENT_CAP = 200
_TOTAL_NUDGE_CAP = 8000  # cap total injected text (wired up in _pre_llm_call)
_FIRST_USER_MSG_CAP = 1500  # cap re-anchor goal to 1.5k to avoid payload bloat
_DYM_MAX_CANDIDATES = 500  # cap difflib candidate list for better latency

# Per-nudge-kind throttle (Bug #2 fix: were referenced but never defined).
# After a kind hits the per-window limit, suppress further emits of the same
# kind for the next _NUDGE_WINDOW_TURNS user turns. "3 in 5 turns" mirrors
# the cadence of how often a model can re-trip the same failure mode before
# a longer intervention is warranted.
_NUDGE_WINDOW_TURNS = 5
_NUDGE_LIMIT_PER_WINDOW = 3

# Recent events across ended sessions.
_global_events: Deque[Dict[str, Any]] = deque(maxlen=_EVENT_CAP)

# session_id → first user message (re-anchor target)
_first_user_msg: Dict[str, str] = {}
# session_id → {path: [(start, end)]} reset per turn
_read_history: Dict[str, Dict[str, List[Tuple[int, int]]]] = {}
# session_id -> timestamp of last activity
_last_access: Dict[str, float] = {}
# session_id → tool calls since last re-anchor
_calls_since_reanchor: Dict[str, int] = {}
# session_id → count of user turns observed (for periodic cheatsheet refresh)
_turn_count: Dict[str, int] = {}
# Per-session state guard.
_session_lock = threading.RLock()

_SESSION_TTL = 3600.0  # 1 hour
_CLEANUP_INTERVAL = 300.0  # 5 minutes
_cleanup_timer: Optional[threading.Timer] = None

# Stats are accumulated in memory and flushed to disk once per turn (at
# pre_llm_call) and at session end. This avoids synchronous JSON writes
# on every tool-call hook during high-throughput runs.
_stats_lock = threading.Lock()
_pending_stats: Dict[str, int] = {}

# Debounced background flush: every _bump_stat starts/resets a timer.
# This makes persistence resilient even when on_session_end is broken.
_FLUSH_DELAY = 10.0
# Periodic safety-net flush: fires every N seconds regardless of activity
# so an active session that crashes loses at most one interval of stats.
_PERIODIC_FLUSH_INTERVAL = 30.0
_timer_lock = threading.RLock()
_flush_timer: Optional[threading.Timer] = None
_periodic_flush_timer: Optional[threading.Timer] = None
_plugin_ctx: Any = None
_registered_alias_tool_names: set = set()

# Deduplicate corrections log entries (identical kind+detail within 30 s).
_last_log_entry: Optional[Tuple[str, str, float]] = None


_FINGERPRINT_MAX_DEPTH = 32  # Issue #10: cycle / depth guard for fingerprinting.


def _fingerprint_value(key: str, value: Any, _seen: Optional[set] = None, _depth: int = 0) -> Any:
    """Normalize an arg value for stable loop fingerprints.

    Large strings are represented by full-content digests instead of being
    truncated. Truncation made two different long commands look identical when
    they only diverged after the cutoff.

    Issue #10 fix: now protected against cycles and runaway depth. A cyclic
    ``args`` dict (theoretically possible if the framework constructs args
    programmatically rather than parsing JSON) used to recurse forever;
    callers in ``_args_fingerprint`` would then catch ``RecursionError``
    with no useful diagnostic. We now short-circuit on the first repeat
    using an id-set and bail out via a depth limit.
    """
    if _depth >= _FINGERPRINT_MAX_DEPTH:
        return {"__depth_cap__": True}
    if isinstance(value, str):
        if key in {"content", "patch"} or len(value) > 512:
            digest = hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()
            return {"__sha1__": digest, "len": len(value)}
        return value
    # Issue #10: track visited object ids so a cycle becomes a sentinel,
    # not an infinite recursion.
    if _seen is None:
        _seen = set()
    if isinstance(value, (dict, list, tuple)):
        if id(value) in _seen:
            return {"__cycle__": True}
        _seen = _seen | {id(value)}
    if isinstance(value, dict):
        return {str(k): _fingerprint_value(str(k), v, _seen, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fingerprint_value(key, v, _seen, _depth + 1) for v in value]
    return value


def _args_fingerprint(args: Dict[str, Any]) -> str:
    """Hash of args for loop detection without lossy truncation."""
    try:
        lite = {str(k): _fingerprint_value(str(k), v) for k, v in args.items()}
        s = json.dumps(lite, sort_keys=True, default=str)
    except Exception:
        s = repr(args)
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()


_REDACT_KEYS = {"apikey", "token", "password", "secret", "privatekey", "auth"}


def _redact_dict(data: Any) -> Any:
    """Recursively redact sensitive-looking values using substring matching."""
    if isinstance(data, dict):
        return {
            k: ("[REDACTED]" if any(rk in _normalize_key(str(k)) for rk in _REDACT_KEYS) else _redact_dict(v))
            for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [_redact_dict(v) for v in data]
    return data


def _record_call(session_id: str, tool_name: str, args: Dict[str, Any]) -> bool:
    """Return True if this call completed a loop (A-A-A or A-B-A-B)."""
    sid = session_id or "default"
    fp = _args_fingerprint(args)
    call_item = (tool_name, fp)
    with _session_lock:
        _last_access[sid] = time.time()
        hist = _call_history.setdefault(sid, deque(maxlen=_HISTORY_CAP))
        hist.append(call_item)
        _call_count[sid] = _call_count.get(sid, 0) + 1

        # 1. Simple repeat (A-A-A)
        if len(hist) >= _LOOP_REPEATS:
            tail = list(hist)[-_LOOP_REPEATS:]
            if all(item == call_item for item in tail):
                if _last_notified_loop.get(sid) == call_item:
                    return False
                _last_notified_loop[sid] = call_item
                return True

        # 2. Sequence loop (A-B-A-B)
        if len(hist) >= 4:
            items = list(hist)
            if items[-1] == items[-3] and items[-2] == items[-4]:
                pattern = (items[-2], items[-1])
                if _last_notified_loop.get(sid) == pattern:
                    return False
                _last_notified_loop[sid] = pattern
                return True
            # 3. Triple sequence (A-B-C-A-B-C)
            if len(hist) >= 6:
                if items[-1] == items[-4] and items[-2] == items[-5] and items[-3] == items[-6]:
                    triple = (items[-3], items[-2], items[-1])
                    if _last_notified_loop.get(sid) == triple:
                        return False
                    _last_notified_loop[sid] = triple
                    return True

        _last_notified_loop.pop(sid, None)
    return False


def _record_event(session_id: str, kind: str, detail: str, **fields: Any) -> None:
    """Keep bounded, per-session telemetry for /sherpa telemetry."""
    sid = session_id or "default"
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session_id": sid,
        "kind": kind,
        "detail": detail,
    }
    # Redact sensitive fields before updating
    clean_fields = _redact_dict(fields)
    event.update({k: v for k, v in clean_fields.items() if v is not None})
    with _session_lock:
        bucket = _session_events.setdefault(sid, deque(maxlen=_EVENT_CAP))
        bucket.append(event)
        _global_events.append(event)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Issue #8 fix: rotation runs under a per-file lock, not the shared
        # correction-log lock, so writes to events.jsonl don't block on
        # corrections.log rotations (and vice versa).
        with _event_rotate_lock:
            global _last_event_rotation
            now = time.time()
            if (now - _last_event_rotation) > 10.0:
                _rotate_file(EVENT_LOG_FILE, lock=_event_rotate_lock)
                _last_event_rotation = now
        with _event_log_lock, EVENT_LOG_FILE.open("a") as f:
            f.write(json.dumps(event, sort_keys=True, default=str) + "\n")
    except Exception as exc:
        # Issue #16 improvement: log swallowed exceptions so a silent failure
        # leaves a trail. Was previously `except Exception: pass`.
        logger.debug("model-sherpa: failed to persist event %s: %s", event.get("kind"), exc)


def _load_recent_events_from_disk(session_id: Optional[str], n: int) -> List[Dict[str, Any]]:
    if not EVENT_LOG_FILE.exists():
        return []
    try:
        lines = EVENT_LOG_FILE.read_text().splitlines()[-max(n * 5, n) :]
    except Exception:
        return []
    events: List[Dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except Exception:
            continue
        if session_id and event.get("session_id") != session_id:
            continue
        events.append(event)
    return events[-n:]


def _format_events(session_id: Optional[str] = None, n: int = 30) -> str:
    with _session_lock:
        if session_id:
            items = [(session_id or "default", list(_session_events.get(session_id or "default", [])))]
        else:
            items = [(sid, list(events)) for sid, events in sorted(_session_events.items())]
            if not items:
                items = [("recent", list(_global_events))]
    if session_id and (not items or not items[0][1]):
        disk_events = _load_recent_events_from_disk(session_id, n)
        items = [(session_id or "default", disk_events)]
    elif not session_id and items == [("recent", [])]:
        items = [("recent", _load_recent_events_from_disk(None, n))]
    lines: List[str] = []
    for sid, events in items:
        for event in events[-n:]:
            extras = {k: v for k, v in event.items() if k not in {"ts", "kind", "detail"}}
            suffix = (" " + json.dumps(extras, sort_keys=True, default=str)) if extras else ""
            lines.append(f"{event['ts']} [{sid}] {event['kind']}: {event['detail']}{suffix}")
    if not lines:
        return "(no Sherpa telemetry recorded for active sessions)"
    return "\n".join(lines[-n:])


def _queue_nudge(session_id: str, kind: str, text: str) -> None:
    """Queue a nudge for next pre_llm_call.

    Dedup is by *kind*, not text. Within a single turn we only emit one
    nudge per kind — so a session that trips three different loop variants
    (each with a different tool/args text) still gets exactly one "loop"
    nudge instead of three. First-wins: the earliest text for a given kind
    is the one the model sees, on the theory that later occurrences in the
    same turn are just the same problem manifesting again.
    """
    sid = session_id or "default"
    suppressed = False
    with _session_lock:
        _last_access[sid] = time.time()
        bucket = _pending_nudges.setdefault(sid, [])
        if any(k == kind for k, _ in bucket):
            return
        turn = _turn_count.get(sid, 0)
        by_kind = _nudge_throttle.setdefault(sid, {})
        bucket_state = by_kind.setdefault(kind, {"start": turn, "count": 0, "suppressed_until": -1})
        if turn - bucket_state.get("start", 0) >= _NUDGE_WINDOW_TURNS:
            bucket_state.update({"start": turn, "count": 0, "suppressed_until": -1})
        if turn <= bucket_state.get("suppressed_until", -1):
            suppressed = True
        if bucket_state.get("count", 0) >= _NUDGE_LIMIT_PER_WINDOW:
            bucket_state["suppressed_until"] = turn + _NUDGE_WINDOW_TURNS
            suppressed = True
        if not suppressed:
            bucket_state["count"] = bucket_state.get("count", 0) + 1
            bucket.append((kind, text))
    if suppressed:
        _bump_stat("nudges_suppressed")


def _drain_nudges(session_id: str) -> List[str]:
    sid = session_id or "default"
    with _session_lock:
        return [text for _, text in _pending_nudges.pop(sid, [])]


# ---------------------------------------------------------------------------
# Multi-step / "plan first" detection
# ---------------------------------------------------------------------------

_MULTISTEP_HINTS = re.compile(
    r"\b(?:then|after that|next|finally|also|and (?:also|then)|"
    r"first .* then|step \d|"
    r"(?:1[.)]|2[.)]|3[.)])\s)",
    re.I,
)

# Bug #5 fix: a corrected bullet/numbered-list detector. The old code used
# `\+` which matched a literal `+` — almost nothing a user types. The
# `re.M` flag is baked into the compiled pattern for clarity.
_BULLET_RE = re.compile(
    r"^\s*(?:[-*•]|\d+[.)])\s+\S",
    re.M,
)


def _is_multistep_request(msg: str) -> bool:
    if not msg:
        return False
    if len(msg) > MAX_TOOL_RESULT_LENGTH:
        msg = msg[:MAX_TOOL_RESULT_LENGTH]
    sentences = re.split(r"[.!?\n]+", msg.strip())
    sentences = [s for s in sentences if len(s.split()) >= 3]
    if len(sentences) >= 3:
        return True
    if _MULTISTEP_HINTS.search(msg):
        return True
    # Numbered/bulleted list (≥2 items). See _BULLET_RE for the corrected
    # pattern (Bug #5).
    bullets = _BULLET_RE.findall(msg)
    return len(bullets) >= 2


# ---------------------------------------------------------------------------
# "Did you mean…?" path suggestion
# ---------------------------------------------------------------------------


def _didyoumean_path(path: str) -> Optional[str]:
    """If *path* doesn't exist, propose the closest sibling in its parent dir.

    Climbs up parent directories until it finds an existing ancestor to scan
    for sibling/directory suggestions. This helps models recover from deep
    hallucinated paths (e.g. proposing `~/.hermes/tmp/` when `~/.hermes/missing/deep/`
    is passed).
    """
    if not path or not isinstance(path, str):
        return None
    try:
        p = Path(path).expanduser()
    except Exception:
        return None
    if p.exists():
        return None

    # Climb up to find first existing ancestor.
    ancestor = p.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent

    if not ancestor.exists() or not ancestor.is_dir():
        return None

    # Skip DYM for known heavy/noise directories to avoid latency spikes
    if ancestor.name in {"node_modules", ".git", ".venv", "vendor", "__pycache__"}:
        return None

    # Sample a bounded prefix of large directories.
    try:
        siblings: List[str] = []
        entries_seen = 0
        with os.scandir(ancestor) as it:
            for entry in it:
                entries_seen += 1
                siblings.append(entry.name)
                if entries_seen >= _DYM_MAX_CANDIDATES:
                    break
    except OSError:
        return None
    if not siblings:
        return None

    # Case-insensitive mapping
    l_to_orig = {s.lower(): s for s in siblings}
    matches = difflib.get_close_matches(p.name.lower(), l_to_orig.keys(), n=1, cutoff=0.6)
    if not matches:
        return None
    return str(ancestor / l_to_orig[matches[0]])


def _didyoumean_tool(name: str) -> Optional[str]:
    """If *name* isn't a registered tool, propose the closest match via difflib.

    Mirrors _didyoumean_path but for tool names. Only fires when there are
    ≥2 registered tools to compare against.
    """
    if not name or not isinstance(name, str):
        return None
    if _feature("aliases"):
        alias_target = _SOFT_ALIAS_TARGETS.get(name)
        if alias_target:
            return str(alias_target)
    candidates = _registered_tool_names()
    if len(candidates) < 2:
        return None
    if name in candidates:
        return None

    l_to_orig = {c.lower(): c for c in candidates}
    matches = difflib.get_close_matches(name.lower(), l_to_orig.keys(), n=1, cutoff=0.6)
    if not matches:
        return None
    return l_to_orig[matches[0]]


# ---------------------------------------------------------------------------
# Registry helpers — centralize the few places we peek at the tool registry
# so internal refactors only have to update one spot. Uses public APIs
# (get_entry, get_all_tool_names, get_schema) instead of `_tools` directly.
# ---------------------------------------------------------------------------


def _registry():
    """Lazy import + fail-soft accessor for the global tool registry."""
    try:
        from tools.registry import registry

        return registry
    except Exception as exc:  # pragma: no cover
        logger.debug("model-sherpa: tools.registry unavailable: %s", exc)
        return None


def _registry_tools() -> Dict[str, Any]:
    """Return the registry's tool-entry mapping.

    Single chokepoint for every read of the registry's internal `_tools`
    attribute. Previously we leaked this private access in three call
    sites (`_register_aliases`, `/sherpa aliases`, schema preview); routing
    them all through here means a future registry refactor only has to be
    chased down in this one helper. Always returns an empty dict on
    failure so callers can iterate / `in`-check without a None branch.
    """
    reg = _registry()
    if reg is None:
        return {}
    # Public iteration: prefer get_all_tool_names() when available, then
    # rehydrate entries via get_entry() so we never touch `_tools`.
    names_getter = getattr(reg, "get_all_tool_names", None)
    entry_getter = getattr(reg, "get_entry", None)
    if callable(names_getter) and callable(entry_getter):
        try:
            return {n: entry_getter(n) for n in names_getter()}
        except Exception:
            pass
    # Fallback for stub registries used in tests — this is the ONLY
    # place in the plugin that touches the private attribute.
    return dict(getattr(reg, "_tools", {}) or {})


def _tool_registry_generation() -> int:
    """Return the registry generation when available, otherwise 0.

    Hermes increments this on tool registration/deregistration, including MCP
    refreshes. Keying caches on it keeps schema and name lookups fresh without
    giving up cheap cached reads in the common static case.
    """
    reg = _registry()
    if reg is None:
        return 0
    try:
        return int(getattr(reg, "_generation", 0) or 0)
    except Exception:
        return 0


@functools.lru_cache(maxsize=8)
def _registered_tool_names_cached(generation: int) -> List[str]:
    """Return registered tool names for a specific registry generation."""
    return list(_registry_tools().keys())


def _registered_tool_names() -> List[str]:
    return _registered_tool_names_cached(_tool_registry_generation())


def _has_tool(name: str) -> bool:
    return name in _registered_tool_names()


@functools.lru_cache(maxsize=512)
def _get_schema_cached(name: str, generation: int) -> Optional[Dict[str, Any]]:
    """Return the JSON Schema for *name* at a registry generation."""
    reg = _registry()
    if reg is None:
        return None
    # Public API first — avoids materialising the full tools dict.
    getter = getattr(reg, "get_schema", None)
    if callable(getter):
        try:
            schema = getter(name)
            return schema if isinstance(schema, dict) else None
        except Exception:
            return None
    entry = _registry_tools().get(name)
    raw = getattr(entry, "schema", None) if entry else None
    return raw if isinstance(raw, dict) else None


def _get_schema(name: str) -> Optional[Dict[str, Any]]:
    return _get_schema_cached(name, _tool_registry_generation())


def _invalidate_tool_cache() -> None:
    """Clear cached tool registry lookups.

    Only needed when tools are registered dynamically at runtime (e.g.
    during alias registration). The registry is otherwise static after
    plugin load.
    """
    _registered_tool_names_cached.cache_clear()
    _get_schema_cached.cache_clear()


# ---------------------------------------------------------------------------
# Tool-schema lookup (for schema-on-demand on arg failures)
# ---------------------------------------------------------------------------


def _render_schema_prop(name: str, prop: Any, required: bool, depth: int = 0) -> str:
    """Recursively render a JSON Schema property into a compact string."""
    star = "*" if required else ""
    if not isinstance(prop, dict) or depth > 3:
        return f"{name}{star}"

    # Handle composite types (anyOf, oneOf, allOf)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in prop and isinstance(prop[key], list):
            choices = [_render_schema_prop("", v, False, depth + 1).split(": ", 1)[-1] for v in prop[key]]
            sep = " | " if key != "allOf" else " & "
            return f"{name}{star}: ({sep.join(choices)})"

    ptype = prop.get("type", "any")
    if "enum" in prop and isinstance(prop["enum"], list):
        # Format enum values compactly
        enum_str = " | ".join(json.dumps(v) for v in prop["enum"])
        return f"{name}{star}: {enum_str}"

    if ptype == "object" and "properties" in prop:
        inner_props = prop["properties"]
        req_set = set(prop.get("required") or [])
        bits = [_render_schema_prop(k, v, k in req_set, depth + 1) for k, v in inner_props.items()]
        return f"{name}{star}: {{ {', '.join(bits)} }}"

    if ptype == "array" and "items" in prop:
        inner = _render_schema_prop("", prop["items"], False, depth + 1)
        # Strip the ": " prefix if it was returned by a nested object/array
        inner_val = inner.split(": ", 1)[-1] if ": " in inner else inner
        return f"{name}{star}: [{inner_val}]"

    return f"{name}{star}: {ptype}"


def _tool_schema_preview(tool_name: str) -> Optional[str]:
    """Return a compact recursive preview of the tool's schema.

    Example: todo(todos*: [{id*: string, content*: string, status*: string}])
    """
    schema = _get_schema(tool_name) or {}
    params = schema.get("parameters") if isinstance(schema, dict) else {}
    if not isinstance(params, dict):
        params = {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props or not isinstance(props, dict):
        return f"{tool_name}() — no parameters"
    bits = [_render_schema_prop(name, prop, name in required) for name, prop in props.items()]
    return f"{tool_name}({', '.join(bits)})   * = required"


# ---------------------------------------------------------------------------
# Terminal command linter (command_lint feature)
# ---------------------------------------------------------------------------

# Smart/curly quotes → straight quotes.
_SMART_QUOTE_MAP = str.maketrans(
    {
        "\u201c": '"',  # “ → "
        "\u201d": '"',  # ” → "
        "\u2018": "'",  # ‘ → '
        "\u2019": "'",  # ’ → '
    }
)

# Leading prompt char: $ or % with optional space.
_LEADING_PROMPT_RE = re.compile(r"^\s*[\$%]\s")

# bash -c "..." / bash -c '...' wrapping (quotes optional).
_BASH_WRAP_RE = re.compile(r"^bash\s+(?:--?\w+\s+)*-[\w]*c\s+([\"']?)(.*?)\1\s*$", re.DOTALL)
# sh -c "..." variant (quotes optional).
_SH_WRAP_RE = re.compile(r"^sh\s+(?:--?\w+\s+)*-[\w]*c\s+([\"']?)(.*?)\1\s*$", re.DOTALL)

# cd /abs/path && cmd  /  cd /abs/path; cmd.
_CD_RE = re.compile(
    r"^\s*cd\s+((?:\"[^\"]+\"|'[^']+'|[^;&|]+?))\s*(?:&&|;)\s*",
)


def _lint_terminal_command(command: str, args: Dict[str, Any]) -> Tuple[str, List[str], int]:
    """Inspect and repair a terminal command; return (command, warnings, fix_count).

    Detects and silently repairs four common model mistakes:

    1. Smart/curly quotes (\u201c\u201d\u2018\u2019) → straight ``"`` / ``'``
    2. Leading ``$ `` or ``% `` — copy-pasted shell prompt
    3. ``bash -c "..."`` / ``sh -c "..."`` wrapping (terminal already runs bash)
    4. ``cd /path && cmd`` — extract *cd* into ``workdir``, keep *cmd*

    Changes are applied in-place to *args* where appropriate (workdir).
    The caller is responsible for logging, stats, and toggling via the
    ``command_lint`` feature flag.
    """
    warnings: List[str] = []
    fix_count = 0

    # 1) Smart quotes → straight quotes.
    translated = command.translate(_SMART_QUOTE_MAP)
    if translated != command:
        command = translated
        fix_count += 1

    # 2) Leading $ or % prompt.
    m = _LEADING_PROMPT_RE.match(command)
    if m:
        command = command[m.end() :]
        fix_count += 1

    # 3) bash -c / sh -c wrapping.
    for pat in (_BASH_WRAP_RE, _SH_WRAP_RE):
        m = pat.match(command)
        if m:
            inner = m.group(2)
            # Unwrap only if the inner command is simple and lacks shell operators,
            # variable expansions, escapes, or brace expansions that require a shell.
            if inner.strip() and not re.search(r"[;&|<>$`\\{}]", inner):
                command = inner
                fix_count += 1
            else:
                # Complex inner command — warn instead.
                warnings.append("`bash -c` is unnecessary; `terminal` already runs bash. Pass the command directly.")
            break

    # 4) cd /path && cmd → workdir + stripped command.
    current_workdir = args.get("workdir")
    while True:
        m = _CD_RE.match(command)
        if not m:
            break
        cd_path = m.group(1).strip()
        try:
            parts = shlex.split(cd_path)
            if len(parts) == 1:
                cd_path = parts[0]
        except Exception:
            pass

        if not current_workdir:
            current_workdir = cd_path
            command = command[m.end() :]
            fix_count += 1
        else:
            # Try to combine. If cd_path is absolute, it overrides.
            # If relative, it appends.
            try:
                p_base = Path(current_workdir)
                p_next = Path(cd_path)
                if p_next.is_absolute():
                    current_workdir = str(p_next)
                else:
                    current_workdir = str(p_base / p_next)
                command = command[m.end() :]
                fix_count += 1
            except Exception:
                break

    if current_workdir != args.get("workdir"):
        args["workdir"] = current_workdir

    return command, warnings, fix_count


# Required-field hints used by arg_guard to produce helpful block messages.
_REQUIRED_FIELD_HINTS: Dict[str, Dict[str, str]] = {
    "terminal": {"command": "non-empty shell string"},
    "read_file": {"path": "absolute or ~-relative path to an existing file"},
    "write_file": {"path": "target path", "content": "full file body (string)"},
    "search_files": {"pattern": "regex (for content) or glob like '*.py' (for files)"},
    "memory": {"action": "'add' | 'replace' | 'remove'", "target": "'memory' | 'user'"},
    "skill_view": {"name": "exact skill name (e.g. 'meta/self-evolution')"},
}


def _is_missing_required_value(tool_name: str, field: str, value: Any) -> bool:
    """Return True when a required arg is absent or invalidly empty.

    We are permissive with empty strings for custom tools, only blocking
    them for core tools where an empty value is definitely a model error.
    """
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        # Core fields where empty strings are known to be invalid.
        if tool_name == "terminal" and field == "command":
            return True
        if tool_name in {"read_file", "write_file", "patch", "search_files"} and field == "path":
            return True
        return False
    return False


def _schema_field_hint(prop: Any) -> str:
    if not isinstance(prop, dict):
        return "required"
    if isinstance(prop.get("enum"), list) and prop["enum"]:
        return " | ".join(repr(str(v)) for v in prop["enum"])
    ptype = prop.get("type")
    if isinstance(ptype, list):
        ptype = "|".join(str(v) for v in ptype)
    if ptype:
        return str(ptype)
    return "required"


def _schema_type_matches(expected: Any, value: Any) -> bool:
    if expected is None:
        return True
    types = expected if isinstance(expected, list) else [expected]
    for typ in types:
        if typ == "string" and isinstance(value, str):
            return True
        if typ == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if typ == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if typ == "boolean" and isinstance(value, bool):
            return True
        if typ == "array" and isinstance(value, list):
            return True
        if typ == "object" and isinstance(value, dict):
            return True
        if typ == "null" and value is None:
            return True
    return False


def _schema_validate_value(path: str, prop: Any, value: Any) -> List[str]:
    """Return simple schema validation issues for type/enum/nested required fields."""
    if not isinstance(prop, dict) or value is None:
        return []
    issues: List[str] = []
    enum = prop.get("enum")
    if isinstance(enum, list) and value not in enum:
        issues.append(f"{path} ({' | '.join(repr(str(v)) for v in enum)})")
        return issues
    expected_type = prop.get("type")
    if expected_type is not None and not _schema_type_matches(expected_type, value):
        issues.append(f"{path} ({_schema_field_hint(prop)})")
        return issues
    if isinstance(value, dict):
        child_props = prop.get("properties") or {}
        if isinstance(child_props, dict):
            for field in prop.get("required") or []:
                if isinstance(field, str) and _is_missing_required_value("", field, value.get(field)):
                    issues.append(f"{path}.{field} (required)")
            for field, child_value in value.items():
                child_prop = child_props.get(field)
                issues.extend(_schema_validate_value(f"{path}.{field}", child_prop, child_value))
    if isinstance(value, list):
        item_schema = prop.get("items")
        for i, item in enumerate(value):
            issues.extend(_schema_validate_value(f"{path}[{i}]", item_schema, item))
    return issues


def _schema_required_or_invalid_args(tool_name: str, args: Dict[str, Any]) -> List[str]:
    """Validate against the live registry schema where it is precise enough.

    This handles ordinary required fields and enum values without duplicating
    every tool schema in Sherpa. Conditional requirements (for example,
    memory.replace needing old_text) are still layered below.
    """
    schema = _get_schema(tool_name) or {}
    params = schema.get("parameters") if isinstance(schema, dict) else {}
    if not isinstance(params, dict):
        return []
    props = params.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    missing: List[str] = []
    for field in params.get("required") or []:
        prop = props.get(field) if isinstance(field, str) else None
        # JSON Schema defaults mean the underlying tool can often supply the
        # value, so do not block simply because a defaulted required field is
        # absent. PATCH_SCHEMA currently uses this pattern for mode=replace.
        if isinstance(prop, dict) and "default" in prop and field not in args:
            continue
        if isinstance(field, str) and _is_missing_required_value(tool_name, field, args.get(field)):
            missing.append(f"{field} ({_schema_field_hint(prop)})")
    for field, value in args.items():
        missing.extend(_schema_validate_value(field, props.get(field), value))
    return missing


def _missing_required_args(tool_name: str, args: Dict[str, Any]) -> List[str]:
    """Return human-readable missing/invalid required arg descriptions."""
    schema_missing = _schema_required_or_invalid_args(tool_name, args)
    if tool_name == "memory":
        missing = [
            f"{field} ({desc})"
            for field, desc in _REQUIRED_FIELD_HINTS["memory"].items()
            if _is_missing_required_value("memory", field, args.get(field))
        ]
        action = args.get("action")
        if isinstance(action, str):
            action = action.strip()
        if action and action not in {"add", "replace", "remove"}:
            missing.append("action ('add' | 'replace' | 'remove')")
        if action in {"add", "replace"} and _is_missing_required_value("memory", "content", args.get("content")):
            missing.append("content (required for add/replace)")
        if action in {"replace", "remove"} and _is_missing_required_value("memory", "old_text", args.get("old_text")):
            missing.append("old_text (unique text identifying existing entry)")
        target = args.get("target")
        if isinstance(target, str):
            target = target.strip()
        if target and target not in {"memory", "user"}:
            missing.append("target ('memory' | 'user')")
        return list(dict.fromkeys(missing or schema_missing))

    if tool_name == "todo":
        todos = args.get("todos")
        if todos is None:
            return []
        if not isinstance(todos, list):
            return ["todos (array of {id, content, status})"]
        valid_statuses = {"pending", "in_progress", "completed", "cancelled"}
        missing = []
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                missing.append(f"todos[{i}] (object with id/content/status)")
                continue
            for field in ("id", "content", "status"):
                if _is_missing_required_value("todo", field, item.get(field)):
                    missing.append(f"todos[{i}].{field} (required)")
            status = item.get("status")
            if isinstance(status, str) and status and status not in valid_statuses:
                missing.append(f"todos[{i}].status (pending|in_progress|completed|cancelled)")
        return list(dict.fromkeys(missing or schema_missing))

    if tool_name == "patch":
        mode = args.get("mode")
        if mode is None or (isinstance(mode, str) and not mode.strip()):
            mode = "patch" if args.get("patch") else "replace"
        if mode not in {"replace", "patch"}:
            return list(dict.fromkeys([*schema_missing, "mode ('replace' or 'patch')"]))
        if mode == "patch":
            if _is_missing_required_value("patch", "patch", args.get("patch")):
                return list(dict.fromkeys([*schema_missing, "patch (V4A patch content)"]))
            return schema_missing
        required = {
            "path": "target file path",
            "old_string": "text to find",
            "new_string": "replacement text (may be empty)",
        }
        return list(
            dict.fromkeys(
                schema_missing
                + [
                    f"{field} ({desc})"
                    for field, desc in required.items()
                    if _is_missing_required_value("patch", field, args.get(field))
                ]
            )
        )

    req = _REQUIRED_FIELD_HINTS.get(tool_name) or {}
    return list(
        dict.fromkeys(
            schema_missing
            + [
                f"{field} ({desc})"
                for field, desc in req.items()
                if _is_missing_required_value(tool_name, field, args.get(field))
            ]
        )
    )


def _pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Silent arg rewrite, arg-guard block, redundant-read block.

    Return contract: ``None`` means "no change" — let the framework dispatch
    the call as-is. A dict ``{"action": "block", "message": str}`` means
    "abort the call; show ``message`` to the model". This contract is part
    of Hermes' pre-tool-call interface and should not change without
    coordinating with the framework.

    Fail-open: any unhandled exception is logged and the original ``args``
    are returned unchanged (None) so a buggy Sherpa never crashes the host
    agent. See CONTRIBUTING.md ("Code Style & Design Rules > Safety First").
    """
    try:
        return _pre_tool_call_impl(
            tool_name=tool_name,
            args=args,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    except Exception as exc:
        logger.exception(
            "model-sherpa: _pre_tool_call failed for tool=%s (%s); failing open with original args",
            tool_name,
            exc,
        )
        return None


def _pre_tool_call_impl(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Implementation of _pre_tool_call. See _pre_tool_call for the contract."""
    state = _load_state()
    if not state.get("enabled") or not isinstance(args, dict):
        return None
    dry_run = bool((state.get("features") or {}).get("dry_run", False))

    # 1) Silent arg rename (always on when plugin is enabled)
    repair_target = dict(args) if dry_run else args
    fixes = _repair_args(tool_name, repair_target)
    effective_args = repair_target if dry_run else args
    if fixes:
        stat_key = "dry_runs" if dry_run else "rewrites"
        _bump_tool_stat(tool_name, stat_key, len(fixes))
        kind = "dry_run" if dry_run else "rewrite"
        detail = f"{tool_name}: {', '.join(fixes)}"
        _log_correction(kind, detail)
        _record_event(session_id, kind, detail, tool=tool_name, fixes=fixes)
        if dry_run:
            _queue_nudge(
                session_id,
                "dry_run",
                f"[SHERPA dry-run] Would repair `{tool_name}` args: {', '.join(fixes)}.",
            )

    # 2) Terminal command lint (command_lint feature)
    if _feature("command_lint") and tool_name == "terminal":
        raw_cmd = (effective_args.get("command") or "").strip()
        if raw_cmd:
            lint_args = effective_args
            new_cmd, warnings, lint_count = _lint_terminal_command(raw_cmd, lint_args)
            if lint_count:
                stat_key = "dry_runs" if dry_run else "cmd_lints"
                _bump_tool_stat(tool_name, stat_key, lint_count)
                kind = "dry_run" if dry_run else "cmd_lint"
                detail = f"terminal: {lint_count} fix(es) → {new_cmd[:120]}"
                _log_correction(kind, detail)
                _record_event(session_id, kind, detail, tool=tool_name)
                if dry_run:
                    _queue_nudge(
                        session_id,
                        "dry_run",
                        f"[SHERPA dry-run] Would lint terminal command to: `{new_cmd}`.",
                    )
            if new_cmd != raw_cmd:
                effective_args["command"] = new_cmd
            for w in warnings:
                _queue_nudge(session_id, "cmd_lint", f"[SHERPA] {w}")

    # 3) Empty / missing required-arg block (arg_guard feature)
    if _feature("arg_guard"):
        missing = _missing_required_args(tool_name, effective_args)
        if missing:
            schema_preview = _tool_schema_preview(tool_name) if _feature("schema_on_demand") else None
            _bump_tool_stat(tool_name, "dry_runs" if dry_run else "arg_blocks")
            _log_correction("arg_block", f"{tool_name}: missing {missing}")
            _record_event(
                session_id,
                "arg_block",
                f"{tool_name}: missing {missing}",
                tool=tool_name,
                missing=missing,
                dry_run=dry_run,
            )
            msg = (
                f"[SHERPA] `{tool_name}` blocked: missing required arg(s): "
                f"{', '.join(missing)}. Re-emit the call with the required fields."
            )
            if schema_preview:
                msg += f"\nSchema: {schema_preview}"
            if dry_run:
                _queue_nudge(session_id, "dry_run", msg.replace("[SHERPA]", "[SHERPA dry-run] Would block"))
                return None
            return {"action": "block", "message": msg}

    # 4) Smart read-range damper
    if _feature("read_damper") and tool_name == "read_file":
        path = (effective_args.get("path") or "").strip()
        if path:
            key_path = _canonical_read_key_path(path)
            try:
                offset = int(effective_args.get("offset", 1))
                limit = int(effective_args.get("limit", 500))
            except Exception:
                offset, limit = 1, 500

            new_range = (offset, offset + limit - 1)
            sid = session_id or "default"
            blocked = False
            with _session_lock:
                _last_access[sid] = time.time()
                history = _read_history.setdefault(sid, {}).setdefault(key_path, [])
                # Check if new_range is a subset of any existing range
                for start, end in history:
                    if offset >= start and (offset + limit - 1) <= end:
                        blocked = True
                        break
                if not blocked:
                    history.append(new_range)

            if blocked:
                _bump_tool_stat(tool_name, "dry_runs" if dry_run else "read_blocks")
                _log_correction("read_block", f"{path} range={new_range} (subset)")
                msg = (
                    f"[SHERPA] You've already read this part of `{path}` "
                    f"(range {new_range[0]}-{new_range[1]}) this turn. "
                    "The content is in your context above — re-read your "
                    "own previous output instead of re-fetching."
                )
                _record_event(
                    session_id,
                    "read_block",
                    f"{path} range={new_range} (subset)",
                    tool=tool_name,
                    path=path,
                    dry_run=dry_run,
                )
                if dry_run:
                    _queue_nudge(session_id, "dry_run", msg.replace("[SHERPA]", "[SHERPA dry-run] Would block"))
                    return None
                return {"action": "block", "message": msg}
    return None


def _post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int = 0,
    **_: Any,
) -> None:
    """Track loops + errors; queue next-turn nudges."""
    state = _load_state()
    if not state.get("enabled"):
        return
    if tool_name in _ALIAS_TOOL_NAMES and _is_sherpa_alias_registered(tool_name):
        return

    args_dict = args if isinstance(args, dict) else {}
    looped = _record_call(session_id, tool_name, args_dict)
    if looped:
        _bump_tool_stat(tool_name, "loops")
        _log_correction("loop", f"{tool_name} repeated {_LOOP_REPEATS}x")
        _record_event(session_id, "loop", f"{tool_name} repeated {_LOOP_REPEATS}x", tool=tool_name)
        _queue_nudge(
            session_id,
            "loop",
            f"[SHERPA] You called `{tool_name}` with the same args "
            f"{_LOOP_REPEATS} times in a row — that's a loop. STOP and "
            "change strategy: try different args, a different tool, or "
            "summarise what you know so far and answer.",
        )

    # Bump per-session call counter for re-anchor cadence
    sid = session_id or "default"
    with _session_lock:
        _calls_since_reanchor[sid] = _calls_since_reanchor.get(sid, 0) + 1

    result_text = _result_to_text(result)
    if _looks_like_error(result, tool_name):
        with _session_lock:
            _error_streak[sid] = _error_streak.get(sid, 0) + 1
            streak = _error_streak[sid]

        # Did-you-mean: file-tools ENOENT → propose closest sibling
        if _feature("didyoumean") and tool_name in {"read_file", "patch", "write_file"}:
            if re.search(r"no such file|file not found|enoent", result_text, re.I):
                path = (args_dict.get("path") or "").strip()
                guess = _didyoumean_path(path) if path else None
                if guess and guess != path:
                    _bump_tool_stat(tool_name, "didyoumean")
                    _record_event(
                        session_id,
                        "didyoumean",
                        f"{path} → {guess}",
                        tool=tool_name,
                        path=path,
                        suggestion=guess,
                    )
                    _queue_nudge(
                        session_id,
                        "didyoumean",
                        f"[SHERPA] `{path}` doesn't exist — did you mean `{guess}`?",
                    )

        # Did-you-mean: unknown tool → propose closest registered name
        if _feature("didyoumean") and re.search(
            r"no such tool|unknown tool|tool .* not (?:found|available)",
            result_text,
            re.I,
        ):
            guess = _didyoumean_tool(tool_name)
            if guess:
                _bump_tool_stat(tool_name, "tool_dym")
                _record_event(
                    session_id,
                    "tool_dym",
                    f"{tool_name} → {guess}",
                    tool=tool_name,
                    suggestion=guess,
                )
                _queue_nudge(
                    session_id,
                    "didyoumean",
                    f"[SHERPA] `{tool_name}` isn't a registered tool — did you mean `{guess}`?",
                )

        if streak >= 2:
            hint = _match_error_hint(result_text)
            if hint:
                _bump_tool_stat(tool_name, "hints")
                _record_event(session_id, "hint", hint, tool=tool_name, streak=streak)
                _queue_nudge(session_id, "hint", f"[SHERPA] {hint}")
    else:
        with _session_lock:
            _error_streak[sid] = 0


def _transform_tool_result(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int = 0,
    **_: Any,
) -> Optional[str]:
    """Append a Tip: footer to error results so the model sees it in-turn.

    Only fires when the framework already has a string result (the
    transform_tool_result contract requires us to return a string).
    For non-string results we still seed a next-turn nudge via the
    post_tool_call path, which uses _result_to_text() and catches
    structured dict errors too.
    """
    state = _load_state()
    if not state.get("enabled"):
        return None
    if tool_name in _ALIAS_TOOL_NAMES and _is_sherpa_alias_registered(tool_name):
        return None
    if not isinstance(result, str) or not result:
        return None
    if not _looks_like_error(result, tool_name):
        return None
    hint = _match_error_hint(result)
    if not hint:
        return None
    # Avoid double-appending if a previous plugin already added the tip.
    if "[SHERPA]" in result:
        return None
    return result + f"\n\n[SHERPA] {hint}"


def _pre_llm_call(
    session_id: str = "",
    user_message: str = "",
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    is_first_turn: bool = False,
    model: str = "",
    platform: str = "",
    sender_id: str = "",
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Inject per-turn nudges into the user message."""
    _flush_stats()
    state = _load_state()
    if not state.get("enabled"):
        return None

    sid = session_id or "default"
    with _session_lock:
        _last_access[sid] = time.time()

    # Cache the first user message for re-anchor injection later. Keep
    # most of the prompt (was 600 chars — too short for non-trivial goals)
    # so the re-anchor nudge can actually re-state what the user asked for.
    if is_first_turn and user_message:
        with _session_lock:
            _first_user_msg[sid] = (user_message[:_FIRST_USER_MSG_CAP]).strip()

    # Reset per-turn read history at the start of every turn.
    with _session_lock:
        _read_history.pop(sid, None)
        _turn_count[sid] = _turn_count.get(sid, 0) + 1
        turns = _turn_count[sid]

    parts: List[str] = []

    # First-turn cheatsheet + periodic refresh every N user turns so
    # long-running sessions don't lose the tool-name reminders entirely.
    # Injected via the user-message context, so prompt cache is preserved.
    show_cheatsheet = is_first_turn or (turns > 1 and turns % _CHEATSHEET_EVERY_TURNS == 0)
    if show_cheatsheet:
        _bump_stat("cheatsheets")
        parts.append(CHEATSHEET)

    # Plan-first nudge — fires on first turn for multi-step requests.
    # Only nudge when `todo` is actually registered; pointing the model at
    # a missing tool is worse than no nudge at all.
    if is_first_turn and _feature("plan_first") and _has_tool("todo"):
        if _is_multistep_request(user_message):
            _bump_stat("plan_nudges")
            parts.append(
                "[SHERPA] This looks multi-step. Call `todo` first to lay out "
                "the steps with `todo(todos=[{id, content, status}])`, then "
                "work them one at a time. Mark each step `completed` before "
                "moving on."
            )

    # Queued nudges from post_tool_call.
    parts.extend(_drain_nudges(session_id))

    # Re-anchor: every N tool calls (and on loop), re-inject the original goal
    # so models don't drift.
    if _feature("reanchor") and not is_first_turn:
        with _session_lock:
            original = _first_user_msg.get(sid)
            count = _calls_since_reanchor.get(sid, 0)
            should_reanchor = count >= _REANCHOR_EVERY
            if should_reanchor:
                _calls_since_reanchor[sid] = 0
        if should_reanchor and original:
            _bump_stat("reanchors")
            parts.append(
                f"[SHERPA] Re-anchor — your ORIGINAL GOAL was:\n"
                f"  > {original}\n"
                "Are you still working toward this? If you have the answer, "
                "stop calling tools and write it."
            )

    # Late-turn stop reminder.
    with _session_lock:
        should_stop_nudge = _call_count.get(sid, 0) >= _STOP_NUDGE_AT_CALLS
        if should_stop_nudge:
            _call_count[sid] = 0  # reset so we don't re-fire every turn
    if should_stop_nudge:
        parts.append(
            "[SHERPA] You've called many tools this turn. If you already "
            "have what you need, stop calling tools and write the answer."
        )

    if not parts:
        return None

    combined = "\n\n".join(parts)
    # Hard cap on total injected text to avoid drowning out actual tool outputs
    if len(combined) > _TOTAL_NUDGE_CAP:
        combined = combined[:_TOTAL_NUDGE_CAP] + "\n... [TRUNCATED BY SHERPA]"

    return {"context": combined}


_PER_SESSION_DICTS = (
    _call_history,
    _pending_nudges,
    _session_events,
    _nudge_throttle,
    _call_count,
    _error_streak,
    _first_user_msg,
    _read_history,
    _calls_since_reanchor,
    _turn_count,
    _last_notified_loop,
    _last_access,
)


def _cleanup_stale_sessions() -> None:
    """Prune session data for sessions inactive for > _SESSION_TTL."""
    now = time.time()
    stale = []
    with _session_lock:
        for sid, last_ts in _last_access.items():
            if (now - last_ts) > _SESSION_TTL:
                stale.append(sid)
    for sid in stale:
        _clear_session(sid)

    global _cleanup_timer
    with _timer_lock:
        with _session_lock:
            sessions_left = _all_session_ids()
        if sessions_left:
            _cleanup_timer = threading.Timer(_CLEANUP_INTERVAL, _cleanup_stale_sessions)
            _cleanup_timer.daemon = True
            _cleanup_timer.start()
        else:
            _cleanup_timer = None


def _ensure_cleanup_task() -> None:
    with _timer_lock:
        if _cleanup_timer is None:
            _cleanup_stale_sessions()


def _clear_session(sid: str) -> None:
    """Drop all per-session state for *sid*. Holds _session_lock so it can't
    race with hook handlers iterating the same dicts."""
    with _session_lock:
        for d in _PER_SESSION_DICTS:
            d.pop(sid, None)


def _all_session_ids() -> List[str]:
    """Snapshot every session id currently tracked across the per-session dicts.

    Held under _session_lock to avoid iterating while another hook mutates.
    """
    with _session_lock:
        seen: set = set()
        for d in _PER_SESSION_DICTS:
            seen.update(d.keys())
        return list(seen)


def _clear_all_sessions() -> None:
    """Wipe per-session state for every known session — used by /sherpa reset.

    Iterates the union of session ids and delegates to _clear_session(sid)
    for each, so the reset and per-session cleanup paths stay in lockstep
    (no risk of one helper learning about a new dict while the other
    forgets it).
    """
    for sid in _all_session_ids():
        _clear_session(sid)


def _on_session_start(session_id: str = "", **_: Any) -> None:
    sid = session_id or "default"
    _clear_session(sid)
    with _session_lock:
        _last_access[sid] = time.time()
    _ensure_cleanup_task()


def _on_session_end(session_id: str = "", **_: Any) -> None:
    sid = session_id or "default"
    _flush_stats()
    _clear_session(sid)
    # Only cancel the background flush timers if there are no other active sessions!
    with _session_lock:
        sessions_left = _all_session_ids()
    if not sessions_left:
        # Bug #4 fix: _cleanup_timer must be in the global declaration. Without
        # it, the assignment on the next line makes Python treat the function's
        # _cleanup_timer as a local, and the prior read raises UnboundLocalError.
        global _flush_timer, _periodic_flush_timer, _cleanup_timer
        with _timer_lock:
            if _flush_timer is not None:
                _flush_timer.cancel()
                _flush_timer = None
            if _periodic_flush_timer is not None:
                _periodic_flush_timer.cancel()
                _periodic_flush_timer = None
            if _cleanup_timer is not None:
                _cleanup_timer.cancel()
                _cleanup_timer = None


# ---------------------------------------------------------------------------
# Hallucinated-tool aliases (bash/cat/grep/find/ls/head/tail/...)
# ---------------------------------------------------------------------------


def _alias_handler(
    real_tool: str,
    arg_map: Optional[Dict[str, str]] = None,
    fixed_args: Optional[Dict[str, Any]] = None,
    build_command: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> Callable:
    """Build a handler that silently re-dispatches to *real_tool*.

    Dispatch goes through ``handle_function_call`` so the real tool sees
    every other plugin's pre/post hooks (including sherpa's own arg-guard,
    silent-rewrite, read-damper, didyoumean, and stats). Falling back to
    ``registry.dispatch`` only when ``handle_function_call`` is unavailable
    (e.g. unit tests with a stub registry).
    """

    def handler(args, **kw):
        if not _feature("aliases") or not _feature("alias_tools"):
            return json.dumps({"error": "model-sherpa aliases are disabled"})
        if build_command is not None:
            real_args = {"command": build_command(args or {})}
        else:
            real_args = dict(fixed_args or {})
            for src, dst in (arg_map or {}).items():
                if src in (args or {}):
                    real_args[dst] = args[src]
            # passthrough any remaining args
            for k, v in (args or {}).items():
                if k not in (arg_map or {}):
                    real_args.setdefault(k, v)
        _bump_tool_stat(real_tool, "aliases_used")
        _log_correction("alias", f"alias→{real_tool} args={list(real_args.keys())}")
        # Import the dispatcher first; only fall back when it is *unavailable*
        # (e.g. unit tests with a stub registry). If the real dispatch itself
        # raises after partial side-effects, retrying via reg.dispatch would
        # double-execute terminal/write tools — so dispatch errors must
        # propagate, not trigger the fallback.
        try:
            from model_tools import handle_function_call
        except ImportError as exc:
            logger.debug(
                "model-sherpa: handle_function_call unavailable for alias %s (%s); falling back to registry.dispatch",
                real_tool,
                exc,
            )
            reg = _registry()
            if reg is None:
                return json.dumps({"error": f"alias dispatch unavailable: {exc}"})
            dispatch = getattr(reg, "dispatch", None)
            if dispatch is None:
                logger.warning(
                    "model-sherpa: registry has no 'dispatch' attribute (%s); cannot fall back for alias %s",
                    reg,
                    real_tool,
                )
                return json.dumps({"error": "alias dispatch unavailable: registry has no dispatch method"})
            return dispatch(
                real_tool,
                real_args,
                task_id=kw.get("task_id"),
                user_task=kw.get("user_task"),
            )
        return handle_function_call(
            real_tool,
            real_args,
            task_id=kw.get("task_id"),
            tool_call_id=kw.get("tool_call_id"),
            session_id=kw.get("session_id"),
            user_task=kw.get("user_task"),
        )

    return handler


def _build_head_command(args: Dict[str, Any]) -> str:
    path = str(args.get("path") or args.get("file") or args.get("filename") or "")
    n = args.get("n") or args.get("lines") or 10
    try:
        n = int(n)
    except Exception:
        n = 10
    return f"head -n {n} {shlex.quote(path)}" if path else "head -n 0"


def _build_tail_command(args: Dict[str, Any]) -> str:
    path = str(args.get("path") or args.get("file") or args.get("filename") or "")
    n = args.get("n") or args.get("lines") or 10
    try:
        n = int(n)
    except Exception:
        n = 10
    # Do not honor tail -f here: alias tools should not create foreground
    # commands that can run forever without an explicit terminal timeout.
    flags = "-n " + str(n)
    return f"tail {flags} {shlex.quote(path)}" if path else "tail -n 0"


def _build_ls_command(args: Dict[str, Any]) -> str:
    path = str(args.get("path") or args.get("dir") or args.get("directory") or ".")
    long_form = bool(args.get("long"))
    return f"ls {'-la' if long_form else '-a'} {shlex.quote(path)}"


_TERMINAL_ALIAS_SCHEMA_PARAMS: Dict[str, Dict[str, Any]] = {
    "command": {"type": "string", "description": "Shell command"},
    "timeout": {"type": "integer", "description": "Max seconds to wait"},
    "workdir": {"type": "string", "description": "Working directory"},
    "background": {"type": "boolean", "description": "Run as tracked background process"},
    "notify_on_complete": {
        "type": "boolean",
        "description": "Notify once when a background command exits",
    },
}


_ALIAS_SPECS: List[Dict[str, Any]] = [
    # bash / shell family → terminal
    {
        "name": "bash",
        "real": "terminal",
        "toolset": "terminal",
        "desc": "Alias of `terminal`. Runs a shell command.",
        "schema_params": _TERMINAL_ALIAS_SCHEMA_PARAMS,
        "required": ["command"],
    },
    {
        "name": "shell",
        "real": "terminal",
        "toolset": "terminal",
        "desc": "Alias of `terminal`. Runs a shell command.",
        "schema_params": _TERMINAL_ALIAS_SCHEMA_PARAMS,
        "required": ["command"],
    },
    {
        "name": "sh",
        "real": "terminal",
        "toolset": "terminal",
        "desc": "Alias of `terminal`. Runs a shell command.",
        "schema_params": _TERMINAL_ALIAS_SCHEMA_PARAMS,
        "required": ["command"],
    },
    {
        "name": "exec",
        "real": "terminal",
        "toolset": "terminal",
        "desc": "Alias of `terminal`. Runs a shell command.",
        "schema_params": _TERMINAL_ALIAS_SCHEMA_PARAMS,
        "required": ["command"],
    },
    # cat → read_file
    {
        "name": "cat",
        "real": "read_file",
        "toolset": "file",
        "desc": "Alias of `read_file`. Reads a text file.",
        "schema_params": {"path": {"type": "string", "description": "File path"}},
        "arg_map": {"path": "path", "file": "path", "filename": "path"},
        "required": ["path"],
    },
    # head / tail → terminal (literal command).
    # `path` is required; `n` defaults to 10 in _build_head_command/_build_tail_command.
    {
        "name": "head",
        "real": "terminal",
        "toolset": "terminal",
        "desc": "Alias that runs `head -n N PATH` via terminal.",
        "schema_params": {
            "path": {"type": "string", "description": "File path"},
            "n": {"type": "integer", "description": "Number of lines (default 10)"},
        },
        "build_command": _build_head_command,
        "required": ["path"],
    },
    {
        "name": "tail",
        "real": "terminal",
        "toolset": "terminal",
        "desc": "Alias that runs `tail -n N PATH` via terminal.",
        "schema_params": {
            "path": {"type": "string", "description": "File path"},
            "n": {"type": "integer", "description": "Number of lines (default 10)"},
        },
        "build_command": _build_tail_command,
        "required": ["path"],
    },
    # grep / rg / egrep → search_files (content)
    {
        "name": "grep",
        "real": "search_files",
        "toolset": "file",
        "desc": "Alias of `search_files`. Searches file contents.",
        "schema_params": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Directory or file to search"},
        },
        "arg_map": {"pattern": "pattern", "path": "path", "regex": "pattern", "query": "pattern", "directory": "path"},
        "fixed": {"target": "content"},
        "required": ["pattern"],
    },
    {
        "name": "rg",
        "real": "search_files",
        "toolset": "file",
        "desc": "Alias of `search_files`. Ripgrep-style content search.",
        "schema_params": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Directory or file to search"},
        },
        "arg_map": {"pattern": "pattern", "path": "path", "regex": "pattern", "query": "pattern", "directory": "path"},
        "fixed": {"target": "content"},
        "required": ["pattern"],
    },
    {
        "name": "egrep",
        "real": "search_files",
        "toolset": "file",
        "desc": "Alias of `search_files`. Extended regex content search.",
        "schema_params": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Directory or file to search"},
        },
        "arg_map": {"pattern": "pattern", "path": "path"},
        "fixed": {"target": "content"},
        "required": ["pattern"],
    },
    # find → search_files (files)
    {
        "name": "find",
        "real": "search_files",
        "toolset": "file",
        "desc": "Alias of `search_files target='files'`. Finds files by name.",
        "schema_params": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. *.py)"},
            "path": {"type": "string", "description": "Directory to search"},
        },
        "arg_map": {"pattern": "pattern", "path": "path", "name": "pattern", "glob": "pattern"},
        "fixed": {"target": "files"},
        "required": ["pattern"],
    },
    # ls → terminal (literal command, since ls flags vary).
    # `path` is intentionally NOT required: _build_ls_command defaults to ".".
    {
        "name": "ls",
        "real": "terminal",
        "toolset": "terminal",
        "desc": "Alias that runs `ls -la PATH` via terminal.",
        "schema_params": {
            "path": {"type": "string", "description": "Directory to list (default .)"},
            "long": {"type": "boolean", "description": "Pass -la"},
        },
        "build_command": _build_ls_command,
        "required": [],
    },
    # skill → skill_view
    {
        "name": "skill",
        "real": "skill_view",
        "toolset": "skills",
        "desc": "Alias of `skill_view`. Loads a saved skill.",
        "schema_params": {
            "name": {"type": "string", "description": "Skill name"},
            "file_path": {"type": "string", "description": "Optional file within the skill"},
        },
        "arg_map": {
            "name": "name",
            "skill": "name",
            "skill_name": "name",
            "file_path": "file_path",
            "path": "file_path",
        },
        "required": ["name"],
    },
]

_SOFT_ALIAS_TARGETS = {spec["name"]: spec["real"] for spec in _ALIAS_SPECS}
_ALIAS_TOOL_NAMES = set(_SOFT_ALIAS_TARGETS)


def _register_aliases(ctx) -> int:
    """Register hallucinated-tool aliases. Skips any that already exist."""
    if not _feature("alias_tools"):
        logger.info("model-sherpa: hard alias tools disabled; using soft aliases")
        return 0
    existing = set(_registered_tool_names())
    if not existing and _registry() is None:
        logger.warning("model-sherpa: cannot access tools.registry; aliases skipped")
        return 0
    registered = 0
    for spec in _ALIAS_SPECS:
        name = spec["name"]
        if name in existing:
            logger.debug("model-sherpa: %s already registered, skipping alias", name)
            continue
        # Per-spec required list. Falls back to the first declared parameter
        # only when a spec doesn't declare one — but every spec in
        # _ALIAS_SPECS now sets `required` explicitly, including `ls`
        # which uses [] because _build_ls_command defaults `path` to ".".
        required = spec.get("required")
        if required is None:
            required = list(spec["schema_params"].keys())[:1]
        schema = {
            "name": name,
            "description": "[sherpa alias] " + spec["desc"] + " Silently dispatches to `" + spec["real"] + "`.",
            "parameters": {
                "type": "object",
                "properties": spec["schema_params"],
                "required": list(required),
            },
        }
        handler = _alias_handler(
            real_tool=spec["real"],
            arg_map=spec.get("arg_map"),
            fixed_args=spec.get("fixed"),
            build_command=spec.get("build_command"),
        )
        try:
            ctx.register_tool(
                name=name,
                toolset=spec["toolset"],
                schema=schema,
                handler=handler,
                emoji="🌀",
            )
            registered += 1
            _registered_alias_tool_names.add(name)
        except Exception as exc:
            logger.warning("model-sherpa: failed to register alias %s: %s", name, exc)
    return registered


def _is_sherpa_alias_registered(name: str) -> bool:
    entry = _registry_tools().get(name)
    schema = getattr(entry, "schema", None) if entry else None
    desc = schema.get("description", "") if isinstance(schema, dict) else ""
    return isinstance(desc, str) and desc.startswith("[sherpa alias]")


def _unregister_aliases() -> int:
    reg = _registry()
    if reg is None:
        return 0
    removed = 0
    for spec in _ALIAS_SPECS:
        name = spec["name"]
        if name not in _registered_alias_tool_names and not _is_sherpa_alias_registered(name):
            continue
        try:
            deregister = getattr(reg, "deregister", None)
            if callable(deregister):
                deregister(name)
                removed += 1
                _registered_alias_tool_names.discard(name)
        except Exception as exc:
            logger.warning("model-sherpa: failed to unregister alias %s: %s", name, exc)
    if removed:
        _invalidate_tool_cache()
    return removed


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

_HELP = textwrap.dedent("""\
    /sherpa — guide-rails for all models

    Subcommands:
      status                    Show feature toggles and stats
      on                        Enable the whole plugin
      off                       Disable everything (no rewrites, no nudges)
      feature <name> <on|off>   Toggle one feature (see `status` for names)
      cheatsheet                Print the first-turn cheatsheet
      add <regex> <hint>        Add a custom error-pattern → hint
      rules                     List arg-repair + error-hint rules
      aliases                   List the hallucinated-tool aliases registered
      telemetry [N|session N]   Show recent per-session Sherpa events
      doctor                    Diagnose registry, aliases, state, and logs
      reset                     Clear runtime counters
      log [N]                   Tail the corrections log (default 20)
""")


def _doctor_report() -> str:
    """Return a compact operational health report."""
    _flush_stats()
    state = _load_state()
    hard_aliases_enabled = bool((state.get("features") or {}).get("alias_tools", False))
    names = set(_registered_tool_names())
    reg = _registry()
    alias_registered = [a["name"] for a in _ALIAS_SPECS if a["name"] in names]
    alias_missing = [a["name"] for a in _ALIAS_SPECS if a["name"] not in names]
    required_tools = [
        "terminal",
        "read_file",
        "search_files",
        "write_file",
        "patch",
        "todo",
        "memory",
        "skill_view",
    ]
    missing_tools = [name for name in required_tools if name not in names]
    schema_issues: List[str] = []

    expected_schema_keys = {
        "todo": {"todos", "merge"},
        "memory": {"action", "target", "content", "old_text"},
        "skill_view": {"name", "file_path"},
        "read_file": {"path", "offset", "limit"},
        "search_files": {"pattern", "target", "path"},
        "patch": {"mode", "path", "old_string", "new_string", "patch"},
    }
    for tool, expected in expected_schema_keys.items():
        if tool not in names:
            continue
        props = _schema_property_names(tool)
        if not props and tool in names:
            schema_issues.append(f"{tool}: registered but schema unavailable")
            continue
        missing = sorted(expected - props)
        if missing:
            schema_issues.append(f"{tool}: missing schema keys {missing}")

    pending_stats_count = 0
    with _stats_lock:
        pending_stats_count = sum(_pending_stats.values())
    session_count = len(_all_session_ids())
    log_lines = 0
    last_log = "(none)"
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text().splitlines()
            log_lines = len(lines)
            if lines:
                last_log = lines[-1]
        except Exception as exc:
            last_log = f"(could not read: {exc})"

    raw_state_keys: List[str] = []
    try:
        if STATE_FILE.exists():
            raw = json.loads(STATE_FILE.read_text())
            if isinstance(raw, dict):
                raw_state_keys = sorted(raw.keys())
    except Exception:
        raw_state_keys = ["(unreadable)"]
    deprecated = [k for k in raw_state_keys if k == "profile"]

    lines = [
        "**model-sherpa doctor**",
        f"  enabled: {state.get('enabled')}",
        f"  state: {STATE_FILE}",
        f"  registry: {'available' if reg is not None else 'unavailable'}",
        f"  registry generation: {_tool_registry_generation()}",
        f"  registered tools visible: {len(names)}",
        f"  aliases registered: {len(alias_registered)}/{len(_ALIAS_SPECS)}",
        f"  pending stat increments: {pending_stats_count}",
        f"  tracked sessions: {session_count}",
        f"  correction log lines: {log_lines}",
        f"  last correction: {last_log}",
    ]
    if missing_tools:
        lines.append("  missing core tools: " + ", ".join(missing_tools))
    if alias_missing and hard_aliases_enabled:
        lines.append("  aliases not registered: " + ", ".join(alias_missing))
    elif alias_missing:
        lines.append("  hard aliases: off (soft alias recovery active)")
    if schema_issues:
        lines.append("  schema issues: " + "; ".join(schema_issues))
    if deprecated:
        lines.append("  deprecated state keys present: " + ", ".join(deprecated))
    if not any([missing_tools, schema_issues, deprecated]) and reg is not None:
        lines.append("  verdict: OK")
    return "\n".join(lines)


def _handle_slash(raw: str) -> str:
    try:
        argv = shlex.split(raw)
    except Exception:
        argv = raw.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _help_with_status()
    sub = argv[0].lower()

    if sub == "status":
        return _help_with_status()

    if sub == "on":
        _update_state(lambda st: st.update({"enabled": True}))
        return "model-sherpa: ENABLED"

    if sub == "off":
        _update_state(lambda st: st.update({"enabled": False}))
        return "model-sherpa: DISABLED (no rewrites, no nudges, no hints)"

    if sub == "feature":
        if len(argv) < 3:
            s = _load_state()
            feats = s.get("features") or {}
            return (
                "Features:\n"
                + "\n".join(f"  {k}: {'ON' if v else 'off'}" for k, v in feats.items())
                + "\n\nUsage: /sherpa feature <name> <on|off>"
            )
        name = argv[1]
        val = argv[2].lower()
        if val not in {"on", "off", "true", "false", "1", "0"}:
            return f"Invalid value '{val}'. Use on|off."

        alias_tools_on = val in {"on", "true", "1"}
        s = _load_state()
        if name not in (s.get("features") or {}):
            return f"Unknown feature '{name}'. Available: " + ", ".join(s.get("features", {}).keys())

        def update_feat(st):
            st["features"][name] = alias_tools_on

        _update_state(update_feat)

        if name == "alias_tools":
            if alias_tools_on:
                if _plugin_ctx is None:
                    return "model-sherpa: feature alias_tools → on (restart required to register hard aliases)"
                n = _register_aliases(_plugin_ctx)
                if n:
                    _invalidate_tool_cache()
                return f"model-sherpa: feature alias_tools → on ({n} hard aliases registered)"
            removed = _unregister_aliases()
            return f"model-sherpa: feature alias_tools → off ({removed} hard aliases unregistered)"
        return f"model-sherpa: feature {name} → {'on' if alias_tools_on else 'off'}"

    if sub == "aliases":
        names = set(_registered_tool_names())
        if not names and _registry() is None:
            return "Could not inspect registry."
        registered = [a["name"] for a in _ALIAS_SPECS if a["name"] in names]
        not_registered = [a["name"] for a in _ALIAS_SPECS if a["name"] not in registered]
        hard_enabled = _feature("alias_tools")
        soft_enabled = _feature("aliases")
        lines = [
            f"Soft aliases: {'ON' if soft_enabled else 'off'}",
            f"Hard visible alias tools: {'ON' if hard_enabled else 'off'}",
            f"Hard aliases registered ({len(registered)}/{len(_ALIAS_SPECS)}):",
        ]
        for spec in _ALIAS_SPECS:
            marker = "✓" if spec["name"] in registered else "✗"
            lines.append(f"  {marker} {spec['name']:8} → {spec['real']}")
        if not_registered:
            if hard_enabled:
                lines.append("\n(✗ = hard alias unavailable: name collision, registry error, or registration failure)")
            else:
                lines.append("\nHard aliases are intentionally not registered unless `alias_tools` is ON.")
        return "\n".join(lines)

    if sub == "doctor":
        return _doctor_report()

    if sub == "telemetry":
        n = 30
        session = None
        if len(argv) > 1:
            if argv[1].isdigit():
                n = max(1, min(int(argv[1]), 200))
            else:
                session = argv[1]
        if len(argv) > 2 and argv[2].isdigit():
            n = max(1, min(int(argv[2]), 200))
        return _format_events(session, n)

    if sub == "cheatsheet":
        return "Cheatsheet injected on turn 1 for all models:\n\n" + CHEATSHEET

    if sub == "add":
        if len(argv) < 3:
            return "Usage: /sherpa add <regex> <hint text>"
        pat = argv[1]
        hint = " ".join(argv[2:])
        # Validate with the same flags _match_error_hint uses at runtime so a
        # pattern that compiles here will compile there too — no ticking
        # time bomb gets persisted into state.json.
        try:
            re.compile(pat, re.I)
        except re.error as e:
            return f"[SHERPA] Invalid regular expression pattern: {e}"

        def update_hints(st):
            existing = [h for h in st["custom_hints"] if h["pattern"] == pat]
            if existing:
                existing[0]["hint"] = hint
            else:
                st["custom_hints"].append({"pattern": pat, "hint": hint})

        s = _update_state(update_hints)
        return f"Added custom hint for /{pat}/ ({len(s['custom_hints'])} total)."

    if sub == "rules":
        lines = ["**Arg-repair rules** (silent rewrites):"]
        for tool, aliases in _ARG_ALIASES.items():
            lines.append(f"  {tool}: " + ", ".join(f"{w}→{r}" for w, r in aliases.items()))
        lines.append("\n**Built-in error-hint patterns:**")
        for err_pat, err_hint in _ERROR_HINTS:
            lines.append(f"  /{err_pat.pattern}/ → {err_hint[:80]}…")
        state = _load_state()
        if state.get("custom_hints"):
            lines.append("\n**Custom hints:**")
            for entry in state["custom_hints"]:
                lines.append(f"  /{entry['pattern']}/ → {entry['hint']}")
        return "\n".join(lines)

    if sub == "reset":
        _update_state(lambda st: st.update({"stats": dict(_DEFAULT_STATE["stats"])}))
        # Also drop in-memory pending stats so flush doesn't immediately
        # re-populate the persisted file with stale increments.
        with _stats_lock:
            _pending_stats.clear()
        _clear_all_sessions()
        return "model-sherpa: counters cleared."

    if sub == "log":
        n = 20
        if len(argv) > 1 and argv[1].isdigit():
            n = max(1, min(int(argv[1]), 500))
        if not LOG_FILE.exists():
            return "(no corrections logged yet)"
        try:
            lines = LOG_FILE.read_text().splitlines()[-n:]
        except Exception as e:
            return f"(could not read log: {e})"
        return f"Last {len(lines)} correction(s):\n" + "\n".join(lines)

    return f"Unknown subcommand: {sub}\n\n{_HELP}"


def _help_with_status() -> str:
    _flush_stats()
    state = _load_state()
    stats = state.get("stats") or {}
    feats = state.get("features") or {}
    feat_lines = "\n".join(f"            {k:18} {'ON' if v else 'off'}" for k, v in feats.items())
    # Build per-tool histogram.
    per_tool = stats.get("per_tool", {}) or {}
    tool_lines = ""
    if per_tool:
        _STAT_LABELS = {
            "rewrites": "rw",
            "arg_blocks": "arg",
            "read_blocks": "rd",
            "loops": "lp",
            "hints": "hint",
            "didyoumean": "dym",
            "aliases_used": "alias",
            "cmd_lints": "lint",
            "tool_dym": "tdym",
            "dry_runs": "dry",
        }
        # Sort by total corrections descending.
        ranked = sorted(
            per_tool.items(),
            key=lambda kv: sum(kv[1].values()),
            reverse=True,
        )
        tool_lines = "\n        **Per-tool corrections** (rw=rewrites arg=arg-block rd=read-block lp=loop hint=error-hint dym=did-you-mean alias=alias-dispatch lint=cmd-lint tdym=tool-dym):\n"
        for tool, buckets in ranked:
            total = sum(buckets.values())
            bits = []
            for key, abbr in _STAT_LABELS.items():
                v = buckets.get(key, 0)
                if v:
                    bits.append(f"{abbr}:{v}")
            detail = "  ".join(bits) if bits else "—"
            tool_lines += f"          {tool:16} {detail} → {total}\n"
    return textwrap.dedent(f"""\
        **model-sherpa** — guide-rails for all models

          enabled : {state.get("enabled")}
          custom-hints : {len(state.get("custom_hints", []))}

          **Features** (toggle with /sherpa feature <name> <on|off>):
{feat_lines}

        **Lifetime stats** (persisted):
          silent arg rewrites : {stats.get("rewrites", 0)}
          error hints fired   : {stats.get("hints", 0)}
          loops detected      : {stats.get("loops", 0)}
          cheatsheets shown   : {stats.get("cheatsheets", 0)}
          alias dispatches    : {stats.get("aliases_used", 0)}
          did-you-mean        : {stats.get("didyoumean", 0)}
          goal re-anchors     : {stats.get("reanchors", 0)}
          plan-first nudges   : {stats.get("plan_nudges", 0)}
          arg-guard blocks    : {stats.get("arg_blocks", 0)}
          read-damper blocks  : {stats.get("read_blocks", 0)}
          dry-run advisories  : {stats.get("dry_runs", 0)}
          nudges suppressed   : {stats.get("nudges_suppressed", 0)}
{tool_lines}
        {_HELP}""")


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    global _plugin_ctx
    _plugin_ctx = ctx
    # Ensure state dir exists at first load.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_state()

    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)

    ctx.register_command(
        "sherpa",
        handler=_handle_slash,
        description="Guide-rails for all models (status/feature/aliases/log).",
        args_hint="[status|on|off|feature|cheatsheet|aliases|telemetry|doctor|add|rules|reset|log]",
    )

    # Hard alias tools are opt-in because registered tools are visible in
    # model schemas. Soft aliases still provide recovery hints without adding
    # bash/grep/find/etc. to the tool surface.
    n_aliases = _register_aliases(ctx)
    if n_aliases:
        _invalidate_tool_cache()
    # Session cleanup safety net
    _ensure_cleanup_task()

    logger.info("model-sherpa loaded (state=%s, %d aliases registered)", STATE_FILE, n_aliases)
