# Fortnite Bot Web UI (FastAPI)

Interactive web interface to control a Fortnite bot from the browser with live status dashboard and Epic DeviceAuth login flow. Ships with a lightweight command framework compatible with a future `rebootpy` integration.

## Features
- Interactive SPA-like UI served by FastAPI (SSR template with vanilla JS)
- Real-time chat over WebSocket (send/receive) with broadcast to all clients
- Command help/autocomplete-ready endpoint exposing command registry
- Live status dashboard:
  - Bot: online/ready
  - Auth: not authenticated, pending device code, authenticated as <display_name>, last error
  - Party: members list, count, privacy, playlist
- Epic DeviceAuth login flow (device code):
  - `GET /auth/start` -> returns `verification_uri_complete` for the UI to open
  - Server polls until the device code is approved; creates DeviceAuth and persists to `device_auths.json`
  - `GET /auth/status` -> returns current auth status
  - `POST /auth/logout` -> clears active session
- Localhost-only by default; simple per-WS cooldown for spam prevention

## Project Structure
- `bot/fortnite_bot.py`
  - Minimal bot, command registry, and cogs (`CosmeticCommands`, `PartyCommands`)
  - `create_bot()` and `start_bot()` exposed for server startup
  - Status emitter so the server can broadcast bot/party deltas in real time
- `server/main.py`
  - FastAPI app with REST endpoints, WebSocket `/ws`, live status broadcasting
- `server/auth.py`
  - Epic DeviceAuth flow helper (see licensing notice inside the file)
- `server/webctx.py`
  - Thin wrapper re-exporting `WebCtx` to keep a single context implementation
- `templates/index.html` + `static/*`
  - SSR web UI + scripts and styles (no build step required)
- `run.py`
  - Convenience script to launch the FastAPI app via `uvicorn`

## Endpoints
- `GET /` – Web UI
- `GET /ws` – WebSocket for chat and live status updates
- `GET /auth/start` – Begin Epic device auth; JSON includes `verification_uri_complete`
- `GET /auth/status` – Current auth state
- `POST /auth/logout` – Clear active auth state
- `GET /commands` – Bot command registry (names, aliases, help)

## Requirements
- Python 3.10+
- See `requirements.txt`

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

Run the web server (localhost-only by default):

```bash
uvicorn server.main:app --reload
# or
python run.py
```

Open http://localhost:8000 and test commands like:
- `!hello`
- `!skin Nog Ops`
- `!ready`
- `!privacy private`

## Configuration
- `COMMAND_PREFIX`: Command prefix (default `!`).
- `EPIC_SWITCH_BASIC`: Optional override for the Epic SWITCH OAuth client Basic token (base64(client_id:client_secret)).
- `EPIC_ANDROID_BASIC`: Optional override for the Epic ANDROID OAuth client Basic token (base64(client_id:client_secret)).

Notes:
- The server will try SWITCH first, then ANDROID. If an override is not provided via environment variables, embedded defaults will be used where available.
- For localhost safety, the device-code flow does not require or use any redirect/callback URLs.

## Notes for integrating with a real Fortnite bot
- The current commands simulate actions and log to the console. Integrate your Fortnite SDK (e.g., `rebootpy`) inside `Bot` and its cogs. Do not change command signatures so that both in-game/DM and web UI control remain compatible.
- `WebCtx` provides `send(text)` and `author` to mirror typical bot framework `Context`.

## Security
- CORS intentionally limited to same-origin. Bind uvicorn to `127.0.0.1` during development.
- The device auth file `device_auths.json` is ignored by git (see `.gitignore`).

## License
- Project code: MIT (see LICENSE)
- `server/auth.py`: includes a license/notice reflecting Commons Clause + Apache 2.0 Modified terms commonly used by device-auth generators. See NOTICE for details.
