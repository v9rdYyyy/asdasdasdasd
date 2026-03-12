from __future__ import annotations

import sqlite3
from typing import Optional

import discord

from familybot.constants import (
    APP_STATUS_ACCEPTED,
    APP_STATUS_APPROVED_PENDING,
    APP_STATUS_INTERVIEW_FAILED,
    APP_STATUS_REJECTED,
    APP_STATUS_RESERVE_PENDING,
    APP_STATUS_SUBMITTED,
)
from familybot.embeds import (
    build_approval_text,
    build_interview_success_text,
    build_rejection_text,
    build_reserve_text,
    build_result_embed,
    RESULT_COLORS,
)
from familybot.utils import mention_channel, sanitize_channel_name


async def fetch_guild_channel(guild: discord.Guild, channel_id: Optional[int]) -> Optional[discord.abc.GuildChannel]:
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel:
        return channel
    try:
        return await guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def fetch_user(bot: discord.Client, user_id: int) -> Optional[discord.User]:
    cached = bot.get_user(user_id)
    if cached:
        return cached
    try:
        return await bot.fetch_user(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def safe_dm(user: discord.User, content: str) -> bool:
    try:
        await user.send(content)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def config_is_ready(config: Optional[sqlite3.Row]) -> bool:
    if not config:
        return False
    return bool(config["result_channel_id"] and config["voice_channel_id"] and config["review_role_id"])


def member_can_review(member: discord.Member, review_role_id: Optional[int]) -> bool:
    if member.guild_permissions.administrator:
        return True
    return bool(review_role_id and any(role.id == review_role_id for role in member.roles))


async def create_application_channel(
    guild: discord.Guild,
    applicant: discord.Member,
    review_role_id: int,
    applications_category_id: Optional[int],
) -> discord.TextChannel:
    review_role = guild.get_role(review_role_id)
    if review_role is None:
        raise RuntimeError("Роль рекрутов не найдена")

    category = guild.get_channel(applications_category_id) if applications_category_id else None
    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None

    bot_member = guild.me or guild.get_member(guild._state.self_id)
    if bot_member is None:
        raise RuntimeError("Бот не найден в участниках сервера")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        review_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
            add_reactions=True,
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            manage_channels=True,
        ),
    }

    return await guild.create_text_channel(
        name=sanitize_channel_name(applicant),
        category=category,
        overwrites=overwrites,
        topic=f"Заявка пользователя {applicant} ({applicant.id})",
        reason="Новая заявка в семью",
    )


async def send_primary_log(
    guild: discord.Guild,
    config: sqlite3.Row,
    application: sqlite3.Row,
    reviewer: discord.Member,
    target_status: str,
) -> None:
    result_channel = await fetch_guild_channel(guild, config["result_channel_id"])
    if not isinstance(result_channel, discord.TextChannel):
        return

    applicant_mention = f"<@{application['user_id']}>"
    if target_status == APP_STATUS_APPROVED_PENDING:
        text = build_approval_text(applicant_mention, reviewer.mention, mention_channel(config["voice_channel_id"]))
        embed = build_result_embed("✅ Заявка одобрена", text, guild, RESULT_COLORS["approve"])
    else:
        text = build_reserve_text(applicant_mention, reviewer.mention)
        embed = build_result_embed("🟡 Заявка отправлена в резерв", text, guild, RESULT_COLORS["reserve"])

    await result_channel.send(embed=embed)


async def send_rejection_notifications(
    bot: discord.Client,
    guild: discord.Guild,
    config: sqlite3.Row,
    application: sqlite3.Row,
    reviewer: discord.Member,
    reason: str,
) -> None:
    result_channel = await fetch_guild_channel(guild, config["result_channel_id"])
    applicant_mention = f"<@{application['user_id']}>"
    text = build_rejection_text(applicant_mention, reviewer.mention, reason)

    if isinstance(result_channel, discord.TextChannel):
        embed = build_result_embed("❌ Заявка отклонена", text, guild, RESULT_COLORS["reject"])
        await result_channel.send(embed=embed)

    user = await fetch_user(bot, application["user_id"])
    if user:
        await safe_dm(user, text)


async def send_interview_success_log(
    guild: discord.Guild,
    config: sqlite3.Row,
    application: sqlite3.Row,
    reviewer: discord.Member,
) -> None:
    result_channel = await fetch_guild_channel(guild, config["result_channel_id"])
    if not isinstance(result_channel, discord.TextChannel):
        return

    text = build_interview_success_text(f"<@{application['user_id']}>", reviewer.mention)
    embed = build_result_embed("🎉 Обзвон пройден", text, guild, RESULT_COLORS["interview_success"])
    await result_channel.send(embed=embed)


async def archive_application_channel(
    guild: discord.Guild,
    config: sqlite3.Row,
    application: sqlite3.Row,
) -> None:
    channel = await fetch_guild_channel(guild, application["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    review_role = guild.get_role(config["review_role_id"])
    overwrites = channel.overwrites
    if review_role:
        current = overwrites.get(review_role, discord.PermissionOverwrite())
        current.view_channel = True
        current.read_message_history = True
        current.send_messages = False
        current.attach_files = False
        current.add_reactions = False
        overwrites[review_role] = current

    archive_category = guild.get_channel(config["archive_category_id"]) if config["archive_category_id"] else None
    if archive_category is not None and not isinstance(archive_category, discord.CategoryChannel):
        archive_category = None

    new_name = channel.name if channel.name.startswith("archive-") else f"archive-{channel.name}"
    await channel.edit(
        name=new_name[:95],
        category=archive_category or channel.category,
        overwrites=overwrites,
        reason="Заявка обработана и скрыта в архив",
    )


def decision_requires_submitted(application: sqlite3.Row) -> bool:
    return application["status"] == APP_STATUS_SUBMITTED


def interview_pending(application: sqlite3.Row) -> bool:
    return application["status"] in (APP_STATUS_APPROVED_PENDING, APP_STATUS_RESERVE_PENDING)


def final_status_for_reject_mode(mode: str) -> str:
    return APP_STATUS_REJECTED if mode == "initial_reject" else APP_STATUS_INTERVIEW_FAILED


def final_status_for_success() -> str:
    return APP_STATUS_ACCEPTED
