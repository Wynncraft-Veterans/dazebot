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
        description="(Operator) Dump up to 10k raw messages from the anni channel as a gzipped JSONL attachment for offline debug.",
    )
    @is_operator()
    async def script_extract_anni_timestamps(self, ctx: commands.Context):
        """Dump raw history of ``AnniConfig.CHANNEL_ID`` to JSONL.gz.

        Iterates ``channel.history(limit=10_000)`` and writes one JSON
        object per message with the metadata needed to debug filter
        decisions (``webhook_id``, ``interaction_metadata``, ``flags``,
        ``author.bot``, embeds, …).

        Replaces the prior CSV timestamp-extraction flow, which had
        become brittle against multiple Magbot/announcement-follow embed
        shapes; the new output is meant for offline analysis (jq /
        sqlite) before any filter is re-derived.
        """
        import gzip
        import io
        import json
        import time

        HISTORY_LIMIT = 10_000
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

        def embed_dict(emb: discord.Embed) -> dict:
            return {
                "title": emb.title,
                "description": emb.description,
                "author_name": emb.author.name if emb.author else None,
                "footer_text": emb.footer.text if emb.footer else None,
                "fields": [{"name": f.name, "value": f.value} for f in emb.fields],
            }

        def msg_dict(msg: discord.Message) -> dict:
            im = msg.interaction_metadata
            return {
                "id": msg.id,
                "created_at": msg.created_at.isoformat(),
                "type": msg.type.name if msg.type else None,
                "content": msg.content,
                "clean_content": msg.clean_content,
                "author": {
                    "id": msg.author.id,
                    "name": msg.author.name,
                    "bot": msg.author.bot,
                    "system": getattr(msg.author, "system", False),
                },
                "webhook_id": msg.webhook_id,
                "application_id": msg.application_id,
                "interaction_metadata": (
                    {
                        "id": im.id,
                        "type": im.type.name if im.type else None,
                        "user_id": im.user.id if im.user else None,
                    }
                    if im is not None
                    else None
                ),
                "flags": {
                    "value": msg.flags.value,
                    "crossposted": msg.flags.crossposted,
                    "is_crossposted": msg.flags.is_crossposted,
                    "suppress_embeds": msg.flags.suppress_embeds,
                    "ephemeral": msg.flags.ephemeral,
                    "loading": msg.flags.loading,
                },
                "role_mention_names": [r.name for r in msg.role_mentions],
                "embeds": [embed_dict(e) for e in msg.embeds],
                "attachment_count": len(msg.attachments),
                "reaction_count": sum(r.count for r in msg.reactions),
            }

        status = await ctx.reply(
            f"Starting dump of #{channel.name} (up to {HISTORY_LIMIT:,} msgs)..."
        )
        started = time.monotonic()
        last_edit = started
        messages_scanned = 0
        lines: list[bytes] = []

        async def push_status():
            nonlocal last_edit
            last_edit = time.monotonic()
            elapsed = last_edit - started
            text = (
                f"Dumping #{channel.name}...\n"
                f"messages scanned: {messages_scanned:,}/{HISTORY_LIMIT:,}\n"
                f"elapsed: {elapsed:.0f}s"
            )
            try:
                await status.edit(content=text)
            except discord.HTTPException:
                pass
            logger.info(
                "extract_anni_timestamps progress: %d msgs scanned, %.0fs elapsed",
                messages_scanned, elapsed,
            )

        try:
            async for msg in channel.history(limit=HISTORY_LIMIT):
                messages_scanned += 1
                lines.append(
                    (
                        json.dumps(msg_dict(msg), ensure_ascii=False, default=str)
                        + "\n"
                    ).encode("utf-8")
                )
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
                content=(
                    f"Dump complete in {elapsed:.0f}s — "
                    f"see attached file ({messages_scanned:,} messages)."
                )
            )
        except discord.HTTPException:
            pass

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            for line in lines:
                gz.write(line)
        data = buf.getvalue()

        await ctx.reply(
            content=f"Done. {messages_scanned:,} messages from #{channel.name}.",
            file=discord.File(io.BytesIO(data), filename="anni_messages.jsonl.gz"),
        )


async def setup(bot: Bot):
    await bot.add_cog(Scripts(bot))
    logger.info("Scripts cog loaded successfully")
