import itertools
from pydantic import BaseModel, Field
from typing import Dict, Generator, List, Optional

import requests


from pydantic import field_validator
from typing import Any


class BannerLayer(BaseModel):
    colour: str
    pattern: str


class Banner(BaseModel):
    base: str
    tier: int
    structure: str
    layers: List[BannerLayer]


class SeasonRank(BaseModel):
    rating: int
    finalTerritories: int = Field(alias="finalTerritories")


class BaseMember(BaseModel):
    username: str
    uuid: str
    online: bool
    server: Optional[str] = None
    contributed: int
    contributionRank: int
    joined: str


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
    wars: int
    created: str
    members: Members
    online: int
    banner: Banner
    seasonRanks: Dict[str, SeasonRank] = Field(default_factory=dict)


# crappy way to make sure API is still coherent with local models
r = requests.get("https://api.wynncraft.com/v3/guild/Returners?identifier=username")
guild = Guild(**r.json())
print(guild.members.owner[0].username)
