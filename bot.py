from datetime import datetime, timezone
import logging
from pathlib import Path
import discord
from discord.ext import commands
import os

from config import Config, CurrConfig
from lib.wynn_api.requestor import Requestor
from orm import close_db, init_db


# Subdirectories under cogs/ that are *not* cog extensions but rather
# helper packages imported transitively by a host cog (e.g. cogs.returns'
# week_*.py modules registered via the @register decorator). Recursively
# loading their .py files as discord.py extensions would fail with
# NoEntryPointError -- the catch in _load_cogs would also handle it, but
# explicit skipping keeps the log clean and the intent obvious.
COG_SKIP_DIRS: set[str] = {"returns"}

logger = logging.getLogger("dazebot.bot")
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class NOSQL:
    LAST_DEAD_ALERT = datetime.fromtimestamp(0, tz=timezone.utc)
    LAST_CAP_ALERT = datetime.fromtimestamp(0, tz=timezone.utc)
    LAST_CHECK_GUILD = datetime.fromtimestamp(
        0, tz=timezone.utc
    )  # tasks might keep repeating even on cog reload? if yes, implement this variable where needed


class Bot(commands.Bot):
    config: Config = CurrConfig
    nosql: NOSQL

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.nosql = NOSQL()

    async def setup_hook(self):
        try:
            await init_db()
            logger.info("Connected to database")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
        # Apply persisted /config overrides on top of compiled defaults.
        try:
            from lib.runtime_config import load_overrides

            await load_overrides()
        except Exception as e:
            logger.error(f"Failed to load runtime config overrides: {e}")
        # Register persistent views so buttons survive restarts.
        try:
            from lib.first_install_view import FirstInstallView, LinkFallbackView

            self.add_view(FirstInstallView())
            self.add_view(LinkFallbackView())
        except Exception as e:
            logger.error(f"Failed to register FirstInstallView: {e}")
        try:
            from cogs.integrations.vetsmod import VetsmodFallbackView

            self.add_view(VetsmodFallbackView())
        except Exception as e:
            logger.error(f"Failed to register VetsmodFallbackView: {e}")
        await self._load_cogs()

    async def _load_cogs(self):
        """Load every .py file under cogs/ as a discord.py extension.

        Walks subdirectories so cogs can be organized by domain. Skips
        ``__*`` files (dunder modules / pycache), any directory in
        ``COG_SKIP_DIRS``, and modules without a ``setup()`` entry point
        (caught via :class:`commands.NoEntryPointError`).
        """
        root = Path("./cogs")
        for path in root.rglob("*.py"):
            if path.name.startswith("__"):
                continue
            rel = path.relative_to(root)
            if any(part in COG_SKIP_DIRS for part in rel.parts):
                continue
            module = ".".join(("cogs", *rel.with_suffix("").parts))
            try:
                await self.load_extension(module)
                logger.info(f"Loaded cog: {module}")
            except commands.NoEntryPointError:
                logger.debug(f"Skipping non-extension module: {module}")
            except Exception as e:
                logger.error(f"Failed to load cog {module}: {e}")

    async def on_ready(self):
        logger.info(f"{self.user} has connected to Discord!")
        logger.info(f"Bot is in {len(self.guilds)} guild(s)")
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

    async def close(self):
        logger.info("Shutting down bot...")
        await close_db()
        await Requestor().close()
        await super().close()

    async def on_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        if isinstance(error, (commands.RangeError,)):
            await ctx.send(f"❌ {error}")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Invalid argument: {error}")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Missing argument: {error.param.name}")
        elif isinstance(error, commands.ArgumentParsingError):
            await ctx.send(
                f"❌ Couldn't parse arguments: {error}\n"
                "Tip: prefix commands are positional — `name:value` syntax only works "
                "in the slash form (e.g. `/return action:announce message:hello world`)."
            )
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            print(f"Unhandled error: {error}")
            raise error


# if __name__ == "__main__":
#     bot = Bot(command_prefix=os.environ["PREFIX"], intents=intents)
#     bot.run(os.environ["TOKEN"])

from discord.utils import _ColourFormatter  # private but stable enough


class LineColourFormatter(_ColourFormatter):
    def format(self, record: logging.LogRecord) -> str:
        location = f"{record.filename}:{record.lineno}"
        funcname = f"({record.funcName})"
        record.name = f"{f'[{record.name}]':<30} {location:<20} {funcname:<15}"
        return super().format(record)


if __name__ == "__main__":
    is_debug = os.environ.get("DEBUG") == "true"
    level = logging.DEBUG if is_debug else logging.INFO

    discord.utils.setup_logging(level=level, formatter=LineColourFormatter())

    logging.getLogger().setLevel(logging.WARNING)

    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("dazebot").setLevel(logging.DEBUG if is_debug else logging.INFO)

    logger.info(
        f"Log level is set to {level}. (If not already the case) It can be set to debug mode by setting the environment variable `DEBUG` to `true`, see `.env.example`"
    )

    bot = Bot(command_prefix=os.environ["PREFIX"], intents=intents)
    bot.run(os.environ["TOKEN"], log_handler=None)
