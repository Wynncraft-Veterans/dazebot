from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional, Dict


class LegacyRankColour(BaseModel):
    main: str
    sub: str


class Guild(BaseModel):
    name: str
    prefix: str
    rank: str
    rankStars: Optional[str] = None


class Dungeons(BaseModel):
    total: int
    list: Dict[str, int]


class Raids(BaseModel):
    total: int
    list: Dict[str, int]


class PVP(BaseModel):
    kills: int
    deaths: int


class GlobalData(BaseModel):
    wars: int
    totalLevels: Optional[int] = None
    killedMobs: Optional[int] = None
    chestsFound: int
    dungeons: Dungeons
    raids: Raids
    completedQuests: int
    pvp: PVP
    contentCompletion: Optional[int] = None


class CharacterProfessionsType(str, Enum):
    # Gathering
    FARMING = "farming"
    FISHING = "fishing"
    MINING = "mining"
    WOODCUTTING = "woodcutting"

    # Crafting
    ALCHEMISM = "alchemism"
    ARMOURING = "armouring"
    COOKING = "cooking"
    JEWELING = "jeweling"
    SCRIBING = "scribing"
    TAILORING = "tailoring"
    WEAPONSMITHING = "weaponsmithing"
    WOODWORKING = "woodworking"


class CharacterProfession(BaseModel):
    level: int


class Character(BaseModel):
    professions: dict[CharacterProfessionsType, CharacterProfession]


class WynncraftPlayer(BaseModel):
    """Wynncraft v3 /player response.

    Per https://docs.wynncraft.com/privacy, players may opt to hide individual
    fields. ANY field below that is `Optional[...] = None` may be null because
    of privacy settings, not just because the data is missing.

    The previous behaviour of mapping `None` -> epoch (1970) for `firstJoin` /
    `lastJoin` has been removed: callers MUST explicitly handle `None` (e.g.
    skip inactivity actions, do not assign vanity role) per ../../.claude/membership_spec.md §7.
    """

    username: str
    online: Optional[bool] = None
    server: Optional[str] = None
    activeCharacter: Optional[str] = None
    nickname: Optional[str] = None
    uuid: str
    rank: Optional[str] = None
    rankBadge: Optional[str] = None
    legacyRankColour: Optional[LegacyRankColour] = None
    shortenedRank: Optional[str] = None
    supportRank: Optional[str] = None
    veteran: Optional[bool] = None
    firstJoin: Optional[datetime] = None
    lastJoin: Optional[datetime] = None
    playtime: Optional[float] = None
    guild: Optional[Guild] = None
    globalData: Optional[GlobalData] = None
    forumLink: Optional[int] = None
    ranking: Dict[str, int] = Field(default_factory=dict)
    previousRanking: Dict[str, int] = Field(default_factory=dict)
    publicProfile: Optional[bool] = None
    onlineStatus: Optional[bool] = None
    characters: Optional[dict[str, Character]] = None
