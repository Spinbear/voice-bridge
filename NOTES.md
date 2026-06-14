# NOTES — voice-bridge

## 2026-06-13
Created project. FastAPI server scaffolded with rolling conversation history, Tailscale-only binding, and Sonnet 4.6 backend. Next: add ANTHROPIC_API_KEY to .env, run start.sh, wire up iOS Shortcut (recipe in Telegram conversation), and optionally set up launchd to auto-start on boot.

## 2026-06-13 (later — corrections + git)
The "next steps" above are superseded:
- **Backend is the `claude` CLI**, not the Anthropic SDK — the `ANTHROPIC_API_KEY` in `.env` is **vestigial/unused**.
- Runs as a **tmux service** via `~/bin/start-voice-bridge.sh` (started over SSH), **NOT launchd** — a LaunchAgent hit the no-FDA wall (`PermissionError` reading `.venv` under `~/Documents`) and crash-looped; the plist is disabled (`.disabled`).
- Made **project-aware + read-only**: `--append-system-prompt` + `--allowedTools Read,Glob,Grep` — grounds answers in real project files (e.g. reads NOTES.md) but can't write/edit or run shell commands. Refuses "do X" requests by voice.
- **Committed to git** this session (first commit). Endpoint is currently **unauthenticated** (Tailscale-only) — broadening tools / adding request auth is an open decision (owner sleeping on it); risk analysis in `mac-mini-ops` NOTES.

## 2026-06-14
iOS Shortcut built and working end-to-end. Apple Watch availability TBD (see Telegram conversation).

## 2026-06-14 (session 2)
- Done: Added Bearer token API key auth on /ask and /history (key in .env, read via _read_env_key to avoid polluting subprocess env — load_dotenv() was leaking vestigial ANTHROPIC_API_KEY and breaking Claude auth). Added Bash + Write + Edit to --allowedTools so voice agent can run permitted commands and author files. Added Linear access: personal API key (full access) stored in .env, passed as LINEAR_API_KEY to subprocess env; system prompt teaches Claude to query api.linear.app/graphql via curl. Removed broken mcp__claude_ai_Linear approach (cloud connector, not available in -p mode). All changes committed and pushed.
- Decided: _read_env_key() reads individual keys from .env without os.environ pollution — safer than load_dotenv() for a process that shells out to claude. Deploy/VPS operations prohibited unless owner says "deploy" explicitly. Linear write allowed (owner chose full-access key).
- Next: Test Linear queries and blog article writing via voice once watchOS update is complete and Watch shortcut is working.
