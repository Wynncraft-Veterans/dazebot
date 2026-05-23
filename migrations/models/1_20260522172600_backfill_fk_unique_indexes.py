"""Backfill UNIQUE indexes for FKs that ``orm.py`` declares OneToOne (and the
initial migration emits as ``... UNIQUE REFERENCES``) but that the legacy
``Tortoise.generate_schemas()`` path silently dropped at the DDL layer.

On the production DB at first deploy this migration is the FIRST one that
runs for real — the initial migration is fake-applied because the schema
already exists. So this is where the actual uniqueness gate gets attached
to the four affected columns. On fresh dev DBs the columns already carry
a UNIQUE constraint from the initial migration's CREATE TABLE, so
``CREATE UNIQUE INDEX IF NOT EXISTS`` is effectively a no-op (it creates a
redundant secondary index, harmless and cheap).

Verified prior to writing this migration: zero duplicate values existed on
any of the four columns in the ephemeral production snapshot at
2026-05-22, so the constraint adds cleanly without rejecting rows.
"""

from tortoise import BaseDBAsyncClient


RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE UNIQUE INDEX IF NOT EXISTS "uniq_cult_memberships_discord_account_id" ON "cult_memberships" ("discord_account_id");
        CREATE UNIQUE INDEX IF NOT EXISTS "uniq_discord_accounts_minecraft_account_id" ON "discord_accounts" ("minecraft_account_id");
        CREATE UNIQUE INDEX IF NOT EXISTS "uniq_waitlist_minecraft_account_id" ON "waitlist" ("minecraft_account_id");
        CREATE UNIQUE INDEX IF NOT EXISTS "uniq_blocklist_minecraft_account_id" ON "blocklist" ("minecraft_account_id");
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "uniq_cult_memberships_discord_account_id";
        DROP INDEX IF EXISTS "uniq_discord_accounts_minecraft_account_id";
        DROP INDEX IF EXISTS "uniq_waitlist_minecraft_account_id";
        DROP INDEX IF EXISTS "uniq_blocklist_minecraft_account_id";
    """


MODELS_STATE = (
    "eJztXVtz2ziy/isov8SeY8mOk0lyMrtTJV8y8cSXrCxPtna0RUEkJCEmAS1BSqOdmvPbTz"
    "dASuJFiijLtujwJbFAoAk0msDXje7GnzuedJir6seutO9croKd9+TPHUE9Bn9kH+6THToc"
    "zh5hQUC7rq7dTVTrqsCnNtLrUVcxKHKYsn0+DLgUWP1WMV8RKUgw4IpgO0J9RnrSt5lDAg"
    "nljDRZHx4wH0p86TJChUNsKohgI+aTLrMl9BRpSxvex0V/o5Tb4qMUMvSpPyEH5COnQajg"
    "j0vmdaHOAflCeYDkmVMnN4yRuu3S0GEHnq6gBnxoqSGz655D2uHhIX171K1jb0PB/xMyK5"
    "B9Bl3xoc+//xuKuXDYH0zFP4d3Vo8z10lMCXeQgC63gslQl93enp9+0DWRE13Llm7oiVnt"
    "4SQYSDGtHobcqWMbfNZngvkURjA3USJ03WhS4yLTYygI/JBNu+rMChzWo6GL073zt14obJ"
    "xlot+E/7z+eScjAPiW1MxFRbYUKDxcBMiLP/8yo5qNWZfu4KtOPjaau6/e7OlRShX0ff1Q"
    "c2TnL92QBtQ01XydMdJnVEE/Msw8GVA/n5mzFimGQmdXYGXEqCkn4yozVs4+lJiXMY/WY9"
    "yOR/+wXCb6wQB+/nh4uISTvzWamplQS3NTwsdrPuur6NGReYZcnXFRf/PMsboTy+HKtnCy"
    "i/B0UftScvjoxx9X4DDUWshh/SzJYRvEDkZs0SDL11N4EnCP5fM22TLFUSdqWo//WGcxeA"
    "QGwxica+FOouldwt/W+eXZTatx+RlH4in1H1ezqNE6wydHunSSKt19k5qKKRHy5bz1keBP"
    "8q/rq7P0AjOt1/rXDvaJhoG0hBxb1JlbFOPSmDGJifW4YLZPe4FFbVuGIrCKre2L2j/uan"
    "+/6V1/accNsnc3t7JjQZfad2PqO1bmiTySubtAhonZGbgWrCXhHz0L59B9KmyWw+gILF3G"
    "FBszglvK9lnp7BU+HU9Rx0IRg1HDWFlgFvfGzUnj9Gznr8QMJBmOj7wjL11CBe3r8WE3sV"
    "Mx4JTBiRQ93r8GLOZzh+Wi0kyl5ehUBjhgqG/JqIFaDah+BiinQR6ZNkQsSczbCQ2A2d0w"
    "gFLFAjLilBzY0SMHOEgAScJInXoGp26KcFusBD5f3Rd73rFJkb09qr7WVv7on8SD7eSLEe"
    "iIujARX3NRaIv9EeRzNdlqM8x9+H182b599s9WYsuOmbh72fjnXmLbvri++iWuPsf0k4vr"
    "4xRqCofOmqgp2bJCTU+KmnTn77XVb3BTCrnrfPalJ6PpyO5IyRrLtyOsaw3jyituRQ0ylv"
    "6dGshh7WeXd7VhAnaM0KsFA5xzMiVIhsyHJx5sLt0JOTDlLLsH3ZtiW7TFrYoKHYZlaGLp"
    "gXRrO4v0eZ8L6k7fQyLCu0oSXR9p2xTIqImwiWBjvbExkGUCAi0JD7RxJhSoK8KvfSiGXR"
    "NIyB7RSg6MhVDSAykbtIUUbK9OmnKstNXHABUHzUEqtG2mVC90457WyU1AXUZ8rL0bjb8t"
    "oh6CKITUdScxjf3MGPrwsn3CAru+p18G3wMV8LJwiP3Q4xewkk+5hT3GQgWCkyamOdnpIB"
    "NgOfKG+EpYhTod6AdUJOMBMxRdqgLiM/yw4FUejAgEFkphOXGANdh9+BnNZUR7OjWhAjiB"
    "HFWaVqcD6I75f4e32PDZ9Xrx3LXFD7CuhQAAYfwwJz9MKUYvBH6hpQzWR+R+l4FoMCTMFW"
    "EckQXSbwuAXS8UtAko7oNkAJ2BRwByKBmz7kDKuxr3QLJgP8MlNBoX8ikWnLaYMgqWuQDk"
    "Yq8yom2JES2SCctIWa4Cuxgf5jYuI1p883oFsPjm9UKsiI+S8CWW+PUYm9+64iw+ixbi9S"
    "2WCwmUBYo/gs1yyqPi8DvVtMLfW2a1TMOTovOb176ck1ySSY2HnZnVLVGrTsxcZJQpXb5U"
    "hbKhxsp6U+fAZ0HoC3KISNcN3gMA9cJA4/sa+8N2Q8VHoLkw6sGTU1jYpe8gWvb1qfBXyU"
    "We8rQJsoj7W6gpjUHEyW4bvqd+6DOEyu2dPQTUlKStyqADOYCEOTBZa16oPnFR66NegdSR"
    "mYiyEcMRGkRqEqok+GEB7J9CA0D+3OgCUe/aYujzEXxqsQaIz3BsoAGMUHkQpB0eHb58jS"
    "/SL+90bNlXdVAH4NOoG4ao+pixO+uw02kLtGnOrJJEq3mo0S1sCsiwPuJsrOrwsTEfX27h"
    "75iY7UularpPRhuB+aiTq2g5IMqc8JtFR/O5ywh8Daa3Wi0B7nlDicoEaVy0zpo4qvcIud"
    "93uOCB5XQ7ZAxqjAxBVSLRkvGTJgvKSg30RVBtQP573HVB8ZSe0cxYn9oT1OIWjUoPxfAV"
    "RPz2omW1PjbPGqc3Ws0bEj8U6LnQxmMGPwiHoCUz1NKaEQ2ga7mSwqxVStC2KEH6/wIIMq"
    "5fAXJ8tkTDOeb9c7HADL5MteErnbo9gbdAH99T+9+jo1ev3h4dvnrz7sfXb9/++O7wHdTV"
    "fco+eruE38fnv5xfpYzhWFA5EHwXUFzjhYJOA/NtNrmYPyncLuoqkGJhln8fpM94X3xik0"
    "c89H80Fn7z2H9eSBJH/U0Q2eb5SSt11r+AuTPIp3LW9qjxh09N5tL4vCSfu6gKXE6JlYu3"
    "fz207jTHmAVaVJJ1y/UpKzVpq6hW81rNC1BWALGNjMZQJ5eRLkSmutB7VEc8beMX+uAFAX"
    "WOZrUBqm0REZkqTUxETsBGDyC3V+f/uD0js1kzXsMMIbxj2sauLwDQP3wiu+iFkfAIqnD4"
    "tuBwVKbXwjmJhhXM2TKYo5elYh/JXJPvBeTMcyy1chVkXn7r79SrNCmGG0CKsVlza2Xvm+"
    "hw7uNawQ90ZU/clNhlWV3QDze59W+xmH6T4fkf5EP74J5e3jARXMh+HqycPVyKKB3PUlDP"
    "cqOK3wSTLXh8hwZQVlMDGZDTS0B+Lu5tE4KUyC6r9+ukx30V1NALCFAgsQfAPFU7vdyPrM"
    "wZNLkZsmhh1zCUBxMCDIaf/T00No8ZcaR4ERA1pN7Mv0db+X021EYUd7IIJ/6+kzhGvgN5"
    "2/l3BR4fGTyu5QtQnf/nn/9rGS7Ax7h+OVm4eZu4XjTXOFufNau0mC3QYrbkhP0UmPoLuh"
    "s3XObnnrWnaizf0/HYpa+9lynWXs1MtFgEKsvJ029+1RFRtepsfNVJ6l95q05GQ1uy6iR1"
    "oGrRKf+isy2Iu7SRhXN4Ef2UrAV2k4UuFKlWazlRrLVcH96DmcaF4ujl67ev371683rqOT"
    "EtWeYwkXWO2MIg/LRfSomspaucCrtc3Fk+g7Gp4J7nwhdAqmkobSeyWHgoPC+C1L0vH2be"
    "B2WzLSdXMdjh2T1ZcYM0yswDXJPvywOkUWYeBNKfWIr1dQzoPXmBtG4MqZKxZNlhyVakLX"
    "mijWozWUtuzlrk6vbiYumRSeoQ2ks40uQLZcz2B/BqenyH1aWyWUAX/IAHK+fmXOVSCg4f"
    "5U6OQphXbX+ZVqgPbKzowMbyTJPCWV18ph2NotDsOJ66yzCuOaIJtTDu4CDvhChz2rQxym"
    "3hM6oVOVUnLdBTgQadYDAD+wPK3Yl2doo8o9DniWKwAhpayO7U0wkDwoGDLrdNAIgr+9yu"
    "nJa2RQs2ltRiSvB8m3IemTxMprwBFYK5BZmZbFWxc859Vy9XBdmZbFVZaCoLe2VhfyALuz"
    "6xO6FDavNgsvBsL6fWUkxlNhc7alCd8T0XoFGtQNUKtOkV6DyOzr40u37e+pOps3T1mcV7"
    "x2mmVtPmQOMmMgy6oO472fBwZrz8MHZDR3BzIbRmFL2LdMMgkDnx/RuhiuH9jeGQCacmQQ"
    "AJDR0eoBpmwsmPXg9qGFKum9hSuo4cCwzJd5gPep0OVYGvxJ9An0xqLx3mAqolvhy1vvFA"
    "KowRh95AG0tzDydBh5QH9oCB9ojGBoKFoFTuChmQD5+USVmmjBKqJGZQC4PQj4L9faZzCO"
    "DoOp0Dw+yoUL8CY/FdRjEjwIBrkx0ZShi8zsUWmNRnULdGg5rWSONoeoeKvouVoAeVFrol"
    "m0NadIog/ry2D6VGlS22nfog1OuxNa9tObXTB/GPRJFby4Mgr205+fowiqpJsZjl6OKMxH"
    "NNysLIZfDvIdIRVw69FezeHOz+leqjg4Uqf+L5Urj91dQsouXvNJnOTVRTtMcwvawMApfp"
    "HLXM12cZCP0QadQQCvd9jJaJ3vNCxWHdGai9EaptoULPwyS1u5iA6cBDQQTIKmx2EFWuDy"
    "d7dYzgjoPESZRHN2r5kwn/jmoTnaiJpFF3pzPTiQFlB5jI2OYOwxy9s9S3vzauzlvXTatx"
    "cdZsWadnF60GoGAB+J4MqCLMpUOFN0ddct+XPgwi4YV90BZZ00188FMB5y0BzpVVpVreN7"
    "28o1vbicy/9GT6bOmyrn3s7LjaCik8htGRNYZChqDDuzolRugxZ2+WevBvtZ/jpBz4AoIv"
    "yMuJuD4xNJV8YpM4q6AHCD3KZQir7K4rx8y3qUI6SsJaXPNDgUGYaKCYEoGa0XYBu0EiHS"
    "I0wMDPaXwm1t6HJZvj7X/E4b0e87HTzlzmETyC1w6G+JpEZKfJsDhi/hj2ImbI4payy8WI"
    "utyh0yZDn0sghK+rzt23ZeGek61Cx5vJZmU839y8Or4tnvzPRAF3CklkXL+cLHz5ZgUOvk"
    "zjgBkD8VF10v5sEVl1pdOzm9htutJpPoBkAdieiy/5Bt6ej2nZrK/E7/n+3umMOfdKGrJ4"
    "g3lcT645+Ch9r64G0kf/7fsAyOSOswr6WQx+Xn/rUr9q+3kWq1R2+9n2pGpPemhSIDndFk"
    "ZcloqXma2zUFTQd57ZuNiFxks+/w1w9v4J6raHr2unqHs0qJeR4xy8lyfri0FfRpYqL9ny"
    "mwWLWrHKnIri1Sq2l1eLbS+vMrYX7TtehH3TBmvx7wlu8Hh4+994IsRapulMw3JaBB8mlm"
    "mLrP3PgqF4B7ElhQt7YFE9N9W0nIpuSRTbeNhLNVs9Ieaua8seMPturRlNE6jmdSvmVTEm"
    "4B9/lHfPzZK7mXPalnKP3vwBk2GIJbv6j3UsffkUNvDBPFmGiGfxvZhsEngZSNEJTbasJv"
    "KJJzKQdyxnDpfEWsQNSrnErbLCLV7gMusbVxbwQIY+9SdZJh5L6TIqFhgWUk1T7OxC24fa"
    "9osaW1YX/+Pr64uE5B+fpz3+by+Pz2AX0TyGStxYvEy2wQJHncl76RxresFxagoKXp221U"
    "hreSqwKllgNlmgJXv348QzSRc49GWPKQWkLRt635c+v2/2wM9TkicJiiXiUCEb+oyZXVfa"
    "dy5XOVi2eGaz43liW7d5riRc3zzuKc6Vwuc9W8qaMeXBpiTlyxyt8nBj7aMn9xvHTu7qR0"
    "7uquFaDcfh+Dd1Z873JD6xIjQIqD1gDsYw0cSdm6TLJlI46MHOc+7n3ATVthj6XIdr4T4P"
    "1TGd3S5U0/7/B4D493TeO/IiqvfCxAqogLsu/Im5B6RI3/JZzzvkxbAurojmJBlINwrqkn"
    "jQtjANwe95y0D2kL26rKmKuqqcrConq8rJ6tnwcomTVeUItClHoKUCuwG+fs+ua0/kZCW/"
    "UtG/gjk9QfyXC3ZTVZbjXV3ZMjmnptVXTu0sAqJbYQJm816Nakjt51kEpyvlXThUZLduuz"
    "R02MEsybelhsyuew5ph4eH9O2Pe9lg1wd7U1s0WQ/WpAGBGQfY3Lls/NNq/HKmkw0MqHBc"
    "k14LM0WLPrt3Jq3K76m439MSL7I1fE0qR5NcR5Mq6utZQP1tivrKtfLmbFWLrMGL96uFJu"
    "lHiQfDtxsBrywSj22RmPE+d8k/E6GXQa0J3iYIPPH6v/Oh0bw8v/oFG/iezjv64fzmoynh"
    "AFiwBCroAniuf3+5vj49uW21dOFYSscOg0A/aVycfDy7PL+5fE+oCyDJ48qD0ubl9W1T14"
    "aXyNDXdU+urz/pMhvQki759ezL2YUu+srGzNVlNyfN82Ndht3u6rJW4/zi2tALKHeloffl"
    "rPH5+urmElYz0zFGhyAPHg8G015/uW5+mvZ6LH18bxrpreRqtEpo4cvFsYUvM4kVokUk5x"
    "h+NaGab//UMvX54uz4PRm6rNsWv12fn74nI8mdtjg9a16dn7zHdLeC2zCP563G1fktCEvA"
    "YVQ8BFk5Ob+6ahw3miAWXAjapRmD9Crz826F6Xm3cHbeZXyKKxtLFchWWQMW46wm9DDkAV"
    "699g9MYJ2HsTJ1luIrf1bbwpzYq6Irncjbdrl9F9/FFCXrniO4NAn4im2/kep71TTfJnnV"
    "ojTf38jvTRLpvdtCzS6ZmuavMmm8a/A9cpcz+Gwwz6ECiEUVvCCdsb3TqbJWbQnMXCuTdJ"
    "WZO5uyKvIpWzeJ9ILmld1mymCzPK9jt0m2rOw2W3BEuyWGG3O5bw6KmN76uxg6zC4X3rQt"
    "ho3YA+TjqbbHNbZHFctBkpcAaBYE4sT1U9zk26oe7Og7dmrTy+7fQRXdl2nJ2yWcNQ7plb"
    "/F5v0t9BpQkHvzbb4Xni3R+afL6D31/C+M3bmTs1HpLvzeT6n48/JR5ad5PLeUJ8I2eDlW"
    "LrbRD5ZjG6xSZZ8pP36p/FqfkdKUiBnHL5T5BRFCslWFEWKGVJtbYnNLislWbWoB7fUaet"
    "09E0H+CUCmzvKtDmvDNo7VLcBHqx8BNLQtHg3paAqnRAHrXUZs+G5xWzggMFx9mcQBYV+Z"
    "HRCNv9AAD3Ki7fOZk4H7k2zD41ofTfI6ExbRwyO0D7OtAqA4i60ZunTC/Dq5YawtOp16/S"
    "B2I5xniap7zuwCDNKD6SDatxBPJk5CL8SArNG0izV9YycJZEDdaAzm2kHyd3Jze7mrH6s9"
    "fc0Foa7bFjBYpWt2OtEFhbgOQX9Ia+Cz2dhNSyCDIT3tnZgRuri981NbBGNJEqWK7P6dvE"
    "kR2AOB50MTq4MLbK1LBV6Cip6R0nXq5JTZdILHKHjowb2hy/A8hjk/kUg44ttIsNPAfl9z"
    "ogkbjbli5JezFjmgQ36gr20V1DXsrEXsPPgTh/cX3j4S32GiZ23EAgXS+QKPSw7GI6Kz3n"
    "Q6e4QKTRj7G4c9dTr/Fw1UAR1bevC5ONCNLz4PAiZMPz5f3yzrCHYABoBfZNSDgHlDiXH2"
    "NZNNBK+eEiiDWoKMaynxQ6F7GDG109nX/YXewJ9toafxQIsmvICL6J4rZ08zCQOvzBzDMN"
    "Onp8QDtkN7/A9Zz/5AHtOA6NtQiL7xtVZri+jNCuYVRspgplQII4glPBRR5JcRb1UnXxi5"
    "YxNkCqxxKOkJMcPDLfLhk77/BQgwX8uKwImF7567GA8GTPDgNdq9Y0j96PisOsLaEow7N5"
    "9FTlxSzUp5jLX5hIsxVyLfYYD/VozvizI2h0Q5j7M2f1gIa7D0C0tsslU5ebl5iY24cg+B"
    "XUyhnDzevLzeQdeLcDSuX07+bT7Dm8GeBc6RZg2+14Mkz7gKZXm2+NrouSYlSbu1zCD1EL"
    "dGV+bAZ2QO3BYfCtg8Jzesj2ryTq45Zu75/nJTDNS0lKlawA+z51PdBN0pKejDKGMwC9ou"
    "Aeqoz4IQ9VOi6RuTSb5T5jqEULG9VfEFo/qSaP1ARdXVwZixO+vtq/pw0um8J4zaA8xrwo"
    "Z4R/SUKnn7qtPB26a7WtVEpVO7fSpt70B1U+ctMfYX/XqutN0HJTy0A9MBtAKZMIWpWYXC"
    "O7AH0G3Tw/l7ptG4ACTQq1+Y60VxCHjDNTxNX54aUdHeoKB7f/iEkaNzh6Nah6bQRRgPiz"
    "qJbcjXUAWgOXP7TsFzwcYEbUTt8Ojw5WvQ9YnCEA4aBZ7WCfreYFdMUpa+1Feh+jLsD7Q5"
    "QUjfoy4q8nPvBj52OtppB2/PRoFShmEo4zVt5rHvSD8EaU/dwI33scYesdr6kjrZM7dxAy"
    "e73Jn5u8LHOECT2Jijq6u5NLYyCGyJQQCFrgDWiqs/FNLatAVgw0Ar+t6LAK25JmVB+BXS"
    "qpBWlVBoew9iK2+jcnsbAQT2f6OCB5OTgQRQlKcJZOos1QbQHmeNdHXL1vVXVAma+jQUER"
    "7FE0+dJHBAFTH3SbgTgkgUj4yIoQ7Iz2VkxCk5MAUaOWYUhM2QbQtcXTwA2zbRSd5/lVwA"
    "OkUNggLw7wutgHgAmcnVdUsflPrcYTrbICDyG8bIN9O9HFVYdFuw6LbcZP+4CXEeJAAIv6"
    "fc7XZJVNWsSVmA6iMw0sa4T2F1J5Y+288ydGlK/pzWVVb+KsXQMwT/25Ri6Dfm894E0G8e"
    "sJo9XIqoRrqadccmq3q4EVeKfs014eSM+gB59M0mEQgKAVYIQDIgfiqVlvmFIpFLUwZIbY"
    "RqW9gYaI6uZug5lvJdQjNlp0OHvI53GGKTuvT7nY7xQzo3LkPaqHkQ0TMOZ8zAOi2k+DAU"
    "mHie/A1Y9vPUoYn7M+eluBv7cb+03RMacyhC6yo6Ek2ICrsKA3yhv6OX5Avr3gBZhmH7Ag"
    "ghu9GxydfuZTzA91AB70dWoPOdD9Ndzx/miLrc0ZyC4Qx4lCDHof9lXRloe+bMEQzJoTeY"
    "LxEuxhbea2NfRre2BK/rpMlqkWkzwSgSGbZn1lBgT1vsgui6si9DZeYEr8c4ASHUDYDgXp"
    "1oz0ztuOWzEcy4RsfoTaYtv1DoAdxtCyCHEyh96FcAY0tXM4W61t5PmpSts8ITBV1Ew7Ch"
    "Pm/jptABFZumZyzQjmRo3qXcnbqoAZ+55i227HQ8O3YVM8u0wiwIIJc/KEGHaiC1kxrebg"
    "Y90QkWf9CSBBzBer5OiiDd0ZyPmh362oks5jaiFEX+J85ZfnkS5z1H3zMtQHgwYIMCpHqh"
    "m+z+vjG6a7EdD2ScvZHgEMhunFmf6cSRRl/Yb4vp3QzM2ScssOt7BFQEM6E+7w904nV4Gx"
    "nzAF2B9ShyJKHSMrZEy7hjCzI6LXDPYBtM4PS4msXmfVsqDW1jioVXnJHePdn41Felbd6f"
    "7QlvFn5qZm7+48ZtsAgX4/rltBa8OlpFGI8WC+NRxlRQHWM9B002e4ylr+PFi3HWmNp02+"
    "p60ie+nnSm7xSdymTLaiKfYCK3xMA0vTgtx740f6naYvPS/DVuVdqAUuuT1bb/TLf9MqQY"
    "fmhFeIP+K0WW6kLJiOMLL1f0ddlEKuJHYvtDJiK+3wY4l+Ypbw9MZoFasg3qipZxAq/2wt"
    "LvhQGgmmLxr3GDUtpgHsQw+KQe2Q9uWN2AQ3aBnSWVnPKeF5RPE51uHwxbtGEUu5G84C7Q"
    "YD63B3kbQPRk6dpPZ3W2ZtFf+JXlrvk531j0uZT9E1u2xo8wpYv5TFZd5eealNNY/CALPX"
    "4aBZgYVS8nA18eHq4SJ354uDhQHJ+tGI3z6831VdFonFsBA/zd4XawT9Ay8+/tZOsSLuKo"
    "E6p1JjYnHYaTAnhI4Pi+qtt9t5e//h9VblDs"
)
