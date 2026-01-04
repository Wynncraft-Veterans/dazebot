import asyncio
import discord
import aiohttp
from discord.ext import commands
from bot import Bot

# This is disguisting. This entire thing needs to be ripped out and replaced.

class ProfSelection(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label="armouring", description="Helmets and/or chestplates.", emoji="🪖"
            ),
            discord.SelectOption(
                label="tailoring", description="Leggings and/or boots.", emoji="👞"
            ),
            discord.SelectOption(
                label="jeweling", description="Bracelets, rings, and/or necklaces.", emoji="💍"
            ),
            discord.SelectOption(
                label="weaponsmithing", description="Spears an/or daggers.", emoji="🗡️"
            ),
            discord.SelectOption(
                label="woodworking", description="Wands, bows, and/or reliks.", emoji="🏹"
            ),
            discord.SelectOption(
                label="alchemism", description="Potions.", emoji="⚗️"
            ),
            discord.SelectOption(
                label="scribing", description="Scrolls.", emoji="📜"
            ),
            discord.SelectOption(
                label="cooking", description="Food.", emoji="🍗"
            ),
        ]
        super().__init__(
            placeholder="What do you need your profer to make?",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Profer list",
            description="Compiling a list of profers able to craft your item.",
            color=discord.Color.blurple(),
        )
        # remove view and edit original message
        await interaction.response.edit_message(embed=embed, content=None, view=None)
        theMessage = await interaction.followup.send(embed=embed)

        async def sendReply(messagePayload: str) -> None:
            e = discord.Embed(
                title="Profer list",
                description=messagePayload,
                color=discord.Color.blurple(),
            )
            await theMessage.edit(embed=e)

        async def fetchAPI(linkToAPI: str):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(linkToAPI) as response:
                        while response.status == 429:
                            await sendReply("We're getting ratelimited by an API. Trying again in a few seconds")
                            await asyncio.sleep(2.5)
                            async with session.get(linkToAPI) as response:
                                pass
                        if response.status == 200:
                            return await response.json()
                        else:
                            return None
            except Exception:
                return None

        def iterate(filter_key, obj):
            if isinstance(obj, list):
                for item in obj:
                    yield from iterate(filter_key, item)
            elif isinstance(obj, dict):
                for key, item in obj.items():
                    if key == filter_key:
                        yield item
                    else:
                        yield from iterate(filter_key, item)

        def prepareMemberList(guildObject):
            parsedObject = list(iterate("uuid", guildObject))
            memberList = parsedObject[1:]
            preparedMemberList = {}
            for uuid in memberList:
                preparedMemberList[uuid] = {"online": False, "profLevel": 0}
            return preparedMemberList

        async def fetchUserInfo(profession, UUID):
            playerObject = await fetchAPI("https://api.wynncraft.com/v3/player/" + UUID + "?fullResult")
            if not playerObject:
                return {"online": False, "profLevel": 0}
            professionObject = list(iterate(profession, playerObject))
            professionLevels = list(iterate("level", professionObject))
            professionLevel = max(professionLevels) if professionLevels else 0

            onlineStatusList = list(iterate("online", playerObject))
            onlineStatus = onlineStatusList[0] if onlineStatusList else False

            return {"online": onlineStatus, "profLevel": professionLevel}

        async def populateProferData(profession):
            await sendReply("Fetching a list of the guild's members from the API")
            guildObject = await fetchAPI("https://api.wynncraft.com/v3/guild/prefix/VETS")
            if not guildObject:
                return "Failed to fetch guild data from the API."
            memberUUIDsList = prepareMemberList(guildObject)

            await sendReply("Pinging the API for info on every member of the guild. [0%]")
            processedItems = 0
            total = len(memberUUIDsList)
            for uuid in list(memberUUIDsList.keys()):
                processedItems += 1
                percent = round(100 * processedItems / total) if total else 100
                await sendReply(f"Pinging the API for info on every member of the guild. [{percent}%]")
                memberUUIDsList[uuid] = await fetchUserInfo(profession, uuid)

            await sendReply("Resolving UUIDs of relevant members. [0%]")
            voidProfers = {}
            dernicProfers = {}
            processedItems = 0
            for uuid in memberUUIDsList:
                processedItems += 1
                percent = round(100 * processedItems / total) if total else 100
                await sendReply(f"Resolving UUIDs of relevant members. [{percent}%]")
                memberData = memberUUIDsList[uuid]
                if 100 <= memberData["profLevel"] <= 102:
                    playerUsernameObject = await fetchAPI("https://api.minecraftservices.com/minecraft/profile/lookup/" + uuid)
                    playerUsername = playerUsernameObject["name"] if playerUsernameObject and "name" in playerUsernameObject else uuid
                    playerStatus = memberData["online"]
                    voidProfers[playerUsername] = playerStatus
                elif 103 <= memberData["profLevel"]:
                    playerUsernameObject = await fetchAPI("https://api.minecraftservices.com/minecraft/profile/lookup/" + uuid)
                    playerUsername = playerUsernameObject["name"] if playerUsernameObject and "name" in playerUsernameObject else uuid
                    playerStatus = memberData["online"]
                    dernicProfers[playerUsername] = playerStatus

            onlineVoidProfers = [username for (username, status) in voidProfers.items() if status]
            offlineVoidProfers = [u for u in voidProfers.keys() if u not in onlineVoidProfers]
            onlineDernicProfers = [username for (username, status) in dernicProfers.items() if status]
            offlineDernicProfers = [u for u in dernicProfers.keys() if u not in onlineDernicProfers]

            resultMessage = ""
            if onlineVoidProfers or onlineDernicProfers:
                resultMessage += "```Online members:```\n"
                if onlineDernicProfers:
                    resultMessage += "**Able to do dernic " + profession + " crafts:**\n"
                    for username in onlineDernicProfers:
                        resultMessage += "- `" + username + "`\n"
                if onlineVoidProfers:
                    resultMessage += "\n**Able to do non-dernic " + profession + " crafts:**\n"
                    for username in onlineVoidProfers:
                        resultMessage += "- `" + username + "`\n"

            if offlineVoidProfers or offlineDernicProfers:
                resultMessage += "```Offline members:```\n"
                if offlineDernicProfers:
                    resultMessage += "**Able to do dernic " + profession + " crafts:**\n"
                    for username in offlineDernicProfers:
                        resultMessage += "- `" + username + "`\n"
                if offlineVoidProfers:
                    resultMessage += "\n**Able to do non-dernic " + profession + " crafts:**\n"
                    for username in offlineVoidProfers:
                        resultMessage += "- `" + username + "`\n"

            return resultMessage

        profession = str(self.values[0])
        await sendReply("Please wait as we generate a list of guild members with high " + profession + " levels")

        try:
            proferData = await populateProferData(profession)
            await sendReply(proferData)
        except Exception as e:
            await sendReply("An error occurred while generating the profer list.")

class ProfSelectorView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__()
        self.add_item(ProfSelection())

class TemporaryProfCog(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.hybrid_command(name='temporary_findprofer')
    async def temporary_findprofer(self, ctx: commands.Context) -> None:
        """Temporary implementation of the profer finder, based on the legacy implementation."""
        view = ProfSelectorView()
        await ctx.send("Please make your choice", view=view)

async def setup(bot: Bot):
    await bot.add_cog(TemporaryProfCog(bot))
