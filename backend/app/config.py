from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    catalog_db_path: Path
    storage_dir: Path
    admin_token: str | None
    enable_mistral_ocr: bool
    mistral_api_key: str | None
    mistral_ocr_model: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        log_level=os.getenv("LOG_LEVEL", "info"),
        catalog_db_path=Path(os.getenv("CATALOG_DB_PATH", "/app/catalog/ebm_kbv.sqlite")),
        storage_dir=Path(os.getenv("STORAGE_DIR", "./storage")),
        admin_token=os.getenv("ADMIN_TOKEN") or None,
        enable_mistral_ocr=_as_bool(os.getenv("ENABLE_MISTRAL_OCR"), False),
        mistral_api_key=os.getenv("MISTRAL_API_KEY") or None,
        mistral_ocr_model=os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-latest"),
    )
