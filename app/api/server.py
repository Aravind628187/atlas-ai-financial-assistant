"""
Read-only analytics API behind the Atlas dashboard.

The dashboard reads data from the same database written by the Telegram bot.
It does not communicate directly with Telegram or Gemini.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text

from app.config import settings

from app.database import get_session, init_db
from app.models import (
    Alert,
    BriefingLog,
    Document,
    Message,
    DataFetchLog,
    ResponseValidationLog,
    User,
    WatchlistItem,
)
from app.services.financial_data_gateway import gateway
from app.services.data_freshness import market_session_status
from app.services.providers.base import DataStatus, QuoteData
from app.services.runtime_state import runtime_state
from app.services.top_companies_service import top_companies_service


# ============================================================
# Helpers
# ============================================================

def utc_iso(value: dt.datetime | None) -> str | None:
    """
    Convert database timestamps to ISO-8601 UTC strings.

    The database currently stores naive UTC timestamps.
    Adding Z tells JavaScript that the timestamp is UTC so
    the browser can correctly convert it to the user's local timezone.
    """

    if value is None:
        return None

    # Already timezone-aware
    if value.tzinfo is not None:
        return value.astimezone(
            dt.timezone.utc
        ).isoformat().replace("+00:00", "Z")

    # Naive datetime is treated as UTC
    return value.isoformat() + "Z"


FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "frontend",
)

DASHBOARD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "dashboard",
)

SESSION_COOKIE = "atlas_admin_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24
_SESSION_SECRET = settings.secret_key.encode("utf-8") if settings.secret_key else secrets.token_bytes(32)
_PUBLIC_CACHE: dict[str, tuple[float, object]] = {}
_PUBLIC_CACHE_LOCK = threading.Lock()


def _public_cached(key: str, ttl_seconds: int, builder):
    now = time.monotonic()
    with _PUBLIC_CACHE_LOCK:
        cached = _PUBLIC_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
    value = builder()
    with _PUBLIC_CACHE_LOCK:
        _PUBLIC_CACHE[key] = (time.monotonic() + ttl_seconds, value)
    return value


def clear_public_cache() -> None:
    with _PUBLIC_CACHE_LOCK:
        _PUBLIC_CACHE.clear()


def _public_quote(symbol: str) -> dict:
    try:
        result = gateway.get_quote(symbol)
    except Exception:
        result = None
    quote = result.data if result else None
    usable = bool(
        isinstance(quote, QuoteData) and quote.price is not None
        and result.status not in {DataStatus.UNAVAILABLE, DataStatus.ERROR, DataStatus.STALE, DataStatus.CONFLICTING_DATA}
    )
    verification = result.verification or {} if result else {}
    return {
        "symbol": symbol,
        "name": quote.name if usable else None,
        "price": quote.price if usable else None,
        "currency": quote.currency if usable else None,
        "change_pct": quote.change_pct if usable else None,
        "source": result.source if usable else None,
        "verified_with": verification.get("secondary_source") if verification.get("verified_fields") else None,
        "data_as_of": utc_iso(result.data_as_of) if usable else None,
        "freshness": result.freshness if usable else "unavailable",
        "market_status": result.market_status if result else market_session_status().value,
        "available": usable,
    }


def _public_quotes(symbols: list[str] | tuple[str, ...]) -> list[dict]:
    """Resolve independent public quotes concurrently without browser fan-out."""
    if not symbols:
        return []
    with ThreadPoolExecutor(max_workers=min(6, len(symbols)), thread_name_prefix="public-quotes") as pool:
        return list(pool.map(_public_quote, symbols))


def _sign_value(value: str) -> str:
    return hmac.new(
        _SESSION_SECRET,
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_session_token(email: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{email}|{timestamp}"
    signature = _sign_value(payload)
    return f"{payload}|{signature}"


def validate_session_token(token: str) -> bool:
    try:
        email, timestamp, signature = token.split("|")
    except ValueError:
        return False

    if email != settings.admin_email:
        return False

    expected = _sign_value(f"{email}|{timestamp}")

    if not hmac.compare_digest(expected, signature):
        return False

    try:
        age = int(time.time()) - int(timestamp)
    except ValueError:
        return False

    return 0 <= age <= SESSION_MAX_AGE_SECONDS


def is_admin_authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    return bool(token and validate_session_token(token))


# ============================================================
# FastAPI application
# ============================================================

def create_dashboard_app() -> FastAPI:
    init_db()

    app = FastAPI(
        title="Atlas AI Dashboard API"
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def protect_admin_api(request: Request, call_next):
        """Prevent public access to user conversations and operational telemetry."""
        public_api_paths = {"/api/health", "/api/auth/login", "/api/auth/logout"}
        is_public_api = request.url.path.startswith("/api/public/")
        if (
            request.url.path.startswith("/api/")
            and request.url.path not in public_api_paths
            and not is_public_api
            and request.method != "OPTIONS"
            and not is_admin_authenticated(request)
        ):
            return JSONResponse({"detail": "Admin authentication required"}, status_code=401)
        return await call_next(request)

    @app.get("/")
    def public_home():
        index_path = os.path.join(FRONTEND_DIR, "index.html")
        if not os.path.exists(index_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(index_path)

    @app.get("/login")
    def login_page():
        login_path = os.path.join(FRONTEND_DIR, "login.html")
        if not os.path.exists(login_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(login_path)

    @app.get("/frontend/{resource_path:path}")
    def frontend_resource(resource_path: str):
        resource = os.path.realpath(os.path.join(FRONTEND_DIR, resource_path))
        if not resource.startswith(os.path.realpath(FRONTEND_DIR)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if not os.path.exists(resource):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(resource)

    @app.get("/admin")
    def admin_dashboard(request: Request):
        if not is_admin_authenticated(request):
            return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
        dashboard_index = os.path.join(DASHBOARD_DIR, "index.html")
        if not os.path.exists(dashboard_index):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(dashboard_index)

    @app.post("/api/auth/login")
    def api_login(payload: dict[str, str]):
        if not settings.admin_password or not settings.secret_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin login is disabled until ADMIN_PASSWORD and SECRET_KEY are configured",
            )
        if payload.get("email") != settings.admin_email or payload.get("password") != settings.admin_password:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

        token = make_session_token(payload["email"])
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            httponly=True,
            secure=False,  # set by the local-first deployment; terminate HTTPS at the reverse proxy
            samesite="lax",
            max_age=SESSION_MAX_AGE_SECONDS,
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    def api_logout():
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # ========================================================
    # Public product data — deliberately excludes all user data
    # ========================================================

    @app.get("/api/public/market-overview")
    def public_market_overview():
        def build():
            quotes = _public_quotes(("NVDA", "MSFT", "AAPL", "GOOGL"))
            session = next((item["market_status"] for item in quotes if item.get("available")), None)
            return {
                "market": "US",
                "market_status": session or market_session_status().value,
                "quotes": quotes,
                "generated_at": utc_iso(dt.datetime.now(dt.timezone.utc)),
            }
        return _public_cached("market-overview", 45, build)

    @app.get("/api/public/top-companies")
    def public_top_companies(limit: int = 15):
        safe_limit = max(1, min(limit, settings.max_watchlist_items))

        def build():
            ranking = top_companies_service.get_top(safe_limit)
            quotes = _public_quotes(tuple(company.symbol for company in ranking.companies))
            companies = []
            for rank, (company, quote) in enumerate(zip(ranking.companies, quotes), 1):
                companies.append({
                    "rank": rank,
                    "symbol": company.symbol,
                    "name": company.name,
                    "price": quote["price"],
                    "currency": quote["currency"],
                    "change_pct": quote["change_pct"],
                    "quote_source": quote["source"],
                    "quote_freshness": quote["freshness"],
                    "quote_available": quote["available"],
                })
            return {
                "source": "FMP" if ranking.source == "fmp" else "fallback_seed",
                "retrieved_at": utc_iso(ranking.retrieved_at),
                "is_live_ranking": ranking.is_live,
                "companies": companies,
            }
        return _public_cached(f"top-companies:{safe_limit}", 60, build)

    @app.get("/api/public/provider-summary")
    def public_provider_summary():
        health = gateway.health().get("router", {}).get("providers", {})
        definitions = (
            ("finnhub", "Finnhub", "Primary quotes"),
            ("twelve_data", "Twelve Data", "Quote verification · history"),
            ("fmp", "FMP", "Fundamentals · rankings"),
            ("alpha_vantage", "Alpha Vantage", "Fundamentals fallback"),
            ("newsapi", "NewsAPI", "Financial news"),
            ("sec_edgar", "SEC EDGAR", "Official filings"),
            ("yfinance", "yfinance", "Final market fallback"),
        )
        providers = []
        for key, label, role in definitions:
            state = health.get(key, {})
            configured = bool(state.get("configured", key == "yfinance"))
            raw_status = str(state.get("status", "degraded")).lower()
            if key == "yfinance":
                public_status = "Fallback"
            elif not configured:
                public_status = "Unavailable"
            elif raw_status == "ok":
                public_status = "Connected"
            elif raw_status in {"rate_limited", "degraded", "not_entitled"}:
                public_status = "Limited"
            else:
                public_status = "Unavailable"
            providers.append({"provider": label, "role": role, "status": public_status})
        return {"providers": providers, "generated_at": utc_iso(dt.datetime.now(dt.timezone.utc))}

    @app.get("/api/public/system-summary")
    def public_system_summary():
        username = settings.telegram_bot_username.strip().lstrip("@")
        return {
            "telegram_first": True,
            "telegram_username": username or None,
            "telegram_url": f"https://t.me/{username}" if username else None,
            "max_watchlist_items": settings.max_watchlist_items,
            "sources_supported": 7,
            "official_sec_filings": True,
            "ranking_cache_hours": 12,
        }

    @app.get("/api/public/telegram-qr")
    def public_telegram_qr():
        """Return a scannable QR for the configured public bot URL only."""
        username = settings.telegram_bot_username.strip().lstrip("@")
        if not username:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Public Telegram link is not configured")
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_Q,
            box_size=10,
            border=3,
        )
        qr.add_data(f"https://t.me/{username}")
        qr.make(fit=True)
        image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        output = BytesIO()
        image.save(output)
        return Response(
            output.getvalue(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # ========================================================
    # Overview
    # ========================================================

    @app.get("/api/overview")
    def overview():
        with get_session() as db:

            total_users = (
                db.scalar(
                    select(func.count(User.id))
                )
                or 0
            )

            done_users = (
                db.scalar(
                    select(func.count(User.id)).where(
                        User.onboarding_stage == "done"
                    )
                )
                or 0
            )

            since = (
                dt.datetime.utcnow()
                - dt.timedelta(hours=24)
            )

            messages_24h = (
                db.scalar(
                    select(func.count(Message.id)).where(
                        Message.created_at >= since
                    )
                )
                or 0
            )

            total_watchlist = (
                db.scalar(
                    select(
                        func.count(WatchlistItem.id)
                    )
                )
                or 0
            )

            active_alerts = (
                db.scalar(
                    select(func.count(Alert.id)).where(
                        Alert.active.is_(True)
                    )
                )
                or 0
            )

            documents_processed = (
                db.scalar(
                    select(func.count(Document.id))
                )
                or 0
            )

            briefings_sent = (
                db.scalar(
                    select(
                        func.count(BriefingLog.id)
                    )
                )
                or 0
            )

            onboarding_pct = (
                round(
                    (done_users / total_users) * 100,
                    1,
                )
                if total_users
                else 0
            )

            return {
                "total_users": total_users,
                "onboarded_users": done_users,
                "onboarding_completion_pct": onboarding_pct,
                "messages_last_24h": messages_24h,
                "watchlist_items": total_watchlist,
                "active_alerts": active_alerts,
                "documents_processed": documents_processed,
                "briefings_sent": briefings_sent,
            }


    # ========================================================
    # Popular symbols
    # ========================================================

    @app.get("/api/symbols/popular")
    def popular_symbols(limit: int = 10):
        with get_session() as db:

            rows = db.execute(
                select(
                    WatchlistItem.symbol,
                    func.count(
                        WatchlistItem.id
                    ).label("count"),
                )
                .group_by(
                    WatchlistItem.symbol
                )
                .order_by(
                    func.count(
                        WatchlistItem.id
                    ).desc()
                )
                .limit(limit)
            ).all()

            return [
                {
                    "symbol": row[0],
                    "count": row[1],
                }
                for row in rows
            ]


    # ========================================================
    # Message volume
    # ========================================================

    @app.get("/api/messages/volume")
    def message_volume(days: int = 7):
        with get_session() as db:

            # Keep a sensible range
            days = max(
                1,
                min(days, 365),
            )

            since = (
                dt.datetime.utcnow()
                - dt.timedelta(days=days)
            )

            rows = db.execute(
                select(
                    Message.created_at
                ).where(
                    Message.created_at >= since
                )
            ).all()

            buckets: dict[str, int] = {}

            for (created_at,) in rows:

                if not created_at:
                    continue

                key = created_at.strftime(
                    "%Y-%m-%d"
                )

                buckets[key] = (
                    buckets.get(key, 0) + 1
                )

            ordered = sorted(
                buckets.items()
            )

            return [
                {
                    "date": date,
                    "count": count,
                }
                for date, count in ordered
            ]


    # ========================================================
    # Users
    # ========================================================

    @app.get("/api/users")
    def list_users(limit: int = 50):
        with get_session() as db:

            limit = max(
                1,
                min(limit, 200),
            )

            users = (
                db.execute(
                    select(User)
                    .order_by(
                        User.last_active_at.desc()
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            output = []

            for user in users:

                message_count = (
                    db.scalar(
                        select(
                            func.count(Message.id)
                        ).where(
                            Message.user_id
                            == user.id
                        )
                    )
                    or 0
                )

                output.append(
                    {
                        "id": user.id,
                        "username": user.username,
                        "first_name": user.first_name,
                        "role": user.role,
                        "onboarding_stage":
                            user.onboarding_stage,
                        "message_count":
                            message_count,
                        "watchlist_size":
                            len(
                                user.watchlist_items
                            ),
                        "last_active_at":
                            utc_iso(
                                user.last_active_at
                            ),
                        "created_at":
                            utc_iso(
                                user.created_at
                            ),
                    }
                )

            return output


    # ========================================================
    # Recent messages
    # ========================================================

    @app.get("/api/messages/recent")
    def recent_messages(limit: int = 30):
        with get_session() as db:

            limit = max(
                1,
                min(limit, 200),
            )

            rows = (
                db.execute(
                    select(Message)
                    .order_by(
                        Message.created_at.desc()
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            return [
                {
                    "id": message.id,
                    "user_id":
                        message.user_id,
                    "role":
                        message.role,
                    "content":
                        (
                            message.content
                            or ""
                        )[:280],
                    "intent":
                        message.intent,
                    "input_kind":
                        message.input_kind,
                    "created_at":
                        utc_iso(
                            message.created_at
                        ),
                }
                for message in rows
            ]


    # ========================================================
    # Recent briefings
    # ========================================================

    @app.get("/api/briefings/recent")
    def recent_briefings(limit: int = 20):
        with get_session() as db:

            limit = max(
                1,
                min(limit, 100),
            )

            rows = (
                db.execute(
                    select(BriefingLog)
                    .order_by(
                        BriefingLog.created_at.desc()
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            return [
                {
                    "user_id":
                        briefing.user_id,
                    "kind":
                        briefing.kind,
                    "content":
                        briefing.content,
                    "created_at":
                        utc_iso(
                            briefing.created_at
                        ),
                }
                for briefing in rows
            ]


    # ========================================================
    # Health check
    # ========================================================

    @app.get("/api/health")
    def health():
        database_status = "ok"
        try:
            with get_session() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            database_status = "error"
        return {
            "status": "ok" if database_status == "ok" else "degraded",
            "service": "atlas-ai",
            "database": database_status,
            "telegram": "configured" if settings.telegram_bot_token else "not_configured",
            "gemini": "configured" if settings.gemini_api_key else "not_configured",
            "market_provider": getattr(gateway.primary, "name", "not_configured"),
            "scheduler": "running" if runtime_state.scheduler_running else "not_running",
        }

    @app.get("/api/health/market-data")
    def market_data_health(request: Request):
        if not is_admin_authenticated(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")
        return gateway.health()

    @app.get("/api/health/financial-data")
    def financial_data_health(request: Request):
        """Safe provider state; never includes credentials or request parameters."""
        if not is_admin_authenticated(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")
        health = gateway.health()
        router = health.get("router", {"overall": "degraded", "providers": {}})
        safe = {}
        for name, value in router.get("providers", {}).items():
            safe[name] = {
                "provider": value.get("provider", name),
                "configured": bool(value.get("configured")),
                "status": str(value.get("status", "error")).upper(),
                "latency_ms": value.get("latency_ms"),
                "last_success": value.get("last_success"),
                "last_failure": value.get("last_failure"),
                "failure_category": value.get("failure_category"),
            }
        return {"overall": str(router.get("overall", "degraded")).upper(), "providers": safe}

    @app.get("/api/reliability")
    def reliability(request: Request):
        if not is_admin_authenticated(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")
        with get_session() as db:
            since = dt.datetime.utcnow() - dt.timedelta(hours=24)
            fetches = db.scalar(select(func.count(DataFetchLog.id)).where(DataFetchLog.created_at >= since)) or 0
            failures = db.scalar(select(func.count(DataFetchLog.id)).where(
                DataFetchLog.created_at >= since,
                DataFetchLog.status.in_(["unavailable", "error", "conflicting_data"]),
            )) or 0
            stale = db.scalar(select(func.count(DataFetchLog.id)).where(
                DataFetchLog.created_at >= since, DataFetchLog.status == "stale"
            )) or 0
            blocked = db.scalar(select(func.count(ResponseValidationLog.id)).where(
                ResponseValidationLog.created_at >= since, ResponseValidationLog.result == "blocked"
            )) or 0
            deterministic = db.scalar(select(func.count(Message.id)).where(
                Message.created_at >= since,
                Message.role == "assistant",
                Message.intent.in_([
                    "market_quote", "market_move", "company_fundamentals", "historical_price",
                    "company_news", "watchlist_add", "watchlist_remove", "watchlist_show",
                    "alert_create", "alert_list", "alert_remove", "alert_update", "financial_calculation",
                ]),
            )) or 0
            active_alerts = db.scalar(select(func.count(Alert.id)).where(Alert.active.is_(True))) or 0
            triggered_alerts = db.scalar(select(func.count(Alert.id)).where(Alert.last_triggered_at.is_not(None))) or 0
        return {
            "data_quality": {"fetches_24h": fetches, "provider_errors_24h": failures, "stale_results_24h": stale},
            "ai_reliability": {"deterministic_responses_24h": deterministic, "responses_blocked_24h": blocked},
            "alert_activity": {
                "active_alerts": active_alerts, "triggered_alerts": triggered_alerts,
                "last_alert_check": utc_iso(runtime_state.last_alert_check),
            },
        }


    # ========================================================
    # Dashboard static files
    # IMPORTANT: Keep this LAST.
    # ========================================================

    if os.path.isdir(FRONTEND_DIR):
        app.mount(
            "/frontend",
            StaticFiles(directory=FRONTEND_DIR),
            name="frontend",
        )

    if os.path.isdir(
        DASHBOARD_DIR
    ):
        app.mount(
            "/",
            StaticFiles(
                directory=DASHBOARD_DIR,
                html=True,
            ),
            name="dashboard",
        )

    return app
