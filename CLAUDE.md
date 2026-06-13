# voice-bridge

Tailscale-only FastAPI server that powers hands-free voice interaction with Claude via iOS Shortcuts.

## Architecture
- `server.py` — FastAPI app, binds to the Tailscale IP only. Each `/ask` shells out to the
  installed `claude` CLI (`claude -p`) — rides the Claude Code subscription, **no API key**.
  The CLI runs **project-aware + read-only**: `--append-system-prompt` (keeps its tool-using
  brain, plus the voice style + grounding rules) with `--allowedTools Read,Glob,Grep`, so it
  reads real project files to ground answers but cannot write/edit or run shell commands.
  Secrets stay blocked by the agent deny-list.
- `history.json` — rolling conversation history (auto-managed, gitignored)
- `.env` — `TAILSCALE_IP`, `PORT` (plus a **vestigial** `ANTHROPIC_API_KEY` left from the first
  SDK-based draft — unused; the CLI path needs no key)

## Run
Always-on as a **tmux service started over SSH** — **NOT launchd**. A gui-domain LaunchAgent
has no Full Disk Access and can't even read `.venv/pyvenv.cfg` under `~/Documents`
(`PermissionError` → crash loop); tmux-over-SSH gets the FDA umbrella + the claude file credential.
```bash
~/bin/start-voice-bridge.sh   # sources the auth token, activates the venv, restart loop
```
Recovery + machine kit: see `mac-mini-ops` RUNBOOK §5b/§5e. Re-run after each reboot.

## Endpoints
- `POST /ask` — `{"text": "..."}` → plain text reply
- `DELETE /history` — wipe conversation history
- `GET /health` — liveness check

## Tailscale IP
100.65.52.120 (spinbear-mini)

## Port
8765
