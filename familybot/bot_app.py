from __future__ import annotations

import logging

import discord
from discord.ext import commands

from familybot.config import BotConfig
from familybot.constants import APP_STATUS_APPROVED_PENDING, APP_STATUS_RESERVE_PENDING, APP_STATUS_SUBMITTED
from familybot.database import Database


class FamilyBot(commands.Bot):
    def __init__(self, app_config: BotConfig):
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.app_config = app_config
        self.db = Database(app_config.db_path)
        self.logger = logging.getLogger("familybot")

    async def setup_hook(self) -> None:
        from familybot.cogs.admin import AdminCog
        from familybot.views.panel import PanelEntryView
        from familybot.views.review import ApplicationReviewView, InterviewResultView

        await self.add_cog(AdminCog(self))

        self.add_view(PanelEntryView(self))
        for application in self.db.get_open_applications():
            claimed = bool(application["reviewer_id"])
            if application["status"] == APP_STATUS_SUBMITTED:
                self.add_view(ApplicationReviewView(self, int(application["id"]), claimed=claimed))
            elif application["status"] in (APP_STATUS_APPROVED_PENDING, APP_STATUS_RESERVE_PENDING):
                self.add_view(InterviewResultView(self, int(application["id"]), claimed=claimed))

        synced = await self.tree.sync()
        self.logger.info("Slash-команды синхронизированы: %s", len(synced))

    async def on_ready(self) -> None:
        self.logger.info("Бот запущен как %s (%s)", self.user, self.user.id if self.user else "?")
