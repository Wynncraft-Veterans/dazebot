import logging
from math import ceil
import discord
from typing import List


class Paginator(discord.ui.View):
    def __init__(self, embeds: List[discord.Embed]):
        super().__init__(timeout=180.0)
        self.embeds = embeds
        self.current = 0
    
    @discord.ui.button(label="◀", style=discord.ButtonStyle.gray)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = (self.current - 1) % len(self.embeds)
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)
    
    @discord.ui.button(label="▶", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = (self.current + 1) % len(self.embeds)
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)


def from_lines(title: str, lines: list[str], lines_per_page: int, logger: logging.Logger):
    embeds = []
    pages = ceil(len(lines) / lines_per_page)
    for page in range(pages):
        start_idx = page * lines_per_page
        end_idx = start_idx + lines_per_page
        page_lines = lines[start_idx:end_idx]
        if not page_lines:
            logger.warning("nothing in page")
            break
        embed = discord.Embed(
            title=title
        )
        embed.set_footer(text=f"Page {page+1}/{pages}")
        embed.add_field(
            name='',
            value='\n'.join(page_lines),
            inline=False
        )
        embeds.append(embed)

    logger.info(embeds)
    return embeds