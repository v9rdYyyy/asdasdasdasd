from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from familybot.bot_app import FamilyBot


async def has_bot_admin_access(bot: "FamilyBot", interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False

    if interaction.user.guild_permissions.administrator:
        return True

    if interaction.user.id in bot.app_config.global_bot_admin_ids:
        return True

    return bot.db.is_bot_admin(interaction.guild.id, interaction.user.id)


def bot_admin_only() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        client = interaction.client
        assert isinstance(client, FamilyBot)
        allowed = await has_bot_admin_access(client, interaction)
        if not allowed:
            raise app_commands.CheckFailure("У вас нет доступа к этой команде.")
        return True

    return app_commands.check(predicate)
