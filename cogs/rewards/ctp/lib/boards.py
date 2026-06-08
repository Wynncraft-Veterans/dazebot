"""Board catalog CRUD for the ``~ctp board`` admin commands.

Boards (DEV / TXT / ART / OPS / IGA / BLD / WEN / ...) tie ``~ctp reward``
to a tasks.wynnvets.org board id and optionally a Discord role that names
who can earn from that board. The overloaded ``~ctp board <ENUM> <arg>``
syntax (spec lines 145–159) is parsed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from orm import CTPBoard, CTPBoardMembership, DiscordAccount


# Lengths used to disambiguate "is this a board number, a role snowflake,
# or the literal delete-token". Discord IDs are 17–20 digits today;
# tasks.wynnvets.org board ids are small ints.
_ROLE_SNOWFLAKE_MIN_LEN = 17


@dataclass(frozen=True)
class BoardOpResult:
    """Outcome of ``apply_board_command``. ``action`` is one of:
    ``created`` | ``moved`` | ``role_changed`` | ``deleted`` | ``noop``.
    ``board`` is the post-op row (or None on delete / noop-because-missing).
    ``message`` is a short human description suitable for the cog reply.
    """

    action: str
    board: Optional[CTPBoard]
    message: str


async def find_by_enum(enum: str) -> Optional[CTPBoard]:
    """Case-insensitive lookup. Returns None when no board with that tag
    exists yet (the admin needs to run the 3-arg create form first).
    """
    return await CTPBoard.filter(enum__iexact=enum.upper()).first()


def format_board_url(board: CTPBoard) -> str:
    """The tasks.wynnvets.org URL for the board's page, used in command
    output (e.g. ``~ctp info`` history lines).
    """
    return f"https://tasks.wynnvets.org/board/{board.board_number}"


def format_task_url(task_number: int) -> str:
    """The tasks.wynnvets.org URL for a single task. ``task_number == 0``
    is the spec-defined "N/A dummy"; callers should suppress the link in
    that case rather than format it here.
    """
    return f"https://tasks.wynnvets.org/task/{task_number}"


async def list_manual_enums_for(disc: DiscordAccount) -> list[str]:
    """The set of board ``enum`` tags this user has been manually assigned
    to via ``~ctp assign``. Surfaced by ``_board_memberships`` in
    ``cogs/rewards/ctp.py`` alongside role-derived memberships; the cog
    deduplicates.
    """
    rows = await CTPBoardMembership.filter(discord_account=disc).prefetch_related("board")
    return [r.board.enum for r in rows]


async def assign_membership(
    *, disc: DiscordAccount, board: CTPBoard, actor_disc_uuid: str
) -> bool:
    """Idempotent create. Returns True if a new row was written, False if
    the user was already assigned. The caller is responsible for any
    Discord role grant — this function does not touch Discord state.
    """
    _, created = await CTPBoardMembership.get_or_create(
        discord_account=disc,
        board=board,
        defaults={"actor_disc_uuid": actor_disc_uuid},
    )
    return created


async def revoke_membership(*, disc: DiscordAccount, board: CTPBoard) -> bool:
    """Delete the manual association if present. Returns True if a row was
    deleted, False if there was nothing to remove. As with
    ``assign_membership``, Discord roles are the caller's concern.
    """
    deleted = await CTPBoardMembership.filter(
        discord_account=disc, board=board
    ).delete()
    return bool(deleted)


async def apply_board_command(enum: str, args: list[str]) -> BoardOpResult:
    """Dispatch the overloaded ``~ctp board ENUM <args...>`` syntax.

    Per spec lines 145–159:

    * 2 args, value == "0" → delete the board.
    * 2 args, value is a 17–20 digit snowflake → change ``role_id`` only
      (the board must already exist).
    * 2 args, value is a short int → move ``board_number`` only (the
      board must already exist).
    * 3 args, ``<board_number> <role_id>`` → create (or replace) the
      board with both fields populated.

    Anything else returns a ``noop`` with a message explaining the
    expected forms — the cog surfaces this to the admin verbatim.
    """
    enum = enum.upper()

    # 3-arg form: create / replace.
    if len(args) == 2:
        raw_num, raw_role = args
        if not raw_num.isdigit() or not raw_role.isdigit():
            return BoardOpResult(
                "noop", None,
                f"Could not parse `{raw_num} {raw_role}` as `<board_number> <role_id>`.",
            )
        board, created = await CTPBoard.update_or_create(
            enum=enum,
            defaults={"board_number": int(raw_num), "role_id": raw_role},
        )
        action = "created" if created else "moved"
        msg = (
            f"{'Added' if created else 'Replaced'} board `{enum}` → "
            f"{format_board_url(board)} (role <@&{raw_role}>)."
        )
        return BoardOpResult(action, board, msg)

    # 1-arg forms below.
    if len(args) != 1:
        return BoardOpResult(
            "noop", None,
            "Expected `~ctp board ENUM <board_number> <role_id>` (create), "
            "`~ctp board ENUM <board_number>` (move), "
            "`~ctp board ENUM <role_id>` (change role), or "
            "`~ctp board ENUM 0` (delete).",
        )
    value = args[0]
    if not value.isdigit():
        return BoardOpResult(
            "noop", None,
            f"Could not parse `{value}` as a number (board id, role id, or 0).",
        )

    existing = await find_by_enum(enum)

    if value == "0":
        if existing is None:
            return BoardOpResult("noop", None, f"No board `{enum}` to delete.")
        await existing.delete()
        return BoardOpResult("deleted", None, f"Deleted board `{enum}`.")

    if existing is None:
        return BoardOpResult(
            "noop", None,
            f"Board `{enum}` doesn't exist yet — use the 3-arg form "
            f"`~ctp board {enum} <board_number> <role_id>` to create it.",
        )

    if len(value) >= _ROLE_SNOWFLAKE_MIN_LEN:
        existing.role_id = value
        await existing.save(update_fields=["role_id", "updated_at"])
        return BoardOpResult(
            "role_changed", existing,
            f"Changed `{enum}` role to <@&{value}>.",
        )

    existing.board_number = int(value)
    await existing.save(update_fields=["board_number", "updated_at"])
    return BoardOpResult(
        "moved", existing,
        f"Moved board `{enum}` → {format_board_url(existing)}.",
    )
