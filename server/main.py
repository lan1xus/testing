import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Set, List, Callable, Awaitable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bot.fortnite_bot import (
    create_bot,
    start_bot,
    WebCtx,
    Bot,
    BotConfig,
    party_snapshot,
    status_summary,
)
from server.auth import AuthManager

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

logger = logging.getLogger("fortnite_web_server")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
if not logger.handlers:
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

# Build bot config and instance
_bot_config = BotConfig(
    command_prefix=os.environ.get("COMMAND_PREFIX", "!"),
    device_auths_path=BASE_DIR / "device_auths.json",
)
bot: Bot = create_bot(_bot_config)

_bot_task: asyncio.Task | None = None
_auth_task: asyncio.Task | None = None
_bus_unsubs: List[Callable[[], Awaitable[None]]] = []
auth_manager = AuthManager(storage_path=BASE_DIR / "device_auths.json")

state: Dict[str, Any] = {
    "bot": status_summary(bot),
    "auth": auth_manager.status(),
    "party": party_snapshot(bot),
}


async def _on_event(topic: str, payload: Dict[str, Any]) -> None:
    if topic == "bot.status":
        state["bot"] = payload
        await broadcaster.broadcast_json({"type": "status", "data": {"bot": payload}})
    elif topic.startswith("party."):
        state["party"] = payload
        await broadcaster.broadcast_json({"type": "status", "data": {"party": payload}})
    else:
        # Generic forwarding under "evt" type for debugging
        await broadcaster.broadcast_json({"type": "evt", "topic": topic, "data": payload})


@app.on_event("startup")
async def on_startup() -> None:
    global _bot_task, _auth_task, _bus_unsubs
    logger.info("Starting bot background task...")
    # Subscribe to bot event bus
    unsub1 = await bot.event_bus.on("bot.status", _on_event)
    unsub2 = await bot.event_bus.on("party.update", _on_event)
    _bus_unsubs = [unsub1, unsub2]
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
                # If authentication just completed and the bot is not connected yet,
                # refresh device auths from disk and attempt to connect.
                try:
                    if snap.get("authenticated") and not bot.online:
                        bot.reload_device_auths()
                        await bot.ensure_connected()
                except Exception as e:
                    logger.warning("Failed to auto-connect bot after auth: %s", e)

    _auth_task = asyncio.create_task(auth_watchdog())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _bot_task, _auth_task, _bus_unsubs
    logger.info("Shutting down bot...")
    await bot.stop()
    if _bot_task:
        try:
            await asyncio.wait_for(_bot_task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Bot task did not stop in time.")
    if _auth_task:
        _auth_task.cancel()
    # Unsubscribe from event bus
    for unsub in _bus_unsubs:
        try:
            await unsub()
        except Exception:
            pass
    _bus_unsubs = []
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
