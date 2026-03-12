from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from familybot.checks import bot_admin_only
from familybot.embeds import build_panel_embed, build_setup_embed
from familybot.services import config_is_ready, fetch_guild_channel
from familybot.utils import human_recruitment_status
from familybot.views.panel import PanelEntryView


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    recruitment = app_commands.Group(name="recruitment", description="Управление набором")
    panel = app_commands.Group(name="panel", description="Управление панелью заявок")
    setup = app_commands.Group(name="setup", description="Настройка ID и параметров")
    botadmin = app_commands.Group(name="botadmin", description="Управление бот-админами")

    @setup.command(name="ids", description="Сохранить каналы, роль и название сервера")
    @bot_admin_only()
    async def setup_ids(
        self,
        interaction: discord.Interaction,
        result_channel_id: str,
        voice_channel_id: str,
        review_role_id: str,
        applications_category_id: str | None = None,
        archive_category_id: str | None = None,
        server_name: str = "Ваш сервер",
    ) -> None:
        assert interaction.guild is not None
        try:
            result_id = int(result_channel_id)
            voice_id = int(voice_channel_id)
            review_id = int(review_role_id)
            app_cat = int(applications_category_id) if applications_category_id else None
            archive_cat = int(archive_category_id) if archive_category_id else None
        except ValueError:
            await interaction.response.send_message("Один из ID указан неверно.", ephemeral=True)
            return

        self.bot.db.upsert_guild_settings(
            interaction.guild.id,
            result_channel_id=result_id,
            voice_channel_id=voice_id,
            review_role_id=review_id,
            applications_category_id=app_cat,
            archive_category_id=archive_cat,
            server_name=server_name,
        )
        await self._refresh_panel(interaction.guild)
        config = self.bot.db.get_guild_settings(interaction.guild.id)
        assert config is not None
        await interaction.response.send_message(embed=build_setup_embed(config), ephemeral=True)

    @panel.command(name="deploy", description="Создать или обновить закреплённую панель")
    @bot_admin_only()
    async def panel_deploy(self, interaction: discord.Interaction, channel_id: str | None = None) -> None:
        assert interaction.guild is not None
        config = self.bot.db.get_guild_settings(interaction.guild.id)
        if not config or not config_is_ready(config):
            await interaction.response.send_message("Сначала выполните /setup ids.", ephemeral=True)
            return

        target_channel: discord.TextChannel | None = None
        if channel_id:
            try:
                parsed = int(channel_id)
            except ValueError:
                await interaction.response.send_message("channel_id должен быть числом.", ephemeral=True)
                return
            fetched = await fetch_guild_channel(interaction.guild, parsed)
            if isinstance(fetched, discord.TextChannel):
                target_channel = fetched
        elif isinstance(interaction.channel, discord.TextChannel):
            target_channel = interaction.channel

        if target_channel is None:
            await interaction.response.send_message("Не удалось определить текстовый канал.", ephemeral=True)
            return

        old_channel_id = config["panel_channel_id"]
        old_message_id = config["panel_message_id"]
        replaced = False

        if old_channel_id and old_message_id:
            old_channel = await fetch_guild_channel(interaction.guild, old_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_message = await old_channel.fetch_message(old_message_id)
                    if old_channel.id == target_channel.id:
                        await old_message.edit(embed=build_panel_embed(interaction.guild, config), view=PanelEntryView(self.bot))
                    else:
                        new_message = await target_channel.send(embed=build_panel_embed(interaction.guild, config), view=PanelEntryView(self.bot))
                        await new_message.pin(reason="Панель заявок")
                        try:
                            await old_message.unpin(reason="Панель перенесена")
                        except discord.HTTPException:
                            pass
                        self.bot.db.upsert_guild_settings(
                            interaction.guild.id,
                            panel_channel_id=target_channel.id,
                            panel_message_id=new_message.id,
                        )
                    replaced = True
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    replaced = False

        if not replaced:
            message = await target_channel.send(embed=build_panel_embed(interaction.guild, config), view=PanelEntryView(self.bot))
            try:
                await message.pin(reason="Панель заявок")
            except discord.HTTPException:
                pass
            self.bot.db.upsert_guild_settings(
                interaction.guild.id,
                panel_channel_id=target_channel.id,
                panel_message_id=message.id,
            )

        await interaction.response.send_message(f"Панель опубликована в {target_channel.mention}.", ephemeral=True)

    @recruitment.command(name="open", description="Открыть приём заявок")
    @bot_admin_only()
    async def recruitment_open(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        self.bot.db.upsert_guild_settings(interaction.guild.id, recruitment_open=1)
        await self._refresh_panel(interaction.guild)
        await interaction.response.send_message(f"Статус: **{human_recruitment_status(True)}**", ephemeral=True)

    @recruitment.command(name="close", description="Закрыть приём заявок")
    @bot_admin_only()
    async def recruitment_close(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        self.bot.db.upsert_guild_settings(interaction.guild.id, recruitment_open=0)
        await self._refresh_panel(interaction.guild)
        await interaction.response.send_message(f"Статус: **{human_recruitment_status(False)}**", ephemeral=True)

    @setup.command(name="show", description="Показать текущую конфигурацию")
    @bot_admin_only()
    async def setup_show(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = self.bot.db.get_guild_settings(interaction.guild.id)
        if not config:
            await interaction.response.send_message("Настройки ещё не заданы.", ephemeral=True)
            return
        await interaction.response.send_message(embed=build_setup_embed(config), ephemeral=True)

    @botadmin.command(name="add", description="Добавить бот-админа по ID")
    @bot_admin_only()
    async def botadmin_add(self, interaction: discord.Interaction, user_id: str) -> None:
        assert interaction.guild is not None
        try:
            parsed = int(user_id)
        except ValueError:
            await interaction.response.send_message("user_id должен быть числом.", ephemeral=True)
            return
        self.bot.db.add_bot_admin(interaction.guild.id, parsed)
        await interaction.response.send_message(f"Пользователь `{parsed}` добавлен в бот-админы.", ephemeral=True)

    @botadmin.command(name="remove", description="Удалить бот-админа по ID")
    @bot_admin_only()
    async def botadmin_remove(self, interaction: discord.Interaction, user_id: str) -> None:
        assert interaction.guild is not None
        try:
            parsed = int(user_id)
        except ValueError:
            await interaction.response.send_message("user_id должен быть числом.", ephemeral=True)
            return
        self.bot.db.remove_bot_admin(interaction.guild.id, parsed)
        await interaction.response.send_message(f"Пользователь `{parsed}` удалён из бот-админов.", ephemeral=True)

    @botadmin.command(name="list", description="Показать список бот-админов")
    @bot_admin_only()
    async def botadmin_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        ids = self.bot.db.list_bot_admin_ids(interaction.guild.id)
        global_ids = self.bot.app_config.global_bot_admin_ids
        description = (
            f"**Глобальные:** {', '.join(f'`{i}`' for i in global_ids) if global_ids else 'нет'}\n"
            f"**Для этого сервера:** {', '.join(f'`{i}`' for i in ids) if ids else 'нет'}"
        )
        embed = discord.Embed(title="👮 Бот-админы", description=description, color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _refresh_panel(self, guild: discord.Guild) -> bool:
        config = self.bot.db.get_guild_settings(guild.id)
        if not config or not config["panel_channel_id"] or not config["panel_message_id"]:
            return False
        channel = await fetch_guild_channel(guild, config["panel_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            message = await channel.fetch_message(config["panel_message_id"])
            await message.edit(embed=build_panel_embed(guild, config), view=PanelEntryView(self.bot))
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
