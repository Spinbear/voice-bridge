"""
voice-bridge: Tailscale-only FastAPI server.
Receives spoken text from iOS Shortcut → Claude CLI → returns reply text.
Uses the installed claude CLI (no API key needed — rides Claude Code subscription).
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel


def _read_env_key(key: str) -> str:
    """Read a single key from the local .env without polluting os.environ."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line[len(key) + 1:].strip().strip('"').strip("'")
    return ""

HISTORY_FILE = Path(__file__).parent / "history.json"
MAX_HISTORY_TURNS = 10  # keep last N exchanges in context

API_KEY = os.environ.get("API_KEY") or _read_env_key("API_KEY")
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
    "ACTIONS — you may take the following actions when the owner asks:\n"
    "PERMITTED COMMANDS: git read operations (status, log, diff, show, branch); running tests; "
    "reading Linear (use the mcp__claude_ai_Linear tools — Spinbear workspace, team key SPI); "
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


def build_prompt(history: list[dict], user_text: str) -> str:
    lines = []
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{role}: {msg['content']}")
    lines.append(f"User: {user_text}")
    lines.append("Assistant:")
    return "\n".join(lines)


class AskRequest(BaseModel):
    text: str


@app.post("/ask", response_class=PlainTextResponse, dependencies=[Depends(_require_key)])
async def ask(req: AskRequest) -> str:
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty input")

    history = load_history()
    prompt = build_prompt(history, req.text.strip())

    result = subprocess.run(
        [
            str(CLAUDE_BIN),
            "-p",
            "--output-format", "text",
            # append (not replace) the default agent prompt so it keeps its tool-using
            # brain, then layer the voice style + grounding/read-only rules on top.
            "--append-system-prompt", SYSTEM_PROMPT,
            # read-only tool set: can read/search files to ground answers, but cannot
            # write/edit or run shell commands. Secrets stay blocked by the agent deny-list.
            "--allowedTools", "Read,Glob,Grep,Bash,Write,Edit,mcp__claude_ai_Linear,mcp__0b5df993-74ea-4b67-ab52-95bf2f19bfdd,ToolSearch",
            "--no-session-persistence",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=120,  # tool round-trips (reading NOTES etc.) need more headroom than a bare chat reply
    )

    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Claude error: {result.stderr[:200] or result.stdout[:200]}")

    reply = result.stdout.strip()

    history.append({"role": "user", "content": req.text.strip()})
    history.append({"role": "assistant", "content": reply})
    save_history(trim_history(history))

    return reply


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
