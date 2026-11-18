"""Read-only API over the JSON reports written by the backtest CLIs.

Reports live in `BACKTEST_REPORT_DIR` (default backend/reports/, gitignored). Only files whose
name matches ^[A-Za-z0-9_.-]+\\.json$ and whose resolved path stays inside that directory are
ever opened; everything else is rejected. A malformed report is skipped in the listing.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.backtest.report_io import KINDS
from app.config import settings
from app.deps import Actor

router = APIRouter(prefix="/backtests", tags=["backtests"])

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json$")
BACKEND_DIR = Path(__file__).resolve().parents[2]
MAX_REPORT_BYTES = 50 * 1024 * 1024


def report_dir() -> Path:
    p = Path(settings.backtest_report_dir)
    return (p if p.is_absolute() else BACKEND_DIR / p).resolve()


def safe_path(name: str) -> Path:
    """Resolve `name` inside the report dir or raise 400 (bad name / escapes the directory)."""
    if not NAME_RE.fullmatch(name) or name.startswith(".."):
        raise HTTPException(status_code=400, detail="invalid report name")
    base = report_dir()
    path = (base / name).resolve()
    if path.parent != base:  # also catches symlinks pointing outside the directory
        raise HTTPException(status_code=400, detail="invalid report name")
    return path


def _read(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # unreadable or malformed (UnicodeDecodeError is a ValueError)
        return None
    return data if isinstance(data, dict) else None


def _entry(name: str, data: dict, path: Path) -> dict | None:
    kind, created, summary = data.get("kind"), data.get("created_at"), data.get("summary")
    if kind is None and {"split", "config", "universe"} <= data.keys():
        # Legacy `app.backtest.run` output written without --created-at.
        kind = "baseline"
        created = created or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        summary = summary if isinstance(summary, dict) else {"split": data.get("split"),
                                                             "universe": data.get("universe")}
    if kind not in KINDS or not isinstance(created, str) or not isinstance(summary, dict):
        return None
    try:
        datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return {"name": name, "kind": kind, "created_at": created, "summary": summary}


@router.get("/reports")
def list_reports(_actor: str = Actor):
    base = report_dir()
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.iterdir()):
        if not NAME_RE.fullmatch(path.name) or not path.is_file() or path.resolve().parent != base:
            continue
        data = _read(path)
        entry = _entry(path.name, data, path) if data is not None else None
        if entry:
            out.append(entry)
    out.sort(key=lambda e: (e["created_at"], e["name"]), reverse=True)
    return out


@router.get("/reports/{name}")
def get_report(name: str, _actor: str = Actor):
    path = safe_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    data = _read(path)
    if data is None:
        raise HTTPException(status_code=422, detail="report is unreadable or not valid JSON")
    return data
