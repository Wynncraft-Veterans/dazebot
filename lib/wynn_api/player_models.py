from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field, field_validator
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
    username: str
    online: bool
    server: Optional[str]
    activeCharacter: Optional[str] = None
    nickname: Optional[str] = None
    uuid: str
    rank: str
    rankBadge: Optional[str]
    legacyRankColour: Optional[LegacyRankColour]
    shortenedRank: Optional[str] = None
    supportRank: Optional[str]
    veteran: Optional[bool] = None
    firstJoin: datetime = Field(default_factory=lambda: datetime.fromtimestamp(0, tz=timezone.utc))
    lastJoin: datetime = Field(default_factory=lambda: datetime.fromtimestamp(0, tz=timezone.utc))
    playtime: float = Field(default_factory=float)
    guild: Optional[Guild]
    globalData: Optional[GlobalData] = None
    forumLink: Optional[int] = None
    ranking: Dict[str, int]
    previousRanking: Dict[str, int]
    publicProfile: Optional[bool] = None
    onlineStatus: Optional[bool] = None
    characters: Optional[dict[str, Character]] = None

    @field_validator("lastJoin", "firstJoin", mode="before")
    def handle_none_datetime(cls, v):
        """Convert None to epoch datetime, let Pydantic handle everything else"""
        if v is None:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        return v
