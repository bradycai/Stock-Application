"""Reports API: contract shape, path-traversal refusal, malformed files skipped. No DB needed."""
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.deps import require_token
from app.routers import backtests


@pytest.fixture()
def env(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(settings, "backtest_report_dir", str(reports))
    (tmp_path / "secret.json").write_text(json.dumps({"kind": "baseline", "secret": True}))
    app = FastAPI()
    app.include_router(backtests.router)
    app.dependency_overrides[require_token] = lambda: "user:test"
    return TestClient(app), reports, tmp_path


def put(reports, name, **body):
    (reports / name).write_text(json.dumps(body))


def test_list_and_get_follow_the_contract(env):
    client, reports, _ = env
    put(reports, "ai_1.json", kind="ai_replay", created_at="2026-05-02T00:00:00+00:00",
        summary={"simulated": 3}, extra=[1, 2])
    put(reports, "paper.json", kind="paper_report", created_at="2026-05-03T00:00:00Z", summary={})
    put(reports, "base.json", kind="baseline", created_at="2026-05-01T00:00:00+00:00",
        summary={"split": "none"})
    r = client.get("/backtests/reports")
    assert r.status_code == 200
    assert [e["name"] for e in r.json()] == ["paper.json", "ai_1.json", "base.json"]
    assert set(r.json()[0]) == {"name", "kind", "created_at", "summary"}
    full = client.get("/backtests/reports/ai_1.json")
    assert full.status_code == 200 and full.json()["extra"] == [1, 2]
    assert client.get("/backtests/reports/nope.json").status_code == 404


def test_legacy_baseline_report_is_listed_as_baseline(env):
    client, reports, _ = env
    put(reports, "old.json", split="none", config={}, universe=["AAPL"], full={})
    (entry,) = client.get("/backtests/reports").json()
    assert entry["kind"] == "baseline" and entry["summary"]["universe"] == ["AAPL"]


def test_malformed_and_foreign_files_are_skipped_not_500(env):
    client, reports, _ = env
    put(reports, "good.json", kind="ai_replay", created_at="2026-05-02T00:00:00+00:00", summary={})
    (reports / "broken.json").write_text("{not json")
    (reports / "binary.json").write_bytes(b"\xff\xfe\x00\x01")
    (reports / "list.json").write_text("[1, 2]")
    put(reports, "nokind.json", created_at="2026-05-02T00:00:00+00:00", summary={})
    put(reports, "badkind.json", kind="other", created_at="2026-05-02T00:00:00+00:00", summary={})
    put(reports, "baddate.json", kind="baseline", created_at="yesterday", summary={})
    (reports / "notes.txt").write_text("x")
    (reports / "sub").mkdir()
    r = client.get("/backtests/reports")
    assert r.status_code == 200 and [e["name"] for e in r.json()] == ["good.json"]
    assert client.get("/backtests/reports/broken.json").status_code == 422


def test_missing_report_directory_lists_nothing(env, monkeypatch, tmp_path):
    client, _, _ = env
    monkeypatch.setattr(settings, "backtest_report_dir", str(tmp_path / "does-not-exist"))
    assert client.get("/backtests/reports").json() == []


@pytest.mark.parametrize("name", [
    "..", "../secret.json", "..%2Fsecret.json", "%2e%2e%2fsecret.json", "..%5Csecret.json",
    "%2Fetc%2Fpasswd", "/etc/passwd", "....//secret.json", "secret.json%00", "a%2Fb.json",
    "report.txt", ".json", "sp ace.json", "..secret.json",
])
def test_path_traversal_and_bad_names_are_refused(env, name):
    client, reports, _ = env
    put(reports, "report.json", kind="baseline", created_at="2026-05-02T00:00:00+00:00", summary={})
    r = client.get(f"/backtests/reports/{name}")
    assert r.status_code in (400, 404, 405)
    assert "secret" not in r.text


def test_bad_name_is_a_400(env):
    client, _, _ = env
    assert client.get("/backtests/reports/report.txt").status_code == 400
    assert client.get("/backtests/reports/sp ace.json").status_code == 400
    assert client.get("/backtests/reports/..secret.json").status_code == 400


def test_symlink_escaping_the_directory_is_refused(env):
    client, reports, tmp = env
    try:
        os.symlink(tmp / "secret.json", reports / "link.json")
    except OSError:
        pytest.skip("symlinks unavailable")
    assert client.get("/backtests/reports/link.json").status_code == 400
    assert client.get("/backtests/reports").json() == []


def test_requires_auth():
    app = FastAPI()
    app.include_router(backtests.router)
    assert TestClient(app).get("/backtests/reports").status_code == 401


def test_stamped_baseline_report_is_listed(env):
    from datetime import date

    from app.backtest import report_io

    client, reports, _ = env
    body = {"split": "none", "config": {}, "universe": ["A"],
            "full": {"metrics": {"net_total_return": 0.1, "trades": 3}}}
    rep = report_io.envelope("baseline", "2026-05-01T00:00:00+00:00",
                             report_io.baseline_summary(body, date(2026, 1, 1), date(2026, 4, 1)), body)
    (reports / "b.json").write_text(report_io.dumps(rep))
    (entry,) = client.get("/backtests/reports").json()
    assert entry["kind"] == "baseline" and entry["summary"]["net_total_return"] == 0.1
