"""Persistent button + ephemeral select + modal for cross-cult messaging.

The pinned button is installed in each cult's thread at cult-creation
time (``~manage_return 0 createCult`` calls :func:`install_intercult_in_thread`)
and can be bulk-reinstalled across all cult threads via
``/script install_intercult``. Any cult member can click the button to
deliver one message to another cult's thread; the same text is mirrored
back into the sender's own thread so everyone in the sending cult sees
what was sent on their behalf. Each cult is rate-limited to one outbound
intercult message per 24 hours, regardless of which member sent it.

The button itself is persistent (registered with ``bot.add_view`` in
``bot.py:setup_hook``). The select menu and modal are per-click; if the
bot restarts mid-flow the user just re-clicks the button.

Thread mapping is read from the ``Cult.thread_id`` column. Cults without a
thread_id set are silently skipped (they aren't valid recipients/senders
for cross-cult messaging until an operator configures the thread).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

from orm import Cult, CultMembership, DiscordAccount, IntercultMessage

logger = logging.getLogger("dazebot.cogs.events.returns.lib.views.intercult_view")


INTERCULT_BUTTON_CUSTOM_ID = "intercult:button"
INTERCULT_SELECT_CUSTOM_ID = "intercult:select"

COOLDOWN = timedelta(hours=24)


async def _sender_cult_for(disc_uuid: str) -> Optional[Cult]:
    disc = await DiscordAccount.filter(disc_uuid=disc_uuid).first()
    if disc is None:
        return None
    membership = await CultMembership.filter(discord_account=disc).first()
    if membership is None:
        return None
    return await Cult.get(id=membership.cult_id)


async def _cooldown_remaining(cult_name: str) -> Optional[timedelta]:
    cutoff = datetime.now(timezone.utc) - COOLDOWN
    latest = (
        await IntercultMessage.filter(
            sender_cult_name=cult_name, sent_at__gte=cutoff
        )
        .order_by("-sent_at")
        .first()
    )
    if latest is None:
        return None
    remaining = (latest.sent_at + COOLDOWN) - datetime.now(timezone.utc)
    return remaining if remaining.total_seconds() > 0 else None


def _format_remaining(td: timedelta) -> str:
    total = max(0, int(td.total_seconds()))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


async def _resolve_thread(bot, thread_id: int) -> Optional[discord.Thread]:
    ch = bot.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("intercult: thread %s not fetchable: %s", thread_id, e)
        return None
    return ch if isinstance(ch, discord.Thread) else None


class _IntercultMessageModal(discord.ui.Modal):
    message = discord.ui.TextInput(
        label="Your message",
        placeholder="Type the message to send to the other cult.",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=1500,
    )

    def __init__(self, sender_cult: str, target_cult: str):
        super().__init__(title=f"Message {target_cult.capitalize()}")
        self.sender_cult = sender_cult
        self.target_cult = target_cult

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Re-resolve sender cult: the clicker may have switched cults
        # between opening this dialog and submitting.
        current = await _sender_cult_for(str(interaction.user.id))
        if current is None or current.name != self.sender_cult:
            await interaction.followup.send(
                "Your cult membership changed since you opened this dialog. "
                "Close this and re-click the button to start over.",
                ephemeral=True,
            )
            return

        # Re-check cooldown: someone else in the cult may have raced through
        # the window after this user opened the modal.
        remaining = await _cooldown_remaining(self.sender_cult)
        if remaining is not None:
            await interaction.followup.send(
                f"`{self.sender_cult}` already sent an intercult message recently. "
                f"Try again in **{_format_remaining(remaining)}**.",
                ephemeral=True,
            )
            return

        target_row = await Cult.filter(name=self.target_cult).first()
        sender_row = await Cult.filter(name=self.sender_cult).first()
        target_thread_id = target_row.thread_id if target_row else None
        sender_thread_id = sender_row.thread_id if sender_row else None
        if not target_thread_id or not sender_thread_id:
            await interaction.followup.send(
                "Internal error: one of the cult threads is no longer configured.",
                ephemeral=True,
            )
            return

        target_thread = await _resolve_thread(interaction.client, target_thread_id)
        sender_thread = await _resolve_thread(interaction.client, sender_thread_id)
        if target_thread is None or sender_thread is None:
            await interaction.followup.send(
                "Couldn't reach one of the cult threads. Ask staff to investigate.",
                ephemeral=True,
            )
            return

        text = str(self.message.value).strip()

        # Deliver to the recipient first. If that fails, don't consume the
        # cooldown and don't mirror back — the send hasn't happened.
        try:
            await target_thread.send(
                f"**Message from `{self.sender_cult}`:**\n{text}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            logger.warning(
                "intercult: deliver to target %s (%s) failed: %s",
                target_thread_id, self.target_cult, e,
            )
            await interaction.followup.send(
                "Failed to deliver to the recipient cult; nothing was sent.",
                ephemeral=True,
            )
            return

        try:
            await sender_thread.send(
                f"**Sent to `{self.target_cult}`:**\n{text}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            # Recipient already got the message; the mirror is best-effort.
            # Log and still consume the cooldown so the rate-limit holds.
            logger.warning(
                "intercult: mirror to sender %s (%s) failed: %s",
                sender_thread_id, self.sender_cult, e,
            )

        await IntercultMessage.create(
            sender_cult_name=self.sender_cult,
            target_cult_name=self.target_cult,
            sender_disc_uuid=str(interaction.user.id),
            content=text,
        )

        await interaction.followup.send(
            f"Delivered to `{self.target_cult}`. `{self.sender_cult}` can send "
            "another intercult message in 24 hours.",
            ephemeral=True,
        )


class _IntercultCultSelectView(discord.ui.View):
    """Ephemeral select-menu shown after the button is clicked. On select,
    opens the message-body modal. Not persistent — discarded after the
    clicker's flow finishes (or after timeout).
    """

    def __init__(self, sender_cult: str, candidate_names: list[str]):
        super().__init__(timeout=300)
        self.sender_cult = sender_cult
        self._candidate_names = set(candidate_names)
        options = [
            discord.SelectOption(label=name.capitalize(), value=name)
            for name in candidate_names
        ]
        select: discord.ui.Select = discord.ui.Select(
            placeholder="Pick a cult to message",
            options=options,
            min_values=1,
            max_values=1,
            custom_id=INTERCULT_SELECT_CUSTOM_ID,
        )
        select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        target = interaction.data["values"][0]  # type: ignore[index]
        if target not in self._candidate_names or target == self.sender_cult:
            await interaction.response.send_message(
                "Invalid target cult.", ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            _IntercultMessageModal(sender_cult=self.sender_cult, target_cult=target)
        )


class IntercultButtonView(discord.ui.View):
    """Persistent view pinned in every cult thread.

    Register exactly once via ``bot.add_view(IntercultButtonView())`` in
    ``setup_hook`` so callbacks survive bot restarts.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Send an intercult message",
        style=discord.ButtonStyle.primary,
        emoji="✉️",
        custom_id=INTERCULT_BUTTON_CUSTOM_ID,
    )
    async def send_intercult(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        cult = await _sender_cult_for(str(interaction.user.id))
        if cult is None:
            await interaction.response.send_message(
                "You aren't in a cult, so you can't send an intercult message. "
                "Use `/joincult` or `/return 0` to join one first.",
                ephemeral=True,
            )
            return

        if not cult.thread_id:
            await interaction.response.send_message(
                f"Your cult `{cult.name}` has no thread configured for intercult messaging.",
                ephemeral=True,
            )
            return

        # Build the candidate list: every other cult with a thread_id.
        candidates = [
            c.name
            for c in await Cult.filter(thread_id__not_isnull=True).order_by("name")
            if c.name != cult.name
        ]
        if not candidates:
            await interaction.response.send_message(
                "No other cult is configured for intercult messaging yet.",
                ephemeral=True,
            )
            return

        remaining = await _cooldown_remaining(cult.name)
        if remaining is not None:
            await interaction.response.send_message(
                f"`{cult.name}` has already sent an intercult message recently. "
                f"Try again in **{_format_remaining(remaining)}**.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"You're sending on behalf of `{cult.name}`. Pick a recipient cult:",
            view=_IntercultCultSelectView(sender_cult=cult.name, candidate_names=candidates),
            ephemeral=True,
        )


INTERCULT_PINNED_BODY = (
    "**Cross-cult messaging.**\n"
    "Click below to send a single message to another cult's thread. "
    "Your cult can send one such message every 24 hours; the same text "
    "is mirrored back here for transparency."
)


async def install_intercult_in_thread(
    bot, thread: discord.Thread
) -> str:
    """Idempotently install + pin the intercult button in one thread.

    Returns one of ``"posted"`` (sent and pinned now), ``"skipped"`` (a
    pinned button with our ``custom_id`` was already present), or
    ``"failed"`` (send raised). A pin failure after a successful send still
    reports ``"posted"`` — the button itself is in place; only the pin is
    missing, which staff can fix manually.

    ``createCult`` calls this with the new cult's thread to wire the
    button up on first creation. ``install_intercult_button`` (below) is
    just a bulk wrapper for one-shot reinstalls via ``/script``.
    """
    if await _intercult_button_already_pinned(thread):
        return "skipped"
    try:
        msg = await thread.send(INTERCULT_PINNED_BODY, view=IntercultButtonView())
    except discord.HTTPException as e:
        logger.warning(
            "intercult install: send to %s failed: %s", thread.id, e,
        )
        return "failed"
    try:
        await msg.pin(reason="intercult bootstrap")
    except discord.HTTPException as e:
        logger.warning(
            "intercult install: pin in %s failed: %s", thread.id, e,
        )
    return "posted"


async def install_intercult_button(bot) -> tuple[int, int, int, int]:
    """Bulk-install the intercult button across every cult thread.

    Idempotent (delegates per-thread to :func:`install_intercult_in_thread`).
    Cults with no ``thread_id`` or an unresolvable thread are counted as
    ``unresolved``. Returns ``(posted, skipped, failed, unresolved)``.

    Bootstrap-only — call from ``/script install_intercult``. Routine cult
    creation goes through ``createCult`` which installs the button at
    creation time.
    """
    posted = skipped = failed = unresolved = 0
    for cult in await Cult.all():
        if not cult.thread_id:
            unresolved += 1
            continue
        thread = await _resolve_thread(bot, cult.thread_id)
        if thread is None:
            unresolved += 1
            continue
        result = await install_intercult_in_thread(bot, thread)
        if result == "posted":
            posted += 1
        elif result == "skipped":
            skipped += 1
        else:
            failed += 1
    return posted, skipped, failed, unresolved


async def _intercult_button_already_pinned(thread: discord.Thread) -> bool:
    try:
        pins = await thread.pins()
    except discord.HTTPException as e:
        logger.warning("intercult install: pins() on %s failed: %s", thread.id, e)
        return False
    for msg in pins:
        for row in msg.components:
            for child in getattr(row, "children", []):
                if (
                    isinstance(child, discord.Button)
                    and child.custom_id == INTERCULT_BUTTON_CUSTOM_ID
                ):
                    return True
    return False
