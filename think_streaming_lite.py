"""
Ultra-light pass-through SSE proxy.
Accepts requests from opencode, adds auth + chat_template_kwargs,
and forwards raw SSE bytes from the corporate API without parsing.
"""

# version 261027c by ssh (rolling token-quota counter 15M/4h: colored [QUOTA]
# lines in console + log, history restored from the log on restart, ANSI kept
# out of the log file, GET /quota status endpoint with CORS for the OpenCode
# Desktop quota pill widget, opencode TUI toasts at 80/95/100%;
# budget-aware context truncation: oldest tool results first)

import os
import re
import sys
import json
import base64
import hashlib
import time
import logging
import logging.handlers
import queue
import threading
import traceback
from collections import deque
import requests
import urllib3
from flask import Flask, request, Response, stream_with_context, jsonify
from dotenv import load_dotenv
import security_filter
from security_filter import security_stream_wrapper

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Paths & .env discovery
# ---------------------------------------------------------------------------
# Relative paths are anchored to the script directory (not the process CWD)
# so the proxy behaves the same however it is launched. Override the .env
# location with ENV_FILE / DOTENV_PATH and the log path with LOG_FILE.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_env_file():
    """Locate the .env file: explicit override first, then the usual spots
    next to the script, then one level up (legacy location)."""
    override = os.getenv("ENV_FILE") or os.getenv("DOTENV_PATH")
    candidates = []
    if override:
        candidates.append(override)
        if not os.path.isabs(override):
            candidates.append(os.path.join(BASE_DIR, override))
    candidates.extend([
        os.path.join(BASE_DIR, ".env.creoapi"),
        os.path.join(BASE_DIR, ".env"),
        os.path.join(os.path.dirname(BASE_DIR), ".env.creoapi"),
    ])
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


_ENV_FILE_PATH = _resolve_env_file()
if _ENV_FILE_PATH:
    load_dotenv(_ENV_FILE_PATH)
else:
    load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(
    BASE_DIR, os.getenv("LOG_FILE") or "think_streaming_lite.log"
)
# Cap the log at 10 MB (rotated to .1). Multi-MB logs kept by OneDrive as
# "online only" placeholders broke the open handle (flush -> EINVAL) and
# blocked reads forever; small files sync and hydrate reliably.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 1


class _AsyncFileHandler(logging.Handler):
    """File logging on a daemon writer thread.

    A log file that OneDrive has dehydrated to a placeholder can make
    open()/flush() block forever (seen 02.11: the main thread hung on a
    flush before app.run, port 8081 never bound, proxy served nothing).
    The main thread must never touch the file: records go to a bounded
    queue and a daemon thread writes them through a RotatingFileHandler.
    If the file I/O wedges, only the writer thread wedges; records are
    dropped (the console handler still has every line) and the proxy
    keeps serving. When OneDrive releases the file, writes resume."""

    def __init__(self, path, max_bytes, backup_count):
        super().__init__()
        self._queue = queue.Queue(maxsize=10000)
        self._file = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while True:
            record = self._queue.get()
            try:
                self._file.handle(record)
            except Exception:
                pass

    def setFormatter(self, fmt):
        super().setFormatter(fmt)
        self._file.setFormatter(fmt)

    def emit(self, record):
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            pass


console_handler = logging.StreamHandler(sys.stdout)
file_handler = _AsyncFileHandler(LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT)
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Console color for [QUOTA] lines. Coloring is done by a formatter that is
# installed on the console handler only, so the log file always stays plain
# text (a filter that rewrote record.msg leaked ANSI escapes into the file:
# every handler formats the same record object).
# ---------------------------------------------------------------------------
def _console_ansi_supported():
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    try:
        import colorama
        colorama.just_fix_windows_console()
    except ImportError:
        pass  # Windows 10+ consoles handle ANSI natively
    return True


_CONSOLE_ANSI = _console_ansi_supported()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _QuotaColorFormatter(logging.Formatter):
    QUOTA_COLOR = "\033[1;93m"  # bold bright yellow — stands out in the log stream
    RESET = "\033[0m"

    def format(self, record):
        text = super().format(record)
        if "[QUOTA]" in text:
            text = f"{self.QUOTA_COLOR}{text}{self.RESET}"
        return text


if _CONSOLE_ANSI:
    console_handler.setFormatter(
        _QuotaColorFormatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    )

logger.info("security_filter loaded from: %s", security_filter.__file__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config (from .env or defaults)
# ---------------------------------------------------------------------------
if _ENV_FILE_PATH:
    logger.info("Loaded env: %s", _ENV_FILE_PATH)
else:
    logger.warning(
        "Env file not found (tried ENV_FILE/DOTENV_PATH, .env.creoapi next "
        "to the script or its parent, and .env) — using process env + defaults"
    )

AUTH_URL = os.getenv("AUTH_URL", "https://qa-aicb.pmru.com/api/v1/auths/signin")
AUTH_EMAIL = os.getenv("AUTH_EMAIL", "")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")

CORP_URL = os.getenv("CORP_URL", "https://qa-aicb.pmru.com/api/chat/completions")
CORP_MODEL = os.getenv("CORP_MODEL", "qa-api-model")

# HTTP server bind. Defaults preserve the historical 127.0.0.1:8081.
PROXY_HOST = os.getenv("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8081"))
PROXY_DEBUG = os.getenv("PROXY_DEBUG", "false").strip().lower() in (
    "1", "true", "yes", "on"
)
# TLS verification for upstream calls. Disabled by default (corporate
# self-signed cert); enable with UPSTREAM_VERIFY_SSL=true.
UPSTREAM_VERIFY_SSL = os.getenv("UPSTREAM_VERIFY_SSL", "false").strip().lower() in (
    "1", "true", "yes", "on"
)

# Timeouts: (connect, read). A read timeout is an *inactivity* timeout,
# not a limit on total generation time. Use "none"/"0" to disable it.
UPSTREAM_CONNECT_TIMEOUT = float(os.getenv("UPSTREAM_CONNECT_TIMEOUT", "30"))


def _optional_timeout_from_env(name, default):
    raw = os.getenv(name, default).strip().lower()
    if raw in ("", "0", "none", "off", "false", "disabled"):
        return None
    return float(raw)


UPSTREAM_READ_TIMEOUT = _optional_timeout_from_env("UPSTREAM_READ_TIMEOUT", "3600")

current_token = None
# Zero means lossless pass-through. These limits remain available as explicit
# emergency safeguards, but must not silently shrink a 128K model context.
MAX_TOOL_DESC_LEN = int(os.getenv("MAX_TOOL_DESC_LEN", "0"))
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "0"))
# OpenCode 1.1.x clamps compatible-provider requests to 32K even when the
# configured model output limit is higher. Keep half of the 128K context
# available for input/tool history; zero keeps the client value.
UPSTREAM_MAX_TOKENS = int(os.getenv("UPSTREAM_MAX_TOKENS", "65536"))
# Upstream vLLM context window (tokens). The proactive cap below only lets
# a request through when estimated_prompt + max_tokens fits within it.
UPSTREAM_CONTEXT_TOKENS = int(os.getenv("UPSTREAM_CONTEXT_TOKENS", "131072"))
# Extra margin (tokens) added on top of the prompt estimate before capping.
PROMPT_GROWTH_SAFETY_TOKENS = int(os.getenv("PROMPT_GROWTH_SAFETY_TOKENS", "4096"))
CONTEXT_OVERFLOW_SAFETY_TOKENS = int(
    os.getenv("CONTEXT_OVERFLOW_SAFETY_TOKENS", "1024")
)
MIN_VISIBLE_OUTPUT_TOKENS = int(os.getenv("MIN_VISIBLE_OUTPUT_TOKENS", "2048"))
# Context-pressure hint: when the estimated room for output drops below
# this, the proxy injects a system note telling the model the window is
# nearly full (switch to grep/offset/limit reads, warn the user about
# /compact) before the session dies.
CONTEXT_PRESSURE_HINT_TOKENS = int(
    os.getenv("CONTEXT_PRESSURE_HINT_TOKENS", "16384")
)
# Last-words floor: when the estimated room is below MIN_VISIBLE_OUTPUT_TOKENS
# the proxy sends one small request so the model can ask the user to run
# /compact in a single short line.
LAST_WORDS_MIN_TOKENS = int(os.getenv("LAST_WORDS_MIN_TOKENS", "64"))
# Zero-byte drop retry: the upstream closed the stream before sending any
# data — nothing was consumed, so re-issuing the same round is lossless.
# Retry up to this many times with a 15 s * attempt backoff; 0 disables.
ZERO_BYTE_DROP_MAX_RETRIES = int(os.getenv("ZERO_BYTE_DROP_MAX_RETRIES", "2"))
# Reserved tokens for an injected context-pressure system message.
_CTX_HINT_TOKENS = 256
# Upstream vLLM hard-rejects any prompt containing more than 10 images
# ("At most 10 image(s) may be provided in one prompt"). opencode keeps
# every image read by the read tool in the session history and re-sends
# the whole history on every request, so a long OCR session eventually
# exceeds the limit and every subsequent request fails with HTTP 400 -
# the session is dead until the images leave the history. To keep the
# session alive, the proxy replaces the OLDEST image parts with a text
# placeholder, keeping at most this many images per prompt. 0 disables.
MAX_IMAGES_IN_PROMPT = int(os.getenv("MAX_IMAGES_IN_PROMPT", "8"))

# ---------------------------------------------------------------------------
# Context truncation (token-budget aware, oldest-tool-first)
# ---------------------------------------------------------------------------
# When the estimated prompt exceeds the context budget the proxy trims the
# history instead of letting the upstream reject the request:
#   1. the system prompt and the last TRUNCATE_PROTECT_LAST_N messages are
#      never touched;
#   2. the oldest unprotected tool results are content-truncated first
#      (head_tail: keep the start and the tail/error, cut the middle);
#   3. if that is still not enough, the oldest unprotected regular
#      user/assistant messages are dropped from the head of the history.
# Budget = context - output_buffer - system_prompt_tokens.
CONTEXT_TRUNCATE_ENABLED = os.getenv(
    "CONTEXT_TRUNCATE_ENABLED", "true"
).strip().lower() not in ("0", "false", "no", "off", "disabled")
TRUNCATE_PROTECT_LAST_N = int(os.getenv("TRUNCATE_PROTECT_LAST_N", "2"))
TRUNCATE_OUTPUT_BUFFER_TOKENS = int(
    os.getenv("TRUNCATE_OUTPUT_BUFFER_TOKENS", "4096")
)
# chars per token used for the budget estimate (3.5 or 3).
TRUNCATE_CHARS_PER_TOKEN = float(os.getenv("TRUNCATE_CHARS_PER_TOKEN", "3.5"))
# Never shrink a single tool result below this many tokens.
TRUNCATE_TOOL_MIN_KEEP_TOKENS = int(
    os.getenv("TRUNCATE_TOOL_MIN_KEEP_TOKENS", "256")
)
# Default head/tail split for tool results that are neither clearly a
# document nor clearly a log.
TRUNCATE_TOOL_HEAD_RATIO = float(os.getenv("TRUNCATE_TOOL_HEAD_RATIO", "0.5"))
# Marker inserted where content was cut ({n} = removed chars).
TRUNCATE_MARKER_TEMPLATE = os.getenv(
    "TRUNCATE_MARKER_TEMPLATE", "\n...[truncated {n} chars]...\n"
)

# ---------------------------------------------------------------------------
# Token-quota counter (rolling window over input + output tokens)
# ---------------------------------------------------------------------------
# The corporate API enforces a per-hour token quota where the limit is the
# SUM of input (prompt) and output (completion) tokens. The proxy counts
# both over the last TOKEN_WINDOW_SECONDS and reports the running total in
# the console (colored [QUOTA] line) and in the log after every stream
# round. Input tokens come from usage.prompt_tokens when available; output
# from usage.completion_tokens. On a broken stream (which never delivers the
# final usage chunk) both fall back to char-based estimates so the counter
# stays honest and the break/cooldown point is visible.
TOKEN_WINDOW_SECONDS = int(os.getenv("TOKEN_WINDOW_SECONDS", "3600"))
# Optional quota in tokens per window (input + output combined);
# 0 = count only, no quota warnings.
HOURLY_TOKEN_QUOTA = int(os.getenv("HOURLY_TOKEN_QUOTA", "0"))
# Restore the counter from the log on startup. Without it the first [QUOTA]
# line of a fresh process shows only the rounds of that process, so an
# already exhausted quota looks like it appeared out of nowhere.
QUOTA_STATE_RESTORE = os.getenv("QUOTA_STATE_RESTORE", "1").strip().lower() not in (
    "0", "false", "no", "off", "disabled",
)
TOKEN_STATE_LOOKBACK_SECONDS = int(
    os.getenv("TOKEN_STATE_LOOKBACK_SECONDS", str(24 * 3600))
)
# A gap longer than this between logged rounds starts a new session, so the
# session totals do not accumulate across days.
TOKEN_SESSION_IDLE_GAP_SECONDS = int(
    os.getenv("TOKEN_SESSION_IDLE_GAP_SECONDS", str(4 * 3600))
)


def _parse_hours(raw):
    """Parse a comma-separated list of positive integer hours."""
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h = int(part)
        except ValueError:
            continue
        if h > 0 and h not in out:
            out.append(h)
    return tuple(out)


# Additional cumulative windows (hours) reported next to the main window so
# multi-hour quota usage (e.g. a limit that spans several hours) is visible
# in both the console and the log.
TOKEN_EXTRA_WINDOWS_HOURS = _parse_hours(
    os.getenv("TOKEN_EXTRA_WINDOWS_HOURS", "2,3,4")
)


def _fmt_tokens(n):
    return f"{n:,}".replace(",", " ")


def _plural_ru(n, forms):
    """Russian plural form for n: forms = (one, few, many)."""
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def _window_label(seconds=None):
    """Human label for a rolling window: "1 час", "30 минут", "24 часа"."""
    total = max(60, int(TOKEN_WINDOW_SECONDS if seconds is None else seconds))
    if total % 3600 == 0:
        n, forms = total // 3600, ("час", "часа", "часов")
    else:
        n, forms = total // 60, ("минуту", "минуты", "минут")
    return f"{n} {_plural_ru(n, forms)}"


_QUOTA_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
# Current format: "... | этот раунд: in +76 206, out +219, итого +76 425 ..."
_QUOTA_ROUND_IN_OUT_RE = re.compile(r"этот раунд: in \+([\d\s]+), out \+([\d\s]+)")
# Legacy format counted completion tokens only: "... (этот раунд +2 268) ..."
_QUOTA_ROUND_OUT_ONLY_RE = re.compile(r"\(этот раунд \+([\d\s]+)\)")


class _HourlyTokenTracker:
    """Thread-safe rolling-window counter of input + output tokens."""

    def __init__(self, window_seconds, extra_windows_hours=()):
        self._window = max(1, int(window_seconds))
        self._extra_windows = tuple(
            int(h) for h in extra_windows_hours if int(h) > 0
        )
        self._retention = max(
            [self._window] + [h * 3600 for h in self._extra_windows]
        )
        self._events = deque()  # (wall time, input tokens, output tokens)
        self._lock = threading.Lock()
        self.session_input = 0
        self.session_output = 0

    def record(self, input_tokens, output_tokens):
        """Add this round's input/output tokens to the window; return a
        stats dict, or None when there is nothing to count."""
        def _valid(n):
            return isinstance(n, int) and not isinstance(n, bool) and n > 0

        in_tok = input_tokens if _valid(input_tokens) else 0
        out_tok = output_tokens if _valid(output_tokens) else 0
        if in_tok <= 0 and out_tok <= 0:
            return None
        now = time.time()
        with self._lock:
            self.session_input += in_tok
            self.session_output += out_tok
            self._events.append((now, in_tok, out_tok))
            # Retain events long enough for the widest reported window.
            cutoff = now - self._retention
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

            def _sums(seconds):
                c = now - seconds
                wi = sum(e[1] for e in self._events if e[0] >= c)
                wo = sum(e[2] for e in self._events if e[0] >= c)
                return wi, wo

            window_input, window_output = _sums(self._window)
            extra = []
            for h in self._extra_windows:
                wi, wo = _sums(h * 3600)
                extra.append({
                    "hours": h,
                    "input": wi,
                    "output": wo,
                    "total": wi + wo,
                })
            return {
                "window_input": window_input,
                "window_output": window_output,
                "window_total": window_input + window_output,
                "window_seconds": self._window,
                "session_input": self.session_input,
                "session_output": self.session_output,
                "session_total": self.session_input + self.session_output,
                "this_input": in_tok,
                "this_output": out_tok,
                "this_total": in_tok + out_tok,
                "extra_windows": extra,
            }

    def snapshot(self):
        """Read-only view of the current window/session (records nothing)."""
        now = time.time()
        with self._lock:
            def _sums(seconds):
                c = now - seconds
                wi = sum(e[1] for e in self._events if e[0] >= c)
                wo = sum(e[2] for e in self._events if e[0] >= c)
                return wi, wo

            window_input, window_output = _sums(self._window)
            extra = []
            for h in self._extra_windows:
                wi, wo = _sums(h * 3600)
                extra.append({
                    "hours": h,
                    "input": wi,
                    "output": wo,
                    "total": wi + wo,
                })
            return {
                "window_input": window_input,
                "window_output": window_output,
                "window_total": window_input + window_output,
                "window_seconds": self._window,
                "session_input": self.session_input,
                "session_output": self.session_output,
                "session_total": self.session_input + self.session_output,
                "extra_windows": extra,
            }

    def restore_from_log(self, log_path, lookback_seconds=None,
                         idle_gap_seconds=None):
        """Rebuild the rolling window (and session totals) from [QUOTA]
        lines written by earlier proxy processes.

        Only per-round increments are replayed, so the window is recomputed
        against the current clock instead of trusting totals printed at the
        time. Rounds older than lookback_seconds are ignored. Both log
        formats are understood: the current one ("этот раунд: in +X, out
        +Y") and the legacy one that counted completion tokens only
        ("(этот раунд +N)")."""
        lookback = (
            TOKEN_STATE_LOOKBACK_SECONDS
            if lookback_seconds is None
            else int(lookback_seconds)
        )
        gap = (
            TOKEN_SESSION_IDLE_GAP_SECONDS
            if idle_gap_seconds is None
            else int(idle_gap_seconds)
        )
        now = time.time()
        cutoff = now - max(1, lookback)
        events = []
        legacy_rounds = 0
        skipped = 0
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "[QUOTA]" not in line:
                        continue
                    plain = _ANSI_RE.sub("", line)
                    if "накоплено" in plain or "не учтены" in plain:
                        continue  # informational companion lines
                    ts_match = _QUOTA_TS_RE.match(plain)
                    if not ts_match:
                        skipped += 1
                        continue
                    try:
                        ts = time.mktime(
                            time.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                        )
                    except (ValueError, OverflowError):
                        skipped += 1
                        continue
                    if ts < cutoff or ts > now + 60:
                        skipped += 1
                        continue
                    m = _QUOTA_ROUND_IN_OUT_RE.search(plain)
                    if m:
                        in_tok = int(m.group(1).replace(" ", ""))
                        out_tok = int(m.group(2).replace(" ", ""))
                    else:
                        m = _QUOTA_ROUND_OUT_ONLY_RE.search(plain)
                        if not m:
                            skipped += 1
                            continue
                        in_tok = 0
                        out_tok = int(m.group(1).replace(" ", ""))
                        legacy_rounds += 1
                    if in_tok <= 0 and out_tok <= 0:
                        skipped += 1
                        continue
                    events.append((ts, in_tok, out_tok))
        except OSError as exc:
            return {"ok": False, "path": log_path, "error": str(exc)}

        events.sort(key=lambda e: e[0])
        with self._lock:
            for ev in events:
                self._events.append(ev)
            keep_from = now - self._retention
            while self._events and self._events[0][0] < keep_from:
                self._events.popleft()
            # The current session starts after the last long idle gap.
            session_start = 0
            for idx in range(1, len(events)):
                if events[idx][0] - events[idx - 1][0] > gap:
                    session_start = idx
            current = events[session_start:]
            self.session_input = sum(e[1] for e in current)
            self.session_output = sum(e[2] for e in current)
            window_from = now - self._window
            window_input = sum(e[1] for e in self._events if e[0] >= window_from)
            window_output = sum(e[2] for e in self._events if e[0] >= window_from)
            return {
                "ok": True,
                "path": log_path,
                "rounds_restored": len(events),
                "legacy_rounds": legacy_rounds,
                "skipped_lines": skipped,
                "events_in_memory": len(self._events),
                "window_input": window_input,
                "window_output": window_output,
                "window_total": window_input + window_output,
                "session_input": self.session_input,
                "session_output": self.session_output,
                "session_total": self.session_input + self.session_output,
                "session_rounds": len(current),
                "lookback_seconds": lookback,
                "idle_gap_seconds": gap,
                "newest_event": events[-1][0] if events else None,
            }


TOKEN_TRACKER = _HourlyTokenTracker(
    TOKEN_WINDOW_SECONDS, TOKEN_EXTRA_WINDOWS_HOURS
)


def _restore_quota_with_timeout(timeout=30):
    """Run restore_from_log on a daemon thread with a join timeout.

    restore_from_log reads the log synchronously; if OneDrive has
    dehydrated it to a placeholder that cannot re-download, the read
    blocks forever and the proxy never reaches app.run (seen 02.11:
    port 8081 never bound). A blocked restore is worse than an empty
    window: the proxy serves nothing."""
    box = {}

    def _run():
        box["value"] = TOKEN_TRACKER.restore_from_log(LOG_FILE)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning(
            "[QUOTA] restore from %s did not finish in %ss (log is likely "
            "an online-only OneDrive file); starting with an empty window",
            LOG_FILE,
            timeout,
        )
        return {
            "ok": False,
            "path": LOG_FILE,
            "error": "restore timed out (blocked read, e.g. OneDrive placeholder)",
        }
    return box.get("value") or {
        "ok": False,
        "path": LOG_FILE,
        "error": "restore returned no result",
    }


def _log_quota_restore(restored):
    """Report the result of restoring the counter from the log."""
    if not restored.get("ok"):
        logger.warning(
            "[QUOTA] не удалось восстановить историю из %s: %s",
            restored.get("path", LOG_FILE),
            restored.get("error"),
        )
        return
    logger.info(
        "[QUOTA] восстановлена история из %s: %s за %s, "
        "окно %s = %s токенов (in %s + out %s), сессия %s (%s)%s",
        restored["path"],
        f"{restored['rounds_restored']} "
        f"{_plural_ru(restored['rounds_restored'], ('раунд', 'раунда', 'раундов'))}",
        _window_label(restored["lookback_seconds"]),
        _window_label(),
        _fmt_tokens(restored["window_total"]),
        _fmt_tokens(restored["window_input"]),
        _fmt_tokens(restored["window_output"]),
        _fmt_tokens(restored["session_total"]),
        f"{restored['session_rounds']} "
        f"{_plural_ru(restored['session_rounds'], ('раунд', 'раунда', 'раундов'))}",
        (
            f", legacy-раундов: {restored['legacy_rounds']}"
            if restored["legacy_rounds"] else ""
        ),
    )


# ---------------------------------------------------------------------------
# opencode TUI quota toasts. Credentials are taken from the sidecar file the
# gw-creds plugin rewrites on every opencode start, so a restarted opencode
# (new port) is picked up automatically.
# ---------------------------------------------------------------------------
OC_SIDECAR_FILE = os.getenv(
    "OPENCODE_SIDECAR_FILE",
    r"C:\Automation\Creo_API_QA_PG\Gateway Opencode\.gw-sidecar.json",
)
OC_QUOTA_TOASTS = os.getenv("OPENCODE_QUOTA_TOASTS", "1") == "1"
_last_quota_toast_tier = 0


def _oc_toast(title, message, variant="info"):
    """Push one toast into the local opencode TUI. Never raises."""
    try:
        with open(OC_SIDECAR_FILE, "r", encoding="utf-8") as fh:
            creds = json.load(fh)
        auth = "Basic " + base64.b64encode(
            ("%s:%s" % (creds.get("username", "opencode"),
                        creds.get("password", ""))).encode()
        ).decode()
        requests.post(
            creds["url"].rstrip("/") + "/tui/show-toast",
            json={"title": title, "message": message, "variant": variant,
                  "duration": 8000},
            headers={"Authorization": auth},
            timeout=3,
        )
    except Exception:
        pass  # a failed notification must never break the request flow


def _quota_toast_maybe(used_pct, window_total, left):
    """Toast once per crossed threshold (80/95/100%) per window."""
    global _last_quota_toast_tier
    if not OC_QUOTA_TOASTS:
        return
    if used_pct >= 100.0:
        tier = 3
    elif used_pct >= 95.0:
        tier = 2
    elif used_pct >= 80.0:
        tier = 1
    else:
        _last_quota_toast_tier = 0  # window rolled over / usage dropped
        return
    if tier <= _last_quota_toast_tier:
        return
    _last_quota_toast_tier = tier
    label = _window_label()
    if tier == 3:
        msg = "КВОТА за %s ПЕРЕВЫШЕНА: %s из %s" % (
            label, _fmt_tokens(window_total), _fmt_tokens(HOURLY_TOKEN_QUOTA))
        variant = "error"
    elif tier == 2:
        msg = "Квота за %s: использовано %.0f%%, осталось %s из %s" % (
            label, used_pct, _fmt_tokens(left), _fmt_tokens(HOURLY_TOKEN_QUOTA))
        variant = "error"
    else:
        msg = "Квота за %s: использовано %.0f%%, осталось %s" % (
            label, used_pct, _fmt_tokens(left))
        variant = "warning"
    threading.Thread(
        target=_oc_toast, args=("Квота", msg, variant), daemon=True
    ).start()

# ---------------------------------------------------------------------------
# (040726) Watchdog removed.
#
# The watchdog was a background thread that, every WATCHDOG_INTERVAL
# seconds, checked whether the proxy had been idle and — if so — fired a
# non-streaming nudge ("Ты закончил выполнение задачи? ...") to the
# upstream LLM in the hope of either keeping the session warm or
# diagnosing a perceived freeze.
#
# In practice it was net-negative:
#   * it consumed an extra upstream request every few minutes, even
#     when the proxy was simply idle and the model was fine;
#   * its "diagnostic" value was zero — the model just answered
#     "Задача выполнена" to the probe regardless of the real state of
#     the user's task, so the log entry never told us anything new;
#   * the actual root cause of the user's "freezes" turned out to be
#     the model emitting a user-input tool call (ask_user / question /
#     clarify) that the IDE never surfaced — the user-input-tool
#     detection warning in stream_from_corp() is the real fix for
#     that, not a periodic probe.
#
# If a watchdog ever becomes useful again (e.g. for a different model
# that needs keep-alive pings), reintroduce it as an env-gated thread
# rather than always-on, and make sure its probe is informative.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def refresh_token():
    global current_token
    logger.info("Auth: requesting token from %s", AUTH_URL)
    try:
        r = requests.post(
            AUTH_URL,
            json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD},
            verify=UPSTREAM_VERIFY_SSL,
            timeout=30,
        )
        if r.status_code != 200:
            logger.error("Auth failed: %s — %s", r.status_code, r.text[:500])
            return False
        data = r.json()
        token = data.get("token") or data.get("access_token") or data.get("accessToken")
        if not token:
            logger.error("Auth: no token in response, keys=%s", list(data.keys()))
            return False
        current_token = token
        logger.info("Auth: token obtained")
        return True
    except Exception as e:
        logger.error("Auth error: %s", e)
        return False


def ensure_token():
    if current_token is None:
        logger.warning("No token, refreshing...")
        return refresh_token()
    return True


# ---------------------------------------------------------------------------
# Payload preparation
# ---------------------------------------------------------------------------
# Strips <think>...</think> blocks that assistant turns may carry over from
# previous responses. Qwen3-family models are documented to get confused
# (and tend to give up / stop early) when old <think> blocks accumulate in
# the conversation history — each turn should only see the *final* content
# of prior assistant messages, not their internal reasoning. Left unstripped,
# a long multi-turn session (dozens of tool calls) can balloon the request
# body by hundreds of KB of stale reasoning text, which matches real
# incidents we've seen (148 messages / ~800KB payload) right before the
# model silently stopped mid-task.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think_blocks(text):
    if not isinstance(text, str) or "<think" not in text.lower():
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text)
    # Collapse leftover blank lines caused by the removed block.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _strip_think_from_messages(messages):
    """Remove <think> blocks from assistant message content (string or
    list-of-parts content). Only assistant turns can contain them, but we
    check defensively regardless of role."""
    stripped_count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            new_content = _strip_think_blocks(content)
            if new_content != content:
                stripped_count += 1
            msg["content"] = new_content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    new_text = _strip_think_blocks(part["text"])
                    if new_text != part["text"]:
                        stripped_count += 1
                    part["text"] = new_text
    return stripped_count


# ---------------------------------------------------------------------------
# Image-part detection & limit enforcement
# ---------------------------------------------------------------------------
# opencode embeds every image read by the read tool into the session history
# as a base64 image part and re-sends the whole history on every request.
# The upstream vLLM rejects any prompt with more than 10 images (HTTP 400),
# which permanently kills the session. This section keeps the outbound
# prompt within the limit by replacing the oldest image parts with a short
# text placeholder (OCR results already extracted by the model remain in
# the assistant turns).
_IMAGE_PART_TYPES = ("image_url", "input_image")


def _part_image_url(part):
    """Return the image URL carried by a content part, or None if the part
    is not an image."""
    if not isinstance(part, dict):
        return None
    ptype = part.get("type")
    iu = part.get("image_url")
    if ptype in _IMAGE_PART_TYPES:
        if isinstance(iu, str):
            return iu
        if isinstance(iu, dict) and isinstance(iu.get("url"), str):
            return iu["url"]
        return "<image>"
    if ptype == "file":
        f = part.get("file")
        if isinstance(f, dict):
            for key in ("file_data", "url"):
                v = f.get(key)
                if isinstance(v, str) and v.startswith("data:image"):
                    return v
    for key in ("file_data", "url"):
        v = part.get(key)
        if isinstance(v, str) and v.startswith("data:image"):
            return v
    return None


def _image_mime(url):
    if isinstance(url, str) and url.startswith("data:"):
        return url.split(",", 1)[0]
    return ""


def _enforce_image_limit(messages, keep):
    """Replace the oldest image parts beyond `keep` with a text placeholder
    so the outbound prompt stays within the upstream image cap. Mutates
    `messages` in place. Returns the number of removed images."""
    if keep < 0:
        keep = 0
    images = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for i, part in enumerate(content):
            if _part_image_url(part) is not None:
                images.append((msg, i, part))
    excess = len(images) - keep
    if excess <= 0:
        return 0
    removed = 0
    for msg, i, part in images[:excess]:
        mime = _image_mime(_part_image_url(part))
        msg["content"][i] = {
            "type": "text",
            "text": (
                "[image removed from context to respect the upstream "
                "image limit" + (f"; was {mime}" if mime else "") + "]"
            ),
        }
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# Tool-error detection & recovery prompt injection
# ---------------------------------------------------------------------------
# When the model invokes a tool and the tool returns an error (e.g. a
# Python SyntaxError in generated code, a file-not-found, a network
# failure, a refused action), Qwen 3.8 27B in our setup has been observed
# to respond with an *empty* `finish_reason=stop` turn — i.e. it silently
# gives up on the next step. To break that pattern, when the *most
# recent* tool message in the conversation history looks like an error
# and the last assistant turn before it had `tool_calls`, we inject a
# short user-style recovery reminder right after the failing tool
# message. This nudges the model to attempt a fix instead of ending
# the turn empty.
_TOOL_ERROR_MARKERS = (
    "syntaxerror",
    "traceback (most recent call last)",
    "nameerror",
    "typeerror",
    "valueerror",
    "importerror",
    "modulenotfounderror",
    "attributeerror",
    "indexerror",
    "keyerror",
    "filenotfounderror",
    "permissionerror",
    "ioerror",
    "oserror",
    "exception:",
    "error:",
    "failed",
    "fatal",
    "could not",
    "doesn't exist",
    "does not exist",
    "не найден",
    "ошибка",
    "traceback",
    "unterminated",
    "unexpected eof",
    "invalid syntax",
    "no such file",
    "no module named",
    "command not found",
    "exit code",
    "non-zero exit",
    "stderr",
)


def _looks_like_tool_error(content):
    """Heuristic: does this tool message body look like an error report
    (Python traceback, shell stderr, exception text, etc.)?"""
    if not isinstance(content, str):
        return False
    lower = content.lower()
    if len(lower) > 8000:
        lower = lower[:8000]
    for marker in _TOOL_ERROR_MARKERS:
        if marker in lower:
            return True
    return False


def _last_assistant_had_tool_calls(messages):
    """Walk backwards from the end of `messages` and return True if the
    most recent assistant turn contained tool_calls. Stops scanning at
    the first user/system message encountered, so we only consider the
    tail that belongs to the *current* turn cycle."""
    for m in reversed(messages or []):
        role = m.get("role")
        if role == "assistant":
            return bool(m.get("tool_calls"))
        if role in ("user", "system"):
            return False
    return False


def _maybe_inject_recovery(messages):
    """If the most recent tool message in `messages` is an error and the
    assistant turn that produced it had tool_calls, append a synthetic
    user message that nudges the model to either fix the error and
    continue OR — if it genuinely needs the user's input — actually
    CALL a user-input tool (question/ask_user/clarify), not just write
    a textual question and end the turn.

    To avoid spamming recovery messages across long multi-turn sessions
    where the model has already recovered, we only inject when:
      * there is no user-style recovery message already at the tail, and
      * the very last message in the list is a tool message with error
        content (i.e. we are sitting right after the failure).
    Returns the number of messages appended (0 or 1)."""
    if not messages:
        return 0
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "tool":
        return 0
    if not _looks_like_tool_error(last.get("content")):
        return 0
    # We need to confirm the *assistant turn* before this tool message
    # was the one that requested the tool call. Walk back skipping over
    # consecutive tool messages.
    saw_assistant_with_tools = False
    for m in reversed(messages[:-1]):
        role = m.get("role")
        if role == "assistant":
            if m.get("tool_calls"):
                saw_assistant_with_tools = True
            break
        if role in ("user", "system"):
            break
    if not saw_assistant_with_tools:
        return 0

    # Avoid double-injection: if a recovery message is already the
    # previous user message, do nothing.
    for m in reversed(messages[:-1]):
        role = m.get("role")
        if role in ("user", "system"):
            c = m.get("content")
            if isinstance(c, str) and c.startswith("[recovery]"):
                return 0
            break

    recovery = (
        "[recovery] The previous tool call returned an error. "
        "Decide one of:\n"
        "  (a) if you genuinely need clarification or confirmation from the user, "
        "CALL the appropriate tool (question/ask_user/clarify) — do NOT just write "
        "a textual question and end the turn, because the user cannot see text-only "
        "questions;\n"
        "  (b) otherwise, fix the issue (correct the syntax, path, arguments, or "
        "approach) and continue the original task with a corrected tool call.\n"
        "Do not produce a 'ready, awaiting your answer' message without actually "
        "invoking a user-input tool. Do not stop until the task is genuinely "
        "finished or you have hit a blocker that the user must resolve."
    )
    messages.append({"role": "user", "content": recovery})
    return 1


# ---------------------------------------------------------------------------
# reasoning_effort normalization
# ---------------------------------------------------------------------------
# The vLLM upstream accepts: none, xhigh (default), medium, low.
# The client (opencode) sends "high" for its top thinking level, which the
# upstream rejects with HTTP 400 ("Unexpected reasoning effort high").
# Map client values onto the supported set; unknown values are stripped so
# the upstream default (xhigh) applies.
_REASONING_EFFORT_MAP = {
    "none": "none",
    "xhigh": "xhigh",
    "high": "xhigh",
    "medium": "medium",
    "med": "medium",
    "low": "low",
    "minimal": "low",
}


def _normalize_reasoning_effort(value):
    """Return the upstream-supported reasoning_effort for a client value,
    or None to strip the parameter (upstream then uses its default)."""
    if not isinstance(value, str):
        return None
    return _REASONING_EFFORT_MAP.get(value.strip().lower())


# ---------------------------------------------------------------------------
# Prompt-size tracking — proactive context capping
# ---------------------------------------------------------------------------
# vLLM rejects any request with prompt + max_tokens > context with an
# immediate HTTP 400, and the error text ("prompt contains at least N
# input tokens") only carries N = context - max_tokens + 1 — a tautology,
# not the real prompt size. The proxy therefore cannot solve for a safe
# budget from the error alone: every correction attempt is a fresh request
# that fails again upstream, and each one shows up in the upstream server
# logs as a 400. To keep those 400s out of the upstream logs, the proxy
# learns the exact prompt size from the usage block of every successful
# stream (usage.prompt_tokens) and caps the next request's output budget
# *before* sending it.
_PROMPT_TRACKER = []            # newest last; bounded to _PROMPT_TRACKER_MAX
_PROMPT_TRACKER_MAX = 16
_PROMPT_CHARS_PER_TOKEN = 3     # conservative chars/token for mixed text
_IMAGE_STANDIN_CHARS = 2500     # fixed stand-in for an image part's token cost


def _message_texts_for_fingerprint(content):
    """Return (hash_text, estimate_text) for a message content value.
    hash_text is the canonical form used for matching; estimate_text is
    what the char->token estimate counts (image data-URLs replaced by a
    fixed stand-in so base64 size does not skew the estimate)."""
    if isinstance(content, str):
        return content, content
    if isinstance(content, list):
        hash_parts = []
        est_parts = []
        for part in content:
            if isinstance(part, dict):
                canonical = json.dumps(part, sort_keys=True, default=str)
                hash_parts.append(canonical)
                ptype = part.get("type")
                if ptype == "text":
                    est_parts.append(str(part.get("text", "")))
                elif ptype in ("image_url", "image"):
                    est_parts.append("x" * _IMAGE_STANDIN_CHARS)
                else:
                    est_parts.append(canonical)
            else:
                s = str(part)
                hash_parts.append(s)
                est_parts.append(s)
        return "\x00".join(hash_parts), "\x00".join(est_parts)
    if content is None:
        return "", ""
    s = str(content)
    return s, s


def _fingerprint_messages(messages):
    """Fingerprint a message list as (tuple of (role, sha1) per message,
    tuple of cumulative estimate-text chars). Stable across requests as
    long as the client re-sends its own history unchanged, which lets us
    find the previous request of the same conversation by prefix match."""
    fp = []
    prefix = [0]
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", ""))
        hash_text, est_text = _message_texts_for_fingerprint(m.get("content"))
        total += len(est_text)
        fp.append((role, hashlib.sha1(hash_text.encode("utf-8", "replace")).hexdigest()))
        prefix.append(total)
    return tuple(fp), tuple(prefix)


def _record_prompt_usage(fp, char_prefix, usage):
    if not fp or not isinstance(usage, dict):
        return
    prompt_tokens = usage.get("prompt_tokens")
    if not isinstance(prompt_tokens, int) or prompt_tokens <= 0:
        return
    completion_tokens = usage.get("completion_tokens")
    if not isinstance(completion_tokens, int):
        completion_tokens = 0
    _PROMPT_TRACKER.append({
        "fp": fp,
        "char_prefix": char_prefix,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    })
    if len(_PROMPT_TRACKER) > _PROMPT_TRACKER_MAX:
        del _PROMPT_TRACKER[: len(_PROMPT_TRACKER) - _PROMPT_TRACKER_MAX]


def _estimate_prompt_tokens(fp, char_prefix):
    """Estimate the current prompt size in tokens from the last successful
    upstream response of the same conversation, or None when no tracked
    conversation matches (fresh session, /compact, proxy restart)."""
    if not fp:
        return None
    for entry in reversed(_PROMPT_TRACKER):
        prev_fp = entry["fp"]
        if len(prev_fp) > len(fp) or fp[: len(prev_fp)] != prev_fp:
            continue
        if len(prev_fp) == len(fp):
            # Identical prompt (client retry): size is what we already saw.
            return entry["prompt_tokens"] + PROMPT_GROWTH_SAFETY_TOKENS
        new_msgs = fp[len(prev_fp):]
        extra_chars = 0
        for offset, (role, _digest) in enumerate(new_msgs):
            idx = len(prev_fp) + offset
            if offset == 0 and role == "assistant":
                # The previous response as recorded by the client; its token
                # cost is already covered by completion_tokens.
                continue
            extra_chars += char_prefix[idx + 1] - char_prefix[idx]
        return (
            entry["prompt_tokens"]
            + entry["completion_tokens"]
            + extra_chars // _PROMPT_CHARS_PER_TOKEN
            + PROMPT_GROWTH_SAFETY_TOKENS
        )
    return None


def _ctx_pressure_hint(est_prompt, context_tokens):
    """System note injected when the context window is nearly full."""
    return {
        "role": "system",
        "content": (
            f"[Контекст почти полон: ~{est_prompt} из {context_tokens} токенов.] "
            "Не читай большие файлы целиком — ищи по grep и читай точечно "
            "(offset/limit). Если задачу не удаётся завершить до переполнения, "
            "прямо попроси пользователя выполнить /compact или начать новую сессию."
        ),
    }


def _ctx_exhausted_hint():
    """System note for the last-words request: one short line, no tools."""
    return {
        "role": "system",
        "content": (
            "[Контекст переполнен, места на полный ответ не осталось.] "
            "Ответь одной короткой фразой: попроси пользователя выполнить "
            "/compact или начать новую сессию. Не вызывай инструменты."
        ),
    }


# ---------------------------------------------------------------------------
# Context truncation helpers
# ---------------------------------------------------------------------------
def _truncate_marker(removed_chars):
    """Marker inserted where content was cut, so the model knows a chunk is
    missing and does not try to reconstruct it."""
    try:
        return TRUNCATE_MARKER_TEMPLATE.format(n=removed_chars)
    except (KeyError, ValueError, IndexError):
        return "\n...[truncated]...\n"


def truncate_text(text, max_chars, mode="head_tail", head_ratio=0.5):
    """Truncate `text` to at most `max_chars` characters.

    mode:
      "head"      keep the beginning (files, documents, chat history);
      "tail"      keep the end (logs, stderr, command results);
      "head_tail" keep both ends and cut the middle (default for tool output).

    Returns (new_text, removed_chars, was_truncated). The inserted marker is
    counted inside `max_chars`.
    """
    if not isinstance(text, str) or max_chars <= 0 or len(text) <= max_chars:
        return text, 0, False

    # Reserve the widest possible marker (digit count of len(text)).
    reserve = len(_truncate_marker(len(text)))
    if max_chars <= reserve:
        new_text = text[:max_chars]
        return new_text, len(text) - len(new_text), True

    keep = max_chars - reserve
    if mode == "tail":
        head_part, tail_part = "", text[-keep:]
    elif mode == "head":
        head_part, tail_part = text[:keep], ""
    else:
        ratio = head_ratio if 0.0 <= head_ratio <= 1.0 else 0.5
        head_len = int(keep * ratio)
        tail_len = keep - head_len
        head_part = text[:head_len]
        tail_part = text[len(text) - tail_len:] if tail_len > 0 else ""

    removed = len(text) - len(head_part) - len(tail_part)
    new_text = head_part + _truncate_marker(removed) + tail_part
    return new_text, removed, True


def _truncate_message_content(msg, max_chars, mode, head_ratio):
    """Truncate a message's content in place. Handles plain strings and
    list-of-parts (text parts only; image parts are left to the image cap).
    Returns (removed_chars, changed)."""
    content = msg.get("content")
    if isinstance(content, str):
        new_text, removed, changed = truncate_text(
            content, max_chars, mode, head_ratio
        )
        if changed:
            msg["content"] = new_text
        return removed, changed
    if isinstance(content, list):
        parts = [
            part for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        total = sum(len(part["text"]) for part in parts)
        if total <= max_chars or total == 0:
            return 0, False
        removed_total = 0
        changed_any = False
        for part in parts:
            share = max(1, int(max_chars * (len(part["text"]) / total)))
            new_text, removed, changed = truncate_text(
                part["text"], share, mode, head_ratio
            )
            if changed:
                part["text"] = new_text
                removed_total += removed
                changed_any = True
        return removed_total, changed_any
    return 0, False


_DOC_MARKERS = (
    "```", "def ", "class ", "import ", "function ", "<!doctype", "<html",
    "<?xml", "select ", "create table", "## ",
)
_LOG_MARKERS = (
    "traceback", "stderr", "exception", "error:", "failed", "fatal",
    "exit code", "stack trace", "panic:", " at ",
)


def _tool_truncation_mode(text):
    """Choose the head/tail split for a tool result: documents keep more of
    the beginning, logs/errors keep more of the end, otherwise split evenly."""
    low = (text or "").lower()
    head_hits = sum(1 for marker in _DOC_MARKERS if marker in low)
    tail_hits = sum(1 for marker in _LOG_MARKERS if marker in low)
    if head_hits > tail_hits:
        return "head_tail", 0.7
    if tail_hits > head_hits:
        return "head_tail", 0.3
    return "head_tail", TRUNCATE_TOOL_HEAD_RATIO


def _estimate_messages_chars(messages):
    """Estimated character size of a message list (image data-URLs counted
    as a fixed stand-in, tool_calls counted by their JSON size)."""
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        _hash_text, est_text = _message_texts_for_fingerprint(m.get("content"))
        total += len(est_text)
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"], default=str))
    return total


def fit_messages(messages, context_tokens, output_buffer_tokens, protect_last_n=2):
    """Trim `messages` in place so the estimated prompt fits the budget.

    Order (oldest-tool-first):
      1. the system prompt and the last `protect_last_n` messages are never
         touched;
      2. the oldest unprotected tool results are content-truncated first
         (head_tail: keep the start and the tail/error, cut the middle);
      3. if still over budget, the oldest unprotected regular user/assistant
         messages are dropped from the head of the history.

    Budget = context - output_buffer - system_prompt_tokens.
    Returns a stats dict for logging.
    """
    stats = {
        "total_chars_before": 0,
        "total_chars_after": 0,
        "budget_chars": 0,
        "system_chars": 0,
        "tool_truncated": 0,
        "tool_removed_chars": 0,
        "messages_dropped": 0,
        "dropped_chars": 0,
        "still_over": False,
    }
    if not isinstance(messages, list) or not messages:
        return stats

    total_chars = _estimate_messages_chars(messages)
    system_chars = sum(
        len(_message_texts_for_fingerprint(m.get("content"))[1])
        for m in messages
        if isinstance(m, dict) and m.get("role") == "system"
    )
    # Stay at least as conservative as the proactive cap's estimator so the
    # prompt that survives truncation still passes the estimate below.
    chars_per_token = min(TRUNCATE_CHARS_PER_TOKEN, _PROMPT_CHARS_PER_TOKEN)
    system_tokens = int(system_chars / chars_per_token)
    allowed_input_tokens = max(0, context_tokens - output_buffer_tokens) - system_tokens
    allowed_input_chars = int(max(0, allowed_input_tokens) * chars_per_token)

    stats["total_chars_before"] = total_chars
    stats["system_chars"] = system_chars
    stats["budget_chars"] = allowed_input_chars

    excess = total_chars - system_chars - allowed_input_chars
    if excess <= 0:
        stats["total_chars_after"] = total_chars
        return stats

    n = len(messages)
    protected = set(range(max(0, n - protect_last_n), n))
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "system":
            protected.add(i)

    # Step 2: oldest unprotected tool results first, then (as a last resort)
    # the protected ones. The system prompt is never truncated.
    tool_floor_chars = max(
        1, int(TRUNCATE_TOOL_MIN_KEEP_TOKENS * chars_per_token)
    )
    for pass_protected in (False, True):
        for i, m in enumerate(messages):
            if excess <= 0:
                break
            if not isinstance(m, dict) or m.get("role") != "tool":
                continue
            if (i in protected) != pass_protected:
                continue
            content = m.get("content")
            if isinstance(content, str):
                current_chars = len(content)
                probe_text = content
            elif isinstance(content, list):
                probe_text = "\n".join(
                    part["text"] for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
                current_chars = len(probe_text)
            else:
                continue
            if current_chars <= tool_floor_chars:
                continue
            target = max(tool_floor_chars, current_chars - excess)
            mode, ratio = _tool_truncation_mode(probe_text)
            removed, changed = _truncate_message_content(m, target, mode, ratio)
            if changed:
                stats["tool_truncated"] += 1
                stats["tool_removed_chars"] += removed
                excess -= removed

    # Step 3: drop oldest unprotected regular user/assistant messages.
    if excess > 0:
        drop = set()
        for i, m in enumerate(messages):
            if excess <= 0:
                break
            if i in protected or not isinstance(m, dict):
                continue
            if m.get("role") not in ("user", "assistant") or m.get("tool_calls"):
                continue
            c = len(_message_texts_for_fingerprint(m.get("content"))[1])
            drop.add(i)
            stats["messages_dropped"] += 1
            stats["dropped_chars"] += c
            excess -= c
        if drop:
            messages[:] = [m for i, m in enumerate(messages) if i not in drop]
            # Never leave an orphaned tool result at the head of the history.
            while (
                messages
                and isinstance(messages[0], dict)
                and messages[0].get("role") == "tool"
            ):
                messages.pop(0)

    stats["total_chars_after"] = _estimate_messages_chars(messages)
    stats["still_over"] = (
        stats["total_chars_after"] > system_chars + allowed_input_chars
    )
    return stats


# updated 030726
def prepare_payload(data):
    """Minimal processing: add thinking and preserve the client context."""
    p = dict(data)
    p["model"] = CORP_MODEL
    # Only default parent_id when the client didn't supply one — forcing it
    # to None unconditionally on every request can interfere with
    # upstream (Open-WebUI-style) session/budget bookkeeping.
    p.setdefault("parent_id", None)
    p["stream"] = True
    p["stream_options"] = {"include_usage": True}
    p.pop("stream_include_usage", None)

    if UPSTREAM_MAX_TOKENS > 0:
        client_max_tokens = p.get("max_tokens")
        if client_max_tokens != UPSTREAM_MAX_TOKENS:
            p["max_tokens"] = UPSTREAM_MAX_TOKENS
            logger.info(
                "Overrode client max_tokens=%r -> %d",
                client_max_tokens,
                UPSTREAM_MAX_TOKENS,
            )

    # Normalize reasoning_effort onto the upstream-supported set
    # (none/xhigh/medium/low); strip values the upstream would reject with 400.
    if "reasoning_effort" in p:
        original = p["reasoning_effort"]
        normalized = _normalize_reasoning_effort(original)
        if normalized is None:
            p.pop("reasoning_effort", None)
            logger.info("Stripped unsupported reasoning_effort=%r (upstream default applies)", original)
        elif normalized != original:
            logger.info("Mapped reasoning_effort %r -> %r", original, normalized)
            p["reasoning_effort"] = normalized

    # OpenCode model variants use llama.cpp's `reasoning_budget_tokens`
    # metadata to expose a per-level cap in the UI. The corporate backend is
    # vLLM, whose equivalent per-request sampling field is
    # `thinking_token_budget`. Translate at the proxy boundary and do not send
    # the llama.cpp-only alias upstream.
    reasoning_budget = p.pop("reasoning_budget_tokens", None)
    if reasoning_budget is not None and "thinking_token_budget" not in p:
        if (
            isinstance(reasoning_budget, int)
            and not isinstance(reasoning_budget, bool)
            and reasoning_budget >= 0
        ):
            p["thinking_token_budget"] = reasoning_budget
            logger.info(
                "Mapped reasoning_budget_tokens=%d -> thinking_token_budget=%d",
                reasoning_budget,
                reasoning_budget,
            )
        else:
            logger.warning(
                "Ignored invalid reasoning_budget_tokens=%r",
                reasoning_budget,
            )

    # --- Context hygiene + budget-aware truncation -------------------------
    # Order matters: think-strip and the image cap shrink the prompt first,
    # then fit_messages trims the history to the token budget, and only then
    # does the proactive cap size the output. Running it here keeps the
    # prompt estimate below consistent with what is actually sent upstream.
    messages = p.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    stripped = _strip_think_from_messages(messages)
    if stripped:
        logger.debug("Stripped  thinking blocks from %d message(s)", stripped)

    # Keep the prompt within the upstream image cap (vLLM: max 10 images per
    # prompt). Oldest images become text placeholders; OCR results already
    # extracted by the model remain in the assistant turns.
    if MAX_IMAGES_IN_PROMPT > 0:
        removed_images = _enforce_image_limit(messages, MAX_IMAGES_IN_PROMPT)
        if removed_images:
            logger.info(
                "Image cap: replaced %d oldest image(s) with placeholders (keeping %d)",
                removed_images,
                MAX_IMAGES_IN_PROMPT,
            )

    # Budget-aware truncation: never touch the system prompt or the last N
    # messages; truncate the oldest tool results first, then drop the oldest
    # plain turns only if that is still not enough.
    if CONTEXT_TRUNCATE_ENABLED and UPSTREAM_CONTEXT_TOKENS > 0 and messages:
        client_max_tokens = p.get("max_tokens")
        if (
            isinstance(client_max_tokens, int)
            and not isinstance(client_max_tokens, bool)
        ):
            output_buffer = min(client_max_tokens, TRUNCATE_OUTPUT_BUFFER_TOKENS)
        else:
            output_buffer = TRUNCATE_OUTPUT_BUFFER_TOKENS
        output_buffer = max(MIN_VISIBLE_OUTPUT_TOKENS, output_buffer)
        # Leave the same overflow margin the proactive cap below subtracts,
        # so a truncated prompt is not squeezed to a tiny output budget.
        output_buffer += CONTEXT_OVERFLOW_SAFETY_TOKENS
        stats = fit_messages(
            messages,
            UPSTREAM_CONTEXT_TOKENS,
            output_buffer,
            TRUNCATE_PROTECT_LAST_N,
        )
        if stats["tool_truncated"] or stats["messages_dropped"]:
            logger.warning(
                "Context truncation: chars %d -> %d (budget %d), "
                "tool results truncated=%d (-%d chars), messages dropped=%d "
                "(-%d chars)%s",
                stats["total_chars_before"],
                stats["total_chars_after"],
                stats["budget_chars"],
                stats["tool_truncated"],
                stats["tool_removed_chars"],
                stats["messages_dropped"],
                stats["dropped_chars"],
                " — STILL OVER BUDGET" if stats["still_over"] else "",
            )

    # Proactive context cap: estimate the prompt size (learned from the
    # previous successful response of this conversation, or chars/3 for a
    # fresh one) and shrink the output budget so the request cannot
    # overflow the context window. When the window is nearly full, inject a
    # pressure hint so the model switches to targeted reads and tells the
    # user about /compact; when even the minimum visible output does not
    # fit, send one small "last words" request so the model can ask the
    # user to /compact instead of the turn dying in a 400 retry storm.
    if (
        UPSTREAM_CONTEXT_TOKENS > 0
        and isinstance(p.get("max_tokens"), int)
        and not isinstance(p.get("max_tokens"), bool)
    ):
        msgs = p.get("messages")
        if not isinstance(msgs, list):
            msgs = []
        fp, char_prefix = _fingerprint_messages(msgs)
        est_prompt = _estimate_prompt_tokens(fp, char_prefix)
        if est_prompt is None:
            est_prompt = max(0, char_prefix[-1]) // _PROMPT_CHARS_PER_TOKEN
            logger.info(
                "No learned prompt size for this conversation; char-based "
                "fallback estimate: est_prompt=%d from %d chars",
                est_prompt,
                char_prefix[-1],
            )
        room = UPSTREAM_CONTEXT_TOKENS - est_prompt - CONTEXT_OVERFLOW_SAFETY_TOKENS
        if room < MIN_VISIBLE_OUTPUT_TOKENS:
            # The estimate says even the minimum visible output does not
            # fit. Ask the model for one short line asking the user to run
            # /compact; if this still 400s (estimate too optimistic), the
            # overflow fallback ends the turn immediately — no retry storm.
            p["max_tokens"] = max(LAST_WORDS_MIN_TOKENS, room)
            p["thinking_token_budget"] = 0
            p["reasoning_effort"] = "none"
            p.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
            p.pop("tools", None)
            msgs.append(_ctx_exhausted_hint())
            logger.warning(
                "Context exhausted by estimate: est_prompt=%d, room=%d — "
                "last-words attempt with max_tokens=%d. If it fails, run "
                "/compact or start a new session.",
                est_prompt,
                room,
                p["max_tokens"],
            )
        elif room < CONTEXT_PRESSURE_HINT_TOKENS:
            msgs.append(_ctx_pressure_hint(est_prompt, UPSTREAM_CONTEXT_TOKENS))
            est_prompt += _CTX_HINT_TOKENS
            room = UPSTREAM_CONTEXT_TOKENS - est_prompt - CONTEXT_OVERFLOW_SAFETY_TOKENS
            logger.warning(
                "Context pressure hint injected: est_prompt=%d, room=%d",
                est_prompt,
                room,
            )
        capped = max(MIN_VISIBLE_OUTPUT_TOKENS, room)
        if capped < p["max_tokens"]:
            logger.info(
                "Proactive context cap: est_prompt=%d, room=%d, "
                "max_tokens %d -> %d",
                est_prompt,
                room,
                p["max_tokens"],
                capped,
            )
            p["max_tokens"] = capped
            max_reasoning = max(0, capped - MIN_VISIBLE_OUTPUT_TOKENS)
            tb = p.get("thinking_token_budget")
            if (
                isinstance(tb, int)
                and not isinstance(tb, bool)
                and tb > max_reasoning
            ):
                p["thinking_token_budget"] = max_reasoning

    # Enable thinking by default, but respect an explicit false from variants
    # such as "none". vLLM can combine model reasoning_effort with its
    # independent sampler-level thinking_token_budget hard cap.
    if "chat_template_kwargs" not in p:
        p["chat_template_kwargs"] = {}
    p["chat_template_kwargs"].setdefault("enable_thinking", True)

    # Optional emergency limit. Disabled by default: tool schemas are part of
    # the model context and truncating them can break tool selection/arguments.
    if MAX_TOOL_DESC_LEN > 0:
        truncated_tools = 0
        for tool in p.get("tools", []):
            fn = tool.get("function", {})
            desc = fn.get("description", "")
            if isinstance(desc, str) and len(desc) > MAX_TOOL_DESC_LEN:
                fn["description"] = desc[:MAX_TOOL_DESC_LEN]
                truncated_tools += 1
        if truncated_tools:
            logger.warning(
                "Truncated %d tool description(s) to %d chars",
                truncated_tools,
                MAX_TOOL_DESC_LEN,
            )

    # Optional emergency limit. Disabled by default so large tool results and
    # source files are not silently cut before reaching the 128K model.
    if MAX_MESSAGE_CHARS > 0:
        truncated_messages = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str) and len(content) > MAX_MESSAGE_CHARS:
                msg["content"] = content[:MAX_MESSAGE_CHARS]
                truncated_messages += 1
        if truncated_messages:
            logger.warning(
                "Truncated %d message(s) to %d chars",
                truncated_messages,
                MAX_MESSAGE_CHARS,
            )

    # Inject a recovery user message right after a tool error so the
    # model is nudged to fix and continue instead of producing an empty
    # finish_reason=stop turn. Idempotent: no-op if the last message is
    # not a tool error or if a recovery message is already present.
    injected = _maybe_inject_recovery(messages)
    if injected:
        logger.info(
            "Injected [recovery] user message after tool error (messages=%d)",
            len(messages),
        )

    return p

# ---------------------------------------------------------------------------
# Streaming — raw pass-through of SSE bytes with auto-continuation
# ---------------------------------------------------------------------------
# Auto-continuation config
AUTO_CONTINUE_ENABLED = os.getenv("AUTO_CONTINUE_ENABLED", "true").lower() not in ("0", "false", "no")
AUTO_CONTINUE_MAX = int(os.getenv("AUTO_CONTINUE_MAX", "2"))  # hard cap per request
MAX_CONTEXT_REDUCTIONS = int(os.getenv("MAX_CONTEXT_REDUCTIONS", "8"))
# A dropped stream cannot be resumed losslessly when the upstream keeps
# reasoning in reasoning_content: that hidden state cannot be reconstructed as
# an assistant message. Leave disabled unless the upstream is known to expose
# resumable plain content.
AUTO_CONTINUE_ON_MIDSTREAM_DROP = (
    os.getenv("AUTO_CONTINUE_ON_MIDSTREAM_DROP", "false").lower()
    not in ("0", "false", "no")
)
AUTO_CONTINUE_USER_PROMPT = os.getenv(
    "AUTO_CONTINUE_USER_PROMPT",
    "Your previous response ended abruptly without continuing the task. "
    "Please continue exactly where you left off — pick up the original task, "
    "fix any issue from the latest tool result, and keep going until the "
    "task is fully done. Do not summarize what you have already done, just "
    "continue producing the next concrete step (next tool call, next code "
    "edit, or final answer if nothing more is required).",
)

_SSE_DONE_SENTINEL = b"data: [DONE]\n\n"
_SSE_LINE_SPLIT_RE = re.compile(rb"\r?\n")


def _sse_format(payload_obj, model_name):
    """Build a single SSE event from a dict in the same shape upstream
    uses, so continuation chunks look identical to the client."""
    return ("data: " + json.dumps(payload_obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def _build_assistant_message_from_content(content_text, tool_calls=None, finish_reason="stop"):
    """Construct an OpenAI-style assistant message dict suitable for
    being appended to `messages` before a continuation request."""
    msg = {"role": "assistant", "content": content_text or ""}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _looks_like_premature_stop(last_finish_reason, content_pieces, last_usage, prepared_messages):
    """Decide whether the just-finished stream ended in a way that
    looks like the model gave up (empty or near-empty assistant turn)
    and is therefore a candidate for auto-continuation.

    Heuristics (any one triggers a continuation request):
      * finish_reason == "stop" AND produced visible content is empty;
      * the final assistant turn contains an unclosed <think> block.
    """
    if not AUTO_CONTINUE_ENABLED:
        return False
    if last_finish_reason not in ("stop",):
        return False

    joined = "".join(content_pieces or []).strip()
    empty_content = not joined

    # Unclosed <think>: model emitted <think> but never closed it.
    think_open = joined.lower().count("<think")
    think_close = joined.lower().count("</think")
    unclosed_think = think_open > think_close

    # A short visible answer can be fully legitimate ("yes", a number, a
    # filename, etc.). Continue only an actually empty stop or malformed
    # unclosed inline thinking, never merely because token usage was low.
    return empty_content or unclosed_think


_CONTEXT_OVERFLOW_RE = re.compile(
    r"maximum context length is\s+(\d+)\s+tokens.*?"
    r"requested\s+(\d+)\s+output tokens.*?"
    r"prompt contains at least\s+(\d+)\s+input tokens",
    re.IGNORECASE | re.DOTALL,
)
_RETRY_WITH_ADJUSTED_BUDGET = object()
_CONTEXT_LIMIT_UNRECOVERABLE = object()
_RETRY_AFTER_IMAGE_STRIP = object()
_IMAGE_LIMIT_ERR_RE = re.compile(r"at most\s+\d+\s+image\(s\)\s+may be provided", re.IGNORECASE)


def _adjust_tokens_after_context_overflow(prepared_payload, error_text):
    """Halve the output/reasoning budgets after a vLLM overflow.

    vLLM reports that the prompt contains *at least* N tokens. That value is
    N = context - max_tokens + 1 — a tautology, not an exact prompt count —
    so solving `context - N` repeats the same one-token overflow forever.
    Halving converges in ~log2 steps and keeps the number of guaranteed-400
    requests (and the noise they add to the upstream server logs) minimal.
    The proactive cap in prepare_payload() normally prevents this path from
    running at all.
    """
    match = _CONTEXT_OVERFLOW_RE.search(error_text or "")
    if not match:
        return False

    context_tokens = int(match.group(1))
    requested_output = int(match.group(2))
    input_tokens_at_least = int(match.group(3))
    current_output = prepared_payload.get("max_tokens")
    if not isinstance(current_output, int) or isinstance(current_output, bool):
        current_output = requested_output
    if current_output <= MIN_VISIBLE_OUTPUT_TOKENS:
        return False

    reduction = max(
        CONTEXT_OVERFLOW_SAFETY_TOKENS,
        current_output // 2,
    )
    adjusted_output = max(
        MIN_VISIBLE_OUTPUT_TOKENS,
        current_output - reduction,
    )
    if adjusted_output >= current_output:
        return False

    prepared_payload["max_tokens"] = adjusted_output

    current_reasoning = prepared_payload.get("thinking_token_budget")
    adjusted_reasoning = current_reasoning
    max_reasoning = max(0, adjusted_output - MIN_VISIBLE_OUTPUT_TOKENS)
    if (
        isinstance(current_reasoning, int)
        and current_reasoning > max_reasoning
    ):
        adjusted_reasoning = max_reasoning
        prepared_payload["thinking_token_budget"] = adjusted_reasoning

    logger.warning(
        "Adjusted context-overflow request geometrically: context=%d, "
        "input_at_least=%d, max_tokens %d -> %d, "
        "thinking_token_budget %s -> %s",
        context_tokens,
        input_tokens_at_least,
        current_output,
        adjusted_output,
        current_reasoning,
        adjusted_reasoning,
    )
    return True


def _post_stream(prepared_payload):
    """Open a streaming POST to upstream and return the requests
    Response object (or None on failure). Does not consume the body —
    caller iterates resp.iter_content."""
    if not ensure_token():
        return None
    body = json.dumps(prepared_payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {current_token}",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    try:
        resp = requests.post(
            CORP_URL,
            headers=headers,
            data=body,
            verify=UPSTREAM_VERIFY_SSL,
            timeout=(UPSTREAM_CONNECT_TIMEOUT, UPSTREAM_READ_TIMEOUT),
            stream=True,
        )
    except requests.exceptions.RequestException as e:
        logger.error("Stream POST failed: %s", e)
        return None
    if resp.status_code == 401:
        logger.warning("Stream 401 — refreshing token")
        refresh_token()
        try:
            resp.close()
        except Exception:
            pass
        return None
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 5))
        logger.warning("Stream 429 — waiting %ds", wait)
        time.sleep(wait)
        try:
            resp.close()
        except Exception:
            pass
        return None
    if resp.status_code != 200:
        error_text = resp.text[:4000]
        image_limit_hit = (
            resp.status_code == 400
            and _IMAGE_LIMIT_ERR_RE.search(error_text) is not None
        )
        if image_limit_hit:
            removed = _enforce_image_limit(prepared_payload.get("messages", []), 0)
            if removed:
                logger.warning(
                    "Stream HTTP 400 image limit - stripped %d remaining "
                    "image(s), retrying without images",
                    removed,
                )
                try:
                    resp.close()
                except Exception:
                    pass
                return _RETRY_AFTER_IMAGE_STRIP
            logger.error(
                "Stream HTTP 400 image limit, but no image parts found to "
                "strip: %s",
                error_text[:500],
            )
        context_overflow = (
            resp.status_code == 400
            and _CONTEXT_OVERFLOW_RE.search(error_text) is not None
        )
        adjusted = (
            context_overflow
            and _adjust_tokens_after_context_overflow(
                prepared_payload,
                error_text,
            )
        )
        if adjusted:
            logger.warning(
                "Stream HTTP 400 context overflow — retrying with adjusted budgets"
            )
        else:
            logger.error("Stream HTTP %s: %s", resp.status_code, error_text[:1000])
        try:
            resp.close()
        except Exception:
            pass
        if adjusted:
            return _RETRY_WITH_ADJUSTED_BUDGET
        if context_overflow:
            return _CONTEXT_LIMIT_UNRECOVERABLE
        return None
    return resp


def _iter_sse_chunks(resp):
    """Generator that yields raw SSE chunks (bytes) from `resp`,
    parsing each complete SSE data line exactly once to extract diagnostic
    state. Raw bytes are still forwarded unchanged.
    """
    state = {
        "bytes_received": 0,
        "last_finish_reason": None,
        "last_usage": None,
        "content_pieces": [],
        "reasoning_chars": 0,
        "tool_calls_seen": [],   # [{'name': str|None, 'id': str|None}, ...]
    }

    def _gen():
        pending = b""
        tool_calls_by_key = {}

        def consume_line(raw_line):
            line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                return
            payload_str = line[5:].strip()
            if payload_str in ("", "[DONE]"):
                return
            try:
                obj = json.loads(payload_str)
            except (TypeError, ValueError):
                return

            choices = obj.get("choices") or []
            if choices:
                choice = choices[0]
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    state["content_pieces"].append(piece)

                # Qwen/OpenAI-compatible servers commonly stream hidden
                # thinking separately from visible content. Count it for
                # diagnostics without copying it into conversation history.
                for key in ("reasoning_content", "reasoning"):
                    reasoning_piece = delta.get(key)
                    if isinstance(reasoning_piece, str):
                        state["reasoning_chars"] += len(reasoning_piece)

                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    state["last_finish_reason"] = finish_reason

                for tc in (delta.get("tool_calls") or []):
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id") or None
                    tc_index = tc.get("index")
                    fn = tc.get("function") or {}
                    tc_name = fn.get("name") if isinstance(fn, dict) else None
                    # Index is stable across streamed fragments. Fall back to
                    # id; an id-less/index-less fragment belongs to the latest
                    # call, if one exists.
                    if tc_index is not None:
                        call_key = ("index", tc_index)
                    elif tc_id is not None:
                        call_key = ("id", tc_id)
                    elif state["tool_calls_seen"]:
                        entry = state["tool_calls_seen"][-1]
                        if tc_name and not entry.get("name"):
                            entry["name"] = tc_name
                        continue
                    else:
                        continue

                    entry = tool_calls_by_key.get(call_key)
                    if entry is None:
                        entry = {"id": tc_id, "name": tc_name}
                        tool_calls_by_key[call_key] = entry
                        state["tool_calls_seen"].append(entry)
                    else:
                        if tc_id and not entry.get("id"):
                            entry["id"] = tc_id
                        if tc_name and not entry.get("name"):
                            entry["name"] = tc_name

            usage = obj.get("usage")
            if usage:
                state["last_usage"] = usage

        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                state["bytes_received"] += len(chunk)
                pending += chunk
                while b"\n" in pending:
                    raw_line, pending = pending.split(b"\n", 1)
                    consume_line(raw_line)
                yield chunk
        finally:
            if pending:
                consume_line(pending)
            try:
                resp.close()
            except Exception:
                pass

    return _gen(), state


# Heuristic list of tool names that signal the model is trying to ask
# the user for input rather than do work. If the proxy sees such a tool
# call in a streamed response but the client never gets a chance to
# surface it (e.g. opencode didn't render the question UI for this tool,
# or the tool returned an error), the user perceives the model as
# "frozen mid-thought". Detecting this lets us log the real cause
# instead of guessing.
_USER_INPUT_TOOL_HINTS = (
    "ask_user", "askuser", "ask-user",
    "question", "questions", "user_question",
    "clarify", "clarification",
    "confirm", "confirmation",
    "request_input", "request-input",
    "ask_followup", "ask-followup", "followup_question",
    "human_input", "human-input",
    "user_input", "user-input",
)


def _tool_name_looks_like_user_input(name):
    if not isinstance(name, str):
        return False
    low = name.strip().lower()
    if not low:
        return False
    for hint in _USER_INPUT_TOOL_HINTS:
        if hint in low:
            return True
    return False


def _summarize_tool_calls(tool_calls_seen):
    """Build a compact, log-friendly summary of the tool calls the
    model emitted in this round, in the order they were streamed."""
    out = []
    for tc in (tool_calls_seen or []):
        if not isinstance(tc, dict):
            continue
        nm = tc.get("name") or "<unnamed>"
        out.append(nm)
    return out


def _estimate_input_tokens(messages, tools):
    """Char-based estimate of the prompt (input) token count for a round.
    Used only when the upstream usage block is unavailable (broken stream)
    but the request did reach upstream and its prompt was processed."""
    total_chars = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        _hash_text, est_text = _message_texts_for_fingerprint(m.get("content"))
        total_chars += len(est_text)
        if m.get("tool_calls"):
            total_chars += len(json.dumps(m["tool_calls"], default=str))
    if tools:
        total_chars += len(json.dumps(tools, default=str))
    return total_chars // _PROMPT_CHARS_PER_TOKEN


def _round_token_stats(state, round_payload):
    """Return (input_tokens, input_source, output_tokens, output_source) for
    a finished or broken round. Prefers the upstream usage block (exact).
    When the stream was dropped before the final usage chunk arrived (read
    timeout / connection drop — the "chain broke" case), or the API simply
    did not emit a usage block, falls back to char-based estimates so token
    data still reaches the log and the hourly counter. source is "usage",
    "estimate", or "none"."""
    usage = state.get("last_usage")
    usage = usage if isinstance(usage, dict) else {}

    prompt = usage.get("prompt_tokens")
    if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt > 0:
        in_tokens, in_source = prompt, "usage"
    else:
        est = _estimate_input_tokens(
            round_payload.get("messages"), round_payload.get("tools")
        )
        in_tokens, in_source = est, ("estimate" if est > 0 else "none")

    comp = usage.get("completion_tokens")
    if isinstance(comp, int) and not isinstance(comp, bool) and comp > 0:
        out_tokens, out_source = comp, "usage"
    else:
        content_chars = sum(len(p) for p in (state.get("content_pieces") or []))
        est_chars = content_chars + (state.get("reasoning_chars") or 0)
        if est_chars > 0:
            out_tokens, out_source = max(1, est_chars // _PROMPT_CHARS_PER_TOKEN), "estimate"
        else:
            out_tokens, out_source = 0, "none"

    return in_tokens, in_source, out_tokens, out_source


def stream_from_corp(payload, max_retries=5):
    """Connect to corp API, yield raw bytes. Retry on 401 with token
    refresh. If the model produces a near-empty `finish_reason=stop`
    turn after a tool error (or any other "gave up" pattern), the
    generator transparently issues up to AUTO_CONTINUE_MAX follow-up
    `continue` requests, merging their SSE bytes into the same stream
    and emitting an updated usage block at the end. The client sees
    a single, longer response instead of a premature stop.
    """
    # Fingerprint the *incoming* messages before prepare_payload mutates
    # them (think-strip, image cap, recovery injection): the client's next
    # request re-sends its own history, which only matches what the client
    # originally sent.
    incoming_fp, incoming_char_prefix = _fingerprint_messages(
        payload.get("messages", [])
    )
    prepared = prepare_payload(payload)
    logger.info(
        "Prepared generation options: reasoning_effort=%r, max_tokens=%r, "
        "max_completion_tokens=%r, reasoning_budget_tokens=%r, "
        "thinking_budget=%r, thinking_token_budget=%r, "
        "chat_template_kwargs=%r",
        prepared.get("reasoning_effort"),
        prepared.get("max_tokens"),
        prepared.get("max_completion_tokens"),
        prepared.get("reasoning_budget_tokens"),
        prepared.get("thinking_budget"),
        prepared.get("thinking_token_budget"),
        prepared.get("chat_template_kwargs"),
    )

    # Mutable working copy of the message history that we extend across
    # auto-continuations so the model has full context each round.
    working_messages = list(prepared.get("messages", []))
    aggregated_usage = None
    aggregated_finish_reason = None
    continuation_count = 0
    first_round = True

    # We yield from this top-level generator: it picks which inner
    # request to issue, drains its bytes, and decides whether to
    # continue. The first round uses the client-supplied payload;
    # subsequent rounds build a continuation payload that appends the
    # previous assistant turn (empty or near-empty) and a user
    # "continue" message.
    def _top_level_generator():
        nonlocal working_messages, aggregated_usage, aggregated_finish_reason
        nonlocal continuation_count, first_round
        zero_byte_retries = 0

        while True:
            # Build the request payload for this round.
            round_payload = dict(prepared)
            round_payload["messages"] = working_messages

            round_label = "initial" if first_round else f"continue #{continuation_count}"
            logger.info(
                "Stream round %s: %d messages, tools=%s",
                round_label,
                len(working_messages),
                "YES" if round_payload.get("tools") else "NO",
            )

            # Issue streaming request with retry on 401/429/network.
            resp = None
            network_failures = 0
            context_reductions = 0
            context_exhausted = False
            image_strips = 0
            while network_failures < max_retries:
                resp = _post_stream(round_payload)
                if resp is _RETRY_AFTER_IMAGE_STRIP:
                    image_strips += 1
                    if image_strips > 1:
                        logger.error(
                            "Stream round %s: still hitting the upstream "
                            "image limit after stripping all image parts - "
                            "aborting.",
                            round_label,
                        )
                        resp = None
                        context_exhausted = True
                        break
                    logger.warning(
                        "Stream round %s: upstream image limit hit - "
                        "retrying with all image parts stripped",
                        round_label,
                    )
                    resp = None
                    continue
                if resp is _RETRY_WITH_ADJUSTED_BUDGET:
                    context_reductions += 1
                    if context_reductions > MAX_CONTEXT_REDUCTIONS:
                        logger.error(
                            "Stream round %s: context still over limit after "
                            "%d budget reductions. Run /compact or start a "
                            "new session.",
                            round_label,
                            MAX_CONTEXT_REDUCTIONS,
                        )
                        resp = None
                        context_exhausted = True
                        break
                    logger.warning(
                        "Stream round %s context reduction %d/%d; "
                        "retrying immediately with reduced budgets",
                        round_label,
                        context_reductions,
                        MAX_CONTEXT_REDUCTIONS,
                    )
                    resp = None
                    continue
                if resp is _CONTEXT_LIMIT_UNRECOVERABLE:
                    logger.error(
                        "Stream round %s: context cannot fit even the minimum "
                        "output budget. Run /compact or start a new session.",
                        round_label,
                    )
                    resp = None
                    context_exhausted = True
                    break
                if resp is not None:
                    break
                network_failures += 1
                logger.warning(
                    "Stream round %s network/auth attempt %d/%d failed, retrying...",
                    round_label, network_failures, max_retries,
                )
                if network_failures < max_retries:
                    time.sleep(min(2 ** network_failures, 10))

            if resp is None:
                if not context_exhausted:
                    logger.error(
                        "Stream round %s: all %d network/auth attempts failed",
                        round_label,
                        max_retries,
                    )
                return

            inner, state = _iter_sse_chunks(resp)
            # Flag for recoverable mid-stream drops (upstream closed the
            # connection or the read timeout fired *after* some bytes had
            # already been streamed). Unlike a hard failure with zero
            # bytes, these cases still have partial model output worth
            # feeding into an auto-continuation round, so we set the flag
            # here and let the premature-stop branch below pick it up.
            mid_stream_drop = False
            drop_kind = None
            try:
                for chunk in inner:
                    yield chunk
            except requests.exceptions.ReadTimeout:
                logger.error(
                    "Нет ответа от сервера в течение %s секунд — поток разорван. "
                    "Получено %d байт.",
                    UPSTREAM_READ_TIMEOUT if UPSTREAM_READ_TIMEOUT is not None else "неограниченного времени",
                    state["bytes_received"],
                )
                mid_stream_drop = True
                drop_kind = "read_timeout"
            except requests.exceptions.ChunkedEncodingError as e:
                logger.error(
                    "Upstream closed connection prematurely (ChunkedEncodingError). "
                    "Получено %d байт: %s",
                    state["bytes_received"], e,
                )
                mid_stream_drop = True
                drop_kind = "chunked_encoding"
            except requests.exceptions.ConnectionError as e:
                if "Read timed out" in str(e):
                    logger.error(
                        "Нет ответа от сервера в течение %s секунд — поток разорван. "
                        "Получено %d байт.",
                        UPSTREAM_READ_TIMEOUT if UPSTREAM_READ_TIMEOUT is not None else "неограниченного времени",
                        state["bytes_received"],
                    )
                    mid_stream_drop = True
                    drop_kind = "read_timeout"
                else:
                    logger.error("Stream connection error: %s", e)
                    return
            except GeneratorExit:
                logger.debug(
                    "Stream cancelled by client — %d bytes received (round %s)",
                    state["bytes_received"], round_label,
                )
                return

            # Update aggregated metadata.
            aggregated_finish_reason = state["last_finish_reason"] or aggregated_finish_reason
            if isinstance(state["last_usage"], dict):
                if aggregated_usage is None:
                    aggregated_usage = dict(state["last_usage"])
                else:
                    for k, v in state["last_usage"].items():
                        if isinstance(v, int) and isinstance(aggregated_usage.get(k), int):
                            aggregated_usage[k] = aggregated_usage[k] + v
                        else:
                            aggregated_usage[k] = v

            # Learn the exact prompt size of this conversation from the
            # first round's usage; the proactive cap uses it for the next
            # request of the same conversation.
            if first_round and isinstance(state["last_usage"], dict):
                _record_prompt_usage(
                    incoming_fp, incoming_char_prefix, state["last_usage"]
                )

            content_joined = "".join(state["content_pieces"] or []).strip()
            tool_call_names = _summarize_tool_calls(state["tool_calls_seen"])
            in_tokens, in_source, out_tokens, out_source = _round_token_stats(
                state, round_payload
            )
            round_status = (
                f"broken({drop_kind})" if mid_stream_drop else "complete"
            )
            logger.info(
                "Stream round %s %s: %d bytes, finish_reason=%s, "
                "input_tokens=%s[%s], output_tokens=%s[%s], content_len=%d, "
                "reasoning_len=%d, tool_calls=%s, content_tail=%r",
                round_label,
                round_status,
                state["bytes_received"],
                state["last_finish_reason"],
                in_tokens,
                in_source,
                out_tokens,
                out_source,
                len(content_joined),
                state["reasoning_chars"],
                tool_call_names,
                content_joined[-200:],
            )

            # Token-quota accounting: count this round's input + output
            # tokens into the rolling 1h window and report the running total.
            # The corporate limit is the SUM of input and output tokens. Use
            # the upstream usage block when present (exact); when the stream
            # was dropped before the final usage chunk arrived, use
            # char-based estimates so broken rounds still leave token data in
            # the log and keep the hourly counter honest — this is what makes
            # the break point and the start of the cooldown visible.
            round_total = in_tokens + out_tokens
            if round_total > 0:
                stats = TOKEN_TRACKER.record(in_tokens, out_tokens)
                level = logging.INFO
                quota_part = ""
                src_parts = []
                if in_source != "usage":
                    src_parts.append(f"in={in_source}")
                if out_source != "usage":
                    src_parts.append(f"out={out_source}")
                src_note = (
                    " (" + ", ".join(src_parts) + ")" if src_parts else ""
                )
                if HOURLY_TOKEN_QUOTA > 0:
                    used_pct = 100.0 * stats["window_total"] / HOURLY_TOKEN_QUOTA
                    left = HOURLY_TOKEN_QUOTA - stats["window_total"]
                    if used_pct >= 100.0:
                        level = logging.ERROR
                        quota_part = (
                            f" | КВОТА ПЕРЕВЫШЕНА на {_fmt_tokens(-left)}"
                            f" (лимит {_fmt_tokens(HOURLY_TOKEN_QUOTA)}/час)"
                        )
                    else:
                        if used_pct >= 80.0:
                            level = logging.WARNING
                        quota_part = (
                            f" | квота {_fmt_tokens(HOURLY_TOKEN_QUOTA)}: "
                            f"использовано {used_pct:.0f}%, остаток {_fmt_tokens(left)}"
                        )
                logger.log(
                    level,
                    "[QUOTA] за %s: %s токенов = in %s + out %s | "
                    "этот раунд: in +%s, out +%s, итого +%s%s | "
                    "за сессию: %s%s",
                    _window_label(),
                    _fmt_tokens(stats["window_total"]),
                    _fmt_tokens(stats["window_input"]),
                    _fmt_tokens(stats["window_output"]),
                    _fmt_tokens(in_tokens),
                    _fmt_tokens(out_tokens),
                    _fmt_tokens(round_total),
                    src_note,
                    _fmt_tokens(stats["session_total"]),
                    quota_part,
                )
                _quota_toast_maybe(used_pct, stats["window_total"], left)
                if stats.get("extra_windows"):
                    extra_parts = [
                        f"{w['hours']}ч = {_fmt_tokens(w['total'])} "
                        f"(in {_fmt_tokens(w['input'])} + out {_fmt_tokens(w['output'])})"
                        for w in stats["extra_windows"]
                    ]
                    logger.log(
                        level,
                        "[QUOTA] накоплено: %s",
                        " | ".join(extra_parts),
                    )
            else:
                logger.info(
                    "[QUOTA] раунд %s: токены не учтены (нет usage и нет "
                    "полученного контента)%s",
                    round_label,
                    " — поток оборван" if mid_stream_drop else "",
                )

            if state["last_finish_reason"] == "length":
                logger.warning(
                    "UPSTREAM OUTPUT LIMIT reached: completion_tokens=%s, "
                    "requested max_tokens=%r, requested max_completion_tokens=%r. "
                    "The proxy did not truncate this response.",
                    (state["last_usage"] or {}).get("completion_tokens")
                    if isinstance(state["last_usage"], dict)
                    else None,
                    round_payload.get("max_tokens"),
                    round_payload.get("max_completion_tokens"),
                )

            # Diagnose: if the model tried to invoke a user-input tool
            # in this round and the client may not have surfaced it,
            # emit a loud warning so future "freezes" are explainable
            # from the log.
            user_input_tools_called = [
                name for name in tool_call_names
                if _tool_name_looks_like_user_input(name)
            ]
            if user_input_tools_called:
                logger.warning(
                    "[WARN] model tried to invoke user-input tool %s "
                    "(round %s) — if the client never surfaced it, the "
                    "model will appear frozen. full tool_calls_seen=%s",
                    user_input_tools_called,
                    round_label,
                    state["tool_calls_seen"],
                )

            # Decide whether to auto-continue. A mid-stream upstream drop
            # (ChunkedEncodingError / read timeout) only counts as
            # "premature" when we actually received *some* bytes. A drop
            # with zero bytes has no partial state, but the round itself
            # can be re-issued losslessly — bounded retry with backoff
            # before giving up.
            if mid_stream_drop and state["bytes_received"] <= 0:
                if zero_byte_retries < ZERO_BYTE_DROP_MAX_RETRIES:
                    zero_byte_retries += 1
                    backoff = 15 * zero_byte_retries
                    logger.warning(
                        "Stream round %s: upstream dropped before sending any "
                        "data (kind=%s) — nothing to continue from, retrying "
                        "the same round %d/%d in %ds",
                        round_label, drop_kind, zero_byte_retries,
                        ZERO_BYTE_DROP_MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    continue
                logger.error(
                    "Stream round %s: upstream dropped before sending any "
                    "data (kind=%s) — retry budget exhausted, returning",
                    round_label, drop_kind,
                )
                return
            if mid_stream_drop and not AUTO_CONTINUE_ON_MIDSTREAM_DROP:
                logger.error(
                    "Stream round %s: upstream dropped mid-stream (kind=%s). "
                    "Lossless auto-continuation is disabled because hidden "
                    "reasoning state cannot be reconstructed.",
                    round_label,
                    drop_kind,
                )
                return
            premature = (
                _looks_like_premature_stop(
                    state["last_finish_reason"],
                    state["content_pieces"],
                    state["last_usage"],
                    working_messages,
                )
                or mid_stream_drop
            )
            if mid_stream_drop and premature:
                logger.warning(
                    "Stream round %s: mid-stream upstream drop "
                    "(kind=%s) with %d bytes / %d chars — auto-continuing",
                    round_label, drop_kind,
                    state["bytes_received"], len(content_joined),
                )

            # First round: log the diagnostic warning the old code
            # used to emit, so log scrapers keep working.
            if (
                first_round
                and state["last_finish_reason"] == "stop"
                and isinstance((state["last_usage"] or {}).get("completion_tokens"), int)
                and (state["last_usage"] or {}).get("completion_tokens", 0) <= 5
            ):
                history_preview = []
                for m in working_messages[-3:]:
                    c = m.get("content")
                    c_preview = (c if isinstance(c, str) else str(c))[:200]
                    history_preview.append({
                        "role": m.get("role"),
                        "content_preview": c_preview,
                        "has_tool_calls": bool(m.get("tool_calls")),
                    })
                logger.warning(
                    "POSSIBLE PREMATURE STOP: finish_reason=stop with only "
                    "%s completion_tokens. Model output: %r. "
                    "Last 3 request messages: %s",
                    (state["last_usage"] or {}).get("completion_tokens"),
                    content_joined,
                    history_preview,
                )

            if not premature:
                # Healthy end-of-turn — done.
                return

            if continuation_count >= AUTO_CONTINUE_MAX:
                logger.warning(
                    "Auto-continue: reached cap (%d) — returning stream as-is. "
                    "content_tail=%r",
                    AUTO_CONTINUE_MAX, content_joined[-200:],
                )
                return

            # Build the next round: append the empty assistant turn,
            # then a user "continue" message.
            continuation_count += 1
            first_round = False
            working_messages.append(
                _build_assistant_message_from_content(content_joined)
            )
            working_messages.append(
                {"role": "user", "content": AUTO_CONTINUE_USER_PROMPT}
            )
            logger.info(
                "Auto-continue #%d: sending follow-up 'continue' request "
                "(history=%d messages)",
                continuation_count, len(working_messages),
            )
            # Loop into the next round and stream its bytes directly
            # into the same output to the client.
            # (The for-loop above will yield those chunks next.)

    return _top_level_generator()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
    return response


@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "", 204

    try:
        data = request.get_json(force=True)
    except Exception as e:
        logger.error("Invalid JSON: %s", e)
        return jsonify({"error": "Invalid JSON"}), 400

    data["model"] = CORP_MODEL

    logger.info("=" * 60)
    img_count = 0
    for _m in data.get("messages", []):
        _c = _m.get("content")
        if isinstance(_c, list):
            for _part in _c:
                if _part_image_url(_part) is not None:
                    img_count += 1
    logger.info("Request: %d messages, %d image(s), tools=%s",
                 len(data.get("messages", [])),
                 img_count,
                 "YES" if "tools" in data else "NO")
    logger.info("=" * 60)

    gen = stream_from_corp(data)
    if gen is None:
        return jsonify({"error": "Upstream stream failed"}), 502

    gen = security_stream_wrapper(gen)

    return Response(
        stream_with_context(gen),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Proxy": "think-streaming-lite",
        },
    )


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def models():
    if request.method == "OPTIONS":
        return "", 204
    return jsonify({
        "data": [{
            "id": CORP_MODEL,
            "object": "model",
            "created": 1700000000,
            "owned_by": "openai",
        }]
    })


@app.route("/v1/models/<model_id>", methods=["GET", "OPTIONS"])
def model_detail(model_id):
    if request.method == "OPTIONS":
        return "", 204
    return jsonify({
        "id": model_id,
        "object": "model",
        "created": 1700000000,
        "owned_by": "openai",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "auth": "AVAILABLE" if current_token else "NONE",
        "upstream": CORP_URL,
        "timeouts": (
            f"connect={UPSTREAM_CONNECT_TIMEOUT}s, "
            f"read={UPSTREAM_READ_TIMEOUT if UPSTREAM_READ_TIMEOUT is not None else 'disabled'}"
        ),
    })


_BACKUP_EVENTS_CACHE = {"key": None, "events": []}


def _parse_quota_events(path, min_ts, max_ts=None):
    """Yield (ts, in, out) rounds from [QUOTA] log lines in one file."""
    if max_ts is None:
        max_ts = time.time() + 60
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "[QUOTA]" not in line:
                    continue
                plain = _ANSI_RE.sub("", line)
                if "накоплено" in plain or "не учтены" in plain:
                    continue
                m = _QUOTA_TS_RE.match(plain)
                if not m:
                    continue
                try:
                    ts = time.mktime(
                        time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    )
                except (ValueError, OverflowError):
                    continue
                if ts < min_ts or ts > max_ts:
                    continue
                mi = _QUOTA_ROUND_IN_OUT_RE.search(plain)
                if mi:
                    i_tok = int(mi.group(1).replace(" ", ""))
                    o_tok = int(mi.group(2).replace(" ", ""))
                else:
                    mo = _QUOTA_ROUND_OUT_ONLY_RE.search(plain)
                    if not mo:
                        continue
                    i_tok = 0
                    o_tok = int(mo.group(1).replace(" ", ""))
                if i_tok <= 0 and o_tok <= 0:
                    continue
                yield (ts, i_tok, o_tok)
    except OSError:
        return


def _backup_log_events():
    """Parsed [QUOTA] events from LOG_FILE.1 (immutable after rotation),
    cached per process, keyed on (mtime, size)."""
    path = LOG_FILE + ".1"
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    if _BACKUP_EVENTS_CACHE["key"] != key:
        floor = time.time() - TOKEN_TRACKER._retention - 3600
        _BACKUP_EVENTS_CACHE["key"] = key
        _BACKUP_EVENTS_CACHE["events"] = list(_parse_quota_events(path, floor))
    return _BACKUP_EVENTS_CACHE["events"]


def _quota_history_events(lookback_seconds=None):
    """All (ts, in, out) rounds of the last N hours, no duplicates:
    in-memory events (rolling retention, authoritative for the recent part)
    + log events older than the retention (current log + .log.1 backup)."""
    lookback = (
        TOKEN_STATE_LOOKBACK_SECONDS
        if lookback_seconds is None
        else int(lookback_seconds)
    )
    now = time.time()
    cutoff = now - max(1, lookback)
    memory_floor = now - TOKEN_TRACKER._retention
    with TOKEN_TRACKER._lock:
        events = [e for e in TOKEN_TRACKER._events if e[0] >= cutoff]
    for ev in _backup_log_events():
        if cutoff <= ev[0] < memory_floor:
            events.append(ev)
    for ev in _parse_quota_events(LOG_FILE, cutoff, memory_floor):
        events.append(ev)
    events.sort(key=lambda e: e[0])
    return events


@app.route("/quota/stats", methods=["GET"])
def quota_stats():
    """Detailed quota stats (JSON) for the dashboard: 24h hourly buckets,
    sessions, top rounds, burn rates and a simple burn-rate forecast."""
    now = time.time()
    events = _quota_history_events()
    stats = TOKEN_TRACKER.snapshot()
    quota = HOURLY_TOKEN_QUOTA
    used = stats["window_total"]
    remaining = max(0, quota - used) if quota > 0 else None

    total_in = sum(e[1] for e in events)
    total_out = sum(e[2] for e in events)

    hour_floor = (int(now) // 3600) * 3600
    agg = {}
    for ts, i_tok, o_tok in events:
        b = (int(ts) // 3600) * 3600
        a = agg.setdefault(b, [0, 0])
        a[0] += i_tok
        a[1] += o_tok
    hourly = []
    for k in range(23, -1, -1):
        start = hour_floor - k * 3600
        wi, wo = agg.get(start, (0, 0))
        hourly.append({"ts": start, "in": wi, "out": wo, "total": wi + wo})

    gap = TOKEN_SESSION_IDLE_GAP_SECONDS
    sessions = []
    cur = None
    prev_ts = None
    for ts, i_tok, o_tok in events:
        if cur is None or ts - prev_ts > gap:
            if cur is not None:
                sessions.append(cur)
            cur = {"start": ts, "end": ts, "rounds": 1,
                   "in": i_tok, "out": o_tok}
        else:
            cur["end"] = ts
            cur["rounds"] += 1
            cur["in"] += i_tok
            cur["out"] += o_tok
        prev_ts = ts
    if cur is not None:
        sessions.append(cur)
    for s in sessions:
        s["total"] = s["in"] + s["out"]
        s["start"] = int(s["start"])
        s["end"] = int(s["end"])
    sessions = sessions[-12:]

    top_rounds = [
        {"ts": int(t), "in": i, "out": o, "total": i + o}
        for (t, i, o) in sorted(events, key=lambda e: e[1] + e[2],
                                reverse=True)[:10]
    ]

    def _sum_since(sec):
        c = now - sec
        return sum(i + o for (ts, i, o) in events if ts >= c)

    rate_1h = _sum_since(3600)
    rate_3h = _sum_since(3 * 3600) / 3.0
    rate_24h = (total_in + total_out) / 24.0

    window_from = now - stats["window_seconds"]
    oldest_in_window = None
    for ts, _i, _o in events:
        if ts >= window_from:
            oldest_in_window = ts
            break
    window_reset_ts = (
        int(oldest_in_window + stats["window_seconds"])
        if oldest_in_window is not None else None
    )

    eta_ts = None
    if quota > 0:
        if used >= quota:
            eta_ts = int(now)
        elif rate_1h > 0 and remaining:
            eta_ts = int(now + (remaining / rate_1h) * 3600)
    projected_at_reset = None
    if window_reset_ts and quota > 0 and rate_1h > 0:
        dt_h = (window_reset_ts - now) / 3600.0
        if dt_h > 0:
            projected_at_reset = min(quota, int(used + rate_1h * dt_h))

    # Release schedule: a round leaves the rolling window exactly
    # window_seconds after it was recorded, so each in-window event maps to
    # a concrete future release time. No extrapolation involved.
    W = stats["window_seconds"]
    releases = []
    for ts, i_tok, o_tok in events:
        if ts >= window_from:
            releases.append((ts + W, i_tok + o_tok))
    releases.sort(key=lambda r: r[0])
    total_releasing = sum(r[1] for r in releases)

    bucket_s = 1800
    n_buckets = 12
    release_buckets = []
    eta_release_ts = None
    if quota > 0:
        idx = 0
        cum = 0
        for k in range(n_buckets):
            t1 = now + (k + 1) * bucket_s
            rel = 0
            while idx < len(releases) and releases[idx][0] < t1:
                rel += releases[idx][1]
                idx += 1
            cum += rel
            free_after = min(quota, remaining + cum)
            free_proj = max(0, free_after - rate_1h * ((t1 - now) / 3600.0))
            release_buckets.append({
                "ts": int(now + k * bucket_s),
                "released": int(rel),
                "free_after": int(free_after),
                "free_projected": int(free_proj),
            })
        if used >= quota:
            eta_release_ts = int(now)
        elif rate_1h > 0:
            idx2 = 0
            cum2 = 0
            t = now + 300
            while t <= now + 6 * 3600:
                while idx2 < len(releases) and releases[idx2][0] <= t:
                    cum2 += releases[idx2][1]
                    idx2 += 1
                if remaining + cum2 - rate_1h * ((t - now) / 3600.0) <= 0:
                    eta_release_ts = int(t)
                    break
                t += 300

    resp = jsonify({
        "ok": True,
        "ts": int(now),
        "window": {
            "label": _window_label(),
            "seconds": stats["window_seconds"],
            "quota": quota if quota > 0 else None,
            "used": used,
            "input": stats["window_input"],
            "output": stats["window_output"],
            "remaining": remaining,
            "pct": round(100.0 * used / quota, 1) if quota > 0 else None,
            "reset_ts": window_reset_ts,
        },
        "session": {
            "total": stats["session_total"],
            "input": stats["session_input"],
            "output": stats["session_output"],
        },
        "total_24h": {
            "in": total_in, "out": total_out,
            "total": total_in + total_out, "rounds": len(events),
        },
        "hourly": hourly,
        "sessions": sessions,
        "top_rounds": top_rounds,
        "burn": {
            "rate_1h": int(rate_1h),
            "rate_3h": int(rate_3h),
            "rate_24h": int(rate_24h),
        },
        "forecast": {
            "eta_ts": eta_ts,
            "projected_at_reset": projected_at_reset,
        },
        "release": {
            "bucket_seconds": bucket_s,
            "buckets": release_buckets,
            "total_releasing": int(total_releasing),
            "eta_ts": eta_release_ts,
        },
        "extra_windows": stats["extra_windows"],
    })
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/quota/dash", methods=["GET"])
def quota_dash():
    """Single-file quota dashboard (HTML), same origin as /quota/stats."""
    path = os.path.join(BASE_DIR, "quota_dashboard.html")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
    except OSError as exc:
        return jsonify({"error": "dashboard file missing: %s" % exc}), 500
    resp = app.response_class(html, mimetype="text/html")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/quota", methods=["GET"])
def quota_status():
    """Read-only quota state (JSON) for external widgets."""
    stats = TOKEN_TRACKER.snapshot()
    quota = HOURLY_TOKEN_QUOTA
    used = stats["window_total"]
    resp = jsonify({
        "ok": True,
        "ts": int(time.time()),
        "window_label": _window_label(),
        "window_seconds": stats["window_seconds"],
        "quota": quota if quota > 0 else None,
        "window_total": used,
        "window_input": stats["window_input"],
        "window_output": stats["window_output"],
        "remaining": max(0, quota - used) if quota > 0 else None,
        "used_pct": round(100.0 * used / quota, 1) if quota > 0 else None,
        "session_total": stats["session_total"],
        "extra_windows": stats["extra_windows"],
    })
    # CORS for the OpenCode Desktop quota pill (cross-origin from oc2://).
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.errorhandler(Exception)
def on_error(e):
    logger.error("Unhandled: %s\n%s", e, traceback.format_exc())
    return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Think Streaming LITE — raw SSE pass-through proxy")
    logger.info("Target: %s", CORP_URL)
    logger.info(
        "Timeouts: connect=%ss, read=%s",
        UPSTREAM_CONNECT_TIMEOUT,
        f"{UPSTREAM_READ_TIMEOUT}s" if UPSTREAM_READ_TIMEOUT is not None else "DISABLED",
    )
    logger.info(
        "Payload limits: messages=%s, tool descriptions=%s",
        MAX_MESSAGE_CHARS or "DISABLED",
        MAX_TOOL_DESC_LEN or "DISABLED",
    )
    logger.info(
        "Image cap: %s image(s) per prompt (oldest images replaced with placeholders)",
        MAX_IMAGES_IN_PROMPT if MAX_IMAGES_IN_PROMPT > 0 else "DISABLED",
    )
    logger.info(
        "Upstream max_tokens override: %s",
        UPSTREAM_MAX_TOKENS or "DISABLED",
    )
    logger.info(
        "Proactive context cap: context=%d, growth_safety=%d tokens",
        UPSTREAM_CONTEXT_TOKENS,
        PROMPT_GROWTH_SAFETY_TOKENS,
    )
    logger.info(
        "Context truncation: %s (protect_last=%d, tool_floor=%d tokens, "
        "chars/token=%.1f, output_buffer=%d tokens)",
        "ENABLED" if CONTEXT_TRUNCATE_ENABLED else "DISABLED",
        TRUNCATE_PROTECT_LAST_N,
        TRUNCATE_TOOL_MIN_KEEP_TOKENS,
        TRUNCATE_CHARS_PER_TOKEN,
        TRUNCATE_OUTPUT_BUFFER_TOKENS,
    )
    logger.info(
        "Token quota counter: window=%s, квота=%s (in+out суммарно), "
        "доп. окна: %s",
        _window_label(),
        _fmt_tokens(HOURLY_TOKEN_QUOTA) if HOURLY_TOKEN_QUOTA > 0 else "DISABLED (только подсчёт)",
        ", ".join(f"{h}ч" for h in TOKEN_EXTRA_WINDOWS_HOURS) or "нет",
    )
    if QUOTA_STATE_RESTORE:
        _log_quota_restore(_restore_quota_with_timeout())
    logger.info("Watchdog: DISABLED (removed 040726)")
    logger.info("Mid-stream drop continuation: ENABLED (030726)")
    logger.info("Health: http://%s:%d/health", PROXY_HOST, PROXY_PORT)
    logger.info("=" * 60)

    if not refresh_token():
        logger.error("INITIAL AUTH FAILED — check .env")
    else:
        logger.info("Initial auth OK")

    app.run(host=PROXY_HOST, port=PROXY_PORT, debug=PROXY_DEBUG, threaded=True)
