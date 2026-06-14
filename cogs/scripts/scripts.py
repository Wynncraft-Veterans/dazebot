"""Operator-only ``~script`` group: prefix-only one-off maintenance scripts.

These commands are intentionally kept out of the slash picker (no
``hybrid_*`` decorators) so they don't clutter operator UIs and don't
auto-discover into the global command tree. They're throwaway recovery /
backfill helpers — each subcommand is expected to be invoked rarely (often
once) and removed once its purpose is served.

Lives in its own cog (rather than parked under another domain) so that
adding or retiring a one-off doesn't touch any real feature code.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot import Bot
from config import CurrConfig
from lib.auth import is_operator

logger = logging.getLogger("dazebot.cogs.scripts.scripts")


class Scripts(commands.Cog):
    """``~script <subcommand>`` — operator-only one-off maintenance."""

    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Scripts cog initialized")

    @commands.group(
        name="script",
        description="(Operator) One-off maintenance scripts.",
    )
    @is_operator()
    async def script_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Available scripts: `~script edit_welcome <channel> <message_id>`, "
                "`~script rename_cult`, `~script install_intercult`, "
                "`~script install_recruitment`, `~script install_recruitment_deercult`, "
                "`~script extract_anni_timestamps`."
            )

    @script_group.command(
        name="edit_welcome",
        description="(Operator) Rewrite an onboarding message to the simplified copy, keeping the link button.",
    )
    @is_operator()
    async def script_edit_welcome(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        message_id: str,
    ):
        from lib.discord_utils.first_install_view import FirstInstallView

        await ctx.defer(ephemeral=True)
        try:
            mid = int(message_id)
        except ValueError:
            await ctx.reply("`message_id` must be numeric.", ephemeral=True)
            return

        try:
            msg = await channel.fetch_message(mid)
        except (discord.NotFound, discord.Forbidden) as e:
            await ctx.reply(f"❌ Couldn't fetch that message: {e}", ephemeral=True)
            return

        if msg.author.id != self.bot.user.id:
            await ctx.reply(
                "❌ That message wasn't posted by me, so I can't edit it.",
                ephemeral=True,
            )
            return

        new_content = (
            "# IN-GAME VETS GUILD MEMBERS!\n"
            "## Use this to unlock guild-specific channels!"
        )
        try:
            await msg.edit(content=new_content, embed=None, view=FirstInstallView())
        except discord.HTTPException as e:
            await ctx.reply(f"❌ Edit failed: {e}", ephemeral=True)
            return

        await ctx.reply(f"✅ Edited {msg.jump_url}.", ephemeral=True)

    @script_group.command(
        name="rename_cult",
        description="(Operator) One-off: rename the `dazecult` row to `deercult`.",
    )
    @is_operator()
    async def script_rename_cult(self, ctx: commands.Context):
        from orm import Cult

        await ctx.defer(ephemeral=True)
        old, new = "dazecult", "deercult"
        if await Cult.filter(name=new).exists():
            await ctx.reply(f"❌ `{new}` already exists; aborting.", ephemeral=True)
            return
        cult = await Cult.filter(name=old).first()
        if cult is None:
            await ctx.reply(f"❌ No cult named `{old}` to rename.", ephemeral=True)
            return
        cult.name = new
        await cult.save(update_fields=["name"])
        await ctx.reply(
            f"✅ Renamed `{old}` → `{new}`. (Thread mapping lives on the row "
            "itself in `Cult.thread_id`; no code change needed.)",
            ephemeral=True,
        )

    @script_group.command(
        name="install_intercult",
        description="(Operator) Reinstall the intercult button in every cult thread (idempotent).",
    )
    @is_operator()
    async def script_install_intercult(self, ctx: commands.Context):
        """Bulk reinstall of the cross-cult messaging button.

        Routine cult creation already installs the button via
        ``~manage_return 0 createCult``; this command is the one-off
        recovery path for re-pinning after the original message was
        deleted, or for bootstrapping a backfilled DB.
        """
        from cogs.events.returns.lib.views.intercult_view import install_intercult_button

        await ctx.defer(ephemeral=True)
        posted, skipped, failed, unresolved = await install_intercult_button(self.bot)
        await ctx.reply(
            f"✅ intercult install: posted={posted} skipped={skipped} "
            f"failed={failed} unresolved={unresolved}",
            ephemeral=True,
        )

    @script_group.command(
        name="install_recruitment",
        description="(Operator) Install the recruitment button in every cult thread except deercult (idempotent).",
    )
    @is_operator()
    async def script_install_recruitment(self, ctx: commands.Context):
        """Bulk install of the recruitment button across non-deercult cults.

        Deercult is excluded by default (membership-balance reasons);
        opt it in separately with ``~script install_recruitment_deercult``.
        Routine cult creation installs this button automatically (also
        skipping deercult); this command exists for the initial bootstrap
        and for re-pinning if the message was deleted.
        """
        from cogs.events.returns.lib.views.recruitment_view import install_recruitment_button

        await ctx.defer(ephemeral=True)
        posted, skipped, failed, unresolved, excluded = await install_recruitment_button(self.bot)
        await ctx.reply(
            f"✅ recruitment install: posted={posted} skipped={skipped} "
            f"failed={failed} unresolved={unresolved} excluded_deercult={excluded}",
            ephemeral=True,
        )

    @script_group.command(
        name="install_recruitment_deercult",
        description="(Operator) One-shot: install the recruitment button in deercult's thread.",
    )
    @is_operator()
    async def script_install_recruitment_deercult(self, ctx: commands.Context):
        """Install the recruitment button in deercult, normally excluded.

        Call once membership rebalances make it appropriate. Idempotent —
        safe to run twice (the second run reports ``skipped``).
        """
        from cogs.events.returns.lib.views.recruitment_view import install_recruitment_in_deercult

        await ctx.defer(ephemeral=True)
        result = await install_recruitment_in_deercult(self.bot)
        note = {
            "posted": "✅ Recruitment button installed and pinned in deercult.",
            "skipped": "ℹ️ Recruitment button was already pinned in deercult.",
            "failed": "❌ Recruitment button install failed; check logs.",
            "unresolved": "❌ deercult has no resolvable `thread_id`; aborting.",
        }[result]
        await ctx.reply(note, ephemeral=True)

    @script_group.command(
        name="extract_anni_timestamps",
        description="(Operator) Dump Discord timestamps from webhook-posted 'annihilation' messages in the anni channel.",
    )
    @is_operator()
    async def script_extract_anni_timestamps(self, ctx: commands.Context):
        """Scrape ``AnniConfig.CHANNEL_ID`` for webhook-authored messages
        whose content or embeds mention "annihilation", extract every
        ``<t:N[:fmt]>`` Discord timestamp, dedupe, sort ascending, and reply
        with the result as a CSV attachment.

        The historical bulk poster (Magbot) no longer exists and its webhook
        id can't be recovered, so the filter is "any webhook + keyword"
        rather than a specific ``webhook_id``.

        Edits a single status message at least every 5 seconds with the
        message counter and elapsed time so the operator can see progress
        during the (potentially multi-minute) scan. Also mirrors progress
        to the cog logger so the docker logs reflect liveness independently
        of Discord state.
        """
        import csv
        import io
        import re
        import time

        TS_RE = re.compile(r"<t:(\d+)(?::[A-Za-z])?>")
        EDIT_INTERVAL_S = 5.0

        if ctx.guild is None:
            await ctx.reply("❌ Must be run in a guild context.")
            return

        channel = ctx.guild.get_channel(CurrConfig.AnniConfig.CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await ctx.reply(
                f"❌ AnniConfig.CHANNEL_ID ({CurrConfig.AnniConfig.CHANNEL_ID}) "
                "is not a readable text channel in this guild."
            )
            return

        def message_text(msg: discord.Message) -> str:
            parts: list[str] = [msg.content or ""]
            for emb in msg.embeds:
                if emb.title:
                    parts.append(emb.title)
                if emb.description:
                    parts.append(emb.description)
                if emb.author and emb.author.name:
                    parts.append(emb.author.name)
                if emb.footer and emb.footer.text:
                    parts.append(emb.footer.text)
                for f in emb.fields:
                    if f.name:
                        parts.append(f.name)
                    if f.value:
                        parts.append(f.value)
            return "\n".join(parts)

        status = await ctx.reply(f"Starting scrape of #{channel.name}...")

        timestamps: set[int] = set()
        messages_scanned = 0
        matched = 0
        started = time.monotonic()
        last_edit = started

        async def push_status():
            nonlocal last_edit
            last_edit = time.monotonic()
            elapsed = last_edit - started
            text = (
                f"Scraping #{channel.name}...\n"
                f"messages scanned: {messages_scanned:,}\n"
                f"matched webhook messages: {matched:,}\n"
                f"timestamps found: {len(timestamps):,}\n"
                f"elapsed: {elapsed:.0f}s"
            )
            try:
                await status.edit(content=text)
            except discord.HTTPException:
                pass
            logger.info(
                "extract_anni_timestamps progress: %d msgs scanned, "
                "%d matched, %d timestamps, %.0fs elapsed",
                messages_scanned, matched, len(timestamps), elapsed,
            )

        try:
            async for msg in channel.history(limit=None):
                messages_scanned += 1
                if msg.webhook_id is not None:
                    text = message_text(msg)
                    if "annihilation" in text.lower():
                        matched += 1
                        timestamps.update(int(s) for s in TS_RE.findall(text))
                if time.monotonic() - last_edit >= EDIT_INTERVAL_S:
                    await push_status()
        except (discord.Forbidden, discord.HTTPException) as e:
            try:
                await status.edit(content=f"❌ History read failed: {e}")
            except discord.HTTPException:
                pass
            return

        elapsed = time.monotonic() - started
        try:
            await status.edit(
                content=f"Scrape complete in {elapsed:.0f}s — see attached CSV."
            )
        except discord.HTTPException:
            pass

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["unix_timestamp"])
        for ts in sorted(timestamps):
            writer.writerow([ts])

        data = buf.getvalue().encode("utf-8")
        await ctx.reply(
            content=(
                f"Done. {len(timestamps):,} unique timestamps from "
                f"{matched:,} matched webhook messages in #{channel.name}."
            ),
            file=discord.File(io.BytesIO(data), filename="anni_timestamps.csv"),
        )


async def setup(bot: Bot):
    await bot.add_cog(Scripts(bot))
    logger.info("Scripts cog loaded successfully")
