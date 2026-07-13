from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "return_81_teams" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "week" INT NOT NULL,
    "team_number" INT NOT NULL UNIQUE,
    "creator_disc_uuid" VARCHAR(255) NOT NULL,
    "state" VARCHAR(16) NOT NULL,
    "thread_id" BIGINT,
    "status_msg_id" BIGINT,
    "embed_msg_ids_json" TEXT,
    "expansion_count" INT NOT NULL DEFAULT 0,
    "picker_msg_id" BIGINT,
    "picker_candidates_json" TEXT,
    "pending_invite_msg_id" BIGINT,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) /* One r81 team. Grows through phases: pending (invites out) → picking */;
CREATE INDEX IF NOT EXISTS "idx_return_81_t_week_417d67" ON "return_81_teams" ("week");
CREATE INDEX IF NOT EXISTS "idx_return_81_t_creator_5cc349" ON "return_81_teams" ("creator_disc_uuid");
CREATE INDEX IF NOT EXISTS "idx_return_81_t_picker__fb82e3" ON "return_81_teams" ("picker_msg_id");
CREATE INDEX IF NOT EXISTS "idx_return_81_t_pending_30c8bc" ON "return_81_teams" ("pending_invite_msg_id");
        CREATE TABLE IF NOT EXISTS "return_81_bingo_events" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "line_key" TEXT NOT NULL,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "team_id" CHAR(36) NOT NULL REFERENCES "return_81_teams" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_return_81_b_team_id_6669cd" UNIQUE ("team_id", "line_key")
) /* One completed bingo line for a team. ``line_key`` is the canonical */;
        CREATE TABLE IF NOT EXISTS "return_81_cell_states" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "cell" VARCHAR(8) NOT NULL,
    "caption" TEXT NOT NULL,
    "team_id" CHAR(36) NOT NULL REFERENCES "return_81_teams" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_return_81_c_team_id_119807" UNIQUE ("team_id", "cell")
) /* Per-cell placeholder caption, seeded when a cell is first added to */;
        CREATE TABLE IF NOT EXISTS "return_81_invites" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "invitee_disc_uuid" VARCHAR(255) NOT NULL,
    "state" VARCHAR(16) NOT NULL,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "team_id" CHAR(36) NOT NULL REFERENCES "return_81_teams" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_return_81_i_team_id_87124e" UNIQUE ("team_id", "invitee_disc_uuid")
) /* One outstanding (or resolved) invite to join a specific team. Both */;
CREATE INDEX IF NOT EXISTS "idx_return_81_i_invitee_13ec2f" ON "return_81_invites" ("invitee_disc_uuid");
        CREATE TABLE IF NOT EXISTS "return_81_submissions" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "cell" VARCHAR(8) NOT NULL,
    "submitter_disc_uuid" VARCHAR(255) NOT NULL,
    "image_url" TEXT NOT NULL,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "team_id" CHAR(36) NOT NULL REFERENCES "return_81_teams" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_return_81_s_team_id_7ad310" UNIQUE ("team_id", "cell")
) /* One accepted photo submission for a given team+cell. Overwrites are */;
        CREATE TABLE IF NOT EXISTS "return_81_team_members" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "disc_uuid" VARCHAR(255) NOT NULL,
    "role" VARCHAR(16) NOT NULL,
    "joined_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "team_id" CHAR(36) NOT NULL REFERENCES "return_81_teams" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_return_81_t_team_id_4afc2c" UNIQUE ("team_id", "disc_uuid")
) /* One confirmed member of a team. ``role`` records how they joined: */;
CREATE INDEX IF NOT EXISTS "idx_return_81_t_disc_uu_50868b" ON "return_81_team_members" ("disc_uuid");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "return_81_submissions";
        DROP TABLE IF EXISTS "return_81_bingo_events";
        DROP TABLE IF EXISTS "return_81_team_members";
        DROP TABLE IF EXISTS "return_81_teams";
        DROP TABLE IF EXISTS "return_81_invites";
        DROP TABLE IF EXISTS "return_81_cell_states";"""


MODELS_STATE = (
    "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"
)
