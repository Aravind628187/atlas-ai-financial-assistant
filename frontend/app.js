(() => {
  "use strict";

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const byId = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  async function fetchJson(url, timeoutMs = 9000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("Request unavailable");
      const payload = await response.json();
      if (!payload || typeof payload !== "object") throw new Error("Invalid response");
      return payload;
    } finally {
      window.clearTimeout(timer);
    }
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

  function freshnessLabel(quote, marketStatus) {
    if (!quote.available) return "Data unavailable";
    const freshness = String(quote.freshness || "").toLowerCase();
    if (freshness === "delayed") return "Delayed quote";
    if (freshness === "stale") return "Latest available quote";
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

  function renderMarket(payload) {
    const grid = byId("market-grid");
    const tape = byId("market-tape-track");
    const quotes = Array.isArray(payload.quotes) ? payload.quotes : [];
    const status = String(payload.market_status || "unknown").toLowerCase();
    const label = marketLabel(status);

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
          return `
            <article class="market-card">
              <div class="market-card-head"><strong>${escapeHtml(quote.symbol)}</strong><span>${escapeHtml(quote.name || quote.symbol)}</span></div>
              <div class="freshness-badge">${escapeHtml(freshnessLabel(quote, status))}</div>
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
    if (byId("market-note")) byId("market-note").textContent = formatTimestamp(payload.generated_at);
  }

  function renderCompanies(payload) {
    const grid = byId("companies-grid");
    const companies = Array.isArray(payload.companies) ? payload.companies : [];
    const live = Boolean(payload.is_live_ranking);
    if (byId("ranking-source")) {
      byId("ranking-source").innerHTML = `<span></span>${live ? "Live ranking · FMP" : "Reference ranking · not live"}`;
      byId("ranking-source").className = `ranking-source ${live ? "is-live" : "is-reference"}`;
    }
    if (byId("ranking-time")) byId("ranking-time").textContent = formatTimestamp(payload.retrieved_at);
    if (!grid) return;
    grid.setAttribute("aria-busy", "false");
    if (!companies.length) {
      grid.innerHTML = '<p class="data-error">Company ranking is temporarily unavailable.</p>';
      return;
    }
    grid.innerHTML = companies.map((company) => {
      const hasQuote = company.quote_available && finiteNumber(company.price) !== null;
      const move = movePresentation(company.change_pct);
      return `
        <article class="company-card">
          <span class="company-rank">${escapeHtml(company.rank)}</span>
          <div class="company-identity">
            <strong>${escapeHtml(company.symbol)}</strong>
            <span>${escapeHtml(company.name || company.symbol)}</span>
          </div>
          <div class="company-value">
            ${hasQuote ? `<b>${escapeHtml(formatPrice(company.price, company.currency))}</b><span class="${move.tone}">${escapeHtml(move.text)}</span>` : '<span class="quote-unavailable">Latest quote unavailable</span>'}
          </div>
        </article>`;
    }).join("");
  }

  function renderProviders(payload) {
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
  }

  function showSectionError(id, message) {
    const element = byId(id);
    if (!element) return;
    element.setAttribute("aria-busy", "false");
    element.innerHTML = `<p class="data-error">${escapeHtml(message)}</p>`;
  }

  async function loadPublicData() {
    const tasks = [
      ["system", "/api/public/system-summary", renderSystem],
      ["market", "/api/public/market-overview", renderMarket],
      ["companies", "/api/public/top-companies?limit=15", renderCompanies, 15000],
      ["providers", "/api/public/provider-summary", renderProviders],
    ];
    const results = await Promise.allSettled(tasks.map(([, url, , timeout]) => fetchJson(url, timeout)));
    results.forEach((result, index) => {
      const [name, , renderer] = tasks[index];
      if (result.status === "fulfilled") {
        renderer(result.value);
        return;
      }
      if (name === "market") showSectionError("market-grid", "Market data is temporarily unavailable.");
      if (name === "companies") showSectionError("companies-grid", "Company ranking is temporarily unavailable.");
      if (name === "providers") showSectionError("provider-list", "Provider status is temporarily unavailable.");
      if (name === "system") setTelegram({});
    });
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
    if (byId("market-grid")) loadPublicData();
  });
})();
