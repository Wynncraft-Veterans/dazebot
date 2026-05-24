"""Runtime-config cog: ``/config`` and ``/alerts`` admin groups.

``/config`` is the generic key/value override surface backed by
:mod:`lib.runtime_config`. ``/alerts`` is a thin convenience wrapper
that sets the dead/full guild-alert keys without forcing the admin to
remember their names.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Optional

from discord import app_commands
from discord.ext import commands

from bot import Bot
from config import CurrConfig
from lib import runtime_config
from lib.auth import is_admin


logger = logging.getLogger("dazebot.cogs.membership.runtime_config_cog")


# Long enough that the throttle effectively silences the alert, while
# still finite/resettable. ~10 years.
_ALERTS_MUTE_SECONDS = 10 * 365 * 24 * 3600


class RuntimeConfigCog(commands.Cog):
    """Admin commands to view/modify runtime config and guild alerts."""

    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("RuntimeConfig cog initialized")

    # ---------- /config ----------

    @commands.hybrid_group(name="config", description="(Admin) View/modify runtime config.")
    @is_admin()
    async def config_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Use `/config list`, `/config get <key>`, `/config set <key> <value>`, `/config reset <key>`."
            )

    @config_group.command(name="list")
    async def config_list(self, ctx: commands.Context):
        keys = runtime_config.list_keys()
        chunks: list[str] = []
        cur = ""
        for k in keys:
            try:
                v = runtime_config.get_value(k)
            except Exception:
                continue
            line = f"`{k}` = `{v}`\n"
            if len(cur) + len(line) > 1800:
                chunks.append(cur)
                cur = ""
            cur += line
        if cur:
            chunks.append(cur)
        if not chunks:
            await ctx.reply("(no overridable keys)")
            return
        for c in chunks:
            await ctx.send(c)

    @config_group.command(name="get")
    async def config_get(self, ctx: commands.Context, key: str):
        if not hasattr(CurrConfig, key):
            await ctx.reply(f"Unknown key: `{key}`.")
            return
        try:
            v = runtime_config.get_value(key)
        except Exception as e:
            await ctx.reply(f"Error: {e}")
            return
        await ctx.reply(f"`{key}` = `{v}`")

    @config_group.command(name="set")
    async def config_set(self, ctx: commands.Context, key: str, value: str):
        try:
            new = await runtime_config.set_override(key, value)
        except (KeyError, PermissionError, ValueError) as e:
            await ctx.reply(f"❌ {e}")
            return
        await ctx.reply(f"✅ `{key}` = `{new}` (persisted)")

    @config_group.command(name="reset")
    async def config_reset(self, ctx: commands.Context, key: str):
        await runtime_config.clear_override(key)
        await ctx.reply(
            f"✅ Cleared override for `{key}`. Restart bot for compiled default to take effect."
        )

    # ---------- /alerts (shortcuts on top of /config for the guild dead/full alerts) ----------

    @commands.hybrid_group(name="alerts", description="(Admin) Mute / unmute / tune guild dead+full+hiatus alerts.")
    @is_admin()
    async def alerts_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Use `/alerts status`, `/alerts mute [duration_hours]`, `/alerts unmute`, "
                "`/alerts thresholds <dead_when> <full_when>`, "
                "`/alerts hiatus_mute`, `/alerts hiatus_unmute`."
            )

    @alerts_group.command(
        name="status",
        description="Show current dead/full alert thresholds, throttle deltas, and hiatus alert state.",
    )
    async def alerts_status(self, ctx: commands.Context):
        dead_when = runtime_config.get_value("GUILD_DEAD_WHEN")
        full_slots_remaining = runtime_config.get_value("GUILD_FULL_SLOTS_REMAINING")
        dead_delta = runtime_config.get_value("GUILD_DEAD_ALERT_DELTA")
        full_delta = runtime_config.get_value("GUILD_FULL_ALERT_DELTA")
        hiatus_enabled = runtime_config.get_value("HIATUS_ALERTS_ENABLED")

        def _muted(delta: timedelta) -> str:
            return " ⛔ muted" if delta.total_seconds() >= 365 * 24 * 3600 else ""

        hiatus_state = "🔊 enabled" if hiatus_enabled else "🔇 muted"
        await ctx.reply(
            "**Guild alert config**\n"
            f"• Dead alert: fires when online ≤ `{dead_when}`, "
            f"throttled `{dead_delta}`{_muted(dead_delta)}\n"
            f"• Full alert: fires when ≤ `{full_slots_remaining}` slot(s) remain "
            f"(cap derived from guild level), throttled `{full_delta}`{_muted(full_delta)}\n"
            f"• Hiatus-spotted alert: {hiatus_state} (24h per-user cooldown)"
        )

    @alerts_group.command(
        name="mute",
        description="Silence dead+full guild alerts (default: ~forever).",
    )
    @app_commands.describe(duration_hours="How long to mute, in hours. Omit for ~forever.")
    async def alerts_mute(self, ctx: commands.Context, duration_hours: Optional[float] = None):
        seconds = int(duration_hours * 3600) if duration_hours else _ALERTS_MUTE_SECONDS
        try:
            await runtime_config.set_override("GUILD_DEAD_ALERT_DELTA", str(seconds))
            await runtime_config.set_override("GUILD_FULL_ALERT_DELTA", str(seconds))
        except (KeyError, PermissionError, ValueError) as e:
            await ctx.reply(f"❌ {e}")
            return
        td = timedelta(seconds=seconds)
        await ctx.reply(f"🔇 Muted dead+full alerts for `{td}`. Use `/alerts unmute` to restore.")

    @alerts_group.command(
        name="unmute",
        description="Restore the compiled default throttle for dead+full alerts.",
    )
    async def alerts_unmute(self, ctx: commands.Context):
        await runtime_config.clear_override("GUILD_DEAD_ALERT_DELTA")
        await runtime_config.clear_override("GUILD_FULL_ALERT_DELTA")
        await ctx.reply(
            "🔊 Cleared dead+full alert throttle overrides. "
            "Compiled defaults take effect after the next bot restart; "
            "the running process keeps the muted values until then."
        )

    @alerts_group.command(
        name="hiatus_mute",
        description="Silence hiatus-spotted alerts (stops the bulk /v3/player poll too).",
    )
    async def alerts_hiatus_mute(self, ctx: commands.Context):
        try:
            await runtime_config.set_override("HIATUS_ALERTS_ENABLED", "false")
        except (KeyError, PermissionError, ValueError) as e:
            await ctx.reply(f"❌ {e}")
            return
        await ctx.reply(
            "🔇 Muted hiatus-spotted alerts. The bulk-endpoint poll is suspended; "
            "the server_watcher HIATUS scope continues writing activity data but "
            "won't post alerts. Use `/alerts hiatus_unmute` to restore."
        )

    @alerts_group.command(
        name="hiatus_unmute",
        description="Re-enable hiatus-spotted alerts.",
    )
    async def alerts_hiatus_unmute(self, ctx: commands.Context):
        try:
            await runtime_config.set_override("HIATUS_ALERTS_ENABLED", "true")
        except (KeyError, PermissionError, ValueError) as e:
            await ctx.reply(f"❌ {e}")
            return
        await ctx.reply("🔊 Re-enabled hiatus-spotted alerts.")

    @alerts_group.command(
        name="thresholds",
        description="Set dead-when (online <=) and full-slots-remaining (n) thresholds.",
    )
    @app_commands.describe(
        dead_when="Online-player count at or below which the dead alert fires.",
        full_slots_remaining="Open-slot count at or below which the full alert fires. Cap is derived per-tick from guild level.",
    )
    async def alerts_thresholds(
        self,
        ctx: commands.Context,
        dead_when: Optional[int] = None,
        full_slots_remaining: Optional[int] = None,
    ):
        if dead_when is None and full_slots_remaining is None:
            await ctx.reply("Provide at least one of `dead_when` or `full_slots_remaining`.")
            return
        changes: list[str] = []
        try:
            if dead_when is not None:
                v = await runtime_config.set_override("GUILD_DEAD_WHEN", str(dead_when))
                changes.append(f"`GUILD_DEAD_WHEN` = `{v}`")
            if full_slots_remaining is not None:
                v = await runtime_config.set_override("GUILD_FULL_SLOTS_REMAINING", str(full_slots_remaining))
                changes.append(f"`GUILD_FULL_SLOTS_REMAINING` = `{v}`")
        except (KeyError, PermissionError, ValueError) as e:
            await ctx.reply(f"❌ {e}")
            return
        await ctx.reply("✅ " + "; ".join(changes))


async def setup(bot: Bot):
    await bot.add_cog(RuntimeConfigCog(bot))
    logger.info("RuntimeConfig cog loaded successfully")
