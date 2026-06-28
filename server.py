"""
voice-bridge: Tailscale-only FastAPI server.
Receives spoken text from iOS Shortcut → Claude CLI → returns reply text.
Uses the installed claude CLI (no API key needed — rides Claude Code subscription).

Long tasks (e.g. "prepare the next tokn-watch article") auto-promote to a
background job: the /ask request returns a short spoken ack within SOFT_DEADLINE
seconds, the job keeps running detached, and when it finishes the result is
pushed to the phone (Pushcut → a Shortcut speaks it; Telegram as fallback).
"""

import asyncio
import json
import os
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel


def _read_env_key(key: str, env_file: Optional[Path] = None) -> str:
    """Read a single key from a .env file without polluting os.environ."""
    env_file = env_file or (Path(__file__).parent / ".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line[len(key) + 1:].strip().strip('"').strip("'")
    return ""

HISTORY_FILE = Path(__file__).parent / "history.json"   # default (no-project) history
HISTORY_DIR = Path(__file__).parent / "history"          # per-project history files (SPI-255)
RESULTS_FILE = Path(__file__).parent / "results_queue.json"
ACTIVITY_FILE = Path(__file__).parent / "activity.jsonl"
MAX_HISTORY_TURNS = 10  # keep last N exchanges in context

# Project scoping (SPI-255). A client may send a `project` path; the agent then
# runs with that folder as its cwd (so the CLI auto-loads the project's CLAUDE.md
# and file tools default there) and keeps a per-project conversation history. The
# path is constrained to PROJECTS_ROOT so a typo/hostile value can't escape it.
PROJECTS_ROOT = Path(
    os.environ.get("PROJECTS_ROOT") or _read_env_key("PROJECTS_ROOT")
    or str(Path.home() / "Documents" / "Projects")
).expanduser()
# Context files we surface to the agent if present (CLAUDE.md is auto-loaded by the
# CLI via cwd; the rest are named so the agent reads whatever the project provides).
CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md", "README.md", "README", ".cursorrules")

# A normal spoken Q&A finishes fast. If the claude job hasn't returned within
# SOFT_DEADLINE, we stop waiting on the HTTP request (so the phone is freed and
# the iOS Shortcut doesn't itself time out) and let the job finish in the
# background, notifying the phone on completion. HARD_CAP bounds a runaway job.
SOFT_DEADLINE = 25      # seconds to wait before promoting to a background job
HARD_CAP = 900          # 15 min absolute ceiling for a background job
# Per-line buffer for the claude CLI's stream-json stdout. The asyncio default is
# 64 KiB, which a single event line (a big tool result / file read) easily exceeds —
# the overflow raised "Separator is found, but chunk is longer than limit" and errored
# the whole task. 16 MiB gives generous headroom for large tool output.
STREAM_LIMIT = 1024 * 1024 * 16

# Spoken requests that *start with* one of these phrases are forced down the
# delayed/background path no matter how fast the answer is — so the delayed
# pipeline (ack now → doorbell + /result/next later) can be tested on demand.
# The trigger phrase is stripped; whatever follows becomes the actual question.
FORCE_BG_TRIGGERS = (
    "test delay",
    "delay test",
    "delayed test",
    "test the delayed answer",
)

API_KEY = os.environ.get("API_KEY") or _read_env_key("API_KEY")
LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY") or _read_env_key("LINEAR_API_KEY")

# Completion notification for the (b) "speak it" path.
# Pushcut's *dynamic content* (per-call text/input) is a paid feature, so we don't
# rely on it: the server rings a STATIC Pushcut notification (free tier) purely as
# a doorbell, and the Shortcut it launches fetches the actual text from /result/next
# here and speaks that. If Pushcut isn't configured, fall back to a Telegram push
# carrying the full text (free, read-not-spoken).
PUSHCUT_API_KEY = os.environ.get("PUSHCUT_API_KEY") or _read_env_key("PUSHCUT_API_KEY")
PUSHCUT_NOTIFICATION = (
    os.environ.get("PUSHCUT_NOTIFICATION")
    or _read_env_key("PUSHCUT_NOTIFICATION")
    or "voice-bridge-done"
)
# Telegram fallback: reaches the phone as a push you tap (read, not auto-spoken).
_TG_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"
TELEGRAM_BOT_TOKEN = _read_env_key("TELEGRAM_BOT_TOKEN", _TG_ENV)
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID") or _read_env_key("OWNER_CHAT_ID") or "6917470053"

# APNs (token-based push) — silently wakes the GoSancho app to fetch /v1/results.
# Entirely optional: if the key/creds or the apns module are missing, APNs is
# disabled and delivery falls back to the Pushcut/Telegram doorbell exactly as before.
APNS_KEY_PATH = os.environ.get("APNS_KEY_PATH") or _read_env_key("APNS_KEY_PATH")
APNS_KEY_ID = os.environ.get("APNS_KEY_ID") or _read_env_key("APNS_KEY_ID")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID") or _read_env_key("APNS_TEAM_ID")
APNS_TOPIC = os.environ.get("APNS_TOPIC") or _read_env_key("APNS_TOPIC")
DEVICES_FILE = Path(__file__).parent / "devices.json"
try:
    import apns as _apns
    _APNS_OK = True
except Exception as _exc:  # missing deps must never take the server down
    _apns, _APNS_OK = None, False
    print(f"[voice-bridge] APNs disabled (import failed: {_exc})", flush=True)
APNS_ENABLED = bool(_APNS_OK and APNS_KEY_PATH and APNS_KEY_ID and APNS_TEAM_ID
                    and APNS_TOPIC and Path(APNS_KEY_PATH).exists())

# --- Confirm gate (CONFIRM_GATE.md) — OPT-IN, default OFF -------------------
# When enabled, run_claude wires a PreToolUse hook that pauses MUTATING tools
# (Write/Edit/mutating Bash) until the owner approves on the phone, with a hard
# timeout → DENY. Read-only tools never interrupt. Off ⇒ the hook is never
# wired and the live assistant behaves exactly as before. The client confirm UI
# (GoSancho screen 09) reads /v1/approvals and posts the decision.
CONFIRM_GATE_ENABLED = (os.environ.get("CONFIRM_GATE_ENABLED")
                        or _read_env_key("CONFIRM_GATE_ENABLED") or "").strip().lower() \
                       in ("1", "true", "yes", "on")
APPROVALS_FILE = Path(__file__).parent / "approvals.json"
APPROVALS_RETAIN = 100
CONFIRM_GATE_HOOK = Path(__file__).parent / "hooks" / "confirm_gate.py"
CONFIRM_GATE_SETTINGS = Path(__file__).parent / "confirm_gate_settings.json"
if CONFIRM_GATE_ENABLED:
    # Generate the CLI --settings file with the absolute hook path (machine-local).
    CONFIRM_GATE_SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "hooks": [{"type": "command",
                   "command": f"/usr/bin/python3 {CONFIRM_GATE_HOOK}"}]}]}}, indent=2))
    print("[voice-bridge] confirm gate ENABLED (mutating tools require approval)", flush=True)

_bearer = HTTPBearer(auto_error=False)


def _require_key(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY not configured on server")
    if not creds or creds.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

SYSTEM_PROMPT = (
    "You are the owner's voice assistant, running as the `agent` user on the spinbear-mini "
    "(a headless Mac mini). Your replies are read aloud by iOS text-to-speech, so: "
    "be concise (1-3 sentences unless detail is truly needed); no markdown, bullet points, or code blocks; "
    "no 'Here is...' preamble — answer directly; "
    "spell out numbers and times (e.g. 'three forty-five PM', 'two hundred euros'). "
    "The owner is based in Berlin (Europe/Berlin timezone).\n\n"
    "GROUNDING — do not answer about the owner's projects, work, or system state from memory; "
    "investigate first, then answer. Projects live at ~/Documents/Projects/ (dev work under dev/<name>/). "
    "To report a project's status, read its NOTES.md (the newest entry is the LAST one in the file) and any "
    "relevant project files (README, CLAUDE.md, source). Private projects are NOT in your training data, so any "
    "unverified claim about them will be wrong — if you can't confirm something from the files, say so plainly "
    "rather than guessing. Still keep the spoken answer short, even after reading a lot.\n\n"
    "TASK LENGTH — a long task (drafting an article, a multi-file change) is fine: just do it. "
    "The server returns a short spoken acknowledgement to the owner immediately and delivers your final reply "
    "to their phone when you finish, so you do NOT need to refuse long work or rush. When you finish a long task, "
    "make your final reply a short spoken confirmation of what you did (e.g. 'The tokn-watch article is drafted "
    "and saved, ready for review') — the file itself stays on disk; never read a long document aloud.\n\n"
    "RECORD-KEEPING — after you complete any task that creates, changes, or publishes content, or otherwise "
    "changes state (writing/editing files, drafting or publishing an article, updating Linear, running a "
    "state-changing command), append a brief dated entry to the worked-on project's NOTES.md so there is a "
    "durable record — the same way an interactive session logs its work. Convention: newest entry LAST (append "
    "at the END of the file, never mid-file); a dated header using today's Europe/Berlin date (run `date` if "
    "unsure), then 1-3 lines covering what you did, where, and any file paths. Do this as part of finishing the "
    "task, without being asked; it is separate from (and in addition to) your short spoken reply. A read-only "
    "question needs no entry.\n\n"
    "ACTIONS — you may take the following actions when the owner asks:\n"
    "PERMITTED COMMANDS: git read operations (status, log, diff, show, branch); running tests; "
    "reading and updating Linear via the GraphQL API — the env var LINEAR_API_KEY is set; "
    "query https://api.linear.app/graphql with header 'Authorization: $LINEAR_API_KEY' and Content-Type application/json; "
    "workspace is Spinbear (team key SPI); you may read issues/projects and update issue state or assignee when asked; "
    "keep GraphQL queries minimal and targeted; "
    "starting or stopping the owner's own services (tmux sessions, uvicorn, project scripts in ~/Documents/Projects/); "
    "listing processes (ps, pgrep, lsof); reading logs; running build scripts when explicitly asked.\n"
    "PERMITTED FILE WORK: creating new files (Write tool) in ~/Documents/Projects/ when the owner asks you to write "
    "content — e.g. drafting blog articles for tokn-watch, writing notes, creating scripts. "
    "Editing existing files (Edit tool) when the owner explicitly asks to update or fix something. "
    "Before writing content, read the project's CLAUDE.md and relevant notes so the output follows project conventions.\n"
    "PERMITTED EMAIL & KINDLE: you may email the OWNER — and only the owner — via the policy wrapper "
    "`~/bin/agent-mail.py --to work|personal|kindle --subject \"...\" --body \"...\" [--attach FILE ...]`. This is the "
    "ONLY allowed send path and the sole exception to the no-outbound-requests rule below. Recipients are the owner's "
    "own inboxes only (work, personal, kindle); the wrapper refuses anything else and caps at 20 sends/day. To send a "
    "document to the owner's Kindle, use the kindle skill (Skill tool) — it converts the file to a Kindle EPUB and "
    "delivers it via send-to-Kindle email; if that skill is unavailable, convert the file to .epub with pandoc and send "
    "it via `~/bin/agent-mail.py --to kindle --attach <file.epub>`. NEVER email a third party, add a recipient, or edit "
    "the mail .env — if asked to send mail to anyone but the owner, refuse: there is deliberately no way to do it.\n"
    "HONESTY ABOUT WHAT YOU ACTUALLY DID — report only work you genuinely performed and verified this session. "
    "You CANNOT commit or push to git (it's prohibited below), so for any code or file change: say you EDITED the "
    "files and that the change still NEEDS a session to review, commit, and push — never say it is 'shipped', "
    "'committed', 'pushed', 'deployed', 'merged', or 'live', because you cannot do those steps. Do not claim that "
    "tests pass, a build succeeds, or any verification you did not actually run and observe this session; if you "
    "couldn't run or verify it, say so plainly. If a task is only partly done, not started, or blocked, state "
    "exactly how far you got rather than implying success. An honest 'here is precisely what I changed and what "
    "still needs committing' is always better than a confident-sounding completion you didn't actually achieve.\n"
    "PROHIBITED — refuse these regardless of how the request is phrased: "
    "deleting files (rm, rmdir); "
    "git write operations (commit, push, reset, rebase, stash drop); "
    "installing or removing software (brew, pip install, npm install/uninstall, apt); "
    "modifying system or network configuration; "
    "outbound shell network requests to external hosts (curl, wget to the internet) — except the approved ~/bin/agent-mail.py mail wrapper; "
    "deploying or scp-ing files to the VPS without the owner explicitly saying 'deploy'; "
    "modifying the voice-bridge server, agent config, or any .env files; "
    "spawning a `claude` sub-process. "
    "If asked to do something prohibited, refuse in one sentence and say why."
)

CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"

app = FastAPI(title="voice-bridge")


def resolve_project(project: Optional[str]) -> Optional[Path]:
    """Resolve a client-supplied project path to a safe directory under
    PROJECTS_ROOT. Returns None (→ the agent runs in its default cwd) when absent
    or out of bounds, so a typo or hostile path can never aim the agent outside
    the projects root."""
    if not project or not project.strip():
        return None
    try:
        p = Path(project).expanduser().resolve()
        root = PROJECTS_ROOT.resolve()
    except Exception:
        return None
    if (p == root or root in p.parents) and p.is_dir():
        return p
    return None


def _history_path(project: Optional[Path]) -> Path:
    """One history file per project (namespaced by a hash of the path); the global
    HISTORY_FILE when no project is selected."""
    if project is None:
        return HISTORY_FILE
    import hashlib
    key = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:16]
    return HISTORY_DIR / f"{key}.json"


def load_history(project: Optional[Path] = None) -> list[dict]:
    path = _history_path(project)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []


def save_history(history: list[dict], project: Optional[Path] = None) -> None:
    path = _history_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2))


def trim_history(history: list[dict]) -> list[dict]:
    max_messages = MAX_HISTORY_TURNS * 2
    return history[-max_messages:] if len(history) > max_messages else history


RESULTS_RETAIN = 50  # keep at most the last N results in the log


def _load_store() -> dict:
    """Load the result store as the v2 object, migrating the old list-of-strings
    shape on read. Shape: {version, seq, legacy_cursor, results:[{id, task_id,
    text, created_at}]}. `legacy_cursor` = the highest id already handed to the
    destructive /result*/next consumer (the legacy iOS Shortcut)."""
    empty = {"version": 2, "seq": 0, "legacy_cursor": 0, "results": []}
    if not RESULTS_FILE.exists():
        return empty
    try:
        raw = json.loads(RESULTS_FILE.read_text())
    except Exception:
        return empty
    if isinstance(raw, dict) and raw.get("version") == 2:
        return raw
    if isinstance(raw, list):  # migrate old format: bare strings, none popped yet
        results = [{"id": i + 1, "task_id": None, "text": t, "created_at": None}
                   for i, t in enumerate(raw)]
        return {"version": 2, "seq": len(results), "legacy_cursor": 0, "results": results}
    return empty


def _save_store(store: dict) -> None:
    RESULTS_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2))


def enqueue_result(text: str, task_id: Optional[str] = None) -> int:
    """Append a finished job's spoken text to the append-only result log; return
    its id. New clients read it non-destructively via /v1/results; the legacy
    /result*/next endpoints consume it through `legacy_cursor`."""
    store = _load_store()
    store["seq"] += 1
    entry = {"id": store["seq"], "task_id": task_id, "text": text,
             "created_at": datetime.now().isoformat()}
    store["results"].append(entry)
    if len(store["results"]) > RESULTS_RETAIN:  # bound, but never drop legacy-unread
        unread = {r["id"] for r in store["results"] if r["id"] > store["legacy_cursor"]}
        recent = {r["id"] for r in store["results"][-RESULTS_RETAIN:]}
        keep = unread | recent
        store["results"] = [r for r in store["results"] if r["id"] in keep]
    _save_store(store)
    return entry["id"]


def pop_result() -> str:
    """Legacy destructive read: return the oldest result the legacy consumer has
    not yet seen and advance `legacy_cursor`; empty string if none. Behaviour is
    identical to the original pop-queue (oldest-first, once each) for the Shortcut."""
    store = _load_store()
    nxt = next((r for r in store["results"] if r["id"] > store["legacy_cursor"]), None)
    if nxt is None:
        return ""
    store["legacy_cursor"] = nxt["id"]
    _save_store(store)
    return nxt["text"]


def results_after(after_id: int, limit: int = RESULTS_RETAIN) -> dict:
    """Non-destructive cursor read for new clients (GoSancho): results with
    id > after_id (ascending), plus the latest seq for cursor sync."""
    store = _load_store()
    items = [r for r in store["results"] if r["id"] > after_id][:limit]
    return {"results": items, "seq": store["seq"]}


def legacy_pending_count() -> int:
    """Count results the legacy consumer hasn't read (for startup re-notify)."""
    store = _load_store()
    return sum(1 for r in store["results"] if r["id"] > store["legacy_cursor"])


# --- Confirm-gate approval store ---------------------------------------------
# Single-writer (this server) JSON store; the PreToolUse hook only POSTs new
# requests and GETs state, the app POSTs decisions — so all writes serialize
# here and there is no file-lock race.

def _load_approvals() -> dict:
    empty = {"version": 1, "seq": 0, "approvals": []}
    if not APPROVALS_FILE.exists():
        return empty
    try:
        raw = json.loads(APPROVALS_FILE.read_text())
        return raw if isinstance(raw, dict) and raw.get("version") == 1 else empty
    except Exception:
        return empty


def _save_approvals(store: dict) -> None:
    APPROVALS_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2))


def create_approval(tool: str, command: str, description: str,
                    task_id: Optional[str]) -> dict:
    store = _load_approvals()
    store["seq"] += 1
    entry = {"id": store["seq"], "task_id": task_id, "tool": tool,
             "command": command, "description": description or tool,
             "state": "pending", "created_at": datetime.now().isoformat(),
             "decided_at": None}
    store["approvals"].append(entry)
    store["approvals"] = store["approvals"][-APPROVALS_RETAIN:]
    _save_approvals(store)
    return entry


def get_approval(aid: int) -> Optional[dict]:
    return next((a for a in _load_approvals()["approvals"] if a["id"] == aid), None)


def decide_approval(aid: int, decision: str) -> Optional[dict]:
    store = _load_approvals()
    a = next((x for x in store["approvals"] if x["id"] == aid), None)
    if a is None:
        return None
    if a["state"] == "pending":   # first decision wins; ignore late/duplicate posts
        a["state"] = decision
        a["decided_at"] = datetime.now().isoformat()
        _save_approvals(store)
    return a


def pending_approvals() -> list[dict]:
    return [a for a in _load_approvals()["approvals"] if a["state"] == "pending"]


# --- APNs device registry + silent-wake fan-out ------------------------------

def _load_devices() -> list[dict]:
    if DEVICES_FILE.exists():
        try:
            return json.loads(DEVICES_FILE.read_text())
        except Exception:
            return []
    return []


def _save_devices(devices: list[dict]) -> None:
    DEVICES_FILE.write_text(json.dumps(devices, ensure_ascii=False, indent=2))


def register_device(token: str, env: str) -> None:
    """Upsert an APNs device token (env = 'sandbox' | 'production'), capped."""
    devices = [d for d in _load_devices() if d.get("token") != token]
    devices.append({"token": token, "env": env})
    _save_devices(devices[-20:])


def push_to_devices(task_id: Optional[str]) -> bool:
    """Visible 'doorbell' alert to every registered device when a long task finishes.
    The body is GENERIC — the result text is NOT in the payload, so it never leaves
    your server (privacy); the app fetches /v1/results itself when the user taps (the
    task id rides along). A visible alert (vs a silent content-available wake) is the
    reliable path: iOS heavily throttles silent pushes. Returns True if at least one
    device accepted. Prunes dead tokens (BadDeviceToken / Unregistered / 410)."""
    if not APNS_ENABLED:
        return False
    devices = _load_devices()
    if not devices:
        return False
    payload = {
        "aps": {
            "alert": {"title": "Your task is done", "body": "Tap to read the result."},
            "sound": "default",
        },
        "task_id": task_id,   # doorbell only — no result text in the payload
    }
    delivered, survivors = False, []
    for d in devices:
        try:
            status, reason = _apns.send_push(
                d["token"], key_path=APNS_KEY_PATH, key_id=APNS_KEY_ID,
                team_id=APNS_TEAM_ID, topic=APNS_TOPIC,
                sandbox=(d.get("env") != "production"), payload=payload,
                push_type="alert")
        except Exception as exc:
            print(f"[voice-bridge] APNs send error: {exc}", flush=True)
            survivors.append(d)  # keep on transient error
            continue
        if status == 200:
            delivered = True
            survivors.append(d)
        elif reason in ("BadDeviceToken", "Unregistered") or status == 410:
            print(f"[voice-bridge] pruning dead device token ({reason})", flush=True)
        else:
            print(f"[voice-bridge] APNs {status} {reason}", flush=True)
            survivors.append(d)
    if len(survivors) != len(devices):
        _save_devices(survivors)
    return delivered


def push_approval_to_devices(approval_id: int, description: str) -> bool:
    """Wake every registered device that an action needs approval (confirm screen
    09). Like push_to_devices but carries `approval_id` + an APPROVAL category the
    app routes to the confirm UI; the body is the short action description (no
    secrets — the full command is fetched via /v1/approvals only after the tap)."""
    if not APNS_ENABLED:
        return False
    devices = _load_devices()
    if not devices:
        return False
    payload = {
        "aps": {
            "alert": {"title": "Approve action?",
                      "body": (description or "An action needs your OK.")[:120]},
            "sound": "default",
            "category": "APPROVAL",
        },
        "approval_id": approval_id,
    }
    delivered, survivors = False, []
    for d in devices:
        try:
            status, reason = _apns.send_push(
                d["token"], key_path=APNS_KEY_PATH, key_id=APNS_KEY_ID,
                team_id=APNS_TEAM_ID, topic=APNS_TOPIC,
                sandbox=(d.get("env") != "production"), payload=payload,
                push_type="alert")
        except Exception as exc:
            print(f"[voice-bridge] APNs approval send error: {exc}", flush=True)
            survivors.append(d)
            continue
        if status == 200:
            delivered = True
            survivors.append(d)
        elif reason in ("BadDeviceToken", "Unregistered") or status == 410:
            print(f"[voice-bridge] pruning dead device token ({reason})", flush=True)
        else:
            print(f"[voice-bridge] APNs {status} {reason}", flush=True)
            survivors.append(d)
    if len(survivors) != len(devices):
        _save_devices(survivors)
    return delivered


def record_turn(user_text: str, reply: str, project: Optional[Path] = None) -> None:
    """Append a completed exchange to the project's history (re-reads to avoid
    clobbering a concurrent write — backgrounded jobs finish out of band)."""
    history = load_history(project)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    save_history(trim_history(history), project)


def log_activity(request: str, reply: str, mode: str) -> None:
    """Append-only durable record of every completed voice task — NEVER trimmed
    (unlike history.json's rolling window). One JSON object per line: Berlin
    timestamp, mode (sync/background/sync-error), the request, and the spoken
    reply. Best-effort: a logging failure must not break the response."""
    try:
        ts = datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="seconds")
        entry = {"ts": ts, "mode": mode, "request": request, "reply": reply}
        with ACTIVITY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[voice-bridge] activity log failed: {exc}", flush=True)


def _context_hint(project: Optional[Path]) -> str:
    """Native context discovery: tell the agent which project it's in and which of
    the project's own context files exist (CLAUDE.md is auto-loaded by the CLI via
    cwd; AGENTS.md/README/etc. are named so the agent reads whatever's there — it
    works for any user's folder, including an empty one)."""
    if project is None:
        return ""
    found = [f for f in CONTEXT_FILES if (project / f).is_file()]
    note = f"You are working in the project at {project}."
    if found:
        note += " Read these for its conventions before acting: " + ", ".join(found) + "."
    return note + "\n\n"


def build_prompt(history: list[dict], user_text: str, project: Optional[Path] = None) -> str:
    lines = []
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{role}: {msg['content']}")
    lines.append(f"User: {user_text}")
    lines.append("Assistant:")
    return _context_hint(project) + "\n".join(lines)


def _claude_env() -> Optional[dict]:
    return {**os.environ, "LINEAR_API_KEY": LINEAR_API_KEY} if LINEAR_API_KEY else None


# --- Live agent-activity echo (tool-echo) --------------------------------------
# As the claude CLI runs, each tool it uses is turned into a human one-liner and
# appended to a per-task list, so the app can show "what the agent is doing" live
# while a task runs. In-memory, bounded; the final spoken reply still flows through
# the normal result path.
_ACTIVITY: dict = {}
_ACTIVITY_ORDER: list = []
ACTIVITY_MAX_TASKS = 50


def _append_activity(task_id: Optional[str], line: str) -> None:
    if not task_id or not line:
        return
    if task_id not in _ACTIVITY:
        _ACTIVITY[task_id] = []
        _ACTIVITY_ORDER.append(task_id)
        while len(_ACTIVITY_ORDER) > ACTIVITY_MAX_TASKS:
            _ACTIVITY.pop(_ACTIVITY_ORDER.pop(0), None)
    _ACTIVITY[task_id].append(line)


def _tool_line(name: str, inp: dict) -> str:
    """One human-readable line for a tool_use event."""
    if name == "Bash":
        return inp.get("description") or ("$ " + str(inp.get("command", ""))[:80])
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        verb = {"Read": "Reading", "Edit": "Editing", "Write": "Writing", "NotebookEdit": "Editing"}[name]
        fp = str(inp.get("file_path", ""))
        return f"{verb} {os.path.basename(fp) or fp}".strip()
    if name in ("Glob", "Grep"):
        return f"Searching {inp.get('pattern', '')}".strip()
    if name == "Skill":
        return f"Skill: {inp.get('command') or inp.get('name', '')}".strip()
    if name == "Task":
        return f"Subagent: {inp.get('description', 'working')}"
    if "__" in name:
        return name.split("__")[-1]
    return name


async def run_claude(prompt: str, cwd: Optional[Path] = None,
                     task_id: Optional[str] = None) -> tuple[int, str, str]:
    """Run the claude CLI with streaming JSON output, capturing each tool the agent
    uses into the live per-task activity echo and returning the final spoken reply.
    Bounded by HARD_CAP. `cwd` scopes the run to a project (SPI-255). The prompt is
    fed via stdin (avoids the variadic --allowedTools swallowing it)."""
    # Confirm gate (opt-in): wire the PreToolUse hook so mutating tools pause for
    # owner approval. Empty when disabled ⇒ the spawn is byte-identical to before.
    gate_args = ["--settings", str(CONFIRM_GATE_SETTINGS)] if CONFIRM_GATE_ENABLED else []
    proc = await asyncio.create_subprocess_exec(
        str(CLAUDE_BIN),
        "-p",
        "--output-format", "stream-json", "--verbose",
        "--append-system-prompt", SYSTEM_PROMPT,
        "--allowedTools",
        "Read,Glob,Grep,Bash,Write,Edit,Skill,mcp__claude_ai_Linear,mcp__0b5df993-74ea-4b67-ab52-95bf2f19bfdd,ToolSearch",
        "--no-session-persistence",
        *gate_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,   # raise the per-line cap above the 64 KiB default
        env=_claude_env(),
        cwd=str(cwd) if cwd else None,
    )
    if proc.stdin is not None:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    final = ""

    async def _read() -> None:
        nonlocal final
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            etype = evt.get("type")
            if etype == "assistant":
                for block in evt.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        _append_activity(task_id, _tool_line(block.get("name", ""), block.get("input", {}) or {}))
            elif etype == "result":
                final = evt.get("result", "") or final

    try:
        await asyncio.wait_for(_read(), timeout=HARD_CAP)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"job exceeded HARD_CAP ({HARD_CAP}s)"
    except Exception as e:
        # A stream-read failure (e.g. a line past STREAM_LIMIT) must not crash the task
        # into a 500 - return it as a clean error the caller can surface / queue.
        proc.kill()
        await proc.wait()
        return -1, "", f"stream read failed: {type(e).__name__}: {e}"
    err = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
    rc = await proc.wait()
    return rc, final.strip(), err


def _post_json(url: str, payload: dict, headers: dict) -> None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _notify_sync(text: str) -> None:
    """Alert the phone that a finished result is waiting.

    Pushcut path (free): ring the STATIC notification as a doorbell — it carries
    no dynamic content (that's Pushcut's paid feature); the Shortcut it launches
    pulls the words from /result/next. The result is queued by the caller before
    this runs. Telegram fallback: send the full text to read."""
    if PUSHCUT_API_KEY:
        try:
            # Bare trigger — no title/text/input. The notification's own static
            # config shows the banner and runs the "speak the next result" Shortcut.
            _post_json(
                f"https://api.pushcut.io/v1/notifications/{PUSHCUT_NOTIFICATION}",
                {},
                {"API-Key": PUSHCUT_API_KEY},
            )
            return
        except Exception as exc:  # fall through to Telegram
            print(f"[voice-bridge] Pushcut notify failed: {exc}", flush=True)
    if TELEGRAM_BOT_TOKEN and OWNER_CHAT_ID:
        try:
            _post_json(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                {"chat_id": OWNER_CHAT_ID, "text": text},
                {},
            )
            return
        except Exception as exc:
            print(f"[voice-bridge] Telegram notify failed: {exc}", flush=True)
    print(f"[voice-bridge] no notify channel configured; result: {text}", flush=True)


async def notify(text: str) -> None:
    await asyncio.to_thread(_notify_sync, text)


# Hold strong refs to detached jobs — the event loop only keeps weak refs, so an
# un-referenced background task can be garbage-collected mid-run.
_BG_TASKS: set = set()


def spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def finish_in_background(task: "asyncio.Task", user_text: str,
                               task_id: Optional[str] = None,
                               project: Optional[Path] = None) -> None:
    """Await a promoted job, record it, queue the spoken text, and ring the phone."""
    try:
        rc, out, err = await task
    except Exception as exc:
        reply = f"Sorry, the background task errored: {exc}"
    else:
        if rc != 0:
            reply = f"Sorry, the background task failed: {err[:200] or out[:200]}"
        else:
            reply = out or "Done."
            record_turn(user_text, reply, project)
    # Queue the text first so it's available the instant a client fetches it,
    # then ring the doorbell (Pushcut) / send the fallback (Telegram).
    enqueue_result(reply, task_id)
    log_activity(user_text, reply, "background")
    # Client-aware delivery: if a registered device got the silent APNs wake (the
    # app fetches the text from /v1/results itself), skip the legacy Pushcut/Telegram
    # doorbell — that's what stops the now-redundant empty Shortcut trigger. Fall
    # back to the doorbell when APNs is off or no device accepted.
    pushed = await asyncio.to_thread(push_to_devices, task_id)
    if not pushed:
        await notify(reply)


@app.on_event("startup")
async def _renotify_queued() -> None:
    """On restart, re-ring the phone for any results that were queued but whose
    notification was lost (e.g. a restart killed the background task mid-notify)."""
    count = legacy_pending_count()
    if count:
        label = "result" if count == 1 else "results"
        print(f"[voice-bridge] startup: {count} queued {label} — re-notifying", flush=True)
        await notify(f"Voice bridge restarted with {count} pending {label} waiting. Tap to hear.")


def strip_force_trigger(text: str) -> tuple[bool, str]:
    """If `text` starts with a FORCE_BG_TRIGGERS phrase, return (True, the rest
    of the request with the trigger removed); otherwise (False, text unchanged).
    A bare trigger (nothing after it) gets a default self-test question."""
    low = text.lstrip().lower()
    for trig in FORCE_BG_TRIGGERS:
        if low.startswith(trig):
            remainder = text.lstrip()[len(trig):].lstrip(" ,.:;-—")
            if not remainder:
                remainder = "In one short spoken sentence, confirm that the delayed answer path is working."
            return True, remainder
    return False, text


class AskRequest(BaseModel):
    text: str
    project: Optional[str] = None   # SPI-255: scope the agent to this folder


@app.post("/ask", response_class=PlainTextResponse, dependencies=[Depends(_require_key)])
async def ask(req: AskRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty input")

    # Mint a task id up front and return it as a header. The body stays plain
    # text (legacy clients ignore the header); new clients use it to correlate
    # the eventual /v1/results entry to this request.
    task_id = "t_" + uuid.uuid4().hex[:12]
    headers = {"X-Agent-Task-Id": task_id}
    project = resolve_project(req.project)   # None unless a valid in-bounds folder
    force_bg, user_text = strip_force_trigger(req.text.strip())
    history = load_history(project)
    prompt = build_prompt(history, user_text, project)

    task = asyncio.create_task(run_claude(prompt, cwd=project, task_id=task_id))

    if force_bg:
        # Forced delayed path: detach immediately without waiting the soft
        # deadline, so the ack + doorbell + /result/next flow always exercises.
        spawn_background(finish_in_background(task, user_text, task_id, project))
        return PlainTextResponse("I'm on it.", headers=headers)

    done, _ = await asyncio.wait({task}, timeout=SOFT_DEADLINE)

    if task in done:
        # Fast path: finished within the soft deadline — answer synchronously,
        # spoken aloud by the Shortcut exactly as before.
        rc, out, err = task.result()
        if rc != 0:
            log_activity(user_text, f"[error] {err[:200] or out[:200]}", "sync-error")
            raise HTTPException(status_code=502, detail=f"Claude error: {err[:200] or out[:200]}")
        record_turn(user_text, out, project)
        log_activity(user_text, out, "sync")
        return PlainTextResponse(out, headers=headers)

    # Slow path: promote to a background job. Detach it, notify on completion,
    # and return a short spoken ack now so the phone is freed.
    spawn_background(finish_in_background(task, user_text, task_id, project))
    return PlainTextResponse("I'm on it.", headers=headers)


@app.api_route("/result/next", methods=["GET", "POST"], response_class=PlainTextResponse,
               dependencies=[Depends(_require_key)])
@app.api_route("/results/next", methods=["GET", "POST"], response_class=PlainTextResponse,
               dependencies=[Depends(_require_key)])
async def result_next() -> str:
    """Pop the oldest finished-job result for the Speak-Voice-Result Shortcut to
    read aloud. Returns a spoken-friendly placeholder when nothing is queued.
    `/results/next` (plural) is an alias — the Shortcut was built with that path."""
    return pop_result() or "No new results."


@app.get("/v1/results", dependencies=[Depends(_require_key)])
async def v1_results(after: int = 0, limit: int = RESULTS_RETAIN) -> dict:
    """Non-destructive, id-correlated cursor read for new clients (GoSancho):
    results with id > `after` (ascending) plus the latest `seq`. Does not touch
    `legacy_cursor`, so it never starves the Shortcut and the Shortcut never
    starves it — the two consumers are independent."""
    return results_after(after, limit)


class DeviceRegistration(BaseModel):
    token: str
    env: str = "sandbox"


@app.post("/v1/devices", dependencies=[Depends(_require_key)])
async def v1_register_device(reg: DeviceRegistration) -> dict:
    """Register this device's APNs token so long-task results wake the app to
    fetch them. `env` is 'sandbox' (dev/TestFlight-internal) or 'production'."""
    token = reg.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Empty token")
    register_device(token, "production" if reg.env == "production" else "sandbox")
    return {"ok": True, "apns_enabled": APNS_ENABLED}


# --- Confirm-gate approval endpoints (CONFIRM_GATE.md) ------------------------
# Active surface only when CONFIRM_GATE_ENABLED wires the PreToolUse hook; the
# endpoints themselves are always mounted (harmless when nothing posts to them).

class ApprovalCreate(BaseModel):
    tool: str
    command: str = ""
    description: str = ""
    task_id: Optional[str] = None


class ApprovalDecision(BaseModel):
    decision: str


@app.post("/v1/approvals", dependencies=[Depends(_require_key)])
async def v1_create_approval(req: ApprovalCreate) -> dict:
    """Create a pending approval (called by the PreToolUse hook) and wake the
    phone to the confirm screen. Returns the id the hook then polls until decided."""
    entry = create_approval(req.tool, req.command, req.description, req.task_id)
    try:
        push_approval_to_devices(entry["id"], entry["description"])
    except Exception as exc:
        print(f"[voice-bridge] approval push failed: {exc}", flush=True)
    return {"id": entry["id"], "state": entry["state"]}


@app.get("/v1/approvals", dependencies=[Depends(_require_key)])
async def v1_list_approvals() -> dict:
    """Pending approvals for the app to display (confirm screen 09)."""
    return {"approvals": pending_approvals()}


@app.get("/v1/approvals/{aid}", dependencies=[Depends(_require_key)])
async def v1_get_approval(aid: int) -> dict:
    """Poll one approval's state — the hook blocks on this until allow/deny."""
    a = get_approval(aid)
    if a is None:
        raise HTTPException(status_code=404, detail="no such approval")
    return {"id": a["id"], "state": a["state"]}


@app.post("/v1/approvals/{aid}", dependencies=[Depends(_require_key)])
async def v1_decide_approval(aid: int, req: ApprovalDecision) -> dict:
    """Record the owner's decision (app confirm screen, Telegram, or terminal)."""
    if req.decision not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail="decision must be 'allow' or 'deny'")
    a = decide_approval(aid, req.decision)
    if a is None:
        raise HTTPException(status_code=404, detail="no such approval")
    return {"ok": True, "id": aid, "state": a["state"]}


@app.get("/v1/projects", dependencies=[Depends(_require_key)])
async def v1_projects(path: Optional[str] = None) -> dict:
    """List the immediate subfolders of `path` (default: PROJECTS_ROOT) so the
    client can browse the tree one level at a time (nested picker, SPI-254).
    `hasChildren` flags folders worth drilling into. Bounded to PROJECTS_ROOT —
    an out-of-bounds path falls back to the root, never the wider filesystem."""
    root = PROJECTS_ROOT.resolve()
    base = resolve_project(path) or root   # resolve_project bounds to root; None → root
    entries: list[dict] = []
    if base.is_dir():
        for c in sorted(base.iterdir()):
            if not c.is_dir() or c.name.startswith("."):
                continue
            try:
                has_children = any(s.is_dir() and not s.name.startswith(".") for s in c.iterdir())
            except (PermissionError, OSError):
                has_children = False
            entries.append({"name": c.name, "path": str(c), "hasChildren": has_children})
            if len(entries) >= 500:
                break
    return {"path": str(base), "entries": entries}


@app.get("/v1/activity", dependencies=[Depends(_require_key)])
async def v1_activity(task_id: str, after: int = 0) -> dict:
    """Live agent tool-activity echo for a (possibly still-running) task — the lines
    the app shows under 'On it…'. `after` = how many the client already has; only
    newer lines are returned so it can append."""
    lines = _ACTIVITY.get(task_id, [])
    return {"task_id": task_id, "lines": lines[after:], "total": len(lines)}


@app.delete("/history", response_class=PlainTextResponse, dependencies=[Depends(_require_key)])
async def clear_history() -> str:
    save_history([])
    return "History cleared."


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "ok"


if __name__ == "__main__":
    tailscale_ip = os.environ.get("TAILSCALE_IP", "100.65.52.120")
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host=tailscale_ip, port=port)
