from __future__ import annotations

import discord

from familybot.constants import (
    APP_STATUS_ACCEPTED,
    APP_STATUS_APPROVED_PENDING,
    APP_STATUS_INTERVIEW_FAILED,
    APP_STATUS_REJECTED,
    APP_STATUS_RESERVE_PENDING,
    APP_STATUS_SUBMITTED,
)
from familybot.embeds import build_application_embed, build_interview_prompt_embed
from familybot.services import (
    archive_application_channel,
    config_is_ready,
    create_application_channel,
    decision_requires_submitted,
    fetch_guild_channel,
    fetch_user,
    interview_pending,
    member_can_review,
    safe_dm,
    send_interview_success_log,
    send_primary_log,
    send_rejection_notifications,
)


PENDING_INTERVIEW_STATUSES = (APP_STATUS_APPROVED_PENDING, APP_STATUS_RESERVE_PENDING)


def claimed_reviewer_id(application) -> int:
    return int(application["reviewer_id"] or 0) if application else 0


async def refresh_review_message(bot, guild: discord.Guild, application_id: int, *, disabled: bool = False) -> None:
    application = bot.db.get_application(application_id)
    if not application:
        return

    channel = await fetch_guild_channel(guild, application["channel_id"])
    if not isinstance(channel, discord.TextChannel) or not application["review_message_id"]:
        return

    try:
        message = await channel.fetch_message(int(application["review_message_id"]))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    applicant = guild.get_member(application["user_id"]) or await fetch_user(bot, application["user_id"])
    if applicant is None:
        return

    reviewer_mention = f"<@{claimed_reviewer_id(application)}>" if claimed_reviewer_id(application) else None
    embed = build_application_embed(
        guild,
        applicant,
        int(application["id"]),
        bot.db.answers_from_row(application),
        reviewer_mention=reviewer_mention,
    )
    view = ApplicationReviewView(
        bot,
        int(application["id"]),
        disabled=disabled,
        claimed=bool(claimed_reviewer_id(application)),
    )
    try:
        await message.edit(embed=embed, view=view)
    except discord.HTTPException:
        pass


async def refresh_interview_message(bot, guild: discord.Guild, application_id: int, *, disabled: bool = False) -> None:
    application = bot.db.get_application(application_id)
    if not application:
        return

    channel = await fetch_guild_channel(guild, application["channel_id"])
    if not isinstance(channel, discord.TextChannel) or not application["interview_message_id"]:
        return

    try:
        message = await channel.fetch_message(int(application["interview_message_id"]))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    reviewer_mention = f"<@{claimed_reviewer_id(application)}>" if claimed_reviewer_id(application) else None
    view = InterviewResultView(
        bot,
        int(application["id"]),
        disabled=disabled,
        claimed=bool(claimed_reviewer_id(application)),
    )
    try:
        await message.edit(embed=build_interview_prompt_embed(reviewer_mention), view=view)
    except discord.HTTPException:
        pass


class FamilyApplicationModal(discord.ui.Modal, title="Подать заявку на вступление в семью"):
    def __init__(self, bot):
        super().__init__(timeout=600)
        self.bot = bot

        self.identity = discord.ui.TextInput(
            label="Ник / Имя ИРЛ / Возраст ИРЛ",
            placeholder="Vordy Nutsovich / Давид / 19",
            max_length=120,
            required=True,
        )
        self.rp_experience = discord.ui.TextInput(
            label="Ваш опыт на RP проекте",
            placeholder="Мой путь начинался с далекого 2019...",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.gta_hours_family_experience = discord.ui.TextInput(
            label="Количество часов в GTA / опыт в семьях",
            placeholder="У меня около 7000 часов. Опыт в семьях...",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.gunfight_rollback = discord.ui.TextInput(
            label="Откат с ГГшки",
            placeholder="Откат: Любая карта, Спешик/Тяга + Сайга. 7+ минут.",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.family_time_online_tz = discord.ui.TextInput(
            label="Время семье / Средний онлайн / Часовой пояс",
            placeholder="5 / 6 / МСК",
            max_length=200,
            required=True,
        )

        for item in (
            self.identity,
            self.rp_experience,
            self.gta_hours_family_experience,
            self.gunfight_rollback,
            self.family_time_online_tz,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Форму можно отправить только на сервере.", ephemeral=True)
            return

        config = self.bot.db.get_guild_settings(interaction.guild.id)
        if not config or not config_is_ready(config):
            await interaction.response.send_message("Панель ещё не настроена.", ephemeral=True)
            return

        if not bool(config["recruitment_open"]):
            await interaction.response.send_message("Сейчас набор закрыт.", ephemeral=True)
            return

        active = self.bot.db.get_active_user_application(interaction.guild.id, interaction.user.id)
        if active:
            await interaction.response.send_message(
                "У вас уже есть активная заявка. Дождитесь решения рекрутера.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = await create_application_channel(
            interaction.guild,
            interaction.user,
            int(config["review_role_id"]),
            config["applications_category_id"],
        )

        answers = {
            "identity": str(self.identity),
            "rp_experience": str(self.rp_experience),
            "gta_hours_family_experience": str(self.gta_hours_family_experience),
            "gunfight_rollback": str(self.gunfight_rollback),
            "family_time_online_tz": str(self.family_time_online_tz),
        }
        application_id = self.bot.db.create_application(interaction.guild.id, interaction.user.id, channel.id, answers)
        view = ApplicationReviewView(self.bot, application_id, claimed=False)
        message = await channel.send(
            content=f"<@&{config['review_role_id']}>, поступила новая заявка.",
            embed=build_application_embed(interaction.guild, interaction.user, application_id, answers),
            view=view,
        )
        self.bot.db.update_application(application_id, review_message_id=message.id)
        self.bot.add_view(view)

        await interaction.followup.send("Ваша заявка отправлена. Ожидайте ответа рекрутеров.", ephemeral=True)


class ApplicationReviewView(discord.ui.View):
    def __init__(self, bot, application_id: int, disabled: bool = False, claimed: bool = False):
        super().__init__(timeout=None)
        self.bot = bot
        self.application_id = application_id
        self.take_button.custom_id = f"family:take:{application_id}"
        self.approve_button.custom_id = f"family:approve:{application_id}"
        self.reserve_button.custom_id = f"family:reserve:{application_id}"
        self.reject_button.custom_id = f"family:reject:{application_id}"

        decisions_enabled = claimed and not disabled
        self.take_button.disabled = disabled or claimed
        self.approve_button.disabled = not decisions_enabled
        self.reserve_button.disabled = not decisions_enabled
        self.reject_button.disabled = not decisions_enabled

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Действие доступно только на сервере.", ephemeral=True)
            return False

        config = self.bot.db.get_guild_settings(interaction.guild.id)
        if not config or not member_can_review(interaction.user, config["review_role_id"]):
            await interaction.response.send_message("У вас нет доступа к рассмотрению заявок.", ephemeral=True)
            return False

        application = self.bot.db.get_application(self.application_id)
        if not application:
            await interaction.response.send_message("Заявка не найдена.", ephemeral=True)
            return False

        custom_id = str((interaction.data or {}).get("custom_id", ""))
        reviewer_id = claimed_reviewer_id(application)

        if custom_id.startswith("family:take:"):
            if application["status"] != APP_STATUS_SUBMITTED:
                await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
                return False
            if reviewer_id and reviewer_id != interaction.user.id:
                await interaction.response.send_message(
                    f"Эту заявку уже взял на рассмотрение <@{reviewer_id}>.",
                    ephemeral=True,
                )
                return False
            if reviewer_id == interaction.user.id:
                await interaction.response.send_message("Эта заявка уже у вас на рассмотрении.", ephemeral=True)
                return False
            return True

        if application["status"] != APP_STATUS_SUBMITTED:
            await interaction.response.send_message("Начальный этап по этой заявке уже завершён.", ephemeral=True)
            return False

        if not reviewer_id:
            await interaction.response.send_message("Сначала нажмите «Взять на рассмотрение».", ephemeral=True)
            return False

        if reviewer_id != interaction.user.id:
            await interaction.response.send_message(
                f"Эту заявку рассматривает <@{reviewer_id}>.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="Взять на рассмотрение", style=discord.ButtonStyle.secondary, emoji="👀")
    async def take_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        claimed = self.bot.db.claim_application(self.application_id, interaction.user.id)
        if not claimed:
            application = self.bot.db.get_application(self.application_id)
            reviewer_id = claimed_reviewer_id(application)
            if application and reviewer_id and reviewer_id != interaction.user.id:
                await interaction.response.send_message(
                    f"Эту заявку уже взял на рассмотрение <@{reviewer_id}>.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("Не удалось взять заявку на рассмотрение.", ephemeral=True)
            return

        await refresh_review_message(self.bot, interaction.guild, self.application_id)
        await interaction.response.send_message("Заявка закреплена за вами.", ephemeral=True)

    @discord.ui.button(label="Одобрить", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await handle_primary_decision(self.bot, interaction, self.application_id, APP_STATUS_APPROVED_PENDING)

    @discord.ui.button(label="Отправить в резерв", style=discord.ButtonStyle.primary, emoji="🟡")
    async def reserve_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await handle_primary_decision(self.bot, interaction, self.application_id, APP_STATUS_RESERVE_PENDING)

    @discord.ui.button(label="Отказать", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            RejectionReasonModal(self.bot, self.application_id, mode="initial_reject", placeholder="Стрельба мувмент")
        )


class InterviewResultView(discord.ui.View):
    def __init__(self, bot, application_id: int, disabled: bool = False, claimed: bool = True):
        super().__init__(timeout=None)
        self.bot = bot
        self.application_id = application_id
        self.accept_button.custom_id = f"family:interview:accept:{application_id}"
        self.fail_button.custom_id = f"family:interview:fail:{application_id}"
        self.accept_button.disabled = disabled or not claimed
        self.fail_button.disabled = disabled or not claimed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Действие доступно только на сервере.", ephemeral=True)
            return False
        config = self.bot.db.get_guild_settings(interaction.guild.id)
        if not config or not member_can_review(interaction.user, config["review_role_id"]):
            await interaction.response.send_message("У вас нет доступа к рассмотрению заявок.", ephemeral=True)
            return False

        application = self.bot.db.get_application(self.application_id)
        if not application or not interview_pending(application):
            await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
            return False

        reviewer_id = claimed_reviewer_id(application)
        if not reviewer_id:
            await interaction.response.send_message("Сначала возьмите заявку на рассмотрение.", ephemeral=True)
            return False
        if reviewer_id != interaction.user.id:
            await interaction.response.send_message(
                f"Эту заявку рассматривает <@{reviewer_id}>.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Отлично! Принят!", style=discord.ButtonStyle.success, emoji="🎉")
    async def accept_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        application = self.bot.db.get_application(self.application_id)
        if not application or not interview_pending(application):
            await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = self.bot.db.transition_application(
            self.application_id,
            PENDING_INTERVIEW_STATUSES,
            APP_STATUS_ACCEPTED,
            interaction.user.id,
            require_reviewer_id=interaction.user.id,
        )
        if not updated:
            await interaction.followup.send("Не удалось завершить заявку: состояние уже изменилось.", ephemeral=True)
            return

        config = self.bot.db.get_guild_settings(interaction.guild.id)
        updated_application = self.bot.db.get_application(self.application_id)
        await refresh_interview_message(self.bot, interaction.guild, self.application_id, disabled=True)
        if config and updated_application:
            await send_interview_success_log(interaction.guild, config, updated_application, interaction.user)
            await archive_application_channel(interaction.guild, config, updated_application)
        await interaction.followup.send("Обзвон отмечен как успешный. Канал скрыт в архив.", ephemeral=True)

    @discord.ui.button(label="Плохо 😢 Не принят.", style=discord.ButtonStyle.danger, emoji="🚫")
    async def fail_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            RejectionReasonModal(self.bot, self.application_id, mode="interview_reject", placeholder="читер!!!")
        )


class RejectionReasonModal(discord.ui.Modal, title="Причина отказа"):
    def __init__(self, bot, application_id: int, mode: str, placeholder: str):
        super().__init__(timeout=600)
        self.bot = bot
        self.application_id = application_id
        self.mode = mode
        self.reason = discord.ui.TextInput(
            label="Укажите причину отказа",
            placeholder=placeholder,
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Действие доступно только на сервере.", ephemeral=True)
            return

        config = self.bot.db.get_guild_settings(interaction.guild.id)
        if not config or not member_can_review(interaction.user, config["review_role_id"]):
            await interaction.response.send_message("У вас нет доступа.", ephemeral=True)
            return

        application = self.bot.db.get_application(self.application_id)
        if not application:
            await interaction.response.send_message("Заявка не найдена.", ephemeral=True)
            return

        valid = decision_requires_submitted(application) if self.mode == "initial_reject" else interview_pending(application)
        if not valid:
            await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
            return

        reviewer_id = claimed_reviewer_id(application)
        if not reviewer_id:
            await interaction.response.send_message("Сначала возьмите заявку на рассмотрение.", ephemeral=True)
            return
        if reviewer_id != interaction.user.id:
            await interaction.response.send_message(
                f"Эту заявку рассматривает <@{reviewer_id}>.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        new_status = APP_STATUS_REJECTED if self.mode == "initial_reject" else APP_STATUS_INTERVIEW_FAILED
        from_statuses = (APP_STATUS_SUBMITTED,) if self.mode == "initial_reject" else PENDING_INTERVIEW_STATUSES
        updated = self.bot.db.transition_application(
            self.application_id,
            from_statuses,
            new_status,
            interaction.user.id,
            require_reviewer_id=interaction.user.id,
            reason=str(self.reason),
        )
        if not updated:
            await interaction.followup.send("Не удалось завершить заявку: состояние уже изменилось.", ephemeral=True)
            return

        updated_application = self.bot.db.get_application(self.application_id)
        if updated_application is None:
            await interaction.followup.send("Заявка уже недоступна.", ephemeral=True)
            return

        await send_rejection_notifications(self.bot, interaction.guild, config, updated_application, interaction.user, str(self.reason))
        if self.mode == "initial_reject":
            await refresh_review_message(self.bot, interaction.guild, self.application_id, disabled=True)
        else:
            await refresh_interview_message(self.bot, interaction.guild, self.application_id, disabled=True)
        await archive_application_channel(interaction.guild, config, updated_application)
        await interaction.followup.send("Отказ отправлен. Канал скрыт в архив.", ephemeral=True)


async def handle_primary_decision(bot, interaction: discord.Interaction, application_id: int, target_status: str) -> None:
    application = bot.db.get_application(application_id)
    if not application or not decision_requires_submitted(application):
        await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
        return

    reviewer_id = claimed_reviewer_id(application)
    if not reviewer_id:
        await interaction.response.send_message("Сначала нажмите «Взять на рассмотрение».", ephemeral=True)
        return
    if reviewer_id != interaction.user.id:
        await interaction.response.send_message(
            f"Эту заявку рассматривает <@{reviewer_id}>.",
            ephemeral=True,
        )
        return

    assert interaction.guild is not None
    config = bot.db.get_guild_settings(interaction.guild.id)
    if not config:
        await interaction.response.send_message("Настройки сервера не найдены.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    updated = bot.db.transition_application(
        application_id,
        (APP_STATUS_SUBMITTED,),
        target_status,
        interaction.user.id,
        require_reviewer_id=interaction.user.id,
    )
    if not updated:
        await interaction.followup.send("Не удалось сохранить решение: состояние заявки уже изменилось.", ephemeral=True)
        return

    updated_application = bot.db.get_application(application_id)
    if updated_application is None:
        await interaction.followup.send("Заявка уже недоступна.", ephemeral=True)
        return

    user = await fetch_user(bot, updated_application["user_id"])
    if user:
        if target_status == APP_STATUS_APPROVED_PENDING:
            text = (
                f'Вы были приняты на сервере "{config["server_name"]}". '
                f'Заходите в голосовой канал <#{config["voice_channel_id"]}> для обзвона!\n'
                f'Рассматривал заявку: {interaction.user.mention}'
            )
        else:
            text = (
                f"Заявка от пользователя {user.mention}\n\n"
                "На вступление в семью была отклонена. 😢\n\n"
                "Причина: На данный момент свободных слотов в семье нету. Вы будете добавлены в базу резерва "
                "и как только будут освобождаться слоты в семье, рекрут вас пригласит на обзвон.\n"
                f"Рассматривал заявку: {interaction.user.mention}"
            )
        await safe_dm(user, text)

    await send_primary_log(interaction.guild, config, updated_application, interaction.user, target_status)
    await refresh_review_message(bot, interaction.guild, application_id, disabled=True)
    await ensure_interview_prompt(bot, interaction.guild, application_id)
    summary = "Одобрено" if target_status == APP_STATUS_APPROVED_PENDING else "Отправлено в резерв"
    await interaction.followup.send(f"Решение сохранено: **{summary}**.", ephemeral=True)


async def ensure_interview_prompt(bot, guild: discord.Guild, application_id: int) -> None:
    application = bot.db.get_application(application_id)
    if not application or application["interview_message_id"] or application["status"] not in PENDING_INTERVIEW_STATUSES:
        return

    channel = await fetch_guild_channel(guild, application["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    reviewer_mention = f"<@{claimed_reviewer_id(application)}>" if claimed_reviewer_id(application) else None
    view = InterviewResultView(bot, application_id, claimed=bool(claimed_reviewer_id(application)))
    message = await channel.send(embed=build_interview_prompt_embed(reviewer_mention), view=view)
    bot.db.update_application(application_id, interview_message_id=message.id)
    bot.add_view(view)
