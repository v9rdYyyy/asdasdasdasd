from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import discord


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_channel_name(user: discord.abc.User) -> str:
    digits = str(user.id)[-6:]
    base = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "-", user.display_name.lower()).strip("-")
    if not base:
        base = "anketa"
    return f"anketa-{base[:24]}-{digits}"[:95]


def mention_channel(channel_id: Optional[int]) -> str:
    return f"<#{channel_id}>" if channel_id else "не задан"


def mention_role(role_id: Optional[int]) -> str:
    return f"<@&{role_id}>" if role_id else "не задана"


def human_recruitment_status(is_open: bool) -> str:
    return "🟢 Приём заявок открыт" if is_open else "🔴 Приём заявок закрыт"


def truncate_field(text: str, limit: int = 1024) -> str:
    clean = text.strip() or "—"
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."
