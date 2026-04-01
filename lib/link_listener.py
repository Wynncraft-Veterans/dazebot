from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class PendingLink:
    # tuple[uuid, real_username, message_content]
    future: asyncio.Future[tuple[str, str, str]]


# keyed by username.lower()
link_listeners: dict[str, PendingLink] = {}
