import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bot.fortnite_bot import create_bot, start_bot, WebCtx, Bot
from server.auth import AuthManager

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

logger = logging.getLogger("fortnite_web_server")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
logger.addHandler(_handler)

app = FastAPI(title="Fortnite Bot Web UI")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class BroadcastManager:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self._clients:
                self._clients.remove(ws)

    async def broadcast_text(self, message: str) -> None:
        async with self._lock:
            clients = list(self._clients)
        to_remove: Set[WebSocket] = set()
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                to_remove.add(ws)
        if to_remove:
            async with self._lock:
                for ws in to_remove:
                    if ws in self._clients:
                        self._clients.remove(ws)

    async def broadcast_json(self, payload: Dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        to_remove: Set[WebSocket] = set()
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                to_remove.add(ws)
        if to_remove:
            async with self._lock:
                for ws in to_remove:
                    if ws in self._clients:
                        self._clients.remove(ws)


broadcaster = BroadcastManager()
bot: Bot = create_bot()
_bot_task: asyncio.Task | None = None
_auth_task: asyncio.Task | None = None
auth_manager = AuthManager(storage_path=BASE_DIR / "device_auths.json")


state: Dict[str, Any] = {
    "bot": bot.get_status(),
    "auth": auth_manager.status(),
    "party": bot.get_party_status(),
}


async def emit_status(kind: str, payload: Dict[str, Any]) -> None:
    # Update in-memory state and broadcast a delta
    state[kind] = payload
    await broadcaster.broadcast_json({"type": "status", "data": {kind: payload}})


@app.on_event("startup")
async def on_startup() -> None:
    global _bot_task, _auth_task
    logger.info("Starting bot background task...")
    bot.set_status_emitter(emit_status)
    _bot_task = asyncio.create_task(start_bot(bot))

    async def auth_watchdog() -> None:
        last_snapshot = None
        while True:
            await asyncio.sleep(1.0)
            snap = auth_manager.status()
            if snap != last_snapshot:
                last_snapshot = snap
                state["auth"] = snap
                await broadcaster.broadcast_json({"type": "status", "data": {"auth": snap}})

    _auth_task = asyncio.create_task(auth_watchdog())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _bot_task, _auth_task
    logger.info("Shutting down bot...")
    await bot.stop()
    if _bot_task:
        try:
            await asyncio.wait_for(_bot_task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Bot task did not stop in time.")
    if _auth_task:
        _auth_task.cancel()
    await auth_manager.close()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/auth/status")
async def auth_status() -> JSONResponse:
    return JSONResponse(auth_manager.status())


@app.post("/auth/logout")
async def auth_logout() -> JSONResponse:
    await auth_manager.logout()
    snap = auth_manager.status()
    state["auth"] = snap
    await broadcaster.broadcast_json({"type": "status", "data": {"auth": snap}})
    return JSONResponse({"ok": True})


@app.get("/auth/start")
async def auth_start() -> JSONResponse:
    res = await auth_manager.start()
    # Broadcast status update
    snap = auth_manager.status()
    state["auth"] = snap
    await broadcaster.broadcast_json({"type": "status", "data": {"auth": snap}})
    return JSONResponse(res)


@app.get("/commands")
async def list_commands() -> JSONResponse:
    return JSONResponse({"commands": bot.list_commands()})


@app.get("/status")
async def full_status() -> JSONResponse:
    # Return a full snapshot of server-side state
    return JSONResponse(state)


RATE_LIMIT_SECONDS = 0.3
_last_message_at: dict[int, float] = {}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await broadcaster.connect(ws)
    # Send welcome and initial status snapshot
    await ws.send_json({"type": "chat", "text": "Connected to Fortnite Bot Web UI. Type !hello to test."})
    await ws.send_json({"type": "status", "data": state})
    try:
        while True:
            text = await ws.receive_text()
            now = time.monotonic()
            key = id(ws)
            last = _last_message_at.get(key, 0)
            if now - last < RATE_LIMIT_SECONDS:
                await ws.send_json({"type": "chat", "text": "Slow down! You're sending messages too quickly."})
                continue
            _last_message_at[key] = now

            # Build a context that broadcasts chat messages to all clients
            async def _send(msg: str) -> None:
                await broadcaster.broadcast_json({"type": "chat", "text": msg})

            ctx = WebCtx(send_func=_send, author={"id": str(key), "display_name": "WebUser"}, bot=bot)

            # Echo the user's message to all first
            await broadcaster.broadcast_json({"type": "chat", "text": f"You: {text}"})

            # Dispatch to the bot
            await bot.dispatch_line(text, ctx)
    except WebSocketDisconnect:
        await broadcaster.disconnect(ws)
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
        await broadcaster.disconnect(ws)
