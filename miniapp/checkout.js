/* Reliable checkout transport shared by the storefront and offline regression tests. */
(function (root) {
  "use strict";
  function createClient({ fetch, read, write, timeoutMs = 20000 }) {
    const memory = new Map();
    const volatile = new Set();
    const readPending = key => {
      if (volatile.has(key)) return memory.get(key);
      try { const value = read(key) || null; memory.set(key, value); return value; } catch (_) { return memory.get(key); }
    };
    const writePending = (key, value) => {
      memory.set(key, value);
      try { write(key, value); volatile.delete(key); } catch (_) { volatile.add(key); }
    };
    async function request(path, payload, initData) {
      if (!initData) throw new Error("Открой витрину из бота, чтобы отправить заявку.");
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(path, {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json", "X-Telegram-Init-Data": initData },
          body: JSON.stringify(payload), signal: controller.signal
        });
        const data = await response.json();
        if (response.status === 401) throw new Error("Сессия истекла. Закрой и снова открой витрину из бота. Корзина сохранена.");
        if (!response.ok || !data.ok) throw Object.assign(new Error(data.error || "Не получилось принять заявку. Корзина сохранена."), { code: data.code });
        return data;
      } catch (error) {
        if (error.name === "AbortError" || error instanceof TypeError || error instanceof SyntaxError) {
          throw new Error("Нет подтверждения от сервера. Корзина сохранена — попробуй ещё раз.");
        }
        throw error;
      } finally {
        clearTimeout(timer);
      }
    }
    return {
      peek(owner = "") { return readPending(`vorozhbitov_pending_checkout:${owner}`); },
      async submit(payload, initData, owner = "", cartRevision) {
        if (!initData) throw new Error("Открой витрину из бота, чтобы оформить заказ. Корзина сохранена.");
        const key = `vorozhbitov_pending_checkout:${owner}`;
        const fingerprint = JSON.stringify(payload);
        // Capture once; no caller mutation can change the payload in flight.
        const snapshot = JSON.parse(fingerprint);
        const previous = readPending(key);
        const pending = previous && previous.fingerprint === fingerprint ? previous : {
          fingerprint,
          request_id: `web-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`,
          ...(Number.isSafeInteger(cartRevision) && cartRevision >= 0 ? { cart_revision: cartRevision } : {})
        };
        writePending(key, pending);
        let result;
        try {
          result = await request("/api/checkout", { ...snapshot,
            ...(Number.isSafeInteger(pending.cart_revision) ? { cart_revision: pending.cart_revision } : {}),
            request_id: pending.request_id }, initData);
        } catch (error) {
          if (((error.code === "cart_revision_conflict" && Number.isSafeInteger(pending.cart_revision))
              || ["promotion_requires_chat", "promotion_changed"].includes(error.code))
              && readPending(key)?.request_id === pending.request_id) writePending(key, null);
          throw error;
        }
        // An older response must not erase a newer, unresolved request.
        if (readPending(key)?.request_id === pending.request_id) writePending(key, null);
        return result;
      },
      waitlist(payload, initData) { return request("/api/waitlist", payload, initData); }
    };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { createClient };
  else root.ShopCheckout = { createClient };
})(typeof window === "undefined" ? globalThis : window);
