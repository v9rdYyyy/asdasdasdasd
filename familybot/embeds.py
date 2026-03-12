from __future__ import annotations

import sqlite3

import discord

from familybot.constants import (
    COLOR_DANGER,
    COLOR_INFO,
    COLOR_PANEL,
    COLOR_SUCCESS,
    COLOR_WARNING,
    PANEL_FOOTER,
)
from familybot.utils import human_recruitment_status, mention_channel, mention_role, truncate_field, utcnow_dt


ANSWER_FIELDS: list[tuple[str, str]] = [
    ("identity", "1. Ник / Имя ИРЛ / Возраст ИРЛ"),
    ("rp_experience", "2. Ваш опыт на RP проекте"),
    ("gta_hours_family_experience", "3. Количество часов в GTA / опыт в семьях"),
    ("gunfight_rollback", "4. Откат с ГГшки"),
    ("family_time_online_tz", "5. Время семье / Средний онлайн / Часовой пояс"),
]


def build_panel_embed(guild: discord.Guild, config: sqlite3.Row) -> discord.Embed:
    embed = discord.Embed(
        title="👋 Путь в семью начинается здесь!",
        description=(
            "Уведомление о приглашении на обзвон обычно отправляется в личные сообщения. "
            f"Если ЛС закрыты, оно отправляется в канал — {mention_channel(config['result_channel_id'])}. "
            "В этот канал также приходят уведомления об отказе в наборе.\n\n"
            "Обычно заявки обрабатываются в течение **2-3 часов** — всё зависит от того, "
            "насколько загружены наши рекрутеры на данный момент.\n\n"
            "Подать заявку можно только при открытом наборе. Если не выходит — набор закрыт.\n"
            "Понять, открыт ли набор, можно по сообщению ниже."
        ),
        color=COLOR_PANEL,
        timestamp=utcnow_dt(),
    )
    embed.add_field(name="Статус набора", value=human_recruitment_status(bool(config["recruitment_open"])), inline=False)
    embed.add_field(
        name="Дальше всё просто",
        value=(
            "1. Нажимаете кнопку ниже.\n"
            "2. Открываете анкету.\n"
            "3. Заполняете все поля.\n"
            "4. Ждёте решение рекрутера."
        ),
        inline=False,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=PANEL_FOOTER)
    return embed


def build_application_embed(
    guild: discord.Guild,
    applicant: discord.abc.User,
    application_id: int,
    answers: dict[str, str],
    reviewer_mention: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📨 Заявка #{application_id}",
        description=f"**Пользователь:** {applicant.mention}\n**ID:** `{applicant.id}`",
        color=COLOR_INFO,
        timestamp=utcnow_dt(),
    )
    for key, label in ANSWER_FIELDS:
        embed.add_field(name=label, value=truncate_field(answers.get(key, "—")), inline=False)
    review_text = (
        f"На рассмотрении у {reviewer_mention}"
        if reviewer_mention
        else "Ожидает, пока рекрут нажмёт «Взять на рассмотрение»."
    )
    embed.add_field(name="Статус рассмотрения", value=review_text, inline=False)
    if guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)
    embed.set_footer(text="Ниже выберите решение по заявке")
    return embed


def build_interview_prompt_embed(reviewer_mention: str | None = None) -> discord.Embed:
    description = "Ниже выберите итог разговора."
    if reviewer_mention:
        description = f"Заявку продолжает вести {reviewer_mention}.\n\n{description}"
    return discord.Embed(
        title="🎤 Как прошёл обзвон?",
        description=description,
        color=COLOR_WARNING,
        timestamp=utcnow_dt(),
    )


def build_result_embed(title: str, description: str, guild: discord.Guild, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=utcnow_dt())
    if guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)
    embed.set_footer(text=PANEL_FOOTER)
    return embed


def build_setup_embed(config: sqlite3.Row) -> discord.Embed:
    return discord.Embed(
        title="⚙️ Настройки сохранены",
        description=(
            f"**Итог заявок:** {mention_channel(config['result_channel_id'])}\n"
            f"**Проверка:** {mention_channel(config['voice_channel_id'])}\n"
            f"**Роль рекрутов:** {mention_role(config['review_role_id'])}\n"
            f"**Категория заявок:** {mention_channel(config['applications_category_id'])}\n"
            f"**Архивная категория:** {mention_channel(config['archive_category_id'])}\n"
            f"**Название сервера:** {config['server_name']}\n"
            f"**Статус набора:** {human_recruitment_status(bool(config['recruitment_open']))}"
        ),
        color=COLOR_SUCCESS,
        timestamp=utcnow_dt(),
    )


def build_rejection_text(applicant_mention: str, reviewer_mention: str, reason: str) -> str:
    return (
        f"Заявка от пользователя {applicant_mention}\n\n"
        "На вступление в семью была отклонена. 😢\n\n"
        f"Причина: {reason}\n"
        f"Рассматривал заявку: {reviewer_mention}"
    )


def build_reserve_text(applicant_mention: str, reviewer_mention: str) -> str:
    return (
        f"Заявка от пользователя {applicant_mention}\n\n"
        "На вступление в семью была отклонена. 😢\n\n"
        "Причина: На данный момент свободных слотов в семье нету. Вы будете добавлены в базу резерва "
        "и как только будут освобождаться слоты в семье, рекрут вас пригласит на обзвон.\n"
        f"Рассматривал заявку: {reviewer_mention}"
    )


def build_approval_text(applicant_mention: str, reviewer_mention: str, voice_mention: str) -> str:
    return (
        f"Пользователь {applicant_mention} был приглашён на обзвон.\n"
        f"Голосовой канал: {voice_mention}\n"
        f"Рассматривал заявку: {reviewer_mention}"
    )


def build_interview_success_text(applicant_mention: str, reviewer_mention: str) -> str:
    return (
        f"Пользователь {applicant_mention} успешно прошёл обзвон.\n"
        f"Рассматривал заявку: {reviewer_mention}"
    )


RESULT_COLORS = {
    "approve": COLOR_SUCCESS,
    "reserve": COLOR_WARNING,
    "reject": COLOR_DANGER,
    "interview_success": COLOR_SUCCESS,
}
