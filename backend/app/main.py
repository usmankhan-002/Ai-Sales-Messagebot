import asyncio
import secrets
import uuid
import json
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field

from .import ai, storage
from .config import settings
from .db import db_status, init_db
from .schemas import SalesMessageRequest, SalesMessageResponse

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"
logger = logging.getLogger(__name__)


class SmsSummaryRequest(BaseModel):
    sales_pitch: str = Field(..., min_length=1, max_length=8000)
    deal_link: str = Field(..., min_length=1, max_length=1000)
    client_id: uuid.UUID | None = None  # bot client, to file the draft under


class SmsSummaryResponse(BaseModel):
    message: str


class RankingIndicator(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=120)
    guidance: str = Field(..., min_length=1, max_length=500)


# Used when a caller sends no indicators of its own (e.g. an org that has not
# configured any yet), so every integration gets the same baseline rubric.
DEFAULT_RANKING_INDICATORS: list[dict] = [
    {"id": "client-objectives", "name": "References client objectives", "guidance": "Connect the proposal to goals, priorities, or objectives explicitly present in the client context."},
    {"id": "strong-close", "name": "Has a strong close", "guidance": "End with a clear, appropriate close that invites a decision or response without overstating."},
    {"id": "clear-next-steps", "name": "Clear next steps", "guidance": "State a concrete next action for the advisor or client when supported by context."},
    {"id": "client-profile-alignment", "name": "Aligns to client profile", "guidance": "Tailor the wording to the supplied client profile, risk appetite, horizon, or industry where relevant."},
    {"id": "appropriate-urgency", "name": "Sense of urgency", "guidance": "Give a timely reason to act only when supported by supplied facts; do not create pressure or false urgency."},
    {"id": "trade-confirmation-link", "name": "Click link to confirm trade", "guidance": "Reference a trade confirmation or execution link only when one is actually supplied in the message or deal context."},
    {"id": "ongoing-engagement", "name": "Ongoing communication / engagement", "guidance": "Invite continued discussion, follow-up, or ongoing engagement in a way that fits the relationship."},
    {"id": "deal-parameter-alignment", "name": "Aligns to priced product parameters", "guidance": "Keep product description aligned with the supplied structure, currency pair, rates, levels, and other deal parameters; preserve them exactly."},
]


class RankMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    # Empty = use DEFAULT_RANKING_INDICATORS.
    indicators: list[RankingIndicator] = Field(default_factory=list, max_length=20)
    client_context: str | None = Field(default=None, max_length=4000)
    deal_context: str | None = Field(default=None, max_length=4000)
    # Optional links so the stored ranking can be tied to a saved draft/client.
    draft_id: int | None = Field(default=None, ge=1)
    client_id: uuid.UUID | None = None


class RankedIndicator(BaseModel):
    id: str
    score: int = Field(..., ge=0, le=5)
    evidence: str


class RankMessageResponse(BaseModel):
    indicators: list[RankedIndicator]
    overall_score: float
    band: str


def get_client() -> OpenAI:
    if not settings.key_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "OpenAI API key is not configured. Set OPENAI_API_KEY in the "
                "backend .env file, then restart the service."
            ),
        )
    return OpenAI(api_key=settings.openai_api_key)


def provider_http_error(exc: Exception) -> HTTPException:
    """Log upstream failures for operators without returning SDK internals to browsers."""
    logger.error("OpenAI request failed (%s)", type(exc).__name__, exc_info=True)
    error_name = type(exc).__name__
    if error_name == "APIConnectionError":
        detail = "Could not connect to OpenAI from this service. Check outbound network access and retry."
    elif error_name == "APITimeoutError":
        detail = "The OpenAI request timed out. Please retry."
    elif error_name == "AuthenticationError":
        detail = "OpenAI rejected the configured API key. Update OPENAI_API_KEY in the hosting environment."
    elif error_name == "RateLimitError":
        detail = "OpenAI rejected the request because of a rate or account limit. Check the OpenAI project limits and billing."
    else:
        detail = f"The OpenAI request failed ({error_name}). Check the service logs for details."
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail)


def require_token(request: Request) -> None:
    """Accept the key as `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    if not settings.auth_required:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:].strip() if header[:7].lower() == "bearer " else ""
    supplied = supplied or request.headers.get("x-api-key", "").strip()
    # Compare against every key without short-circuiting, in constant time.
    matched = False
    for token in settings.api_tokens:
        matched |= secrets.compare_digest(supplied.encode(), token.encode())
    if not supplied or not matched:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _org_id_from_request(request: Request) -> str | None:
    for header_name in (
        "x-org-id",
        "x-organization-id",
        "organization-id",
        "x-tenant-id",
        "tenant-id",
    ):
        value = request.headers.get(header_name, "").strip()
        if value:
            return value
    return None


def _header(request: Request, *names: str) -> str | None:
    for name in names:
        value = request.headers.get(name, "").strip()
        if value:
            return value[:128]
    return None


def _is_demo_page(request: Request) -> bool:
    return bool(getattr(request.state, "demo_page", False))


_UNRESOLVED = object()


def _storage_org_id(request: Request) -> uuid.UUID | None:
    """The bot organization named by X-Org-Id, if it exists in the bot database."""
    cached = getattr(request.state, "bot_org", _UNRESOLVED)
    if cached is _UNRESOLVED:
        cached = storage.try_resolve_org(_org_id_from_request(request))
        request.state.bot_org = cached
    return cached


def _user_id_from_request(request: Request) -> str | None:
    return None if _is_demo_page(request) else _header(request, "x-user-id")


def _deal_id_from_request(request: Request) -> str | None:
    return None if _is_demo_page(request) else _header(request, "x-deal-id")


def _role_from_request(request: Request) -> str | None:
    for header_name in ("x-user-role", "x-role", "role"):
        value = request.headers.get(header_name, "").strip()
        if value:
            return value.lower()
    return None


def require_ai_access(request: Request) -> None:
    """Allow a global AI off switch and organization-level allow/block checks."""
    if not settings.ai_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="AI access is disabled for this environment.",
        )

    org_id = _org_id_from_request(request)
    if settings.org_allowlist:
        if not org_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="AI access is restricted by organization. Supply X-Org-Id.",
            )
        if org_id not in settings.org_allowlist:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"Organization '{org_id}' is not allowed to use AI.",
            )
    if settings.org_blocklist and org_id and org_id in settings.org_blocklist:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"Organization '{org_id}' has AI disabled.",
        )

    if settings.allowed_roles:
        role = _role_from_request(request)
        if not role or role not in settings.allowed_roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="This user role is not permitted to use AI.",
            )


def _purge_once() -> None:
    try:
        removed = storage.purge_expired()
        if any(removed.values()):
            logger.info("Retention cleanup removed %s", removed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Retention cleanup failed: %s", type(exc).__name__)


async def _retention_loop() -> None:
    while True:
        await asyncio.to_thread(_purge_once)
        await asyncio.sleep(24 * 3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Creates the bot's own tables if they don't exist, then runs retention daily.
    task = None
    if init_db():
        logger.info("Database ready.")
        task = asyncio.create_task(_retention_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Foundry FX — AI Sales Message", version="0.1.0", lifespan=lifespan)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["POST", "GET", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "X-API-Key",
            "X-Org-Id",
            "X-User-Id",
            "X-User-Role",
            "X-Deal-Id",
            "Content-Type",
        ],
        max_age=600,
    )


@app.get("/healthz", tags=["meta"])
def healthz():
    return {
        "status": "ok",
        "key_configured": settings.key_configured,
        "auth_required": settings.auth_required,
        "ai_enabled": settings.ai_enabled,
        "database": db_status(),
        "org_allowlist": sorted(settings.org_allowlist),
        "org_blocklist": sorted(settings.org_blocklist),
        "allowed_roles": sorted(settings.allowed_roles),
    }


@app.post(
    "/v1/ai/sales-message",
    response_model=SalesMessageResponse,
    dependencies=[Depends(require_token), Depends(require_ai_access)],
    tags=["ai"],
)
def sales_message(
    req: SalesMessageRequest,
    request: Request,
    client: OpenAI = Depends(get_client),
):
    """Integration endpoint: requires the API key when AI_API_TOKEN is set.

    Optional headers X-Org-Id, X-User-Id and X-Deal-Id tie the saved draft
    and usage record to the Trade Builder's org, user and deal.
    """
    return _generate(req, client, request, "ai/sales-message")


# ---------------------------------------------------------------------- #
# Built-in web page endpoint: no API key, so the page works in any       #
# browser. Guarded instead by a same-origin check and per-IP and global  #
# rate limits, so it can't be used as a free general-purpose API.        #
# ---------------------------------------------------------------------- #
_ui_hits: dict[str, deque[float]] = defaultdict(deque)
_ui_all: deque[float] = deque()
_ui_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _allow(bucket: deque[float], limit: int, now: float) -> bool:
    while bucket and now - bucket[0] > settings.ui_rate_window_seconds:
        bucket.popleft()
    return len(bucket) < limit


def require_same_origin(request: Request) -> None:
    # Key-free page routes: there is no login, so no user ID is recorded.
    request.state.demo_page = True
    if not settings.ui_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found.")
    origin = request.headers.get("origin", "")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    same_origin = request.headers.get("sec-fetch-site") == "same-origin" or (
        bool(origin) and urlparse(origin).netloc == host
    )
    if not same_origin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="This endpoint is for the built-in web page. Integrations should use /v1/ai/sales-message with an API key.",
        )


def require_same_origin_and_rate_limit(request: Request) -> None:
    require_same_origin(request)
    now = time.monotonic()
    ip = _client_ip(request)
    with _ui_lock:
        mine = _ui_hits[ip]
        if not _allow(mine, settings.ui_rate_limit, now) or not _allow(
            _ui_all, settings.ui_rate_limit_global, now
        ):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many drafts in a short time. Please wait a few minutes and try again.",
                headers={"Retry-After": str(settings.ui_rate_window_seconds)},
            )
        mine.append(now)
        _ui_all.append(now)


@app.post(
    "/v1/ui/sales-message",
    response_model=SalesMessageResponse,
    dependencies=[Depends(require_same_origin_and_rate_limit), Depends(require_ai_access)],
    tags=["ui"],
)
def ui_sales_message(
    req: SalesMessageRequest,
    request: Request,
    client: OpenAI = Depends(get_client),
):
    return _generate(req, client, request, "ui/sales-message")


@app.post(
    "/v1/ui/sms-summary",
    response_model=SmsSummaryResponse,
    dependencies=[Depends(require_same_origin_and_rate_limit), Depends(require_ai_access)],
    tags=["ui"],
)
def ui_sms_summary(req: SmsSummaryRequest, request: Request, client: OpenAI = Depends(get_client)):
    return _sms_summary(req, client, request, "ui/sms-summary")


@app.post(
    "/v1/ai/sms-summary",
    response_model=SmsSummaryResponse,
    dependencies=[Depends(require_token), Depends(require_ai_access)],
    tags=["ai"],
)
def sms_summary(req: SmsSummaryRequest, request: Request, client: OpenAI = Depends(get_client)):
    """Integration endpoint: requires the API key when AI_API_TOKEN is set."""
    return _sms_summary(req, client, request, "ai/sms-summary")


def _sms_summary(req: SmsSummaryRequest, client: OpenAI, request: Request, endpoint: str) -> SmsSummaryResponse:
    result = _tracked(request, endpoint, lambda: _sms_summary_call(req, client))
    storage.record_draft(
        org_id=_storage_org_id(request), client_id=req.client_id, user_id=_user_id_from_request(request),
        deal_id=_deal_id_from_request(request), kind="sms_summary", message=result.message,
        request=req.model_dump(mode="json"), model=settings.model,
    )
    return result


def _sms_summary_call(req: SmsSummaryRequest, client: OpenAI) -> SmsSummaryResponse:
    link_suffix = f" Deal: {req.deal_link}"
    if len(link_suffix) > 200:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The deal link is too long for a 200-character SMS.")
    summary_budget = max(0, 200 - len(link_suffix))
    try:
        completion = client.chat.completions.create(
            model=settings.model,
            temperature=0.3,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize the provided client sales pitch as one concise, "
                        "client-ready SMS sentence. Use only facts in the pitch. "
                        f"Keep it within {summary_budget} characters. Output only the summary."
                    ),
                },
                {"role": "user", "content": req.sales_pitch},
            ],
        )
        summary = (completion.choices[0].message.content or "").strip()
        if len(summary) > summary_budget:
            summary = summary[:summary_budget].rsplit(" ", 1)[0]
        message = f"{summary.rstrip(' .')} {link_suffix.strip()}".strip()
        return SmsSummaryResponse(message=message[:200])
    except Exception as exc:
        raise provider_http_error(exc) from exc


@app.post(
    "/v1/ui/rank-sales-message",
    response_model=RankMessageResponse,
    dependencies=[Depends(require_same_origin_and_rate_limit), Depends(require_ai_access)],
    tags=["ui"],
)
def ui_rank_sales_message(req: RankMessageRequest, request: Request, client: OpenAI = Depends(get_client)):
    return _rank_message(req, client, request, "ui/rank-sales-message")


@app.post(
    "/v1/ai/rank-sales-message",
    response_model=RankMessageResponse,
    dependencies=[Depends(require_token), Depends(require_ai_access)],
    tags=["ai"],
)
def rank_sales_message(req: RankMessageRequest, request: Request, client: OpenAI = Depends(get_client)):
    """Integration endpoint: requires the API key when AI_API_TOKEN is set.

    With no indicators in the body, the org's saved rubric (X-Org-Id) is used,
    then the default rubric.
    """
    return _rank_message(req, client, request, "ai/rank-sales-message")


def _rank_message(req: RankMessageRequest, client: OpenAI, request: Request, endpoint: str) -> RankMessageResponse:
    if not req.indicators:
        saved = storage.try_get_indicators(_storage_org_id(request))
        try:
            indicators = [RankingIndicator(**item) for item in (saved or DEFAULT_RANKING_INDICATORS)]
        except Exception:  # noqa: BLE001 - a malformed saved rubric falls back to the default
            indicators = [RankingIndicator(**item) for item in DEFAULT_RANKING_INDICATORS]
        req = req.model_copy(update={"indicators": indicators})
    result = _tracked(request, endpoint, lambda: _rank_call(req, client))
    storage.record_ranking(
        org_id=_storage_org_id(request), draft_id=req.draft_id, client_id=req.client_id,
        message=req.message, indicators=[i.model_dump() for i in req.indicators],
        results=[r.model_dump() for r in result.indicators], overall_score=result.overall_score,
        band=result.band, model=settings.model,
    )
    return result


def _rank_call(req: RankMessageRequest, client: OpenAI) -> RankMessageResponse:
    if len({indicator.id for indicator in req.indicators}) != len(req.indicators):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Indicator IDs must be unique.")
    rubric = [{"id": item.id, "name": item.name, "guidance": item.guidance} for item in req.indicators]
    user_data = {
        "sales_message": req.message,
        "client_context": req.client_context or "Not provided",
        "deal_context": req.deal_context or "Not provided",
        "indicators": rubric,
    }
    try:
        completion = client.chat.completions.create(
            model=settings.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Evaluate a client-facing FX sales message against the supplied organization indicators. "
                        "Score each indicator from 0 to 5: 0 means absent or unsupported, 1 poor, 2 weak, "
                        "3 adequate, 4 strong, 5 excellent. Judge only evidence in the message and supplied "
                        "client/deal context. Never reward invented prices, levels, claims, urgency, or trade "
                        "confirmation links. For each item give one short evidence-based reason. Return one JSON "
                        'object with "indicators": [{"id": string, "score": integer, "evidence": string}] and '
                        'include every supplied indicator exactly once.'
                    ),
                },
                {"role": "user", "content": json.dumps(user_data, ensure_ascii=False)},
            ],
        )
        data = json.loads(completion.choices[0].message.content or "{}")
        raw = {str(item.get("id")): item for item in data.get("indicators", []) if isinstance(item, dict)}
        ranked = []
        for indicator in req.indicators:
            item = raw.get(indicator.id, {})
            try:
                score = max(0, min(5, int(item.get("score", 0))))
            except (TypeError, ValueError):
                score = 0
            evidence = str(item.get("evidence") or "No supporting evidence was identified.")[:300]
            ranked.append(RankedIndicator(id=indicator.id, score=score, evidence=evidence))
        overall = round(sum(row.score for row in ranked) / len(ranked), 1)
        band = "red" if overall < 2.3 else "amber" if overall < 3.8 else "green"
        return RankMessageResponse(indicators=ranked, overall_score=overall, band=band)
    except HTTPException:
        raise
    except Exception as exc:
        raise provider_http_error(exc) from exc


T = TypeVar("T")


def _tracked(request: Request, endpoint: str, call: Callable[[], T]) -> T:
    """Run an AI call and write one usage-log row, whatever the outcome."""
    started = time.perf_counter()
    code, error = 200, None
    try:
        return call()
    except HTTPException as exc:
        code, error = exc.status_code, str(exc.detail)
        raise
    except Exception as exc:  # noqa: BLE001
        code, error = 500, type(exc).__name__
        raise
    finally:
        storage.record_usage(
            org_id=_storage_org_id(request), user_id=_user_id_from_request(request),
            endpoint=endpoint, model=settings.model, status_code=code,
            latency_ms=int((time.perf_counter() - started) * 1000), error=error,
        )


def _generate(req: SalesMessageRequest, client: OpenAI, request: Request, endpoint: str) -> SalesMessageResponse:
    org_id = _storage_org_id(request)
    today = datetime.now(timezone.utc).date()
    # Advisor turned commentary on but the caller sent none: use the org's saved entry for today.
    if req.use_market_commentary and not (req.market_commentary or "").strip():
        saved = storage.try_get_commentary_text(org_id, today)
        if saved:
            req = req.model_copy(update={"market_commentary": saved, "market_commentary_date": today})
    result = _tracked(request, endpoint, lambda: _generate_call(req, client))
    draft_id = storage.record_draft(
        org_id=org_id, client_id=req.client_id, user_id=_user_id_from_request(request),
        deal_id=_deal_id_from_request(request), kind="sales_message", product=req.product,
        advisor_note=req.advisor_note, message=result.message, subject=result.subject,
        alternative_message=result.alternative_message, request=req.model_dump(mode="json"),
        model=result.model,
    )
    return result.model_copy(update={"draft_id": draft_id})


def _generate_call(req: SalesMessageRequest, client: OpenAI) -> SalesMessageResponse:
    try:
        result = ai.generate_sales_message(
            client, req, model=settings.model, temperature=settings.temperature
        )
    except ai.AiGenerationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:  # openai errors, network failures
        raise provider_http_error(exc) from exc

    return SalesMessageResponse(
        message=result.message,
        subject=result.subject,
        alternative_message=result.alternative_message,
        model=settings.model,
        generated_at=ai.now_iso(),
    )


# ---------------------------------------------------------------------- #
# Bot data: organizations, clients, commentary, ranking settings,        #
# draft history. Stored only in the bot's own database.                  #
# ---------------------------------------------------------------------- #
class OrgIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    sector: str | None = Field(default=None, max_length=200)


class OrgPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    sector: str | None = Field(default=None, max_length=200)


class ClientIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=300)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    type: str | None = Field(default=None, max_length=40)
    industry: str | None = Field(default=None, max_length=200)
    risk_appetite: str | None = Field(default=None, max_length=120)
    hedging_horizon: str | None = Field(default=None, max_length=120)
    functional_currency: str | None = Field(default=None, max_length=12)
    notes: str | None = Field(default=None, max_length=4000)


class ClientImportIn(BaseModel):
    clients: list[ClientIn] = Field(..., min_length=1, max_length=200)


class CommentaryIn(BaseModel):
    model_config = {"populate_by_name": True}
    # JSON field "date" (YYYY-MM-DD); defaults to today (UTC).
    commentary_date: date | None = Field(default=None, alias="date")
    text: str = Field(..., min_length=1, max_length=8000)


class IndicatorsIn(BaseModel):
    indicators: list[RankingIndicator] = Field(..., min_length=1, max_length=20)


def require_db() -> None:
    if not storage.enabled():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not configured. Set DATABASE_URL in the .env file, then restart the service.",
        )


def _db_call(fn: Callable[[], T]) -> T:
    try:
        return fn()
    except (HTTPException, ValueError):  # ValueError = input validation, not an outage
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Database request failed: %s", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="The database is unavailable. Try again shortly.") from exc


def require_org(request: Request) -> uuid.UUID:
    raw = _org_id_from_request(request)
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Supply X-Org-Id.")
    org_id = storage.parse_uuid(raw)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="X-Org-Id must be an organization id (UUID).")
    if not _db_call(lambda: storage.org_exists(org_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown organization.")
    return org_id


def _clean(model: BaseModel) -> dict:
    """Fields the caller sent, trimmed; blank strings become null."""
    out = {}
    for key, value in model.model_dump(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip() or None
        out[key] = value
    return out


def _today() -> date:
    return datetime.now(timezone.utc).date()


def build_data_router(read_deps: list, write_deps: list, *, reports: bool) -> APIRouter:
    r = APIRouter()

    # ---- organizations ----
    @r.get("/orgs", dependencies=read_deps)
    def list_orgs():
        return {"orgs": _db_call(storage.list_orgs)}

    @r.post("/orgs", dependencies=write_deps, status_code=status.HTTP_201_CREATED)
    def create_org(body: OrgIn):
        fields = _clean(body)
        if not fields.get("name"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Organization name is required.")
        return _db_call(lambda: storage.create_org(fields["name"], fields.get("sector")))

    @r.patch("/orgs/{org_id}", dependencies=write_deps)
    def update_org(org_id: uuid.UUID, body: OrgPatch):
        fields = _clean(body)
        if "name" in fields and not fields["name"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Organization name can't be blank.")
        found = _db_call(lambda: storage.update_org(org_id, fields))
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown organization.")
        return found

    # ---- clients (scoped by X-Org-Id) ----
    @r.get("/clients", dependencies=read_deps)
    def list_clients(request: Request, include_inactive: bool = False):
        org_id = require_org(request)
        return {"clients": _db_call(lambda: storage.list_clients(org_id, include_inactive))}

    @r.post("/clients", dependencies=write_deps, status_code=status.HTTP_201_CREATED)
    def create_client(body: ClientIn, request: Request):
        org_id = require_org(request)
        fields = _clean(body)
        if not (fields.get("full_name") or fields.get("company")):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Add a contact name or company.")
        return _db_call(lambda: storage.create_client(org_id, fields, _user_id_from_request(request)))

    @r.post("/clients/import", dependencies=write_deps)
    def import_clients(body: ClientImportIn, request: Request):
        """One-time upload of a browser's saved clients into an org that has none.

        Atomic: if the org already has clients (e.g. another tab got there first),
        nothing is imported and the existing clients are returned.
        """
        org_id = require_org(request)
        rows = [f for f in (_clean(c) for c in body.clients) if f.get("full_name") or f.get("company")]
        return _db_call(lambda: storage.import_clients(org_id, rows, _user_id_from_request(request)))

    @r.put("/clients/{client_id}", dependencies=write_deps)
    def update_client(client_id: uuid.UUID, body: ClientIn, request: Request):
        org_id = require_org(request)
        fields = _clean(body)
        try:
            found = _db_call(lambda: storage.update_client(org_id, client_id, fields))
        except ValueError as exc:  # validation failed; nothing was saved
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown client.")
        return found

    @r.delete("/clients/{client_id}", dependencies=write_deps, status_code=status.HTTP_204_NO_CONTENT)
    def delete_client(client_id: uuid.UUID, request: Request):
        """Deletes the client and every draft and ranking filed under them."""
        org_id = require_org(request)
        if not _db_call(lambda: storage.delete_client(org_id, client_id)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown client.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---- market commentary ----
    @r.get("/org/market-commentary", dependencies=read_deps)
    def get_market_commentary(request: Request, day: date | None = Query(default=None, alias="date")):
        """The org's market commentary for a date (default today, UTC)."""
        org_id = require_org(request)
        day = day or _today()
        found = _db_call(lambda: storage.get_commentary(org_id, day))
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No market commentary saved for {day.isoformat()}.")
        return found

    @r.put("/org/market-commentary", dependencies=write_deps)
    def put_market_commentary(body: CommentaryIn, request: Request):
        org_id = require_org(request)
        day = body.commentary_date or _today()
        return _db_call(lambda: storage.put_commentary(org_id, day, body.text.strip(), _user_id_from_request(request)))

    # ---- ranking indicators ----
    @r.get("/org/ranking-indicators", dependencies=read_deps)
    def get_ranking_indicators(request: Request):
        """The org's ranking rubric, or the default rubric if none is saved."""
        org_id = require_org(request)
        found = _db_call(lambda: storage.get_indicators(org_id))
        if found:
            return {**found, "source": "organization"}
        return {"org_id": str(org_id), "indicators": DEFAULT_RANKING_INDICATORS, "updated_by": None,
                "updated_at": None, "source": "default"}

    @r.put("/org/ranking-indicators", dependencies=write_deps)
    def put_ranking_indicators(body: IndicatorsIn, request: Request):
        org_id = require_org(request)
        if len({i.id for i in body.indicators}) != len(body.indicators):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Indicator IDs must be unique.")
        rows = [i.model_dump() for i in body.indicators]
        return {**_db_call(lambda: storage.put_indicators(org_id, rows, _user_id_from_request(request))),
                "source": "organization"}

    # ---- draft history ----
    @r.get("/drafts", dependencies=read_deps)
    def get_drafts(
        request: Request,
        deal_id: str | None = Query(default=None, max_length=128),
        user_id: str | None = Query(default=None, max_length=128),
        client_id: uuid.UUID | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ):
        """Draft history for the org, newest first, each with its latest ranking."""
        org_id = require_org(request)
        return {"drafts": _db_call(lambda: storage.list_drafts(
            org_id, deal_id=deal_id, user_id=user_id, client_id=client_id, limit=limit))}

    @r.delete("/drafts/{draft_id}", dependencies=write_deps, status_code=status.HTTP_204_NO_CONTENT)
    def delete_draft(draft_id: int, request: Request):
        """Deletes one saved draft (and its rankings) from the org's history."""
        org_id = require_org(request)
        if not _db_call(lambda: storage.delete_draft(org_id, draft_id)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown draft.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if reports:
        @r.get("/rankings/summary", dependencies=read_deps)
        def get_ranking_summary(request: Request, days: int = Query(default=30, ge=1, le=365)):
            """Average score and green/amber/red counts over the last `days` days."""
            org_id = require_org(request)
            return _db_call(lambda: storage.ranking_summary(org_id, days))

    return r


_KEYED = [Depends(require_token), Depends(require_db)]
_PAGE_READ = [Depends(require_same_origin), Depends(require_db)]
_PAGE_WRITE = [Depends(require_same_origin_and_rate_limit), Depends(require_db)]

# Integrations (API key) and the built-in page (same-origin, no login).
app.include_router(build_data_router(_KEYED, _KEYED, reports=True), prefix="/v1", tags=["bot data"])
app.include_router(build_data_router(_PAGE_READ, _PAGE_WRITE, reports=False), prefix="/v1/ui", tags=["ui"])


if DEMO_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(DEMO_DIR), html=True), name="demo")
