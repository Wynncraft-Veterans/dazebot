from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "return_guesses" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "week" INT NOT NULL,
    "day" INT NOT NULL,
    "disc_uuid" VARCHAR(255) NOT NULL,
    "price" INT NOT NULL,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_return_gues_week_39b4f8" UNIQUE ("week", "day", "disc_uuid")
) /* One user's emerald-price guess for a ``/return 75`` day-N slot. */;
CREATE INDEX IF NOT EXISTS "idx_return_gues_week_d19667" ON "return_guesses" ("week");
CREATE INDEX IF NOT EXISTS "idx_return_gues_disc_uu_7dd031" ON "return_guesses" ("disc_uuid");
CREATE TABLE IF NOT EXISTS "story_segments" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "week" INT NOT NULL,
    "disc_uuid" VARCHAR(255) NOT NULL,
    "content" TEXT NOT NULL,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) /* One fragment of a collaborative ``/return`` story event. */;
CREATE INDEX IF NOT EXISTS "idx_story_segme_week_88117d" ON "story_segments" ("week");
CREATE INDEX IF NOT EXISTS "idx_story_segme_disc_uu_3b1ee7" ON "story_segments" ("disc_uuid");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "eJztV21P20gQ/iurfDmQmuQIb1V7OomjXIta4AThWhVX9sYeO9vYu+7umuCr+O+dWdt5hQ"
    "gQSEjHlziel52ZxzP2Mz9bGmyhpemcuuv7AoxpvWE/W5JngH9u1L9iLZ7nM1qSWD5IZzz8"
    "hEyhMh4Yq3loURvz1ACKIjChFrkVSpLPiQRWGNC/GQYZaJ5G7VyLEJg7hcVKM86CoFudzX"
    "a3g4BFvGwfM5Mq26EgkQoxipDJI5znSU+eG4jYoESrUCWmC5cgrakdTHcMMPJ3tzt5GQRv"
    "GPBwyHgYQm7RyRSDTBiDpXkScQIZGaYwIa3GHXYuxY8CWA6arQUBHRMErzAIRq//CBP6RS"
    "GiIFhnXtH7fWPLkxra01Nd+nYIzOAzorSZMEzDdwgp+hppqHaWFcYybkbMWB7HnhQxeZVs"
    "zKWlf0LXcIQpcA3Ruqv7I5RN4VV2zCgEKy6wcMybUGw7N4SakQXLRTgyaCJhzIS0nhwLO2"
    "RSMRMOIeMsHHKZQKcpkrLdaO/SvTutklDWVTZCMs3HnqwfnHnrdBmXPAF2KTAIoqERVdBo"
    "TBVigVfdFLrgGqFwCPtWJYCOGtvh4qJFibou4aW7NCC3vn3DW4GHXQE1/gXd5iM/FpBGc4"
    "OAtqhyct+WuZOdnx+++9tZ0pkDP1RpkcmpdV7aoZITcwrYIR/SJSCxPnxiM1MhizStx6gR"
    "VdWgwOoCJqlGU0EEMS9Smq3WH3EhQxop5iLRz9afraVpoygLA1OLQiVpUvEhupfAdVXVtO"
    "bq1UCh9j/sna5t7qy7KpWxiXZKh0jr2jlyyytXh+sUyOZRzEN5KO3NSDbmC1hijg9BsRGs"
    "gLGB52GYYUZ4afc2tna3Xm/ubL1GE5fKRLK7AtbD475Db4pW3bB3BKu2fiqspm/vZwrWZK"
    "yXINsfcn0LZrNOC8hhss+zyzJ+5acgEzsktLa3V8D0796pm1a0cuOq8FNcfamPa1Wv0s1j"
    "6d7N92i9if3/tflCDVSfz+0yau9QY0UGN0M377mAX1S7dpo/zxRNrCE6kWlZN/sK6PqHRw"
    "dn/b2jf6iSzJgfqYNor39Amp6TlgvStZ2F3p0cwj4f9j8wumVfT44PFj9IE7v+1xblxAur"
    "fKnGPo9m5rKRNsBcEw2IRzPfLxIMeDgacx35SxrVU7fZLquyXrYoceQmqsGlNBvafWaVLs"
    "8gyZB93sjL5wxWE3NDpr6pbO9BzGPNnQtTMbI8bFtsM3wQVlzClEMTT6TzmSPKNzPyhxx0"
    "Vyq+eX8q3kdaGSPedTxHoquuLhyTxohKI8skoosOxpPN3lAT4yqlyfCihMsI65IhSiTWhY"
    "6Us8Q71N6BXleZOFb9ndj7ArWu9wHk1p5cINdnoXKJGiuwokQBUWqtimSIkT7jgWl5cOnS"
    "6AYBGSPvxpVAolbpzIFHq4MylAStFBI3i/9ggNtQNFivwKKeblvVpiuSdexu2kx4VLH3DK"
    "eOEKQHjHDh6jW3zDCraHEZiGi6u+DYDZX2pB3TbqYLiQklt5H4F6L+QtSf8ef/hXs+Hves"
    "X5nLSPbh6pbum3F5KhwfmzKtokgHX/pz7KiBa+1o78v6HEP6dHL8vjGfgXf/08lfLwT1ha"
    "A+CUG9/gW7uUpd"
)
