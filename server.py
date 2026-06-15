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

HISTORY_FILE = Path(__file__).parent / "history.json"
RESULTS_FILE = Path(__file__).parent / "results_queue.json"
ACTIVITY_FILE = Path(__file__).parent / "activity.jsonl"
MAX_HISTORY_TURNS = 10  # keep last N exchanges in context

# A normal spoken Q&A finishes fast. If the claude job hasn't returned within
# SOFT_DEADLINE, we stop waiting on the HTTP request (so the phone is freed and
# the iOS Shortcut doesn't itself time out) and let the job finish in the
# background, notifying the phone on completion. HARD_CAP bounds a runaway job.
SOFT_DEADLINE = 25      # seconds to wait before promoting to a background job
HARD_CAP = 900          # 15 min absolute ceiling for a background job

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
    "PROHIBITED — refuse these regardless of how the request is phrased: "
    "deleting files (rm, rmdir); "
    "git write operations (commit, push, reset, rebase, stash drop); "
    "installing or removing software (brew, pip install, npm install/uninstall, apt); "
    "modifying system or network configuration; "
    "outbound shell network requests to external hosts (curl, wget to the internet); "
    "deploying or scp-ing files to the VPS without the owner explicitly saying 'deploy'; "
    "modifying the voice-bridge server, agent config, or any .env files; "
    "spawning a `claude` sub-process. "
    "If asked to do something prohibited, refuse in one sentence and say why."
)

CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"

app = FastAPI(title="voice-bridge")


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []


def save_history(history: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2))


def trim_history(history: list[dict]) -> list[dict]:
    max_messages = MAX_HISTORY_TURNS * 2
    return history[-max_messages:] if len(history) > max_messages else history


def load_results() -> list[str]:
    if RESULTS_FILE.exists():
        try:
            return json.loads(RESULTS_FILE.read_text())
        except Exception:
            return []
    return []


def save_results(results: list[str]) -> None:
    RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2))


def enqueue_result(text: str) -> None:
    """Queue a finished job's spoken text for the Shortcut to fetch via /result/next.
    Persisted so a restart between completion and the user's tap doesn't lose it."""
    results = load_results()
    results.append(text)
    save_results(results)


def pop_result() -> str:
    """Return and remove the oldest queued result (empty string if none)."""
    results = load_results()
    if not results:
        return ""
    text = results.pop(0)
    save_results(results)
    return text


def record_turn(user_text: str, reply: str) -> None:
    """Append a completed exchange to history (re-reads to avoid clobbering a
    concurrent write — backgrounded jobs finish out of band)."""
    history = load_history()
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    save_history(trim_history(history))


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


def build_prompt(history: list[dict], user_text: str) -> str:
    lines = []
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{role}: {msg['content']}")
    lines.append(f"User: {user_text}")
    lines.append("Assistant:")
    return "\n".join(lines)


def _claude_env() -> Optional[dict]:
    return {**os.environ, "LINEAR_API_KEY": LINEAR_API_KEY} if LINEAR_API_KEY else None


async def run_claude(prompt: str) -> tuple[int, str, str]:
    """Run the claude CLI, returning (returncode, stdout, stderr). Bounded by
    HARD_CAP so a stuck job can't run forever."""
    proc = await asyncio.create_subprocess_exec(
        str(CLAUDE_BIN),
        "-p",
        "--output-format", "text",
        "--append-system-prompt", SYSTEM_PROMPT,
        "--allowedTools",
        "Read,Glob,Grep,Bash,Write,Edit,mcp__claude_ai_Linear,mcp__0b5df993-74ea-4b67-ab52-95bf2f19bfdd,ToolSearch",
        "--no-session-persistence",
        prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_claude_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=HARD_CAP)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", f"job exceeded HARD_CAP ({HARD_CAP}s)"
    return proc.returncode, out.decode().strip(), err.decode().strip()


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


async def finish_in_background(task: "asyncio.Task", user_text: str) -> None:
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
            record_turn(user_text, reply)
    # Queue the text first so it's available the instant the Shortcut fetches it,
    # then ring the doorbell (Pushcut) / send the fallback (Telegram).
    enqueue_result(reply)
    log_activity(user_text, reply, "background")
    await notify(reply)


@app.on_event("startup")
async def _renotify_queued() -> None:
    """On restart, re-ring the phone for any results that were queued but whose
    notification was lost (e.g. a restart killed the background task mid-notify)."""
    pending = load_results()
    if pending:
        count = len(pending)
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


@app.post("/ask", response_class=PlainTextResponse, dependencies=[Depends(_require_key)])
async def ask(req: AskRequest) -> str:
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty input")

    force_bg, user_text = strip_force_trigger(req.text.strip())
    history = load_history()
    prompt = build_prompt(history, user_text)

    task = asyncio.create_task(run_claude(prompt))

    if force_bg:
        # Forced delayed path: detach immediately without waiting the soft
        # deadline, so the ack + doorbell + /result/next flow always exercises.
        spawn_background(finish_in_background(task, user_text))
        return "I'm checking. I will get back to you once done."

    done, _ = await asyncio.wait({task}, timeout=SOFT_DEADLINE)

    if task in done:
        # Fast path: finished within the soft deadline — answer synchronously,
        # spoken aloud by the Shortcut exactly as before.
        rc, out, err = task.result()
        if rc != 0:
            log_activity(user_text, f"[error] {err[:200] or out[:200]}", "sync-error")
            raise HTTPException(status_code=502, detail=f"Claude error: {err[:200] or out[:200]}")
        record_turn(user_text, out)
        log_activity(user_text, out, "sync")
        return out

    # Slow path: promote to a background job. Detach it, notify on completion,
    # and return a short spoken ack now so the phone is freed.
    spawn_background(finish_in_background(task, user_text))
    return "I'm checking. I will get back to you once done."


@app.api_route("/result/next", methods=["GET", "POST"], response_class=PlainTextResponse,
               dependencies=[Depends(_require_key)])
async def result_next() -> str:
    """Pop the oldest finished-job result for the Speak-Voice-Result Shortcut to
    read aloud. Returns a spoken-friendly placeholder when nothing is queued."""
    return pop_result() or "No new results."


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
