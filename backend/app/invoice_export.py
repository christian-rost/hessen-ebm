from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from .models import AnalysisResult


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def save_upload(upload: UploadFile, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(upload.filename or "upload.pdf").name
    path = target_dir / f"{uuid4().hex}-{safe_name}"
    with path.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    return path


def store_analysis(result: AnalysisResult, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{result.analysis_id}.json"
    path.write_text(json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_analysis(analysis_id: str, target_dir: Path) -> AnalysisResult | None:
    path = target_dir / f"{analysis_id}.json"
    if not path.exists():
        return None
    return AnalysisResult.model_validate_json(path.read_text(encoding="utf-8"))

