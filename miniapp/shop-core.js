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

  function keepNewerCart(submitted, current, pending, submittedIntent, currentIntent) {
    return !!pending || submittedIntent !== currentIntent || JSON.stringify(cleanCart(submitted)) !== JSON.stringify(cleanCart(current));
  }

  function cleanProfile(value) {
    const fields = ["name", "phone", "city", "address", "entrance", "size", "height", "note"];
    const profile = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const result = Object.fromEntries(fields.map(key => [key, typeof profile[key] === "string" ? profile[key] : ""]));
    result.deliver = ["СДЭК", "Яндекс", "Самовывоз", "Яндекс Доставка", "Согласовать с менеджером"].includes(profile.deliver) ? profile.deliver : "СДЭК";
    return result;
  }

  function available(product) {
    return product.active !== false && stringList(product.sizes).length > 0
      && (!product.inventory || product.sizes.some(size => Number.isSafeInteger(product.inventory[size]?.available) && product.inventory[size].available > 0));
  }

  function stockLabel(product, size) {
    const variants = size ? [product.inventory?.[size]] : (product.sizes || []).map(s => product.inventory?.[s]);
    if (size && Number.isSafeInteger(variants[0]?.available)) return variants[0].available > 0 ? `доступно ${variants[0].available} шт.` : "нет в наличии";
    if (variants.some(x => Number.isSafeInteger(x?.available) && x.available > 0)) return "Размеры в наличии";
    return variants.some(x => !Number.isSafeInteger(x?.available)) || !variants.length ? "Наличие уточняется" : "Нет в наличии";
  }

  function priceRub(product) {
    // Price strings are presentation only. The server applies the billing policy.
    const value = product && product.price_rub;
    return Number.isSafeInteger(value) && value > 0 ? value : 0;
  }

  function orderable(product) {
    return !!product && product.active !== false && stringList(product.sizes).length > 0 && priceRub(product) > 0;
  }

  function paymentOutcome(orders, paymentId) {
    const rows = (Array.isArray(orders) ? orders : []).filter(row => row.payment_id === paymentId);
    if (rows.some(row => row.payment_attention || ["review_required", "refund_required"].includes(row.payment_status))) return "review";
    if (rows.some(row => row.payment_status === "paid" || (!row.payment_status && row.status === "paid"))) return "paid";
    if (rows.some(row => ["cancelled", "expired"].includes(row.payment_status) || row.status === "cancelled")) return "cancelled";
    return "pending";
  }

  function storageOwner(user) {
    return user && Number.isSafeInteger(user.id) && user.id > 0 ? `user:${user.id}` : "preview";
  }

  function createOwnedStorage(getStorage, owner) {
    const prefix = `vorozhbitov_v2:${owner}:`;
    const memory = new Map();
    const dirty = new Set();
    return {
      read(key, fallback = null) {
        if (dirty.has(key)) return memory.get(key) ?? fallback;
        try {
          const storage = getStorage();
          let raw = storage.getItem(prefix + key);
          // Only the old owner-qualified pending request may migrate. Never
          // import a shared/anonymous profile, cart or order history.
          if (raw === null && owner.startsWith("user:") && key === `vorozhbitov_pending_checkout:${owner.slice(5)}`) {
            raw = storage.getItem(key);
            if (raw !== null) {
              const value = JSON.parse(raw);
              memory.set(key, value);
              try {
                storage.setItem(prefix + key, raw);
                storage.removeItem(key);
              } catch (_) { dirty.add(key); }
              return value === null ? fallback : value;
            }
          }
          if (raw !== null) {
            const value = JSON.parse(raw);
            memory.set(key, value);
            return value === null ? fallback : value;
          }
          memory.delete(key);
          return fallback;
        } catch (_) { /* denied storage or corrupt JSON */ }
        const value = memory.get(key);
        return value == null ? fallback : value;
      },
      write(key, value) {
        memory.set(key, value);
        dirty.add(key);
        try { getStorage().setItem(prefix + key, JSON.stringify(value)); dirty.delete(key); } catch (_) { /* keep session retry state */ }
      },
      clear() {
        memory.clear();
        dirty.clear();
        try {
          const storage = getStorage();
          const legacy = ["vorozhbitov_profile", "vorozhbitov_cart", "vorozhbitov_orders", "vorozhbitov_saved", "vorozhbitov_viewed",
            `vorozhbitov_pending_checkout:${owner.startsWith("user:") ? owner.slice(5) : ""}`];
          const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index));
          keys.filter(key => key && (key.startsWith(prefix) || legacy.includes(key))).forEach(key => storage.removeItem(key));
          return true;
        } catch (_) { return false; }
      }
    };
  }

  function sizesFor(products) {
    const order = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "ОДИН"];
    const sizes = stringList(products.filter(product => product.active !== false).flatMap(product => stringList(product.sizes)));
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

  const api = { MAX_QUANTITY, stringList, cleanCart, keepNewerCart, cleanProfile, available, stockLabel, priceRub, orderable, paymentOutcome, storageOwner, createOwnedStorage, sizesFor, productCount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ShopCore = api;
})(typeof window !== "undefined" ? window : globalThis);
