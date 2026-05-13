"""Pydantic models for the Wynncraft v3 guild-endpoint envelope."""

import itertools
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Dict, Generator, List, Optional

from pydantic import field_validator
from typing import Any


class BannerLayer(BaseModel):
    colour: str
    pattern: str


class Banner(BaseModel):
    base: str
    tier: int
    # `structure` was removed from the v3 API response (2026-05); banners now
    # report only base/tier/layers (and an optional `pattern`). Keep both
    # fields optional so older cached payloads still validate.
    structure: Optional[str] = None
    pattern: Optional[str] = None
    layers: List[BannerLayer] = Field(default_factory=list)


class SeasonRank(BaseModel):
    rating: int
    finalTerritories: int = Field(alias="finalTerritories")


class BaseMember(BaseModel):
    username: str
    uuid: str
    # Wynncraft privacy opt-out (https://docs.wynncraft.com/privacy) means
    # several previously-required fields can now be null.
    online: Optional[bool] = None
    server: Optional[str] = None
    contributed: Optional[int] = None
    contributionRank: Optional[int] = None
    joined: Optional[str] = None
    # Per-member ``lastJoin`` from the v3 /guild endpoint. Verified to match
    # /v3/player/{uuid}.lastJoin exactly, including the privacy-opted-out
    # ``null`` cases. May be absent on older cached payloads.
    lastJoin: Optional[datetime] = None
    # `username` is the player's CURRENT Minecraft username. `legacyName`, when
    # present, is an OLDER username that some other parts of the Wynncraft API
    # may still report (i.e. the desynced cached name). Use it as an additional
    # candidate when matching API rows to local records by name; treat
    # `username` as canonical. (../../.claude/membership_spec.md \u00a74)
    legacyName: Optional[str] = None

    def name_candidates(self) -> tuple[str, ...]:
        """Returns names that could plausibly match a local record: the
        canonical (current) username first, then the desynced legacyName.
        """
        if self.legacyName and self.legacyName != self.username:
            return (self.username, self.legacyName)
        return (self.username,)


class Members(BaseModel):
    total: int
    owner: List[BaseMember] = Field(default_factory=list)
    chief: List[BaseMember] = Field(default_factory=list)
    strategist: List[BaseMember] = Field(default_factory=list)
    captain: List[BaseMember] = Field(default_factory=list)
    recruiter: List[BaseMember] = Field(default_factory=list)
    recruit: List[BaseMember] = Field(default_factory=list)

    @field_validator("owner", "chief", "strategist", "captain", "recruiter", "recruit", mode="before")
    @classmethod
    def transform_members_dict(cls, v: Any) -> List[Dict[str, Any]]:
        if isinstance(v, dict):
            members_list = []
            for username, member_data in v.items():
                member_data = dict(member_data)  # Copy to avoid modifying original
                member_data["username"] = username
                members_list.append(member_data)
            return members_list
        return v

    def all_members(self) -> Generator[BaseMember, None, None]:
        for item in itertools.chain(
            self.owner, self.chief, self.strategist, self.captain, self.recruiter, self.recruit
        ):
            yield item


class Guild(BaseModel):
    uuid: str
    name: str
    prefix: str
    level: int
    xpPercent: int
    territories: int
    # `wars` is null for guilds that have never participated in wars (and for
    # some privacy-opted-out responses). Treat missing as 0-equivalent.
    wars: Optional[int] = None
    created: str
    members: Members
    online: int
    banner: Banner
    seasonRanks: Dict[str, SeasonRank] = Field(default_factory=dict)


# Note: removed module-level network call that previously validated the model
# against the live API at import time. It made test/CI runs slow and brittle
# (and broke whenever Wynncraft is down).
