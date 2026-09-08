"""Конфигурация приложения.

Единственное место, где читаются переменные окружения. Всё остальное
получает готовый объект Settings через явную передачу (DI), а не импортом
глобала — так модули остаются тестируемыми.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    telegram_token: str

    # --- Notion ---
    notion_token: str
    notion_database_id: str
    notion_version: str = "2022-06-28"
    notion_timeout: float = 20.0
    # Notion держит ~3 req/s на интеграцию. Берём с запасом.
    notion_rps: float = 2.5

    # --- Доступ ---
    # "123456789,987654321"; пустая строка = бот открыт для всех
    allowed_user_ids: str = ""

    # --- Очередь / воркер ---
    db_path: Path = Path("data/outbox.sqlite3")
    worker_poll_interval: float = 1.0
    worker_batch_size: int = 10
    max_attempts: int = 8
    # Если задача зависла в in_flight дольше этого — вернуть в pending
    lease_ttl_seconds: float = 120.0

    log_level: str = "INFO"

    @field_validator("telegram_token", "notion_token", "notion_database_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("значение не может быть пустым")
        return v.strip()

    @property
    def allowlist(self) -> set[int]:
        """Множество разрешённых Telegram user_id. Пустое = разрешены все."""
        raw = self.allowed_user_ids.strip()
        if not raw:
            return set()
        return {int(part) for part in raw.replace(";", ",").split(",") if part.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]