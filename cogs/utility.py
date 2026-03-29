import logging
from datetime import datetime, timezone, timedelta
from typing import Any, cast

import discord
from discord.ext import commands

from bot import Bot
from lib.discord_paginated_embed import Paginator, from_lines
from lib.lib import ProfCategory
from lib.wynn_api.player_models import CharacterProfessionsType
from orm import MinecraftAccount, ProfessionCategories

logger = logging.getLogger("dazebot.cogs.utility")

PROF_LABEL_TO_TYPE: dict[str, CharacterProfessionsType] = {
    "armouring": CharacterProfessionsType.ARMOURING,
    "tailoring": CharacterProfessionsType.TAILORING,
    "jeweling": CharacterProfessionsType.JEWELING,
    "weaponsmithing": CharacterProfessionsType.WEAPONSMITHING,
    "woodworking": CharacterProfessionsType.WOODWORKING,
    "alchemism": CharacterProfessionsType.ALCHEMISM,
    "scribing": CharacterProfessionsType.SCRIBING,
    "cooking": CharacterProfessionsType.COOKING,
}

CATEGORY_EMOJI = {
    ProfCategory.DERNIC: "💎",
    ProfCategory.VOID: "🌀",
}

LINES_PER_PAGE = 10

PROF_LABEL_TO_EMOJI = {
    "armouring": "🪖",
    "tailoring": "👞",
    "jeweling": "💍",
    "weaponsmithing": "🗡️",
    "woodworking": "🏹",
    "alchemism": "⚗️",
    "scribing": "📜",
    "cooking": "🍗",
}


class ProferSelect(discord.ui.Select):
    def __init__(self, include_non_members: bool = False) -> None:
        self.include_non_members = include_non_members
        options = [
            discord.SelectOption(
                label="armouring", description="Helmets and/or chestplates.", emoji=PROF_LABEL_TO_EMOJI["armouring"]
            ),
            discord.SelectOption(
                label="tailoring", description="Leggings and/or boots.", emoji=PROF_LABEL_TO_EMOJI["tailoring"]
            ),
            discord.SelectOption(
                label="jeweling",
                description="Bracelets, rings, and/or necklaces.",
                emoji=PROF_LABEL_TO_EMOJI["jeweling"],
            ),
            discord.SelectOption(
                label="weaponsmithing",
                description="Spears and/or daggers.",
                emoji=PROF_LABEL_TO_EMOJI["weaponsmithing"],
            ),
            discord.SelectOption(
                label="woodworking", description="Wands, bows, and/or reliks.", emoji=PROF_LABEL_TO_EMOJI["woodworking"]
            ),
            discord.SelectOption(label="alchemism", description="Potions.", emoji=PROF_LABEL_TO_EMOJI["alchemism"]),
            discord.SelectOption(label="scribing", description="Scrolls.", emoji=PROF_LABEL_TO_EMOJI["scribing"]),
            discord.SelectOption(label="cooking", description="Food.", emoji=PROF_LABEL_TO_EMOJI["cooking"]),
        ]
        super().__init__(
            placeholder="What do you need your profer to make?",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        chosen = self.values[0]
        prof_type = PROF_LABEL_TO_TYPE.get(chosen)
        if prof_type is None:
            await interaction.followup.send("Unknown profession selected.", ephemeral=True)
            return

        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=15)

        filters: dict[str, Any] = dict(
            prof_type=prof_type,
            category__in=[ProfCategory.DERNIC, ProfCategory.VOID],
        )
        if not self.include_non_members:
            filters["minecraft_account__guild"] = "Returners"

        base_qs = (
            ProfessionCategories.filter(**filters)
            .select_related("minecraft_account")
            .order_by("category", "-minecraft_account__last_online")
        )

        online = await base_qs.filter(minecraft_account__last_online__gte=cutoff)
        offline = await base_qs.filter(minecraft_account__last_online__lt=cutoff)

        if not online and not offline:
            await interaction.followup.send(
                f"No profers found for **{chosen}**.",
                ephemeral=True,
            )
            return

        lines: list[str] = []

        lines.append("~~==~~ **`ONLINE`** ~~==~~:")
        if online:
            for row in online:
                mc = cast(MinecraftAccount, row.minecraft_account)
                emoji = CATEGORY_EMOJI.get(row.category, "❓")
                lines.append(f"{emoji} {row.category.value} - **{mc.mc_username}**")
        else:
            lines.append("*No one online.*")

        lines.append("")
        lines.append("~~==~~ **`OFFLINE`** ~~==~~:")
        if offline:
            for row in offline:
                mc = cast(MinecraftAccount, row.minecraft_account)
                emoji = CATEGORY_EMOJI.get(row.category, "❓")
                lines.append(f"{emoji} {row.category.value} - **{mc.mc_username}**")
        else:
            lines.append("*No offline profers.*")

        scope_txt = "Returners + former members" if self.include_non_members else "Returners"
        embeds = from_lines(
            title=f"**`{chosen.capitalize()}`** {PROF_LABEL_TO_EMOJI[chosen.lower()]} Profers — {scope_txt}",
            lines=lines,
            lines_per_page=LINES_PER_PAGE,
            logger=logger,
        )

        view = Paginator(embeds) if len(embeds) > 1 else discord.utils.MISSING
        await interaction.followup.send(embed=embeds[0], view=view)


class ProferView(discord.ui.View):
    def __init__(self, include_non_members: bool = False):
        super().__init__(timeout=180.0)
        self.add_item(ProferSelect(include_non_members=include_non_members))


class Utility(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Initialized Utility")

    @commands.hybrid_command(name="findprofer")
    async def findprofer(self, ctx: commands.Context, include_non_members: bool = False):
        """Find a Returners profer. Pass include_non_members:True to also include former members."""
        view = ProferView(include_non_members=include_non_members)
        await ctx.send("Please select what you need your profer to help you with:", view=view)


async def setup(bot: Bot):
    await bot.add_cog(Utility(bot))
    logger.info("Loaded Utility")
