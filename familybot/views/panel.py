from __future__ import annotations

import discord

from familybot.embeds import build_panel_embed
from familybot.services import config_is_ready


class PanelEntryView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Здесь вы можете подать заявку",
        style=discord.ButtonStyle.secondary,
        emoji="📩",
        custom_id="family:panel:open",
    )
    async def open_panel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Кнопка работает только на сервере.", ephemeral=True)
            return

        config = self.bot.db.get_guild_settings(interaction.guild.id)
        if not config or not config_is_ready(config):
            await interaction.response.send_message("Панель ещё не настроена.", ephemeral=True)
            return

        if not bool(config["recruitment_open"]):
            await interaction.response.send_message("Сейчас набор закрыт.", ephemeral=True)
            return

        embed = discord.Embed(
            title="✉️ Здесь можно заполнить анкету!",
            description="Нажмите кнопку ниже, чтобы открыть форму и отправить заявку.",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, view=OpenModalView(self.bot), ephemeral=True)


class OpenModalView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Заполнить анкету", style=discord.ButtonStyle.success, emoji="📝")
    async def fill_form(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from familybot.views.review import FamilyApplicationModal

        await interaction.response.send_modal(FamilyApplicationModal(self.bot))
