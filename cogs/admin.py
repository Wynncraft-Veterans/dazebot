import logging
import discord
from discord.ext import commands
logger = logging.getLogger('discord.cogs.admin')
from bot import Bot
from tortoise.expressions import Q
from orm import DiscordAccount, MinecraftAccount, Person

class Admin(commands.Cog):
    bot: Bot
    
    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Admin cog initialized")
    
    @commands.hybrid_command(name='sync', description='Sync slash commands')
    @commands.has_permissions(administrator=True)
    async def sync_commands(self, ctx: commands.Context):
        """Manually sync slash commands"""
        logger.info(f"Sync command initiated by {ctx.author} ({ctx.author.id}) in {ctx.guild}")
        try:
            synced = await self.bot.tree.sync()
            logger.info(f"Successfully synced {len(synced)} slash commands")
            await ctx.send(f'Successfully synced {len(synced)} command(s)')
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
            await ctx.send(f'Failed to sync commands: {e}')
    
    @commands.hybrid_command(name='reload', description='Reload a specific cog')
    @commands.has_permissions(administrator=True)
    async def reload_cog(self, ctx: commands.Context, cog_name: str):
        """Reload a specific cog"""
        logger.info(f"Reload command initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
            await self.bot.reload_extension(f'cogs.{cog_name}')
            logger.info(f"Successfully reloaded cog '{cog_name}'")
            await ctx.send(f'Successfully reloaded {cog_name}')
            await self.bot.tree.sync()
            logger.debug(f"Synced slash commands after reloading '{cog_name}'")
        except Exception as e:
            logger.error(f"Failed to reload cog '{cog_name}': {e}")
            await ctx.send(f'Failed to reload {cog_name}: {e}')
    
    @commands.hybrid_command(name='load', description='Load a specific cog')
    @commands.has_permissions(administrator=True)
    async def load_cog(self, ctx: commands.Context, cog_name: str):
        """Load a specific cog"""
        logger.info(f"Load command initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
            await self.bot.load_extension(f'cogs.{cog_name}')
            logger.info(f"Successfully loaded cog '{cog_name}'")
            await ctx.send(f'Successfully loaded {cog_name}')
            await self.bot.tree.sync()
            logger.debug(f"Synced slash commands after loading '{cog_name}'")
        except Exception as e:
            logger.error(f"Failed to load cog '{cog_name}': {e}")
            await ctx.send(f'Failed to load {cog_name}: {e}')
    
    @commands.hybrid_command(name='unload', description='Unload a specific cog')
    @commands.has_permissions(administrator=True)
    async def unload_cog(self, ctx: commands.Context, cog_name: str):
        """Unload a specific cog"""
        logger.info(f"Unload command initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
            await self.bot.unload_extension(f'cogs.{cog_name}')
            logger.info(f"Successfully unloaded cog '{cog_name}'")
            await ctx.send(f'Successfully unloaded {cog_name}')
            await self.bot.tree.sync()
            logger.debug(f"Synced slash commands after unloading '{cog_name}'")
        except Exception as e:
            logger.error(f"Failed to unload cog '{cog_name}': {e}")
            await ctx.send(f'Failed to unload {cog_name}: {e}')
    
    @commands.hybrid_command(name='say')
    @commands.has_permissions(administrator=True)
    async def say(self, ctx: commands.Context, msg: str):
        if msg:
            await ctx.send(msg)
        else:
            await ctx.send("I cant repeat an empty message you dummy 😡😡😡")
    
    @commands.hybrid_group(name="person")
    async def person(self, ctx: commands.Context):
        """Manage person accounts linking Minecraft and Discord"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use subcommands: link, unlink, check")

    @person.command(name="link")
    async def person_link(self, ctx: commands.Context, user: discord.Member, username_or_uuid: str):
        """Link a Discord user to a Minecraft account"""
        player = await MinecraftAccount.filter(
            Q(uuid=username_or_uuid) | Q(username=username_or_uuid)
        ).prefetch_related('person').first()
        
        if player is None:
            await ctx.reply("That minecraft user is not available. Try forcing a check on the guild.")
            return
    
        discord_acc, _ = await DiscordAccount.get_or_create(
            disc_uuid=str(user.id),
        )
        await discord_acc.fetch_related('person')
        
        if player.person and discord_acc.person:
            if player.person.id == discord_acc.person.id:
                await ctx.reply(f"{user.mention} is already linked to Minecraft account `{player.username}`")
            else:
                await ctx.reply(f"Both accounts are already linked to different persons. Please unlink first.")
    
        elif player.person:
            discord_acc.person = player.person
            await discord_acc.save()
            await ctx.reply(f"Linked {user.mention} to existing person with Minecraft account `{player.username}`")
    
        elif discord_acc.person:
            player.person = discord_acc.person
            await player.save()
            await ctx.reply(f"Linked Minecraft account `{player.username}` to {user.mention}'s existing person")
    
        else:
            person_obj = await Person.create(name=user.display_name)
            player.person = person_obj
            discord_acc.person = person_obj
            await player.save()
            await discord_acc.save()
            await ctx.reply(f"Created new person and linked {user.mention} to Minecraft account `{player.username}`")

    @person.group(name="unlink")
    async def person_unlink(self, ctx: commands.Context):
        """Unlink accounts from persons"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use: unlink mc <username> or unlink disc <user>")

    @person_unlink.command(name="mc")
    async def person_unlink_mc(self, ctx: commands.Context, username_or_uuid: str):
        """Unlink a Minecraft account from its person"""
        player = await MinecraftAccount.filter(
            Q(uuid=username_or_uuid) | Q(username=username_or_uuid)
        ).prefetch_related('person').first()
        
        if player is None:
            await ctx.reply("That minecraft user was not found.")
            return
        
        if player.person is None:
            await ctx.reply(f"Minecraft account `{player.username}` is not linked to any person.")
            return
        
        player.person = None
        await player.save()
        await ctx.reply(f"Unlinked Minecraft account `{player.username}` from person.")

    @person_unlink.command(name="disc")
    async def person_unlink_disc(self, ctx: commands.Context, user: discord.Member):
        """Unlink a Discord account from its person"""
        discord_acc = await DiscordAccount.filter(
            disc_uuid=str(user.id)
        ).prefetch_related('person').first()
        
        if discord_acc is None:
            await ctx.reply(f"{user.mention} does not have a Discord account registered.")
            return
        
        if discord_acc.person is None:
            await ctx.reply(f"{user.mention} is not linked to any person.")
            return
        
        discord_acc.person = None
        await discord_acc.save()
        await ctx.reply(f"Unlinked {user.mention} from person.")

    @person.group(name="check")
    async def person_check(self, ctx: commands.Context):
        """Check linked accounts"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use: check mc <username> or check disc <user>")

    @person_check.command(name="mc")
    async def person_check_mc(self, ctx: commands.Context, username_or_uuid: str):
        """Check a Minecraft account's linked person and all associated accounts"""
        player = await MinecraftAccount.filter(
            Q(uuid=username_or_uuid) | Q(username__iexact=username_or_uuid)
        ).prefetch_related('person').first()
        
        if player is None:
            await ctx.reply("That minecraft user was not found.")
            return
        
        if player.person is None:
            await ctx.reply(f"Minecraft account `{player.username}` is not linked to any person.")
            return
        
        # Fetch all linked accounts for this person
        person_obj = await Person.get(id=player.person.id).prefetch_related(
            'minecraft_accounts', 'discord_accounts'
        )
        
        embed = discord.Embed(
            title=f"Person: {person_obj.name or 'Unnamed'}",
            color=discord.Color.blue()
        )
        
        # Minecraft accounts
        mc_accounts = [f"• `{acc.username}` ({acc.uuid})" for acc in person_obj.minecraft_accounts]
        if mc_accounts:
            embed.add_field(
                name="Minecraft Accounts",
                value="\n".join(mc_accounts),
                inline=False
            )
        
        # Discord accounts
        discord_accounts = []
        for acc in person_obj.discord_accounts:
            try:
                user_obj = await self.bot.fetch_user(int(acc.disc_uuid))
                discord_accounts.append(f"• {user_obj.mention} ({user_obj.name})")
            except:
                discord_accounts.append(f"• <@{acc.disc_uuid}>")
        
        if discord_accounts:
            embed.add_field(
                name="Discord Accounts",
                value="\n".join(discord_accounts),
                inline=False
            )
        
        await ctx.reply(embed=embed)

    @person_check.command(name="disc")
    async def person_check_disc(self, ctx: commands.Context, user: discord.Member):
        """Check a Discord account's linked person and all associated accounts"""
        discord_acc = await DiscordAccount.filter(
            disc_uuid=str(user.id)
        ).prefetch_related('person').first()
        
        if discord_acc is None:
            await ctx.reply(f"{user.mention} does not have a Discord account registered.")
            return
        
        if discord_acc.person is None:
            await ctx.reply(f"{user.mention} is not linked to any person.")
            return
        
        # Fetch all linked accounts for this person
        person_obj = await Person.get(id=discord_acc.person.id).prefetch_related(
            'minecraft_accounts', 'discord_accounts'
        )
        
        embed = discord.Embed(
            title=f"Person: {person_obj.name or 'Unnamed'}",
            color=discord.Color.blue()
        )
        
        # Minecraft accounts
        mc_accounts = [f"• `{acc.username}` ({acc.uuid})" for acc in person_obj.minecraft_accounts]
        if mc_accounts:
            embed.add_field(
                name="Minecraft Accounts",
                value="\n".join(mc_accounts),
                inline=False
            )
        
        # Discord accounts
        discord_accounts = []
        for acc in person_obj.discord_accounts:
            try:
                user_obj = await self.bot.fetch_user(int(acc.disc_uuid))
                discord_accounts.append(f"• {user_obj.mention} ({user_obj.name})")
            except:
                discord_accounts.append(f"• <@{acc.disc_uuid}>")
        
        if discord_accounts:
            embed.add_field(
                name="Discord Accounts",
                value="\n".join(discord_accounts),
                inline=False
            )
        
        await ctx.reply(embed=embed)


async def setup(bot: Bot):
    await bot.add_cog(Admin(bot))
    logger.info("Admin cog loaded successfully")