(function (root) {
  "use strict";

  const MAX_QUANTITY = 20;

  function stringList(value) {
    return Array.isArray(value) ? [...new Set(value.filter(item => typeof item === "string" && item))] : [];
  }

  function cleanCart(value) {
    if (!Array.isArray(value)) return [];
    const lines = new Map();
    for (const row of value) {
      if (!row || typeof row.id !== "string" || !row.id || typeof row.size !== "string" || !row.size) continue;
      const quantity = Number(row.qty);
      if (!Number.isFinite(quantity) || quantity < 1) continue;
      const person = typeof row.person === "string" ? row.person : "";
      const key = `${row.id}::${row.size}${person ? `::${person}` : ""}`;
      const previous = lines.get(key);
      const qty = Math.min(MAX_QUANTITY, Math.floor(quantity) + (previous ? previous.qty : 0));
      lines.set(key, { key, id: row.id, size: row.size, qty, person });
    }
    return [...lines.values()].slice(0, 20);
  }

  function cleanProfile(value) {
    const fields = ["name", "phone", "city", "address", "entrance", "size", "height", "note"];
    const profile = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const result = Object.fromEntries(fields.map(key => [key, typeof profile[key] === "string" ? profile[key] : ""]));
    result.deliver = ["СДЭК", "Яндекс"].includes(profile.deliver) ? profile.deliver : "СДЭК";
    return result;
  }

  function priceNumber(value) {
    // Same whole-RUB contract as payments.parse_price_strict on the server.
    // Never strip punctuation blindly: 4 900,50 ₽ must not become 490050 ₽.
    const match = String(value ?? "").trim().match(/^(?:от\s+)?(\d{1,3}(?:[\s\u00a0\u202f']\d{3})*|\d+)(?:[.,](\d{1,2}))?\s*(?:₽|руб\.?|rub|r)?$/i);
    if (!match || (match[2] && Number(match[2]) !== 0)) return NaN;
    const result = Number(match[1].replace(/[\s\u00a0\u202f']/g, ""));
    return Number.isSafeInteger(result) && result >= 0 && result <= 10000000 ? result : NaN;
  }

  function buyable(product) {
    return product.active !== false && !product.gift && priceNumber(product.price) > 0;
  }

  function available(product) {
    return buyable(product) && stringList(product.sizes).length > 0;
  }

  function sizesFor(products) {
    const order = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "ОДИН"];
    const sizes = stringList(products.filter(buyable).flatMap(product => stringList(product.sizes)));
    return sizes.sort((a, b) => {
      const ia = order.indexOf(a), ib = order.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b, "ru");
    });
  }

  function productCount(count) {
    const tens = count % 100;
    const unit = count % 10;
    return `${String(count).padStart(2, "0")} ${tens >= 11 && tens <= 14 ? "ВЕЩЕЙ" : unit === 1 ? "ВЕЩЬ" : unit >= 2 && unit <= 4 ? "ВЕЩИ" : "ВЕЩЕЙ"}`;
  }

  const api = { MAX_QUANTITY, stringList, cleanCart, cleanProfile, priceNumber, buyable, available, sizesFor, productCount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ShopCore = api;
})(typeof window !== "undefined" ? window : globalThis);
