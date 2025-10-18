import asyncio
import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .event_bus import EventBus

# Optional rebootpy import (no side effects on import)
try:  # pragma: no cover - optional dependency at runtime
    import reboot as _reboot  # type: ignore
except Exception:  # pragma: no cover - library might not be installed in CI
    _reboot = None  # type: ignore


# -----------------------------
# Lightweight commands framework
# -----------------------------

def command(name: Optional[str] = None, aliases: Optional[Sequence[str]] = None, help: Optional[str] = None):
    def decorator(func: Callable[..., Awaitable[Any]]):
        setattr(func, "_is_command", True)
        setattr(func, "_command_name", name or func.__name__)
        setattr(func, "_command_aliases", list(aliases or []))
        setattr(func, "_command_help", help or (func.__doc__ or "").strip())
        return func
    return decorator


@dataclass
class Command:
    name: str
    callback: Callable[..., Awaitable[Any]]
    cog: Any
    signature: inspect.Signature
    aliases: List[str]
    help: str


# -----------------------------
# Config and helpers
# -----------------------------

@dataclass
class BotConfig:
    command_prefix: str = "!"
    device_auths_path: Optional[Path] = None
    account_id: Optional[str] = None  # pick a specific account_id from device_auths.json
    log_level: int = logging.INFO


def load_device_auths(path: Path) -> Dict[str, Dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if text else {}
        # Normalize into {account_id: {device_id, account_id, secret}}
        if isinstance(data, dict) and "accounts" in data and isinstance(data["accounts"], list):
            # Shape from server/auth.py persistence: [{deviceId, accountId, secret, display_name, email}, ...]
            out: Dict[str, Dict[str, str]] = {}
            for item in data.get("accounts", []):
                a_id = item.get("account_id") or item.get("accountId")
                if not a_id:
                    continue
                out[a_id] = {
                    "device_id": item.get("device_id") or item.get("deviceId"),
                    "account_id": a_id,
                    "secret": item.get("secret"),
                }
            return out
        elif isinstance(data, dict):
            # Shape from AuthManager persistence: {key(email|accountId): {accountId, deviceId, secret, ...}}
            out2: Dict[str, Dict[str, str]] = {}
            for _k, _v in data.items():
                if not isinstance(_v, dict):
                    continue
                a_id = _v.get("account_id") or _v.get("accountId") or str(_k)
                d_id = _v.get("device_id") or _v.get("deviceId")
                secret = _v.get("secret")
                if not a_id or not d_id or not secret:
                    continue
                out2[a_id] = {"device_id": d_id, "account_id": a_id, "secret": secret}
            return out2
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return {}


def select_device_auth(device_auths: Dict[str, Dict[str, str]], account_id: Optional[str] = None) -> Optional[Dict[str, str]]:
    if not device_auths:
        return None
    if account_id and account_id in device_auths:
        return device_auths[account_id]
    # Fallback: return the first entry
    try:
        return next(iter(device_auths.values()))
    except StopIteration:
        return None


# -----------------------------
# Web context used by commands
# -----------------------------

class WebCtx:
    def __init__(self, send_func: Callable[[str], Awaitable[None]], author: Optional[Dict[str, str]] = None, bot: Optional["Bot"] = None) -> None:
        self._send_func = send_func
        self.author = author or {"id": "web", "display_name": "WebUser"}
        self.bot = bot

    async def send(self, text: str) -> None:
        await self._send_func(text)


# -----------------------------
# Example cogs mirroring cosmetics/party commands
# -----------------------------

class CosmeticCommands:
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot

    @command(help="Say hello to check connectivity")
    async def hello(self, ctx: WebCtx) -> None:
        await ctx.send("Hello! The bot is online.")

    @command(help="Change skin (cosmetic)")
    async def skin(self, ctx: WebCtx, content: str) -> None:
        self.bot.logger.info(f"[Cosmetic] Setting skin to: {content}")
        await ctx.send(f"Skin set to: {content}")

    @command(help="Play an emote")
    async def emote(self, ctx: WebCtx, content: str) -> None:
        self.bot.logger.info(f"[Cosmetic] Playing emote: {content}")
        await ctx.send(f"Emote: {content}")

    @command(help="Equip pickaxe")
    async def pickaxe(self, ctx: WebCtx, content: str) -> None:
        self.bot.logger.info(f"[Cosmetic] Equipping pickaxe: {content}")
        await ctx.send(f"Pickaxe: {content}")


class PartyCommands:
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot

    @command(help="Set ready state")
    async def ready(self, ctx: WebCtx) -> None:
        self.bot.logger.info("[Party] Setting ready state to ready")
        self.bot.party["bot_ready"] = True
        await self.bot.event_bus.publish("party.update", self.bot.get_party_status())
        await ctx.send("Ready!")

    @command(help="Set party privacy, e.g., !privacy public|friends|private")
    async def privacy(self, ctx: WebCtx, mode: str) -> None:
        mode_l = (mode or "").lower().strip()
        allowed = {"public", "friends", "private"}
        if mode_l not in allowed:
            await ctx.send(f"Invalid privacy mode. Allowed: {', '.join(sorted(allowed))}")
            return
        self.bot.logger.info(f"[Party] Setting privacy: {mode_l}")
        self.bot.party["privacy"] = mode_l
        await self.bot.event_bus.publish("party.update", self.bot.get_party_status())
        await ctx.send(f"Privacy set to: {mode_l}")

    @command(help="Set playlist by ID")
    async def playlist_id(self, ctx: WebCtx, content: str) -> None:
        pid = content.strip()
        self.bot.logger.info(f"[Party] Setting playlist_id: {pid}")
        self.bot.party["playlist"] = pid
        await self.bot.event_bus.publish("party.update", self.bot.get_party_status())
        await ctx.send(f"Playlist set: {pid}")

    @command(help="Stop the bot")
    async def stop(self, ctx: WebCtx) -> None:
        await ctx.send("Stopping bot...")
        await self.bot.stop()


# -----------------------------
# Core Bot wrapper with EventBus
# -----------------------------

class Bot:
    def __init__(self, *, config: BotConfig) -> None:
        self.logger = logging.getLogger("fortnite_web_bot")
        self.logger.setLevel(config.log_level)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
            self.logger.addHandler(handler)

        self.config = config
        self.event_bus = EventBus()

        # Command framework state
        self.command_prefix = config.command_prefix
        self._commands: Dict[str, Command] = {}
        self._aliases: Dict[str, str] = {}

        # Lifecycle state
        self._stopping = asyncio.Event()
        self.online: bool = False
        self.ready: bool = False

        # Party snapshot
        self.party: Dict[str, Any] = {
            "party_id": "local",
            "leader_id": "bot",
            "privacy": "private",
            "playlist": None,
            "in_match": False,
            "members": [
                {"id": "bot", "display_name": "Bot", "leader": True}
            ],
        }

        # rebootpy runtime fields
        self._reboot_client: Any = None
        self._device_auth_details: Optional[Dict[str, str]] = None

        # Preload device auth details if available
        if self.config.device_auths_path:
            auths = load_device_auths(self.config.device_auths_path)
            self._device_auth_details = select_device_auth(auths, self.config.account_id)

    # Back-compat for older servers: allow attaching a status emitter that mirrors event bus publications
    def set_status_emitter(self, emitter: Callable[[str, Dict[str, Any]], Awaitable[None]]) -> None:
        async def _relay(topic: str, payload: Dict[str, Any]) -> None:
            # Map topic -> kind (top-level key used previously)
            kind = "bot" if topic == "bot.status" else ("party" if topic.startswith("party.") else topic)
            try:
                await emitter(kind, payload)
            except Exception:
                pass

        # Fire-and-forget subscription (no handle stored; used transiently during app lifetime)
        asyncio.create_task(self.event_bus.subscribe("bot.status", _relay))
        asyncio.create_task(self.event_bus.subscribe("party.update", _relay))

    # ---- Commands API ----
    def add_cog(self, cog: Any) -> None:
        for attr_name in dir(cog):
            maybe = getattr(cog, attr_name)
            if callable(maybe) and getattr(maybe, "_is_command", False):
                name = getattr(maybe, "_command_name")
                aliases: List[str] = list(getattr(maybe, "_command_aliases", []))
                help_text: str = getattr(maybe, "_command_help", "")
                callback = getattr(cog, attr_name)
                sig = inspect.signature(callback)
                cmd = Command(name=name, callback=callback, cog=cog, signature=sig, aliases=aliases, help=help_text)
                self._commands[name] = cmd
                for al in aliases:
                    self._aliases[al] = name

    def get_command(self, name: str) -> Optional[Command]:
        if name in self._commands:
            return self._commands[name]
        if name in self._aliases:
            return self._commands.get(self._aliases[name])
        return None

    def list_commands(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for name, cmd in sorted(self._commands.items(), key=lambda kv: kv[0]):
            out.append({
                "name": name,
                "aliases": cmd.aliases,
                "help": cmd.help,
                "params": [p.name for p in list(cmd.signature.parameters.values())[1:]],
            })
        return out

    async def dispatch_line(self, line: str, ctx: WebCtx) -> None:
        line = (line or "").strip()
        if not line:
            return
        if not line.startswith(self.command_prefix):
            await ctx.send(f"Echo: {line}")
            return
        content = line[len(self.command_prefix):].strip()
        if not content:
            await ctx.send("No command specified.")
            return
        parts = content.split(maxsplit=1)
        name = parts[0].lower()
        arg_str = parts[1] if len(parts) > 1 else ""
        cmd = self.get_command(name)
        if not cmd:
            await ctx.send(f"Unknown command: {name}")
            return
        try:
            args = self._parse_args(cmd, arg_str)
        except ValueError as e:
            await ctx.send(f"Error: {e}")
            return
        try:
            await cmd.callback(ctx, *args)
        except Exception as e:  # pragma: no cover - defensive
            self.logger.exception("Command execution error")
            await ctx.send(f"An error occurred while executing '{name}': {e}")

    def _parse_args(self, cmd: Command, arg_str: str) -> List[Any]:
        sig = cmd.signature
        params = list(sig.parameters.values())
        if not params:
            return []
        # Skip ctx
        params = params[1:]

        if not params:
            if arg_str.strip():
                raise ValueError("This command takes no arguments.")
            return []

        if len(params) == 1:
            p = params[0]
            ann = p.annotation
            if ann is inspect._empty or ann is str or p.name == "content":
                return [arg_str]

        tokens = arg_str.split() if arg_str else []
        args: List[Any] = []
        idx = 0
        for p in params:
            ann = p.annotation
            has_default = p.default is not inspect._empty
            if idx >= len(tokens):
                if has_default:
                    args.append(p.default)
                    continue
                else:
                    raise ValueError(f"Missing required argument: {p.name}")
            raw = tokens[idx]
            idx += 1
            if ann in (inspect._empty, str):
                val = raw
            elif ann is int:
                try:
                    val = int(raw)
                except Exception:
                    raise ValueError(f"Argument '{p.name}' must be an integer.")
            elif ann is float:
                try:
                    val = float(raw)
                except Exception:
                    raise ValueError(f"Argument '{p.name}' must be a float.")
            elif ann is bool:
                lowered = raw.lower()
                if lowered in ("true", "t", "yes", "y", "1", "on", "ready"):
                    val = True
                elif lowered in ("false", "f", "no", "n", "0", "off", "unready"):
                    val = False
                else:
                    raise ValueError(f"Argument '{p.name}' must be a boolean (true/false).")
            else:
                val = raw
            args.append(val)

        # Glue remaining tokens to last string arg if present
        if idx < len(tokens):
            last = params[-1]
            if last.annotation in (inspect._empty, str):
                remaining = tokens[idx:]
                args[-1] = f"{args[-1]} {' '.join(remaining)}".strip()
        return args

    # ---- Lifecycle ----
    async def start(self) -> None:
        self.logger.info("Bot starting...")
        # If rebootpy is available and we have device auth, prepare client (no side effects beyond local state)
        if _reboot is not None and self._device_auth_details:
            try:
                # Resolve AdvancedAuth class dynamically
                advanced_auth_cls = getattr(_reboot, "AdvancedAuth", None) or getattr(getattr(_reboot, "auth", object), "AdvancedAuth", None)
                client_cls = getattr(_reboot, "Client", None)
                adv_auth = advanced_auth_cls(device_auth_details=self._device_auth_details) if advanced_auth_cls else None
                if client_cls and adv_auth:
                    # We intentionally do not connect to Epic services here to keep import/startup side-effect free for tests.
                    # Store a configured client instance for future extension.
                    self._reboot_client = client_cls(auth=adv_auth)
            except Exception:
                # Fallback to local-only behavior if rebootpy is not usable
                self._reboot_client = None
        self.online = True
        self.ready = True
        await self.event_bus.publish("bot.status", self.get_status())
        await self._stopping.wait()
        self.online = False
        self.ready = False
        await self.event_bus.publish("bot.status", self.get_status())
        self.logger.info("Bot stopped.")

    async def stop(self) -> None:
        self._stopping.set()

    # ---- Snapshots ----
    def get_status(self) -> Dict[str, Any]:
        return {
            "online": self.online,
            "ready": self.ready,
            "status": "ready" if self.ready else ("online" if self.online else "offline"),
        }

    def get_party_status(self) -> Dict[str, Any]:
        members = self.party.get("members", [])
        return {
            **self.party,
            "member_count": len(members),
        }


# -----------------------------
# Factory/entry points
# -----------------------------

def create_bot(config: Optional[BotConfig] = None) -> Bot:
    if config is None:
        config = BotConfig()
    bot = Bot(config=config)
    # Install cogs via our lightweight extension loader
    bot.add_cog(CosmeticCommands(bot))
    bot.add_cog(PartyCommands(bot))
    return bot


async def start_bot(bot: Optional[Bot] = None, *, config: Optional[BotConfig] = None) -> None:
    if bot is None:
        if config is None:
            config = BotConfig()
        bot = create_bot(config)
    await bot.start()


# -----------------------------
# Public helpers for server/state manager
# -----------------------------

def party_snapshot(bot: Bot) -> Dict[str, Any]:
    return bot.get_party_status()


def status_summary(bot: Bot) -> Dict[str, Any]:
    return bot.get_status()
