"use strict";

const assert = require("node:assert/strict");
const { createClient, DEFAULT_RETRY_DELAYS } = require("../frontend/resilience.js");

function response(status, payload = { ok: true }) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
  };
}

async function run() {
  {
    let calls = 0;
    const client = createClient({ fetchImpl: async () => { calls += 1; return response(200, { value: 1 }); } });
    assert.deepEqual(await client.fetchWithRetry("/success"), { value: 1 });
    assert.equal(calls, 1, "initial success must not retry");
  }

  {
    let calls = 0;
    const client = createClient({
      fetchImpl: async () => {
        calls += 1;
        if (calls === 1) throw new TypeError("network unavailable");
        return response(200, { recovered: true });
      },
      sleep: async () => {},
    });
    assert.deepEqual(await client.fetchWithRetry("/network-recovery", { retryDelays: [0, 2] }), { recovered: true });
    assert.equal(calls, 2, "an initial network failure should recover on bounded retry");
  }

  for (const firstFailure of [429, 503]) {
    let calls = 0;
    const retries = [];
    const client = createClient({
      fetchImpl: async () => {
        calls += 1;
        return calls === 1 ? response(firstFailure) : response(200, { recovered: true });
      },
      sleep: async (delay) => retries.push(delay),
    });
    assert.deepEqual(await client.fetchWithRetry(`/retry-${firstFailure}`, {
      retryDelays: [0, 2], onRetry: (state) => retries.push(state.attempt),
    }), { recovered: true });
    assert.equal(calls, 2, `${firstFailure} should retry once before success`);
    assert.deepEqual(retries, [2, 2]);
  }

  {
    let calls = 0;
    const client = createClient({
      fetchImpl: async () => { calls += 1; throw new TypeError("network unavailable"); },
      sleep: async () => {},
    });
    await assert.rejects(client.fetchWithRetry("/bounded", { retryDelays: DEFAULT_RETRY_DELAYS }));
    assert.equal(calls, 4, "all failures must stop after the bounded retry schedule");
  }

  {
    let calls = 0;
    const client = createClient({ fetchImpl: async () => { calls += 1; return response(401); } });
    await assert.rejects(client.fetchWithRetry("/not-retryable"));
    assert.equal(calls, 1, "401 must not retry");
  }

  {
    const storage = memoryStorage();
    const client = createClient({ storage, fetchImpl: async () => response(503), sleep: async () => {} });
    client.cachePayload("market", {
      quotes: [{ symbol: "NVDA", price: 100, source: "finnhub", data_as_of: "2026-08-07T20:00:00Z" }],
      generated_at: "2026-08-10T10:00:00Z",
      api_key: "must-not-be-cached",
      telegram_id: 123,
    });
    await assert.rejects(client.fetchWithRetry("/cache-failure", { retryDelays: [0] }));
    const cached = client.getCachedPayload("market");
    assert.equal(cached.payload.quotes[0].price, 100);
    assert.equal(cached.data_timestamp, "2026-08-07T20:00:00Z");
    assert.equal(cached.source, "finnhub");
    assert.equal(cached.payload.api_key, undefined);
    assert.equal(cached.payload.telegram_id, undefined);
    assert.equal(client.cachePayload("private-user", { value: 1 }), null);
  }

  {
    let resolveFetch;
    let calls = 0;
    const pending = new Promise((resolve) => { resolveFetch = resolve; });
    const client = createClient({ fetchImpl: () => { calls += 1; return pending; } });
    const first = client.fetchWithRetry("/deduplicated");
    const second = client.fetchWithRetry("/deduplicated");
    assert.strictEqual(first, second, "concurrent requests for the same URL must share one promise");
    assert.equal(calls, 1);
    resolveFetch(response(200, { value: 2 }));
    assert.deepEqual(await first, { value: 2 });
  }

  {
    let aborted = false;
    const fetchImpl = async (_url, options) => {
      if (options.signal.aborted) {
        aborted = true;
        const error = new Error("aborted");
        error.name = "AbortError";
        throw error;
      }
      throw new Error("timer did not abort request");
    };
    const client = createClient({
      fetchImpl,
      setTimer: (callback) => { callback(); return 1; },
      clearTimer: () => {},
    });
    await assert.rejects(client.fetchWithRetry("/timeout", { retryDelays: [0], timeoutMs: 10 }));
    assert.equal(aborted, true, "timeout must abort the abandoned request");
  }
}

run().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exitCode = 1;
});
