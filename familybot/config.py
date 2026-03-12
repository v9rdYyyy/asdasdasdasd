from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class BotConfig:
    token: str
    global_bot_admin_ids: list[int] = field(default_factory=list)
    db_path: str = "family_bot.sqlite3"
    log_level: str = "INFO"
    sync_commands_globally: bool = True

    @classmethod
    def from_file(cls, path: str | Path) -> "BotConfig":
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(
                f"Не найден файл конфигурации: {config_path}. "
                "Скопируйте config.example.json в config.json и заполните его."
            )

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        token = str(raw.get("token", "")).strip()
        if token.lower().startswith("bot "):
            token = token[4:].strip()

        if not token:
            raise ValueError("В config.json не заполнен token.")

        admin_ids: list[int] = []
        for value in raw.get("global_bot_admin_ids", []):
            try:
                admin_ids.append(int(value))
            except (TypeError, ValueError):
                continue

        db_path = str(raw.get("db_path", "family_bot.sqlite3")).strip() or "family_bot.sqlite3"
        log_level = str(raw.get("log_level", "INFO")).strip().upper() or "INFO"
        sync_commands_globally = bool(raw.get("sync_commands_globally", True))

        return cls(
            token=token,
            global_bot_admin_ids=admin_ids,
            db_path=db_path,
            log_level=log_level,
            sync_commands_globally=sync_commands_globally,
        )
