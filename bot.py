from datetime import datetime, timedelta, timezone
import logging
import discord
from discord.ext import commands
import os

from orm import close_db, init_db
logger = logging.getLogger('discord.bot')
from dotenv import load_dotenv
load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

class Config:
    GUILD = 1313769181321236490
    GUILD_DEAD_ALERT_CHANNEL = 1401676479300898939
    GUILD_FULL_ALERT_CHANNEL = 1401676479300898939
    GUILD_DEAD_ALERT_ROLE  = 1402295013169172500
    GUILD_FULL_ALERT_ROLE = 1313778812361904188
    GUILD_DEAD_WHEN = 2
    GUILD_FULL_WHEN = 92 - 2
    GUILD_DEAD_ALERT_DELTA = timedelta(hours=4)
    GUILD_FULL_ALERT_DELTA = timedelta(hours=8)


class DevConfig(Config):
    GUILD = 1407388408472666243
    GUILD_DEAD_ALERT_CHANNEL = GUILD_FULL_ALERT_CHANNEL= 1407388410393399494
    GUILD_DEAD_ALERT_ROLE = GUILD_FULL_ALERT_ROLE = 1409300773439012874
    GUILD_DEAD_WHEN = 10
    GUILD_FULL_WHEN = 10
    

class NOSQL:
    LAST_DEAD_ALERT = datetime.fromtimestamp(0, tz=timezone.utc)
    LAST_CAP_ALERT = datetime.fromtimestamp(0, tz=timezone.utc)
    LAST_CHECK_GUILD = datetime.fromtimestamp(0, tz=timezone.utc) # tasks might keep repeating even on cog reload? if yes, implement this variable where needed


class Bot(commands.Bot):
    config: type[Config] = Config
    nosql: NOSQL

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.nosql = NOSQL()
    
    async def setup_hook(self):
        try:
            await init_db()
            logger.info('Connected to database')
        except Exception as e:
            logger.error(f'Failed to connect to database: {e}')
        await self._load_cogs()
    
    async def _load_cogs(self):
        """Load all cogs from the cogs directory"""
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py') and not filename.startswith('__'):
                cog_name = filename[:-3]
                try:
                    await self.load_extension(f'cogs.{cog_name}')
                    logger.info(f'Loaded cog: {cog_name}')
                except Exception as e:
                    logger.error(f'Failed to load cog {cog_name}: {e}')
    
    async def on_ready(self):
        logger.info(f'{self.user} has connected to Discord!')
        logger.info(f'Bot is in {len(self.guilds)} guild(s)')
        try:
            synced = await self.tree.sync()
            logger.info(f'Synced {len(synced)} command(s)')
        except Exception as e:
            logger.error(f'Failed to sync commands: {e}')
    
    async def close(self):
        logger.info('Shutting down bot...')
        await close_db()
        await super().close()
    
    async def on_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.CommandInvokeError):
            error = error.original
        
        if isinstance(error, (commands.RangeError, )):
            await ctx.send(f"❌ {error}")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Invalid argument: {error}")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Missing argument: {error.param.name}")
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            print(f"Unhandled error: {error}")
            raise error


if __name__ == '__main__':
    bot = Bot(command_prefix=os.environ['PREFIX'], intents=intents)
    bot.run(os.environ['TOKEN'])