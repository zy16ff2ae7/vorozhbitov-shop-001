/* Reliable checkout transport shared by the storefront and offline regression tests. */
(function (root) {
  "use strict";
  function managerMessage({ customer = {}, lines = [], totalLabel = "", giftName = "" }) {
    const clean = (value, limit) => String(value ?? "").trim().slice(0, limit);
    const orderLines = lines.map((line, index) => {
      const name = clean(line.name, 120) || "Вещь";
      const size = clean(line.size, 24);
      const quantity = Math.max(1, Math.min(20, Math.floor(Number(line.quantity) || 1)));
      const price = clean(line.priceLabel, 40);
      const person = clean(line.person, 32);
      return `${index + 1}. ${name} · размер ${size || "уточнить"} · ${quantity} шт.${price ? ` · ${price}` : ""}${person ? ` · номер ${person}` : ""}`;
    });
    if (!orderLines.length) throw new Error("Корзина пуста.");
    const text = [
      "Здравствуйте! Хочу оформить заказ в СИЛА И ЧЕСТЬ:",
      ...orderLines,
      clean(giftName, 120) ? `Подарок: ${clean(giftName, 120)} · 0 ₽ (наличие уточняется)` : "",
      `Итого за вещи: ${clean(totalLabel, 40) || "уточнить"}`,
      `Имя: ${clean(customer.name, 80)}`,
      `Телефон: ${clean(customer.phone, 40)}`,
      `Город: ${clean(customer.city, 80)}`,
      `Адрес: ${clean(customer.address, 200) || "уточню в чате"}`,
      clean(customer.entrance, 80) ? `Подъезд / домофон: ${clean(customer.entrance, 80)}` : "",
      `Доставка: ${clean(customer.deliver, 40) || "уточнить"}`,
      clean(customer.note, 160) ? `Комментарий: ${clean(customer.note, 160)}` : "",
      "Прошу подтвердить наличие, стоимость доставки и заказ в ответ."
    ].filter(Boolean).join("\n");
    if (text.length > 3500) throw new Error("Состав слишком длинный для Telegram-ссылки. Раздели заказ на две корзины.");
    return text;
  }

  function managerUrl(username, text) {
    const account = String(username || "").trim().replace(/^@/, "");
    if (!/^[A-Za-z0-9_]{5,32}$/.test(account)) return "";
    const message = String(text || "");
    if (!message || message.length > 3500) return "";
    return `https://t.me/${account}?text=${encodeURIComponent(message)}`;
  }

  function createClient({ fetch, read, write, timeoutMs = 20000 }) {
    async function request(path, payload, initData) {
      if (!initData) throw new Error("Открой витрину из бота, чтобы оформить заказ.");
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
        if (!response.ok || !data.ok) {
          const error = new Error(data.error || "Не получилось принять заказ. Корзина сохранена.");
          error.code = data.code || "";
          throw error;
        }
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
      waitlist(payload, initData) { return request("/api/waitlist", payload, initData); },
      managerMessage,
      managerUrl
    };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { createClient, managerMessage, managerUrl };
  else root.ShopCheckout = { createClient, managerMessage, managerUrl };
})(typeof window === "undefined" ? globalThis : window);
