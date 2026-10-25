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

  // Published size guide (same ranges as the product sheet table).
  const SIZE_GUIDE = [
    { size: "S", height: [165, 174], weight: [60, 72], width: 57 },
    { size: "M", height: [172, 182], weight: [70, 82], width: 60 },
    { size: "L", height: [178, 188], weight: [80, 92], width: 63 },
    { size: "XL", height: [184, 194], weight: [90, 105], width: 66 },
    { size: "XXL", height: [188, 200], weight: [100, 120], width: 69 }
  ];

  function rangePenalty(value, range, scale) {
    if (!Number.isFinite(value)) return 0;
    const [low, high] = range;
    if (value < low) return (low - value) / scale;
    if (value > high) return (value - high) / scale;
    const middle = (low + high) / 2;
    return Math.abs(value - middle) / (scale * 8); // inside the range: prefer the centre
  }

  function recommendSize(input, availableSizes) {
    const height = Number(input && input.height);
    const weight = Number(input && input.weight);
    const fit = String((input && input.fit) || "regular");
    const sizes = stringList(availableSizes).map(size => size.toUpperCase());
    const guide = SIZE_GUIDE.filter(row => !sizes.length || sizes.includes(row.size));
    if (!guide.length || !Number.isFinite(height) || !Number.isFinite(weight)) return null;
    if (height < 130 || height > 230 || weight < 35 || weight > 220) return null;
    const scored = guide.map(row => ({
      size: row.size,
      score: rangePenalty(height, row.height, 6) + rangePenalty(weight, row.weight, 5)
    })).sort((a, b) => a.score - b.score);
    let index = guide.findIndex(row => row.size === scored[0].size);
    if (fit === "close") index = Math.max(0, index - 1);
    if (fit === "oversize") index = Math.min(guide.length - 1, index + 1);
    const chosen = guide[index];
    const rival = scored.find(row => row.size !== scored[0].size);
    const between = Boolean(rival && fit === "regular" && rival.score - scored[0].score < 0.35 && rival.size !== chosen.size);
    return {
      size: chosen.size,
      between: between ? rival.size : "",
      guide: chosen,
      confident: !between && scored[0].score < 1
    };
  }

  function dayNumber(isoDate) {
    const match = String(isoDate || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) return null;
    const utc = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    return Number.isFinite(utc) ? Math.floor(utc / 86400000) : null;
  }

  function clubStreak(dates, todayISO) {
    const days = new Set((Array.isArray(dates) ? dates : []).map(dayNumber).filter(day => day !== null));
    const today = dayNumber(todayISO);
    if (today === null || !days.size) return 0;
    let cursor = days.has(today) ? today : today - 1; // streak survives until end of today
    let streak = 0;
    while (days.has(cursor)) {
      streak += 1;
      cursor -= 1;
    }
    return streak;
  }

  function clubRank(totalCheckins) {
    const total = Math.max(0, Math.floor(Number(totalCheckins) || 0));
    if (total >= 7) return { id: "svoi", label: "СВОЙ", nextAt: null };
    if (total >= 3) return { id: "boec", label: "БОЕЦ", nextAt: 7 };
    return { id: "novichok", label: "НОВИЧОК", nextAt: 3 };
  }

  const api = { MAX_QUANTITY, SIZE_GUIDE, stringList, cleanCart, cleanProfile, priceNumber, buyable, available, sizesFor, productCount, recommendSize, clubStreak, clubRank };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ShopCore = api;
})(typeof window !== "undefined" ? window : globalThis);
