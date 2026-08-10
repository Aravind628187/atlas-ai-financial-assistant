# Atlas AI — Source-Grounded Financial Assistant for Telegram

Atlas is a finance-first Telegram assistant designed for source-grounded, high-reliability financial responses. It follows one execution rule:

> Verify first. Calculate deterministically. Generate last.

Atlas does not claim perfect accuracy. When a current financial fact cannot be verified—or configured providers materially disagree—it fails closed and says the value is unavailable rather than inventing one.

## Architecture

```text
Telegram message
  → deterministic command detection
  → validated intent and conservative ticker resolution
  → SQLite user context
  → typed FinancialDataGateway
      → intent-aware FinancialDataRouter
      → configured authoritative/professional providers
      → health-aware failover + critical-field verification
      → yfinance development/final fallback
      → TTL cache + freshness/session classification
      → official SEC EDGAR submissions provider for filings
  → deterministic formatter/calculator when possible
  → Gemini only for synthesis or general conversation
  → numerical claim validation for ungrounded financial generation
  → message + reliability telemetry
```

All direct yfinance access is isolated in `app/services/providers/yfinance_provider.py`. Application code consumes normalized results from `app/services/financial_data_gateway.py`.

## Implemented capabilities

- Latest available quotes with source, as-of timestamp, market-session semantics, and stale-data rejection
- Company profiles and provider-returned fundamentals only
- Retrieved company news with publisher and publication time; no inferred catalysts
- Provider-verified earnings dates when available
- Deterministic historical returns, observed highs/lows, and maximum drawdown for 1/3/6/12-month periods
- Conversational watchlist add/remove/show/clear with labels, duplicate prevention, and ticker validation where available
- Percentage-movement alert create/list/update/disable-all/re-enable with duplicate prevention
- Official SEC EDGAR 10-Q, 10-K, and 8-K filing lookup with filing dates and direct source links
- Multi-company comparison across valuation, market cap, revenue, growth, margins, and dividends, including contextual follow-ups
- Fully grounded morning briefings that omit unavailable sections
- Deterministic calculator library for percentage change, CAGR, interest, P/E, profit/loss, average cost, weighted return, drawdown, and position sizing. Conversational parsing currently covers trade profit/loss and simple percentage-gain requests.
- PDF/image document handling and document-scoped follow-up questions
- Brief/normal/detailed/beginner response preference storage
- General conversation through Gemini when available, without pretending to have unconfigured live sources
- Admin health, market-data health, data-quality, validator, and alert telemetry

## Providers and freshness

Supported providers:

- `finnhub`: configurable professional provider. Because account entitlements vary, Atlas conservatively labels results delayed/latest available unless explicit real-time entitlement metadata is added.
- `fmp`: fundamentals, statements-derived margins, profiles, earnings, quotes, and historical prices.
- `twelve_data`: quotes and daily historical series.
- `alpha_vantage`: company overview/fundamentals and daily historical fallback.
- `massive`: optional quote snapshot provider.
- `newsapi`: company and general market-news discovery.
- `SEC EDGAR`: authoritative 10-K, 10-Q, and 8-K metadata and source documents.
- `yfinance`: no-key development/fallback provider. It is never treated as infallible or automatically real-time.

The router selects a short provider chain by intent and stops at the first usable result. Critical quotes and equivalent-period fundamentals can be checked against one secondary provider. Material disagreement produces no definitive number. Repeated timeouts, 429s, and upstream failures temporarily remove an unhealthy provider from routing.

Provider ratio units are normalized explicitly. Atlas stores percentages as decimal fractions internally; yfinance dividend yield is derived from annual dividend divided by price because its raw yield field has had ambiguous semantics.

Freshness states are `live`, `delayed`, `latest_available`, `stale`, and `unavailable`. The current engine understands US weekday pre-market/open/after-hours/closed sessions. Exchange holiday truth remains dependent on provider metadata.

## Setup

```bash
cd /path/to/atlas-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Required for the full bot:

```dotenv
TELEGRAM_BOT_TOKEN=your_botfather_token
GEMINI_API_KEY=your_gemini_key
```

Required to enable Mission Control login (login fails closed when omitted):

```dotenv
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=a-long-unique-password
SECRET_KEY=a-long-random-session-signing-secret
```

Default development market data:

```dotenv
MARKET_DATA_PROVIDER=yfinance
MARKET_DATA_API_KEY=
```

Finnhub primary with yfinance fallback:

```dotenv
MARKET_DATA_PROVIDER=finnhub
FINNHUB_API_KEY=your_finnhub_key
SECONDARY_MARKET_DATA_PROVIDER=yfinance
SECONDARY_MARKET_DATA_API_KEY=
SEC_USER_AGENT=AtlasAI/1.0 your-email@example.com
```

Other optional keys are `FMP_API_KEY`, `TWELVE_DATA_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `NEWS_API_KEY`, and `MASSIVE_API_KEY`. Missing keys are reported as `not_configured` and skipped.

Never commit `.env`. `.env.example` contains placeholders only.

## Run

Run Telegram, scheduler, API, public frontend, and admin dashboard together:

```bash
python3 scripts/run_bot.py
```

Run only the API/dashboard:

```bash
uvicorn app.api.server:create_dashboard_app --factory --host 127.0.0.1 --port 8000
```

Public frontend: `http://127.0.0.1:8000/`

Admin login: `http://127.0.0.1:8000/login`

## Tests

The critical reliability suite uses in-memory SQLite and mocked providers; it does not require live market access:

```bash
python3 -m unittest discover -v
```

Additional syntax checks:

```bash
python3 -m compileall -q app tests scripts
node --check frontend/app.js
node --check dashboard/app.js
```

Read-only live provider check (may consume configured provider quota):

```bash
python3 scripts/check_financial_providers.py
```

Tests cover verified/unavailable quotes, adversarial exact-price requests, no-news catalyst behavior, concentration-risk safety, complete alert and watchlist lifecycles, contextual comparison follow-ups, historical drawdown, normalized SEC filings, deterministic calculations, provider timeouts/disagreement, Gemini quota handling, malformed intent output, and unsupported numeric-claim rejection.

## Health and observability

- `GET /api/health`: non-secret service configuration and database state
- `GET /api/health/market-data`: authenticated provider/cache health
- `GET /api/health/financial-data`: authenticated per-provider configuration, health, latency, fallback, and conflict state
- `GET /api/reliability`: authenticated data-quality, deterministic-response, validator, and alert metrics

All APIs containing users, conversations, briefings, or operational telemetry require the admin session cookie. Only login/logout and the non-secret aggregate health check are public.

Provider logs record request ID, provider, operation, symbol, status, latency, cache hit, and safe error type. Tokens, API keys, and admin passwords are never included.

## Accuracy and safety boundaries

- No-data-no-number is enforced in deterministic financial handlers.
- Current quotes are never supplied from Gemini model memory.
- News catalysts must map to retrieved `NewsItem` records.
- Arithmetic is performed by Python, not Gemini.
- Provider errors, rate limits, schema failures, and empty responses fail closed.
- Atlas offers analysis and education, not guaranteed returns or all-in personalized allocations.

Remaining limitations:

- Exchange holiday/session accuracy depends on provider metadata; the local fallback session engine handles weekdays and standard US hours only.
- Finnhub historical candles may require a paid entitlement, so historical requests fall through to yfinance when configured.
- SEC filing support currently retrieves official filing metadata and direct documents; full-text filing synthesis remains document-scoped so unsupported narrative claims cannot leak into answers.
- Numeric response validation is intentionally conservative and does not yet perform full semantic date/entity claim extraction.
- SQLite is suitable for a single-process deployment; multi-instance production should use Postgres and a shared cache.
- Google OAuth token exchange remains unimplemented and is reported as unavailable unless completed by the deployer.

Existing Telegram onboarding, conversations, voice/image/PDF handling, scheduler, watchlists, alerts, briefings, SQLite data, APIs, authentication, public frontend, and Mission Control remain in place.
