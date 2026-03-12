
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands



FALLBACK_TOKEN = ""

BOT_OWNER_IDS: set[int] = {
    504936984326832128,  
}

DEFAULT_RESULTS_CHANNEL_ID = 1481031147717918863
DEFAULT_INTERVIEW_VOICE_CHANNEL_ID = 1444268474686902338
DEFAULT_REVIEW_ROLE_ID = 1444268473592053868
DEFAULT_APPLICATIONS_CATEGORY_ID = 1480994826546581525
DEFAULT_ARCHIVE_CATEGORY_ID = 1481057104025485323
DEFAULT_SERVER_NAME = "A T L E T I C O"

COMMAND_GUILD_ID = 1444268473256513569

DB_PATH = "family_bot.sqlite3"
LOG_LEVEL = logging.INFO
ACTIVE_CATEGORY_NAME = "Заявки"
ARCHIVE_CATEGORY_NAME = "Архив заявок"
PANEL_TITLE = "💍 Заявки в семью"
APPLICATION_COOLDOWN_DAYS = 7


# ============================================================
# БАЗОВАЯ ЛОГИКА
# ============================================================
TOKEN = (os.getenv("TOKEN") or os.getenv("DISCORD_TOKEN") or FALLBACK_TOKEN or "").replace("Bot ", "").strip()
if not TOKEN:
    raise RuntimeError(
        "Токен не найден. Вставь токен в FALLBACK_TOKEN внутри bot.py или задай TOKEN / DISCORD_TOKEN на хостинге."
    )

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("family-bot-inline")

intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


STATUS_SUBMITTED = "submitted"
STATUS_APPROVED_PENDING = "approved_pending_interview"
STATUS_RESERVE_PENDING = "reserve_pending_interview"
STATUS_REJECTED = "rejected"
STATUS_INTERVIEW_FAILED = "interview_failed"
STATUS_ACCEPTED = "accepted_final"

FINAL_STATUSES = {
    STATUS_REJECTED,
    STATUS_INTERVIEW_FAILED,
    STATUS_ACCEPTED,
}

_REASON_UNSET = object()

COLOR_PANEL = discord.Color.from_rgb(155, 26, 39)
COLOR_INFO = discord.Color.blurple()
COLOR_SUCCESS = discord.Color.green()
COLOR_WARNING = discord.Color.gold()
COLOR_DANGER = discord.Color.red()


@dataclass
class GuildConfig:
    guild_id: int
    result_channel_id: int = 0
    voice_channel_id: int = 0
    review_role_id: int = 0
    recruiter_ping_user_id: int = 0
    applications_category_id: int = 0
    archive_category_id: int = 0
    server_name: str = DEFAULT_SERVER_NAME
    recruitment_open: int = 1
    cooldown_enabled: int = 1
    panel_channel_id: int = 0
    panel_message_id: int = 0
    panel_image_url: str = ""
    panel_media_url: str = ""
    panel_media_kind: str = ""
    panel_media_filename: str = ""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    result_channel_id INTEGER DEFAULT 0,
                    voice_channel_id INTEGER DEFAULT 0,
                    review_role_id INTEGER DEFAULT 0,
                    recruiter_ping_user_id INTEGER DEFAULT 0,
                    applications_category_id INTEGER DEFAULT 0,
                    archive_category_id INTEGER DEFAULT 0,
                    server_name TEXT DEFAULT 'Ваш сервер',
                    recruitment_open INTEGER DEFAULT 1,
                    cooldown_enabled INTEGER DEFAULT 1,
                    panel_channel_id INTEGER DEFAULT 0,
                    panel_message_id INTEGER DEFAULT 0,
                    panel_image_url TEXT DEFAULT '',
                    panel_media_url TEXT DEFAULT '',
                    panel_media_kind TEXT DEFAULT '',
                    panel_media_filename TEXT DEFAULT '',
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL UNIQUE,
                    review_message_id INTEGER DEFAULT 0,
                    interview_message_id INTEGER DEFAULT 0,
                    reviewer_id INTEGER DEFAULT 0,
                    status TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    reason TEXT,
                    archive_seq INTEGER DEFAULT 0,
                    archived_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "guild_settings", "cooldown_enabled", "INTEGER DEFAULT 1")
            self._ensure_column(conn, "guild_settings", "recruiter_ping_user_id", "INTEGER DEFAULT 0")
            self._ensure_column(conn, "guild_settings", "panel_image_url", "TEXT DEFAULT ''")
            self._ensure_column(conn, "guild_settings", "panel_media_url", "TEXT DEFAULT ''")
            self._ensure_column(conn, "guild_settings", "panel_media_kind", "TEXT DEFAULT ''")
            self._ensure_column(conn, "guild_settings", "panel_media_filename", "TEXT DEFAULT ''")
            self._ensure_column(conn, "applications", "review_message_id", "INTEGER DEFAULT 0")
            self._ensure_column(conn, "applications", "interview_message_id", "INTEGER DEFAULT 0")
            self._ensure_column(conn, "applications", "archive_seq", "INTEGER DEFAULT 0")
            self._ensure_column(conn, "applications", "archived_at", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_channel ON applications(channel_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_applications_guild_user_status ON applications(guild_id, user_id, status, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_applications_guild_user_created ON applications(guild_id, user_id, created_at DESC, id DESC)"
            )

    def get_config(self, guild_id: int) -> GuildConfig:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()

        default = GuildConfig(
            guild_id=guild_id,
            result_channel_id=DEFAULT_RESULTS_CHANNEL_ID,
            voice_channel_id=DEFAULT_INTERVIEW_VOICE_CHANNEL_ID,
            review_role_id=DEFAULT_REVIEW_ROLE_ID,
            recruiter_ping_user_id=0,
            applications_category_id=DEFAULT_APPLICATIONS_CATEGORY_ID,
            archive_category_id=DEFAULT_ARCHIVE_CATEGORY_ID,
            server_name=DEFAULT_SERVER_NAME,
            recruitment_open=1,
            cooldown_enabled=1,
            panel_image_url="",
            panel_media_url="",
            panel_media_kind="",
            panel_media_filename="",
        )

        if not row:
            return default

        panel_media_url = row["panel_media_url"] if "panel_media_url" in row.keys() and row["panel_media_url"] else (row["panel_image_url"] or "")
        panel_media_kind = row["panel_media_kind"] if "panel_media_kind" in row.keys() and row["panel_media_kind"] else ("image" if panel_media_url else "")
        panel_media_filename = row["panel_media_filename"] if "panel_media_filename" in row.keys() and row["panel_media_filename"] else infer_filename_from_url(panel_media_url)

        return GuildConfig(
            guild_id=row["guild_id"],
            result_channel_id=row["result_channel_id"] or default.result_channel_id,
            voice_channel_id=row["voice_channel_id"] or default.voice_channel_id,
            review_role_id=row["review_role_id"] or default.review_role_id,
            recruiter_ping_user_id=row["recruiter_ping_user_id"] if "recruiter_ping_user_id" in row.keys() and row["recruiter_ping_user_id"] is not None else 0,
            applications_category_id=row["applications_category_id"] or default.applications_category_id,
            archive_category_id=row["archive_category_id"] or default.archive_category_id,
            server_name=row["server_name"] or default.server_name,
            recruitment_open=row["recruitment_open"] if row["recruitment_open"] is not None else 1,
            cooldown_enabled=row["cooldown_enabled"] if row["cooldown_enabled"] is not None else 1,
            panel_channel_id=row["panel_channel_id"] or 0,
            panel_message_id=row["panel_message_id"] or 0,
            panel_image_url=row["panel_image_url"] or panel_media_url or "",
            panel_media_url=panel_media_url,
            panel_media_kind=panel_media_kind,
            panel_media_filename=panel_media_filename,
        )

    def upsert_config(self, guild_id: int, **kwargs) -> GuildConfig:
        current = self.get_config(guild_id)
        payload = {
            "result_channel_id": current.result_channel_id,
            "voice_channel_id": current.voice_channel_id,
            "review_role_id": current.review_role_id,
            "recruiter_ping_user_id": current.recruiter_ping_user_id,
            "applications_category_id": current.applications_category_id,
            "archive_category_id": current.archive_category_id,
            "server_name": current.server_name,
            "recruitment_open": current.recruitment_open,
            "cooldown_enabled": current.cooldown_enabled,
            "panel_channel_id": current.panel_channel_id,
            "panel_message_id": current.panel_message_id,
            "panel_image_url": current.panel_image_url,
            "panel_media_url": current.panel_media_url,
            "panel_media_kind": current.panel_media_kind,
            "panel_media_filename": current.panel_media_filename,
        }
        payload.update(kwargs)

        if "panel_image_url" in kwargs and "panel_media_url" not in kwargs:
            payload["panel_media_url"] = kwargs["panel_image_url"] or ""
            payload["panel_media_kind"] = detect_panel_media_kind(kwargs["panel_image_url"] or "")
            payload["panel_media_filename"] = infer_filename_from_url(kwargs["panel_image_url"] or "")

        if not payload["panel_media_url"]:
            payload["panel_media_kind"] = ""
            payload["panel_media_filename"] = ""
            payload["panel_image_url"] = ""
        elif not payload["panel_image_url"] and payload["panel_media_kind"] == "image":
            payload["panel_image_url"] = payload["panel_media_url"]

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO guild_settings (
                    guild_id,
                    result_channel_id,
                    voice_channel_id,
                    review_role_id,
                    recruiter_ping_user_id,
                    applications_category_id,
                    archive_category_id,
                    server_name,
                    recruitment_open,
                    cooldown_enabled,
                    panel_channel_id,
                    panel_message_id,
                    panel_image_url,
                    panel_media_url,
                    panel_media_kind,
                    panel_media_filename,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    result_channel_id = excluded.result_channel_id,
                    voice_channel_id = excluded.voice_channel_id,
                    review_role_id = excluded.review_role_id,
                    recruiter_ping_user_id = excluded.recruiter_ping_user_id,
                    applications_category_id = excluded.applications_category_id,
                    archive_category_id = excluded.archive_category_id,
                    server_name = excluded.server_name,
                    recruitment_open = excluded.recruitment_open,
                    cooldown_enabled = excluded.cooldown_enabled,
                    panel_channel_id = excluded.panel_channel_id,
                    panel_message_id = excluded.panel_message_id,
                    panel_image_url = excluded.panel_image_url,
                    panel_media_url = excluded.panel_media_url,
                    panel_media_kind = excluded.panel_media_kind,
                    panel_media_filename = excluded.panel_media_filename,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id,
                    payload["result_channel_id"],
                    payload["voice_channel_id"],
                    payload["review_role_id"],
                    payload["recruiter_ping_user_id"],
                    payload["applications_category_id"],
                    payload["archive_category_id"],
                    payload["server_name"],
                    payload["recruitment_open"],
                    payload["cooldown_enabled"],
                    payload["panel_channel_id"],
                    payload["panel_message_id"],
                    payload["panel_image_url"],
                    payload["panel_media_url"],
                    payload["panel_media_kind"],
                    payload["panel_media_filename"],
                    utcnow(),
                ),
            )
        return self.get_config(guild_id)

    def create_application(self, guild_id: int, user_id: int, channel_id: int, answers: dict[str, str]) -> int:
        now = utcnow()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO applications (
                    guild_id, user_id, channel_id, review_message_id, interview_message_id,
                    reviewer_id, status, answers_json, reason, archive_seq, archived_at, created_at, updated_at
                ) VALUES (?, ?, ?, 0, 0, 0, ?, ?, NULL, 0, NULL, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    channel_id,
                    STATUS_SUBMITTED,
                    json.dumps(answers, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def get_application_by_channel(self, channel_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM applications WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()

    def get_open_application_by_user(self, guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT * FROM applications
                WHERE guild_id = ? AND user_id = ?
                AND status IN (?, ?, ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    guild_id,
                    user_id,
                    STATUS_SUBMITTED,
                    STATUS_APPROVED_PENDING,
                    STATUS_RESERVE_PENDING,
                ),
            ).fetchone()

    def get_latest_application_by_user(self, guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT * FROM applications
                WHERE guild_id = ? AND user_id = ?
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT 1
                """,
                (guild_id, user_id),
            ).fetchone()

    def next_archive_seq(self, guild_id: int, user_id: int) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(archive_seq), 0) + 1 AS next_seq
                FROM applications
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            return int(row["next_seq"] or 1)

    def get_archived_applications_by_user(self, guild_id: int, user_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT * FROM applications
                WHERE guild_id = ? AND user_id = ? AND archive_seq > 0
                ORDER BY archive_seq DESC, id DESC
                LIMIT ?
                """,
                (guild_id, user_id, limit),
            ).fetchall()

    def claim_application(self, application_id: int, reviewer_id: int) -> bool:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE applications
                SET reviewer_id = ?, updated_at = ?
                WHERE id = ? AND status = ? AND COALESCE(reviewer_id, 0) = 0
                """,
                (reviewer_id, utcnow(), application_id, STATUS_SUBMITTED),
            )
            return cursor.rowcount > 0

    def transition_application(
        self,
        application_id: int,
        from_statuses: Iterable[str],
        to_status: str,
        reviewer_id: int,
        *,
        require_reviewer_id: Optional[int] = None,
        reason: Any = _REASON_UNSET,
    ) -> bool:
        statuses = tuple(from_statuses)
        if not statuses:
            return False

        set_parts = ["status = ?", "reviewer_id = ?", "updated_at = ?"]
        params: list[Any] = [to_status, reviewer_id, utcnow()]
        if reason is not _REASON_UNSET:
            set_parts.append("reason = ?")
            params.append(reason)

        placeholders = ", ".join("?" for _ in statuses)
        sql = (
            f"UPDATE applications SET {', '.join(set_parts)} "
            f"WHERE id = ? AND status IN ({placeholders})"
        )
        params.append(application_id)
        params.extend(statuses)
        if require_reviewer_id is not None:
            sql += " AND COALESCE(reviewer_id, 0) = ?"
            params.append(require_reviewer_id)

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount > 0

    def update_application(self, application_id: int, **kwargs) -> None:
        if not kwargs:
            return
        kwargs["updated_at"] = utcnow()
        columns = ", ".join(f"{key} = ?" for key in kwargs.keys())
        values = list(kwargs.values())
        values.append(application_id)

        with closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE applications SET {columns} WHERE id = ?", values)


db = Database(DB_PATH)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_retry_time(value: datetime) -> str:
    unix_ts = int(value.timestamp())
    return f"<t:{unix_ts}:F> • <t:{unix_ts}:R>"


def is_bot_admin(member: discord.abc.User | discord.Member) -> bool:
    if member.id in BOT_OWNER_IDS:
        return True
    if isinstance(member, discord.Member):
        return member.guild_permissions.administrator
    return False


def has_reviewer_access(member: discord.Member, config: GuildConfig) -> bool:
    if is_bot_admin(member):
        return True
    if config.review_role_id and discord.utils.get(member.roles, id=config.review_role_id):
        return True
    return False


def clean_channel_name(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        return "user"
    return text[:40]


def build_archive_channel_name(user_id: int, archive_seq: int) -> str:
    return f"archive-{user_id}-{archive_seq:03d}"[:95]


def infer_filename_from_url(url: str) -> str:
    if not url:
        return "panel_media"
    candidate = url.split("?")[0].rsplit("/", 1)[-1].strip()
    return candidate or "panel_media"


def detect_panel_media_kind(url: str) -> str:
    value = (url or "").lower().split("?", 1)[0]
    if any(value.endswith(ext) for ext in (".mp4", ".mov", ".webm", ".m4v")):
        return "video"
    if any(value.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
        return "image"
    return "image" if url else ""


def detect_attachment_media_kind(attachment: discord.Attachment) -> str:
    content_type = (attachment.content_type or "").lower()
    filename = (attachment.filename or "").lower()
    if content_type.startswith("video/") or filename.endswith((".mp4", ".mov", ".webm", ".m4v")):
        return "video"
    if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
        return "image"
    return ""


def panel_media_summary(config: GuildConfig) -> str:
    media_url = config.panel_media_url or config.panel_image_url
    if not media_url:
        return "Не установлено"
    kind_label = "Видео" if config.panel_media_kind == "video" else "Изображение / GIF"
    return f"{kind_label}: {media_url}"


async def download_media_file(url: str, filename: str) -> Optional[discord.File]:
    if not url:
        return None

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.read()
    except Exception:
        logger.exception("Не удалось скачать медиа для панели: %s", url)
        return None

    return discord.File(io.BytesIO(data), filename=filename or infer_filename_from_url(url))


async def build_panel_message_kwargs(guild: discord.Guild, config: GuildConfig, *, for_edit: bool = False) -> dict:
    embed = build_panel_embed(guild, config)
    payload: dict = {"embed": embed, "view": PanelView()}

    if config.panel_media_kind == "video" and config.panel_media_url:
        media_file = await download_media_file(config.panel_media_url, config.panel_media_filename)
        if media_file:
            if for_edit:
                payload["attachments"] = [media_file]
            else:
                payload["file"] = media_file
        elif for_edit:
            payload["attachments"] = []
    elif for_edit:
        payload["attachments"] = []

    return payload


def apply_common_embed_style(embed: discord.Embed, guild: discord.Guild, config: GuildConfig) -> discord.Embed:
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    media_url = config.panel_media_url or config.panel_image_url
    media_kind = config.panel_media_kind or detect_panel_media_kind(media_url)
    if media_url and media_kind == "image":
        embed.set_image(url=media_url)

    embed.set_footer(text=f"{guild.name} • family recruitment")
    return embed


def build_panel_embed(guild: discord.Guild, config: GuildConfig) -> discord.Embed:
    result_channel = guild.get_channel(config.result_channel_id) if config.result_channel_id else None
    result_text = result_channel.mention if isinstance(result_channel, discord.abc.GuildChannel) else "не настроен"
    status_text = "🟢 Приём заявок открыт" if config.recruitment_open else "🔴 Набор сейчас закрыт"

    embed = discord.Embed(
        title=PANEL_TITLE,
        description=(
            "**Добро пожаловать!**\n"
            "Ниже можно открыть форму и подать заявку на вступление.\n\n"
            "**Что важно знать:**\n"
            "• Решение по заявке обычно приходит в течение **2–3 часов**.\n"
            f"• Приглашение на обзвон отправляется в ЛС, а если они закрыты — в {result_text}.\n"
            "• Повторная заявка доступна только после завершения кулдауна.\n"
            "• Если кнопка не даёт подать анкету — значит набор сейчас закрыт или не истёк кулдаун."
        ),
        color=COLOR_PANEL,
    )
    embed.add_field(name="Статус набора", value=status_text, inline=False)
    embed.add_field(name="Куда придёт ответ", value=result_text, inline=False)
    return apply_common_embed_style(embed, guild, config)


def build_panel_popup_embed(guild: discord.Guild, config: GuildConfig) -> discord.Embed:
    status = "🟢 Набор открыт" if config.recruitment_open else "🔴 Набор закрыт"
    cooldown_text = (
        f"Повторная заявка: не чаще 1 раза в {APPLICATION_COOLDOWN_DAYS} дн."
        if config.cooldown_enabled
        else "Повторная заявка: без ожидания"
    )
    embed = discord.Embed(
        title="📩 Подача заявки",
        description=(
            f"{status}\n"
            f"{cooldown_text}\n\n"
            "Нажми кнопку ниже, чтобы открыть анкету."
        ),
        color=COLOR_INFO,
    )
    return apply_common_embed_style(embed, guild, config)


def build_application_embed(
    applicant: discord.User | discord.Member,
    answers: dict[str, str],
    application_id: int,
    reviewer_mention: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="🆕 Новая заявка в семью",
        description=(
            f"**Пользователь:** {applicant.mention}\n"
            f"**ID заявки:** `{application_id}`\n"
            f"**Discord ID:** `{applicant.id}`"
        ),
        color=COLOR_INFO,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="1. Ник / Имя ИРЛ / Возраст ИРЛ", value=answers["identity"], inline=False)
    embed.add_field(name="2. Опыт на RP проекте", value=answers["experience"], inline=False)
    embed.add_field(name="3. Часы в GTA / Опыт в семьях", value=answers["hours"], inline=False)
    embed.add_field(name="4. Откат с ГГшки", value=answers["loadout"], inline=False)
    embed.add_field(name="5. Время / Онлайн / Часовой пояс", value=answers["online"], inline=False)
    review_text = (
        f"На рассмотрении у {reviewer_mention}"
        if reviewer_mention
        else "Ожидает, пока рекрут нажмёт «Взять на рассмотрение»."
    )
    embed.add_field(name="Статус рассмотрения", value=review_text, inline=False)
    embed.set_thumbnail(url=applicant.display_avatar.url)
    embed.set_footer(text=f"Заявку подал: {applicant} • {applicant.id}")
    return embed


def build_results_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))


def recruiter_ping_summary(guild: discord.Guild, config: GuildConfig) -> str:
    if not config.review_role_id:
        return "Не выбрана"
    role = guild.get_role(config.review_role_id)
    if role:
        return f"{role.mention}\n`{role.id}`"
    return f"Роль удалена или недоступна\n`{config.review_role_id}`"


async def get_or_create_category(guild: discord.Guild, category_id: int, fallback_name: str) -> discord.CategoryChannel:
    if category_id:
        category = guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            return category

    category = discord.utils.get(guild.categories, name=fallback_name)
    if category:
        return category

    return await guild.create_category(fallback_name, reason="Создано ботом для заявок")


async def ensure_defaults_saved(guild: discord.Guild) -> GuildConfig:
    current = db.get_config(guild.id)
    return db.upsert_config(
        guild.id,
        result_channel_id=current.result_channel_id,
        voice_channel_id=current.voice_channel_id,
        review_role_id=current.review_role_id,
        recruiter_ping_user_id=current.recruiter_ping_user_id,
        applications_category_id=current.applications_category_id,
        archive_category_id=current.archive_category_id,
        server_name=current.server_name,
        recruitment_open=current.recruitment_open,
        cooldown_enabled=current.cooldown_enabled,
        panel_channel_id=current.panel_channel_id,
        panel_message_id=current.panel_message_id,
        panel_image_url=current.panel_image_url,
        panel_media_url=current.panel_media_url,
        panel_media_kind=current.panel_media_kind,
        panel_media_filename=current.panel_media_filename,
    )


async def send_dm_safely(user: discord.User | discord.Member, content: Optional[str] = None, embed: Optional[discord.Embed] = None) -> bool:
    try:
        await user.send(content=content, embed=embed)
        return True
    except Exception:
        return False


async def send_results_message(guild: discord.Guild, config: GuildConfig, embed: discord.Embed) -> None:
    if not config.result_channel_id:
        return
    channel = guild.get_channel(config.result_channel_id)
    if isinstance(channel, discord.TextChannel):
        await channel.send(embed=embed)


async def clear_channel_history(channel: discord.TextChannel) -> None:
    messages = [message async for message in channel.history(limit=None, oldest_first=False)]
    for message in messages:
        try:
            await message.delete()
        except Exception:
            logger.exception("Не удалось удалить сообщение %s в канале %s", message.id, channel.id)


async def refresh_panel_message(guild: discord.Guild) -> None:
    config = db.get_config(guild.id)
    if not config.panel_channel_id or not config.panel_message_id:
        return

    panel_channel = guild.get_channel(config.panel_channel_id)
    if not isinstance(panel_channel, discord.TextChannel):
        return

    try:
        panel_message = await panel_channel.fetch_message(config.panel_message_id)
        kwargs = await build_panel_message_kwargs(guild, config, for_edit=True)
        await panel_message.edit(**kwargs)
    except Exception:
        logger.exception("Не удалось обновить панель набора в guild %s", guild.id)


async def archive_application_channel(
    channel: discord.TextChannel,
    config: GuildConfig,
    application: sqlite3.Row,
    reviewer: discord.Member,
    final_text: str,
) -> int:
    archive_seq = db.next_archive_seq(application["guild_id"], application["user_id"])
    archive_category = await get_or_create_category(channel.guild, config.archive_category_id, ARCHIVE_CATEGORY_NAME)
    archive_name = build_archive_channel_name(application["user_id"], archive_seq)

    db.update_application(
        application["id"],
        reviewer_id=reviewer.id,
        archive_seq=archive_seq,
        archived_at=utcnow(),
    )

    await channel.edit(
        category=archive_category,
        name=archive_name,
        topic=(
            f"Архив заявки пользователя {application['user_id']} | №{archive_seq:03d} | Рассматривал {reviewer.id}"
        )[:1024],
        reason="Заявка рассмотрена и отправлена в архив",
    )

    await clear_channel_history(channel)
    await channel.send(
        f"Канал архивирован.\n"
        f"Заявка завершена. Рассматривал: {reviewer.mention}\n"
        f"{final_text}"
    )
    return archive_seq


class RejectReasonModal(discord.ui.Modal):
    def __init__(self, interview_stage: bool):
        title = "Причина отказа после обзвона" if interview_stage else "Причина отказа"
        super().__init__(title=title)
        placeholder = "читер!!!" if interview_stage else "Стрельба / мувмент / слабый уровень"
        self.interview_stage = interview_stage
        self.reason = discord.ui.TextInput(
            label="Укажите причину отказа",
            placeholder=placeholder,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=400,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        config = db.get_config(interaction.guild.id)
        if not has_reviewer_access(interaction.user, config):
            await interaction.response.send_message("У тебя нет доступа к рассмотрению заявок.", ephemeral=True)
            return

        application = db.get_application_by_channel(interaction.channel.id)
        if not application:
            await interaction.response.send_message("Не удалось найти заявку для этого канала.", ephemeral=True)
            return

        reviewer_id = claimed_reviewer_id(application)
        if not reviewer_id:
            await interaction.response.send_message("Сначала нажми «Взять на рассмотрение».", ephemeral=True)
            return
        if reviewer_id != interaction.user.id:
            await interaction.response.send_message(
                f"Эту заявку рассматривает <@{reviewer_id}>.",
                ephemeral=True,
            )
            return

        valid_statuses = (STATUS_APPROVED_PENDING, STATUS_RESERVE_PENDING) if self.interview_stage else (STATUS_SUBMITTED,)
        if application["status"] not in valid_statuses:
            await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        reason = str(self.reason).strip()
        rejected_status = STATUS_INTERVIEW_FAILED if self.interview_stage else STATUS_REJECTED
        updated = db.transition_application(
            application["id"],
            valid_statuses,
            rejected_status,
            interaction.user.id,
            require_reviewer_id=interaction.user.id,
            reason=reason,
        )
        if not updated:
            await interaction.followup.send("Не удалось завершить заявку: её состояние уже изменилось.", ephemeral=True)
            return

        application = db.get_application_by_channel(interaction.channel.id)
        if not application:
            await interaction.followup.send("Заявка уже недоступна.", ephemeral=True)
            return

        applicant = await fetch_application_user(interaction.guild, application["user_id"])
        reviewer = interaction.user
        applicant_label = applicant.mention if applicant else f"<@{application['user_id']}>"
        description = (
            f"Заявка от пользователя {applicant_label}\n\n"
            "На вступление в семью была отклонена. 😢\n\n"
            f"**Причина:** {reason}\n"
            f"**Рассматривал заявку:** {reviewer.mention}"
        )
        embed = build_results_embed("Заявка отклонена", description, COLOR_DANGER)
        await send_results_message(interaction.guild, config, embed)
        if applicant:
            await send_dm_safely(applicant, embed=build_results_embed("Заявка отклонена", description, COLOR_DANGER))

        if self.interview_stage:
            await refresh_interview_message(interaction.guild, application, disabled=True)
        else:
            await refresh_review_message(interaction.guild, application, disabled=True)

        final_text = f'Заявка была отклонена по причине: "{reason}".'
        archive_seq = await archive_application_channel(interaction.channel, config, application, reviewer, final_text)
        await interaction.followup.send(
            f"Причина сохранена. Заявка архивирована как `{build_archive_channel_name(application['user_id'], archive_seq)}`.",
            ephemeral=True,
        )


class ApplicationModal(discord.ui.Modal, title="Подать заявку на вступление в семью"):
    identity = discord.ui.TextInput(
        label="Ник / Имя ИРЛ / Возраст ИРЛ",
        placeholder="Vordy Nutsovich / Давид / 19",
        style=discord.TextStyle.short,
        required=True,
        max_length=120,
    )
    experience = discord.ui.TextInput(
        label="Ваш опыт на RP проекте",
        placeholder="Мой путь начинался с далекого 2019...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=800,
    )
    hours = discord.ui.TextInput(
        label="Количество часов в GTA, опыт в семьях",
        placeholder="Около 7000 часов. Опыт в семьях...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=800,
    )
    loadout = discord.ui.TextInput(
        label="Откат с ГГшки",
        placeholder="Любая карта, Спешик/Тяга + Сайга, 7+ минут.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=400,
    )
    online = discord.ui.TextInput(
        label="Онлайн / время / часовой пояс",
        placeholder="5–6 часов / стабильный онлайн / МСК",
        style=discord.TextStyle.short,
        required=True,
        max_length=120,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Подать заявку можно только на сервере.", ephemeral=True)
            return

        config = db.get_config(interaction.guild.id)
        if not config.recruitment_open:
            await interaction.response.send_message("Сейчас набор закрыт. Попробуй позже.", ephemeral=True)
            return

        missing = []
        if not config.result_channel_id:
            missing.append("канал итогов")
        if not config.voice_channel_id:
            missing.append("голосовой канал проверки")
        if not config.review_role_id:
            missing.append("роль рекрутов")
        if missing:
            await interaction.response.send_message(
                "Бот еще не настроен: " + ", ".join(missing),
                ephemeral=True,
            )
            return

        already_open = db.get_open_application_by_user(interaction.guild.id, interaction.user.id)
        if already_open:
            await interaction.response.send_message(
                "У тебя уже есть активная заявка. Дождись решения рекрутов.",
                ephemeral=True,
            )
            return

        latest_application = db.get_latest_application_by_user(interaction.guild.id, interaction.user.id)
        if config.cooldown_enabled and latest_application and APPLICATION_COOLDOWN_DAYS > 0:
            last_created_at = parse_iso_datetime(latest_application["created_at"])
            cooldown_until = last_created_at + timedelta(days=APPLICATION_COOLDOWN_DAYS)
            now_utc = datetime.now(timezone.utc)
            if cooldown_until > now_utc:
                remaining = cooldown_until - now_utc
                total_hours = max(1, int(remaining.total_seconds() // 3600))
                remaining_days = total_hours // 24
                remaining_hours = total_hours % 24
                await interaction.response.send_message(
                    (
                        "Ты уже подавал заявку недавно.\n\n"
                        f"Новая заявка доступна не чаще 1 раза в **{APPLICATION_COOLDOWN_DAYS} дн.**\n"
                        f"Следующая попытка: {format_retry_time(cooldown_until)}\n"
                        f"Осталось примерно: **{remaining_days} д. {remaining_hours} ч.**"
                    ),
                    ephemeral=True,
                )
                return

        answers = {
            "identity": str(self.identity),
            "experience": str(self.experience),
            "hours": str(self.hours),
            "loadout": str(self.loadout),
            "online": str(self.online),
        }

        category = await get_or_create_category(interaction.guild, config.applications_category_id, ACTIVE_CATEGORY_NAME)
        review_role = interaction.guild.get_role(config.review_role_id)
        if not review_role:
            await interaction.response.send_message("Роль рекрутов не найдена. Проверь настройку роли.", ephemeral=True)
            return

        me = interaction.guild.me or interaction.guild.get_member(bot.user.id if bot.user else 0)
        if not me:
            await interaction.response.send_message("Не удалось определить аккаунт бота на сервере.", ephemeral=True)
            return

        channel_name = f"anketa-{clean_channel_name(interaction.user.display_name)}-{str(interaction.user.id)[-4:]}"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
            ),
            review_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
        }

        channel = await interaction.guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Заявка пользователя {interaction.user} ({interaction.user.id})",
            reason="Создан канал новой заявки",
        )

        application_id = db.create_application(interaction.guild.id, interaction.user.id, channel.id, answers)

        recruiter_ping_role = interaction.guild.get_role(config.review_role_id) if config.review_role_id else None
        if recruiter_ping_role:
            await channel.send(
                content=f"{recruiter_ping_role.mention}, новая заявка на рассмотрение.",
                allowed_mentions=discord.AllowedMentions(users=False, roles=True, everyone=False),
            )

        embed = build_application_embed(interaction.user, answers, application_id)
        review_message = await channel.send(embed=embed, view=ReviewView(claimed=False))
        db.update_application(application_id, review_message_id=review_message.id)

        await interaction.response.send_message(
            "Заявка отправлена. Ожидай решения рекрутов в ЛС или в канале итогов, если личные сообщения закрыты.",
            ephemeral=True,
        )


class StartFormView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Заполнить анкету",
        style=discord.ButtonStyle.secondary,
        emoji="📩",
        custom_id="family_start_fill_form",
    )
    async def fill_form(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            await interaction.response.send_modal(ApplicationModal())
        except Exception as exc:
            logger.exception("Не удалось открыть модальное окно анкеты: %s", exc)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Не удалось открыть форму. Проверь логи бота и попробуй еще раз.",
                    ephemeral=True,
                )


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Подать заявку",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        custom_id="family_panel_open_form",
    )
    async def open_form(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        config = db.get_config(interaction.guild.id)
        await interaction.response.send_message(
            embed=build_panel_popup_embed(interaction.guild, config),
            view=StartFormView(),
            ephemeral=True,
        )


def claimed_reviewer_id(application: sqlite3.Row | None) -> int:
    return int(application["reviewer_id"] or 0) if application else 0


async def fetch_application_user(guild: discord.Guild, user_id: int) -> Optional[discord.User | discord.Member]:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await bot.fetch_user(user_id)
    except Exception:
        return None


async def refresh_review_message(guild: discord.Guild, application: sqlite3.Row, *, disabled: bool = False) -> None:
    if not application["review_message_id"]:
        return
    channel = guild.get_channel(application["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(int(application["review_message_id"]))
    except Exception:
        return

    applicant = await fetch_application_user(guild, application["user_id"])
    if not applicant:
        return

    reviewer_mention = f"<@{claimed_reviewer_id(application)}>" if claimed_reviewer_id(application) else None
    embed = build_application_embed(applicant, json.loads(application["answers_json"]), int(application["id"]), reviewer_mention)
    try:
        await message.edit(embed=embed, view=ReviewView(claimed=bool(claimed_reviewer_id(application)), disabled=disabled))
    except Exception:
        logger.exception("Не удалось обновить сообщение заявки %s", application["id"])


def build_interview_stage_embed(reviewer_mention: str) -> discord.Embed:
    return discord.Embed(
        title="🎙️ Как прошёл обзвон?",
        description=(
            f"**Заявку продолжает вести:** {reviewer_mention}\n\n"
            "После завершения обзвона выбери итог ниже."
        ),
        color=COLOR_INFO,
        timestamp=datetime.now(timezone.utc),
    )


async def refresh_interview_message(guild: discord.Guild, application: sqlite3.Row, *, disabled: bool = False) -> None:
    if not application["interview_message_id"]:
        return
    channel = guild.get_channel(application["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(int(application["interview_message_id"]))
    except Exception:
        return

    reviewer_mention = f"<@{claimed_reviewer_id(application)}>" if claimed_reviewer_id(application) else "не назначен"
    try:
        await message.edit(
            embed=build_interview_stage_embed(reviewer_mention),
            view=InterviewView(claimed=bool(claimed_reviewer_id(application)), disabled=disabled),
        )
    except Exception:
        logger.exception("Не удалось обновить сообщение этапа обзвона %s", application["id"])


class ReviewView(discord.ui.View):
    def __init__(self, *, claimed: bool = False, disabled: bool = False):
        super().__init__(timeout=None)
        decisions_enabled = claimed and not disabled
        self.take.disabled = disabled or claimed
        self.approve.disabled = not decisions_enabled
        self.reserve.disabled = not decisions_enabled
        self.reject.disabled = not decisions_enabled

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Эта панель работает только на сервере.", ephemeral=True)
            return False
        config = db.get_config(interaction.guild.id)
        if not has_reviewer_access(interaction.user, config):
            await interaction.response.send_message("У тебя нет доступа к рассмотрению заявок.", ephemeral=True)
            return False
        application = db.get_application_by_channel(interaction.channel.id) if isinstance(interaction.channel, discord.TextChannel) else None
        if not application:
            await interaction.response.send_message("Не удалось найти заявку в базе.", ephemeral=True)
            return False

        custom_id = str((interaction.data or {}).get("custom_id", ""))
        reviewer_id = claimed_reviewer_id(application)
        if custom_id == "family_review_take":
            if application["status"] != STATUS_SUBMITTED:
                await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
                return False
            if reviewer_id and reviewer_id != interaction.user.id:
                await interaction.response.send_message(
                    f"Эту заявку уже взял на рассмотрение <@{reviewer_id}>.",
                    ephemeral=True,
                )
                return False
            if reviewer_id == interaction.user.id:
                await interaction.response.send_message("Эта заявка уже у тебя на рассмотрении.", ephemeral=True)
                return False
            return True

        if application["status"] != STATUS_SUBMITTED:
            await interaction.response.send_message("Начальный этап по этой заявке уже завершён.", ephemeral=True)
            return False
        if not reviewer_id:
            await interaction.response.send_message("Сначала нажми «Взять на рассмотрение».", ephemeral=True)
            return False
        if reviewer_id != interaction.user.id:
            await interaction.response.send_message(
                f"Эту заявку рассматривает <@{reviewer_id}>.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Взять на рассмотрение", style=discord.ButtonStyle.secondary, custom_id="family_review_take", emoji="👀")
    async def take(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        application = db.get_application_by_channel(interaction.channel.id)
        if not application:
            await interaction.response.send_message("Не удалось найти заявку в базе.", ephemeral=True)
            return

        claimed = db.claim_application(application["id"], interaction.user.id)
        if not claimed:
            application = db.get_application_by_channel(interaction.channel.id)
            reviewer_id = claimed_reviewer_id(application)
            if reviewer_id and reviewer_id != interaction.user.id:
                await interaction.response.send_message(
                    f"Эту заявку уже взял на рассмотрение <@{reviewer_id}>.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("Не удалось взять заявку на рассмотрение.", ephemeral=True)
            return

        application = db.get_application_by_channel(interaction.channel.id)
        if application:
            await refresh_review_message(interaction.guild, application)
        await interaction.response.send_message("Заявка закреплена за тобой.", ephemeral=True)

    @discord.ui.button(label="Одобрить", style=discord.ButtonStyle.success, custom_id="family_review_approve")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await process_review_decision(interaction, reserve=False)

    @discord.ui.button(label="Отправить в резерв", style=discord.ButtonStyle.secondary, custom_id="family_review_reserve")
    async def reserve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await process_review_decision(interaction, reserve=True)

    @discord.ui.button(label="Отказать", style=discord.ButtonStyle.danger, custom_id="family_review_reject")
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(RejectReasonModal(interview_stage=False))


class InterviewView(discord.ui.View):
    def __init__(self, *, claimed: bool = True, disabled: bool = False):
        super().__init__(timeout=None)
        self.accept.disabled = disabled or not claimed
        self.reject.disabled = disabled or not claimed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Эта панель работает только на сервере.", ephemeral=True)
            return False
        config = db.get_config(interaction.guild.id)
        if not has_reviewer_access(interaction.user, config):
            await interaction.response.send_message("У тебя нет доступа к рассмотрению заявок.", ephemeral=True)
            return False
        application = db.get_application_by_channel(interaction.channel.id) if isinstance(interaction.channel, discord.TextChannel) else None
        if not application:
            await interaction.response.send_message("Не удалось найти заявку в базе.", ephemeral=True)
            return False
        if application["status"] not in (STATUS_APPROVED_PENDING, STATUS_RESERVE_PENDING):
            await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
            return False

        reviewer_id = claimed_reviewer_id(application)
        if not reviewer_id:
            await interaction.response.send_message("Сначала возьми заявку на рассмотрение.", ephemeral=True)
            return False
        if reviewer_id != interaction.user.id:
            await interaction.response.send_message(
                f"Эту заявку рассматривает <@{reviewer_id}>.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Отлично! Принят!", style=discord.ButtonStyle.success, custom_id="family_interview_accept")
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        config = db.get_config(interaction.guild.id)
        application = db.get_application_by_channel(interaction.channel.id)
        if not application:
            await interaction.response.send_message("Не удалось найти заявку в базе.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = db.transition_application(
            application["id"],
            (STATUS_APPROVED_PENDING, STATUS_RESERVE_PENDING),
            STATUS_ACCEPTED,
            interaction.user.id,
            require_reviewer_id=interaction.user.id,
        )
        if not updated:
            await interaction.followup.send("Не удалось завершить заявку: её состояние уже изменилось.", ephemeral=True)
            return

        application = db.get_application_by_channel(interaction.channel.id)
        if not application:
            await interaction.followup.send("Заявка уже недоступна.", ephemeral=True)
            return

        applicant = await fetch_application_user(interaction.guild, application["user_id"])
        applicant_label = applicant.mention if applicant else f"<@{application['user_id']}>"
        result_text = (
            f"Пользователь {applicant_label} успешно прошёл обзвон.\n"
            f"**Рассматривал заявку:** {interaction.user.mention}"
        )
        await send_results_message(interaction.guild, config, build_results_embed("Обзвон успешно пройден", result_text, COLOR_SUCCESS))
        await refresh_interview_message(interaction.guild, application, disabled=True)

        archive_seq = await archive_application_channel(
            interaction.channel,
            config,
            application,
            interaction.user,
            "Заявка была принята.",
        )
        await interaction.followup.send(
            f"Обзвон отмечен как успешный. Канал архивирован как `{build_archive_channel_name(application['user_id'], archive_seq)}`.",
            ephemeral=True,
        )

    @discord.ui.button(label="Плохо 😢 Не принят", style=discord.ButtonStyle.danger, custom_id="family_interview_reject")
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(RejectReasonModal(interview_stage=True))


async def process_review_decision(interaction: discord.Interaction, reserve: bool) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
        return

    config = db.get_config(interaction.guild.id)
    application = db.get_application_by_channel(interaction.channel.id)
    if not application:
        await interaction.response.send_message("Не удалось найти заявку в базе.", ephemeral=True)
        return

    reviewer_id = claimed_reviewer_id(application)
    if not reviewer_id:
        await interaction.response.send_message("Сначала нажми «Взять на рассмотрение».", ephemeral=True)
        return
    if reviewer_id != interaction.user.id:
        await interaction.response.send_message(
            f"Эту заявку рассматривает <@{reviewer_id}>.",
            ephemeral=True,
        )
        return
    if application["status"] != STATUS_SUBMITTED:
        await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    applicant = await fetch_application_user(interaction.guild, application["user_id"])
    reviewer = interaction.user
    voice_channel = interaction.guild.get_channel(config.voice_channel_id) if config.voice_channel_id else None
    voice_text = voice_channel.mention if isinstance(voice_channel, discord.VoiceChannel) else "голосовой канал проверки"
    status = STATUS_RESERVE_PENDING if reserve else STATUS_APPROVED_PENDING
    updated = db.transition_application(
        application["id"],
        (STATUS_SUBMITTED,),
        status,
        reviewer.id,
        require_reviewer_id=reviewer.id,
    )
    if not updated:
        await interaction.followup.send("Не удалось сохранить решение: состояние заявки уже изменилось.", ephemeral=True)
        return

    application = db.get_application_by_channel(interaction.channel.id)
    if not application:
        await interaction.followup.send("Заявка уже недоступна.", ephemeral=True)
        return

    applicant_label = applicant.mention if applicant else f"<@{application['user_id']}>"
    if reserve:
        reserve_text = (
            f"Заявка от пользователя {applicant_label}\n\n"
            "На вступление в семью была отклонена. 😢\n\n"
            "**Причина:** На данный момент свободных слотов в семье нет. "
            "Вы будете добавлены в резерв, и как только освободится место, рекрут свяжется с вами.\n"
            f"**Рассматривал заявку:** {reviewer.mention}"
        )
        if applicant:
            await send_dm_safely(
                applicant,
                embed=build_results_embed("Заявка отправлена в резерв", reserve_text, COLOR_WARNING),
            )
        await send_results_message(interaction.guild, config, build_results_embed("Заявка отправлена в резерв", reserve_text, COLOR_WARNING))
    else:
        accept_text = (
            f"Вы были приняты на сервере **{config.server_name or interaction.guild.name}**.\n"
            f"Заходите в голосовой канал {voice_text} для обзвона.\n\n"
            f"**Рассматривал заявку:** {reviewer.mention}"
        )
        if applicant:
            await send_dm_safely(
                applicant,
                embed=build_results_embed("Заявка одобрена", accept_text, COLOR_SUCCESS),
            )
        results_text = (
            f"Заявка от пользователя {applicant_label} одобрена.\n\n"
            f"**Канал для обзвона:** {voice_text}\n"
            f"**Рассматривал заявку:** {reviewer.mention}"
        )
        await send_results_message(interaction.guild, config, build_results_embed("Заявка одобрена", results_text, COLOR_SUCCESS))

    await refresh_review_message(interaction.guild, application, disabled=True)

    reviewer_mention = reviewer.mention
    interview_message = await interaction.channel.send(
        embed=build_interview_stage_embed(reviewer_mention),
        view=InterviewView(claimed=True),
    )
    db.update_application(application["id"], interview_message_id=interview_message.id)

    summary = "Одобрено" if not reserve else "Отправлено в резерв"
    await interaction.followup.send(f"Решение сохранено: **{summary}**.", ephemeral=True)


# ============================================================
# SLASH-КОМАНДЫ
# ============================================================
@app_commands.command(name="family_setup", description="Настроить каналы, роль рекрутов и внешний вид панели")
@app_commands.describe(
    result_channel="Канал итогов заявок",
    voice_channel="Голосовой канал для обзвона",
    review_role="Роль рекрутов",
    applications_category="Категория активных заявок",
    archive_category="Категория архива",
    server_name="Название сервера в сообщениях бота",
    panel_image_url="Ссылка на картинку для главной панели (необязательно)",
)
async def family_setup(
    interaction: discord.Interaction,
    result_channel: discord.TextChannel,
    voice_channel: discord.VoiceChannel,
    review_role: discord.Role,
    applications_category: Optional[discord.CategoryChannel] = None,
    archive_category: Optional[discord.CategoryChannel] = None,
    server_name: Optional[str] = None,
    panel_image_url: Optional[str] = None,
) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("У тебя нет доступа к этой команде.", ephemeral=True)
        return

    current_config = db.get_config(interaction.guild.id)
    config = db.upsert_config(
        interaction.guild.id,
        result_channel_id=result_channel.id,
        voice_channel_id=voice_channel.id,
        review_role_id=review_role.id,
        recruiter_ping_user_id=current_config.recruiter_ping_user_id,
        applications_category_id=applications_category.id if applications_category else 0,
        archive_category_id=archive_category.id if archive_category else 0,
        server_name=(server_name or interaction.guild.name),
        panel_image_url=(panel_image_url if panel_image_url is not None else current_config.panel_image_url),
        panel_media_url=(panel_image_url if panel_image_url is not None else current_config.panel_media_url),
        panel_media_kind=(detect_panel_media_kind(panel_image_url) if panel_image_url is not None else current_config.panel_media_kind),
        panel_media_filename=(infer_filename_from_url(panel_image_url) if panel_image_url is not None else current_config.panel_media_filename),
    )

    await refresh_panel_message(interaction.guild)

    embed = discord.Embed(title="Настройка сохранена", color=COLOR_SUCCESS)
    embed.add_field(name="Канал итогов", value=f"{result_channel.mention}\n`{config.result_channel_id}`", inline=False)
    embed.add_field(name="Голосовой канал", value=f"{voice_channel.mention}\n`{config.voice_channel_id}`", inline=False)
    embed.add_field(name="Роль рекрутов", value=f"{review_role.mention}\n`{config.review_role_id}`", inline=False)
    embed.add_field(name="Пинг роли при новой заявке", value=recruiter_ping_summary(interaction.guild, config), inline=False)
    embed.add_field(name="Категория заявок", value=applications_category.mention if applications_category else "Автосоздание", inline=False)
    embed.add_field(name="Категория архива", value=archive_category.mention if archive_category else "Автосоздание", inline=False)
    embed.add_field(name="Название сервера", value=config.server_name, inline=False)
    embed.add_field(name="Медиа панели", value=panel_media_summary(config), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(name="family_panel", description="Отправить и закрепить главную панель набора")
@app_commands.describe(channel="Канал, куда отправить панель")
async def family_panel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("У тебя нет доступа к этой команде.", ephemeral=True)
        return

    config = await ensure_defaults_saved(interaction.guild)
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message("Эта команда должна использоваться в текстовом канале.", ephemeral=True)
        return

    if config.panel_channel_id and config.panel_message_id:
        old_channel = interaction.guild.get_channel(config.panel_channel_id)
        if isinstance(old_channel, discord.TextChannel):
            try:
                old_msg = await old_channel.fetch_message(config.panel_message_id)
                await old_msg.delete()
            except Exception:
                pass

    panel_kwargs = await build_panel_message_kwargs(interaction.guild, config, for_edit=False)
    message = await target_channel.send(**panel_kwargs)
    try:
        await message.pin(reason="Главная панель набора")
    except Exception:
        pass

    db.upsert_config(interaction.guild.id, panel_channel_id=target_channel.id, panel_message_id=message.id)
    await interaction.response.send_message(f"Панель отправлена в {target_channel.mention} и закреплена.", ephemeral=True)


@app_commands.command(name="family_panel_image", description="Установить или убрать картинку, GIF или видео на главной панели")
@app_commands.describe(
    image_url="URL картинки / GIF / mp4 / mov. Если ничего не указать, медиа будет удалено",
    attachment="Файл из Discord: картинка, GIF, mp4 или mov"
)
async def family_panel_image(
    interaction: discord.Interaction,
    image_url: Optional[str] = None,
    attachment: Optional[discord.Attachment] = None,
) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("У тебя нет доступа к этой команде.", ephemeral=True)
        return

    if attachment is not None:
        media_kind = detect_attachment_media_kind(attachment)
        if not media_kind:
            await interaction.response.send_message(
                "Поддерживаются только картинки, GIF, mp4, mov, webm и m4v.",
                ephemeral=True,
            )
            return

        db.upsert_config(
            interaction.guild.id,
            panel_image_url=attachment.url if media_kind == "image" else "",
            panel_media_url=attachment.url,
            panel_media_kind=media_kind,
            panel_media_filename=attachment.filename or infer_filename_from_url(attachment.url),
        )
        await refresh_panel_message(interaction.guild)

        label = "GIF / изображение" if media_kind == "image" else "видео"
        await interaction.response.send_message(
            f"Медиа панели сохранено через вложение Discord ({label}) и панель обновлена.",
            ephemeral=True,
        )
        return

    cleaned = (image_url or "").strip()
    if cleaned:
        media_kind = detect_panel_media_kind(cleaned)
        db.upsert_config(
            interaction.guild.id,
            panel_image_url=cleaned if media_kind == "image" else "",
            panel_media_url=cleaned,
            panel_media_kind=media_kind,
            panel_media_filename=infer_filename_from_url(cleaned),
        )
        await refresh_panel_message(interaction.guild)
        if media_kind == "video":
            await interaction.response.send_message(
                "Видео панели сохранено и панель обновлена. Видео будет показано как вложение в сообщении панели.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("Картинка / GIF панели сохранена и панель обновлена.", ephemeral=True)
        return

    db.upsert_config(
        interaction.guild.id,
        panel_image_url="",
        panel_media_url="",
        panel_media_kind="",
        panel_media_filename="",
    )
    await refresh_panel_message(interaction.guild)
    await interaction.response.send_message("Медиа панели удалено и панель обновлена.", ephemeral=True)


@app_commands.command(name="family_recruitment", description="Открыть или закрыть набор")
@app_commands.describe(status="open или close")
@app_commands.choices(
    status=[
        app_commands.Choice(name="open", value="open"),
        app_commands.Choice(name="close", value="close"),
    ]
)
async def family_recruitment(interaction: discord.Interaction, status: app_commands.Choice[str]) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("У тебя нет доступа к этой команде.", ephemeral=True)
        return

    recruitment_open = 1 if status.value == "open" else 0
    db.upsert_config(interaction.guild.id, recruitment_open=recruitment_open)
    await refresh_panel_message(interaction.guild)

    text = "Набор открыт." if recruitment_open else "Набор закрыт."
    await interaction.response.send_message(text, ephemeral=True)


@app_commands.command(name="family_cooldown", description="Включить или выключить кулдаун на повторную заявку")
@app_commands.describe(status="on или off")
@app_commands.choices(
    status=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def family_cooldown(interaction: discord.Interaction, status: app_commands.Choice[str]) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("У тебя нет доступа к этой команде.", ephemeral=True)
        return

    cooldown_enabled = 1 if status.value == "on" else 0
    db.upsert_config(interaction.guild.id, cooldown_enabled=cooldown_enabled)
    await refresh_panel_message(interaction.guild)

    if cooldown_enabled:
        text = f"Кулдаун заявок включен. Сейчас стоит лимит: {APPLICATION_COOLDOWN_DAYS} дн."
    else:
        text = "Кулдаун заявок выключен. Повторную заявку можно подавать без ожидания."
    await interaction.response.send_message(text, ephemeral=True)


@app_commands.command(name="family_config", description="Показать текущую настройку бота")
async def family_config(interaction: discord.Interaction) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("У тебя нет доступа к этой команде.", ephemeral=True)
        return

    config = db.get_config(interaction.guild.id)
    embed = discord.Embed(title="Текущая настройка", color=COLOR_INFO)
    embed.add_field(name="Канал итогов", value=f"`{config.result_channel_id or 0}`", inline=False)
    embed.add_field(name="Голосовой канал проверки", value=f"`{config.voice_channel_id or 0}`", inline=False)
    embed.add_field(name="Роль рекрутов", value=f"`{config.review_role_id or 0}`", inline=False)
    embed.add_field(name="Пинг роли при новой заявке", value=recruiter_ping_summary(interaction.guild, config), inline=False)
    embed.add_field(name="Категория заявок", value=f"`{config.applications_category_id or 0}`", inline=False)
    embed.add_field(name="Категория архива", value=f"`{config.archive_category_id or 0}`", inline=False)
    embed.add_field(name="Статус набора", value="Открыт" if config.recruitment_open else "Закрыт", inline=False)
    embed.add_field(
        name="Кулдаун заявок",
        value=(f"Включен ({APPLICATION_COOLDOWN_DAYS} дн.)" if config.cooldown_enabled else "Выключен"),
        inline=False,
    )
    embed.add_field(name="Медиа панели", value=panel_media_summary(config), inline=False)
    embed.add_field(name="Название сервера", value=config.server_name or interaction.guild.name, inline=False)
    embed.add_field(name="Бот-админы из bot.py", value=", ".join(f"`{x}`" for x in sorted(BOT_OWNER_IDS) if x) or "Не заданы", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(name="family_sync", description="Принудительно пересинхронизировать slash-команды")
async def family_sync(interaction: discord.Interaction) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("У тебя нет доступа к этой команде.", ephemeral=True)
        return

    synced = await sync_tree_for_guild(interaction.guild)
    await interaction.response.send_message(f"Команды синхронизированы: {synced}", ephemeral=True)


@app_commands.command(name="family_archive_find", description="Найти архивные заявки пользователя")
@app_commands.describe(
    user="Пользователь сервера",
    user_id="ID пользователя, если его нет на сервере или удобнее искать по ID",
)
async def family_archive_find(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    user_id: Optional[str] = None,
) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return

    config = db.get_config(interaction.guild.id)
    if not has_reviewer_access(interaction.user, config):
        await interaction.response.send_message("У тебя нет доступа к этой команде.", ephemeral=True)
        return

    if user is None and not user_id:
        await interaction.response.send_message("Укажи пользователя или его ID.", ephemeral=True)
        return

    target_user_id = 0
    if user is not None:
        target_user_id = user.id
    else:
        digits = re.sub(r"\D+", "", user_id or "")
        if not digits:
            await interaction.response.send_message("ID пользователя должен содержать цифры.", ephemeral=True)
            return
        try:
            target_user_id = int(digits)
        except ValueError:
            await interaction.response.send_message("Не удалось разобрать ID пользователя.", ephemeral=True)
            return

    archived_rows = db.get_archived_applications_by_user(interaction.guild.id, target_user_id, limit=50)
    if not archived_rows:
        await interaction.response.send_message(f"Архивных заявок для пользователя `{target_user_id}` не найдено.", ephemeral=True)
        return

    target_member = interaction.guild.get_member(target_user_id)
    display_name = target_member.mention if target_member else f"<@{target_user_id}>"

    embed = discord.Embed(
        title="Архивные заявки пользователя",
        description=(
            f"**Пользователь:** {display_name}\n"
            f"**ID:** `{target_user_id}`\n"
            f"**Найдено заявок:** **{len(archived_rows)}**"
        ),
        color=COLOR_INFO,
        timestamp=datetime.now(timezone.utc),
    )

    lines: list[str] = []
    for row in archived_rows[:20]:
        archived_channel_name = build_archive_channel_name(row["user_id"], row["archive_seq"] or 0)
        channel_obj = interaction.guild.get_channel(row["channel_id"])
        channel_text = channel_obj.mention if isinstance(channel_obj, discord.TextChannel) else f"`{archived_channel_name}`"

        reviewer_text = f"<@{row['reviewer_id']}>" if row["reviewer_id"] else "не указан"
        status_map = {
            STATUS_ACCEPTED: "Принята",
            STATUS_REJECTED: "Отклонена",
            STATUS_INTERVIEW_FAILED: "Не принята после обзвона",
            STATUS_APPROVED_PENDING: "Одобрена, ожидался обзвон",
            STATUS_RESERVE_PENDING: "Резерв, ожидался обзвон",
        }
        status_text = status_map.get(row["status"], row["status"] or "неизвестно")
        reason_text = row["reason"] or "—"
        archived_at = row["archived_at"] or row["updated_at"] or row["created_at"]
        when_text = archived_at[:19].replace("T", " ") if archived_at else "неизвестно"

        lines.append(
            f"**#{row['archive_seq']:03d}** • {channel_text}\n"
            f"Статус: **{status_text}**\n"
            f"Причина: {reason_text}\n"
            f"Рассматривал: {reviewer_text}\n"
            f"Архивировано: `{when_text} UTC`"
        )

    embed.add_field(name="Последние архивы", value="\n\n".join(lines), inline=False)

    if len(archived_rows) > 20:
        embed.set_footer(text=f"Показаны последние 20 из {len(archived_rows)} заявок")

    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(family_setup)
bot.tree.add_command(family_panel)
bot.tree.add_command(family_panel_image)
bot.tree.add_command(family_recruitment)
bot.tree.add_command(family_cooldown)
bot.tree.add_command(family_config)
bot.tree.add_command(family_sync)
bot.tree.add_command(family_archive_find)


async def sync_tree_for_guild(guild: discord.Guild | None) -> int:
    if COMMAND_GUILD_ID and guild:
        target = discord.Object(id=COMMAND_GUILD_ID)
        bot.tree.copy_global_to(guild=target)
        synced = await bot.tree.sync(guild=target)
        return len(synced)

    synced = await bot.tree.sync()
    return len(synced)


@bot.event
async def on_ready() -> None:
    bot.add_view(PanelView())
    bot.add_view(StartFormView())
    bot.add_view(ReviewView())
    bot.add_view(InterviewView())

    for guild in bot.guilds:
        await ensure_defaults_saved(guild)

    try:
        if COMMAND_GUILD_ID:
            target = discord.Object(id=COMMAND_GUILD_ID)
            bot.tree.copy_global_to(guild=target)
            synced = await bot.tree.sync(guild=target)
            logger.info("Slash-команды синхронизированы в guild %s: %s", COMMAND_GUILD_ID, len(synced))
        else:
            synced = await bot.tree.sync()
            logger.info("Глобальные slash-команды синхронизированы: %s", len(synced))
    except Exception as exc:
        logger.exception("Не удалось синхронизировать slash-команды: %s", exc)

    logger.info("Бот запущен как %s (%s)", bot.user, bot.user.id if bot.user else "-")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    await ensure_defaults_saved(guild)
    try:
        await sync_tree_for_guild(guild)
    except Exception:
        logger.exception("Ошибка при синхронизации команд после входа на сервер %s", guild.id)


if __name__ == "__main__":
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.errors.LoginFailure:
        logger.error(
            "Discord отклонил токен. Проверь токен на хостинге или в FALLBACK_TOKEN. "
            "Не вставляй префикс 'Bot ' и убери лишние пробелы."
        )
        raise
