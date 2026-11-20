"""Reads and writes for the bot's tables.

Recording (usage log, drafts, rankings) is best-effort: if the database is not
configured or is unreachable, the AI response still goes back to the caller.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select

from .config import settings
from .db import get_engine, session_scope
from .models import (AiDraft, AiRanking, AiUsageLog, Client, Organization, OrgAiSettings,
                     OrgMarketCommentary, utcnow)

logger = logging.getLogger(__name__)

# API field name -> Client column. Kept in one place so the page, the API and
# the table can't drift apart.
CLIENT_FIELDS = {
    "full_name": "full_name", "company": "company", "email": "email", "phone": "phone",
    "type": "type", "industry": "industry", "risk_appetite": "risk_appetite",
    "hedging_horizon": "hedging_horizon", "functional_currency": "functional_currency", "notes": "notes",
}


def enabled() -> bool:
    return get_engine() is not None


def _iso(value):
    return value.isoformat() if value else None


def parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------- #
# Organizations                                                         #
# --------------------------------------------------------------------- #
def _org_dict(o: Organization) -> dict:
    return {"id": str(o.id), "name": o.name, "sector": o.sector, "status": o.status,
            "created_at": _iso(o.created_at), "updated_at": _iso(o.updated_at)}


def list_orgs() -> list[dict]:
    with session_scope() as s:
        return [_org_dict(o) for o in s.scalars(select(Organization).order_by(Organization.created_at, Organization.name))]


def create_org(name: str, sector: str | None) -> dict:
    with session_scope() as s:
        org = Organization(name=name, sector=sector)
        s.add(org)
        s.flush()
        return _org_dict(org)


def update_org(org_id: uuid.UUID, fields: dict) -> dict | None:
    with session_scope() as s:
        org = s.get(Organization, org_id)
        if org is None:
            return None
        for key in ("name", "sector"):
            if key in fields:
                setattr(org, key, fields[key])
        org.updated_at = utcnow()
        s.flush()
        return _org_dict(org)


def org_exists(org_id: uuid.UUID) -> bool:
    with session_scope() as s:
        return s.get(Organization, org_id) is not None


def try_resolve_org(raw: str | None) -> uuid.UUID | None:
    """An org id from a request header, only if it names a bot organization; never raises."""
    org_id = parse_uuid(raw)
    if not (org_id and enabled()):
        return None
    try:
        return org_id if org_exists(org_id) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not look up organization: %s", type(exc).__name__)
        return None


# --------------------------------------------------------------------- #
# Clients                                                               #
# --------------------------------------------------------------------- #
def _client_dict(c: Client) -> dict:
    out = {"id": str(c.id), "org_id": str(c.org_id)}
    out.update({api: getattr(c, col) for api, col in CLIENT_FIELDS.items()})
    out.update(created_by=c.created_by, created_at=_iso(c.created_at), updated_at=_iso(c.updated_at),
               deactivated_at=_iso(c.deactivated_at))
    return out


def list_clients(org_id: uuid.UUID, include_inactive: bool = False) -> list[dict]:
    with session_scope() as s:
        q = select(Client).where(Client.org_id == org_id)
        if not include_inactive:
            q = q.where(Client.deactivated_at.is_(None))
        return [_client_dict(c) for c in s.scalars(q.order_by(Client.created_at, Client.id))]


def create_client(org_id: uuid.UUID, fields: dict, user_id: str | None) -> dict:
    with session_scope() as s:
        c = Client(org_id=org_id, created_by=user_id, **{CLIENT_FIELDS[k]: v for k, v in fields.items() if k in CLIENT_FIELDS})
        if not c.type:
            c.type = "prospect"
        s.add(c)
        s.flush()
        return _client_dict(c)


_import_lock = threading.Lock()


def import_clients(org_id: uuid.UUID, rows: list[dict], user_id: str | None) -> dict:
    """Insert `rows` only if the org has no clients yet; one transaction, org row locked.

    The Postgres row lock serialises imports across server processes; the
    in-process lock also covers databases that ignore FOR UPDATE (SQLite).
    """
    with _import_lock, session_scope() as s:
        # Row lock on the org serialises concurrent imports (two tabs on first visit).
        s.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
        has_clients = s.scalar(select(func.count()).select_from(Client).where(Client.org_id == org_id)) or 0
        imported = 0
        if not has_clients:
            for fields in rows:
                c = Client(org_id=org_id, created_by=user_id,
                           **{CLIENT_FIELDS[k]: v for k, v in fields.items() if k in CLIENT_FIELDS})
                c.type = c.type or "prospect"
                s.add(c)
                imported += 1
            s.flush()
        clients = s.scalars(select(Client).where(Client.org_id == org_id, Client.deactivated_at.is_(None))
                            .order_by(Client.created_at, Client.id)).all()
        return {"imported": imported, "clients": [_client_dict(c) for c in clients]}


def delete_draft(org_id: uuid.UUID, draft_id: int) -> bool:
    with session_scope() as s:
        d = s.get(AiDraft, draft_id)
        if d is None or d.org_id != org_id:
            return False
        s.execute(delete(AiRanking).where(AiRanking.draft_id == draft_id))
        s.delete(d)
        return True


def update_client(org_id: uuid.UUID, client_id: uuid.UUID, fields: dict) -> dict | None:
    with session_scope() as s:
        c = s.get(Client, client_id)
        if c is None or c.org_id != org_id:
            return None
        merged = {api: getattr(c, col) for api, col in CLIENT_FIELDS.items()}
        merged.update({k: v for k, v in fields.items() if k in CLIENT_FIELDS})
        if not (merged.get("full_name") or merged.get("company")):
            raise ValueError("Add a contact name or company.")  # checked before anything is saved
        for k, v in fields.items():
            if k in CLIENT_FIELDS:
                setattr(c, CLIENT_FIELDS[k], v)
        if not c.type:
            c.type = "prospect"
        c.updated_at = utcnow()
        s.flush()
        return _client_dict(c)


def delete_client(org_id: uuid.UUID, client_id: uuid.UUID) -> bool:
    """Hard delete: the client's drafts and rankings go with it (erasure)."""
    with session_scope() as s:
        c = s.get(Client, client_id)
        if c is None or c.org_id != org_id:
            return False
        # Explicit, so it also holds on databases without ON DELETE CASCADE.
        s.execute(delete(AiRanking).where(AiRanking.client_id == client_id))
        s.execute(delete(AiDraft).where(AiDraft.client_id == client_id))
        s.delete(c)
        return True


def _client_in_org(s, org_id: uuid.UUID | None, client_id: uuid.UUID | None) -> uuid.UUID | None:
    if not (org_id and client_id):
        return None
    c = s.get(Client, client_id)
    return client_id if c is not None and c.org_id == org_id else None


# --------------------------------------------------------------------- #
# Best-effort recording                                                 #
# --------------------------------------------------------------------- #
def record_usage(*, org_id, user_id, endpoint, model, status_code, latency_ms, error=None) -> None:
    if not enabled():
        return
    try:
        with session_scope() as s:
            s.add(AiUsageLog(
                org_id=org_id, user_id=user_id, endpoint=endpoint, model=model,
                status_code=status_code, latency_ms=latency_ms,
                error=(error or None) and str(error)[:300],
            ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record AI usage: %s", type(exc).__name__)


def record_draft(*, org_id, client_id, user_id, deal_id, kind, message, product=None, advisor_note=None,
                 subject=None, alternative_message=None, request=None, model=None) -> int | None:
    if not enabled():
        return None
    try:
        with session_scope() as s:
            row = AiDraft(
                org_id=org_id, client_id=_client_in_org(s, org_id, client_id), user_id=user_id,
                deal_id=deal_id, kind=kind, product=product, advisor_note=advisor_note, message=message,
                subject=subject, alternative_message=alternative_message, request=request, model=model,
            )
            s.add(row)
            s.flush()
            return row.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record AI draft: %s", type(exc).__name__)
        return None


def record_ranking(*, org_id, draft_id, client_id, message, indicators, results, overall_score, band,
                   model) -> int | None:
    if not enabled():
        return None
    try:
        with session_scope() as s:
            draft = s.get(AiDraft, draft_id) if draft_id else None
            if draft is not None and draft.org_id != org_id:
                draft = None  # never link a ranking to another org's draft
            client = _client_in_org(s, org_id, client_id) or (draft.client_id if draft else None)
            row = AiRanking(
                org_id=org_id, draft_id=draft.id if draft else None, client_id=client, message=message,
                indicators=indicators, results=results, overall_score=Decimal(str(overall_score)),
                band=band, model=model,
            )
            s.add(row)
            s.flush()
            return row.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record AI ranking: %s", type(exc).__name__)
        return None


# --------------------------------------------------------------------- #
# Market commentary                                                     #
# --------------------------------------------------------------------- #
def _commentary_dict(row: OrgMarketCommentary) -> dict:
    return {"org_id": str(row.org_id), "date": row.for_date.isoformat(), "text": row.body,
            "updated_by": row.updated_by, "updated_at": _iso(row.updated_at)}


def get_commentary(org_id: uuid.UUID, day: date) -> dict | None:
    with session_scope() as s:
        row = s.get(OrgMarketCommentary, (org_id, day))
        return _commentary_dict(row) if row else None


def put_commentary(org_id: uuid.UUID, day: date, text: str, user_id: str | None) -> dict:
    with session_scope() as s:
        row = s.get(OrgMarketCommentary, (org_id, day))
        if row is None:
            row = OrgMarketCommentary(org_id=org_id, for_date=day, body=text, updated_by=user_id)
            s.add(row)
        else:
            row.body, row.updated_by, row.updated_at = text, user_id, utcnow()
        s.flush()
        return _commentary_dict(row)


def try_get_commentary_text(org_id: uuid.UUID | None, day: date) -> str | None:
    """Used to fill in commentary for a draft; never raises."""
    if not (enabled() and org_id):
        return None
    try:
        found = get_commentary(org_id, day)
        return found["text"] if found else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load market commentary: %s", type(exc).__name__)
        return None


# --------------------------------------------------------------------- #
# Ranking indicators (org_ai_settings)                                  #
# --------------------------------------------------------------------- #
def _settings_dict(row: OrgAiSettings) -> dict:
    return {"org_id": str(row.org_id), "indicators": row.ranking_indicators,
            "updated_by": row.updated_by, "updated_at": _iso(row.updated_at)}


def get_indicators(org_id: uuid.UUID) -> dict | None:
    with session_scope() as s:
        row = s.get(OrgAiSettings, org_id)
        if row is None or not row.ranking_indicators:
            return None
        return _settings_dict(row)


def put_indicators(org_id: uuid.UUID, indicators: list[dict], user_id: str | None) -> dict:
    with session_scope() as s:
        row = s.get(OrgAiSettings, org_id)
        if row is None:
            row = OrgAiSettings(org_id=org_id, ranking_indicators=indicators, updated_by=user_id)
            s.add(row)
        else:
            row.ranking_indicators, row.updated_by, row.updated_at = indicators, user_id, utcnow()
        s.flush()
        return _settings_dict(row)


def try_get_indicators(org_id: uuid.UUID | None) -> list[dict] | None:
    """Used to pick the org's rubric when a rank call sends none; never raises."""
    if not (enabled() and org_id):
        return None
    try:
        found = get_indicators(org_id)
        return found["indicators"] if found else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load ranking indicators: %s", type(exc).__name__)
        return None


# --------------------------------------------------------------------- #
# Draft history                                                         #
# --------------------------------------------------------------------- #
def list_drafts(org_id: uuid.UUID, *, deal_id: str | None, user_id: str | None,
                client_id: uuid.UUID | None, limit: int) -> list[dict]:
    with session_scope() as s:
        q = select(AiDraft).where(AiDraft.org_id == org_id)
        if deal_id:
            q = q.where(AiDraft.deal_id == deal_id)
        if user_id:
            q = q.where(AiDraft.user_id == user_id)
        if client_id:
            q = q.where(AiDraft.client_id == client_id)
        drafts = s.scalars(q.order_by(AiDraft.created_at.desc(), AiDraft.id.desc()).limit(limit)).all()
        latest: dict[int, AiRanking] = {}
        if drafts:
            for r in s.scalars(select(AiRanking).where(AiRanking.draft_id.in_([d.id for d in drafts]))
                               .order_by(AiRanking.created_at, AiRanking.id)):
                latest[r.draft_id] = r  # last one wins = most recent
        return [{
            "id": d.id, "org_id": str(d.org_id) if d.org_id else None,
            "client_id": str(d.client_id) if d.client_id else None,
            "user_id": d.user_id, "deal_id": d.deal_id, "kind": d.kind, "product": d.product,
            "advisor_note": d.advisor_note, "message": d.message, "subject": d.subject,
            "alternative_message": d.alternative_message, "model": d.model, "created_at": _iso(d.created_at),
            "ranking": ({"overall_score": float(latest[d.id].overall_score), "band": latest[d.id].band,
                         "ranked_at": _iso(latest[d.id].created_at)} if d.id in latest else None),
        } for d in drafts]


def ranking_summary(org_id: uuid.UUID, days: int) -> dict:
    """Message quality over the last `days` days, for managers."""
    since = utcnow() - timedelta(days=days)
    with session_scope() as s:
        base = select(AiRanking).where(AiRanking.org_id == org_id, AiRanking.created_at >= since)
        count = s.scalar(select(func.count()).select_from(base.subquery())) or 0
        avg = s.scalar(select(func.avg(AiRanking.overall_score)).where(
            AiRanking.org_id == org_id, AiRanking.created_at >= since))
        bands = dict(s.execute(select(AiRanking.band, func.count()).where(
            AiRanking.org_id == org_id, AiRanking.created_at >= since).group_by(AiRanking.band)).all())
        return {"org_id": str(org_id), "days": days, "rankings": count,
                "average_score": round(float(avg), 1) if avg is not None else None,
                "bands": {b: bands.get(b, 0) for b in ("green", "amber", "red")}}


# --------------------------------------------------------------------- #
# Retention                                                             #
# --------------------------------------------------------------------- #
def purge_expired() -> dict:
    """Delete drafts/rankings and usage rows older than their retention windows."""
    out = {"drafts": 0, "rankings": 0, "usage": 0}
    if not enabled():
        return out
    with session_scope() as s:
        if settings.draft_retention_days > 0:
            cutoff = utcnow() - timedelta(days=settings.draft_retention_days)
            old_drafts = select(AiDraft.id).where(AiDraft.created_at < cutoff)
            out["rankings"] = s.execute(delete(AiRanking).where(
                (AiRanking.created_at < cutoff) | AiRanking.draft_id.in_(old_drafts))).rowcount or 0
            out["drafts"] = s.execute(delete(AiDraft).where(AiDraft.created_at < cutoff)).rowcount or 0
        if settings.usage_retention_days > 0:
            cutoff = utcnow() - timedelta(days=settings.usage_retention_days)
            out["usage"] = s.execute(delete(AiUsageLog).where(AiUsageLog.created_at < cutoff)).rowcount or 0
    return out
