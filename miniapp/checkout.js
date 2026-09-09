/* Reliable checkout transport shared by the storefront and offline regression tests. */
(function (root) {
  "use strict";
  function createClient({ fetch, read, write, timeoutMs = 20000 }) {
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
        if (!response.ok || !data.ok) throw new Error(data.error || "Не получилось принять заявку. Корзина сохранена.");
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
      async submit(payload, initData, owner = "") {
        if (!initData) throw new Error("Открой витрину из бота, чтобы оформить заказ. Корзина сохранена.");
        const key = `vorozhbitov_pending_checkout:${owner}`;
        const fingerprint = JSON.stringify(payload);
        const previous = read(key);
        const pending = previous && previous.fingerprint === fingerprint ? previous : {
          fingerprint,
          request_id: `web-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
        };
        write(key, pending);
        const result = await request("/api/checkout", { ...payload, request_id: pending.request_id }, initData);
        write(key, null);
        return result;
      },
      waitlist(payload, initData) { return request("/api/waitlist", payload, initData); }
    };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { createClient };
  else root.ShopCheckout = { createClient };
})(typeof window === "undefined" ? globalThis : window);
