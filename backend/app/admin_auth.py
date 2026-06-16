from __future__ import annotations

from fastapi import Header, HTTPException

from .config import get_settings


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    token = get_settings().admin_token
    if not token:
        return
    if x_admin_token != token:
        raise HTTPException(status_code=401, detail="Admin token is missing or invalid.")

