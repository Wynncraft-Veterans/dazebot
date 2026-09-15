"""Tests for ``cogs.events.returns.week_90`` (The Bulletin).

r90 is replaced by :class:`FakeR90`, patched in at ``week_90._call``, and
Discord by mocks. The ``Return90Post`` row is real: each test bootstraps a
fresh returns DB through ``init_db`` (the ``_isolated_db`` pattern from
``tests/test_migrations.py``), which also proves migration 4 applies.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.events.returns import MANAGE_REGISTRY, TICK_REGISTRY, Tier
from cogs.events.returns import week_90 as w
from orm import init_db
from orm_returns import Return90Post

ANNOUNCE_CHANNEL = 111111111111111111
ANNOUNCE_MESSAGE = 222222222222222222
STAFF_CHANNEL = 333333333333333333
ROLE = 444444444444444444


@pytest.fixture
async def _isolated_db(tmp_path, monkeypatch):
    from aerich.migrate import Migrate
    from tortoise import Tortoise

    async def _reset():
        try:
            await Tortoise.close_connections()
        except Exception:
            pass
        Tortoise._inited = False
        for attr in ("_last_version_content", "migrate_location", "ddl", "ddl_class", "dialect"):
            if hasattr(Migrate, attr):
                try:
                    delattr(Migrate, attr)
                except AttributeError:
                    pass
        Migrate.upgrade_operators = []
        Migrate.downgrade_operators = []

    await _reset()
    monkeypatch.setenv("DAZEBOT_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DAZEBOT_RETURNS_DB_PATH", str(tmp_path / "test_returns.db"))
    monkeypatch.setenv("DAZEBOT_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("DAZEBOT_ALLOW_FRESH_RETURNS_DB", "1")
    await init_db()
    yield
    await _reset()


class FakeR90:
    """The subset of r90's /bot API that week_90 uses, with its rules."""

    def __init__(self):
        self.epoch = 1
        self.wanted = {"kind": "card", "id": None, "pages": 1, "version": 0}
        self.current = {"kind": "card", "id": None, "pages": 1, "version": 0}
        self.queue_depth = 0
        self.notifications: list[dict] = []
        self.calls: list[tuple[str, str, dict | None]] = []
        self.down = False
        self.drop_acks = False
        self.delay = 0.0
        self.retracted: set[int] = set()

    async def __call__(self, method, path, *, json_body=None):
        self.calls.append((method, path, json_body))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.down:
            raise w.R90Unavailable("ClientConnectorError")
        if path == "/bot/state":
            return 200, json.dumps({
                "epoch": self.epoch, "wanted": self.wanted, "current": self.current,
                "queue_depth": self.queue_depth, "intake_paused": False,
                "notifications": self.notifications,
            }).encode()
        if path == "/bot/card":
            return 200, b"\x89PNG card"
        if path.startswith("/bot/sub/"):
            sub_id = int(path.split("/")[3])
            return (410, b"") if sub_id in self.retracted else (200, b"\x89PNG page")
        if path == "/bot/notifications/ack":
            ids = set(json_body["ids"])
            self.notifications = [n for n in self.notifications if n["id"] not in ids]
            return 200, b"{}"
        if path == "/bot/displayed":
            if self.drop_acks:
                raise w.R90Unavailable("TimeoutError")
            self.current = {"kind": json_body["kind"], "id": json_body["id"]}
            return 200, b"{}"
        if path == "/bot/takedown":
            if json_body["shown_kind"] != "sub":
                return 409, b'{"code": "card_showing"}'
            self.retracted.add(json_body["shown_id"])
            self.wanted = {"kind": "card", "id": None, "pages": 1}
            return 200, json.dumps({"retracted": json_body["shown_id"]}).encode()
        if path == "/bot/display_failed":
            return 200, b"{}"
        if path == "/bot/intake":
            return 200, b"{}"
        return 404, b""

    def paths(self, path):
        return [c for c in self.calls if c[1] == path]


def http_error(status=500):
    return discord.HTTPException(MagicMock(status=status, reason="x"), "boom")


def not_found():
    return discord.NotFound(MagicMock(status=404, reason="Not Found"), "gone")


@pytest.fixture
def r90(monkeypatch):
    fake = FakeR90()
    monkeypatch.setenv("R90_BASE_URL", "http://r90-bulletin:8001")
    monkeypatch.setenv("R90_BOT_SECRET", "s")
    monkeypatch.setenv("R90_PUBLIC_BASE_URL", "https://return.wynnvets.org")
    monkeypatch.setattr(w, "_call", fake)
    monkeypatch.setattr(w, "_body_synced", False)
    w._failures.clear()
    return fake


@pytest.fixture
def discord_env():
    message = MagicMock()
    message.id = ANNOUNCE_MESSAGE
    message.content = w._ANNOUNCEMENT_BODY
    message.edit = AsyncMock()
    message.jump_url = "https://discord.com/channels/1/2/3"

    announce = MagicMock()
    announce.id = ANNOUNCE_CHANNEL
    announce.fetch_message = AsyncMock(return_value=message)
    announce.send = AsyncMock(return_value=message)

    staff = MagicMock()
    staff.id = STAFF_CHANNEL
    staff.send = AsyncMock()
    staff.fetch_message = AsyncMock()

    bot = MagicMock()
    bot.get_channel.side_effect = lambda cid: {ANNOUNCE_CHANNEL: announce, STAFF_CHANNEL: staff}.get(cid)
    bot.fetch_channel = AsyncMock(side_effect=not_found())
    return bot, announce, staff, message


def ctx_for(bot):
    ctx = MagicMock()
    ctx.bot = bot
    ctx.channel = MagicMock(spec=discord.DMChannel)
    ctx.reply = AsyncMock()
    ctx.author = MagicMock()
    return ctx


def reply_text(ctx) -> str:
    return " ".join(call.args[0] for call in ctx.reply.await_args_list)


async def posted(**fields) -> Return90Post:
    values = dict(channel_id=ANNOUNCE_CHANNEL, message_id=ANNOUNCE_MESSAGE,
                  staff_channel_id=STAFF_CHANNEL, ping_role_id=ROLE,
                  shown_kind="card", shown_id=None, shown_epoch=1)
    values.update(fields)
    return await Return90Post.create(id=1, **values)


def filenames(edit_call) -> list[str]:
    return [f.filename for f in edit_call.kwargs["attachments"]]


# ------------------------------------------------------------------ reconcile


async def test_reconcile_puts_wanted_fax_up_and_acks(_isolated_db, r90, discord_env):
    bot, _, _, message = discord_env
    await posted()
    r90.wanted = {"kind": "sub", "id": 12, "pages": 2, "version": 3}
    summary = await w._reconcile(bot)
    assert "fax #12" in summary
    names = filenames(message.edit.await_args)
    assert len(names) == 2 and names[0].startswith("r90_s12_p1_v") and names[1].startswith("r90_s12_p2_v")
    assert "content" not in message.edit.await_args.kwargs
    post = await Return90Post.get(id=1)
    assert (post.shown_kind, post.shown_id, post.shown_epoch) == ("sub", 12, 1)
    assert r90.paths("/bot/displayed")[-1][2] == {"kind": "sub", "id": 12, "epoch": 1}


async def test_in_sync_does_nothing_and_lost_ack_is_resent(_isolated_db, r90, discord_env):
    bot, _, _, message = discord_env
    await posted(shown_kind="sub", shown_id=5)
    r90.wanted = {"kind": "sub", "id": 5, "pages": 1}
    r90.current = {"kind": "card", "id": None}
    assert "re-sent" in await w._reconcile(bot)
    assert message.edit.await_count == 0
    assert "In sync" in await w._reconcile(bot)
    assert len(r90.paths("/bot/displayed")) == 1


async def test_stale_announcement_text_is_edited_once(_isolated_db, r90, discord_env):
    bot, _, _, message = discord_env
    await posted()
    message.content = ""
    assert "In sync" in await w._reconcile(bot)
    assert message.edit.await_args_list == [((), {"content": w._ANNOUNCEMENT_BODY})]
    await w._reconcile(bot)
    assert message.edit.await_count == 1


async def test_notifications_ping_only_the_role_and_are_acked(_isolated_db, r90, discord_env):
    bot, _, staff, _ = discord_env
    await posted()
    r90.notifications = [
        {"id": 1, "kind": "new_submission", "submission_id": 12, "pages": 3, "text": ""},
        {"id": 2, "kind": "note", "submission_id": None, "pages": 0, "text": "A fax could not be read @everyone"},
    ]
    await w._reconcile(bot)
    first, second = staff.send.await_args_list
    assert first.args[0] == (f"<@&{ROLE}> New fax #12 (3 pages) awaiting review → "
                             "https://return.wynnvets.org/admin")
    allowed = first.kwargs["allowed_mentions"]
    assert allowed.everyone is False and allowed.users is False and [r.id for r in allowed.roles] == [ROLE]
    assert second.kwargs["allowed_mentions"].roles is False and second.kwargs["allowed_mentions"].everyone is False
    assert r90.notifications == []


async def test_lock_prevents_double_ping_and_double_edit(_isolated_db, r90, discord_env):
    bot, _, staff, message = discord_env
    await posted()
    r90.delay = 0.01
    r90.wanted = {"kind": "sub", "id": 7, "pages": 1}
    r90.notifications = [{"id": 1, "kind": "new_submission", "submission_id": 7, "pages": 1}]
    await asyncio.gather(w._reconcile(bot), w._reconcile(bot), w.tick(bot))
    assert staff.send.await_count == 1
    assert message.edit.await_count == 1


async def test_display_failures_hand_the_fax_back_after_three(_isolated_db, r90, discord_env):
    bot, _, staff, message = discord_env
    await posted()
    r90.wanted = {"kind": "sub", "id": 9, "pages": 1}
    message.edit.side_effect = http_error(413)
    for _ in range(2):
        await w._reconcile(bot)
    assert r90.paths("/bot/display_failed") == []
    await w._reconcile(bot)
    assert r90.paths("/bot/display_failed")[0][2]["id"] == 9
    assert "Couldn't put fax #9" in staff.send.await_args.args[0]


async def test_deleted_announcement_alerts_once(_isolated_db, r90, discord_env):
    bot, announce, staff, _ = discord_env
    await posted()
    r90.wanted = {"kind": "sub", "id": 3, "pages": 1}
    announce.fetch_message.side_effect = not_found()
    await w._reconcile(bot)
    await w._reconcile(bot)
    assert staff.send.await_count == 1
    assert (await Return90Post.get(id=1)).message_id is None


# ------------------------------------------------------------------- takedown


async def test_lost_ack_then_takedown_retracts_only_the_shown_fax(_isolated_db, r90, discord_env):
    bot, _, _, message = discord_env
    await posted(shown_kind="sub", shown_id=6)
    r90.current = {"kind": "sub", "id": 6}
    r90.wanted = {"kind": "sub", "id": 7, "pages": 1}
    r90.drop_acks = True
    await w._reconcile(bot)                      # 7 goes up; r90 never hears
    assert r90.current == {"kind": "sub", "id": 6}
    r90.drop_acks = False
    ctx = ctx_for(bot)
    await MANAGE_REGISTRY[90]["takedown"].callback(ctx, [])
    assert r90.paths("/bot/takedown")[0][2] == {"shown_kind": "sub", "shown_id": 7}
    assert r90.retracted == {7}
    assert filenames(message.edit.await_args)[0].startswith("r90_card_v")
    assert "Retracted fax #7" in reply_text(ctx)


async def test_takedown_with_r90_down_clears_and_freezes(_isolated_db, r90, discord_env):
    bot, _, _, message = discord_env
    await posted(shown_kind="sub", shown_id=4)
    r90.down = True
    ctx = ctx_for(bot)
    await MANAGE_REGISTRY[90]["takedown"].callback(ctx, [])
    assert message.edit.await_args.kwargs == {"attachments": []}
    post = await Return90Post.get(id=1)
    assert post.frozen and post.shown_kind == "cleared" and post.shown_id == 4
    assert "retract fax #4" in reply_text(ctx)
    r90.down = False
    await w._reconcile(bot)                      # frozen: no edits, even once r90 is back
    assert message.edit.await_count == 1


async def test_resume_refuses_while_r90_still_wants_the_taken_down_fax(_isolated_db, r90, discord_env):
    bot, _, _, message = discord_env
    await posted(shown_kind="cleared", shown_id=4, frozen=True)
    r90.wanted = {"kind": "sub", "id": 4, "pages": 1}
    ctx = ctx_for(bot)
    await MANAGE_REGISTRY[90]["resume"].callback(ctx, [])
    assert "Retract it in /admin first" in reply_text(ctx)
    assert (await Return90Post.get(id=1)).frozen
    r90.wanted = {"kind": "card", "id": None, "pages": 1}
    await MANAGE_REGISTRY[90]["resume"].callback(ctx_for(bot), [])
    post = await Return90Post.get(id=1)
    assert not post.frozen and post.shown_kind == "card"
    assert filenames(message.edit.await_args)[0].startswith("r90_card_v")


# ------------------------------------------------------------------- commands


async def test_post_force_keeps_notify_fields(_isolated_db, r90, discord_env):
    bot, announce, _, _ = discord_env
    await MANAGE_REGISTRY[90]["notify"].callback(ctx_for(bot), [str(STAFF_CHANNEL), f"<@&{ROLE}>"])
    await MANAGE_REGISTRY[90]["post"].callback(ctx_for(bot), [str(ANNOUNCE_CHANNEL)])
    ctx = ctx_for(bot)
    await MANAGE_REGISTRY[90]["post"].callback(ctx, [str(ANNOUNCE_CHANNEL)])
    assert "already exists" in reply_text(ctx)
    r90.epoch = 2
    await MANAGE_REGISTRY[90]["post"].callback(ctx_for(bot), [str(ANNOUNCE_CHANNEL), "--force"])
    assert announce.send.await_count == 2
    assert announce.send.await_args.args == (w._ANNOUNCEMENT_BODY,)
    post = await Return90Post.get(id=1)
    assert (post.staff_channel_id, post.ping_role_id) == (STAFF_CHANNEL, ROLE)
    assert (post.shown_kind, post.shown_epoch) == ("card", 2)


async def test_post_refuses_unless_card_is_wanted_and_current(_isolated_db, r90, discord_env):
    bot, announce, _, _ = discord_env
    r90.queue_depth = 1
    ctx = ctx_for(bot)
    await MANAGE_REGISTRY[90]["post"].callback(ctx, [str(ANNOUNCE_CHANNEL)])
    assert "Refusing" in reply_text(ctx) and announce.send.await_count == 0


async def test_unconfigured_tick_is_a_noop(monkeypatch, discord_env):
    monkeypatch.delenv("R90_BASE_URL", raising=False)
    monkeypatch.delenv("R90_BOT_SECRET", raising=False)
    called = AsyncMock()
    monkeypatch.setattr(w, "_call", called)
    await w.tick(discord_env[0])
    assert called.await_count == 0


def test_tier_gating():
    subs = MANAGE_REGISTRY[90]
    assert {name: sub.tier for name, sub in subs.items()} == {
        "notify": Tier.ADMIN, "post": Tier.OPERATOR, "status": Tier.STAFF, "sync": Tier.STAFF,
        "takedown": Tier.STAFF, "resume": Tier.STAFF, "pause": Tier.STAFF,
        "resume_intake": Tier.STAFF, "end": Tier.OPERATOR,
    }
    assert TICK_REGISTRY[90] is w.tick
