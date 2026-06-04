from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "ctp_boards" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "enum" VARCHAR(8) NOT NULL UNIQUE,
    "board_number" INT NOT NULL,
    "role_id" VARCHAR(64),
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) /* One reward board (DEV \/ TXT \/ ART \/ OPS \/ ...). ``enum`` is the short */;
        CREATE TABLE IF NOT EXISTS "ctp_glint_investments" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "total_invested" INT NOT NULL DEFAULT 0,
    "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "discord_account_id" CHAR(36) NOT NULL UNIQUE REFERENCES "discord_accounts" ("id") ON DELETE CASCADE
) /* Cumulative-only invested-points total per user. The spec is explicit: */;
        CREATE TABLE IF NOT EXISTS "ctp_prizes" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "category" VARCHAR(16) NOT NULL,
    "enum_name" VARCHAR(32) NOT NULL,
    "cost" INT NOT NULL,
    "duration_seconds" INT,
    "display" VARCHAR(255) NOT NULL,
    "disclaimer" TEXT,
    "disabled" INT NOT NULL DEFAULT 0,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_ctp_prizes_categor_168ccf" UNIQUE ("category", "enum_name")
) /* An entry in the redeemable-prize catalog. ``(category, enum_name)`` */;
        CREATE TABLE IF NOT EXISTS "ctp_ledger" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "amount_delta" INT NOT NULL,
    "source" VARCHAR(16) NOT NULL,
    "task_number" INT,
    "prize_display_at_time" VARCHAR(255),
    "prize_category_at_time" VARCHAR(16),
    "expires_at" TIMESTAMP,
    "counterparty_disc_uuid" VARCHAR(255),
    "actor_disc_uuid" VARCHAR(255) NOT NULL,
    "comment" TEXT,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "board_id" CHAR(36) REFERENCES "ctp_boards" ("id") ON DELETE SET NULL,
    "discord_account_id" CHAR(36) NOT NULL REFERENCES "discord_accounts" ("id") ON DELETE CASCADE,
    "prize_id" CHAR(36) REFERENCES "ctp_prizes" ("id") ON DELETE SET NULL
) /* Append-only point ledger. Balance = SUM(amount_delta) per user. */;
CREATE INDEX IF NOT EXISTS "idx_ctp_ledger_source_10d099" ON "ctp_ledger" ("source");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "ctp_glint_investments";
        DROP TABLE IF EXISTS "ctp_boards";
        DROP TABLE IF EXISTS "ctp_ledger";
        DROP TABLE IF EXISTS "ctp_prizes";"""


MODELS_STATE = (
    "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"
    "DeO2YA=="
)
