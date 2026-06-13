# NOTES — voice-bridge

## 2026-06-13
Created project. FastAPI server scaffolded with rolling conversation history, Tailscale-only binding, and Sonnet 4.6 backend. Next: add ANTHROPIC_API_KEY to .env, run start.sh, wire up iOS Shortcut (recipe in Telegram conversation), and optionally set up launchd to auto-start on boot.

## 2026-06-13 (later — corrections + git)
The "next steps" above are superseded:
- **Backend is the `claude` CLI**, not the Anthropic SDK — the `ANTHROPIC_API_KEY` in `.env` is **vestigial/unused**.
- Runs as a **tmux service** via `~/bin/start-voice-bridge.sh` (started over SSH), **NOT launchd** — a LaunchAgent hit the no-FDA wall (`PermissionError` reading `.venv` under `~/Documents`) and crash-looped; the plist is disabled (`.disabled`).
- Made **project-aware + read-only**: `--append-system-prompt` + `--allowedTools Read,Glob,Grep` — grounds answers in real project files (e.g. reads NOTES.md) but can't write/edit or run shell commands. Refuses "do X" requests by voice.
- **Committed to git** this session (first commit). Endpoint is currently **unauthenticated** (Tailscale-only) — broadening tools / adding request auth is an open decision (owner sleeping on it); risk analysis in `mac-mini-ops` NOTES.
