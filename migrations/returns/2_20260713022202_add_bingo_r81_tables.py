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
    "embed_msg_1_id" BIGINT,
    "embed_msg_2_id" BIGINT,
    "embed_msg_3_id" BIGINT,
    "embed_msg_4_id" BIGINT,
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
    "line_key" VARCHAR(32) NOT NULL,
    "bonus_choice" VARCHAR(16),
    "bonus_msg_id" BIGINT,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "team_id" CHAR(36) NOT NULL REFERENCES "return_81_teams" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_return_81_b_team_id_6669cd" UNIQUE ("team_id", "line_key")
) /* One completed bingo line for a team. ``line_key`` is the canonical */;
CREATE INDEX IF NOT EXISTS "idx_return_81_b_bonus_m_a6c2ad" ON "return_81_bingo_events" ("bonus_msg_id");
        CREATE TABLE IF NOT EXISTS "return_81_cell_states" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "cell" VARCHAR(2) NOT NULL,
    "caption" TEXT NOT NULL,
    "team_id" CHAR(36) NOT NULL REFERENCES "return_81_teams" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_return_81_c_team_id_119807" UNIQUE ("team_id", "cell")
) /* Per-cell placeholder caption, seeded once when the team enters */;
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
    "cell" VARCHAR(2) NOT NULL,
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
        DROP TABLE IF EXISTS "return_81_bingo_events";
        DROP TABLE IF EXISTS "return_81_submissions";
        DROP TABLE IF EXISTS "return_81_invites";
        DROP TABLE IF EXISTS "return_81_teams";
        DROP TABLE IF EXISTS "return_81_cell_states";
        DROP TABLE IF EXISTS "return_81_team_members";"""


MODELS_STATE = (
    "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"
)
