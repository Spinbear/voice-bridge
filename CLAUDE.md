# voice-bridge

Tailscale-only FastAPI server that powers hands-free voice interaction with Claude via iOS Shortcuts.

## Architecture
- `server.py` — FastAPI app, binds to the Tailscale IP only. Each `/ask` shells out to the
  installed `claude` CLI (`claude -p`) — rides the Claude Code subscription, **no API key**.
  The CLI runs **project-aware** via `--append-system-prompt` (keeps its tool-using brain, plus
  the voice style + grounding rules) with `--allowedTools Read,Glob,Grep,Bash,Write,Edit,...` —
  it can read files to ground answers and, when the owner asks, run permitted commands and
  author content (e.g. tokn-watch drafts). The system prompt enforces a permit/prohibit list
  (no deletes, no git writes, no installs, no deploy without "deploy"); secrets stay blocked by
  the agent deny-list. `/ask` is **Bearer-auth'd** (`API_KEY` in `.env`).
- **Long tasks run as background jobs.** `/ask` waits `SOFT_DEADLINE` (25s); a normal Q&A returns
  synchronously and is spoken aloud. Anything slower (e.g. drafting an article) detaches, returns
  a short spoken ack, and on completion queues its text and rings the phone (`HARD_CAP` 15m).
  A request that **starts with** a `FORCE_BG_TRIGGERS` phrase ("test delay", "delay test",
  "delayed test", "test the delayed answer") is forced down the delayed path regardless of speed —
  the trigger is stripped and the rest becomes the question — so the delayed pipeline can be
  tested on demand. `/result/next` is FIFO, so drain stale results (hear "No new results.") first.
- **Completion → phone speaks (free path).** Pushcut's dynamic notification content is paid, so
  the server rings a **static** Pushcut notification as a doorbell and queues the reply; the
  Shortcut it launches fetches the words from `/result/next` and speaks them. Tapping the
  notification runs the Shortcut (auto-run needs paid Pushcut Automation Server). Telegram is the
  fallback channel (full text to read) when `PUSHCUT_API_KEY` is unset.
- `history.json` — rolling conversation history (auto-managed, gitignored)
- `results_queue.json` — pending finished-job results awaiting a `/result/next` fetch (gitignored)
- `activity.jsonl` — **append-only, never-trimmed** durable log of every completed `/ask`
  (Berlin timestamp, mode, request, reply); gitignored. Solves "the voice agent did X but
  there's no record" — `history.json` is only a rolling 10-turn window. For *what the agent
  did* (not just in/out), the system prompt also tells it to append a dated entry to the
  worked-on project's NOTES.md after any state-changing task.
- `.env` — `TAILSCALE_IP`, `PORT`, `API_KEY`, `LINEAR_API_KEY`, `PUSHCUT_API_KEY`
  (`PUSHCUT_NOTIFICATION` optional, defaults `voice-bridge-done`); plus a **vestigial**
  `ANTHROPIC_API_KEY` left from the first SDK-based draft — unused; the CLI path needs no key

## Run
Always-on as a **tmux service started over SSH** — **NOT launchd**. A gui-domain LaunchAgent
has no Full Disk Access and can't even read `.venv/pyvenv.cfg` under `~/Documents`
(`PermissionError` → crash loop); tmux-over-SSH gets the FDA umbrella + the claude file credential.
```bash
~/bin/start-voice-bridge.sh   # sources the auth token, activates the venv, restart loop
```
Recovery + machine kit: see `mac-mini-ops` RUNBOOK §5b/§5e. Re-run after each reboot.

## Endpoints
- `POST /ask` — `{"text": "..."}` → plain text reply (Bearer-auth'd; long tasks return an ack + run in background)
- `GET|POST /result/next` (alias `/results/next`) — pop the oldest queued background-job result (Bearer-auth'd; used by the Speak-Voice-Result Shortcut, which was built with the plural path)
- `DELETE /history` — wipe conversation history (Bearer-auth'd)
- `GET /health` — liveness check (open)

## Tailscale IP
100.65.52.120 (spinbear-mini)

## Port
8765
