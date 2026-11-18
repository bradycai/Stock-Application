"""Shared report envelope + writer for the backtest CLIs (deterministic JSON)."""
import json
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("baseline", "ai_replay", "paper_report")


def parse_created_at(value: str | None) -> str:
    """The CLI's only wall-clock value; tests pin it with --created-at."""
    if value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def envelope(kind: str, created_at: str, summary: dict, body: dict) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown report kind {kind!r}")
    return {**body, "kind": kind, "created_at": created_at, "summary": summary}


def dumps(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"


def write_report(report: dict, out: str | None) -> None:
    text = dumps(report)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    else:
        print(text, end="")


def baseline_summary(report: dict, start, end) -> dict:
    """Headline numbers of a run.py report (the first window that carries metrics)."""
    first_fold = (report.get("folds") or [{}])[0].get("result")
    window = report.get("full") or report.get("holdout") or first_fold or {}
    m = window.get("metrics", {})
    return {"start": start.isoformat(), "end": end.isoformat(), "split": report.get("split"),
            "net_total_return": m.get("net_total_return"),
            "benchmark_relative_return": m.get("benchmark_relative_return"),
            "max_drawdown": m.get("max_drawdown"), "trades": m.get("trades")}
