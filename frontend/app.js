(() => {
  "use strict";

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const byId = (id) => document.getElementById(id);
  let publicStorage = null;
  try { publicStorage = window.localStorage; } catch (_error) { publicStorage = null; }
  const resilienceClient = window.AtlasResilience
    ? window.AtlasResilience.createClient({ storage: publicStorage })
    : null;
  const sectionState = new Map();
  const slowRetryTimers = new Map();
  const refreshIntervals = {
    market: 60000,
    companies: 120000,
    providers: 180000,
    system: 300000,
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function formatPrice(value, currency = "USD") {
    const number = finiteNumber(value);
    if (number === null) return "Latest quote unavailable";
    try {
      return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: /^[A-Z]{3}$/.test(currency) ? currency : "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(number);
    } catch (_error) {
      return number.toFixed(2);
    }
  }

  function movePresentation(value) {
    const number = finiteNumber(value);
    if (number === null) return { text: "Change unavailable", tone: "neutral" };
    const sign = number > 0 ? "+" : "";
    return {
      text: `${sign}${number.toFixed(2)}%`,
      tone: number > 0 ? "positive" : number < 0 ? "negative" : "neutral",
    };
  }

  function sourceName(value) {
    const names = {
      fmp: "FMP",
      finnhub: "Finnhub",
      alpha_vantage: "Alpha Vantage",
      twelve_data: "Twelve Data",
      yfinance: "yfinance",
      sec: "SEC EDGAR",
      sec_edgar: "SEC EDGAR",
      newsapi: "NewsAPI",
    };
    return names[String(value || "").toLowerCase()] || String(value || "Source unavailable");
  }

  function marketLabel(status) {
    const labels = {
      open: "Market open",
      closed: "Market closed",
      pre_market: "Pre-market",
      after_hours: "After hours",
      unknown: "Market status unavailable",
    };
    return labels[String(status || "").toLowerCase()] || labels.unknown;
  }

  function freshnessLabel(quote, marketStatus, cached = false) {
    if (!quote.available) return "Data unavailable";
    if (cached) return "Last verified";
    const freshness = String(quote.freshness || "").toLowerCase();
    if (["live", "current", "real_time"].includes(freshness)) return "Live / current";
    if (freshness === "delayed") return "Latest available";
    if (freshness === "last_verified") return "Last verified";
    if (freshness === "stale") return "Stale";
    if (String(marketStatus).toLowerCase() === "open") return "Currently trading";
    if (String(marketStatus).toLowerCase() === "closed") return "Latest close";
    return "Latest available quote";
  }

  function formatTimestamp(value) {
    if (!value) return "Updated time unavailable";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Updated time unavailable";
    return `Updated ${new Intl.DateTimeFormat("en-US", {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(date)}`;
  }

  function formatDataTimestamp(value, prefix = "Latest verified session") {
    if (!value) return `${prefix}: unavailable`;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return `${prefix}: unavailable`;
    return `${prefix}: ${new Intl.DateTimeFormat("en-US", {
      month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
    }).format(date)}`;
  }

  function pageRefreshLabel() {
    return `Page refreshed: ${new Intl.DateTimeFormat("en-US", {
      hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short",
    }).format(new Date())}`;
  }

  function setTelegram(summary) {
    const links = document.querySelectorAll(".telegram-cta");
    const qrImages = document.querySelectorAll(".telegram-qr");
    const status = byId("telegram-status");
    const url = typeof summary.telegram_url === "string" ? summary.telegram_url : "";
    const valid = /^https:\/\/t\.me\/[A-Za-z0-9_]+\/?$/.test(url);
    links.forEach((link) => {
      if (valid) {
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.removeAttribute("aria-disabled");
      } else {
        link.href = "#telegram";
        link.removeAttribute("target");
        link.setAttribute("aria-disabled", "true");
      }
    });
    qrImages.forEach((qr) => {
      qr.hidden = !valid;
      if (valid && !qr.getAttribute("src")) qr.src = "/api/public/telegram-qr";
    });
    if (status) {
      status.textContent = valid && summary.telegram_username
        ? `Continue in Telegram with @${summary.telegram_username}.`
        : "The public Telegram link is not configured.";
    }
  }

  function renderSystem(summary) {
    const sources = finiteNumber(summary.sources_supported);
    const limit = finiteNumber(summary.max_watchlist_items);
    if (byId("source-count")) byId("source-count").textContent = sources === null ? "Multiple" : String(sources);
    if (byId("watchlist-limit")) byId("watchlist-limit").textContent = limit === null ? "Configured" : String(limit);
    if (byId("command-watchlist-limit")) {
      byId("command-watchlist-limit").textContent = limit === null ? "your configured limit" : `${limit} symbols`;
    }
    setTelegram(summary);
  }

  function unavailableCard(symbol, name) {
    return `
      <article class="market-card unavailable">
        <div class="market-card-head"><strong>${escapeHtml(symbol)}</strong><span>${escapeHtml(name || symbol)}</span></div>
        <p class="unavailable-copy">Data unavailable<br />No reliable quote is available right now.</p>
      </article>`;
  }

  function renderMarket(payload, context = {}) {
    const grid = byId("market-grid");
    const tape = byId("market-tape-track");
    const quotes = Array.isArray(payload.quotes) ? payload.quotes : [];
    const status = String(payload.market_status || "unknown").toLowerCase();
    const label = marketLabel(status);
    const hasLastVerified = context.cached || quotes.some((quote) => String(quote.freshness).toLowerCase() === "last_verified");

    ["hero-market-status", "tape-status"].forEach((id) => {
      if (byId(id)) byId(id).textContent = label;
    });
    const pill = byId("market-pill");
    if (pill) {
      const sessionLabel = label.replace(/^Market /, "");
      pill.innerHTML = `<span>US MARKET</span><b>${escapeHtml(sessionLabel)}</b>`;
      pill.className = `market-pill is-${status.replace(/_/g, "-")}`;
      pill.dataset.status = status;
    }

    if (grid) {
      grid.setAttribute("aria-busy", "false");
      if (!quotes.length) {
        grid.innerHTML = '<p class="data-error">Market data is temporarily unavailable.</p>';
      } else {
        grid.innerHTML = quotes.map((quote) => {
          if (!quote.available || finiteNumber(quote.price) === null) {
            return unavailableCard(quote.symbol, quote.name);
          }
          const move = movePresentation(quote.change_pct);
          const cachedQuote = context.cached || String(quote.freshness).toLowerCase() === "last_verified";
          return `
            <article class="market-card">
              <div class="market-card-head"><strong>${escapeHtml(quote.symbol)}</strong><span>${escapeHtml(quote.name || quote.symbol)}</span></div>
              <div class="freshness-badge">${escapeHtml(freshnessLabel(quote, status, cachedQuote))}</div>
              <p class="market-price">${escapeHtml(formatPrice(quote.price, quote.currency))}</p>
              <span class="market-move ${move.tone}">${escapeHtml(move.text)}</span>
              <div class="market-source"><span>${escapeHtml(sourceName(quote.source))}</span>${quote.verified_with ? `<span>Cross-checked · ${escapeHtml(sourceName(quote.verified_with))}</span>` : ""}</div>
            </article>`;
        }).join("");
      }
    }

    const available = quotes.filter((quote) => quote.available && finiteNumber(quote.price) !== null);
    if (tape) {
      tape.innerHTML = available.length
        ? available.map((quote) => {
            const move = movePresentation(quote.change_pct);
            return `<span class="ticker-item"><strong>${escapeHtml(quote.symbol)}</strong> ${escapeHtml(formatPrice(quote.price, quote.currency))} <em class="${move.tone}">${escapeHtml(move.text)}</em></span>`;
          }).join("")
        : '<span class="ticker-item">Reliable market data is temporarily unavailable.</span>';
    }
    const dataTimes = quotes.map((quote) => quote.data_as_of).filter(Boolean).sort();
    const latestDataTime = dataTimes.length ? dataTimes[dataTimes.length - 1] : null;
    if (byId("market-note")) {
      byId("market-note").textContent = hasLastVerified
        ? `${formatDataTimestamp(latestDataTime)} · Current refresh temporarily unavailable. ${pageRefreshLabel()}`
        : `${formatDataTimestamp(latestDataTime)} · ${pageRefreshLabel()}`;
    }
  }

  function renderCompanies(payload, context = {}) {
    const grid = byId("companies-grid");
    const companies = Array.isArray(payload.companies) ? payload.companies : [];
    const live = Boolean(payload.is_live_ranking);
    if (byId("ranking-source")) {
      const cachePrefix = context.cached ? "Last verified · " : "";
      byId("ranking-source").innerHTML = `<span></span>${cachePrefix}${live ? "Live ranking · FMP" : "Reference ranking · not live"}`;
      byId("ranking-source").className = `ranking-source ${live ? "is-live" : "is-reference"}`;
    }
    if (byId("ranking-time")) {
      byId("ranking-time").textContent = context.cached
        ? `${formatDataTimestamp(payload.retrieved_at, "Last verified ranking")} · Current refresh temporarily unavailable.`
        : `${formatDataTimestamp(payload.retrieved_at, "Ranking retrieved")} · ${pageRefreshLabel()}`;
    }
    if (!grid) return;
    grid.setAttribute("aria-busy", "false");
    if (!companies.length) {
      grid.innerHTML = '<p class="data-error">Company ranking is temporarily unavailable.</p>';
      return;
    }
    grid.innerHTML = companies.map((company) => {
      const hasQuote = company.quote_available && finiteNumber(company.price) !== null;
      const lastVerifiedQuote = context.cached || String(company.quote_freshness).toLowerCase() === "last_verified";
      const move = movePresentation(company.change_pct);
      return `
        <article class="company-card">
          <span class="company-rank">${escapeHtml(company.rank)}</span>
          <div class="company-identity">
            <strong>${escapeHtml(company.symbol)}</strong>
            <span>${escapeHtml(company.name || company.symbol)}</span>
          </div>
          <div class="company-value">
            ${hasQuote ? `<b>${escapeHtml(formatPrice(company.price, company.currency))}</b><span class="${move.tone}">${escapeHtml(move.text)}</span>${lastVerifiedQuote ? '<span class="quote-unavailable">Last verified</span>' : ""}` : '<span class="quote-unavailable">Price temporarily unavailable</span>'}
          </div>
        </article>`;
    }).join("");
  }

  function renderProviders(payload, context = {}) {
    const list = byId("provider-list");
    const providers = Array.isArray(payload.providers) ? payload.providers : [];
    if (!list) return;
    list.setAttribute("aria-busy", "false");
    if (!providers.length) {
      list.innerHTML = '<p class="data-error">Provider status is temporarily unavailable.</p>';
      return;
    }
    list.innerHTML = providers.map((provider) => {
      const state = String(provider.status || "Unavailable");
      const statusClass = state.toLowerCase().replace(/[^a-z]+/g, "-");
      return `
        <div class="provider-row">
          <b>${escapeHtml(provider.provider)}</b>
          <span>${escapeHtml(provider.role)}</span>
          <span class="provider-status ${escapeHtml(statusClass)}">${escapeHtml(state)}</span>
        </div>`;
    }).join("");
    if (byId("provider-refresh-status")) {
      byId("provider-refresh-status").textContent = context.cached ? "Showing last verified status" : "Updated just now";
    }
  }

  function showSectionError(id, message) {
    const element = byId(id);
    if (!element) return;
    element.setAttribute("aria-busy", "false");
    element.innerHTML = `<p class="data-error">${escapeHtml(message)}</p>`;
  }

  const publicSections = {
    system: { url: "/api/public/system-summary", renderer: renderSystem, timeoutMs: 15000 },
    market: { url: "/api/public/market-overview", renderer: renderMarket, timeoutMs: 15000 },
    companies: { url: "/api/public/top-companies?limit=15", renderer: renderCompanies, timeoutMs: 15000 },
    providers: { url: "/api/public/provider-summary", renderer: renderProviders, timeoutMs: 15000 },
  };

  function setSectionProgress(name, phase) {
    const messages = {
      connecting: "Connecting to Atlas data network...",
      waking: "Atlas is waking up and reconnecting to its data network...",
      retrying: "Retrying verified market data...",
    };
    const message = messages[phase] || messages.connecting;
    if (name === "market" && byId("market-note")) byId("market-note").textContent = message;
    if (name === "companies" && byId("ranking-time")) byId("ranking-time").textContent = message;
    if (name === "providers" && byId("provider-refresh-status")) byId("provider-refresh-status").textContent = message;
  }

  function renderCachedState(name) {
    if (!resilienceClient) return false;
    const cached = resilienceClient.getCachedPayload(name);
    if (!cached) return false;
    publicSections[name].renderer(cached.payload, { cached: true, cache: cached });
    if (name === "market" && byId("data-refresh-status")) byId("data-refresh-status").textContent = "Showing last verified data";
    return true;
  }

  function scheduleSlowRetry(name) {
    if (slowRetryTimers.has(name)) return;
    const timer = window.setTimeout(() => {
      slowRetryTimers.delete(name);
      if (document.hidden || !navigator.onLine) return;
      refreshSection(name);
    }, 60000);
    slowRetryTimers.set(name, timer);
  }

  function showFinalSectionFailure(name) {
    if (name === "market") {
      showSectionError("market-grid", "Market data is temporarily unavailable. Atlas will retry automatically.");
      if (byId("data-refresh-status")) byId("data-refresh-status").textContent = "Retrying automatically";
    }
    if (name === "companies") showSectionError("companies-grid", "Ranking temporarily unavailable. Retrying automatically...");
    if (name === "providers") showSectionError("provider-list", "Provider status is temporarily unavailable. Retrying automatically...");
    if (name === "system") setTelegram({});
  }

  async function refreshSection(name, options = {}) {
    const section = publicSections[name];
    if (!section || !resilienceClient) return false;
    if (!navigator.onLine) {
      renderCachedState(name);
      return false;
    }
    if (options.initial) setSectionProgress(name, "connecting");
    if (name === "market" && !options.initial && byId("data-refresh-status")) {
      byId("data-refresh-status").textContent = "Refreshing...";
    }
    try {
      const payload = await resilienceClient.fetchWithRetry(section.url, {
        timeoutMs: section.timeoutMs,
        onRetry: ({ attempt }) => setSectionProgress(name, attempt === 2 ? "waking" : "retrying"),
      });
      resilienceClient.cachePayload(name, payload);
      section.renderer(payload, { cached: false });
      sectionState.set(name, { lastSuccess: Date.now() });
      if (name === "market" && byId("data-refresh-status")) byId("data-refresh-status").textContent = "Updated just now";
      if (slowRetryTimers.has(name)) {
        window.clearTimeout(slowRetryTimers.get(name));
        slowRetryTimers.delete(name);
      }
      return true;
    } catch (_error) {
      const usedCache = renderCachedState(name);
      if (!usedCache) showFinalSectionFailure(name);
      scheduleSlowRetry(name);
      return false;
    }
  }

  async function refreshAll(names = Object.keys(publicSections), options = {}) {
    return Promise.allSettled(names.map((name) => refreshSection(name, options)));
  }

  async function manualRefresh() {
    const button = byId("refresh-data");
    const status = byId("data-refresh-status");
    if (button) button.disabled = true;
    if (status) status.textContent = "Refreshing...";
    const results = await refreshAll(["market", "companies", "providers"]);
    const successful = results.some((result) => result.status === "fulfilled" && result.value === true);
    if (status) status.textContent = successful ? "Updated just now" : "Showing last verified data · retrying automatically";
    if (button) button.disabled = false;
  }

  function setupPublicRefresh() {
    const button = byId("refresh-data");
    if (button) button.addEventListener("click", manualRefresh);
    Object.entries(refreshIntervals).forEach(([name, milliseconds]) => {
      window.setInterval(() => {
        if (!document.hidden && navigator.onLine) refreshSection(name);
      }, milliseconds);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden || !navigator.onLine) return;
      Object.entries(refreshIntervals).forEach(([name, staleAfter]) => {
        const state = sectionState.get(name);
        if (!state || Date.now() - state.lastSuccess >= staleAfter) refreshSection(name);
      });
    });
    window.addEventListener("offline", () => {
      const status = byId("connection-status");
      if (status) {
        status.textContent = "You're offline. Showing last verified data.";
        status.className = "is-offline";
      }
      Object.keys(publicSections).forEach(renderCachedState);
    });
    window.addEventListener("online", () => {
      const status = byId("connection-status");
      if (status) {
        status.textContent = "Back online · refreshing verified data...";
        status.className = "";
      }
      refreshAll();
    });
    refreshAll(Object.keys(publicSections), { initial: true });
  }

  function setupNavigation() {
    const toggle = byId("nav-toggle");
    const menu = byId("nav-links");
    if (toggle && menu) {
      toggle.addEventListener("click", () => {
        const open = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", String(!open));
        menu.classList.toggle("open", !open);
      });
      menu.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => {
        toggle.setAttribute("aria-expanded", "false");
        menu.classList.remove("open");
      }));
    }
    const header = document.querySelector(".site-header");
    if (header) {
      const update = () => header.classList.toggle("scrolled", window.scrollY > 12);
      update();
      window.addEventListener("scroll", update, { passive: true });
    }
  }

  function setupReveals() {
    const elements = document.querySelectorAll("[data-reveal]");
    if (!elements.length || reducedMotion || !("IntersectionObserver" in window)) {
      elements.forEach((element) => element.classList.add("is-visible"));
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    elements.forEach((element) => observer.observe(element));
  }

  function setupLoginPage() {
    const clocks = [byId("menubar-clock"), byId("lock-clock")].filter(Boolean);
    if (clocks.length) {
      const tick = () => {
        const now = new Date();
        const shortTime = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" }).format(now);
        clocks.forEach((clock) => { clock.textContent = shortTime; });
        const date = byId("lock-date");
        if (date) date.textContent = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric" }).format(now);
      };
      tick();
      window.setInterval(tick, 30000);
    }

    const toggle = byId("toggle-password");
    const password = byId("password-input");
    if (toggle && password) {
      toggle.addEventListener("click", () => {
        const showing = password.type === "text";
        password.type = showing ? "password" : "text";
        toggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
      });
    }

    const form = byId("login-form");
    if (!form) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = byId("login-status");
      const email = byId("email-input");
      const submit = form.querySelector('button[type="submit"]');
      if (error) error.textContent = "";
      if (submit) submit.disabled = true;
      try {
        const response = await fetch("/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: email ? email.value.trim() : "",
            password: password ? password.value : "",
          }),
        });
        if (!response.ok) throw new Error("Sign-in failed. Check the password and try again.");
        window.location.assign("/admin");
      } catch (requestError) {
        if (error) error.textContent = requestError.message;
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupNavigation();
    setupReveals();
    setupLoginPage();
    if (byId("market-grid")) setupPublicRefresh();
  });
})();
