from datetime import datetime, timedelta, timezone
import os


# TODO: annotate roles/channel IDs with their names (and color if applicable) for easier reference
class Config:
    GUILD = 1313769181321236490
    GUILD_DEAD_ALERT_CHANNEL = 1401676479300898939
    GUILD_FULL_ALERT_CHANNEL = 1401676479300898939
    HIATUS_SPOTTED_ALERT_CHANNEL = 1401676479300898939
    # Master kill-switch for hiatus-spotted alerts. When False, the bulk
    # /v3/player poll short-circuits (no API spend) and the server_watcher
    # HIATUS scope still runs (its DB writes feed other features) but the
    # alert post is suppressed. Toggle via /alerts hiatus_mute / hiatus_unmute.
    HIATUS_ALERTS_ENABLED = True

    # --- VETS membership-state roles (see .claude/membership_spec.md §6) ---
    ROLE_REGISTERED = 1407078577450520637  # Never been a vets member
    ROLE_HIATUS = 1407078148440592444  # Was in vets, currently guildless
    ROLE_MEMBER = 1407078065137254563  # Currently in vets
    ROLE_HONOURARY = 1450046372853055498  # Honourary member
    ROLE_WAITLISTED = 1474854046685855795  # On the waitlist

    # Roles that are wiped on /first-install (legacy)
    ROLES_FIRST_INSTALL_WIPE = (
        1313782801933537371,
        1474854046685855795,
        1450046372853055498,
    )

    # Staff role (members can run staff slash commands without having admin perm)
    STAFF_ROLE = 1337993168502788216

    # Channel where blocklist alerts are posted when a blocked user is currently in the in-game guild.
    BLOCKLIST_ALERT_CHANNEL = 1412981365179416616

    # Public-facing Minecraft server address users connect to in order to redeem
    # link codes (the picolimbo mini-server). Surfaced in onboarding DMs.
    MC_PUBLIC_HOST = os.environ.get("MC_PUBLIC_HOST", "verify.wynnvets.org")

    # Channel used as the public fallback when a user with DMs closed clicks
    # the welcome "Link my Minecraft account" button. The bot pings the user
    # there with a view exposing "Try DM again" / "Show my code" buttons,
    # and posts the link-complete/refused confirmation there too.
    LINK_FALLBACK_CHANNEL = 1313786489112494080

    # The original-tier vanity role (<1.0 / 2013). Users may NOT self-assign this via /vanity;
    # only staff /vanity force can grant it.
    ROLE_VANITY_ORIGINAL = 1318063966420729866

    # --- Inactivity thresholds (configurable via /config) ---
    # x: members in VETS with no last_online activity for INACTIVITY_MEMBER_DAYS get a DM warning.
    #    Default: disabled (False) until staff opts in.
    INACTIVITY_MEMBER_DAYS = 14
    INACTIVITY_MEMBER_ENABLED = False
    # y: waitlisted users (Registered/Hiatus/Honourary + Waitlisted) get the waitlist role removed
    #    after this many days. Default: enabled (True).
    INACTIVITY_WAITLIST_DAYS = 3
    INACTIVITY_WAITLIST_ENABLED = True

    # --- Data-integrity janitor (cogs/maintenance/janitor.py) ---
    # JANITOR_ENABLED gates whether the loop runs at all (detect + alert even
    # when repair is off). JANITOR_REPAIR_ENABLED is the live mutation switch:
    # False = log-only (detect + alert), True = actually fix roles / delete
    # kept LinkCodes. JANITOR_ENABLED and the interval are read at cog __init__
    # / decoration time → toggling needs a restart; JANITOR_REPAIR_ENABLED is
    # re-read every tick. See .claude/role_state.md.
    JANITOR_ENABLED = True
    JANITOR_REPAIR_ENABLED = False
    JANITOR_INTERVAL_MINUTES = 360  # 6h
    JANITOR_ALERT_CHANNEL = 1313786561145606216
    JANITOR_ALERT_DELTA = timedelta(hours=6)  # throttle window for "still detecting" summaries
    JANITOR_ALERT_MAX_SAMPLES = 15

    # _check_guild API-down write-guard: a Wynncraft guild response is treated
    # as untrustworthy (membership inference suppressed) when it returns zero
    # members, or fewer than max(ABS, db_count * FRACTION) members vs. how many
    # we currently have stored for that guild — almost certainly a truncated /
    # degraded API response rather than a real mass exodus.
    GUILD_SCAN_MIN_PLAUSIBLE_FRACTION = 0.5
    GUILD_SCAN_MIN_PLAUSIBLE_ABS = 5

    GUILD_DEAD_ALERT_ROLE_USA = 1402295013169172500
    GUILD_DEAD_ALERT_ROLE_EUROPE = 1436108975132119221
    GUILD_DEAD_ALERT_ROLE_ASIA = 1436109140195020892

    @property
    def GUILD_DEAD_ALERT_ROLE(self):
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour

        # TODO, are you sure you want to hardcode this?

        # Night (wraps midnight): 22..23 and 0..5
        if hour > 21 or hour <= 5:
            return self.GUILD_DEAD_ALERT_ROLE_USA
        # Afternoon: 14..21
        elif 13 < hour <= 21:
            return self.GUILD_DEAD_ALERT_ROLE_EUROPE
        # Morning: 6..13
        else:
            return self.GUILD_DEAD_ALERT_ROLE_ASIA

    GUILD_FULL_ALERT_ROLE = 1313778812361904188
    GUILD_DEAD_WHEN = 2
    # Alert fires when ≤ this many slots remain. Cap is derived per-tick from guild.level via get_max_guild_members.
    GUILD_FULL_SLOTS_REMAINING = 2
    GUILD_DEAD_ALERT_DELTA = timedelta(hours=4)
    GUILD_FULL_ALERT_DELTA = timedelta(hours=8)

    # Complete bot control
    ADMINS = {
        887089019190640640  # @wenweia
    }

    class AnniConfig:
        ROLE_ID = 1457366058951249970
        CHANNEL_ID = 1339393368672702567
        WEBHOOK_ID = 1396669909077070007
        TRIGGER = "@Prelude to Annihilation"

    class DocumentationConfig:
        ALLOWED_CHANNELS = {1313786489112494080}

        SECTIONS = ["guides", "guild", "major-changes"]

    class BuildLibraryConfig:
        # Forum channels for /promote and /demote (see cogs/build_library.py).
        # Both must be forum channels; the bot needs Manage Webhooks + Manage Threads + Manage Messages on each.
        # Set to None to disable the cog (e.g. in DevConfig).
        WORKSHOP_FORUM_ID: int | None = 1359223973208002931  # #build-workshop
        LIBRARY_FORUM_ID: int | None = 1504188786794565753  # #build-library

    class VanityRolesConfig:
        CUTOFFS = [
            (dt.replace(tzinfo=timezone.utc), role_id)
            for dt, role_id in [
                (
                    datetime(2013, 5, 7),
                    "1318063966420729866",
                ),  # annotate the roles by their role names and/or color preferably
                (datetime(2013, 6, 29), "1318064262681464882"),  # like: BLABLA name - COLOR
                (datetime(2013, 10, 30), "1318072464982675456"),
                (datetime(2014, 8, 1), "1318072904474165298"),
                (datetime(2014, 12, 22), "1318073239683207219"),
                (datetime(2015, 12, 20), "1318073513453682698"),
                (datetime(2017, 4, 7), "1318073571477815357"),
                (datetime(2017, 12, 15), "1318073572031205376"),
                (datetime(2019, 1, 18), "1318073572777918554"),
                (datetime(2019, 12, 8), "1318073573667246151"),
            ]
        ]


class DevConfig(Config):
    GUILD = 1407388408472666243
    GUILD_DEAD_ALERT_CHANNEL = GUILD_FULL_ALERT_CHANNEL = 1407388410393399494  # general
    HIATUS_SPOTTED_ALERT_CHANNEL = 1407388410393399494  # general (never post into prod from dev)
    LINK_FALLBACK_CHANNEL = 1407388410393399494  # general
    JANITOR_ALERT_CHANNEL = 1407388410393399494  # general (never post into prod from dev)
    GUILD_FULL_ALERT_ROLE = 1409300773439012874  # @aaaaa
    GUILD_DEAD_WHEN = 10
    # n is huge so threshold = max-n <= 0; the test guild always triggers the full-alert path.
    GUILD_FULL_SLOTS_REMAINING = 100
    ADMINS = {
        174134334628823041  # @sjourd
    }

    class AnniConfig(Config.AnniConfig): ...

    class DocumentationConfig(Config.DocumentationConfig): ...

    class VanityRolesConfig(Config.VanityRolesConfig): ...

    class BuildLibraryConfig(Config.BuildLibraryConfig):
        # No dev forum channels configured; /promote and /demote refuse to run on this deployment.
        WORKSHOP_FORUM_ID = None
        LIBRARY_FORUM_ID = None


if os.environ.get("DAZEBOT_DEPLOYMENT") == "production":
    CurrConfig = Config()
else:
    CurrConfig = DevConfig()
