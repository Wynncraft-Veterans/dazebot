"""Idempotent schema-catchup for r81 board expansion.

Migration 2 was regenerated mid-day 2026-07-13 to introduce the
``expansion_count`` / ``embed_msg_ids_json`` columns and drop the four
fixed ``embed_msg_N_id`` columns + ``bonus_choice`` / ``bonus_msg_id`` on
``return_81_bingo_events``. Any container that applied migration 2 in its
pre-regeneration form ended up with the *old* schema recorded as
"migration 2 applied" — a plain ``CREATE TABLE IF NOT EXISTS`` on the
new file won't add the missing columns.

This migration inspects each affected table via ``PRAGMA table_info`` and
issues only the ALTERs that are still needed. Safe to run on:
  - A fresh DB (returns.db just created by the current migration 2) —
    every check reports the target state; no ALTERs run.
  - A stale DB stuck on the old migration 2 form — the missing columns
    get added and the deprecated ones get dropped.

SQLite ≥ 3.35 (Mar 2021) is required for ``ALTER TABLE ... DROP COLUMN``.
Docker's python:3.13-slim ships with 3.4x, so this is fine in prod.
"""
from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def _table_columns(db: BaseDBAsyncClient, table: str) -> set[str]:
    """Return the column names of ``table`` per ``PRAGMA table_info``.

    Returns ``set()`` if the table doesn't exist so callers can no-op
    cleanly (e.g. a very fresh install where earlier migrations haven't
    materialised yet — shouldn't happen given RUN_IN_TRANSACTION ordering,
    but cheap insurance).
    """
    _, rows = await db.execute_query(f'PRAGMA table_info("{table}")')
    # PRAGMA table_info yields dict-like rows with a "name" key.
    return {r["name"] for r in rows}


async def upgrade(db: BaseDBAsyncClient) -> str:
    teams_cols = await _table_columns(db, "return_81_teams")
    events_cols = await _table_columns(db, "return_81_bingo_events")

    add_stmts: list[str] = []
    if teams_cols and "embed_msg_ids_json" not in teams_cols:
        add_stmts.append(
            'ALTER TABLE "return_81_teams" ADD COLUMN "embed_msg_ids_json" TEXT'
        )
    if teams_cols and "expansion_count" not in teams_cols:
        add_stmts.append(
            'ALTER TABLE "return_81_teams" '
            'ADD COLUMN "expansion_count" INT NOT NULL DEFAULT 0'
        )
    for stmt in add_stmts:
        await db.execute_query(stmt)

    # Backfill embed_msg_ids_json from the legacy fixed columns, but ONLY
    # if those legacy columns still exist. Rows with any-null legacy id
    # get NULL (dashboard will be re-posted on next expansion or manual
    # rerender-hard).
    legacy_ids = ("embed_msg_1_id", "embed_msg_2_id", "embed_msg_3_id", "embed_msg_4_id")
    if all(col in teams_cols for col in legacy_ids):
        json_expr = (
            "json_array("
            + ", ".join(f'"{c}"' for c in legacy_ids)
            + ")"
        )
        not_null_pred = " AND ".join(f'"{c}" IS NOT NULL' for c in legacy_ids)
        await db.execute_query(
            f'UPDATE "return_81_teams" SET "embed_msg_ids_json" = {json_expr} '
            f'WHERE "embed_msg_ids_json" IS NULL AND {not_null_pred}'
        )

    drop_stmts: list[str] = []
    for legacy in legacy_ids:
        if legacy in teams_cols:
            drop_stmts.append(
                f'ALTER TABLE "return_81_teams" DROP COLUMN "{legacy}"'
            )
    for legacy in ("bonus_choice", "bonus_msg_id"):
        if legacy in events_cols:
            drop_stmts.append(
                f'ALTER TABLE "return_81_bingo_events" DROP COLUMN "{legacy}"'
            )
    for stmt in drop_stmts:
        await db.execute_query(stmt)
    return ""


async def downgrade(db: BaseDBAsyncClient) -> str:
    # No downgrade — the deprecated columns' data is lost by design and
    # the new columns hold state the current code depends on.
    return ""


# The post-migration model snapshot. Identical to migration 2's target
# (this migration only reconciles the DB schema against migration 2's
# already-declared shape), so we reuse the same encoded blob.
MODELS_STATE = (
    "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"
)
