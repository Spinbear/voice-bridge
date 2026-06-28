#!/usr/bin/env python3
"""voice-bridge confirm gate — Claude Code PreToolUse hook.

Fires before a tool runs inside the `claude` process that voice-bridge drives.
For a MUTATING tool (Write/Edit/NotebookEdit, or a Bash command that isn't
clearly read-only) it posts a pending approval to voice-bridge and BLOCKS until
the owner answers on the phone (confirm screen 09), then allows or denies. No
answer within the timeout ⇒ DENY (fail-closed) — the live run can never hang.
Read-only tools are allowed instantly and never interrupt (the brand promise).

This is wired in ONLY when CONFIRM_GATE_ENABLED=1 (see server.py / .env). With
the flag off the hook is never invoked, so the live assistant is unchanged.

Stdin: the PreToolUse event JSON. Stdout: a PreToolUse permission decision.
Talks to the server over localhost using the same API_KEY (read from .env).
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POLL_SECONDS = 2
# Hard ceiling the hook will wait for an answer before denying. Kept below the
# server's own job HARD_CAP so the gate resolves before the task is killed.
# Env-overridable (CONFIRM_GATE_TIMEOUT) for testing.
TIMEOUT_SECONDS = int(os.environ.get("CONFIRM_GATE_TIMEOUT") or 300)

# Tools that never gate — pure reads. Anything not listed and not classified
# read-only below is gated (fail-safe: unknown ⇒ ask).
READONLY_TOOLS = {"Read", "Glob", "Grep", "ToolSearch", "NotebookRead",
                  "WebFetch", "WebSearch", "TodoWrite"}
ALWAYS_GATE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Read-only Bash: allowed instantly. Conservative — a command gates unless every
# segment begins with one of these AND it contains no write/redirect operator.
SAFE_BASH = ("ls", "cat", "pwd", "echo", "date", "whoami", "which", "env",
             "head", "tail", "wc", "grep", "rg", "find", "stat", "file", "tree",
             "du", "df", "ps", "pgrep", "lsof", "uname", "hostname", "printenv",
             "git status", "git log", "git diff", "git show", "git branch",
             "git remote", "git rev-parse", "git config --get")
WRITE_OPERATORS = (">", ">>", "rm ", "rmdir", "mv ", "cp ", "tee ", "dd ",
                   "mkdir", "touch ", "chmod", "chown", "kill", "git commit",
                   "git push", "git reset", "git checkout", "git rebase",
                   "git clean", "git stash")


def _env(key: str) -> str:
    if os.environ.get(key):     # env var wins (production sets neither; tests do)
        return os.environ[key]
    f = REPO / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line[len(key) + 1:].strip().strip('"').strip("'")
    return ""


def _bash_is_readonly(cmd: str) -> bool:
    c = " ".join(cmd.strip().split())
    if not c:
        return True
    if any(op in c for op in WRITE_OPERATORS):
        return False
    # Split on shell separators; every segment must start with a safe command.
    for sep in ("&&", "||", ";", "|"):
        c = c.replace(sep, "\n")
    for seg in (s.strip() for s in c.split("\n") if s.strip()):
        if not any(seg == p or seg.startswith(p + " ") for p in SAFE_BASH):
            return False
    return True


def _describe(tool: str, ti: dict) -> str:
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return f"{tool} {ti.get('file_path') or ti.get('notebook_path') or ''}".strip()
    if tool == "Bash":
        cmd = (ti.get("command") or "").strip().replace("\n", " ")
        return "Run: " + (cmd[:160] + "…" if len(cmd) > 160 else cmd)
    return tool


def allow(reason=""):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def _api(method, path, body=None):
    base = f"http://127.0.0.1:{_env('PORT') or '8765'}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_env('API_KEY')}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    try:
        evt = json.load(sys.stdin)
    except Exception:
        allow("hook: unparseable event, not gating")  # never break the run on bad input
    tool = evt.get("tool_name", "")
    ti = evt.get("tool_input", {}) or {}

    if tool in READONLY_TOOLS:
        allow("read-only tool")
    if tool == "Bash" and _bash_is_readonly(ti.get("command", "")):
        allow("read-only command")
    if tool not in ALWAYS_GATE_TOOLS and tool != "Bash":
        allow("not a gated tool")

    # Gated. Post the pending approval, then block on the owner's decision.
    desc = _describe(tool, ti)
    try:
        created = _api("POST", "/v1/approvals", {
            "tool": tool, "command": (ti.get("command") or "")[:500],
            "description": desc, "task_id": evt.get("session_id")})
        aid = created["id"]
    except Exception as e:
        # If the approval queue is unreachable, fail closed — do NOT silently run.
        deny(f"confirm gate unreachable ({type(e).__name__}); action blocked")

    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            state = _api("GET", f"/v1/approvals/{aid}").get("state")
        except Exception:
            continue  # transient; keep polling until the deadline
        if state == "allow":
            allow("approved by owner")
        if state == "deny":
            deny("denied by owner")
    # No answer in time.
    try:
        _api("POST", f"/v1/approvals/{aid}", {"decision": "deny"})  # record the timeout
    except Exception:
        pass
    deny(f"no approval within {TIMEOUT_SECONDS}s — blocked (fail-closed)")


if __name__ == "__main__":
    main()
