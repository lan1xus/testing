import asyncio
import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence


# Command decorator and simple command framework

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


class Bot:
    def __init__(self, command_prefix: str = "!") -> None:
        self.logger = logging.getLogger("fortnite_web_bot")
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
        self.logger.addHandler(handler)

        self.command_prefix = command_prefix
        self._commands: Dict[str, Command] = {}
        self._aliases: Dict[str, str] = {}
        self._started = asyncio.Event()
        self._stopping = asyncio.Event()

        # Runtime state
        self.online: bool = False
        self.ready: bool = False
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

        # Optional async status emitter (set by server)
        self._status_emitter: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None

    def set_status_emitter(self, emitter: Callable[[str, Dict[str, Any]], Awaitable[None]]) -> None:
        self._status_emitter = emitter

    async def _emit(self, kind: str, payload: Dict[str, Any]) -> None:
        if self._status_emitter:
            try:
                await self._status_emitter(kind, payload)
            except Exception:
                self.logger.exception("Status emitter failed")

    def add_cog(self, cog: Any) -> None:
        for attr_name in dir(cog):
            maybe = getattr(cog, attr_name)
            if callable(maybe) and getattr(maybe, "_is_command", False):
                name = getattr(maybe, "_command_name")
                aliases: List[str] = list(getattr(maybe, "_command_aliases", []))
                help_text: str = getattr(maybe, "_command_help", "")
                # Bind the method to the cog (in case it's a function on the class)
                callback = getattr(cog, attr_name)
                sig = inspect.signature(callback)
                cmd = Command(name=name, callback=callback, cog=cog, signature=sig, aliases=aliases, help=help_text)
                if name in self._commands:
                    self.logger.warning(f"Command {name} is already registered; overriding.")
                self._commands[name] = cmd
                for al in aliases:
                    self._aliases[al] = name
                self.logger.debug(f"Registered command: {name} (aliases: {aliases})")

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

    async def start(self) -> None:
        self.logger.info("Bot starting...")
        self.online = True
        self.ready = True
        await self._emit("bot", {"online": self.online, "ready": self.ready})
        await self._stopping.wait()
        self.online = False
        self.ready = False
        await self._emit("bot", {"online": self.online, "ready": self.ready})
        self.logger.info("Bot stopped.")

    async def stop(self) -> None:
        self._stopping.set()

    def get_status(self) -> Dict[str, Any]:
        return {
            "online": self.online,
            "ready": self.ready,
            "status": "ready" if self.ready else ("online" if self.online else "offline"),
        }

    def get_party_status(self) -> Dict[str, Any]:
        # Derive member_count
        members = self.party.get("members", [])
        return {
            **self.party,
            "member_count": len(members),
        }

    async def dispatch_line(self, line: str, ctx: "WebCtx") -> None:
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
        except Exception as e:
            self.logger.exception("Command execution error")
            await ctx.send(f"An error occurred while executing '{name}': {e}")

    def _parse_args(self, cmd: Command, arg_str: str) -> List[Any]:
        sig = cmd.signature
        params = list(sig.parameters.values())
        # Expect first parameter to be ctx; skip that one
        if not params:
            return []
        if params[0].name != "ctx":
            # Allow methods that may still include ctx but under a different name
            pass
        params = params[1:]

        # If there are no further parameters
        if not params:
            if arg_str.strip():
                raise ValueError("This command takes no arguments.")
            return []

        # Catch-all string if a single string parameter or a parameter named 'content'
        if len(params) == 1:
            p = params[0]
            ann = p.annotation
            if ann is inspect._empty or ann is str or p.name == "content":
                return [arg_str]

        # Otherwise split by whitespace and coerce per param types
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
            # Coercion
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
                # Fallback to string
                val = raw
            args.append(val)

        # If there are extra tokens, append them to the last string parameter as a tail if possible
        if idx < len(tokens):
            # Check if last param is str
            last = params[-1]
            if last.annotation in (inspect._empty, str):
                remaining = tokens[idx:]
                args[-1] = f"{args[-1]} {' '.join(remaining)}".strip()
            else:
                # Ignore extras silently
                pass
        return args


# Minimal WebCtx to be used by the server and commands
class WebCtx:
    def __init__(self, send_func: Callable[[str], Awaitable[None]], author: Optional[Dict[str, str]] = None, bot: Optional[Bot] = None) -> None:
        self._send_func = send_func
        self.author = author or {"id": "web", "display_name": "WebUser"}
        self.bot = bot

    async def send(self, text: str) -> None:
        await self._send_func(text)


# Example cogs mimicking the Fortnite bot's cosmetics/party commands
class CosmeticCommands:
    def __init__(self, bot: Bot) -> None:
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
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    @command(help="Set ready state")
    async def ready(self, ctx: WebCtx) -> None:
        self.bot.logger.info("[Party] Setting ready state to ready")
        # Track as part of party state for UI
        self.bot.party["bot_ready"] = True
        await self.bot._emit("party", self.bot.get_party_status())
        await ctx.send("Ready!")

    @command(help="Set party privacy, e.g., !privacy public|friends|private")
    async def privacy(self, ctx: WebCtx, mode: str) -> None:
        mode_l = mode.lower().strip()
        allowed = {"public", "friends", "private"}
        if mode_l not in allowed:
            await ctx.send(f"Invalid privacy mode. Allowed: {', '.join(sorted(allowed))}")
            return
        self.bot.logger.info(f"[Party] Setting privacy: {mode_l}")
        self.bot.party["privacy"] = mode_l
        await self.bot._emit("party", self.bot.get_party_status())
        await ctx.send(f"Privacy set to: {mode_l}")

    @command(help="Set playlist by ID")
    async def playlist_id(self, ctx: WebCtx, content: str) -> None:
        pid = content.strip()
        self.bot.logger.info(f"[Party] Setting playlist_id: {pid}")
        self.bot.party["playlist"] = pid
        await self.bot._emit("party", self.bot.get_party_status())
        await ctx.send(f"Playlist set: {pid}")

    @command(help="Stop the bot")
    async def stop(self, ctx: WebCtx) -> None:
        await ctx.send("Stopping bot...")
        await self.bot.stop()


def create_bot() -> Bot:
    prefix = os.environ.get("COMMAND_PREFIX", "!")
    bot = Bot(command_prefix=prefix)
    bot.add_cog(CosmeticCommands(bot))
    bot.add_cog(PartyCommands(bot))
    return bot


async def start_bot(bot: Optional[Bot] = None) -> None:
    if bot is None:
        bot = create_bot()
    await bot.start()
