"""
Commands used to maximise the guild's activity.
"""
import discord
from discord.ext import tasks, commands
from discord.ext.commands import Context
from discord.utils import get
import requests
import pickle
import os
from datetime import datetime, timedelta, timezone
import time
from collections import Counter

# Complaing whenever activity gets too low or the guild is full.
class Activity(commands.Cog, name="activity"):
    def __init__(self, bot):
        self.index = 0
        self.bot = bot
        self.printer.start()

    def cog_unload(self):
        self.printer.cancel()

    @tasks.loop(minutes=87.0)
    async def printer(self):
        self.index += 1
        print(f'Running the activity checker for the ' + str(self.index) + 'th time')
        
        # Function to send simple embeds.
        async def debug_message(description) -> None:
            channel = self.bot.get_channel(1401676479300898939)
            embed = discord.Embed(
                title = "Debug Message",
                description = description,
                color = discord.Color.greyple()
            )
            await channel.send(embed=embed)
        async def shout_alert(description, role_mention: str = "<@&1402295013169172500>") -> None:
            channel = self.bot.get_channel(1401676479300898939)
            embed = discord.Embed(
                title = "Activity Alert",
                description = description,
                color = discord.Color.red()
            )
            await channel.send(embed=embed, content=role_mention)
        async def prune_alert(description) -> None:
            channel = self.bot.get_channel(1401676479300898939)
            embed = discord.Embed(
                title = "Capacity Alert",
                description = description,
                color = discord.Color.blurple()
            )
            await channel.send(embed=embed, content="<@&1402337642380787752>")
        
        # Send shout array to file
        async def storeShoutData(shoutDataObject) -> None:
            shout_info = shoutDataObject
            pickle.dump(shout_info, open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/shouting.pickle", 'wb')) 
            
        # Fetch shout array from file
        async def fetchShoutData() -> None:
            try:
                shout_info = pickle.load(open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/shouting.pickle", 'rb'))
            except (OSError, IOError) as e:
                shout_info = dict()
                placeholder_time = datetime.now(timezone.utc) - timedelta(weeks=120)
                shout_info[placeholder_time] = 1348088845723238462
                pickle.dump(shout_info, open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/shouting.pickle", 'wb'))
            return shout_info
         
        # A system to fetch shit from APIs.
        async def fetchAPI(linkToAPI):
            response = requests.get(linkToAPI)
            while response.status_code == 429:
                await print("We're getting ratelimited by an API. Trying again in a few seconds")
                time.sleep(2.5)
                response = requests.get(linkToAPI)
            return response.json()
        
        # Use an API query to fetch the guild's current activity.
        async def fetchGuildStats() -> None:
            guildObject =  await fetchAPI('https://api.wynncraft.com/v3/guild/prefix/VETS')
            filledSlots = guildObject['members']['total']
            playerCount = guildObject['online']
            
            guildInfo = {}
            guildInfo['online'] = playerCount
            guildInfo['total'] = filledSlots
            
            return guildInfo
        
        # If the guild is dead, send a message to that effect.
        guildInfo = await fetchGuildStats()
        print(f'Checking if the guild is active enough')
        print(f'looks like there are ' + str(guildInfo['online']) + ' users online, meaning ' + str(guildInfo['online'] < 3))
        if guildInfo['online'] < 3:
            print(f'Nobody was online')
            
            shoutObject = await fetchShoutData()
            lastShout = next(reversed(shoutObject))
            
            safetime = lastShout + timedelta(hours=8)
            if datetime.now(timezone.utc) > safetime:
                now_utc = datetime.now(timezone.utc)
                hour = now_utc.hour

                # Night (wraps midnight): 22..23 and 0..5
                if hour > 21 or hour <= 5:
                    role_mention = "<@&1402295013169172500>"  # Americas
                # Afternoon: 14..21
                elif 13 < hour <= 21:
                    role_mention = "<@&1436108975132119221>"  # Europe
                # Morning: 6..13
                else:
                    role_mention = "<@&1436109140195020892>"  # Asia

                await shout_alert(
                    '__**The guild is dead!**__\nWe are within the allowable shout period!\n\n> Who wants to claim this shout? :D\n> (Wen will pay, plus there are prizes!)',
                    role_mention
                )
                print(f'Alerted the shouters channel with role {role_mention}.')
            else:
                print('The guild is dead, but the last shout was too recent.')
            
        # If the guild is full, send a message to that effect.
        print('Checking if the guild is full')
        print(f'looks like there are' + str(guildInfo['total']) + ' users total, meaning ' + str(guildInfo['online'] > 85))
        if guildInfo['total'] > 90:
            print('The guild was full.')
            try:
                prune_nag = pickle.load(open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/pruning.pickle", 'rb'))
            except (OSError, IOError) as e:
                prune_nag = set()
                prune_nag.add(datetime.now(timezone.utc))
                pickle.dump(prune_nag, open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/pruning.pickle", 'wb'))
            
            lastNag = max(prune_nag)
            safetime = lastNag + timedelta(hours=4)
            if datetime.now(timezone.utc) > safetime:
                await prune_alert(f'__**The guild is full!**__\nA chief needs to kick some people!')
            else:
                print('The guild is full, but the last alert was too recent.')
            prune_nag.add(datetime.now(timezone.utc))
            try:
                prune_nag = pickle.load(open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/pruning.pickle", 'rb'))
            except (OSError, IOError) as e:
                prune_nag = set()
                prune_nag.add(datetime.now(timezone.utc))
                pickle.dump(prune_nag, open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/pruning.pickle", 'wb'))


    @printer.before_loop
    async def before_printer(self):
        print('waiting...')
        await self.bot.wait_until_ready()
        
    @commands.hybrid_command(
        name="shout", description="Record your shout."
    )
    @commands.has_any_role("NA/SA Shouter", "EU/AF Shouter", "AS/AU Shouter")
    async def shout(self, context: Context) -> None:
        """
        A command to record advertisments made for the guild.
        
        :param context: The hybrid command context.
        """
        
        # Send shout array to file
        async def storeShoutData(shoutDataObject) -> None:
            shout_info = shoutDataObject
            pickle.dump(shout_info, open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/shouting.pickle", 'wb')) 
            
        # Fetch shout array from file
        async def fetchShoutData() -> None:
            try:
                shout_info = pickle.load(open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/shouting.pickle", 'rb'))
            except (OSError, IOError) as e:
                shout_info = dict()
                placeholder_time = datetime.now(timezone.utc) - timedelta(weeks=120)
                shout_info[placeholder_time] = 1348088845723238462
                pickle.dump(shout_info, open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/shouting.pickle", 'wb'))
            return shout_info
        
        shoutData = await fetchShoutData()
        shoutSender = context.author.id
        time = datetime.now(timezone.utc)
        shoutData[time] = shoutSender
        await storeShoutData(shoutData)
        
        await context.send(f'Thank you for helping the guild, <@' + str(shoutSender) + '>!\nYour shout has been recorded.')

    @commands.hybrid_command(
        name="shouterboard", description="Tracks prizes for shouts."
    )
    @commands.has_permissions(manage_messages=True)
    async def shouterBoard(self, context: Context) -> None:
        """
        A command to total the shouts given.
        
        :param context: The hybrid command context.
        """
        # Fetch shout array from file
        async def fetchShoutData() -> None:
            try:
                shout_info = pickle.load(open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/shouting.pickle", 'rb'))
            except (OSError, IOError) as e:
                shout_info = dict()
                pickle.dump(shout_info, open(f"{os.path.realpath(os.path.dirname(os.path.dirname(__file__)))}/database/shouting.pickle", 'wb'))
            return shout_info
        
        shoutData = await fetchShoutData()
        value_counts = Counter(shoutData.values())

        if not value_counts:
            await context.send("No shouts recorded yet.")
            return

        # Sort by shout count descending and build a pretty top-10 leaderboard
        sorted_counts = sorted(value_counts.items(), key=lambda kv: kv[1], reverse=True)
        lines = []
        rank = 1
        for uid, count in sorted_counts:
            # try cache first, then fetch if necessary
            user = self.bot.get_user(uid)
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)
                except Exception:
                    user = None

            if user:
                display = f"{user.name}#{user.discriminator}"
                mention = f"<@{uid}>"
            else:
                display = f"Unknown User ({uid})"
                mention = f"<@{uid}>"

            lines.append(f"**{rank}. {mention}** — {count} shouts")
            rank += 1
            if rank > 20:
                break

        desc = "\n".join(lines)
        embed = discord.Embed(title="Shouter Leaderboard", description=desc, color=discord.Color.blurple())
        embed.set_footer(text="Top 20 shouters")
        await context.send(embed=embed)
        

async def setup(bot) -> None:
    await bot.add_cog(Activity(bot))
