(function attachAtlasResilience(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AtlasResilience = api;
})(typeof window !== "undefined" ? window : globalThis, () => {
  "use strict";

  const DEFAULT_RETRY_DELAYS = Object.freeze([0, 2000, 5000, 10000]);
  const RETRYABLE_STATUSES = new Set([429, 502, 503, 504]);
  const SAFE_CACHE_SECTIONS = new Set(["market", "companies", "providers", "system"]);
  const FORBIDDEN_CACHE_KEYS = /(?:token|secret|password|api.?key|telegram.?id|user.?id|message|alert|watchlist|email)/i;

  class PublicFetchError extends Error {
    constructor(message, status = null, retryable = false) {
      super(message);
      this.name = "PublicFetchError";
      this.status = status;
      this.retryable = retryable;
    }
  }

  function sanitizePublicValue(value) {
    if (Array.isArray(value)) return value.map(sanitizePublicValue);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.entries(value)
      .filter(([key]) => !FORBIDDEN_CACHE_KEYS.test(key))
      .map(([key, nested]) => [key, sanitizePublicValue(nested)]));
  }

  function dataTimestamp(payload) {
    if (payload && payload.retrieved_at) return payload.retrieved_at;
    const quotes = payload && Array.isArray(payload.quotes) ? payload.quotes : [];
    const timestamps = quotes.map((quote) => quote.data_as_of).filter(Boolean).sort();
    return timestamps.length ? timestamps[timestamps.length - 1] : null;
  }

  function payloadSource(payload) {
    if (payload && payload.source) return payload.source;
    const quotes = payload && Array.isArray(payload.quotes) ? payload.quotes : [];
    const sources = [...new Set(quotes.map((quote) => quote.source).filter(Boolean))];
    return sources.length ? sources.join(", ") : null;
  }

  function createClient(options = {}) {
    const fetchImpl = options.fetchImpl || globalThis.fetch.bind(globalThis);
    const storage = options.storage || null;
    const now = options.now || (() => new Date());
    const sleep = options.sleep || ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
    const setTimer = options.setTimer || setTimeout;
    const clearTimer = options.clearTimer || clearTimeout;
    const AbortControllerImpl = options.AbortControllerImpl || globalThis.AbortController;
    const inFlight = new Map();
    const memoryCache = new Map();

    async function runRequest(url, requestOptions) {
      const delays = requestOptions.retryDelays || DEFAULT_RETRY_DELAYS;
      const timeoutMs = requestOptions.timeoutMs || 15000;
      let lastError = null;
      for (let attempt = 0; attempt < delays.length; attempt += 1) {
        if (attempt > 0) {
          if (requestOptions.onRetry) requestOptions.onRetry({ attempt: attempt + 1, delayMs: delays[attempt] });
          await sleep(delays[attempt]);
        }
        const controller = new AbortControllerImpl();
        const timer = setTimer(() => controller.abort(), timeoutMs);
        try {
          const response = await fetchImpl(url, {
            headers: { Accept: "application/json" },
            signal: controller.signal,
          });
          if (!response.ok) {
            throw new PublicFetchError(
              "Atlas public endpoint unavailable",
              response.status,
              RETRYABLE_STATUSES.has(response.status),
            );
          }
          let payload;
          try {
            payload = await response.json();
          } catch (_error) {
            throw new PublicFetchError("Invalid Atlas public response", response.status, false);
          }
          if (!payload || typeof payload !== "object") {
            throw new PublicFetchError("Invalid Atlas public response", response.status, false);
          }
          return payload;
        } catch (error) {
          const isAbort = error && error.name === "AbortError";
          const isNetwork = !(error instanceof PublicFetchError) && !isAbort;
          const retryable = isAbort || isNetwork || Boolean(error && error.retryable);
          lastError = error;
          if (!retryable || attempt === delays.length - 1) break;
        } finally {
          clearTimer(timer);
        }
      }
      throw lastError || new PublicFetchError("Atlas public endpoint unavailable");
    }

    function fetchWithRetry(url, requestOptions = {}) {
      if (inFlight.has(url)) return inFlight.get(url);
      const request = runRequest(url, requestOptions).finally(() => inFlight.delete(url));
      inFlight.set(url, request);
      return request;
    }

    function cachePayload(section, payload) {
      if (!SAFE_CACHE_SECTIONS.has(section) || !payload || typeof payload !== "object") return null;
      const safePayload = sanitizePublicValue(payload);
      const envelope = {
        payload: safePayload,
        retrieved_at: now().toISOString(),
        data_timestamp: dataTimestamp(safePayload),
        source: payloadSource(safePayload),
      };
      memoryCache.set(section, envelope);
      if (storage) {
        try {
          storage.setItem(`atlas.public.${section}`, JSON.stringify(envelope));
        } catch (_error) {
          // Browser memory remains available when storage is blocked or full.
        }
      }
      return envelope;
    }

    function getCachedPayload(section) {
      if (!SAFE_CACHE_SECTIONS.has(section)) return null;
      if (memoryCache.has(section)) return memoryCache.get(section);
      if (!storage) return null;
      try {
        const parsed = JSON.parse(storage.getItem(`atlas.public.${section}`) || "null");
        if (!parsed || !parsed.payload || typeof parsed.payload !== "object") return null;
        const safe = { ...parsed, payload: sanitizePublicValue(parsed.payload) };
        memoryCache.set(section, safe);
        return safe;
      } catch (_error) {
        return null;
      }
    }

    return {
      fetchWithRetry,
      cachePayload,
      getCachedPayload,
      activeRequestCount: () => inFlight.size,
    };
  }

  return {
    DEFAULT_RETRY_DELAYS,
    RETRYABLE_STATUSES,
    PublicFetchError,
    createClient,
    sanitizePublicValue,
  };
});
