# AGENTS.md — voice-bridge

Simple project, no multi-agent complexity. One server process, one Claude API call per request.

If extending: keep the server stateless except for history.json. Tool use can be added to the /ask handler via the Anthropic tools API.
