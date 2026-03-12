from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional

from familybot.constants import OPEN_STATUSES
from familybot.utils import utcnow_iso

_REASON_UNSET = object()


class Database:
    def __init__(self, path: str):
        self.path = str(Path(path))
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
                    result_channel_id INTEGER,
                    voice_channel_id INTEGER,
                    review_role_id INTEGER,
                    applications_category_id INTEGER,
                    archive_category_id INTEGER,
                    server_name TEXT DEFAULT 'Ваш сервер',
                    recruitment_open INTEGER DEFAULT 1,
                    panel_channel_id INTEGER,
                    panel_message_id INTEGER,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_bot_admins (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    review_message_id INTEGER,
                    interview_message_id INTEGER,
                    reviewer_id INTEGER DEFAULT 0,
                    status TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "applications", "review_message_id", "INTEGER")
            self._ensure_column(conn, "applications", "interview_message_id", "INTEGER")
            self._ensure_column(conn, "applications", "reviewer_id", "INTEGER DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_applications_channel ON applications(channel_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_applications_guild_user_status ON applications(guild_id, user_id, status, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_applications_guild_user_created ON applications(guild_id, user_id, created_at DESC, id DESC)"
            )

    def get_guild_settings(self, guild_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()

    def upsert_guild_settings(self, guild_id: int, **kwargs: Any) -> None:
        current = self.get_guild_settings(guild_id)
        payload: dict[str, Any] = {
            "result_channel_id": None,
            "voice_channel_id": None,
            "review_role_id": None,
            "applications_category_id": None,
            "archive_category_id": None,
            "server_name": "Ваш сервер",
            "recruitment_open": 1,
            "panel_channel_id": None,
            "panel_message_id": None,
            "updated_at": utcnow_iso(),
        }
        if current:
            payload.update(dict(current))
        payload.update(kwargs)
        payload["updated_at"] = utcnow_iso()

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO guild_settings (
                    guild_id,
                    result_channel_id,
                    voice_channel_id,
                    review_role_id,
                    applications_category_id,
                    archive_category_id,
                    server_name,
                    recruitment_open,
                    panel_channel_id,
                    panel_message_id,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    result_channel_id = excluded.result_channel_id,
                    voice_channel_id = excluded.voice_channel_id,
                    review_role_id = excluded.review_role_id,
                    applications_category_id = excluded.applications_category_id,
                    archive_category_id = excluded.archive_category_id,
                    server_name = excluded.server_name,
                    recruitment_open = excluded.recruitment_open,
                    panel_channel_id = excluded.panel_channel_id,
                    panel_message_id = excluded.panel_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id,
                    payload["result_channel_id"],
                    payload["voice_channel_id"],
                    payload["review_role_id"],
                    payload["applications_category_id"],
                    payload["archive_category_id"],
                    payload["server_name"],
                    payload["recruitment_open"],
                    payload["panel_channel_id"],
                    payload["panel_message_id"],
                    payload["updated_at"],
                ),
            )

    def add_bot_admin(self, guild_id: int, user_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO guild_bot_admins (guild_id, user_id, added_at)
                VALUES (?, ?, ?)
                """,
                (guild_id, user_id, utcnow_iso()),
            )

    def remove_bot_admin(self, guild_id: int, user_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM guild_bot_admins WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )

    def list_bot_admin_ids(self, guild_id: int) -> list[int]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT user_id FROM guild_bot_admins WHERE guild_id = ? ORDER BY user_id",
                (guild_id,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def is_bot_admin(self, guild_id: int, user_id: int) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM guild_bot_admins WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return row is not None

    def create_application(self, guild_id: int, user_id: int, channel_id: int, answers: dict[str, str]) -> int:
        now = utcnow_iso()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO applications (
                    guild_id, user_id, channel_id, review_message_id,
                    interview_message_id, reviewer_id, status,
                    answers_json, reason, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, 0, ?, ?, NULL, ?, ?)
                """,
                (guild_id, user_id, channel_id, OPEN_STATUSES[0], json.dumps(answers, ensure_ascii=False), now, now),
            )
            return int(cursor.lastrowid)

    def update_application(self, application_id: int, **kwargs: Any) -> None:
        if not kwargs:
            return
        kwargs["updated_at"] = utcnow_iso()
        columns = ", ".join(f"{key} = ?" for key in kwargs)
        values = list(kwargs.values())
        values.append(application_id)
        with closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE applications SET {columns} WHERE id = ?", values)

    def claim_application(self, application_id: int, reviewer_id: int) -> bool:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE applications
                SET reviewer_id = ?, updated_at = ?
                WHERE id = ? AND status = ? AND COALESCE(reviewer_id, 0) = 0
                """,
                (reviewer_id, utcnow_iso(), application_id, OPEN_STATUSES[0]),
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
        params: list[Any] = [to_status, reviewer_id, utcnow_iso()]
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

    def get_application(self, application_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM applications WHERE id = ?",
                (application_id,),
            ).fetchone()

    def get_active_user_application(self, guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        params: list[Any] = [guild_id, user_id, *OPEN_STATUSES]
        with closing(self._connect()) as conn:
            return conn.execute(
                f"""
                SELECT * FROM applications
                WHERE guild_id = ? AND user_id = ? AND status IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()

    def get_open_applications(self) -> list[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        with closing(self._connect()) as conn:
            return conn.execute(
                f"SELECT * FROM applications WHERE status IN ({placeholders})",
                OPEN_STATUSES,
            ).fetchall()

    @staticmethod
    def answers_from_row(row: sqlite3.Row) -> dict[str, str]:
        return json.loads(row["answers_json"])
