// ============================================================
// Atlas AI — Mission Control Dashboard
// Reads live data from the read-only /api/* endpoints.
// ============================================================

const API = "/api";

const el = (selector, root = document) => root.querySelector(selector);

const els = (selector, root = document) =>
  Array.from(root.querySelectorAll(selector));


// ============================================================
// CHART INSTANCES
// Keep references so we can destroy/re-render safely.
// ============================================================

let volumeChart = null;
let symbolsChart = null;


// ============================================================
// ROUTING / NAVIGATION
// ============================================================

function initNav() {
  els(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      // Remove active state from all navigation buttons
      els(".nav-item").forEach((item) => {
        item.classList.remove("is-active");
      });

      // Hide all views
      els(".view").forEach((view) => {
        view.classList.remove("is-active");
      });

      // Activate clicked navigation item
      btn.classList.add("is-active");

      // Show matching view
      const target = el(`#view-${btn.dataset.view}`);

      if (target) {
        target.classList.add("is-active");
      }
    });
  });
}


// ============================================================
// FETCH HELPERS
// ============================================================

async function getJSON(path) {
  try {
    const response = await fetch(`${API}${path}`, {
      cache: "no-store",
    });

    if (!response.ok) {
      throw new Error(
        `${response.status} ${response.statusText}`
      );
    }

    return await response.json();
  } catch (error) {
    console.warn(
      "Atlas dashboard: request failed",
      path,
      error
    );

    return null;
  }
}


// ============================================================
// DATE / TIME HELPERS
// ============================================================

function timeAgo(iso) {
  if (!iso) return "—";

  const date = new Date(iso);

  if (Number.isNaN(date.getTime())) {
    return "—";
  }

  const diffMs = Date.now() - date.getTime();

  const mins = Math.floor(diffMs / 60000);

  if (mins < 1) {
    return "just now";
  }

  if (mins < 60) {
    return `${mins}m ago`;
  }

  const hours = Math.floor(mins / 60);

  if (hours < 24) {
    return `${hours}h ago`;
  }

  const days = Math.floor(hours / 24);

  return `${days}d ago`;
}


function fmtTime(iso) {
  if (!iso) {
    return "--:--";
  }

  const date = new Date(iso);

  if (Number.isNaN(date.getTime())) {
    return "--:--";
  }

  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}


// ============================================================
// HTML ESCAPE
// ============================================================

function escapeHtml(value) {
  const div = document.createElement("div");

  div.textContent =
    value === null || value === undefined
      ? ""
      : String(value);

  return div.innerHTML;
}


// ============================================================
// OVERVIEW STATS
// ============================================================

async function loadOverview() {
  const stats = await getJSON("/overview");

  const container = el("#hero-stats");

  const status = el("#connection-status");

  if (!container) {
    return;
  }

  if (!stats) {
    if (status) {
      status.textContent = "waiting for backend…";
    }

    container.innerHTML = `
      <div class="stat">
        <span class="stat-value">—</span>
        <span class="stat-label">
          Start the server to see live stats
        </span>
      </div>
    `;

    return;
  }

  if (status) {
    status.textContent = "live";
  }

  container.innerHTML = `
    <div class="stat">
      <span class="stat-value">
        ${stats.total_users ?? 0}
      </span>
      <span class="stat-label">
        Users
      </span>
    </div>

    <div class="stat">
      <span class="stat-value">
        ${stats.onboarding_completion_pct ?? 0}%
      </span>
      <span class="stat-label">
        Onboarded
      </span>
    </div>

    <div class="stat">
      <span class="stat-value">
        ${stats.messages_last_24h ?? 0}
      </span>
      <span class="stat-label">
        Msgs / 24h
      </span>
    </div>

    <div class="stat">
      <span class="stat-value">
        ${stats.active_alerts ?? 0}
      </span>
      <span class="stat-label">
        Active alerts
      </span>
    </div>

    <div class="stat">
      <span class="stat-value">
        ${stats.briefings_sent ?? 0}
      </span>
      <span class="stat-label">
        Briefings sent
      </span>
    </div>
  `;
}


// ============================================================
// MESSAGE VOLUME CHART
// ============================================================

async function loadVolumeChart() {
  const data =
    (await getJSON("/messages/volume?days=7")) || [];

  const canvas = el("#chart-volume");

  if (!canvas) {
    return;
  }

  // Make sure Chart.js is loaded
  if (typeof Chart === "undefined") {
    console.error(
      "Chart.js is not loaded. Check dashboard/index.html."
    );

    return;
  }

  const labels = data.length
    ? data.map((item) => item.date.slice(5))
    : ["No data"];

  const counts = data.length
    ? data.map((item) => item.count)
    : [0];


  // Destroy previous chart before creating another one
  if (volumeChart) {
    volumeChart.destroy();
    volumeChart = null;
  }


  volumeChart = new Chart(canvas, {
    type: "line",

    data: {
      labels,

      datasets: [
        {
          label: "Messages",

          data: counts,

          borderColor: "#2F6FED",

          backgroundColor: (context) => {
            const chart = context.chart;

            const {
              ctx,
              chartArea,
            } = chart;

            if (!chartArea) {
              return "rgba(47,111,237,0.15)";
            }

            const gradient =
              ctx.createLinearGradient(
                0,
                chartArea.top,
                0,
                chartArea.bottom
              );

            gradient.addColorStop(
              0,
              "rgba(47,111,237,0.35)"
            );

            gradient.addColorStop(
              1,
              "rgba(47,111,237,0)"
            );

            return gradient;
          },

          fill: true,

          tension: 0.35,

          pointRadius: counts.length === 1 ? 5 : 3,

          pointHoverRadius: 6,

          pointBackgroundColor: "#2F6FED",

          borderWidth: 2,
        },
      ],
    },

    options: {
      responsive: true,

      maintainAspectRatio: false,

      interaction: {
        intersect: false,
        mode: "index",
      },

      plugins: {
        legend: {
          display: false,
        },

        tooltip: {
          callbacks: {
            label: (context) =>
              `${context.parsed.y} messages`,
          },
        },
      },

      scales: {
        x: {
          grid: {
            display: false,
          },

          ticks: {
            color: "#8792B0",

            font: {
              family: "JetBrains Mono",
              size: 10,
            },
          },
        },

        y: {
          beginAtZero: true,

          suggestedMax:
            Math.max(...counts, 0) + 2,

          grid: {
            color: "#212B4A",
          },

          ticks: {
            color: "#5A6483",

            precision: 0,

            stepSize: 1,

            font: {
              family: "JetBrains Mono",
              size: 10,
            },
          },
        },
      },
    },
  });
}


// ============================================================
// MOST TRACKED SYMBOLS CHART
// ============================================================

async function loadSymbolsChart() {
  const data =
    (await getJSON("/symbols/popular?limit=8")) || [];

  const canvas = el("#chart-symbols");

  if (!canvas) {
    return;
  }

  if (typeof Chart === "undefined") {
    console.error(
      "Chart.js is not loaded. Check dashboard/index.html."
    );

    return;
  }


  const labels = data.length
    ? data.map((item) => item.symbol)
    : ["No data"];

  const counts = data.length
    ? data.map((item) => item.count)
    : [0];


  if (symbolsChart) {
    symbolsChart.destroy();
    symbolsChart = null;
  }


  symbolsChart = new Chart(canvas, {
    type: "bar",

    data: {
      labels,

      datasets: [
        {
          label: "Users tracking",

          data: counts,

          backgroundColor: "#34D8A6",

          borderRadius: 6,

          borderSkipped: false,

          maxBarThickness: 35,
        },
      ],
    },

    options: {
      responsive: true,

      maintainAspectRatio: false,

      plugins: {
        legend: {
          display: false,
        },

        tooltip: {
          callbacks: {
            label: (context) =>
              `${context.parsed.y} tracked`,
          },
        },
      },

      scales: {
        x: {
          grid: {
            display: false,
          },

          ticks: {
            color: "#8792B0",

            font: {
              family: "JetBrains Mono",
              size: 11,
            },
          },
        },

        y: {
          beginAtZero: true,

          suggestedMax:
            Math.max(...counts, 0) + 1,

          grid: {
            color: "#212B4A",
          },

          ticks: {
            color: "#5A6483",

            precision: 0,

            stepSize: 1,

            font: {
              family: "JetBrains Mono",
              size: 10,
            },
          },
        },
      },
    },
  });


  // Update orbit
  renderOrbitSymbols(
    data
      .map((item) => item.symbol)
      .slice(0, 6)
  );


  // Update Watchlists page bars
  renderSymbolBars(data);
}


// ============================================================
// HERO ORBIT SYMBOLS
// ============================================================

function renderOrbitSymbols(symbols) {
  const group = el("#orbit-symbols");

  if (!group) {
    return;
  }

  group.innerHTML = "";

  if (!symbols.length) {
    return;
  }


  const radius = 190;

  const centerX = 210;

  const centerY = 210;


  symbols.forEach((symbol, index) => {
    const angle =
      (index / symbols.length) *
        2 *
        Math.PI -
      Math.PI / 2;


    const x =
      centerX +
      radius * Math.cos(angle);

    const y =
      centerY +
      radius * Math.sin(angle);


    // Create orbit dot
    const dot =
      document.createElementNS(
        "http://www.w3.org/2000/svg",
        "circle"
      );

    dot.setAttribute("cx", x);

    dot.setAttribute("cy", y);

    dot.setAttribute("r", "3.5");

    dot.setAttribute(
      "class",
      "orbit-symbol-dot"
    );


    // Create symbol label
    const label =
      document.createElementNS(
        "http://www.w3.org/2000/svg",
        "text"
      );

    label.setAttribute("x", x);

    label.setAttribute(
      "y",
      y - 10
    );

    label.setAttribute(
      "text-anchor",
      "middle"
    );

    label.setAttribute(
      "class",
      "orbit-symbol"
    );

    label.textContent = symbol;


    group.appendChild(dot);

    group.appendChild(label);
  });
}


// ============================================================
// WATCHLIST SYMBOL BARS
// ============================================================

function renderSymbolBars(data) {
  const container = el("#symbol-bars");

  const miniStats =
    el("#alert-mini-stats");


  if (!container) {
    return;
  }


  if (!data.length) {
    container.innerHTML = `
      <div class="feed-empty">
        No tickers tracked yet —
        add one from the bot to see it here.
      </div>
    `;


    if (miniStats) {
      miniStats.innerHTML = `
        <div class="mini-stat">
          <b>0</b>
          <span>symbols tracked</span>
        </div>

        <div class="mini-stat">
          <b>±5%</b>
          <span>default threshold</span>
        </div>
      `;
    }

    return;
  }


  const maxCount =
    Math.max(
      ...data.map(
        (item) => Number(item.count) || 0
      ),
      1
    );


  container.innerHTML = data
    .map((item) => {
      const count =
        Number(item.count) || 0;

      const width =
        (count / maxCount) * 100;

      return `
        <div class="bar-row">

          <span class="bar-sym">
            ${escapeHtml(item.symbol)}
          </span>

          <div class="bar-track">

            <div
              class="bar-fill"
              style="width:${width}%"
            ></div>

          </div>

          <span class="bar-count">
            ${count}
          </span>

        </div>
      `;
    })
    .join("");


  if (miniStats) {
    miniStats.innerHTML = `
      <div class="mini-stat">
        <b>${data.length}</b>
        <span>symbols tracked</span>
      </div>

      <div class="mini-stat">
        <b>±5%</b>
        <span>default threshold</span>
      </div>
    `;
  }
}


// ============================================================
// CONVERSATION FEED
// ============================================================

function feedRow(message) {
  const role =
    message.role || "unknown";

  const roleClass =
    role === "user"
      ? "user"
      : "assistant";


  return `
    <div class="feed-row">

      <span class="feed-time">
        ${fmtTime(message.created_at)}
      </span>

      <span class="feed-role ${roleClass}">
        ${escapeHtml(role)}
      </span>

      <span class="feed-content">
        ${escapeHtml(message.content || "")}
      </span>

      <span class="feed-intent">
        ${escapeHtml(message.intent || "")}
      </span>

    </div>
  `;
}


async function loadFeed(
  targetSelector,
  countSelector,
  limit
) {
  const data =
    (await getJSON(
      `/messages/recent?limit=${limit}`
    )) || [];


  const target =
    el(targetSelector);


  if (!target) {
    return;
  }


  if (countSelector) {
    const countElement =
      el(countSelector);

    if (countElement) {
      countElement.textContent =
        `${data.length} recent`;
    }
  }


  target.innerHTML =
    data.length
      ? data
          .map(feedRow)
          .join("")
      : `
        <div class="feed-empty">
          No conversations yet —
          start a conversation with Atlas on Telegram.
        </div>
      `;
}


// ============================================================
// USERS
// ============================================================

async function loadUsers() {
  const data =
    (await getJSON("/users?limit=50")) || [];


  const tbody =
    el("#users-table tbody");


  if (!tbody) {
    return;
  }


  if (!data.length) {
    tbody.innerHTML = `
      <tr>
        <td
          colspan="6"
          class="feed-empty"
        >
          No users yet.
        </td>
      </tr>
    `;

    return;
  }


  tbody.innerHTML = data
    .map((user) => {
      const name =
        user.first_name ||
        user.username ||
        `User #${user.id}`;


      const pillClass =
        user.onboarding_stage === "done"
          ? "done"
          : "progress";


      const pillLabel =
        user.onboarding_stage === "done"
          ? "Active"
          : "Onboarding";


      const username =
        user.username
          ? `
            <span
              style="color:var(--text-dim)"
            >
              @${escapeHtml(user.username)}
            </span>
          `
          : "";


      return `
        <tr>

          <td>
            ${escapeHtml(name)}
            ${username}
          </td>

          <td>
            ${escapeHtml(user.role || "—")}
          </td>

          <td>
            <span class="pill ${pillClass}">
              ${pillLabel}
            </span>
          </td>

          <td>
            ${user.message_count ?? 0}
          </td>

          <td>
            ${user.watchlist_size ?? 0}
          </td>

          <td>
            ${timeAgo(user.last_active_at)}
          </td>

        </tr>
      `;
    })
    .join("");
}


// ============================================================
// BRIEFINGS
// ============================================================

async function loadBriefings() {
  const data =
    (await getJSON(
      "/briefings/recent?limit=25"
    )) || [];


  const target =
    el("#feed-briefings");


  if (!target) {
    return;
  }


  if (!data.length) {
    target.innerHTML = `
      <div class="feed-empty">
        No briefings sent yet.
      </div>
    `;

    return;
  }


  target.innerHTML = data
    .map((briefing) => {
      const kind =
        briefing.kind
          ? briefing.kind.replaceAll(
              "_",
              " "
            )
          : "briefing";


      return `
        <div
          class="feed-row"
          style="
            grid-template-columns:
            70px 100px 1fr;
          "
        >

          <span class="feed-time">
            ${fmtTime(
              briefing.created_at
            )}
          </span>

          <span
            class="feed-role assistant"
          >
            ${escapeHtml(kind)}
          </span>

          <span class="feed-content">
            ${escapeHtml(
              briefing.content || ""
            )}
          </span>

        </div>
      `;
    })
    .join("");
}

function metric(label, value) {
  return `<div class="mini-stat"><b>${escapeHtml(value ?? "—")}</b><span>${escapeHtml(label)}</span></div>`;
}

async function loadReliability() {
  const [health, market, reliability] = await Promise.all([
    getJSON("/health"), getJSON("/health/market-data"), getJSON("/reliability")
  ]);
  const services = el("#health-services");
  const quality = el("#health-data-quality");
  const ai = el("#health-ai");
  const alerts = el("#health-alerts");
  const providers = el("#health-providers");
  if (services && health) services.innerHTML = [
    metric("Database", health.database), metric("Gemini", health.gemini),
    metric("Market provider", health.market_provider), metric("Scheduler", health.scheduler)
  ].join("");
  if (quality && reliability) quality.innerHTML = [
    metric("Fetches", reliability.data_quality.fetches_24h),
    metric("Provider errors", reliability.data_quality.provider_errors_24h),
    metric("Stale results", reliability.data_quality.stale_results_24h),
    metric("Cache entries", market?.cache?.active_entries ?? 0)
  ].join("");
  if (ai && reliability) ai.innerHTML = [
    metric("Deterministic", reliability.ai_reliability.deterministic_responses_24h),
    metric("Blocked", reliability.ai_reliability.responses_blocked_24h)
  ].join("");
  if (alerts && reliability) alerts.innerHTML = [
    metric("Active", reliability.alert_activity.active_alerts),
    metric("Triggered", reliability.alert_activity.triggered_alerts),
    metric("Last check", timeAgo(reliability.alert_activity.last_alert_check))
  ].join("");
  if (providers && market?.router?.providers) providers.innerHTML = Object.entries(market.router.providers)
    .map(([name, state]) => metric(name.replaceAll("_", " "), state.status))
    .join("");
}


// ============================================================
// LIVE DATA REFRESH
// ============================================================

async function refreshDashboard() {
  await Promise.all([
    loadOverview(),

    loadVolumeChart(),

    loadSymbolsChart(),

    loadFeed(
      "#feed-overview",
      "#feed-count",
      12
    ),

    loadFeed(
      "#feed-full",
      null,
      60
    ),

    loadUsers(),

    loadBriefings(),
    loadReliability(),
  ]);
}


// ============================================================
// APPLICATION START
// ============================================================

async function boot() {
  // Navigation should only be initialized once.
  initNav();

  // First dashboard load.
  await refreshDashboard();

  // Refresh live database information every 30 seconds.
  setInterval(
    refreshDashboard,
    30000
  );
}


// Wait until HTML is ready.
if (
  document.readyState === "loading"
) {
  document.addEventListener(
    "DOMContentLoaded",
    boot
  );
} else {
  boot();
}
