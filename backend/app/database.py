from __future__ import annotations

from functools import lru_cache
from typing import Any

from .config import get_settings

try:  # pragma: no cover - exercised in the deployed image when the package is installed.
    from supabase import Client, create_client
except ImportError:  # pragma: no cover - keeps local tests usable without optional dependency.
    Client = Any  # type: ignore[misc, assignment]
    create_client = None  # type: ignore[assignment]


@lru_cache(maxsize=1)
def get_supabase() -> Client | None:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_key:
        return None
    if create_client is None:
        raise RuntimeError("Supabase-Paket ist nicht installiert.")
    return create_client(settings.supabase_url, settings.supabase_key)


def supabase_status() -> dict[str, object]:
    settings = get_settings()
    configured = bool(settings.supabase_url and settings.supabase_key)
    return {
        "configured": configured,
        "client_available": create_client is not None,
        "schema": settings.supabase_schema,
    }
