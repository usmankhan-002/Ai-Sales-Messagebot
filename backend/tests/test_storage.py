"""The bot's own database: organizations, clients, org settings, drafts,
rankings, usage log and retention.

Runs against a throwaway SQLite file (see conftest.py), never against Neon.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, db, storage
from app.main import DEFAULT_RANKING_INDICATORS, app, get_client
from app.models import AiDraft, AiRanking, AiUsageLog, Client

client = TestClient(app)
AUTH = {"Authorization": "Bearer sekrit"}
PAGE = {"Origin": "http://testserver"}


class FakeOpenAI:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


class FailingOpenAI:
    chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(RuntimeError("down"))))


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(config.settings, "api_token", "sekrit")
    monkeypatch.setattr(config.settings, "ai_enabled", True)
    monkeypatch.setattr(config.settings, "ai_org_allowlist", "")
    monkeypatch.setattr(config.settings, "ai_org_blocklist", "")
    monkeypatch.setattr(config.settings, "ai_allowed_roles", "")
    yield
    app.dependency_overrides.clear()


def use(fake):
    app.dependency_overrides[get_client] = lambda: fake
    return fake


def rows(model):
    with db.session_scope() as s:
        return s.scalars(select(model)).all()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def make_org(name="Foundry FX", headers=AUTH) -> dict:
    r = client.post("/v1/orgs", headers=headers, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def org_headers(org: dict, user: str | None = "user-1") -> dict:
    h = {**AUTH, "X-Org-Id": org["id"]}
    if user:
        h["X-User-Id"] = user
    return h


def make_client(headers, **fields) -> dict:
    r = client.post("/v1/clients", headers=headers, json={"full_name": "Ada Novik", "company": "Novik Trading", **fields})
    assert r.status_code == 201, r.text
    return r.json()


DRAFT = {"product": "FEC", "advisor_note": "Hedge."}


# ---- without a database ------------------------------------------------- #

def test_without_database_data_routes_return_503_and_drafting_still_works():
    r = client.get("/v1/orgs", headers=AUTH)
    assert r.status_code == 503 and "DATABASE_URL" in r.json()["detail"]
    use(FakeOpenAI("Hi Ada"))
    r = client.post("/v1/ai/sales-message", headers=AUTH, json=DRAFT)
    assert r.status_code == 200 and r.json()["message"] == "Hi Ada" and r.json()["draft_id"] is None


def test_health_reports_database_status(sqlite_db):
    assert client.get("/healthz").json()["database"] == "ok"


# ---- organizations ------------------------------------------------------ #

def test_orgs_create_list_rename(sqlite_db):
    assert client.post("/v1/orgs", json={"name": "X"}).status_code == 401
    org = make_org("Foundry FX")
    assert org["status"] == "active" and len(org["id"]) == 36
    r = client.patch(f"/v1/orgs/{org['id']}", headers=AUTH, json={"name": "Foundry FX AU"})
    assert r.status_code == 200 and r.json()["name"] == "Foundry FX AU"
    assert [o["name"] for o in client.get("/v1/orgs", headers=AUTH).json()["orgs"]] == ["Foundry FX AU"]
    assert client.patch(f"/v1/orgs/{org['id']}", headers=AUTH, json={"name": "  "}).status_code == 422


def test_org_header_must_be_a_known_org(sqlite_db):
    assert client.get("/v1/clients", headers=AUTH).status_code == 400
    assert client.get("/v1/clients", headers={**AUTH, "X-Org-Id": "not-a-uuid"}).status_code == 400
    assert client.get("/v1/clients", headers={**AUTH, "X-Org-Id": "00000000-0000-0000-0000-000000000001"}).status_code == 404


# ---- clients ------------------------------------------------------------ #

def test_clients_crud_and_isolated_per_org(sqlite_db):
    a, b = make_org("A"), make_org("B")
    ha, hb = org_headers(a), org_headers(b)
    c = make_client(ha, email="ada@example.com", risk_appetite="Balanced", functional_currency="AUD")
    assert (c["type"], c["created_by"], c["risk_appetite"]) == ("prospect", "user-1", "Balanced")
    assert [x["id"] for x in client.get("/v1/clients", headers=ha).json()["clients"]] == [c["id"]]
    assert client.get("/v1/clients", headers=hb).json()["clients"] == []
    # another org can't read, edit or delete it
    assert client.put(f"/v1/clients/{c['id']}", headers=hb, json={"notes": "x"}).status_code == 404
    assert client.delete(f"/v1/clients/{c['id']}", headers=hb).status_code == 404
    r = client.put(f"/v1/clients/{c['id']}", headers=ha, json={"notes": "Budget 0.72", "email": ""})
    assert r.status_code == 200 and r.json()["notes"] == "Budget 0.72" and r.json()["email"] is None
    assert client.delete(f"/v1/clients/{c['id']}", headers=ha).status_code == 204
    assert client.get("/v1/clients", headers=ha).json()["clients"] == []


def test_client_needs_name_or_company(sqlite_db):
    h = org_headers(make_org())
    assert client.post("/v1/clients", headers=h, json={"email": "x@example.com"}).status_code == 422
    c = make_client(h)
    r = client.put(f"/v1/clients/{c['id']}", headers=h, json={"full_name": "", "company": ""})
    assert r.status_code == 422


# ---- market commentary (fix 1: per organization) ------------------------ #

def test_commentary_saved_per_org_and_used_only_for_that_org(sqlite_db):
    a, b = make_org("A"), make_org("B")
    ha, hb = org_headers(a), org_headers(b)
    r = client.put("/v1/org/market-commentary", headers=ha, json={"text": "A: RBA on hold."})
    assert r.status_code == 200 and r.json()["date"] == today() and r.json()["updated_by"] == "user-1"
    client.put("/v1/org/market-commentary", headers=ha, json={"text": "A: AUD softer."})
    assert client.get("/v1/org/market-commentary", headers=ha).json()["text"] == "A: AUD softer."
    assert client.get("/v1/org/market-commentary", headers=hb).status_code == 404

    fake = use(FakeOpenAI("Draft"))
    client.post("/v1/ai/sales-message", headers=ha, json={**DRAFT, "use_market_commentary": True})
    client.post("/v1/ai/sales-message", headers=hb, json={**DRAFT, "use_market_commentary": True})
    assert "A: AUD softer." in fake.calls[0]["messages"][1]["content"]
    assert "A: AUD softer." not in fake.calls[1]["messages"][1]["content"]


def test_commentary_for_a_specific_date_and_toggle_off(sqlite_db):
    h = org_headers(make_org())
    client.put("/v1/org/market-commentary", headers=h, json={"date": "2026-01-02", "text": "Old."})
    assert client.get("/v1/org/market-commentary?date=2026-01-02", headers=h).json()["text"] == "Old."
    assert client.get("/v1/org/market-commentary", headers=h).status_code == 404
    client.put("/v1/org/market-commentary", headers=h, json={"text": "Today."})
    fake = use(FakeOpenAI("Draft"))
    client.post("/v1/ai/sales-message", headers=h, json=DRAFT)
    assert "Today." not in fake.calls[0]["messages"][1]["content"]


# ---- ranking indicators (fix 1: per organization) ----------------------- #

def test_indicators_per_org_and_used_by_rank(sqlite_db):
    a, b = make_org("A"), make_org("B")
    ha, hb = org_headers(a), org_headers(b)
    assert client.get("/v1/org/ranking-indicators", headers=ha).json()["source"] == "default"
    custom = [{"id": "close", "name": "Strong close", "guidance": "Ends with an ask."}]
    r = client.put("/v1/org/ranking-indicators", headers=ha, json={"indicators": custom})
    assert r.status_code == 200 and r.json()["source"] == "organization"
    fake = use(FakeOpenAI(json.dumps({"indicators": [{"id": "close", "score": 5, "evidence": "ok"}]})))
    r = client.post("/v1/ai/rank-sales-message", headers=ha, json={"message": "Hi"})
    assert [i["id"] for i in r.json()["indicators"]] == ["close"]
    assert json.loads(fake.calls[0]["messages"][1]["content"])["indicators"] == custom
    got_b = client.get("/v1/org/ranking-indicators", headers=hb).json()
    assert got_b["source"] == "default" and len(got_b["indicators"]) == len(DEFAULT_RANKING_INDICATORS)


def test_indicators_reject_duplicate_ids(sqlite_db):
    h = org_headers(make_org())
    dup = [{"id": "x", "name": "A", "guidance": "a"}, {"id": "x", "name": "B", "guidance": "b"}]
    assert client.put("/v1/org/ranking-indicators", headers=h, json={"indicators": dup}).status_code == 422


# ---- drafts, rankings (fix 3) and usage --------------------------------- #

def test_draft_ranking_and_usage_recorded_and_linked(sqlite_db):
    org = make_org()
    h = {**org_headers(org), "X-Deal-Id": "deal-42"}
    c = make_client(h)
    use(FakeOpenAI(json.dumps({"message": "Hi Ada", "subject": "Your FEC"})))
    r = client.post("/v1/ai/sales-message", headers=h, json={**DRAFT, "suggest_subject": True, "client_id": c["id"]})
    assert r.status_code == 200, r.text
    draft_id = r.json()["draft_id"]
    assert draft_id
    [draft] = rows(AiDraft)
    assert (str(draft.org_id), str(draft.client_id), draft.user_id, draft.deal_id) == (org["id"], c["id"], "user-1", "deal-42")
    assert (draft.kind, draft.message, draft.subject) == ("sales_message", "Hi Ada", "Your FEC")

    scores = {"indicators": [{"id": i["id"], "score": 4, "evidence": "ok"} for i in DEFAULT_RANKING_INDICATORS]}
    use(FakeOpenAI(json.dumps(scores)))
    r = client.post("/v1/ai/rank-sales-message", headers=h, json={"message": "Hi Ada", "draft_id": draft_id})
    assert r.status_code == 200 and r.json()["band"] == "green"
    [ranking] = rows(AiRanking)
    assert (ranking.draft_id, str(ranking.client_id), float(ranking.overall_score), ranking.band) == (draft_id, c["id"], 4.0, "green")
    assert len(ranking.results) == len(DEFAULT_RANKING_INDICATORS)

    listed = client.get("/v1/drafts?deal_id=deal-42", headers=h).json()["drafts"]
    assert listed[0]["message"] == "Hi Ada" and listed[0]["ranking"]["band"] == "green"
    assert client.get(f"/v1/drafts?client_id={c['id']}", headers=h).json()["drafts"][0]["id"] == draft_id
    summary = client.get("/v1/rankings/summary", headers=h).json()
    assert summary["rankings"] == 1 and summary["average_score"] == 4.0 and summary["bands"]["green"] == 1
    endpoints = sorted(u.endpoint for u in rows(AiUsageLog))
    assert endpoints == ["ai/rank-sales-message", "ai/sales-message"]


def test_ranking_never_links_to_another_orgs_draft(sqlite_db):
    a, b = make_org("A"), make_org("B")
    use(FakeOpenAI("Hi"))
    draft_id = client.post("/v1/ai/sales-message", headers=org_headers(a), json=DRAFT).json()["draft_id"]
    use(FakeOpenAI(json.dumps({"indicators": []})))
    client.post("/v1/ai/rank-sales-message", headers=org_headers(b), json={"message": "Hi", "draft_id": draft_id})
    [ranking] = rows(AiRanking)
    assert ranking.draft_id is None and str(ranking.org_id) == b["id"]


def test_client_from_another_org_is_not_attached(sqlite_db):
    a, b = make_org("A"), make_org("B")
    c = make_client(org_headers(a))
    use(FakeOpenAI("Hi"))
    client.post("/v1/ai/sales-message", headers=org_headers(b), json={**DRAFT, "client_id": c["id"]})
    [draft] = rows(AiDraft)
    assert draft.client_id is None


def test_unknown_org_header_still_drafts_but_files_no_org(sqlite_db):
    use(FakeOpenAI("Hi"))
    r = client.post("/v1/ai/sales-message", headers={**AUTH, "X-Org-Id": "org-from-elsewhere"}, json=DRAFT)
    assert r.status_code == 200
    [draft] = rows(AiDraft)
    assert draft.org_id is None


def test_failed_ai_call_logs_usage_but_saves_no_draft(sqlite_db):
    use(FailingOpenAI())
    r = client.post("/v1/ai/sales-message", headers=org_headers(make_org()), json=DRAFT)
    assert r.status_code == 502
    assert rows(AiDraft) == []
    [usage] = rows(AiUsageLog)
    assert usage.status_code == 502 and usage.error


def test_sms_summary_saved_as_draft(sqlite_db):
    h = org_headers(make_org())
    c = make_client(h)
    use(FakeOpenAI("Your AUD hedge is ready"))
    r = client.post("/v1/ai/sms-summary", headers=h,
                    json={"sales_pitch": "Long pitch", "deal_link": "https://x.example/d/1", "client_id": c["id"]})
    assert r.status_code == 200, r.text
    [draft] = rows(AiDraft)
    assert draft.kind == "sms_summary" and str(draft.client_id) == c["id"]


# ---- client data deletion (fix 2) --------------------------------------- #

def test_deleting_a_client_deletes_their_drafts_and_rankings(sqlite_db):
    h = org_headers(make_org())
    keep, gone = make_client(h, full_name="Keep"), make_client(h, full_name="Gone")
    use(FakeOpenAI("Hi"))
    ids = {}
    for c in (keep, gone):
        ids[c["id"]] = client.post("/v1/ai/sales-message", headers=h, json={**DRAFT, "client_id": c["id"]}).json()["draft_id"]
    use(FakeOpenAI(json.dumps({"indicators": []})))
    for c in (keep, gone):
        client.post("/v1/ai/rank-sales-message", headers=h, json={"message": "Hi", "draft_id": ids[c["id"]]})
    assert client.delete(f"/v1/clients/{gone['id']}", headers=h).status_code == 204
    assert [str(d.client_id) for d in rows(AiDraft)] == [keep["id"]]
    assert [str(r.client_id) for r in rows(AiRanking)] == [keep["id"]]
    assert [str(c.id) for c in rows(Client)] == [keep["id"]]


def test_retention_deletes_old_drafts_rankings_and_usage(sqlite_db, monkeypatch):
    monkeypatch.setattr(config.settings, "draft_retention_days", 90)
    monkeypatch.setattr(config.settings, "usage_retention_days", 365)
    h = org_headers(make_org())
    use(FakeOpenAI("Hi"))
    old_id = client.post("/v1/ai/sales-message", headers=h, json=DRAFT).json()["draft_id"]
    new_id = client.post("/v1/ai/sales-message", headers=h, json=DRAFT).json()["draft_id"]
    use(FakeOpenAI(json.dumps({"indicators": []})))
    client.post("/v1/ai/rank-sales-message", headers=h, json={"message": "Hi", "draft_id": old_id})
    with db.session_scope() as s:
        s.get(AiDraft, old_id).created_at = datetime.now(timezone.utc) - timedelta(days=91)
        for u in s.scalars(select(AiUsageLog)).all()[:1]:
            u.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    removed = storage.purge_expired()
    assert removed == {"drafts": 1, "rankings": 1, "usage": 1}
    assert [d.id for d in rows(AiDraft)] == [new_id]


def test_retention_zero_keeps_everything(sqlite_db, monkeypatch):
    monkeypatch.setattr(config.settings, "draft_retention_days", 0)
    monkeypatch.setattr(config.settings, "usage_retention_days", 0)
    h = org_headers(make_org())
    use(FakeOpenAI("Hi"))
    draft_id = client.post("/v1/ai/sales-message", headers=h, json=DRAFT).json()["draft_id"]
    with db.session_scope() as s:
        s.get(AiDraft, draft_id).created_at = datetime.now(timezone.utc) - timedelta(days=1000)
    assert storage.purge_expired() == {"drafts": 0, "rankings": 0, "usage": 0}


def test_database_outage_does_not_break_drafting(sqlite_db, monkeypatch):
    h = org_headers(make_org())

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(storage, "session_scope", boom)
    use(FakeOpenAI("Still works"))
    r = client.post("/v1/ai/sales-message", headers=h, json={**DRAFT, "use_market_commentary": True})
    assert r.status_code == 200 and r.json()["message"] == "Still works"
    assert client.get("/v1/org/market-commentary", headers=h).status_code == 503


# ---- built-in page routes ----------------------------------------------- #

def test_page_routes_work_per_org_without_a_key(sqlite_db):
    r = client.post("/v1/ui/orgs", headers=PAGE, json={"name": "Page Org"})
    assert r.status_code == 201, r.text
    ph = {**PAGE, "X-Org-Id": r.json()["id"], "X-User-Id": "spoofed"}
    c = client.post("/v1/ui/clients", headers=ph, json={"full_name": "Tom"}).json()
    assert c["created_by"] is None  # no login on the page, so no user is recorded
    assert client.put("/v1/ui/org/market-commentary", headers=ph, json={"text": "Page text."}).status_code == 200
    fake = use(FakeOpenAI("Draft"))
    r = client.post("/v1/ui/sales-message", headers=ph, json={**DRAFT, "use_market_commentary": True, "client_id": c["id"]})
    assert r.json()["draft_id"]
    assert "Page text." in fake.calls[0]["messages"][1]["content"]
    [draft] = rows(AiDraft)
    assert str(draft.client_id) == c["id"] and draft.user_id is None
    # the page can load its saved history; the reports summary stays integration-only
    assert [d["id"] for d in client.get(f"/v1/ui/drafts?client_id={c['id']}", headers=ph).json()["drafts"]] == [draft.id]
    assert client.get("/v1/ui/rankings/summary", headers=ph).status_code in (404, 405)


def test_page_routes_reject_cross_origin(sqlite_db):
    r = client.get("/v1/ui/orgs", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ---- regressions found in review ---------------------------------------- #

def test_rejected_client_update_saves_nothing(sqlite_db):
    h = org_headers(make_org())
    c = make_client(h, full_name="Ada Novik", company="Novik Trading", notes="keep")
    r = client.put(f"/v1/clients/{c['id']}", headers=h, json={"full_name": "", "company": "", "notes": "changed"})
    assert r.status_code == 422
    [stored] = client.get("/v1/clients", headers=h).json()["clients"]
    assert (stored["full_name"], stored["company"], stored["notes"]) == ("Ada Novik", "Novik Trading", "keep")


def test_postgres_engine_fails_fast_and_skips_prepared_statements(monkeypatch):
    monkeypatch.setattr(config.settings, "database_url", "postgresql://u:p@127.0.0.1:1/x?sslmode=disable")
    db.reset_engine()
    engine = db.get_engine()
    assert engine.dialect.driver == "psycopg"
    assert engine.pool.timeout() == 5
    import time
    started = time.perf_counter()
    assert db.db_status() == "error"
    storage.record_usage(org_id=None, user_id=None, endpoint="x", model=None, status_code=200, latency_ms=1)
    assert time.perf_counter() - started < 15
    db.reset_engine()


def test_cors_allows_every_method_the_api_uses():
    import inspect as _inspect

    import app.main as main_module

    allowed = _inspect.getsource(main_module).split("allow_methods=[")[1].split("]")[0]
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert f'"{method}"' in allowed


# ---- first-visit client import and draft history ------------------------ #

def test_client_import_only_fills_an_empty_org(sqlite_db):
    h = org_headers(make_org())
    batch = {"clients": [{"full_name": "Ada"}, {"company": "Harbour Wines"}, {"email": "no-name@example.com"}]}
    first = client.post("/v1/clients/import", headers=h, json=batch).json()
    assert first["imported"] == 2 and len(first["clients"]) == 2  # nameless row skipped
    again = client.post("/v1/clients/import", headers=h, json=batch).json()  # e.g. a second tab
    assert again["imported"] == 0 and len(again["clients"]) == 2
    assert len(client.get("/v1/clients", headers=h).json()["clients"]) == 2


def test_parallel_imports_never_duplicate(sqlite_db):
    from concurrent.futures import ThreadPoolExecutor
    h = org_headers(make_org())
    batch = {"clients": [{"full_name": "Ada"}, {"full_name": "Tom"}]}
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda _: client.post("/v1/clients/import", headers=h, json=batch), range(4)))
    assert sorted(x["full_name"] for x in client.get("/v1/clients", headers=h).json()["clients"]) == ["Ada", "Tom"]


def test_delete_saved_draft_is_org_scoped(sqlite_db):
    a, b = make_org("A"), make_org("B")
    use(FakeOpenAI("Hi"))
    draft_id = client.post("/v1/ai/sales-message", headers=org_headers(a), json=DRAFT).json()["draft_id"]
    use(FakeOpenAI(json.dumps({"indicators": []})))
    client.post("/v1/ai/rank-sales-message", headers=org_headers(a), json={"message": "Hi", "draft_id": draft_id})
    assert client.delete(f"/v1/drafts/{draft_id}", headers=org_headers(b)).status_code == 404
    assert client.delete(f"/v1/drafts/{draft_id}", headers=org_headers(a)).status_code == 204
    assert rows(AiDraft) == [] and rows(AiRanking) == []
