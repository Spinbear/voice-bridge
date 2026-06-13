"""
voice-bridge: Tailscale-only FastAPI server.
Receives spoken text from iOS Shortcut → Claude CLI → returns reply text.
Uses the installed claude CLI (no API key needed — rides Claude Code subscription).
"""

import json
import os
import subprocess
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

HISTORY_FILE = Path(__file__).parent / "history.json"
MAX_HISTORY_TURNS = 10  # keep last N exchanges in context

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
    "READ-ONLY — you investigate and report; you do not change anything. Never create, edit, or delete files, "
    "and never run state-changing commands (no commits, pushes, installs, restarts, no `claude` sub-calls). "
    "If asked to DO something rather than answer, say that's not something you can do by voice."
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


@app.post("/ask", response_class=PlainTextResponse)
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
            "--allowedTools", "Read,Glob,Grep",
            "--no-session-persistence",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=120,  # tool round-trips (reading NOTES etc.) need more headroom than a bare chat reply
    )

    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Claude error: {result.stderr[:200]}")

    reply = result.stdout.strip()

    history.append({"role": "user", "content": req.text.strip()})
    history.append({"role": "assistant", "content": reply})
    save_history(trim_history(history))

    return reply


@app.delete("/history", response_class=PlainTextResponse)
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
