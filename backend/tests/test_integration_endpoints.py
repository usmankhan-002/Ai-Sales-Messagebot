"""Key-protected /v1/ai/rank-sales-message and /v1/ai/sms-summary.

These are the integration twins of the built-in page's /v1/ui/* routes, so a
server (e.g. the Foundry API) can call them with the API key instead of the
same-origin check.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import DEFAULT_RANKING_INDICATORS, app, get_client

client = TestClient(app)


class FakeOpenAI:
    """Answers every chat completion with `reply` and remembers the request."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))]
        )


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


def use_fake(reply: str) -> FakeOpenAI:
    fake = FakeOpenAI(reply)
    app.dependency_overrides[get_client] = lambda: fake
    return fake


AUTH = {"Authorization": "Bearer sekrit"}


# ---- rank ------------------------------------------------------------------ #

def test_rank_requires_the_key():
    use_fake("{}")
    r = client.post("/v1/ai/rank-sales-message", json={"message": "Hi"})
    assert r.status_code == 401


def test_rank_with_key_uses_default_indicators_when_none_sent():
    scores = {"indicators": [{"id": i["id"], "score": 4, "evidence": "ok"} for i in DEFAULT_RANKING_INDICATORS]}
    fake = use_fake(json.dumps(scores))
    r = client.post("/v1/ai/rank-sales-message", headers=AUTH, json={"message": "Hi Ada"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["indicators"]) == len(DEFAULT_RANKING_INDICATORS)
    assert body["overall_score"] == 4.0 and body["band"] == "green"
    sent = json.loads(fake.calls[0]["messages"][1]["content"])
    assert [i["id"] for i in sent["indicators"]] == [i["id"] for i in DEFAULT_RANKING_INDICATORS]


def test_rank_with_custom_indicators_and_band():
    fake = use_fake(json.dumps({"indicators": [{"id": "a", "score": 1, "evidence": "weak"}]}))
    r = client.post(
        "/v1/ai/rank-sales-message",
        headers={"X-API-Key": "sekrit"},
        json={"message": "Hi", "indicators": [{"id": "a", "name": "A", "guidance": "g"}]},
    )
    assert r.status_code == 200
    assert r.json()["band"] == "red"
    assert len(fake.calls) == 1


def test_rank_respects_the_org_blocklist(monkeypatch):
    monkeypatch.setattr(config.settings, "ai_org_blocklist", "org-9")
    fake = use_fake("{}")
    r = client.post(
        "/v1/ai/rank-sales-message",
        headers={**AUTH, "X-Org-Id": "org-9"},
        json={"message": "Hi"},
    )
    assert r.status_code == 403
    assert fake.calls == []


# ---- sms ------------------------------------------------------------------- #

def test_sms_requires_the_key():
    use_fake("x")
    r = client.post("/v1/ai/sms-summary", json={"sales_pitch": "p", "deal_link": "https://x.test/d"})
    assert r.status_code == 401


def test_sms_with_key_stays_within_200_chars_and_keeps_the_link():
    use_fake("word " * 100)
    link = "https://portal.foundryfx.org/deal/123"
    r = client.post("/v1/ai/sms-summary", headers=AUTH, json={"sales_pitch": "pitch", "deal_link": link})
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert len(msg) <= 200
    assert msg.endswith(link)


# ---- the page routes are unchanged ---------------------------------------- #

def test_ui_routes_still_reject_cross_origin_calls():
    use_fake("{}")
    r = client.post("/v1/ui/rank-sales-message", json={"message": "Hi"})
    assert r.status_code == 403
