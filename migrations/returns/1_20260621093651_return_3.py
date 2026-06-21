from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "return_3_dashboards" (
    "cult" VARCHAR(32) NOT NULL PRIMARY KEY,
    "thread_id" BIGINT NOT NULL,
    "message_id" BIGINT NOT NULL
) /* One row per cult: which message in which thread holds that cult's */;
        CREATE TABLE IF NOT EXISTS "return_3_game_state" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "phase" VARCHAR(16) NOT NULL,
    "started_at" TIMESTAMP NOT NULL,
    "drafting_deadline" TIMESTAMP,
    "lot_naz" INT,
    "lot_deer" INT,
    "lot_wen" INT,
    "lot_fish" INT,
    "turn_order_csv" VARCHAR(64),
    "current_turn_number" INT NOT NULL DEFAULT 0,
    "turn_started_at" TIMESTAMP,
    "dominance_streak_loops" INT NOT NULL DEFAULT 0,
    "dominance_leader_cult" VARCHAR(32),
    "winner_cult" VARCHAR(32),
    "ended_at" TIMESTAMP
) /* Singleton row holding the in-progress Return 3 game. */;
        CREATE TABLE IF NOT EXISTS "return_3_subscriptions" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "disc_uuid" VARCHAR(255) NOT NULL UNIQUE,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) /* Opt-in turn-alert subscription. Cult is resolved at notify-time via */;
        CREATE TABLE IF NOT EXISTS "return_3_tiles" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "controlling_cult" VARCHAR(32),
    "army_count" INT NOT NULL DEFAULT 0,
    "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) /* One tile on the 5x5 board. ``id`` == row * 5 + col (0..24). */;
        CREATE TABLE IF NOT EXISTS "return_3_votes" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "turn_number" INT NOT NULL,
    "voter_disc_uuid" VARCHAR(255) NOT NULL,
    "action_kind" VARCHAR(16) NOT NULL,
    "source_tile_id" INT NOT NULL,
    "target_tile_id" INT,
    "voted_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_return_3_vo_turn_nu_80630d" UNIQUE ("turn_number", "voter_disc_uuid")
) /* One vote in one turn. UNIQUE on (turn_number, voter) — vote changes */;
CREATE INDEX IF NOT EXISTS "idx_return_3_vo_turn_nu_0ac0e2" ON "return_3_votes" ("turn_number");
CREATE INDEX IF NOT EXISTS "idx_return_3_vo_voter_d_f7c7c0" ON "return_3_votes" ("voter_disc_uuid");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "return_3_dashboards";
        DROP TABLE IF EXISTS "return_3_game_state";
        DROP TABLE IF EXISTS "return_3_votes";
        DROP TABLE IF EXISTS "return_3_subscriptions";
        DROP TABLE IF EXISTS "return_3_tiles";"""


MODELS_STATE = (
    "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"
)
