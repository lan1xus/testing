# Fortnite Bot Web UI (FastAPI)

This project provides a simple web UI to control a Fortnite bot via browser-style chat commands. It exposes a FastAPI server with a WebSocket endpoint and a minimal command framework that mimics common commands such as `!hello`, `!skin`, `!emote`, `!pickaxe`, `!ready`, `!privacy`, `!playlist_id`, and `!stop`.

Note: This repository ships with a lightweight, built-in command framework for the web UI. It is structured to be easily integrated with an actual Fortnite SDK (e.g., rebootpy) later. The current commands log actions and respond to the UI but do not perform real Fortnite party interactions.

## Features
- FastAPI app serving a single-page chat UI
- WebSocket-based chat with broadcast to multiple clients
- Minimal `WebCtx` with `send(text)` for command responses
- Command prefix configurable via `COMMAND_PREFIX` env var (default: `!`)

## Project Structure
- `bot/fortnite_bot.py`
  - Minimal bot, command registry, and cogs (`CosmeticCommands`, `PartyCommands`)
  - `create_bot()` and `start_bot()` exposed for server startup
- `server/main.py`
  - FastAPI app with routes and WebSocket handling
- `server/webctx.py`
  - Thin wrapper re-exporting `WebCtx` to keep a single context implementation
- `templates/index.html`
  - Chat UI page
- `static/app.js`, `static/styles.css`
  - Frontend logic and styling
- `run.py`
  - Convenience script to launch the FastAPI app via `uvicorn`

## Requirements
- Python 3.10+
- See `requirements.txt`

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

Run the web server:

```bash
uvicorn server.main:app --reload
# or
python run.py
```

Open http://localhost:8000 and test commands like:
- `!hello`
- `!skin Nog Ops`
- `!emote Floss`
- `!pickaxe Candy Axe`
- `!ready`
- `!privacy public`
- `!playlist_id playlist_defaultsolo`
- `!stop`

## Notes for integrating with a real Fortnite bot
- The `WebCtx` class provides a `send(text)` method that commands use to reply. This mirrors the `ctx.send(...)` pattern found in common bot frameworks and should be straightforward to adapt.
- The current code does not establish any Fortnite session or device authentication. To integrate with an SDK such as `rebootpy`, refactor `bot/fortnite_bot.py` so the `Bot` class internally manages the SDK client and cogs call into it. Keep `create_bot()` and `start_bot()` as the public API used by the FastAPI app.
- Keep the `COMMAND_PREFIX` the same across CLI and web usage to avoid confusion.

## Safety & Rate Limiting
- The WebSocket endpoint applies a simple per-connection cooldown to avoid spam.

## License
MIT
