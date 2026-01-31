from datetime import datetime, timedelta, timezone
import os


class Config:
    GUILD = 1313769181321236490
    GUILD_DEAD_ALERT_CHANNEL = 1401676479300898939
    GUILD_FULL_ALERT_CHANNEL = 1401676479300898939

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
    GUILD_FULL_WHEN = 92 - 1
    GUILD_DEAD_ALERT_DELTA = timedelta(hours=4)
    GUILD_FULL_ALERT_DELTA = timedelta(hours=8)

    # Complete bot control
    ADMINS = {
        887089019190640640  # @wenweia
    }

    # Player management things
    MODERATORS = ADMINS | {
        0,  # Replace with actual mod
    }

    class AnniConfig:
        ROLE_ID = 1457366058951249970
        CHANNEL_ID = 1339393368672702567
        WEBHOOK_ID = 1396669909077070007
        TRIGGER = "@Prelude to Annihilation"


class DevConfig(Config):
    GUILD = 1407388408472666243
    GUILD_DEAD_ALERT_CHANNEL = GUILD_FULL_ALERT_CHANNEL = 1407388410393399494
    GUILD_FULL_ALERT_ROLE = 1409300773439012874
    GUILD_DEAD_WHEN = 10
    GUILD_FULL_WHEN = 10
    ADMINS = {
        174134334628823041  # @sjourd
    }
    MODERATORS = (
        ADMINS
        | Config.MODERATORS
        | {
            0,
        }
    )

    class AnniConfig(Config.AnniConfig): ...


if os.environ.get("DAZEBOT_DEPLOYMENT") == "production":
    CurrConfig = Config()
else:
    CurrConfig = DevConfig()
