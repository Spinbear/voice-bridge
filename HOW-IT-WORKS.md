# How voice-bridge works

A hands-free voice interface to Claude, running on a Mac mini (headless home server) and triggered from an iPhone or Apple Watch.

---

## What the user experiences

1. Say "Hey Siri, [shortcut name]" — Siri opens the iOS/watchOS shortcut.
2. Dictate a question or command.
3. The phone/Watch speaks the answer aloud in a few seconds (short answers) or rings with a notification that you tap to hear the reply (long tasks like writing an article).

---

## The pipeline, step by step

```
Voice → iOS Shortcut → voice-bridge server → Claude agent → spoken reply
```

### 1. Voice capture (iOS Shortcut)
An iOS Shortcut uses Apple's built-in **Dictate Text** action (synchronous — hands back the transcript inline). It then POSTs the transcript to the server:

```
POST http://<tailscale-ip>:8765/ask
Authorization: Bearer <API_KEY>
{"text": "your question here"}
```

The request travels over **Tailscale** (private VPN mesh) — the server is never exposed to the public internet.

### 2. Server (`server.py`)
A **FastAPI** app running on the Mac mini, bound to the Tailscale IP only. It receives the question and shells out to the **Claude Code CLI** (`claude -p`), which runs as a project-aware agent with access to:
- Read, search, and grep files in `~/Documents/Projects/`
- Run permitted shell commands (process listing, starting/stopping services, reading logs)
- Write and edit files when asked (e.g. drafting a blog article)
- Query the Linear API for project management tasks

It cannot: delete files, push git commits, install software, or deploy to a VPS unless the word "deploy" is explicit.

### 3. Short vs. long tasks
The server waits up to **25 seconds** for a reply.

- **Short Q&A** (returns in time): the plain-text answer goes straight back in the HTTP response. The Shortcut reads it aloud via a Speak Text action.
- **Long task** (drafting an article, research): the job detaches into the background (up to 15 minutes). The server immediately returns a spoken acknowledgement: *"I'm checking. I'll get back to you once done."* When finished, it queues the reply and sends a **Pushcut notification** to the phone as a doorbell. Tapping the notification runs a second shortcut (`Agent Reply`) which fetches the text from `/result/next` and speaks it.

### 4. Conversation history
The server maintains a rolling 10-turn conversation window (`history.json`), so follow-up questions have context. Every completed request is also appended to an append-only activity log (`activity.jsonl`) for a permanent record.

---

## Components you need to replicate this

| Component | What it does |
|---|---|
| **Mac mini (or always-on Mac)** | Runs the server and the Claude agent |
| **Tailscale** | Private network so the server is reachable from the phone without port-forwarding |
| **Claude Code CLI** | The AI brain — uses a Claude Code subscription, no separate API key needed |
| **iOS Shortcut (Ask)** | Captures voice, POSTs to server, speaks the reply |
| **iOS Shortcut (Agent Reply)** | Fetches and speaks long-task results when tapped |
| **Pushcut** (free tier) | Sends the "done" doorbell notification to the phone |
| **`.env` file on the server** | Holds the Tailscale IP, port, API key, and optional Pushcut key |

---

## What the agent is and isn't

The Claude agent running here is a **project-aware assistant**, not a generic chatbot. It reads the actual project files on this machine to ground its answers (NOTES.md, READMEs, source files) before replying. It will refuse to guess about private project details it can't verify from files.

It is deliberately restricted: no deleting files, no git writes, no installing packages, no outbound network requests beyond what's explicitly permitted. These limits are enforced in the system prompt and cannot be overridden by voice.

---

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /ask` | Bearer | Submit a question or command |
| `GET /result/next` | Bearer | Fetch the oldest queued long-task reply |
| `DELETE /history` | Bearer | Wipe the conversation window |
| `GET /health` | Open | Liveness check |

---

## Running the server

The server runs in a **tmux session** started over SSH — not as a LaunchAgent (macOS LaunchAgents in the GUI domain lack the file-system access the Claude CLI needs).

```bash
~/bin/start-voice-bridge.sh   # activates the venv and runs a restart loop
```

After a reboot, SSH in and run this script again. Recovery notes are in `mac-mini-ops RUNBOOK §5b`.
