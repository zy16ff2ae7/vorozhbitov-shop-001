/* Owner-scoped, versioned cart sync. No phone/address/checkout payloads here. */
(function (root) {
  "use strict";
  const canonical = items => (items || []).map(x => ({ product_id: x.product_id || x.id,
    size: x.size, person: x.person || "", quantity: x.quantity ?? x.qty }))
    .sort((a, b) => JSON.stringify([a.product_id, a.size, a.person]).localeCompare(JSON.stringify([b.product_id, b.size, b.person])));
  const signature = items => JSON.stringify(canonical(items));
  const operationId = () => `cart-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;

  function createSync({ request, getLocal, setLocal, readBase, writeBase, pending, notify = () => {} }) {
    let base = readBase() || null;
    let remote = null;
    let mode = "loading";
    let running = null;
    let generation = 0;
    let mutation = base?.mutation || null;
    const signal = value => { mode = value; notify(value); };
    const remember = () => writeBase(base ? { ...base, mutation } : { mutation });
    const adopt = cart => {
      remote = cart;
      base = { revision: cart.revision, items: canonical(cart.items) };
      mutation = null;
      remember();
      setLocal(cart.items);
      signal("ready");
      return cart.revision;
    };
    async function writeCart() {
      if (running) return running;
      generation++; // Invalidate older GET snapshots before mutating.
      running = (async () => {
        if (!Number.isSafeInteger(base?.revision)) throw new Error("Сначала загрузи общую корзину из бота.");
        if (mode === "conflict") throw new Error("Корзина изменилась. Выбери, какой состав оставить.");
        signal("saving");
        try {
          // Recover an uncertain write BEFORE sending a new one. If a different
          // device changed the cart since that operation, surface a conflict.
          do {
            if (!mutation && signature(getLocal()) === signature(base.items)) break;
            mutation ||= { revision: base.revision, operation_id: operationId(), items: canonical(getLocal()) };
            remember();
            const result = await request("POST", mutation);
            const cart = result.cart;
            remote = cart;
            if (cart.revision !== mutation.revision + 1 || signature(cart.items) !== signature(mutation.items)) {
              signal("conflict");
              throw new Error("Корзину уже изменили в боте или другом окне. Ничего не перезаписали.");
            }
            base = { revision: cart.revision, items: canonical(cart.items) };
            mutation = null;
            remember();
          } while (signature(getLocal()) !== signature(base.items));
          signal("ready");
          return base.revision;
        } catch (error) {
          if (error.cart) remote = error.cart;
          if (error.status === 400) { mutation = null; remember(); }
          signal(error.code === "cart_conflict" || mode === "conflict" ? "conflict" : "offline");
          throw error;
        }
      })();
      try { return await running; } finally { running = null; }
    }
    async function load(choice) {
      if (running) await running.catch(() => {});
      const requestedGeneration = ++generation;
      if (choice === "local" && remote) {
        // Confirm against the snapshot actually shown, not a silently refetched
        // version that another device could have changed since the warning.
        base = { revision: remote.revision, items: canonical(remote.items) };
        mutation = null;
        remember();
        signal("ready");
        return writeCart();
      }
      signal("loading");
      try {
        const { cart } = await request("GET");
        if (requestedGeneration !== generation) return;
        remote = cart;
        if (pending() && !choice) {
          // A committed checkout may have cleared the remote cart while its
          // response was lost. Never replace the retry form or its CAS version.
          signal("frozen");
          return;
        }
        if (choice === "preserve") {
          if (pending()) { signal("frozen"); return; }
          if (signature(getLocal()) === signature(cart.items)) return adopt(cart);
          signal("conflict"); return;
        }
        if (choice === "remote") return adopt(cart);
        if (choice === "local") {
          base = { revision: cart.revision, items: canonical(cart.items) };
          mutation = null;
          remember();
          signal("ready");
          return writeCart();
        }
        if (mutation) return writeCart();
        const local = getLocal();
        if (signature(local) === signature(cart.items)) return adopt(cart);
        if (!base && !local.length) return adopt(cart);
        if (base && signature(local) === signature(base.items)) return adopt(cart);
        if ((!base && cart.revision === 0) || base?.revision === cart.revision) {
          base = { revision: cart.revision, items: canonical(cart.items) };
          signal("ready");
          return writeCart();
        }
        signal("conflict");
      } catch (error) {
        if (requestedGeneration !== generation) return;
        if (mode !== "conflict") signal("offline");
        throw error;
      }
    }
    return {
      load,
      manual: () => signal("manual"),
      mode: () => mode,
      remote: () => remote,
      async changed() {
        if (pending()) return signal("frozen");
        if (["ready", "saving", "offline"].includes(mode) && base) return writeCart();
      },
      async beforeCheckout(payload) {
        const old = pending();
        if (old && old.fingerprint === JSON.stringify(payload)) return old.cart_revision;
        if (!base) await load();
        // A deliberately edited retry must CAS the old cart first. If the old
        // checkout committed, its cart clear makes this fail rather than duplicate.
        return writeCart();
      },
      afterCheckout: preserve => load(preserve || pending() ? "preserve" : "remote")
    };
  }
  const api = { createSync, canonical };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ShopCartSync = api;
})(typeof window === "undefined" ? globalThis : window);
