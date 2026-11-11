(() => {
  "use strict";

  const Core = window.ShopCore;
  const FALLBACK_CATALOG = {
    brand: { name: "СИЛА И ЧЕСТЬ", descriptor: "Сила и честь", drop: "ВЫПУСК 001" },
    channel_url: "https://t.me/sila_i_chestb",
    privacy_url: "",
    media: { welcome_motion: true },
    drop_event: {}, film: [], club: {}, community: [],
    categories: [], products: []
  };

  const ICONS = {
    bag: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 8h14l1 13H4L5 8Z"/><path d="M8.5 9V6a3.5 3.5 0 0 1 7 0v3"/></svg>',
    sliders: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/><path d="M8 4v4M15 10v4M10 16v4"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.6"/><path d="m16 16 5 5"/></svg>',
    share: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.8 7.5-4.3M8.2 13.2l7.5 4.3"/></svg>',
    home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 10.8 9-7.3 9 7.3V21H3V10.8Z"/><path d="M9 21v-6h6v6"/></svg>',
    grid: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>',
    pin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s7-6.2 7-11.2A7 7 0 0 0 5 9.8C5 14.8 12 21 12 21Z"/><circle cx="12" cy="9.8" r="2.2"/></svg>',
    people: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3"/><path d="M3 20c1-3.5 3-5 6-5s5 1.5 6 5M16 5a3 3 0 0 1 0 6M17 15c2 0 3.5 1.5 4 4"/></svg>',
    user: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.4"/><path d="M5 20c1.2-3.8 3.8-5.6 7-5.6s5.8 1.8 7 5.6"/></svg>'
  };


  const state = {
    data: FALLBACK_CATALOG,
    category: "all",
    search: "",
    stock: "all",
    sizeFilter: "all",
    sort: "featured",
    cart: Core.cleanCart(loadJSON("vorozhbitov_cart", [])),
    viewed: Core.stringList(loadJSON("vorozhbitov_viewed", [])),
    profile: Core.cleanProfile(loadJSON("vorozhbitov_profile", {})),
    catalogReady: false,
    catalogLoading: false,
    checkoutPending: false,
    modalFocus: new Map(),
    currentProduct: null,
    selectedSize: null,
    qty: 1,
    galleryIndex: 0,
    galleryGroup: "studio",
    cartStep: "items",
    deliver: "СДЭК",
    toastTimer: null,
    modalStack: [],
    viewer: null,
    stage: "3d",
    profileOrders: [],
    club: loadJSON("vorozhbitov_club", { joined: false, started_on: "", checkins: [], total: 0, completed: false, early_access: false }),
    clubSource: "local",
    clubBusy: false,
    fitChoice: "regular",
    filmIndex: 0,
    filmTimer: null,
    ownerOrder: null,
    dropTimer: null
  };

  // The catalogue now spells this existing size 2XL. Preserve saved carts.
  let migratedSize = false;
  state.cart = state.cart.map(item => {
    if (item.id !== "tee-sila-i-chest" || item.size !== "XXL") return item;
    migratedSize = true;
    return { ...item, size: "2XL", key: `tee-sila-i-chest::2XL${item.person ? `::${item.person}` : ""}` };
  });
  if (migratedSize) {
    state.cart = Core.cleanCart(state.cart);
    saveJSON("vorozhbitov_cart", state.cart);
  }

  // Snapshot the previous visit before this session changes the cart or history.
  const returnVisit = { cart: state.cart.length > 0, viewed: [...state.viewed], paymentId: "", dismissed: false, generation: 0 };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  let syncClubLifterMotion = () => {};

  function loadJSON(key, fallback) {
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || "null");
      return parsed === null ? fallback : parsed;
    } catch (_) {
      return fallback;
    }
  }

  function saveJSON(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode */ }
  }

  const checkoutClient = window.ShopCheckout.createClient({
    fetch: window.fetch.bind(window),
    read: key => loadJSON(key, null),
    write: saveJSON
  });

  function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  function iconize(root = document) {
    $$("[data-icon]", root).forEach(node => { node.innerHTML = ICONS[node.dataset.icon] || ""; });
  }

  function haptic(kind) {
    try {
      const hap = tg && tg.HapticFeedback;
      if (!hap) return;
      if (kind === "success") hap.notificationOccurred("success");
      else if (kind === "error") hap.notificationOccurred("error");
      else hap.impactOccurred(kind || "light");
    } catch (_) { /* browser */ }
  }

  function imagesFor(product) {
    const list = Array.isArray(product.images) && product.images.length ? product.images : [product.image || product.image_url || "assets/base-tee.jpg"];
    return list.map(src => String(src));
  }

  function imageFor(product) {
    return product.card_image || imagesFor(product)[0];
  }

  function backImageFor(product) {
    const images = imagesFor(product);
    if (!images || images.length < 2) return null;
    const labels = product.image_labels || [];
    let backIdx = labels.findIndex(lbl => /спина\s*·\s*исходный/i.test(lbl));
    if (backIdx < 0) backIdx = labels.findIndex(lbl => /спина/i.test(lbl));
    if (backIdx < 0 && images.length > 1) backIdx = 1;
    return backIdx >= 0 && images[backIdx] ? images[backIdx] : null;
  }

  function productById(id) {
    return state.data.products.find(product => product.id === id);
  }

  function priceNumber(price) {
    return Core.priceNumber(price);
  }

  function rubles(value) {
    const amount = Number(value);
    return Number.isFinite(amount) ? `${Math.round(amount).toLocaleString("ru-RU")} ₽` : "Цена уточняется";
  }

  function isLimited(product) {
    return /limited|лимит|последн|мало|выпуск|drop|дроп/i.test(`${product.badge || ""} ${product.stock_label || ""}`);
  }

  function telegramUser() {
    return (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || null;
  }

  function profileScore(profile) {
    return ["name", "phone", "city", "address", "entrance"].reduce((n, key) => n + (String(profile[key] || "").trim() ? 1 : 0), 0);
  }

  function readProfileForm() {
    return {
      name: ($("#profileName") && $("#profileName").value.trim()) || "",
      phone: ($("#profilePhone") && $("#profilePhone").value.trim()) || "",
      city: ($("#profileCity") && $("#profileCity").value.trim()) || "",
      address: ($("#profileAddress") && $("#profileAddress").value.trim()) || "",
      entrance: ($("#profileEntrance") && $("#profileEntrance").value.trim()) || "",
      deliver: state.profile.deliver || "СДЭК",
      size: state.profile.size || "",
      note: ($("#profileNote") && $("#profileNote").value.trim()) || ""
    };
  }

  function fillProfileForm() {
    const profile = state.profile;
    const user = telegramUser();
    if ($("#profileName")) $("#profileName").value = profile.name || (user && user.first_name) || "";
    if ($("#profilePhone")) $("#profilePhone").value = profile.phone || "";
    if ($("#profileCity")) $("#profileCity").value = profile.city || "";
    if ($("#profileAddress")) $("#profileAddress").value = profile.address || "";
    if ($("#profileEntrance")) $("#profileEntrance").value = profile.entrance || "";
    if ($("#profileNote")) $("#profileNote").value = profile.note || "";
    if ($("#profileAvatar")) $("#profileAvatar").textContent = "";
    if ($("#profileHello")) $("#profileHello").textContent = profile.name || (user && user.first_name) || "Свой";
    if ($("#profileHandle")) {
      $("#profileHandle").textContent = user && user.username ? `@${user.username}` : "Данные для заказа и доставки";
    }
    if ($("#profileFill")) $("#profileFill").textContent = `${profileScore(profile)} / 5`;
    $$("#profileDeliver .filter-chip").forEach(node => node.classList.toggle("active", node.dataset.profileDeliver === (profile.deliver || "СДЭК")));
    if ($("#profileCartN")) $("#profileCartN").textContent = String(state.cart.reduce((sum, item) => sum + Number(item.qty || 0), 0));
  }

  function formatOrderWhen(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  }

  function renderProfileOrders(orders, source) {
    const box = $("#profileOrders");
    if (!box) return;
    state.profileOrders = Array.isArray(orders) ? orders : [];
    if (!orders.length) {
      const mascotVid = `<video src="assets/mascot/steel-glasses-anthracite-20261112a.mp4" poster="assets/mascot/steel-glasses-anthracite-20261112a.jpg" autoplay loop muted playsinline webkit-playsinline preload="metadata" aria-hidden="true"></video>`;
      box.innerHTML = source === "bot"
        ? `<div class="profile-empty-mascot">${mascotVid}<p class="profile-empty">Заказов пока нет. Здесь появятся заказы, зарегистрированные магазином.</p></div>`
        : `<div class="profile-empty-mascot">${mascotVid}<p class="profile-empty">Открой витрину из Telegram: здесь будут реальные заказы, способ оплаты и этапы доставки.</p></div>`;
      return;
    }
    box.innerHTML = `<p class="profile-caption">Заказы</p>` + orders.map((row, orderIndex) => {
      const status = row.status || "new";
      const when = formatOrderWhen(row.created_at || row.at);
      const size = row.size ? ` · размер ${row.size}` : "";
      const qty = Number(row.quantity || 1);
      const pay = "";
      const cancel = row.can_cancel
        ? `<button type="button" class="profile-order-cancel" data-cancel-order="${Number(row.id)}">Отменить заказ</button>`
        : "";
      const ownerCard = `<button type="button" class="profile-order-pay profile-order-card" data-owner-card="${orderIndex}">Карта владельца</button>`;
      const address = [row.city, row.address, row.entrance].map(value => String(value || "").trim()).filter(Boolean).join(" · ");
      const giftSerial = row.gift && row.gift.serial_label ? ` · №${row.gift.serial_label}` : "";
      return `<article class="profile-order" data-status="${escapeHTML(status)}">
        ${["new", "awaiting_payment", "paid", "confirmed", "shipped", "received", "completed"].includes(status) ? `<div class="order-progress" aria-label="Этап заказа"><span class="${["paid", "confirmed", "shipped", "received", "completed"].includes(status) ? "is-done" : "is-current"}">01 · ОПЛАТА</span><span class="${["confirmed", "shipped", "received", "completed"].includes(status) ? "is-done" : status === "paid" ? "is-current" : ""}">02 · СБОРКА</span><span class="${["shipped", "received", "completed"].includes(status) ? "is-current" : ""}">03 · ДОСТАВКА</span></div>` : ""}
        <div class="profile-order-main">
          <strong>${escapeHTML(row.product_name || "Вещь")}${escapeHTML(size)} · ${qty} шт.</strong>
          <small>${escapeHTML(when)}${row.amount_label ? ` · Сумма: ${escapeHTML(row.amount_label)}` : ""}</small>
          ${row.payment_label ? `<small>Оплата: ${escapeHTML(row.payment_label)}</small>` : ""}
          ${row.payment_method ? `<small>Способ оплаты: ${escapeHTML(row.payment_method_label || row.payment_method)}</small>` : ""}
          ${row.phone ? `<small>Телефон: ${escapeHTML(row.phone)}</small>` : ""}
          ${row.fulfilment_label ? `<small>Сборка: ${escapeHTML(row.fulfilment_label)}</small>` : ""}
          ${row.delivery_status_label ? `<small>Доставка: ${escapeHTML(row.delivery_status_label)}${row.delivery_method ? ` · ${escapeHTML(row.delivery_method)}` : ""}</small>` : ""}
          ${address ? `<small>Адрес на момент заказа: ${escapeHTML(address)}</small>` : ""}
          ${!address && row.legacy_details ? `<small>Детали на момент заказа: ${escapeHTML(row.legacy_details)}</small>` : ""}
          ${row.gift ? `<small class="profile-order-gift">Подарок: ${escapeHTML(row.gift.name || "Жетон")} · 0 ₽${escapeHTML(giftSerial)}</small>` : ""}
        </div>
        <div class="profile-order-side">
          <span class="profile-order-status is-${escapeHTML(status)}">${escapeHTML(row.status_label || status)}</span>
          ${pay}${cancel}${ownerCard}
        </div>
      </article>`;
    }).join("");
  }

  async function loadProfileOrders() {
    const box = $("#profileOrders");
    if (!box) return;
    box.innerHTML = `<p class="profile-empty">Загружаю заказы…</p>`;
    const initData = tg && tg.initData;
    if (!initData) {
      renderProfileOrders([], "preview");
      return;
    }
    try {
      const response = await fetch("/api/my-orders", {
        headers: { Accept: "application/json", "X-Telegram-Init-Data": initData }
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "orders unavailable");
      renderProfileOrders(Array.isArray(payload.orders) ? payload.orders : [], "bot");
    } catch (_) {
      box.innerHTML = `<div class="profile-empty"><p>Не удалось загрузить заказы. Проверь соединение и повтори.</p><button type="button" class="button button-ghost" data-retry-orders>ПОВТОРИТЬ ЗАГРУЗКУ</button></div>`;
    }
  }

  async function cancelProfileOrder(orderId) {
    const initData = tg && tg.initData;
    if (!initData) {
      showToast("Отмена — в чате с ботом, «Мои покупки».");
      return;
    }
    try {
      const response = await fetch("/api/my-orders/cancel", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Telegram-Init-Data": initData
        },
        body: JSON.stringify({ order_id: orderId })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        showToast(payload.error || "Не получилось отменить.");
        haptic("error");
        return;
      }
      haptic("success");
      showToast("Все позиции заказа отменены.");
      loadProfileOrders();
    } catch (_) {
      showToast("Сеть не ответила. Попробуй ещё раз.");
    }
  }

  function applyProfileToCheckout() {
    const profile = state.profile;
    const user = telegramUser();
    if ($("#checkoutName") && !$("#checkoutName").value) $("#checkoutName").value = profile.name || (user && user.first_name) || "";
    if ($("#checkoutPhone") && !$("#checkoutPhone").value) $("#checkoutPhone").value = profile.phone || "";
    if ($("#checkoutCity") && !$("#checkoutCity").value) $("#checkoutCity").value = profile.city || "";
    if ($("#checkoutAddress") && !$("#checkoutAddress").value) $("#checkoutAddress").value = profile.address || "";
    if ($("#checkoutNote") && !$("#checkoutNote").value) {
      $("#checkoutNote").value = profile.note || "";
    }
    if (profile.deliver) {
      state.deliver = profile.deliver;
      $$("#deliverRow .filter-chip").forEach(node => { const active = node.dataset.deliver === profile.deliver; node.classList.toggle("active", active); node.setAttribute("aria-pressed", String(active)); });
    }
  }

  function persistProfile(profile, pushToBot) {
    state.profile = profile;
    saveJSON("vorozhbitov_profile", profile);
    if (profile.deliver) state.deliver = profile.deliver;
    applyProfileToCheckout();
    fillProfileForm();
    if (pushToBot && tg && typeof tg.sendData === "function") {
      try {
        tg.sendData(JSON.stringify({ type: "profile", consent: true, profile }));
        return "bot";
      } catch (_) { /* keep local */ }
    }
    return "local";
  }

  function saveProfile() {
    const profile = readProfileForm();
    if (profile.name && profile.name.length < 2) return showToast("Имя слишком короткое.");
    if (profile.phone && profile.phone.replace(/\D/g, "").length < 10) return showToast("Проверь номер телефона.");
    persistProfile(profile, false);
    haptic("success");
    showToast("Профиль сохранён. В заказе подставится сам.");
  }

  function openProfile() {
    fillProfileForm();
    loadProfileOrders();
    openModal("profileModal");
    haptic("light");
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.innerHTML = '<span class="toast-mark" aria-hidden="true"></span><span>' + escapeHTML(message) + "</span>";
    toast.classList.add("visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("visible"), 2800);
  }

  function scrollToId(id) {
    const element = document.getElementById(id);
    if (element) element.scrollIntoView({ behavior: reducedMotion() ? "instant" : "smooth", block: "start" });
  }

  function syncBackButton() {
    if (!tg || !tg.BackButton) return;
    try {
      if (state.modalStack.length) tg.BackButton.show();
      else tg.BackButton.hide();
    } catch (_) { /* old clients */ }
  }

  function syncModalLayers() {
    const top = state.modalStack.at(-1);
    const covered = Boolean(top) || document.body.classList.contains("welcoming") || document.body.classList.contains("booting");
    $("#app").inert = covered;
    $(".bottom-nav").inert = covered;
    $("#welcome").inert = Boolean(top) || $("#welcome").classList.contains("hidden");
    $$(".modal-backdrop").forEach(modal => {
      const index = state.modalStack.indexOf(modal.id);
      modal.style.zIndex = String(120 + Math.max(index, 0) * 2);
      modal.inert = modal.id !== top;
      modal.setAttribute("aria-hidden", String(modal.id !== top));
    });
    if (top !== "productModal" || state.stage !== "3d" || document.hidden) stop3D();
    else if (state.viewer) state.viewer.start();
    syncWelcomeVideo();
    syncClubLifterMotion();
  }

  function focusableElements(modal) {
    return $$("button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), summary, [tabindex='0']", modal)
      .filter(node => !node.closest("[inert]") && node.getClientRects().length);
  }

  function openModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    if (!state.modalStack.includes(id)) {
      state.modalFocus.set(id, document.activeElement);
      state.modalStack.push(id);
    }
    modal.classList.remove("hidden");
    document.body.classList.add("modal-open");
    syncModalLayers();
    const focus = modal.querySelector("[data-close]") || focusableElements(modal)[0];
    if (focus) focus.focus({ preventScroll: true });
    syncBackButton();
    syncMainButton();
  }

  function closeModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    const wasTop = state.modalStack.at(-1) === id;
    modal.classList.add("hidden");
    state.modalStack = state.modalStack.filter(item => item !== id);
    if (!state.modalStack.length) document.body.classList.remove("modal-open");
    if (id === "productModal") {
      stop3D();
      pauseProductVideo();
      const frame = $("#real3dFrame");
      if (frame) frame.removeAttribute("src");
    }
    if (id === "payModal") stopPayPoll();
    if (id === "filmModal") stopFilm();
    syncModalLayers();
    const previous = state.modalFocus.get(id);
    state.modalFocus.delete(id);
    if (wasTop) {
      const top = document.getElementById(state.modalStack.at(-1));
      const target = previous && previous.isConnected && !previous.closest("[inert]")
        ? previous : top ? focusableElements(top)[0] : $("#heroProductButton");
      if (target) target.focus({ preventScroll: true });
    }
    syncBackButton();
    syncMainButton();
  }

  function ensureViewer() {
    const canvas = $("#view3d");
    if (!canvas || !window.Vorozhbitov3D) return null;
    if (!state.viewer) {
      try { state.viewer = new window.Vorozhbitov3D.Viewer(canvas); }
      catch (_) { return null; }
    }
    return state.viewer;
  }

  function start3D(product) {
    const viewer = ensureViewer();
    if (!viewer) {
      setStage("photo", true);
      return;
    }
    if (viewer.product !== product) viewer.setProduct(product);
    viewer.start();
    window.requestAnimationFrame(() => viewer.resize());
  }

  function stop3D() {
    if (state.viewer) state.viewer.stop();
  }

  function pauseProductVideo() {
    const video = $("#sheetVideo");
    if (video) video.pause();
  }

  function playProductVideo() {
    const video = $("#sheetVideo");
    if (!video || !state.currentProduct || !state.currentProduct.video) return;
    const playback = video.play();
    if (playback && typeof playback.catch === "function") playback.catch(() => { /* Controls remain available after an autoplay block. */ });
  }

  function setStage(mode, silent) {
    if (mode === "video" && (!state.currentProduct || !state.currentProduct.video)) mode = "photo";
    if (mode === "3d" && (!state.currentProduct || !Array.isArray(state.currentProduct.spin) || state.currentProduct.spin.length < 2)) mode = "photo";
    const previousMode = state.stage;
    state.stage = mode;
    const galleryMode = mode === "photo";
    if (!galleryMode) $("#mediaError").classList.add("hidden");
    const canvas = $("#view3d");
    const real3d = mode === "3d" && ["tee-sila-i-chest", "tee-vzglyad"].includes(state.currentProduct?.id);
    const real3dFrame = $("#real3dFrame");
    const photo = $("#sheetImage");
    const fit = $("#sheetFit");
    const video = $("#sheetVideo");
    const hint = $("#view3dHint");
    const dots = $("#galleryDots");
    if (mode !== "3d" || real3d) stop3D();
    if (mode !== "video") pauseProductVideo();
    if ($("#stage3d")) $("#stage3d").classList.toggle("active", mode === "3d");
    if ($("#stagePhoto")) $("#stagePhoto").classList.toggle("active", mode === "photo");
    if ($("#stageFit")) $("#stageFit").classList.toggle("active", mode === "fit");
    if ($("#stageVideo")) $("#stageVideo").classList.toggle("active", mode === "video");
    if (canvas) canvas.classList.toggle("hidden", mode !== "3d" || real3d);
    if (real3dFrame) {
      real3dFrame.classList.toggle("hidden", !real3d);
      if (real3d) {
        const targetSrc = `assets/vzglyad-3d/index-20261111b.html?product=${state.currentProduct?.id === "tee-sila-i-chest" ? "sila" : "vzglyad"}`;
        if (real3dFrame.getAttribute("src") !== targetSrc) real3dFrame.src = targetSrc;
      } else {
        real3dFrame.removeAttribute("src");
      }
    }
    if (photo) photo.classList.toggle("hidden", !galleryMode);
    if (fit) fit.classList.toggle("hidden", mode !== "fit");
    if (video) video.classList.toggle("hidden", mode !== "video");
    if (hint) hint.classList.toggle("hidden", mode !== "3d" || real3d);
    if (dots) dots.classList.toggle("hidden", !galleryMode);
    $("#galleryGroups").classList.toggle("hidden", !galleryMode || !state.currentProduct?.source_images?.length);
    if (galleryMode && previousMode !== mode && state.currentProduct) renderGallery(state.currentProduct);
    $("#zoomProduct").classList.toggle("hidden", mode === "video" || mode === "fit" || real3d);
    $(".media-toolbar").classList.toggle("hidden", mode === "video" || mode === "fit" || real3d);
    $("#spinToggle").classList.toggle("hidden", mode !== "3d");
    $$("[data-stage]").forEach(button => {
      const active = button.dataset.stage === mode;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    updateMediaPosition();
    if (silent) return;
    if (mode === "3d" && state.currentProduct && !real3d) start3D(state.currentProduct);
    else if (mode === "video") playProductVideo();
  }

  function closeTopModal() {
    const id = state.modalStack[state.modalStack.length - 1];
    if (id) closeModal(id);
  }

  function categoryName(id) {
    const found = state.data.categories.find(category => category.id === id);
    return found ? found.name : "ВЫПУСК";
  }

  function renderCategoryChips() {
    const root = $("#categoryChips");
    const visible = state.data.categories.filter(category => state.data.products.some(product => Core.buyable(product) && product.category === category.id));
    root.classList.toggle("hidden", visible.length <= 1);
    if (visible.length <= 1) state.category = "all";
    const all = [{ id: "all", name: "Все вещи" }, ...visible];
    root.innerHTML = all.map(category => `<button class="category-chip ${state.category === category.id ? "active" : ""}" data-category="${escapeHTML(category.id)}" type="button" aria-pressed="${state.category === category.id}">${escapeHTML(category.name)}</button>`).join("");
  }

  function filteredProducts() {
    const search = state.search.trim().toLowerCase();
    let list = state.data.products.filter(product => {
      if (!Core.buyable(product)) return false;
      if (state.category !== "all" && product.category !== state.category) return false;
      if (state.stock === "limited" && !isLimited(product)) return false;
      if (state.stock === "available" && !Core.available(product)) return false;
      if (state.sizeFilter !== "all" && !(product.sizes || []).includes(state.sizeFilter)) return false;
      if (search && !`${product.name} ${product.description} ${product.badge} ${product.material} ${product.signature || ""} ${product.print || ""} ${(product.details || []).join(" ")}`.toLowerCase().includes(search)) return false;
      return true;
    });
    if (state.sort === "price-asc") list = [...list].sort((a, b) => priceNumber(a.price) - priceNumber(b.price));
    else if (state.sort === "price-desc") list = [...list].sort((a, b) => priceNumber(b.price) - priceNumber(a.price));
    else if (state.sort === "limited") list = [...list].sort((a, b) => Number(isLimited(b)) - Number(isLimited(a)));
    return list;
  }

  function renderProductCard(product, index) {
    const spin = Array.isArray(product.spin) ? product.spin : [];
    const studioModelCard = ["tee-sila-i-chest", "tee-vzglyad"].includes(product.id);
    const brandedSpin = studioModelCard || (product.spin_kind === "visualization" && spin.length >= 8);
    const spinLabels = Array.isArray(product.spin_labels) ? product.spin_labels : [];
    const backIndex = spinLabels.findIndex(label => /180°|сзади\s*[·—-]?\s*180/i.test(String(label)));
    const frontImg = studioModelCard ? imageFor(product) : brandedSpin ? spin[0] : imageFor(product);
    const backImg = studioModelCard ? backImageFor(product) : brandedSpin ? spin[backIndex >= 0 && backIndex < spin.length ? backIndex : Math.floor(spin.length / 2)] : backImageFor(product);
    return `<article class="product-card ${brandedSpin ? 'is-branded-spin' : ''}" data-product-id="${escapeHTML(product.id)}" style="--i:${index}">
      <div class="product-image ${backImg ? 'has-back' : ''}">
        <img class="product-img-front ${backImg ? 'has-back-pair' : ''}" src="${escapeHTML(frontImg)}" alt="${escapeHTML(product.name)}" ${index === 0 ? 'loading="eager" fetchpriority="high"' : 'loading="lazy"'}>
        ${backImg ? `<img class="product-img-back" src="${escapeHTML(backImg)}" alt="${escapeHTML(product.name)} (вид со спины)" loading="lazy">` : ''}
        <span class="product-badge">${escapeHTML(product.badge || "БАЗА")}</span>
        ${backImg ? `<button type="button" class="badge-side" aria-label="Показать вид со спины" data-action="flip">СПИНА ↺</button>` : ''}
        ${backImg ? `<div class="card-dots" aria-hidden="true"><span class="card-dot active"></span><span class="card-dot"></span></div>` : ''}
        ${studioModelCard ? `<span class="badge-3d">3D · 360°</span>` : Array.isArray(product.spin) && product.spin.length > 1 ? `<span class="badge-3d">${product.spin_kind === "visualization" ? "360°" : "ВРАЩЕНИЕ 360°"}</span>` : `<span class="badge-3d">${imagesFor(product).length} ФОТО</span>`}
        <span class="product-hover">ОТКРЫТЬ КАРТОЧКУ <b>↗</b></span>
      </div>
      <div class="product-info">
        <div class="product-topline"><span>${escapeHTML(categoryName(product.category))}</span><span class="product-stock">${escapeHTML(product.stock_label || "В наличии")}</span></div>
        <h3><button type="button" class="product-open" aria-label="Открыть ${escapeHTML(product.name)}">${escapeHTML(product.name)}</button></h3>
        <div class="product-sizes">${(product.sizes || []).map(escapeHTML).join(" · ") || "Ждём пополнение"}</div>
        <div class="product-bottom"><strong class="product-price">${escapeHTML(product.price)}</strong><span class="product-card-link" aria-hidden="true">О ВЕЩИ ↗</span></div>
      </div>
    </article>`;
  }

  function renderProducts() {
    const buyableTotal = (state.data.products || []).filter(Core.buyable).length;
    $(".catalog-tools")?.classList.toggle("hidden", buyableTotal <= 4);
    const products = filteredProducts();
    const root = $("#productGrid");
    $("#productCount").textContent = Core.productCount(products.length);
    root.innerHTML = products.map(renderProductCard).join("");
    $("#emptyState").classList.toggle("hidden", products.length > 0 || !state.catalogReady);
    iconize(root);
    updateCounters();
  }

  function galleryLabel(product, index) {
    return state.galleryGroup === "source"
      ? (product.source_labels || [])[index] || `Исходный кадр ${index + 1}`
      : (product.image_labels || [])[index] || `Ракурс ${index + 1}`;
  }

  function galleryImages(product) {
    return state.galleryGroup === "source" && product.source_images?.length
      ? product.source_images : imagesFor(product);
  }

  function updateMediaPosition() {
    const viewer = state.viewer;
    if (state.stage === "video") {
      $("#mediaPosition").textContent = "ВИДЕО";
      $("#mediaViewName").textContent = "";
      $("#mediaPrev").disabled = true;
      $("#mediaNext").disabled = true;
      $("#spinToggle").disabled = true;
      return;
    }
    const count = state.stage === "3d" && viewer ? viewer.frames.length : galleryImages(state.currentProduct || {}).length;
    const index = state.stage === "3d" && viewer ? viewer.frameIndex() : state.galleryIndex;
    $("#mediaPosition").textContent = `${String(index + 1).padStart(2, "0")} / ${String(count).padStart(2, "0")}`;
    const product = state.currentProduct || {};
    $("#mediaViewName").textContent = state.stage === "3d"
      ? (product.spin_labels || [])[index] || "Обзор вещи"
      : galleryLabel(product, index);
    const sourceActive = state.stage === "photo" && state.galleryGroup === "source";
    $$("[data-gallery-group]").forEach(button => {
      const active = button.dataset.galleryGroup === (sourceActive ? "source" : "studio");
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $("#mediaPrev").disabled = count < 2;
    $("#mediaNext").disabled = count < 2;
    $("#spinToggle").disabled = count < 2;
    const rotating = Boolean(viewer && viewer.rotating);
    $("#spinToggle").textContent = rotating ? "Остановить" : "Вращать";
    $("#spinToggle").setAttribute("aria-pressed", String(rotating));
  }

  function renderGallery(product) {
    const images = galleryImages(product);
    state.galleryIndex = 0;
    $("#mediaError").classList.add("hidden");
    $(".sheet-image").classList.toggle("source-media", state.galleryGroup === "source");
    $("#sheetImage").src = images[0];
    $("#sheetImage").alt = `${product.name} — ${galleryLabel(product, 0)}`;
    $("#galleryDots").innerHTML = images.map((src, index) => `<button type="button" data-gallery="${index}" class="${index === 0 ? "active" : ""}" aria-pressed="${index === 0}" aria-label="${escapeHTML(galleryLabel(product, index))}"><span aria-hidden="true">${String(index + 1).padStart(2, "0")}</span><img src="${escapeHTML(src)}" alt="" decoding="async"></button>`).join("");
  }

  function setGallery(index) {
    const product = state.currentProduct;
    if (!product) return;
    const images = galleryImages(product);
    $("#mediaError").classList.add("hidden");
    state.galleryIndex = (index + images.length) % images.length;
    const sheetImg = $("#sheetImage");
    if (sheetImg) {
      sheetImg.style.opacity = "0.7";
      sheetImg.src = images[state.galleryIndex];
      sheetImg.alt = `${product.name} — ${galleryLabel(product, state.galleryIndex)}`;
      window.requestAnimationFrame(() => { sheetImg.style.opacity = "1"; });
    }
    $$("#galleryDots button").forEach((node, i) => {
      node.classList.toggle("active", i === state.galleryIndex);
      node.setAttribute("aria-pressed", String(i === state.galleryIndex));
    });
    updateMediaPosition();
  }

  function stepMedia(delta) {
    if (state.stage === "video") return;
    if (state.stage === "3d" && state.viewer) state.viewer.step(delta);
    else setGallery(state.galleryIndex + delta);
    updateMediaPosition();
  }

  function zoomProduct() {
    const product = state.currentProduct;
    if (!product || state.stage === "video") return;
    const src = state.stage === "3d" && state.viewer ? state.viewer.currentSource() : galleryImages(product)[state.galleryIndex];
    openLightbox(src, `${product.name} · ${state.stage === "3d" ? "Обзор 360° · визуализация" : galleryLabel(product, state.galleryIndex)}`);
  }

  function updatePurchaseSummary() {
    const product = state.currentProduct && productById(state.currentProduct.id);
    const gift = Boolean(product && !Core.buyable(product));
    $("#purchaseSummary").textContent = gift
      ? "Добавим бесплатно к заказу, пока есть тираж."
      : product ? `${product.price} · ${state.selectedSize ? `размер ${state.selectedSize}` : "Выбери размер"}` : "Выбери размер";
    const blocked = !state.catalogReady || !product || Boolean(product.stock_check_unavailable) || !Core.available(product) || gift;
    $("#addToCartButton").disabled = blocked;
    $("#addToCartButton").innerHTML = !state.catalogReady ? (state.catalogLoading ? "ПРОВЕРЯЕМ НАЛИЧИЕ" : "ОБНОВИ КАТАЛОГ") : gift ? "ТОЛЬКО В ПОДАРОК" : product?.stock_check_unavailable ? "НАЛИЧИЕ УТОЧНЯЕТСЯ" : blocked ? "ЖДЁМ ПОПОЛНЕНИЕ" : state.selectedSize ? 'ДОБАВИТЬ В КОРЗИНУ <span>+</span>' : 'ВЫБРАТЬ РАЗМЕР <span>↑</span>';
    const directBtn = $("#directOrderButton");
    if (directBtn) {
      directBtn.disabled = blocked;
      const isPreorder = /предзаказ/i.test(`${product?.badge || ""} ${product?.stock_label || ""}`);
      directBtn.innerHTML = gift ? "ТОЛЬКО В ПОДАРОК" : blocked ? "ЖДЁМ ПОПОЛНЕНИЕ" : isPreorder ? "ОФОРМИТЬ ПРЕДЗАКАЗ В TELEGRAM ↗" : "ОФОРМИТЬ ЗАКАЗ В TELEGRAM ↗";
    }
  }

  // The brand's chart describes body measurements, not garment dimensions.
  const BODY_SIZE_CHART = [
    {size:"S", chest:[88,96], waist:[76,84], hips:[92,100], height:[170,176]},
    {size:"M", chest:[96,104], waist:[84,92], hips:[100,108], height:[176,182]},
    {size:"L", chest:[104,112], waist:[92,100], hips:[108,116], height:[182,188]},
    {size:"XL", chest:[112,120], waist:[100,108], hips:[116,124], height:[188,194]},
    {size:"2XL", chest:[120,128], waist:[108,116], hips:[124,132], height:[194,200]},
    {size:"3XL", chest:[128,136], waist:[116,124], hips:[132,140], height:[200,206]}
  ];
  const normalizeBodySize = size => String(size).toUpperCase() === "XXL" ? "2XL" : String(size).toUpperCase();
  function bodySizeRows(product) {
    if (!product || !["tee-sila-i-chest", "tee-vzglyad"].includes(product.id)) return [];
    return (product.sizes || []).map(size => {
      const row = BODY_SIZE_CHART.find(item => item.size === normalizeBodySize(size));
      return row ? { ...row, size: String(size) } : null;
    }).filter(Boolean);
  }
  function renderBodySizeGuide(product) {
    const rows = bodySizeRows(product);
    const range = values => values.join("–");
    const isFitted = product && product.id === "tee-vzglyad";
    const cutNote = isFitted
      ? "У модели «ВЗГЛЯД» прилегающий крой: для плотной посадки бери свой размер по таблице, для более свободной — на размер больше."
      : "Значения в таблице — обхваты тела в сантиметрах (грудь, талия, бёдра, рост). У модели «СИЛА И ЧЕСТЬ» прямой свободный крой.";
    $("#measurementTable").innerHTML = rows.length ? `<div class="measurement-table body-measurements" role="table" aria-label="Таблица параметров тела в сантиметрах"><div class="measurement-head" role="row"><span>РАЗМЕР</span><span>ГРУДЬ</span><span>ТАЛИЯ</span><span>БЁДРА</span><span>РОСТ</span></div>${rows.map(row => `<div role="row"><strong>${escapeHTML(row.size)}</strong><span>${range(row.chest)}</span><span>${range(row.waist)}</span><span>${range(row.hips)}</span><span>${range(row.height)}</span></div>`).join("")}</div><p class="size-guide-note">${escapeHTML(cutNote)}</p>` : "<p class=\"size-guide-note\">Таблица для этой вещи пока недоступна.</p>";
  }

  function renderSheet(product) {
    state.currentProduct = product;
    state.galleryGroup = "studio";
    state.stage = "photo";
    renderBodySizeGuide(product);
    $$(".product-more").forEach(detail => { detail.open = false; });
    $(".sheet-image").classList.toggle("studio-media", product.spin_kind === "visualization");
    $("#view3dHint").textContent = product.spin_kind === "visualization"
      ? "ВИЗУАЛИЗАЦИЯ · ТЯНИ В СТОРОНЫ" : "ТЯНИ В СТОРОНЫ";
    const hasReal3d = ["tee-sila-i-chest", "tee-vzglyad"].includes(product.id);
    if ($("#stage3d")) $("#stage3d").textContent = hasReal3d ? "3D" : "360°";
    if ($("#stagePhoto")) $("#stagePhoto").textContent = "ФОТО";
    if ($("#stageVideo")) $("#stageVideo").textContent = "360° ВИДЕО";
    const preferred = (product.sizes || []).find(size => normalizeBodySize(size) === normalizeBodySize((state.profile.size || "").trim())) || "";
    state.selectedSize = preferred && (product.sizes || []).includes(preferred)
      ? preferred
      : ((product.sizes || []).length === 1 ? product.sizes[0] : null);
    state.qty = 1;
    $("#qtyValue").textContent = "1";
    renderGallery(product);
    const productVideo = $("#sheetVideo");
    pauseProductVideo();
    $("#stageVideo").classList.toggle("hidden", !product.video);
    $("#stage3d").classList.toggle("hidden", !Array.isArray(product.spin) || product.spin.length < 2);
    const sourceTurnVideo = String(product.video || "").includes("front-to-back");
    $("#sheetVideo").classList.toggle("sheet-video-portrait", String(product.video || "").includes("concrete"));
    $("#sheetVideo").classList.toggle("sheet-video-source", sourceTurnVideo);
    if (product.video) {
      if (productVideo.getAttribute("src") !== product.video) productVideo.src = product.video;
      productVideo.poster = product.card_image || product.image || "";
      productVideo.setAttribute("aria-label", sourceTurnVideo ? `${product.name} — исходное видео модели от переда к спине, без звука` : `${product.name} — видеообзор без звука`);
    } else {
      productVideo.removeAttribute("src");
      productVideo.removeAttribute("poster");
      productVideo.load();
    }
    $("#sheetBadge").textContent = product.badge || "БАЗА";
    $("#sheetKicker").textContent = `${categoryName(product.category).toUpperCase()} / ДРОП 001`;
    $("#sheetTitle").textContent = product.name;
    const sign = $("#sheetSign");
    if (sign) {
      sign.textContent = product.signature || "Сила и честь. Бери размер, пока есть.";
    }
    const gift = !Core.buyable(product);
    $("#sheetPrice").textContent = gift ? "Бесплатно · в подарок" : product.price;
    $(".size-block").classList.toggle("hidden", gift);
    $(".qty-row").classList.toggle("hidden", gift);
    // Каталог пока не сообщает, какие отдельные размеры закончились.
    $("#waitlistButton").classList.add("hidden");
    $("#sheetDescription").textContent = product.description;
    $("#careMaterial").textContent = product.material ? `Состав: ${product.material}.` : "Состав материала уточняется.";
    $$(".product-care details").forEach(item => { item.open = false; });
    const fitFact = product.fit ? `<div class="fact-row"><span>Посадка</span><span>${escapeHTML(product.fit)}</span></div>` : "";
    $("#sheetFacts").innerHTML = `${fitFact}<div class="fact-row"><span>Материал</span><span>${escapeHTML(product.material || "Уточняется")}</span></div><div class="fact-row"><span>Статус</span><span>${escapeHTML(product.stock_check_unavailable ? "Наличие уточняется" : (product.stock_label || "В наличии"))}</span></div>`;
    $("#preorderNotice").classList.toggle("hidden", !/предзаказ/i.test(`${product.badge || ""} ${product.stock_label || ""}`));
    renderPassport(product);
    $("#sizeList").innerHTML = (product.sizes || []).map(size => `<button class="size-button ${state.selectedSize === size ? "selected" : ""}" data-size="${escapeHTML(size)}" aria-pressed="${state.selectedSize === size}" type="button">${escapeHTML(size)}</button>`).join("");
    $("#sizeHint").textContent = product.stock_check_unavailable ? "Не удалось проверить наличие размеров. Попробуй позже." : state.selectedSize ? `Размер ${state.selectedSize} выбран.` : (product.sizes || []).length ? "Выбери размер вручную или нажми подходящий вариант в калькуляторе выше." : "Наличие размеров пока уточняется.";
    $("#sizeHint").classList.remove("error");
    const hasApparelSizes = (product.sizes || []).some(s => ["S", "M", "L", "XL", "XXL", "2XL", "3XL"].includes(String(s).toUpperCase()));
    const guideToggle = $("#sizeGuideToggle");
    if (guideToggle) {
      guideToggle.classList.toggle("hidden", !hasApparelSizes);
      guideToggle.setAttribute("aria-expanded", "false");
      guideToggle.textContent = "ТАБЛИЦА В СМ ▾";
    }
    const guide = $("#sizeGuide");
    if (guide) guide.classList.add("hidden");
    resetFitFinder(product);
    $("#sheetDetails").innerHTML = product.media_note
      ? `<strong>ЧЕСТНО О МЕДИА</strong><p class="media-note">${escapeHTML(product.media_note)}</p>`
      : "";
    renderPersonalization(product);
    sheetProduct = product;
    drawPersonPreview();
    const others = state.data.products.filter(item => item.id !== product.id && Core.buyable(item));
    const related = others.filter(item => item.category === product.category).slice(0, 2);
    if (!related.length) related.push(...others.slice(0, 2));
    $("#related").innerHTML = related.length
      ? `<h4>ЕЩЁ В ДРОПЕ</h4>${related.map(item => `<button type="button" class="related-card" data-related="${escapeHTML(item.id)}"><img src="${escapeHTML(imageFor(item))}" alt="${escapeHTML(item.name)}"><span>${escapeHTML(item.name)}</span></button>`).join("")}`
      : "";
    iconize($("#productModal"));
    updatePurchaseSummary();
  }

  function rememberViewed(id) {
    state.viewed = [id, ...state.viewed.filter(item => item !== id)].slice(0, 8);
    saveJSON("vorozhbitov_viewed", state.viewed);
  }

  function openProduct(id, photoIndex) {
    const product = productById(id);
    if (!product) return;
    rememberViewed(id);
    renderSheet(product);
    openModal("productModal");
    $(".product-scroll").scrollTop = 0;
    if (Number.isInteger(photoIndex)) {
      setStage("photo");
      setGallery(photoIndex);
    } else setStage(Array.isArray(product.spin) && product.spin.length > 1 ? "3d" : "photo");
    haptic("light");
  }

  function renderPersonalization(product) {
    const row = $("#personRow");
    if (!row) return;
    const config = product && product.personalization;
    if (!config) {
      row.classList.add("hidden");
      if ($("#personInput")) $("#personInput").value = "";
      return;
    }
    row.classList.remove("hidden");
    if ($("#personLabel")) $("#personLabel").textContent = config.label || "ПЕРСОНАЛИЗАЦИЯ";
    const input = $("#personInput");
    if (input) {
      input.value = "";
      input.placeholder = config.placeholder || "";
      input.classList.remove("error");
    }
    if ($("#personHint")) $("#personHint").textContent = config.hint || "";
  }

  /** Значение персонализации: строка, "" если не нужно, false если ввод неверный. */
  function personValue(product) {
    const config = product && product.personalization;
    const input = $("#personInput");
    if (!config || !input) return "";
    const value = String(input.value || "").trim();
    if (!value) {
      if (config.optional) return "";
      $("#personHint").textContent = "Заполни это поле.";
      input.classList.add("error");
      haptic("error");
      return false;
    }
    if (config.pattern && !new RegExp(config.pattern).test(value)) {
      $("#personHint").textContent = "Только цифры, до пяти знаков.";
      input.classList.add("error");
      haptic("error");
      return false;
    }
    return value;
  }

  function addToCart() {
    if (state.checkoutPending) return showToast("Подожди подтверждения заказа. Корзина сохранена.");
    if (!state.catalogReady) return showToast("Обнови каталог, чтобы проверить цену и наличие. Корзина сохранена.");
    const product = state.currentProduct && productById(state.currentProduct.id);
    if (!product) return showToast("Вещь больше недоступна. Обнови каталог.");
    if (product.stock_check_unavailable) return showToast("Наличие уточняется. Обнови каталог перед добавлением.");
    if (!Core.buyable(product)) return showToast("Жетон не продаётся отдельно: подарок доступен в первом дропе, пока есть жетоны из первой сотни.");
    const dropGate = dropPurchaseGate();
    if (dropGate) { showToast(dropGate); haptic("error"); return; }
    if (!state.selectedSize) {
      $("#sizeHint").textContent = "Сначала выбери размер.";
      $(".size-block").scrollIntoView({block:"center", behavior:reducedMotion() ? "instant" : "smooth"});
      $("#sizeList button")?.focus({preventScroll:true});
      $("#sizeHint").classList.add("error");
      haptic("error");
      return;
    }
    if (!Core.available(product) || !product.sizes.includes(state.selectedSize)) return showToast("Этот размер больше недоступен. Выбери другой размер.");
    const person = personValue(product);
    if (person === false) return;
    // Разные номера жетона — разные строки заказа, поэтому номер входит в ключ.
    const key = `${product.id}::${state.selectedSize}${person ? `::${person}` : ""}`;
    const existing = state.cart.find(item => item.key === key);
    if (existing && existing.qty + state.qty > Core.MAX_QUANTITY) return showToast("В одной позиции — не больше 20 штук.");
    if (!existing && state.cart.length >= 20) return showToast("В одном заказе — не больше 20 позиций.");
    if (existing) existing.qty += state.qty;
    else state.cart.push({ key, id: product.id, size: state.selectedSize, qty: state.qty, person: person || "" });
    saveJSON("vorozhbitov_cart", state.cart);
    updateCounters();
    closeModal("productModal");
    haptic("success");
    showToast("Добавлено в корзину.");
    setTimeout(() => openCart(), 160);
  }

  async function sendWaitlist() {
    const product = state.currentProduct;
    if (!product) return;
    const size = state.selectedSize;
    if (!size) {
      $("#sizeHint").textContent = "Выбери размер, который ждёшь.";
      $("#sizeHint").classList.add("error");
      return;
    }
    const button = $("#waitlistButton");
    if (button.disabled) return;
    button.disabled = true;
    try {
      await checkoutClient.waitlist({ product_id: product.id, size }, tg && tg.initData);
      const list = loadJSON("vorozhbitov_waitlist", []);
      const key = `${product.id}::${size}`;
      if (!list.includes(key)) list.push(key);
      saveJSON("vorozhbitov_waitlist", list);
      showToast("Размер записан. Напишем в Telegram, когда вернётся.");
      haptic("success");
    } catch (error) {
      showToast(error.message || "Не получилось записать размер. Попробуй ещё раз.");
      haptic("error");
    } finally {
      button.disabled = false;
    }
  }

  function cartItems() {
    return state.cart.map(item => ({ item, product: productById(item.id) })).filter(pair => pair.product);
  }

  function cartTotal() {
    return cartItems().reduce((sum, { item, product }) => {
      if (!Core.available(product) || !(product.sizes || []).includes(item.size)) return sum;
      return sum + priceNumber(product.price) * item.qty;
    }, 0);
  }

  function renderCheckoutGift() {
    const box = $("#checkoutGift");
    if (!box) return;
    const gift = state.data.gift;
    if (!gift) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    const available = Boolean(gift.available && Number(gift.remaining) > 0);
    box.classList.remove("hidden");
    box.classList.toggle("is-sold-out", !available);
    box.innerHTML = `${gift.image ? `<img src="${escapeHTML(gift.image)}" alt="">` : ""}<div><span class="section-kicker">ПОДАРОК К ЗАКАЗУ</span><strong>${escapeHTML(gift.name || "Жетон")}</strong><small>${available ? "Подарок из первой сотни · наличие уточняется" : "Наличие подарка уточняется"}</small></div><b>${available ? "0 ₽" : "—"}</b>`;
  }

  function renderDeliverySummary() {
    const box = $("#checkoutDelivery");
    if (!box) return;
    const city = $("#checkoutCity").value.trim();
    const preorder = state.cart.map(item => productById(item.id)).filter(product => product
      && /предзаказ/i.test(`${product.badge || ""} ${product.stock_label || ""}`));
    const names = [...new Set(preorder.map(product => product.name))];
    box.innerHTML = `<strong>ПРЕДПОЧТЕНИЕ ПО ДОСТАВКЕ</strong>
      <dl><div><dt>${escapeHTML(state.deliver)}${city ? ` · ${escapeHTML(city)}` : ""}</dt><dd>Доступность, стоимость и срок ещё не подтверждены.</dd></div>
      <div><dt>Отправка</dt><dd>${names.length ? "После готовности предзаказа. Дата отправки уточняется." : "Дата передачи перевозчику уточняется."}</dd></div></dl>
      ${names.length ? `<p class="checkout-preorder">Предзаказ: ${escapeHTML(names.join(", "))}. К сроку в пути добавляется ожидание готовности партии.</p>` : ""}
      <small>Стоимость и условия доставки уточняются до оплаты.</small>`;
  }

  function renderCart() {
    const content = $("#cartContent");
    $("#cartNext").classList.add("hidden");
    content.classList.remove("hidden");
    if (!state.catalogReady) {
      content.innerHTML = `<div class="cart-empty"><h3>${state.catalogLoading ? "Проверяем цены и размеры." : "Не удалось проверить каталог."}</h3><p>Сохранённые вещи на месте. Для оформления нужно подтвердить актуальные цены и наличие.</p><button class="button button-outline" data-cart-retry type="button" ${state.catalogLoading ? "disabled" : ""}>${state.catalogLoading ? "ПРОВЕРЯЕМ КАТАЛОГ…" : "ПОВТОРИТЬ ЗАГРУЗКУ"}</button></div>`;
      $("#checkoutForm").classList.add("hidden");
      return;
    }
    if (!state.cart.length) {
      content.innerHTML = `<div class="cart-empty cart-empty-brand"><div class="cart-empty-art"><video class="cart-empty-mascot" src="assets/mascot/steel-glasses-anthracite-20261112a.mp4" poster="assets/mascot/steel-glasses-anthracite-20261112a.jpg" autoplay loop muted playsinline webkit-playsinline preload="metadata" aria-hidden="true"></video><span>СИЛА И ЧЕСТЬ / 001</span></div><div class="cart-empty-copy"><span class="cart-empty-kicker">ПЕРВЫЙ ШАГ</span><h3>ВЫБЕРИ СВОЮ<br>ВЕЩЬ.</h3><p>Корзина пока пуста. Первый дроп уже в витрине.</p><button class="button button-primary" data-cart-shop type="button">ОТКРЫТЬ ВИТРИНУ <span aria-hidden="true">↗</span></button></div></div>`;
      $("#checkoutForm").classList.add("hidden");
      return;
    }
    let unavailable = false;
    let stockUnknown = false;
    content.innerHTML = state.cart.map(item => {
      const product = productById(item.id);
      const unknown = Boolean(product?.stock_check_unavailable);
      if (unknown) stockUnknown = true;
      const ready = product && !unknown && Core.available(product) && product.sizes.includes(item.size);
      if (!ready) unavailable = true;
      const image = product ? `<img class="cart-line-image" src="${escapeHTML(imageFor(product))}" alt="">` : '<span class="cart-line-image unavailable-mark" aria-hidden="true">—</span>';
      return `<div class="cart-line ${ready ? "" : "is-unavailable"}" data-cart-key="${escapeHTML(item.key)}">${image}<div class="cart-line-name"><strong>${escapeHTML(product ? product.name : "Вещь недоступна")}</strong><small>Размер: ${escapeHTML(item.size)}${item.person ? ` · Номер: ${escapeHTML(item.person)}` : ""}</small>${ready ? `<div class="qty-control"><button data-qty="minus" type="button" aria-label="Уменьшить">−</button><span>${item.qty}</span><button data-qty="plus" type="button" aria-label="Увеличить" ${item.qty >= Core.MAX_QUANTITY ? "disabled" : ""}>+</button></div>` : unknown ? '<small class="unavailable-note">Не удалось проверить наличие. Попробуй обновить каталог.</small>' : '<small class="unavailable-note">Удали эту позицию или выбери доступный размер.</small>'}</div><div class="cart-line-end"><strong>${ready ? rubles(priceNumber(product.price) * item.qty) : unknown ? "Наличие уточняется" : "Недоступно"}</strong><button class="remove-line" data-remove-key="${escapeHTML(item.key)}" type="button">УДАЛИТЬ</button></div></div>`;
    }).join("");
    if (unavailable) {
      state.cartStep = "items";
      content.insertAdjacentHTML("beforeend", stockUnknown
        ? '<p class="cart-warning" role="status">Не удалось проверить наличие. Корзина сохранена; обнови каталог и попробуй снова.</p><button class="button button-outline" data-cart-retry type="button">ОБНОВИТЬ КАТАЛОГ</button>'
        : '<p class="cart-warning" role="status">Состав изменился. Удали недоступные позиции, чтобы продолжить.</p>');
    }
    $("#cartTotal").textContent = rubles(cartTotal());
    $("#submitOrder").disabled = unavailable || state.checkoutPending;
    $("#submitManagerOrder").disabled = unavailable || state.checkoutPending;
    $$("#cartContent button, #checkoutForm input, #checkoutForm .filter-chip").forEach(node => { node.disabled = state.checkoutPending; });
    $$("#cartContent [data-qty=plus]").forEach(node => { const item = state.cart.find(row => row.key === node.closest("[data-cart-key]").dataset.cartKey); node.disabled = state.checkoutPending || item.qty >= Core.MAX_QUANTITY; });
    const editing = state.cartStep === "details";
    $("#checkoutForm").classList.toggle("hidden", !editing);
    $("#cartNext").classList.toggle("hidden", editing);
    content.classList.toggle("hidden", editing);
    $("#cartPreviewTotal").textContent = rubles(cartTotal());
    const orderContactReady = /^[A-Za-z0-9_]{5,32}$/.test(String(state.data.order_manager_username || "").replace(/^@/, ""));
    $("#orderUnavailable").classList.toggle("hidden", orderContactReady);
    $("#beginCheckout").disabled = unavailable || state.checkoutPending || !orderContactReady;
    const preorderItems = state.cart.map(item => productById(item.id)).filter(product => product && /предзаказ/i.test(`${product.badge || ""} ${product.stock_label || ""}`));
    $("#cartPreorderNote").classList.toggle("hidden", !preorderItems.length);
    renderCheckoutGift();
    renderDeliverySummary();
  }

  function openCart() {
    state.cartStep = "items";
    applyProfileToCheckout();
    renderCart();
    openModal("cartModal");
    $(".cart-sheet").scrollTop = 0;
  }

  function updateCartItem(key, delta) {
    if (state.checkoutPending) return showToast("Подожди подтверждения заказа. Корзина сохранена.");
    const line = state.cart.find(item => item.key === key);
    if (!line) return;
    if (line.qty + delta > Core.MAX_QUANTITY) return showToast("В одной позиции — не больше 20 штук.");
    line.qty += delta;
    if (line.qty <= 0) state.cart = state.cart.filter(item => item.key !== key);
    saveJSON("vorozhbitov_cart", state.cart);
    renderCart();
    updateCounters();
  }

  function removeCartItem(key) {
    if (state.checkoutPending) return showToast("Подожди подтверждения заказа. Корзина сохранена.");
    state.cart = state.cart.filter(item => item.key !== key);
    saveJSON("vorozhbitov_cart", state.cart);
    renderCart();
    updateCounters();
    showToast("Вещь убрана из корзины.");
  }

  let mainButtonBound = false;

  function syncMainButton() {
    if (!tg || !tg.MainButton) return;
    const cartCount = state.cart.reduce((sum, item) => sum + Number(item.qty || 0), 0);
    const cartOpen = state.modalStack.length > 0 || document.body.classList.contains("welcoming") || document.body.classList.contains("booting");
    try {
      if (cartCount > 0 && !cartOpen) {
        tg.MainButton.setText(cartCount === 1 ? "КОРЗИНА" : `КОРЗИНА · ${cartCount}`);
        tg.MainButton.show();
        if (!mainButtonBound && typeof tg.MainButton.onClick === "function") {
          tg.MainButton.onClick(() => openCart());
          mainButtonBound = true;
        }
      } else {
        tg.MainButton.hide();
      }
      if (typeof tg.enableClosingConfirmation === "function" && typeof tg.disableClosingConfirmation === "function") {
        if (cartCount > 0) tg.enableClosingConfirmation();
        else tg.disableClosingConfirmation();
      }
    } catch (_) { /* old clients */ }
  }

  function updateCounters() {
    const cartCount = state.cart.reduce((sum, item) => sum + Number(item.qty || 0), 0);
    [$("#cartCount"), $("#bottomCartCount")].forEach(node => {
      if (node) { node.textContent = cartCount; node.classList.toggle("hidden", cartCount === 0); }
    });
    if ($("#profileCartN")) $("#profileCartN").textContent = String(cartCount);
    syncMainButton();
  }

  function updateFilterCount() {
    let count = 0;
    if (state.stock !== "all") count += 1;
    if (state.sizeFilter !== "all") count += 1;
    $("#filterCount").textContent = count;
    $("#filterCount").classList.toggle("hidden", count === 0);
  }

  let payPollTimer = null;
  let payWatchId = "";
  let payPollGeneration = 0;

  function stopPayPoll() {
    payPollGeneration += 1;
    if (payPollTimer) {
      clearInterval(payPollTimer);
      payPollTimer = null;
    }
  }

  function markPaidUi(orders) {
    stopPayPoll();
    haptic("success");
    $("#payPendingView").classList.add("hidden");
    $("#paySuccessView").classList.remove("hidden");
    $("#payModal .pay-sheet").classList.add("is-paid");
    $("#payTitle").textContent = "ГОТОВО.";
    const orderIds = [...new Set((orders || []).map(row => row.id).filter(Boolean))];
    const orderLabel = $("#paySuccessOrder");
    orderLabel.textContent = orderIds.length ? `ЗАКАЗ ${orderIds.map(id => `№ ${id}`).join(", ")}` : "";
    orderLabel.classList.toggle("hidden", !orderIds.length);
    $("#paySuccessDetail").textContent = "Оплата подтверждена. Статус заказа доступен в профиле; информация о доставке появится после её подтверждения.";
    loadProfileOrders();
  }

  function startPayPoll(paymentId) {
    stopPayPoll();
    payWatchId = paymentId || "";
    const initData = tg && tg.initData;
    if (!payWatchId || !initData) return;
    let attempts = 0;
    let checking = false;
    const generation = payPollGeneration;
    const check = async () => {
      // Do not burn battery/traffic while hidden, and stop after ~10 minutes:
      // the invoice stays payable from «Мои покупки».
      if (document.hidden || checking || generation !== payPollGeneration) return;
      attempts += 1;
      if (attempts > 150) { stopPayPoll(); $("#payStatus").textContent = "Проверка приостановлена · статус доступен в профиле"; return; }
      checking = true;
      try {
        const response = await fetch("/api/my-orders", {
          headers: { Accept: "application/json", "X-Telegram-Init-Data": initData }
        });
        if (!response.ok) return;
        const payload = await response.json();
        if (generation !== payPollGeneration) return;
        const orders = (payload.orders || []).filter(row => row.payment_id === payWatchId);
        if (orders.some(row => row.payment_status === "paid")) markPaidUi(orders);
        else if (orders.some(row => row.payment_status === "refund_required")) {
          stopPayPoll();
          $("#payStatus").textContent = "Требуется возврат · открой чат магазина";
          $("#payMethods").classList.add("hidden");
          $("#payNote").textContent = "Проверь заказ в профиле и открой чат магазина для решения вопроса.";
        } else if (orders.some(row => ["cancelled", "expired", "refunded"].includes(row.payment_status))) {
          stopPayPoll();
          $("#payStatus").textContent = "Счёт больше не активен · проверь заказ в профиле";
          $("#payMethods").classList.add("hidden");
        }
      } catch (_) { /* keep waiting */ }
      finally { checking = false; }
    };
    payPollTimer = setInterval(check, 4000);
    check();
  }

  function openPayLink(url) {
    if (!url) return false;
    if (tg && typeof tg.openLink === "function") {
      try { tg.openLink(url); return true; } catch (_) { /* fall through */ }
    }
    window.open(url, "_blank", "noopener,noreferrer");
    return true;
  }

  function startPayMethod(method, url, kind) {
    if (kind === "invoice" && url && tg && typeof tg.openInvoice === "function") {
      try {
        const activePayment = payWatchId;
        tg.openInvoice(url, status => {
          if (!state.modalStack.includes("payModal") || payWatchId !== activePayment) return;
          if (status === "paid") { $("#payStatus").textContent = "Проверяем подтверждение оплаты…"; startPayPoll(payWatchId); }
          else if (status === "cancelled") { $("#payStatus").textContent = "Оплата отменена · можно выбрать способ снова"; showToast("Оплата отменена. Счёт сохранён."); }
          else { $("#payStatus").textContent = "Ожидаем подтверждение оплаты"; showToast("Проверь статус или попробуй другой способ."); }
        });
        return;
      } catch (_) { /* fall through to link */ }
    }
    if (url && openPayLink(url)) {
      $("#payStatus").textContent = "Ожидаем подтверждение оплаты";
      showToast("Окно оплаты открыто. Статус обновится сам.");
      return;
    }
    showToast("Этот способ сейчас недоступен. Кнопки также в чате с ботом.");
  }

  function showPaySheet(data) {
    $("#payModal .pay-sheet").classList.remove("is-paid");
    $("#payTitle").textContent = "ВЫБЕРИ СПОСОБ.";
    $("#payPendingView").classList.remove("hidden");
    $("#paySuccessView").classList.add("hidden");
    $("#payStatus").textContent = "Счёт создан · ожидает оплаты";
    $("#payMethods").classList.remove("hidden");
    $("#payNote").textContent = "Статус обновится сам. Если закроешь окно, вернись к оплате в профиле, в разделе заказов.";
    const amount = $("#payAmount");
    if (amount) amount.textContent = data.amount_label || "—";
    const linesBox = $("#payLines");
    if (linesBox) {
      const lines = Array.isArray(data.lines) ? data.lines : [];
      linesBox.innerHTML = lines.map(line => {
        const qty = Number(line.quantity || 1);
        const extra = qty > 1 ? ` · ${qty} шт.` : "";
        return `<li><span>${escapeHTML(line.name || "Вещь")}${line.size ? ` · ${escapeHTML(line.size)}` : ""}</span><span>${escapeHTML(extra)}</span></li>`;
      }).join("") + (data.gift ? `<li class="pay-gift-line"><span>Подарок: ${escapeHTML(data.gift.name || "Жетон")}${data.gift.serial_label ? ` · №${escapeHTML(data.gift.serial_label)}` : ""}</span><span>0 ₽</span></li>` : "");
      linesBox.classList.toggle("hidden", !lines.length && !data.gift);
    }
    const box = $("#payMethods");
    if (box) {
      const methods = Array.isArray(data.methods) ? data.methods : [];
      if (!methods.length) {
        $("#payTitle").textContent = "ОПЛАТА НЕДОСТУПНА.";
        $("#payStatus").textContent = "Счёт создан · способы оплаты пока недоступны";
        $("#payNote").textContent = "Заказ сохранён. Проверить статус и попробовать снова можно в профиле.";
        box.innerHTML = `<p class="pay-lead">Не удалось получить способы оплаты. Попробуй ещё раз позже.</p>${data.payment_id ? `<button class="button button-outline" type="button" data-pay-retry="${escapeHTML(data.payment_id)}">ПОВТОРИТЬ ↻</button>` : ""}`;
      } else {
        box.innerHTML = methods.map(method => {
          const title = escapeHTML(method.title || method.id);
          const hint = escapeHTML(method.hint || "");
          const id = escapeHTML(method.id || "");
          const url = escapeHTML(method.url || "");
          const kind = escapeHTML(method.kind || "link");
          return `<button class="pay-card" type="button" data-pay-method="${id}" data-pay-url="${url}" data-pay-kind="${kind}"><span class="pay-mark" aria-hidden="true"></span><span class="pay-copy"><strong>${title}</strong>${hint ? `<small>${hint}</small>` : ""}</span><span class="pay-go">↗</span></button>`;
        }).join("");
      }
    }
    closeModal("reviewModal");
    closeModal("cartModal");
    closeModal("profileModal");
    openModal("payModal");
    startPayPoll(data.payment_id || "");
  }

  async function resumePayment(paymentId) {
    const initData = tg && tg.initData;
    if (!initData) {
      showToast("Оплата — в чате с ботом, «Мои покупки».");
      return;
    }
    try {
      const response = await fetch("/api/pay", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Telegram-Init-Data": initData
        },
        body: JSON.stringify({ payment_id: paymentId })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        showToast(data.error || "Не получилось открыть оплату.");
        haptic("error");
        return;
      }
      showPaySheet(data);
      return true;
    } catch (_) {
      showToast("Сеть не ответила. Попробуй ещё раз.");
    }
    return false;
  }

  function checkoutFieldError(id, message) {
    const field = $("#" + id);
    const error = $("#" + id + "Error");
    if (field) {
      field.setAttribute("aria-invalid", "true");
      field.focus({ preventScroll: true });
      field.scrollIntoView({ behavior: reducedMotion() ? "instant" : "smooth", block: "center" });
    }
    if (error) { error.textContent = message; error.classList.remove("hidden"); }
    haptic("error");
    return null;
  }

  function clearCheckoutFieldError(field) {
    field.removeAttribute("aria-invalid");
    const error = $("#" + field.id + "Error");
    if (error) { error.textContent = ""; error.classList.add("hidden"); }
  }

  function askDelivery() {
    const city = $("#checkoutCity").value.trim();
    const lines = cartItems().map(({item, product}) => `${product.name} · размер ${item.size} · ${item.qty} шт.`);
    const text = ["Здравствуйте! Подскажите стоимость и срок доставки до оплаты.",
      ...lines, `Способ: ${state.deliver}`, city ? `Город: ${city}` : "Город: "].join("\n");
    const url = checkoutClient.managerUrl(state.data.support_username, text);
    if (!url) { showToast("Контакт магазина пока не подключён."); return; }
    if (tg && typeof tg.openTelegramLink === "function") tg.openTelegramLink(url);
    else window.open(url, "_blank", "noopener,noreferrer");
    showToast("Проверь вопрос и отправь его в чат магазина.");
    haptic("light");
  }

  function prepareOrder() {
    const pairs = cartItems();
    if (!state.catalogReady) { showToast("Подожди загрузку каталога."); return null; }
    if (pairs.length !== state.cart.length || pairs.some(({item, product}) => product.stock_check_unavailable || !Core.available(product) || !product.sizes.includes(item.size))) {
      renderCart();
      showToast("Проверь недоступные позиции в корзине.");
      return null;
    }
    const dropGate = dropPurchaseGate();
    if (dropGate) { showToast(dropGate); return null; }
    const name = $("#checkoutName").value.trim();
    const phone = $("#checkoutPhone").value.trim();
    const city = $("#checkoutCity").value.trim();
    const address = ($("#checkoutAddress") && $("#checkoutAddress").value.trim()) || "";
    const note = $("#checkoutNote").value.trim();
    const consent = $("#checkoutConsent").checked;
    if (!pairs.length) { showToast("Добавь хотя бы одну вещь."); return null; }
    if (name.length < 2) return checkoutFieldError("checkoutName", "Укажи имя — не меньше двух букв.");
    if (phone.replace(/\D/g, "").length < 10) return checkoutFieldError("checkoutPhone", "Проверь номер телефона.");
    if (city.length < 2) return checkoutFieldError("checkoutCity", "Укажи город доставки.");
    if (!consent) return checkoutFieldError("checkoutConsent", "Подтверди согласие на обработку данных.");

    persistProfile({
      ...state.profile,
      name, phone, city,
      address: address || state.profile.address,
      note: note || state.profile.note,
      deliver: state.deliver
    }, false);
    const customer = {
      name, phone, city,
      address: address || state.profile.address || "",
      entrance: state.profile.entrance || "",
      deliver: state.deliver,
      note
    };
    const payload = {
      type: "order",
      customer,
      consent: true,
      items: pairs.map(({ item, product }) => {
        const line = { product_id: product.id, size: item.size, quantity: item.qty };
        if (item.person) line.person = item.person;
        return line;
      })
    };
    return { pairs, payload };
  }

  function reviewOrder() {
    const prepared = prepareOrder();
    if (!prepared) return;
    const customer = prepared.payload.customer;
    $("#reviewLines").innerHTML = prepared.pairs.map(({ item, product }) => `<div class="review-line"><img src="${escapeHTML(imageFor(product))}" alt=""><div><strong>${escapeHTML(product.name)}</strong><small>Размер ${escapeHTML(item.size)} · ${Number(item.qty)} шт.${item.person ? ` · номер ${escapeHTML(item.person)}` : ""}</small></div><b>${rubles(priceNumber(product.price) * item.qty)}</b></div>`).join("");
    const gift = state.data.gift;
    const showGift = Boolean(gift && gift.available && Number(gift.remaining) > 0);
    $("#reviewGift").classList.toggle("hidden", !showGift);
    $("#reviewGift").textContent = showGift ? `Подарок: ${gift.name || "Жетон"} · наличие уточняется` : "";
    $("#reviewCustomer").textContent = [customer.name, customer.phone].filter(Boolean).join(" · ");
    $("#reviewDelivery").textContent = [`Предпочтение: ${customer.deliver}`, customer.city, customer.address].filter(Boolean).join(" · ");
    $("#reviewTotal").textContent = rubles(cartTotal());
    $("#reviewError").classList.add("hidden");
    openModal("reviewModal");
  }

  function submitManagerOrder() {
    if (state.checkoutPending) return;
    const prepared = prepareOrder();
    if (!prepared) return;
    try {
      const text = checkoutClient.managerMessage({
        customer: prepared.payload.customer,
        totalLabel: rubles(cartTotal()),
        giftName: state.data.gift && state.data.gift.available && Number(state.data.gift.remaining) > 0 ? state.data.gift.name || "Жетон" : "",
        lines: prepared.pairs.map(({ item, product }) => ({
          name: product.name,
          size: item.size,
          quantity: item.qty,
          priceLabel: rubles(priceNumber(product.price) * item.qty),
          person: item.person || ""
        }))
      });
      const url = checkoutClient.managerUrl(state.data.order_manager_username, text);
      if (!url) {
        showToast("Чат магазина пока не подключён. Корзина сохранена.");
        return;
      }
      if (tg && typeof tg.openTelegramLink === "function") tg.openTelegramLink(url);
      else window.open(url, "_blank", "noopener,noreferrer");
      haptic("light");
      showToast("Чат открыт. Проверь готовый текст и отправь его — без сообщения заказа нет.");
    } catch (error) {
      showToast(error.message || "Не удалось подготовить текст заказа. Корзина сохранена.");
    }
  }

  async function submitOrder() {
    if (state.checkoutPending || ($("#submitOrder") && $("#submitOrder").disabled)) return;
    const prepared = prepareOrder();
    if (!prepared) return;
    const { payload } = prepared;
    const button = $("#confirmOrder");
    const previous = button ? button.innerHTML : "";
    state.checkoutPending = true;
    renderCart();
    if (button) {
      button.disabled = true;
      button.textContent = "СОЗДАЮ СЧЁТ…";
    }
    try {
      const user = telegramUser();
      const data = await checkoutClient.submit(payload, tg && tg.initData, user ? String(user.id) : "");
      state.cart = [];
      saveJSON("vorozhbitov_cart", state.cart);
      updateCounters();
      if (data.status === "awaiting_payment") {
        haptic("success");
        showPaySheet(data);
      } else {
        closeModal("reviewModal");
        closeModal("cartModal");
        openProfile();
        showToast(data.status === "paid" ? "Этот заказ уже оплачен." : "Заказ уже обработан. Проверь его статус.");
      }
    } catch (error) {
      if (error.code === "gift_unavailable") loadCatalog();
      const message = error.message || "Нет подтверждения от сервера. Корзина сохранена.";
      $("#reviewError").textContent = message;
      $("#reviewError").classList.remove("hidden");
      showToast(message);
      haptic("error");
    } finally {
      state.checkoutPending = false;
      if (button) { button.innerHTML = previous; button.disabled = false; }
      renderCart();
    }
  }

  function openChannel() {
    const url = state.data.channel_url || FALLBACK_CATALOG.channel_url;
    if (tg && typeof tg.openTelegramLink === "function" && /^https:\/\/t\.me\//.test(url)) tg.openTelegramLink(url);
    else window.open(url, "_blank", "noopener,noreferrer");
  }

  function openLightbox(src, caption) {
    setLightboxZoom(false);
    $("#lightboxImage").src = src;
    $("#lightboxImage").alt = caption || "";
    $("#lightboxCaption").textContent = caption || "";
    openModal("lightbox");
  }

  function centerLightboxImage() {
    const viewport = $("#lightboxViewport");
    viewport.scrollTo((viewport.scrollWidth - viewport.clientWidth) / 2,
      (viewport.scrollHeight - viewport.clientHeight) / 2);
  }

  function setLightboxZoom(zoomed) {
    const viewport = $("#lightboxViewport");
    viewport.classList.toggle("zoomed", zoomed);
    $("#lightboxZoom").setAttribute("aria-pressed", String(zoomed));
    $("#lightboxZoom").textContent = zoomed ? "Весь кадр −" : "Увеличить +";
    $("#lightboxHint").classList.toggle("hidden", !zoomed);
    viewport.setAttribute("aria-label", zoomed
      ? "Увеличенная фотография. Перемещай пальцем, мышью или стрелками."
      : "Фотография товара. Нажми «Увеличить», чтобы рассмотреть детали.");
    if (zoomed) window.requestAnimationFrame(centerLightboxImage);
    else viewport.scrollTo(0, 0);
  }

  function configureTelegram() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      if (typeof tg.disableVerticalSwipes === "function") tg.disableVerticalSwipes();
      if (typeof tg.enableClosingConfirmation === "function") tg.enableClosingConfirmation();
      if (typeof tg.setHeaderColor === "function") tg.setHeaderColor("#0c0c0e");
      if (typeof tg.setBackgroundColor === "function") tg.setBackgroundColor("#0c0c0e");
      if (tg.MainButton) tg.MainButton.hide();
      if (tg.BackButton && typeof tg.BackButton.onClick === "function") {
        tg.BackButton.onClick(() => closeTopModal());
      }
      const user = telegramUser();
      if (user && user.first_name) {
        if (!state.profile.name) state.profile.name = user.first_name;
        if ($("#checkoutName") && !$("#checkoutName").value) $("#checkoutName").value = state.profile.name || user.first_name;
      }
      applyProfileToCheckout();
    } catch (_) { /* browser preview */ }
  }

  function enterShop() {
    const welcome = $("#welcome");
    if (!welcome || welcome.classList.contains("is-leaving")) return;
    welcome.classList.add("is-leaving");
    stopWelcomeVideo();
    document.body.classList.remove("welcoming", "booting");
    haptic("medium");
    window.setTimeout(() => {
      welcome.classList.add("hidden");
      welcome.classList.remove("is-leaving");
      stopWelcomeVideo();
      syncModalLayers();
      syncMainButton();
    }, reducedMotion() ? 0 : 350);
  }

  function startExperience() {
    const boot = $("#boot");
    const welcome = $("#welcome");
    if (boot) boot.classList.add("hidden");
    welcome?.classList.remove("hidden");
    welcome?.classList.remove("is-leaving");
    document.body.classList.remove("booting");
    document.body.classList.add("welcoming");
    setupWelcomeVideo(mediaConfig());
    syncModalLayers();
    syncMainButton();
  }

  function renderReturnVisit() {
    const section = $("#resumeShopping");
    if (!section || returnVisit.dismissed || !state.catalogReady) return;
    const paymentId = "";
    const hasCart = !paymentId && returnVisit.cart && state.cart.length > 0;
    const product = !paymentId && !hasCart && returnVisit.viewed.map(productById).find(item => item && Core.buyable(item) && Core.available(item));
    if (!paymentId && !hasCart && !product) { section.classList.add("hidden"); return; }
    $("#resumeShoppingTitle").textContent = paymentId ? "ЗАКАЗ ЖДЁТ ОПЛАТЫ." : hasCart ? "ВЕЩИ ЖДУТ ТЕБЯ." : "ПРОДОЛЖИМ?";
    $("#resumeShoppingText").textContent = paymentId
      ? "У тебя есть действующий заказ. Продолжи оплату, когда удобно."
      : hasCart ? "Твоя корзина сохранена на этом устройстве. Проверь вещи и размер."
      : `Ты смотрел «${product.name}». Вернись к вещи, когда удобно.`;
    const action = $("#resumeShoppingAction");
    action.textContent = paymentId ? "ПРОДОЛЖИТЬ ОПЛАТУ ↗" : hasCart ? "ОТКРЫТЬ КОРЗИНУ ↗" : "ВЕРНУТЬСЯ К ВЕЩИ ↗";
    action.onclick = async () => {
      if (paymentId) {
        action.disabled = true;
        try {
          const opened = await resumePayment(paymentId);
          if (!opened) { await refreshReturnPayment(); return; }
        } finally { action.disabled = false; }
      }
      returnVisit.dismissed = true;
      section.classList.add("hidden");
      if (!paymentId) { if (hasCart) openCart(); else openProduct(product.id); }
    };
    $("#resumeShoppingDismiss").onclick = () => { returnVisit.dismissed = true; section.classList.add("hidden"); };
    section.classList.remove("hidden");
  }

  async function refreshReturnPayment() {
    const generation = ++returnVisit.generation;
    const initData = tg && tg.initData;
    if (!initData || returnVisit.dismissed) return;
    try {
      const response = await fetch("/api/my-orders", { headers: { Accept: "application/json", "X-Telegram-Init-Data": initData } });
      if (!response.ok) throw new Error("orders unavailable");
      const payload = await response.json();
      if (generation !== returnVisit.generation || returnVisit.dismissed) return;
      const orders = Array.isArray(payload.orders) ? payload.orders : [];
      const pending = orders.find(row => row && row.can_pay === true && row.payment_status === "pending" && typeof row.payment_id === "string" && row.payment_id);
      returnVisit.paymentId = pending ? pending.payment_id : "";
    } catch (_) {
      if (generation !== returnVisit.generation || returnVisit.dismissed) return;
      returnVisit.paymentId = "";
    }
    renderReturnVisit();
  }

  async function loadCatalog() {
    if (state.catalogLoading) return;
    state.catalogLoading = true;
    state.catalogReady = false;
    if (state.modalStack.includes("cartModal")) renderCart();
    if (state.currentProduct) updatePurchaseSummary();
    $("#catalogStatus").classList.remove("hidden");
    $("#catalogError").classList.add("hidden");
    $("#productGrid").setAttribute("aria-busy", "true");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/catalog", { headers: { Accept: "application/json" }, signal: controller.signal });
      if (!response.ok) throw new Error("catalog unavailable");
      const remote = await response.json();
      if (!remote || !Array.isArray(remote.products) || !Array.isArray(remote.categories)) throw new Error("invalid catalog");
      if (remote.products.some(product => !product || typeof product.id !== "string" || typeof product.name !== "string" || !Array.isArray(product.sizes))) throw new Error("invalid product");
      state.data = {
        ...FALLBACK_CATALOG, ...remote,
        products: remote.products.filter(product => product.active !== false),
        privacy_url: remote.privacy_url || "",
        support_username: remote.support_username || ""
      };
      state.catalogReady = true;
      const privacy = $("#privacyLink");
      const privacyBtn = $("#privacyInfoButton");
      if (privacy && state.data.privacy_url) {
        privacy.href = state.data.privacy_url;
        privacy.classList.remove("hidden");
        privacyBtn?.classList.add("hidden");
      } else {
        privacyBtn?.classList.remove("hidden");
      }
      $("#privacyNotice")?.classList.toggle("hidden", Boolean(state.data.privacy_url));
      $("#sizeFilters").innerHTML = ["all", ...Core.sizesFor(state.data.products)].map(size => `<button class="filter-chip ${size === state.sizeFilter ? "active" : ""}" data-size-filter="${escapeHTML(size)}" type="button" aria-pressed="${size === state.sizeFilter}">${size === "all" ? "Любой" : escapeHTML(size)}</button>`).join("");
      applyMedia();
      renderCategoryChips();
      renderProducts();
      renderDropEvent();
      renderCommunity();
      renderClub();
      renderMascotGift();
      renderReturnVisit();
      loadClubState();
    } catch (_) {
      $("#catalogError").classList.remove("hidden");
      renderProducts();
    } finally {
      clearTimeout(timeout);
      state.catalogLoading = false;
      if (state.modalStack.includes("cartModal")) renderCart();
      if (state.currentProduct) updatePurchaseSummary();
      $("#catalogStatus").classList.add("hidden");
      $("#productGrid").setAttribute("aria-busy", "false");
    }
  }

  /* --- Необязательная фоновая петля приветствия --- */

  function mediaConfig() {
    const media = (state.data && state.data.media) || {};
    const staleLoop = !media.welcome_loop || media.welcome_loop.includes("welcome-real-fighter-20261028.mp4") || media.welcome_loop.includes("welcome-seal-20261029.mp4") || media.welcome_loop.includes("welcome-loop-20261112a.mp4");
    const stalePoster = !media.welcome_poster || media.welcome_poster.includes("welcome-real-fighter-20261028.jpg") || media.welcome_poster.includes("welcome-seal-20261029.jpg") || media.welcome_poster.includes("welcome-poster-20261112a.jpg");
    return {
      welcomeLoop: media.welcome_motion === false ? "" : (staleLoop ? "assets/video/welcome-seal-20261112b.mp4" : media.welcome_loop),
      welcomePoster: stalePoster ? "assets/video/welcome-poster-20261112b.jpg" : media.welcome_poster
    };
  }

  function saverMode() {
    // Экономия трафика или медленная сеть — чёрный фон, видео не грузим.
    const conn = navigator.connection || {};
    if (conn.saveData) return true;
    return /(^|-)2g$/.test(String(conn.effectiveType || ""));
  }

  function reducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function renderMascotGift() {
    const section = $("#mascotGift");
    const gift = state.data.gift;
    const product = gift && productById(gift.product_id);
    if (!section) return;
    section.classList.toggle("hidden", !gift || !product);
    if (!gift || !product) return;
    const available = Boolean(gift.available && Number(gift.remaining) > 0);
    $("#mascotGiftDescription").textContent = available
      ? "К первому дропу — 100 подарочных жетонов с фирменной гравировкой. Со второго дропа подарочных жетонов не будет."
      : "Металлический жетон с фирменной гравировкой — знак первого дропа. Наличие подарка уточняется.";
    $("#mascotGiftStatus").textContent = available
      ? "Подарок из первой сотни · наличие уточняется"
      : "Наличие подарка уточняется";
    $("#mascotGiftLabel").textContent = available ? "ПОДАРОК К ЗАКАЗУ" : "ЖЕТОН ПЕРВОГО ДРОПА";
    $("#mascotGiftNote").textContent = available
      ? "Подарок доступен в первом дропе, пока есть жетоны из первой сотни."
      : "Подарочные жетоны предусмотрены для первого дропа; наличие уточняется.";
    $("#mascotGiftImage").src = gift.image || product.image;
    $("#mascotGiftDetails").onclick = () => openProduct(product.id);
    $("#mascotGiftShop").onclick = () => { scrollToId("catalog"); haptic("light"); };
    section.classList.toggle("gift-exhausted", !available);
  }

  function setupClubLifter() {
    const section = $("#club");
    const art = $("#clubLifter");
    const button = $("#clubLifterToggle");
    if (!section || !art || !button) return;
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const positions = [[0, 0], [33.333, 0], [66.667, 0], [100, 0],
      [0, 100], [33.333, 100], [66.667, 100], [100, 100]];
    let visible = false;
    let requested = null;
    let timer = null;
    let frame = 0;
    const draw = () => {
      const [x, y] = positions[frame];
      art.style.backgroundPosition = `${x}% ${y}%`;
    };
    const sync = () => {
      const wantsMotion = requested === null ? !query.matches && !saverMode() : requested;
      const shouldPlay = visible && !document.hidden && !document.body.classList.contains("welcoming")
        && !state.modalStack.length && wantsMotion;
      if (shouldPlay && timer === null) timer = window.setInterval(() => { frame = (frame + 1) % positions.length; draw(); }, 260);
      if (!shouldPlay && timer !== null) { window.clearInterval(timer); timer = null; }
      button.textContent = timer === null ? "▶" : "Ⅱ";
      button.setAttribute("aria-label", timer === null ? "Включить анимацию" : "Остановить анимацию");
      button.setAttribute("aria-pressed", String(timer !== null));
    };
    button.addEventListener("click", () => { requested = timer === null; sync(); });
    syncClubLifterMotion = sync;
    new IntersectionObserver(entries => { visible = entries[0].isIntersecting && entries[0].intersectionRatio >= .15; sync(); }, { threshold: .15 }).observe(art);
    document.addEventListener("visibilitychange", sync);
    query.addEventListener?.("change", () => { requested = null; sync(); });
    navigator.connection?.addEventListener?.("change", sync);
    draw();
    sync();
  }

  function applyMedia() {
    const media = mediaConfig();
    /* Approved founder image from page. */
    if (state.data.media && state.data.media.hero_background && $("#heroBackground")) $("#heroBackground").src = state.data.media.hero_background;
    if (state.data.media && state.data.media.hero_subject && $("#heroSubject")) $("#heroSubject").src = state.data.media.hero_subject;
    syncHeroPoster();
    if (!$("#welcome").classList.contains("hidden") && !$("#welcome").classList.contains("is-leaving")) setupWelcomeVideo(media);
    // Тираж на заставке берём из каталога, а не пишем руками.
    const stock = $("#welcomeStock");
    const lead = (state.data.products || []).find(p => Core.buyable(p) && (p.featured || p.real_photos) && p.stock_label);
    if (stock && lead) stock.textContent = String(lead.stock_label).toUpperCase();
  }

  const welcomePlayback = { source: "", ready: false, paused: false, blocked: false, failed: false, finished: false, bound: false, pending: false, watchdog: 0 };

  function welcomeCanPlay() {
    const welcome = $("#welcome");
    return welcomePlayback.ready && !welcomePlayback.paused && !welcomePlayback.blocked && !welcomePlayback.failed && !welcomePlayback.finished
      && !reducedMotion() && !document.hidden && !state.modalStack.length
      && !welcome.classList.contains("hidden") && !welcome.classList.contains("is-leaving");
  }

  function syncWelcomeVideo() {
    const video = $("#welcomeVideo");
    // Беззвучие — и атрибутом, и свойством: часть WebView для автоплея смотрит только на свойство.
    video.muted = true;
    video.defaultMuted = true;
    const control = $("#welcomeMotion");
    control.classList.toggle("hidden", !welcomePlayback.ready || reducedMotion());
    const waiting = welcomePlayback.finished || welcomePlayback.paused || welcomePlayback.blocked || welcomePlayback.failed;
    control.setAttribute("aria-label", welcomePlayback.finished ? "Повторить фильм" : waiting ? "Запустить фильм" : "Остановить фильм");
    const duration = Math.round(video.duration);
    $("#welcomeFrameCount").textContent = welcomePlayback.finished ? "ПОВТОРИТЬ ФИЛЬМ"
      : waiting ? "ЗАПУСТИТЬ ФИЛЬМ" : Number.isFinite(duration) && duration > 0 ? `ФИЛЬМ · ${duration} СЕК` : "ФИЛЬМ";
    control.setAttribute("aria-pressed", String(waiting));
    $("#welcomeMotionIcon").textContent = waiting ? "▷" : "Ⅱ";
    const canPlay = welcomeCanPlay();
    const fallback = $("#welcomeFallback");
    const isPlaying = video.classList.contains("is-playing");
    if (fallback) fallback.classList.toggle("hidden", isPlaying && !reducedMotion() && !welcomePlayback.failed);
    // Источник ставим сразу, не дожидаясь конца загрузки и гейта воспроизведения:
    // ролик буферизуется уже под бут-штампом и стартует мгновенно.
    if (!reducedMotion() && welcomePlayback.source && video.getAttribute("src") !== welcomePlayback.source) {
      video.src = welcomePlayback.source;
      video.load();
    }
    if (!canPlay) {
      video.pause();
      if (reducedMotion() || !welcomePlayback.source) video.classList.remove("is-playing");
      return;
    }
    if (!video.paused || welcomePlayback.pending) return;
    welcomePlayback.pending = true;
    const attempt = video.play();
    Promise.resolve(attempt).catch(error => {
      // Запрет автоплея (строгая политика WebView, энергосбережение iOS) — это не пауза:
      // помечаем отдельно, первый же жест снимет запрет и запустит фильм.
      if (error.name === "NotAllowedError") {
        welcomePlayback.blocked = true;
        syncWelcomeVideo();
      } else if (error.name !== "AbortError" && welcomeCanPlay()) {
        welcomePlayback.paused = true;
        syncWelcomeVideo();
      }
    }).finally(() => {
      welcomePlayback.pending = false;
      if (!welcomeCanPlay()) video.pause();
      // При запрете автоплея ретрай бесполезен — ждём жест, иначе цикл отказов.
      else if (video.paused && !welcomePlayback.blocked) syncWelcomeVideo();
    });
  }

  function setupWelcomeVideo(media) {
    const video = $("#welcomeVideo");
    // Беззвучие прописываем и свойством: без muted-флага WebView не даст автоплей.
    video.muted = true;
    video.defaultMuted = true;
    video.playsInline = true;
    video.autoplay = true;
    video.loop = false;
    video.removeAttribute("loop");
    // Каталог приходит позже старта и приносит тот же файл с новым ?v-штампом.
    // Уже идущий ролик не трогаем, иначе начало проиграется дважды.
    const playing = video.classList.contains("is-playing") || (!video.paused && video.currentTime > 0);
    if (welcomePlayback.source !== media.welcomeLoop && (!playing || !media.welcomeLoop)) {
      video.pause();
      video.classList.remove("is-playing");
      welcomePlayback.source = media.welcomeLoop;
      if (!media.welcomeLoop) {
        video.removeAttribute("src");
        video.load();
      }
      welcomePlayback.failed = false;
      welcomePlayback.blocked = false;
    }
    $("#welcomeFallback").src = media.welcomePoster;
    video.poster = media.welcomePoster;
    welcomePlayback.ready = Boolean(media.welcomeLoop);
    if (!welcomePlayback.bound) {
      welcomePlayback.bound = true;
      // На фирменном финале убираем кикер и крупный заголовок: остаётся кованый знак НВ в кадре и «НЕ НА СЛОВАХ.» над печатью.
      const syncWelcomeFinale = () => {
        const src = video.currentSrc || video.getAttribute("src") || welcomePlayback.source || "";
        const isBrandedFilm = /welcome-seal-/.test(src);
        const inFinale = isBrandedFilm && !welcomePlayback.failed
          && (welcomePlayback.finished || video.currentTime >= 4.55);
        $("#welcome")?.classList.toggle("is-logo-finale", inFinale);
      };
      $("#welcomeMotion").addEventListener("click", () => {
        if (welcomePlayback.finished) {
          welcomePlayback.finished = false;
          welcomePlayback.paused = false;
          welcomePlayback.blocked = false;
          video.currentTime = 0;
          syncWelcomeFinale();
          $("#welcomeFrameCount").textContent = `ФИЛЬМ · ${Math.round(video.duration)} СЕК`;
        } else if (welcomePlayback.failed) {
          welcomePlayback.failed = false;
          welcomePlayback.paused = false;
          welcomePlayback.blocked = false;
          video.load();
        } else if (welcomePlayback.blocked) {
          // Тап по кнопке со ▷ — пользовательский жест, он снимает запрет автоплея.
          welcomePlayback.blocked = false;
          welcomePlayback.paused = false;
        } else {
          welcomePlayback.paused = !welcomePlayback.paused;
        }
        syncWelcomeVideo();
      });
      video.addEventListener("ended", () => {
        welcomePlayback.finished = true;
        syncWelcomeFinale();
        syncWelcomeVideo();
      });
      video.addEventListener("timeupdate", syncWelcomeFinale);
      video.addEventListener("pause", syncWelcomeFinale);
      video.addEventListener("emptied", syncWelcomeFinale);
      video.addEventListener("loadedmetadata", () => {
        const seconds = Math.round(video.duration);
        if (Number.isFinite(seconds) && seconds > 0) {
          $("#welcomeFrameCount").textContent = `ФИЛЬМ · ${seconds} СЕК`;
        }
      });
      video.addEventListener("playing", () => {
        if (welcomeCanPlay()) {
          video.classList.add("is-playing");
          $("#welcomeFallback")?.classList.add("hidden");
        } else video.pause();
      });
      video.addEventListener("canplay", () => {
        // Первая попытка play() могла уйти в пустоту до данных — повторяем по готовности.
        if (!video.classList.contains("is-playing") && !welcomePlayback.blocked) syncWelcomeVideo();
      });
      video.addEventListener("error", () => {
        // Removing a source when leaving is cleanup, not a failed media request.
        if (!video.hasAttribute("src")) return;
        welcomePlayback.failed = true;
        video.classList.remove("is-playing");
        syncWelcomeVideo();
      });
      window.matchMedia?.("(prefers-reduced-motion: reduce)").addEventListener?.("change", syncWelcomeVideo);
      navigator.connection?.addEventListener?.("change", syncWelcomeVideo);
      // WebView может резать автоплей: первый жест снимает запрет, возврат во вкладку — повод повторить.
      window.addEventListener("pointerdown", event => {
        if (video.classList.contains("is-playing")) return;
        // Тап по кнопке обработает её click/touchend: не перехватываем жест
        if (event.target.closest && (event.target.closest("#welcomeMotion") || event.target.closest("#enterShop"))) return;
        welcomePlayback.blocked = false;
        syncWelcomeVideo();
      }, { passive: true });
      document.addEventListener("visibilitychange", syncWelcomeVideo);
      if (tg && typeof tg.onEvent === "function") tg.onEvent("activated", syncWelcomeVideo);
    }
    // Сторожевой таймер: не заиграло за 4 секунды — перезагружаем и повторяем попытку.
    window.clearTimeout(welcomePlayback.watchdog);
    welcomePlayback.watchdog = window.setTimeout(() => {
      if (video.classList.contains("is-playing") || !welcomeCanPlay()) return;
      welcomePlayback.pending = false;
      video.load();
      syncWelcomeVideo();
    }, 4000);
    syncWelcomeVideo();
  }

  function stopWelcomeVideo() {
    const video = $("#welcomeVideo");
    video.pause();
    video.classList.remove("is-playing");
    // Гасим источник и сторожевой таймер, иначе syncWelcomeVideo вернёт src
    // и ролик будет грузиться фоном, пока человек в магазине.
    welcomePlayback.source = "";
    welcomePlayback.finished = false;
    $("#welcome")?.classList.remove("is-logo-finale");
    welcomePlayback.blocked = false;
    welcomePlayback.pending = false;
    window.clearTimeout(welcomePlayback.watchdog);
    if (video.hasAttribute("src")) {
      video.removeAttribute("src");
      video.load();
    }
  }

  function browseCollection(category = "all") {
    state.stock = "all";
    state.sizeFilter = "all";
    state.search = "";
    state.category = category;
    state.sort = "featured";
    $("#searchInput").value = "";
    $("#sortSelect").value = "featured";
    $("#clearSearch").classList.add("hidden");
    $$("#filterOptions .filter-chip").forEach(node => node.classList.toggle("active", node.dataset.stock === "all"));
    $$("#sizeFilters .filter-chip").forEach(node => { node.classList.toggle("active", node.dataset.sizeFilter === "all"); node.setAttribute("aria-pressed", String(node.dataset.sizeFilter === "all")); });
    updateFilterCount();
    renderCategoryChips();
    renderProducts();
  }

  function renderSkeletons() {
    const root = $("#productGrid");
    if (!root || root.children.length) return;
    const card = '<article class="product-card skeleton-card" aria-hidden="true"><div class="sk-media"></div><div class="sk-line"></div><div class="sk-line short"></div></article>';
    root.innerHTML = card + card;
  }

  /* --- Lookbook-истории --- */

  let sheetProduct = null;
  const personArt = { img: null, ready: false };

  function drawPersonPreview() {
    const wrap = $("#personPreview");
    const canvas = $("#personCanvas");
    if (!wrap || !canvas) return;
    if (!sheetProduct || sheetProduct.id !== "tag-sila-i-chest") { wrap.classList.add("hidden"); return; }
    wrap.classList.remove("hidden");
    const input = $("#personInput");
    const digits = input ? input.value.replace(/\D/g, "").slice(0, 5) : "";
    const render = () => {
      const ctx = canvas.getContext("2d");
      if (!ctx || !personArt.img) return;
      const w = canvas.width, h = canvas.height;
      const iw = personArt.img.naturalWidth, ih = personArt.img.naturalHeight;
      const scale = Math.max(w / iw, h / ih);
      const dw = iw * scale, dh = ih * scale;
      ctx.clearRect(0, 0, w, h);
      ctx.drawImage(personArt.img, (w - dw) / 2, (h - dh) / 2, dw, dh);
      const text = digits || "00063";
      ctx.save();
      if (!digits) ctx.globalAlpha = 0.45;
      ctx.font = `700 ${Math.round(h * 0.13)}px "Sila Chest", "Times New Roman", serif`;
      try { ctx.letterSpacing = "6px"; } catch (_) { /* older canvas */ }
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "rgba(255,255,255,.45)";
      ctx.fillText(text, w * 0.59, h * 0.80 + 2);
      ctx.fillStyle = "#232326";
      ctx.fillText(text, w * 0.59, h * 0.80);
      ctx.restore();
    };
    if (personArt.ready) { render(); return; }
    const img = new Image();
    img.onload = () => { personArt.img = img; personArt.ready = true; render(); };
    img.src = "assets/spin/tag-sich-01-v3.jpg";
  }

  /* --- Живой постер 001: реальные слои одного исходного фото --- */

  const HERO_SLIDES = [{
    kicker: "НИКИТА ВОРОЖБИТОВ / ОСНОВАТЕЛЬ",
    productId: "tee-sila-i-chest",
    isLivingPoster: true
  }];

  let heroCarouselTimer = null;
  let heroCarouselIndex = 0;
  let heroCarouselPaused = false;

  function getHeroSlideDuration(index) {
    if (index === 0) return 5200; // Портрет и подпись основателя должны успеть прочитать.
    return 3600; // Предметные кадры вещей первого дропа
  }

  function showHeroSlide(index, manual = false) {
    const media = $("#heroMedia");
    if (!media) return;
    const count = HERO_SLIDES.length;
    heroCarouselIndex = ((index % count) + count) % count;
    const slide = HERO_SLIDES[heroCarouselIndex];

    if (slide.isLivingPoster) {
      media.classList.add("hero-mode-living");
      media.classList.remove("hero-mode-product");
    } else {
      media.classList.remove("hero-mode-living");
      media.classList.add("hero-mode-product");
    }

    const slideItems = $$("#heroMedia .hero-slide-item");
    slideItems.forEach(el => {
      const idx = Number(el.dataset.heroSlide);
      const isActive = idx === heroCarouselIndex;
      el.classList.toggle("active", isActive);
      const video = el.querySelector("video");
      if (video) {
        if (isActive) {
          try { video.currentTime = 0; video.play().catch(() => {}); } catch (_) {}
        } else {
          try { video.pause(); } catch (_) {}
        }
      }
    });

    const badgeText = $("#heroLiveText");
    if (badgeText) {
      if (slide.isLivingPoster) badgeText.innerHTML = '<small>ОСНОВАТЕЛЬ</small><strong>НИКИТА ВОРОЖБИТОВ</strong>';
      else badgeText.textContent = slide.kicker;
    }

    const progress = $("#heroCarouselProgress");
    if (progress) {
      const duration = getHeroSlideDuration(heroCarouselIndex);
      progress.style.setProperty("--hero-slide-duration", `${duration}ms`);
      progress.innerHTML = HERO_SLIDES.map((_, i) =>
        `<span class="${i < heroCarouselIndex ? "done" : i === heroCarouselIndex ? "current" : ""}" data-hero-target="${i}" role="button" aria-label="Слайд ${i + 1}"><i></i></span>`
      ).join("");
    }

    if (manual) haptic("selection");

    window.clearTimeout(heroCarouselTimer);
    if (HERO_SLIDES.length > 1 && !heroCarouselPaused && !reducedMotion()) {
      const duration = getHeroSlideDuration(heroCarouselIndex);
      heroCarouselTimer = window.setTimeout(() => {
        if (!document.hidden && !heroCarouselPaused) showHeroSlide(heroCarouselIndex + 1);
      }, duration);
    }
  }

  function setupHeroCarousel() {
    const media = $("#heroMedia");
    if (!media || media.dataset.carouselBound) return;
    media.dataset.carouselBound = "1";
    if (HERO_SLIDES.length < 2) { showHeroSlide(0); return; }

    const prevBtn = $("#heroCarouselPrev");
    const nextBtn = $("#heroCarouselNext");
    const progress = $("#heroCarouselProgress");

    if (progress) {
      progress.addEventListener("click", e => {
        const bar = e.target.closest("[data-hero-target]");
        if (bar) {
          e.stopPropagation();
          const target = Number(bar.dataset.heroTarget);
          if (Number.isInteger(target)) {
            showHeroSlide(target, true);
          }
        }
      });
    }

    if (prevBtn) {
      prevBtn.addEventListener("click", e => {
        e.stopPropagation();
        showHeroSlide(heroCarouselIndex - 1, true);
      });
    }
    if (nextBtn) {
      nextBtn.addEventListener("click", e => {
        e.stopPropagation();
        showHeroSlide(heroCarouselIndex + 1, true);
      });
    }

    media.addEventListener("pointerenter", () => { heroCarouselPaused = true; });
    media.addEventListener("pointerleave", () => {
      heroCarouselPaused = false;
      showHeroSlide(heroCarouselIndex);
    });

    let touchStartX = 0;
    let touchStartY = 0;
    media.addEventListener("touchstart", e => {
      if (!e.touches.length) return;
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      heroCarouselPaused = true;
    }, { passive: true });

    media.addEventListener("touchend", e => {
      heroCarouselPaused = false;
      if (!e.changedTouches.length) return;
      const diffX = e.changedTouches[0].clientX - touchStartX;
      const diffY = e.changedTouches[0].clientY - touchStartY;
      if (Math.abs(diffX) > 40 && Math.abs(diffX) > Math.abs(diffY)) {
        if (diffX < 0) showHeroSlide(heroCarouselIndex + 1, true);
        else showHeroSlide(heroCarouselIndex - 1, true);
      } else {
        showHeroSlide(heroCarouselIndex);
      }
    }, { passive: true });

    media.addEventListener("click", e => {
      if (e.target.closest("#heroCarouselPrev") || e.target.closest("#heroCarouselNext")) return;
      const slide = HERO_SLIDES[heroCarouselIndex];
      if (slide && slide.productId && !slide.isLivingPoster) {
        haptic("light");
        openProduct(slide.productId);
      }
    });

    showHeroSlide(0);
  }

  function syncHeroPoster() {
    const media = $("#heroMedia");
    const bg = $("#heroBackground");
    const subject = $("#heroSubject");
    if (!media || !bg || !subject) return;
    const ready = image => image && image.complete && image.naturalWidth > 0;
    const update = () => media.classList.toggle("poster-ready", ready(bg) && ready(subject));
    [bg, subject].forEach(image => {
      if (!image.dataset.posterBound) {
        image.dataset.posterBound = "1";
        image.addEventListener("load", update);
        image.addEventListener("error", update);
      }
    });
    update();
  }

  function setupHeroParallax() {
    const media = $("#heroMedia");
    if (!media || media.dataset.parallaxBound || $("#heroLivingPoster")?.classList.contains("hero-retouched")) return;
    media.dataset.parallaxBound = "1";
    let frame = 0;
    const apply = (x, y) => {
      media.style.setProperty("--poster-bg-x", `${(-x * 7).toFixed(2)}px`);
      media.style.setProperty("--poster-bg-y", `${(-y * 5).toFixed(2)}px`);
      media.style.setProperty("--poster-type-x", `${(-x * 3).toFixed(2)}px`);
      media.style.setProperty("--poster-type-y", `${(-y * 2).toFixed(2)}px`);
      media.style.setProperty("--poster-subject-x", `${(x * 3.5).toFixed(2)}px`);
      media.style.setProperty("--poster-subject-y", `${(y * 2.2).toFixed(2)}px`);
    };
    const fromScroll = () => {
      frame = 0;
      if (reducedMotion()) return;
      const rect = media.getBoundingClientRect();
      const progress = Math.max(-1, Math.min(1, (window.innerHeight / 2 - (rect.top + rect.height / 2)) / Math.max(1, window.innerHeight / 2)));
      apply(0, progress);
    };
    const queueScroll = () => { if (!frame) frame = window.requestAnimationFrame(fromScroll); };
    window.addEventListener("scroll", queueScroll, { passive: true });
    window.addEventListener("resize", queueScroll, { passive: true });
    media.addEventListener("pointermove", event => {
      if (reducedMotion() || event.pointerType === "touch") return;
      const rect = media.getBoundingClientRect();
      const x = Math.max(-1, Math.min(1, ((event.clientX - rect.left) / rect.width - 0.5) * 2));
      const y = Math.max(-1, Math.min(1, ((event.clientY - rect.top) / rect.height - 0.5) * 2));
      apply(x, y);
    }, { passive: true });
    media.addEventListener("pointerleave", queueScroll);

    // Mobile gyroscope tilt (DeviceOrientation)
    if (typeof window !== "undefined" && window.DeviceOrientationEvent && !reducedMotion()) {
      let baseG = null, baseB = null;
      window.addEventListener("deviceorientation", event => {
        if (event.gamma === null || event.beta === null) return;
        if (baseG === null) { baseG = event.gamma; baseB = event.beta; }
        const dx = Math.max(-1, Math.min(1, (event.gamma - baseG) / 22));
        const dy = Math.max(-1, Math.min(1, (event.beta - baseB) / 22));
        apply(dx, dy);
      }, { passive: true });
    }

    queueScroll();
  }

  /* --- Дроп как событие: режим из дат каталога и живого наличия --- */

  function dropMode(now = new Date()) {
    const event = (state.data && state.data.drop_event) || {};
    const starts = event.starts_at ? new Date(event.starts_at) : null;
    const earlyStarts = event.early_starts_at ? new Date(event.early_starts_at) : null;
    const ends = event.ends_at ? new Date(event.ends_at) : null;
    const valid = date => date && !Number.isNaN(date.getTime());
    const earlyMember = normalizeClubState(state.club).early_access;
    const products = (state.data.products || []).filter(product => Core.buyable(product));
    const live = products.filter(product => Core.available(product));
    const base = { event, starts, earlyStarts, ends, products, live, earlyMember, target: starts };
    // A non-empty but unparsable drop date closes the gate (server does the same).
    if ([starts, earlyStarts, ends].some(date => date && Number.isNaN(date.getTime()))) return { ...base, id: "ended", broken: true };
    if (valid(ends) && now > ends) return { ...base, id: "ended" };
    if (valid(starts) && now < starts) {
      if (earlyMember && valid(earlyStarts) && now >= earlyStarts) return { ...base, id: "early", target: starts };
      return { ...base, id: "pre", target: earlyMember && valid(earlyStarts) && now < earlyStarts ? earlyStarts : starts };
    }
    if (state.catalogReady && products.length && !live.length) return { ...base, id: "soldout" };
    return { ...base, id: "live" };
  }

  function dropPurchaseGate() {
    if (!state.catalogReady) return "";
    const mode = dropMode();
    if (mode.id === "pre") {
      return mode.earlyMember
        ? "Раннее окно ещё не открылось. Таймер — в блоке дропа выше."
        : "Дроп ещё не открыт. Ранний доступ — у участников клуба, закрывших все 7 дней.";
    }
    if (mode.id === "ended") return mode.broken ? "Дроп временно недоступен. Попробуй позже." : "Дроп закрыт. Этот выпуск завершён.";
    return "";
  }

  function formatCountdown(target, now = new Date()) {
    const totalSeconds = Math.max(0, Math.floor((target.getTime() - now.getTime()) / 1000));
    const days = Math.floor(totalSeconds / 86400);
    const hours = Math.floor((totalSeconds % 86400) / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const pair = value => String(value).padStart(2, "0");
    return days > 0 ? `${days} Д · ${pair(hours)}:${pair(minutes)}:${pair(seconds)}` : `${pair(hours)}:${pair(minutes)}:${pair(seconds)}`;
  }

  function renderDropEvent() {
    const root = $("#dropEvent");
    if (!root) return;
    const mode = dropMode();
    const title = $("#dropEventTitle");
    const text = $("#dropEventText");
    const timer = $("#dropEventTimer");
    const stats = $("#dropEventStats");
    const button = $("#dropEventButton");
    root.classList.remove("is-pre", "is-early", "is-live", "is-soldout", "is-ended");
    root.classList.add(`is-${mode.id}`);
    // В обычном режиме открытой продажи без таймера не дублируем Hero лишней плашкой.
    const hasCustomLiveNote = Boolean(mode.event && mode.event.note && !String(mode.event.note).includes("берутся из живого каталога"));
    root.classList.toggle("hidden", mode.id === "live" && !hasCustomLiveNote);
    if ($("#dropEventKicker")) $("#dropEventKicker").textContent = `001 / ${String(mode.event.title || "СОБЫТИЕ").toUpperCase()}`;
    window.clearInterval(state.dropTimer);
    state.dropTimer = null;
    if (mode.id === "pre") {
      title.textContent = "СКОРО ОТКРОЕМ";
      text.textContent = mode.earlyMember
        ? "Твоё раннее окно откроется первым — таймер ниже. До него покупку держит сервер."
        : "Ранний доступ — участникам клуба дисциплины, закрывшим все 7 дней. Остальным — по общему таймеру.";
      timer.classList.remove("hidden");
      const tick = () => { timer.textContent = formatCountdown(mode.target); if (new Date() >= mode.target) renderDropEvent(); };
      tick();
      state.dropTimer = window.setInterval(tick, 1000);
      button.textContent = mode.earlyMember ? "СМОТРЕТЬ ВИТРИНУ" : "В КЛУБ ЗА РАННИМ ДОСТУПОМ";
      button.onclick = () => scrollToId(mode.earlyMember ? "catalog" : "club");
    } else if (mode.id === "early") {
      title.textContent = "РАННИЙ ДОСТУП ОТКРЫТ";
      text.textContent = "Ты закрыл все 7 дней клуба. Для всех остальных дроп откроется по таймеру.";
      timer.classList.remove("hidden");
      const tick = () => { timer.textContent = formatCountdown(mode.starts); if (new Date() >= mode.starts) renderDropEvent(); };
      tick();
      state.dropTimer = window.setInterval(tick, 1000);
      button.textContent = "К ВЕЩАМ ПЕРВЫМ";
      button.onclick = () => scrollToId("catalog");
    } else if (mode.id === "soldout") {
      title.textContent = "РАЗМЕРЫ ЗАКОНЧИЛИСЬ";
      text.textContent = "Открой вещь и нажми «Ждать размер» — напишем в Telegram, когда вернётся.";
      timer.classList.add("hidden");
      button.textContent = "ЖДАТЬ РАЗМЕР";
      button.onclick = () => scrollToId("catalog");
    } else if (mode.id === "ended") {
      title.textContent = mode.broken ? "ДРОП НА ПАУЗЕ" : "ДРОП ЗАКРЫТ";
      text.textContent = mode.broken
        ? "Даты дропа уточняются. Следи за обновлениями магазина."
        : "Этот выпуск завершён. Следующий сначала увидят участники клуба.";
      timer.classList.add("hidden");
      button.textContent = "В КЛУБ";
      button.onclick = () => scrollToId("club");
    } else {
      title.textContent = "ДРОП ОТКРЫТ";
      const note = String(mode.event.note || "");
      text.textContent = note && !note.includes("берутся из живого каталога") ? note : "Первый выпуск «Сила и честь». Выбирай вещь, которая говорит за тебя.";
      timer.classList.add("hidden");
      button.textContent = "К ВЕЩАМ";
      button.onclick = () => scrollToId("catalog");
    }
    const sizeCount = new Set(mode.live.flatMap(product => product.sizes || [])).size;
    const modeLabel = { pre: "Скоро", early: "Ранний доступ", live: "Открыт", soldout: "Нет размеров", ended: "Закрыт" }[mode.id] || mode.id;
    stats.innerHTML = state.catalogReady
      ? `<span>ВЕЩЕЙ ДОСТУПНО <b>${mode.live.length}</b></span><span>РАЗМЕРОВ <b>${sizeCount}</b></span><span>РЕЖИМ <b>${modeLabel}</b></span>`
      : `<span>ЗАГРУЖАЕМ ЖИВОЙ КАТАЛОГ…</span>`;
  }

  /* --- Паспорт вещи: только факты из каталога и честный статус медиа --- */

  function pluralRu(count, one, few, many) {
    const mod10 = count % 10, mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
    return many;
  }

  function passportMediaLine(product) {
    const photos = imagesFor(product).length;
    const spin = Array.isArray(product.spin) ? product.spin.length : 0;
    if (["tee-sila-i-chest", "tee-vzglyad"].includes(product.id)) return `${photos} фото · интерактивная 3D-модель 360°`;
    if (product.spin_kind === "visualization" && spin > 1) {
      return `${spin} ракурсов · визуализация 360°`;
    }
    if (product.id === "tee-odin-v-pole-voin" && spin > 1) {
      return `${spin} ${pluralRu(spin, "исходный ракурс", "исходных ракурса", "исходных ракурсов")} · полный подтверждённый оборот 360°`;
    }
    if (String(product.video || "").includes("front-to-back")) {
      return `${photos} фото · исходное видео «перед → спина», не полный 360°`;
    }
    if (spin > 1) return `${photos} фото · ${spin} ${pluralRu(spin, "ракурс", "ракурса", "ракурсов")} в обзоре`;
    return `${photos} фото · полного 360° у этой модели не заявляем`;
  }

  function cleanPassportDetail(item) {
    // В каталоге детали могут начинаться с эмодзи — в паспорте нужен только текст,
    // иначе на устройствах без эмодзи-шрифта появляется битый глиф.
    return String(item || "").replace(/^[^\p{L}\p{N}«"—-]+/u, "").trim();
  }

  function renderPassport(product) {
    const body = $("#passportBody");
    if (!body || !product) return;
    if ($("#passportCode")) $("#passportCode").textContent = String(product.badge || "001").toUpperCase();
    const details = (Array.isArray(product.details) ? product.details : []).map(cleanPassportDetail).filter(item => item && !/жетон|подар|хлопок|посадк|крой|плотност/i.test(item)).slice(0, 8);
    // Материал, посадка и статус уже стоят строкой фактов выше — не дублируем их в паспорте.
    body.innerHTML = `
      <div class="passport-row"><span>МЕДИА</span><span>${escapeHTML(passportMediaLine(product))}</span></div>
      ${details.length ? `<div class="passport-row"><span>ДЕТАЛИ</span><ul class="passport-details">${details.map(item => `<li>${escapeHTML(item)}</li>`).join("")}</ul></div>` : ""}
      `;
  }

  function selectSize(size) {
    if (!size) return;
    state.selectedSize = size;
    $$(".size-button", $("#sizeList")).forEach(node => {
      const active = node.dataset.size === size;
      node.classList.toggle("selected", active);
      node.setAttribute("aria-pressed", String(active));
    });
    $("#sizeHint").textContent = `Размер ${size} выбран.`;
    $("#sizeHint").classList.remove("error");
    updatePurchaseSummary();
    updateCombatSizer();
  }

  function savedFit() {
    const fit = loadJSON("vorozhbitov_fit", {});
    return fit && typeof fit === "object" ? fit : {};
  }

  function setFitChoice(choice) {
    state.fitChoice = ["close", "regular", "oversize"].includes(choice) ? choice : "regular";
    $$("[data-fit-choice]").forEach(node => {
      const active = node.dataset.fitChoice === state.fitChoice;
      node.classList.toggle("active", active);
      node.setAttribute("aria-pressed", String(active));
    });
  }

  function resetFitFinder(product) {
    syncCombatSizer(product);
  }

  const combatSizer = { height: 182, weight: 86 };
  function syncCombatSizer(product) {
    const root = $("#smartSizer");
    if (!root) return;
    const isApparel = Boolean(product && Core.buyable(product) && (product.sizes || []).some(s => ["S", "M", "L", "XL", "XXL", "2XL", "3XL"].includes(String(s).toUpperCase())));
    root.classList.toggle("hidden", !isApparel);
    if (!isApparel) return;
    const saved = savedFit();
    const h = Number(saved.height || state.profile.height);
    const w = Number(saved.weight);
    if (Number.isFinite(h) && h >= 130 && h <= 230) combatSizer.height = Math.round(h);
    if (Number.isFinite(w) && w >= 35 && w <= 220) combatSizer.weight = Math.round(w);
    const isFitted = product.id === "tee-vzglyad";
    if ($("#smartSizerSubtitle")) {
      $("#smartSizerSubtitle").textContent = isFitted
        ? "У «ВЗГЛЯДА» прилегающий атлетический крой — сидит плотно по торсу"
        : "У «СИЛА И ЧЕСТЬ» прямой свободный крой — выбери желаемую посадку";
    }
    if ($("#adviceFitType")) $("#adviceFitType").textContent = isFitted ? "Плотно по торсу" : "По фигуре · База";
    if ($("#adviceFitText")) {
      $("#adviceFitText").textContent = isFitted
        ? "Прилегающая посадка по груди и плечам. Подчёркивает форму."
        : "Классическая прямая посадка по твоим параметрам.";
    }
    if ($("#adviceOversizeType")) $("#adviceOversizeType").textContent = isFitted ? "Комфортно · Свободнее" : "Свободно · Оверсайз";
    if ($("#adviceOversizePill")) $("#adviceOversizePill").textContent = isFitted ? "+1 РАЗМЕР" : "ОВЕРСАЙЗ";
    if ($("#adviceOversizeText")) {
      $("#adviceOversizeText").textContent = isFitted
        ? "На размер больше — если не любишь плотную посадку в обтяжку."
        : "Больше воздуха в плечах и груди — расслабленный силуэт.";
    }
    updateCombatSizer();
  }
  function updateCombatSizer() {
    const root = $("#smartSizer"), product = state.currentProduct; if (!root || !product || root.classList.contains("hidden")) return;
    $("#sizerHeightVal").textContent = String(combatSizer.height); $("#sizerWeightVal").textContent = String(combatSizer.weight);
    [["Fit", "regular"], ["Oversize", "oversize"]].forEach(([id, fit]) => {
      const card = $("#advice" + id + "Card"); if (!card) return;
      const found = Core.recommendSize({height:combatSizer.height, weight:combatSizer.weight, fit}, product.sizes || []); const size = found?.size || "";
      card.disabled = !size; card.dataset.smartSize = size; card.classList.toggle("selected", Boolean(size && state.selectedSize === size)); card.setAttribute("aria-pressed", String(!!size && state.selectedSize === size));
      $("#advice" + id + "Size").textContent = size || "—"; $("#advice" + id + "Target").textContent = size || "—";
    });
  }
  function recommendFit() {
    const product = state.currentProduct;
    const result = $("#fitResult");
    if (!product || !result) return;
    const height = Number($("#fitHeight") && $("#fitHeight").value);
    const weight = Number($("#fitWeight") && $("#fitWeight").value);
    const found = Core.recommendSize({ height, weight, fit: state.fitChoice }, product.sizes || []);
    if (!found) {
      result.classList.add("error");
      result.textContent = "Проверь рост и вес: нужны числа в разумных пределах.";
      haptic("error");
      return;
    }
    saveJSON("vorozhbitov_fit", { height: String(height), weight: String(weight), fit: state.fitChoice });
    if (state.profile) {
      state.profile.size = found.size;
      state.profile.height = String(height);
      saveJSON("vorozhbitov_profile", state.profile);
    }
    selectSize(found.size);
    const fitLabel = state.fitChoice === "close" ? "плотнее" : state.fitChoice === "oversize" ? "свободнее" : "обычная посадка";
    result.classList.remove("error");
    result.innerHTML = found.between
      ? `Ты между <strong>${escapeHTML(found.size)}</strong> и <strong>${escapeHTML(found.between)}</strong>. Выбрали ${escapeHTML(found.size)}; хочешь свободнее — бери ${escapeHTML(found.between)}.`
      : `Твой размер: <strong>${escapeHTML(found.size)}</strong> · ${fitLabel}. Уже выбрали его выше.`;
    haptic("success");
    $("#sizeList")?.scrollIntoView({ block: "center", behavior: reducedMotion() ? "instant" : "smooth" });
  }

  /* --- Фильм первого дропа: только реальные фото и видео из каталога --- */

  function filmScenes() {
    const raw = Array.isArray(state.data.film) ? state.data.film : [];
    const scenes = raw.filter(scene => scene && (typeof scene.image === "string" || typeof scene.video === "string")).map(scene => ({
      image: typeof scene.image === "string" ? scene.image : "",
      video: typeof scene.video === "string" ? scene.video : "",
      poster: typeof scene.poster === "string" ? scene.poster : "",
      kicker: String(scene.kicker || "ФИЛЬМ ДРОПА"),
      title: String(scene.title || "ПЕРВЫЙ ДРОП"),
      text: String(scene.text || ""),
      product_id: scene.product_id && productById(scene.product_id) ? scene.product_id : "",
      cta: scene.cta === "catalog" ? "catalog" : ""
    }));
    if (scenes.length) return scenes;
    return (state.data.lookbook || []).filter(item => item && item.image).map((item, index) => ({
      image: item.image, video: "", poster: "", product_id: "", cta: index === (state.data.lookbook || []).length - 1 ? "catalog" : "",
      kicker: `0${index + 1} / ПЕРВЫЙ ДРОП`, title: String(item.caption || "СИЛА И ЧЕСТЬ"), text: "Реальное фото из каталога."
    }));
  }

  function stopFilm() {
    window.clearTimeout(state.filmTimer);
    state.filmTimer = null;
    const video = $("#filmVideo");
    if (video) video.pause();
  }

  function closeFilm() {
    stopFilm();
    closeModal("filmModal");
  }

  function showFilmScene(index) {
    const scenes = filmScenes();
    if (!scenes.length) { closeFilm(); return; }
    if (index >= scenes.length) { closeFilm(); scrollToId("catalog"); return; }
    state.filmIndex = Math.max(0, index);
    const scene = scenes[state.filmIndex];
    const image = $("#filmImage");
    const video = $("#filmVideo");
    const useVideo = Boolean(scene.video && !saverMode());
    window.clearTimeout(state.filmTimer);
    if (useVideo) {
      image.classList.add("hidden");
      video.classList.remove("hidden");
      if (video.getAttribute("src") !== scene.video) video.src = scene.video;
      video.poster = scene.poster || scene.image || "";
      const playback = video.play();
      if (playback && typeof playback.catch === "function") playback.catch(() => { /* poster stays visible */ });
    } else {
      video.pause();
      video.classList.add("hidden");
      image.classList.remove("hidden");
      image.src = scene.image || scene.poster || "";
      image.alt = `${scene.title} — кадр первого дропа`;
      image.classList.toggle("contain", /product-proof|product-studio|\/spin\//.test(image.src));
    }
    $("#filmKicker").textContent = scene.kicker;
    $("#filmTitle").textContent = scene.title;
    $("#filmText").textContent = scene.text;
    $("#filmCounter").textContent = `${String(state.filmIndex + 1).padStart(2, "0")} / ${String(scenes.length).padStart(2, "0")}`;
    $("#filmProduct").classList.toggle("hidden", !scene.product_id);
    $("#filmCatalog").classList.toggle("hidden", scene.cta !== "catalog");
    const progress = $("#filmProgress");
    const sceneIndex = state.filmIndex;
    const scheduleAdvance = ms => {
      progress.style.setProperty("--film-duration", `${ms}ms`);
      progress.innerHTML = scenes.map((_, i) => `<span class="${i < sceneIndex ? "done" : i === sceneIndex ? "current" : ""}"><i></i></span>`).join("");
      window.clearTimeout(state.filmTimer);
      if (!reducedMotion()) {
        state.filmTimer = window.setTimeout(() => {
          if (state.filmIndex === sceneIndex && state.modalStack.at(-1) === "filmModal" && !document.hidden) showFilmScene(sceneIndex + 1);
        }, ms);
      }
    };
    // Video scenes run for the real clip length, not a guessed constant.
    if (useVideo && Number.isFinite(video.duration) && video.duration > 0) {
      scheduleAdvance(Math.min(30000, Math.max(3000, Math.round(video.duration * 1000))));
    } else {
      scheduleAdvance(useVideo ? 9000 : 5200);
      if (useVideo) {
        video.addEventListener("loadedmetadata", () => {
          if (state.filmIndex !== sceneIndex || !Number.isFinite(video.duration) || video.duration <= 0) return;
          const left = Math.min(30000, Math.max(1200, Math.round((video.duration - (video.currentTime || 0)) * 1000)));
          scheduleAdvance(left);
        }, { once: true });
        video.addEventListener("ended", () => {
          if (state.filmIndex === sceneIndex && state.modalStack.at(-1) === "filmModal") showFilmScene(sceneIndex + 1);
        }, { once: true });
      }
    }
    const nextScene = scenes[sceneIndex + 1];
    if (nextScene && nextScene.image) { const warm = new Image(); warm.src = nextScene.image; }
  }

  function openStories(index = 0) {
    const scenes = filmScenes();
    if (!scenes.length) {
      showToast("Фильм дропа пока недоступен — каталог ещё грузится.");
      return;
    }
    saveJSON("vorozhbitov_film_seen", (state.data.drop_event && state.data.drop_event.id) || "drop-001");
    openModal("filmModal");
    // Программный фокус на кнопке закрытия рисует красное кольцо-«мишень» при автооткрытии.
    // Оставляем фокус в модалке, но без видимого кольца; Tab-ловушка продолжит работать.
    const filmClose = $("#filmModal [data-close]");
    if (filmClose) {
      try { filmClose.focus({ preventScroll: true, focusVisible: false }); }
      catch (_) { /* older WebView без focusVisible — обработает строка ниже */ }
      if (filmClose.matches(":focus-visible")) filmClose.blur();
    }
    showFilmScene(index);
    haptic("light");
  }

  /* --- Клуб дисциплины: сервер в Telegram, локальный запасной режим в браузере --- */

  const CLUB_FALLBACK_TASKS = [
    "Тренировка или 30 минут движения. Без отговорок.",
    "Подъём точно в запланированное время.",
    "Закрой дело, которое давно откладывал.",
    "Час полной тишины: без ленты, новостей и лишнего шума.",
    "Помоги своему человеку делом, а не словами.",
    "Порядок в форме и вещах: всё на своих местах.",
    "Подведи итог недели письменно. Что сдержал — то твоё."
  ];

  function clubConfig() {
    const config = (state.data && state.data.club) || {};
    const tasks = Array.isArray(config.tasks) ? config.tasks.filter(task => typeof task === "string" && task.trim()) : [];
    return {
      title: String(config.title || "7 ДНЕЙ СЛОВА"),
      reward: String(config.reward || "Ранний доступ к следующим выпускам для тех, кто закрыл все 7 дней."),
      tasks: tasks.length >= 7 ? tasks.slice(0, 7) : CLUB_FALLBACK_TASKS
    };
  }

  function localDateISO(date = new Date()) {
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }

  function normalizeClubState(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const seen = new Set();
    const checkins = (Array.isArray(source.checkins) ? source.checkins : [])
      .filter(row => row && /^\d{4}-\d{2}-\d{2}$/.test(String(row.date || "")))
      .map(row => ({ date: String(row.date), task_index: Math.max(0, Math.min(6, Number(row.task_index) || 0)) }))
      .filter(row => !seen.has(row.date) && seen.add(row.date))
      .sort((a, b) => a.date.localeCompare(b.date))
      .slice(0, 7);
    return {
      joined: Boolean(source.joined || checkins.length),
      started_on: typeof source.started_on === "string" ? source.started_on : "",
      checkins,
      total: checkins.length,
      completed: checkins.length >= 7,
      early_access: checkins.length >= 7,
      today: /^\d{4}-\d{2}-\d{2}$/.test(String(source.today || "")) ? String(source.today) : ""
    };
  }

  function persistClub(raw, source) {
    state.club = normalizeClubState(raw);
    state.clubSource = source || state.clubSource || "local";
    saveJSON("vorozhbitov_club", state.club);
    renderClub();
    renderDropEvent();
  }

  function renderClub() {
    state.club = normalizeClubState(state.club);
    const club = state.club;
    const config = clubConfig();
    const rank = Core.clubRank(club.total);
    // Server club days are Europe/Moscow; device-local "today" breaks streaks
    // for other timezones, so prefer the server date when we have it.
    const today = state.clubSource === "bot" && club.today ? club.today : localDateISO();
    const doneToday = club.checkins.some(row => row.date === today);
    const streak = Core.clubStreak(club.checkins.map(row => row.date), today);
    const current = Math.min(club.total, 6);
    if ($("#clubTitle")) {
      const words = config.title.split(" ");
      let tail = words.slice(2).join(" ") || "СЛОВА";
      if (!/[.!]$/.test(tail)) tail += ".";
      $("#clubTitle").innerHTML = `${escapeHTML(words.slice(0, 2).join(" "))}<br><em>${escapeHTML(tail)}</em>`;
    }
    if ($("#clubRank")) $("#clubRank").textContent = `РАНГ: ${rank.label}`;
    if ($("#clubProgressBar")) $("#clubProgressBar").style.width = `${Math.round((club.total / 7) * 100)}%`;
    if ($("#clubStreak")) $("#clubStreak").textContent = `Серия: ${streak}`;
    if ($("#clubTotal")) $("#clubTotal").textContent = `Выполнено: ${club.total} / 7`;
    if ($("#clubSource")) $("#clubSource").textContent = state.clubSource === "bot" ? "Прогресс сохранён в боте" : "Прогресс на этом устройстве";
    if ($("#clubTaskDay")) $("#clubTaskDay").textContent = club.completed ? "НЕДЕЛЯ ЗАКРЫТА" : `ДЕНЬ ${String(current + 1).padStart(2, "0")}`;
    if ($("#clubTaskText")) $("#clubTaskText").textContent = club.completed ? config.reward : config.tasks[current];
    if ($("#clubDays")) {
      $("#clubDays").innerHTML = Array.from({ length: 7 }, (_, index) => {
        const done = index < club.total;
        const isToday = index === club.total && !club.completed;
        return `<span class="club-day ${done ? "done" : ""} ${isToday ? "today" : ""}" aria-label="День ${index + 1}${done ? " выполнен" : ""}">${done ? "✓" : index + 1}</span>`;
      }).join("");
    }
    const button = $("#clubAction");
    if (button) {
      button.disabled = state.clubBusy || club.completed || doneToday;
      button.textContent = state.clubBusy ? "СОХРАНЯЮ…"
        : !club.joined ? "ВСТУПИТЬ В КЛУБ"
        : club.completed ? "РАНГ «СВОЙ» · РАННИЙ ДОСТУП ОТКРЫТ"
        : doneToday ? "СЕГОДНЯ СДЕЛАНО ✓"
        : `СДЕЛАЛ · ДЕНЬ ${current + 1}`;
    }
    if ($("#clubNote")) {
      $("#clubNote").textContent = club.completed ? config.reward
        : doneToday ? "Сегодня закрыто. Следующее задание — завтра."
        : state.clubSource === "bot" ? "Отметка сохранится в боте и не потеряется на другом устройстве."
        : "Из Telegram прогресс сохраняется в боте. В браузере — на этом устройстве.";
    }
    if ($("#profileClubRank")) $("#profileClubRank").textContent = `${rank.label} · ${club.total} / 7`;
    if ($("#profileClubStreak")) $("#profileClubStreak").textContent = club.early_access ? "Ранний доступ открыт" : `Серия: ${streak}`;
  }

  async function clubApi(action) {
    const initData = tg && tg.initData;
    if (!initData) throw new Error("local");
    const response = await fetch("/api/club", action ? {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json", "X-Telegram-Init-Data": initData },
      body: JSON.stringify({ action })
    } : { headers: { Accept: "application/json", "X-Telegram-Init-Data": initData } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.club) throw new Error(payload.error || "club unavailable");
    return payload.club;
  }

  async function loadClubState() {
    renderClub();
    if (!(tg && tg.initData)) return;
    try {
      let serverClub = normalizeClubState(await clubApi(""));
      // Replay an action that was applied locally while the server was down,
      // so an offline check-in is not lost on the next online launch.
      const pending = loadJSON("vorozhbitov_club_pending", "");
      if (pending === "join" || pending === "checkin") {
        try {
          serverClub = normalizeClubState(await clubApi(pending));
          saveJSON("vorozhbitov_club_pending", "");
        } catch (_) { /* keep pending for the next launch */ }
      }
      // Пустой серверный профиль не должен стирать уже заработанный локальный прогресс.
      if (serverClub.total === 0 && normalizeClubState(state.club).total > 0) { renderClub(); return; }
      persistClub(serverClub, "bot");
    } catch (_) { state.clubSource = "local"; renderClub(); }
  }

  function localClubApply(action) {
    const club = normalizeClubState(state.club);
    const today = localDateISO();
    if (!club.joined) {
      club.joined = true;
      club.started_on = today;
    }
    if (action === "checkin" && !club.completed && !club.checkins.some(row => row.date === today)) {
      club.checkins.push({ date: today, task_index: Math.min(club.total, 6) });
    }
    return normalizeClubState(club);
  }

  async function clubAction() {
    if (state.clubBusy) return;
    const before = normalizeClubState(state.club);
    const action = before.joined ? "checkin" : "join";
    state.clubBusy = true;
    renderClub();
    try {
      if (tg && tg.initData) persistClub(await clubApi(action), "bot");
      else persistClub(localClubApply(action), "local");
      haptic("success");
      showToast(action === "join" ? "Ты в клубе. День 01 открыт." : state.club.completed ? "Неделя закрыта. Ранг «СВОЙ»." : "День закрыт. Так держать.");
    } catch (_) {
      persistClub(localClubApply(action), "local");
      saveJSON("vorozhbitov_club_pending", action);
      showToast("Сервер недоступен — сохранил отметку на этом устройстве и досошлю позже.");
    } finally {
      state.clubBusy = false;
      renderClub();
    }
  }

  /* --- Свои: только одобренные реальные фото из каталога --- */

  function renderCommunity() {
    const grid = $("#communityGrid");
    if (!grid) return;
    const entries = (Array.isArray(state.data.community) ? state.data.community : [])
      .filter(item => item && typeof item.image === "string" && item.image);
    if (!entries.length) {
      for (const [id, image] of [["tee-sila-i-chest", "assets/drop/hero-founder-retouch-v4.webp"], ["tee-vzglyad", "assets/client/vzglyad-nikita-front-20261111.webp"]]) {
        const product = productById(id);
        if (product) entries.push({image, name:"Никита Ворожбитов", caption:product.name, city:"НИКИТА ВОРОЖБИТОВ · ОСНОВАТЕЛЬ", product_id:id});
      }
    }
    if (!entries.length) {
      grid.innerHTML = `<div class="community-empty"><span>ПЕРВЫЙ ВЫПУСК</span><strong>Стань первым своим.</strong><span>Здесь появятся фотографии покупателей первого дропа.</span></div>`;
      return;
    }
    grid.innerHTML = entries.map(item => {
      const product = item.product_id ? productById(item.product_id) : null;
      const title = String(item.caption || item.name || (product && product.name) || "Свой человек");
      const city = String(item.city || "СИЛА И ЧЕСТЬ").toUpperCase();
      const details = [Number(item.height_cm) > 0 ? `${Number(item.height_cm)} см` : "", Number(item.weight_kg) > 0 ? `${Number(item.weight_kg)} кг` : "", item.size ? `Размер ${String(item.size)}` : ""].filter(Boolean).join(" · ");
      const video = typeof item.video === "string" && /^(?:https:\/\/|assets\/)/.test(item.video) ? item.video : "";
      const media = video ? `<video src="${escapeHTML(video)}" poster="${escapeHTML(item.image)}" muted playsinline controls preload="none" aria-label="${escapeHTML(title)} — видео образа"></video>` : `<img src="${escapeHTML(item.image)}" alt="${escapeHTML(title)} — сообщество «Свои»" loading="lazy" decoding="async">`;
      return `<figure class="community-card">${media}<figcaption><span>${escapeHTML(city)}</span><strong>${escapeHTML(title)}</strong>${details ? `<small>${escapeHTML(details)}</small>` : ""}${product ? `<button type="button" data-community-product="${escapeHTML(product.id)}">ОТКРЫТЬ ВЕЩЬ ↗</button>` : ""}</figcaption></figure>`;
    }).join("");
  }

  function submitCommunityPhoto() {
    const text = [
      "Здравствуйте! Хочу в раздел «СВОИ» бренда СИЛА И ЧЕСТЬ.",
      "Сейчас прикреплю своё фото в вещи бренда к этому сообщению.",
      "Город: ",
      "Вещь: ",
      "Разрешаю опубликовать фото в миниаппе и канале бренда после проверки."
    ].join("\n");
    const url = checkoutClient.managerUrl(state.data.support_username, text);
    if (!url) {
      showToast("Чат магазина пока не подключён.");
      return;
    }
    if (tg && typeof tg.openTelegramLink === "function") tg.openTelegramLink(url);
    else window.open(url, "_blank", "noopener,noreferrer");
    showToast("Чат открыт. Прикрепи фото к готовому тексту и отправь.");
    haptic("light");
  }

  /* --- Карта владельца по зарегистрированному заказу --- */

  function loadCanvasImage(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = reject;
      image.src = src;
    });
  }

  function wrapCanvasText(ctx, text, x, y, maxWidth, lineHeight, maxLines) {
    const words = String(text || "").split(/\s+/).filter(Boolean);
    let line = "";
    let lines = 0;
    for (const word of words) {
      const test = line ? `${line} ${word}` : word;
      if (ctx.measureText(test).width > maxWidth && line) {
        ctx.fillText(line, x, y + lines * lineHeight);
        lines += 1;
        if (lines >= maxLines) return y + lines * lineHeight;
        line = word;
      } else line = test;
    }
    if (line && lines < maxLines) {
      ctx.fillText(line, x, y + lines * lineHeight);
      lines += 1;
    }
    return y + lines * lineHeight;
  }

  async function drawOwnerCard(row) {
    const canvas = $("#ownerCanvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    try { if (document.fonts && document.fonts.ready) await document.fonts.ready; } catch (_) { /* fallback fonts */ }
    const product = (row.product_id && productById(row.product_id)) || (state.data.products || []).find(item => item.name === row.product_name);
    ctx.fillStyle = "#0b0b0d";
    ctx.fillRect(0, 0, 900, 1200);
    ctx.fillStyle = "#e10600";
    ctx.fillRect(0, 0, 900, 14);
    ctx.fillStyle = "#f4eee6";
    ctx.font = "700 28px 'Sila Chest', Arial, sans-serif";
    ctx.fillText("СИЛА И ЧЕСТЬ", 56, 78);
    ctx.fillStyle = "#a49c8f";
    ctx.font = "600 20px Arial, sans-serif";
    ctx.fillText("ПЕРВЫЙ ДРОП · 001", 56, 112);
    ctx.fillStyle = "#f4eee6";
    ctx.font = "800 82px 'Sila Chest', Arial, sans-serif";
    ctx.fillText("КАРТА", 52, 218);
    ctx.fillText("ВЛАДЕЛЬЦА", 52, 302);
    ctx.fillStyle = "#171719";
    ctx.fillRect(56, 344, 788, 388);
    ctx.strokeStyle = "rgba(255,255,255,.18)";
    ctx.strokeRect(56, 344, 788, 388);
    if (product) {
      try {
        const image = await loadCanvasImage(imageFor(product));
        const box = { x: 56, y: 344, w: 788, h: 388 };
        // Заполняем рамку целиком (кадрирование по верхней трети), иначе портретное фото
        // висит узкой полосой посреди пустого чёрного прямоугольника.
        const scale = Math.max(box.w / image.naturalWidth, box.h / image.naturalHeight);
        const w = image.naturalWidth * scale;
        const h = image.naturalHeight * scale;
        ctx.save();
        ctx.beginPath();
        ctx.rect(box.x, box.y, box.w, box.h);
        ctx.clip();
        ctx.drawImage(image, box.x + (box.w - w) / 2, box.y + (box.h - h) * 0.3, w, h);
        ctx.restore();
      } catch (_) { /* text card remains valid without the photo */ }
    }
    let y = 798;
    const field = (label, value, big = false) => {
      ctx.fillStyle = "#e10600";
      ctx.font = "800 18px Arial, sans-serif";
      ctx.fillText(label, 56, y);
      ctx.fillStyle = "#f4eee6";
      ctx.font = big ? "800 44px 'Sila Chest', Arial, sans-serif" : "700 30px Arial, sans-serif";
      y = wrapCanvasText(ctx, value || "—", 56, y + (big ? 48 : 38), 788, big ? 48 : 36, 2) + 28;
    };
    field("ВЕЩЬ", row.product_name || "Вещь бренда", true);
    field("РАЗМЕР / ЗАКАЗ", `${row.size || "—"} · №${row.id || "—"}${row.quantity ? ` · ${row.quantity} шт.` : ""}`);
    const when = formatOrderWhen(row.created_at || row.at);
    field("СТАТУС", `${row.status_label || row.status || "зарегистрирован"}${when ? ` · ${when}` : ""}`);
    if (row.gift) field("ПОДАРОК", `${row.gift.name || "Жетон"}${row.gift.serial_label ? ` · №${row.gift.serial_label}` : ""}`);
    ctx.fillStyle = "#a49c8f";
    ctx.font = "600 20px Arial, sans-serif";
    ctx.fillText("СИЛА / ДИСЦИПЛИНА / ЧЕСТЬ", 56, 1146);
    const link = $("#ownerDownload");
    if (state.ownerCardUrl) URL.revokeObjectURL(state.ownerCardUrl);
    if (canvas.toBlob) {
      canvas.toBlob(blob => {
        if (!blob || !link) return;
        state.ownerCardUrl = URL.createObjectURL(blob);
        link.href = state.ownerCardUrl;
      }, "image/png");
    } else if (link) link.href = canvas.toDataURL("image/png");
    if (link) link.download = `sila-i-chest-owner-${row.id || "card"}.png`;
  }

  function openOwnerCard(index) {
    const row = state.profileOrders[Number(index)];
    if (!row) return;
    state.ownerOrder = row;
    // Сбрасываем ссылку до перерисовки, чтобы нельзя было скачать карту прошлого заказа.
    const link = $("#ownerDownload");
    if (link) link.setAttribute("href", "#");
    openModal("ownerCardModal");
    drawOwnerCard(row);
    haptic("light");
  }

  async function shareOwnerCard() {
    const canvas = $("#ownerCanvas");
    if (!canvas) return;
    try {
      const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
      if (blob && navigator.share) {
        const file = new File([blob], "sila-i-chest-owner-card.png", { type: "image/png" });
        if (!navigator.canShare || navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file], title: "Карта владельца · СИЛА И ЧЕСТЬ" });
          return;
        }
      }
      throw new Error("share unavailable");
    } catch (_) {
      const link = $("#ownerDownload");
      if (link && link.href && link.href !== "#") link.click();
      showToast("Скачал карту — её можно отправить в чат или сторис вручную.");
    }
  }

  function setupOwnerTilt() {
    const wrap = $("#ownerCanvasWrap");
    if (!wrap || wrap.dataset.tiltBound) return;
    wrap.dataset.tiltBound = "1";

    function handleMove(e) {
      const rect = wrap.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const px = Math.max(0, Math.min(100, (x / rect.width) * 100));
      const py = Math.max(0, Math.min(100, (y / rect.height) * 100));
      const rotX = ((py - 50) / 50) * -7;
      const rotY = ((px - 50) / 50) * 7;

      wrap.style.setProperty("--holo-x", `${px.toFixed(1)}%`);
      wrap.style.setProperty("--holo-y", `${py.toFixed(1)}%`);
      wrap.style.transform = `perspective(800px) rotateX(${rotX.toFixed(2)}deg) rotateY(${rotY.toFixed(2)}deg) scale3d(1.02, 1.02, 1.02)`;
      wrap.classList.add("is-tilting");
    }

    function handleLeave() {
      wrap.style.transform = "perspective(800px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)";
      wrap.classList.remove("is-tilting");
    }

    wrap.addEventListener("mousemove", handleMove);
    wrap.addEventListener("mouseleave", handleLeave);

    wrap.addEventListener("touchmove", e => {
      if (e.touches && e.touches[0]) {
        handleMove(e.touches[0]);
      }
    }, { passive: true });
    wrap.addEventListener("touchend", handleLeave);

    if (typeof window !== "undefined" && window.DeviceOrientationEvent && !reducedMotion()) {
      window.addEventListener("deviceorientation", e => {
        const modal = $("#ownerCardModal");
        if (!modal || modal.classList.contains("hidden")) return;
        if (e.gamma === null || e.beta === null) return;
        const tiltX = Math.max(-25, Math.min(25, e.gamma));
        const tiltY = Math.max(-25, Math.min(25, e.beta - 45));
        const px = ((tiltX + 25) / 50) * 100;
        const py = ((tiltY + 25) / 50) * 100;
        const rotY = (tiltX / 25) * 7;
        const rotX = -(tiltY / 25) * 7;

        wrap.style.setProperty("--holo-x", `${px.toFixed(1)}%`);
        wrap.style.setProperty("--holo-y", `${py.toFixed(1)}%`);
        wrap.style.transform = `perspective(800px) rotateX(${rotX.toFixed(2)}deg) rotateY(${rotY.toFixed(2)}deg)`;
        wrap.classList.add("is-tilting");
      });
    }
  }

  function bindEvents() {
    $("#retryCatalog").addEventListener("click", loadCatalog);
    renderSkeletons();
    const personInput = $("#personInput");
    if (personInput) personInput.addEventListener("input", drawPersonPreview);
    $("#heroProductButton").addEventListener("click", () => {
      haptic("light");
      const slide = HERO_SLIDES[heroCarouselIndex];
      if (slide && slide.productId && heroCarouselIndex > 0) {
        openProduct(slide.productId);
      } else {
        browseCollection();
        scrollToId("catalog");
      }
    });
    if ($("#dropFilmButton")) $("#dropFilmButton").addEventListener("click", () => openStories(0));
    $("#zoomProduct").addEventListener("click", zoomProduct);
    $("#mediaPrev").addEventListener("click", () => stepMedia(-1));
    $("#mediaNext").addEventListener("click", () => stepMedia(1));
    $("#spinToggle").addEventListener("click", () => {
      if (state.viewer) state.viewer.setRotating(!state.viewer.rotating);
      updateMediaPosition();
    });
    $("#view3d").addEventListener("framechange", updateMediaPosition);
    const lightboxViewport = $("#lightboxViewport");
    const lightboxDrag = { pointerId: null, x: 0, y: 0, left: 0, top: 0 };
    $("#lightboxZoom").addEventListener("click", () => {
      setLightboxZoom(!lightboxViewport.classList.contains("zoomed"));
    });
    $("#lightboxImage").addEventListener("load", () => {
      if (lightboxViewport.classList.contains("zoomed")) centerLightboxImage();
    });
    lightboxViewport.addEventListener("pointerdown", event => {
      if (!lightboxViewport.classList.contains("zoomed") || lightboxDrag.pointerId !== null || !event.isPrimary) return;
      lightboxDrag.pointerId = event.pointerId;
      lightboxDrag.x = event.clientX;
      lightboxDrag.y = event.clientY;
      lightboxDrag.left = lightboxViewport.scrollLeft;
      lightboxDrag.top = lightboxViewport.scrollTop;
      lightboxViewport.setPointerCapture(event.pointerId);
      lightboxViewport.classList.add("dragging");
      event.preventDefault();
    });
    lightboxViewport.addEventListener("pointermove", event => {
      if (event.pointerId !== lightboxDrag.pointerId) return;
      lightboxViewport.scrollLeft = lightboxDrag.left - (event.clientX - lightboxDrag.x);
      lightboxViewport.scrollTop = lightboxDrag.top - (event.clientY - lightboxDrag.y);
    });
    const endLightboxDrag = event => {
      if (event.pointerId !== lightboxDrag.pointerId) return;
      lightboxDrag.pointerId = null;
      lightboxViewport.classList.remove("dragging");
      if (lightboxViewport.hasPointerCapture(event.pointerId)) lightboxViewport.releasePointerCapture(event.pointerId);
    };
    lightboxViewport.addEventListener("pointerup", endLightboxDrag);
    lightboxViewport.addEventListener("pointercancel", endLightboxDrag);
    lightboxViewport.addEventListener("lostpointercapture", endLightboxDrag);
    $$('[data-detail-product]').forEach(button => button.addEventListener('click', () => openProduct(button.dataset.detailProduct, Number(button.dataset.detailPhoto))));
    document.addEventListener("visibilitychange", () => {
      syncModalLayers();
      if (document.hidden) pauseProductVideo();
    });
    const enterBtn = $("#enterShop");
    if (enterBtn) {
      const ritual = $("#sealRitual"), progress = $("#sealProgressFill"), badge = $("#sealTimerBadge");
      let started = 0, timer = 0, animation = 0, half = false, completed = false, pointer = null, entryPending = false;
      const reset = () => { clearTimeout(timer); cancelAnimationFrame(animation); started = 0; pointer = null; half = false; ritual?.classList.remove("is-holding"); if (progress) progress.style.strokeDashoffset = "351.9"; if (badge) badge.textContent = ""; };
      const finishEntry = () => {
        if (entryPending || $("#welcome")?.classList.contains("is-leaving")) return;
        entryPending = true;
        ritual?.classList.add("is-activated");
        if (badge) badge.textContent = "Открываем магазин";
        haptic("heavy");
        window.setTimeout(() => {
          enterShop();
          ritual?.classList.remove("is-activated");
          entryPending = false;
        }, reducedMotion() ? 0 : 240);
      };
      enterBtn.addEventListener("pointerdown", event => {
        if (entryPending || event.button !== 0 || $("#welcome")?.classList.contains("is-leaving")) return;
        completed = false; reset(); pointer = event.pointerId; started = performance.now(); ritual?.classList.add("is-holding"); haptic("light");
        const tick = now => { if (!started) return; const ratio = Math.min(1, (now - started) / 1200); if (progress) progress.style.strokeDashoffset = String(351.9 * (1 - ratio)); if (ratio >= .5 && !half) { half = true; haptic("medium"); } if (ratio < 1) animation = requestAnimationFrame(tick); };
        animation = requestAnimationFrame(tick);
        timer = setTimeout(() => { if (pointer === null) return; completed = true; reset(); finishEntry(); }, 1200);
      });
      window.addEventListener("pointerup", event => { if (event.pointerId === pointer) reset(); });
      window.addEventListener("pointercancel", event => { if (event.pointerId === pointer) reset(); });
      enterBtn.addEventListener("click", event => { if (event.detail === 0 || !completed) finishEntry(); });
    }
    $("#smartSizer")?.addEventListener("click", event => {
      const step = event.target.closest("[data-sizer]");
      if (step) { const [field, direction] = step.dataset.sizer.split("-"); const delta = direction === "plus" ? 1 : -1; const limits = field === "height" ? [130,230] : [35,220]; combatSizer[field] = Math.max(limits[0], Math.min(limits[1], combatSizer[field] + delta)); updateCombatSizer(); haptic("light"); return; }
      const card = event.target.closest("[data-smart-size]"); if (!card || card.disabled) return;
      const size = card.dataset.smartSize; if (!((state.currentProduct?.sizes || []).includes(size))) return;
      saveJSON("vorozhbitov_fit", { height: String(combatSizer.height), weight: String(combatSizer.weight), fit: card.id === "adviceOversizeCard" ? "oversize" : "regular" }); selectSize(size); haptic("medium"); $("#addToCartButton")?.focus({preventScroll:true});
    });
    $$("[data-stage]").forEach(button => button.addEventListener("click", () => {
      setStage(button.dataset.stage);
      haptic("light");
    }));

    $("#categoryChips").addEventListener("click", event => {
      const button = event.target.closest("[data-category]");
      if (!button) return;
      state.category = button.dataset.category;
      haptic("light");
      renderCategoryChips();
      renderProducts();
    });

    $("#productGrid").addEventListener("click", event => {
      const flipBtn = event.target.closest('[data-action="flip"]');
      if (flipBtn) {
        event.stopPropagation();
        const card = flipBtn.closest(".product-card");
        if (card) {
          const isFlipped = card.classList.toggle("is-flipped");
          flipBtn.textContent = isFlipped ? "ПЕРЕД ↺" : "СПИНА ↺";
          flipBtn.setAttribute("aria-label", isFlipped ? "Показать вид спереди" : "Показать вид со спины");
          haptic("light");
        }
        return;
      }
      const card = event.target.closest("[data-product-id]");
      if (card) openProduct(card.dataset.productId);
    });

    let touchStartX = 0;
    let touchStartY = 0;
    $("#productGrid").addEventListener("touchstart", event => {
      const imgWrap = event.target.closest(".product-image.has-back");
      if (!imgWrap || !event.touches[0]) return;
      touchStartX = event.touches[0].clientX;
      touchStartY = event.touches[0].clientY;
    }, { passive: true });

    $("#productGrid").addEventListener("touchend", event => {
      const imgWrap = event.target.closest(".product-image.has-back");
      if (!imgWrap || !event.changedTouches || !event.changedTouches[0]) return;
      const dx = event.changedTouches[0].clientX - touchStartX;
      const dy = event.changedTouches[0].clientY - touchStartY;
      if (Math.abs(dx) > 35 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        const card = imgWrap.closest(".product-card");
        if (card) {
          const flipBtn = card.querySelector('[data-action="flip"]');
          const shouldFlip = dx < 0;
          if (card.classList.contains("is-flipped") !== shouldFlip) {
            card.classList.toggle("is-flipped", shouldFlip);
            if (flipBtn) {
              flipBtn.textContent = shouldFlip ? "ПЕРЕД ↺" : "СПИНА ↺";
              flipBtn.setAttribute("aria-label", shouldFlip ? "Показать вид спереди" : "Показать вид со спины");
            }
            haptic("light");
          }
        }
      }
    }, { passive: true });

    $("#returnHelp").addEventListener("click", () => {
      const product = state.currentProduct;
      const text = `Здравствуйте! Хочу уточнить вопрос по заказу${product ? ` «${product.name}»` : ""}. Номер заказа: `;
      const url = checkoutClient.managerUrl(state.data.support_username, text);
      if (!url) { showToast("Контакт магазина пока не подключён."); return; }
      if (tg && typeof tg.openTelegramLink === "function") tg.openTelegramLink(url);
      else window.open(url, "_blank", "noopener,noreferrer");
      showToast("Чат открыт. Добавь детали и отправь сообщение.");
    });
    $("#communityTab").addEventListener("click", () => openModal("communityModal"));
    $("#openCommunityFromHome").addEventListener("click", () => openModal("communityModal"));
    $("#sizeGuideToggle")?.addEventListener("click", () => {
      const guide = $("#sizeGuide");
      if (!guide) return;
      const isHidden = guide.classList.toggle("hidden");
      $("#sizeGuideToggle").setAttribute("aria-expanded", String(!isHidden));
      $("#sizeGuideToggle").textContent = isHidden ? "ТАБЛИЦА В СМ ▾" : "СКРЫТЬ ТАБЛИЦУ ▴";
      if (!isHidden) {
        window.requestAnimationFrame(() => {
          guide.scrollIntoView({ behavior: reducedMotion() ? "instant" : "smooth", block: "nearest" });
        });
      }
      haptic("light");
    });


    $("#sizeList").addEventListener("click", event => {
      const button = event.target.closest("[data-size]");
      if (!button) return;
      selectSize(button.dataset.size);
      haptic("light");
    });
    if ($("#fitFinder")) $("#fitFinder").addEventListener("click", event => {
      const choice = event.target.closest("[data-fit-choice]");
      if (choice) setFitChoice(choice.dataset.fitChoice);
      const preset = event.target.closest("[data-fit-preset]");
      if (preset) {
        const [h, w] = preset.dataset.fitPreset.split(",");
        if ($("#fitHeight")) $("#fitHeight").value = h;
        if ($("#fitWeight")) $("#fitWeight").value = w;
        recommendFit();
        haptic("selection");
      }
    });
    if ($("#fitRecommend")) $("#fitRecommend").addEventListener("click", recommendFit);

    $("#beginCheckout").addEventListener("click", () => {
      const pairs = cartItems();
      if (!state.catalogReady || !pairs.length || pairs.length !== state.cart.length || pairs.some(({item, product}) => product.stock_check_unavailable || !Core.available(product) || !product.sizes.includes(item.size))) {
        renderCart(); showToast("Проверь состав корзины и наличие вещей."); return;
      }
      const gate = dropPurchaseGate();
      if (gate) { showToast(gate); return; }
      const lines = pairs.map(({item, product}, index) => `${index + 1}. ${product.name} · размер ${item.size} · ${item.qty} шт. · ${rubles(priceNumber(product.price) * item.qty)}${item.person ? ` · номер: ${String(item.person).slice(0, 32)}` : ""}${/предзаказ/i.test(`${product.badge || ""} ${product.stock_label || ""}`) ? " · предзаказ" : ""}`);
      const message = ["Здравствуйте! Хочу оформить заказ в «СИЛА И ЧЕСТЬ»:", ...lines, `Итого за вещи: ${rubles(cartTotal())}`, "Прошу подтвердить наличие, подарок, доставку и оплату."].join("\n");
      const url = message.length <= 3500 ? checkoutClient.managerUrl(state.data.order_manager_username, message) : "";
      if (!url) { showToast("Не удалось подготовить заказ. Корзина сохранена."); return; }
      if (tg && typeof tg.openTelegramLink === "function") tg.openTelegramLink(url);
      else window.open(url, "_blank", "noopener,noreferrer");
      haptic("light");
      showToast("Проверь текст и отправь сообщение в чат. До отправки заказ не создан.");
    });
    $("#backToCart").addEventListener("click", () => {
      state.cartStep = "items";
      renderCart();
      $(".cart-sheet").scrollTop = 0;
      $("#beginCheckout").focus({preventScroll: true});
    });
    $("#galleryGroups").addEventListener("click", event => {
      const button = event.target.closest("[data-gallery-group]");
      if (!button || !state.currentProduct) return;
      state.galleryGroup = button.dataset.galleryGroup;
      renderGallery(state.currentProduct);
      updateMediaPosition();
    });
    $("#galleryDots").addEventListener("click", event => {
      const button = event.target.closest("[data-gallery]");
      if (button) setGallery(Number(button.dataset.gallery));
    });
    $("#galleryDots").addEventListener("error", event => {
      if (event.target.tagName === "IMG") event.target.style.visibility = "hidden";
    }, true);
    $("#galleryDots").addEventListener("load", event => {
      if (event.target.tagName === "IMG") event.target.style.visibility = "visible";
    }, true);
    let photoSwipeFinishedAt = -Infinity;
    $("#sheetImage").addEventListener("click", event => {
      // A touch swipe may be followed by a synthetic click; keep tap-to-zoom.
      if (event.detail !== 0 && performance.now() - photoSwipeFinishedAt < 600) return;
      zoomProduct();
    });
    $("#sheetImage").addEventListener("load", () => $("#mediaError").classList.add("hidden"));
    $("#sheetImage").addEventListener("error", () => {
      if (state.stage === "photo") $("#mediaError").classList.remove("hidden");
    });
    $("#retryPhoto").addEventListener("click", () => {
      $("#mediaError").classList.add("hidden");
      const photo = $("#sheetImage");
      photo.src = galleryImages(state.currentProduct)[state.galleryIndex];
      $$("#galleryDots img").forEach(image => { image.src = image.getAttribute("src"); });
    });

    let sheetTouchStartX = 0;
    let sheetTouchStartY = 0;
    const sheetImgWrap = $(".sheet-image");
    if (sheetImgWrap) {
      sheetImgWrap.addEventListener("touchstart", event => {
        if (!event.touches || !event.touches[0]) return;
        sheetTouchStartX = event.touches[0].clientX;
        sheetTouchStartY = event.touches[0].clientY;
      }, { passive: true });
      sheetImgWrap.addEventListener("touchend", event => {
        if (state.stage !== "photo" || !event.changedTouches || !event.changedTouches[0]) return;
        const dx = event.changedTouches[0].clientX - sheetTouchStartX;
        const dy = event.changedTouches[0].clientY - sheetTouchStartY;
        if (Math.abs(dx) > 35 && Math.abs(dx) > Math.abs(dy) * 1.4) {
          photoSwipeFinishedAt = performance.now();
          stepMedia(dx < 0 ? 1 : -1);
          haptic("light");
        }
      }, { passive: true });
    }

    $("#sheetQty").addEventListener("click", event => {
      if (event.target.closest("[data-sheet-qty='plus']")) { state.qty = Math.min(20, state.qty + 1); haptic("light"); }
      if (event.target.closest("[data-sheet-qty='minus']")) { state.qty = Math.max(1, state.qty - 1); haptic("light"); }
      $("#qtyValue").textContent = String(state.qty);
    });

    $("#related").addEventListener("click", event => {
      const card = event.target.closest("[data-related]");
      if (card) openProduct(card.dataset.related);
    });

    $("#addToCartButton").addEventListener("click", addToCart);
    if ($("#directOrderButton")) {
      $("#directOrderButton").addEventListener("click", () => {
        const product = state.currentProduct && productById(state.currentProduct.id);
        if (!product || !Core.buyable(product)) return;
        if (!state.selectedSize) {
          $("#sizeHint").textContent = "Сначала выбери размер.";
          $("#sizeHint").classList.add("error");
          $(".size-block")?.scrollIntoView({block:"center", behavior:reducedMotion() ? "instant" : "smooth"});
          $("#sizeList button")?.focus({preventScroll:true});
          haptic("error");
          return;
        }
        const verb = /предзаказ/i.test(`${product.badge || ""} ${product.stock_label || ""}`) ? "предзаказ" : "заказ";
        const phone = (state.profile?.phone || "").trim();
        const message = [
          `Здравствуйте! Хочу оформить ${verb}:`,
          `• ${product.name} · размер ${state.selectedSize} · 1 шт. · ${product.price}`,
          `• Подарок: ЖЕТОН (первые 100 жетонов к дропу 001, со 2-го дропа в подарок не будет)`,
          `Телефон: ${phone || "укажу в чате"}`
        ].join("\n");
        const manager = state.data?.order_manager_username || "intellectual_sabotage";
        const url = checkoutClient.managerUrl(manager, message);
        if (url) {
          if (tg && typeof tg.openTelegramLink === "function") tg.openTelegramLink(url);
          else window.open(url, "_blank", "noopener,noreferrer");
          haptic("light");
        }
      });
    }
    $("#waitlistButton").addEventListener("click", sendWaitlist);
    $("#cartButton").addEventListener("click", openCart);
    $("#bottomCartButton").addEventListener("click", openCart);
    if ($("#profileButton")) $("#profileButton").addEventListener("click", openProfile);
    if ($("#bottomProfileButton")) $("#bottomProfileButton").addEventListener("click", openProfile);
    $$("#profileModal input").forEach(input => {
      input.addEventListener("pointerdown", e => e.stopPropagation());
      input.addEventListener("click", e => e.stopPropagation());
      input.addEventListener("focus", e => {
        e.stopPropagation();
        setTimeout(() => input.scrollIntoView({ behavior: "smooth", block: "center" }), 180);
      });
    });
    if ($("#saveProfile")) $("#saveProfile").addEventListener("click", saveProfile);
    if ($("#profileOrders")) $("#profileOrders").addEventListener("click", event => {
      if (event.target.closest("[data-retry-orders]")) { loadProfileOrders(); return; }
      const ownerButton = event.target.closest("[data-owner-card]");
      if (ownerButton) {
        openOwnerCard(ownerButton.dataset.ownerCard);
        return;
      }
      const payButton = event.target.closest("[data-pay-id]");
      if (payButton) {
        resumePayment(payButton.dataset.payId);
        return;
      }
      const button = event.target.closest("[data-cancel-order]");
      if (!button) return;
      const orderId = Number(button.dataset.cancelOrder);
      if (!orderId) return;
      button.disabled = true;
      cancelProfileOrder(orderId);
    });
    if ($("#profileCartBtn")) $("#profileCartBtn").addEventListener("click", () => { closeModal("profileModal"); openCart(); });
    if ($("#profileDeliver")) $("#profileDeliver").addEventListener("click", event => {
      const button = event.target.closest("[data-profile-deliver]");
      if (!button) return;
      state.profile.deliver = button.dataset.profileDeliver;
      $$("#profileDeliver .filter-chip").forEach(node => node.classList.toggle("active", node === button));
    });
    $("#cartContent").addEventListener("click", event => {
      if (event.target.closest("[data-cart-retry]")) { loadCatalog(); return; }
      if (event.target.closest("[data-cart-shop]")) { closeModal("cartModal"); scrollToId("catalog"); return; }
      const row = event.target.closest("[data-cart-key]");
      if (!row) return;
      const key = row.dataset.cartKey;
      if (event.target.closest("[data-qty='plus']")) updateCartItem(key, 1);
      if (event.target.closest("[data-qty='minus']")) updateCartItem(key, -1);
      const remove = event.target.closest("[data-remove-key]");
      if (remove) removeCartItem(remove.dataset.removeKey);
    });

    $("#searchToggle").addEventListener("click", () => {
      $("#searchOverlay").classList.toggle("hidden");
      $("#searchToggle").setAttribute("aria-expanded", String(!$("#searchOverlay").classList.contains("hidden")));
      if (!$("#searchOverlay").classList.contains("hidden")) $("#searchInput").focus();
    });
    let searchRenderTimer = null;
    $("#searchInput").addEventListener("input", event => {
      state.search = event.target.value;
      $("#clearSearch").classList.toggle("hidden", !state.search);
      window.clearTimeout(searchRenderTimer);
      searchRenderTimer = window.setTimeout(renderProducts, 120);
    });
    $("#searchInput").addEventListener("keydown", event => {
      if (event.key === "Enter") {
        $("#searchOverlay").classList.add("hidden");
        $("#searchToggle").setAttribute("aria-expanded", "false");
        scrollToId("catalog");
      }
    });
    $("#clearSearch").addEventListener("click", () => {
      state.search = "";
      $("#searchInput").value = "";
      $("#clearSearch").classList.add("hidden");
      renderProducts();
    });
    $$("[data-hint]").forEach(button => button.addEventListener("click", () => {
      state.search = button.dataset.hint;
      $("#searchInput").value = state.search;
      $("#clearSearch").classList.remove("hidden");
      renderProducts();
      $("#searchOverlay").classList.add("hidden");
      $("#searchToggle").setAttribute("aria-expanded", "false");
      scrollToId("catalog");
    }));

    $("#filterToggle").addEventListener("click", () => {
      $("#filterPanel").classList.toggle("hidden");
      $("#filterToggle").setAttribute("aria-expanded", String(!$("#filterPanel").classList.contains("hidden")));
    });
    $("#filterOptions").addEventListener("click", event => {
      const button = event.target.closest("[data-stock]");
      if (!button) return;
      $$("#filterOptions .filter-chip").forEach(node => node.classList.toggle("active", node === button));
      state.stock = button.dataset.stock || "all";
      updateFilterCount();
      renderProducts();
    });
    $("#sizeFilters").addEventListener("click", event => {
      const button = event.target.closest("[data-size-filter]");
      if (!button) return;
      $$("#sizeFilters .filter-chip").forEach(node => { node.classList.toggle("active", node === button); node.setAttribute("aria-pressed", String(node === button)); });
      state.sizeFilter = button.dataset.sizeFilter || "all";
      updateFilterCount();
      renderProducts();
    });
    $("#sortSelect").addEventListener("change", event => {
      state.sort = event.target.value;
      renderProducts();
    });
    $("#resetFilters").addEventListener("click", () => browseCollection());
    $("#emptyReset").addEventListener("click", () => $("#resetFilters").click());
    $("#submitManagerOrder").addEventListener("click", submitManagerOrder);
    $("#askDelivery").addEventListener("click", askDelivery);
    $("#privacyInfoButton")?.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      openModal("privacyModal");
      haptic("light");
    });
    ["checkoutName", "checkoutPhone", "checkoutCity", "checkoutConsent"].forEach(id => {
      const field = $("#" + id);
      field.addEventListener(id === "checkoutConsent" ? "change" : "input", () => clearCheckoutFieldError(field));
    });
    $("#submitOrder").addEventListener("click", reviewOrder);
    $("#confirmOrder").addEventListener("click", submitManagerOrder);
    $("#successOrders").addEventListener("click", () => { closeModal("payModal"); openProfile(); $("#profileOrders")?.scrollIntoView({ block: "start" }); });
    $("#successSupport").addEventListener("click", () => {
      const account = String(state.data.support_username || "").replace(/^@/, "");
      if (!/^[A-Za-z0-9_]{5,32}$/.test(account)) { showToast("Контакт магазина пока не подключён."); return; }
      const url = `https://t.me/${account}`;
      if (tg && typeof tg.openTelegramLink === "function") tg.openTelegramLink(url);
      else window.open(url, "_blank", "noopener,noreferrer");
    });
    if ($("#payMethods")) $("#payMethods").addEventListener("click", async event => {
      const retry = event.target.closest("[data-pay-retry]");
      if (retry) {
        if (retry.disabled) return;
        retry.disabled = true;
        try { await resumePayment(retry.dataset.payRetry); }
        finally { retry.disabled = false; }
        return;
      }
      const button = event.target.closest("[data-pay-method]");
      if (!button) return;
      startPayMethod(button.dataset.payMethod, button.dataset.payUrl || "", button.dataset.payKind || "link");
    });
    $("#channelButton").addEventListener("click", openChannel);
    if ($("#clubAction")) $("#clubAction").addEventListener("click", clubAction);
    if ($("#profileClubButton")) $("#profileClubButton").addEventListener("click", () => { closeModal("profileModal"); scrollToId("club"); });
    if ($("#communitySubmit")) $("#communitySubmit").addEventListener("click", submitCommunityPhoto);
    if ($("#communityGrid")) {
      $("#communityGrid").addEventListener("click", event => {
        const card = event.target.closest("[data-community-product]");
        if (card) openProduct(card.dataset.communityProduct);
      });
      $("#communityGrid").addEventListener("keydown", event => {
        const card = event.target.closest("[data-community-product]");
        if (card && ["Enter", " "].includes(event.key)) { event.preventDefault(); openProduct(card.dataset.communityProduct); }
      });
    }
    if ($("#filmPrev")) $("#filmPrev").addEventListener("click", () => showFilmScene(Math.max(0, state.filmIndex - 1)));
    if ($("#filmNext")) $("#filmNext").addEventListener("click", () => showFilmScene(state.filmIndex + 1));
    if ($("#filmProduct")) $("#filmProduct").addEventListener("click", () => {
      const scene = filmScenes()[state.filmIndex];
      closeFilm();
      if (scene && scene.product_id) openProduct(scene.product_id);
    });
    if ($("#filmCatalog")) $("#filmCatalog").addEventListener("click", () => { closeFilm(); browseCollection(); scrollToId("catalog"); });
    if ($(".film-media")) {
      let filmTouchX = null;
      $(".film-media").addEventListener("pointerdown", event => { filmTouchX = event.clientX; }, { passive: true });
      $(".film-media").addEventListener("pointerup", event => {
        if (filmTouchX === null) return;
        const delta = event.clientX - filmTouchX;
        filmTouchX = null;
        if (Math.abs(delta) < 42) return;
        showFilmScene(state.filmIndex + (delta < 0 ? 1 : -1));
      }, { passive: true });
    }
    document.addEventListener("keydown", event => {
      if (state.modalStack.at(-1) !== "filmModal" || event.target.matches("input,select,textarea")) return;
      if (event.key === "ArrowRight") { event.preventDefault(); showFilmScene(state.filmIndex + 1); }
      if (event.key === "ArrowLeft") { event.preventDefault(); showFilmScene(Math.max(0, state.filmIndex - 1)); }
    });
    if ($("#ownerShare")) $("#ownerShare").addEventListener("click", shareOwnerCard);
    if ($("#ownerDownload")) $("#ownerDownload").addEventListener("click", event => {
      const href = event.currentTarget.getAttribute("href") || "";
      if (!href || href === "#") {
        event.preventDefault();
        showToast("Карта ещё собирается — секунду.");
      }
    });
    $("#closeToast").addEventListener("click", () => $("#successToast").classList.add("hidden"));

    $("#deliverRow").addEventListener("click", event => {
      const button = event.target.closest("[data-deliver]");
      if (!button) return;
      state.deliver = button.dataset.deliver;
      $$("#deliverRow .filter-chip").forEach(node => { node.classList.toggle("active", node === button); node.setAttribute("aria-pressed", String(node === button)); });
      renderDeliverySummary();
    });
    $("#checkoutCity").addEventListener("change", renderDeliverySummary);

    document.addEventListener("click", event => {
      const shot = event.target.closest("[data-lightbox]");
      if (!shot) return;
      if (shot.closest(".modal-backdrop")) return;
      openLightbox(shot.dataset.lightbox, shot.dataset.caption || "");
    });
    document.addEventListener("click", event => {
      const card = event.target.closest("[data-story-index]");
      if (!card || card.closest(".modal-backdrop")) return;
      openStories(Number(card.dataset.storyIndex) || 0);
    });

    $$("[data-scroll]").forEach(button => button.addEventListener("click", () => scrollToId(button.dataset.scroll)));
    $$("[data-close]").forEach(button => button.addEventListener("click", () => closeModal(button.dataset.close)));
    $$(".modal-backdrop").forEach(backdrop => backdrop.addEventListener("click", event => {
      if (event.target === backdrop) closeModal(backdrop.id);
    }));
    document.addEventListener("keydown", event => {
      const top = document.getElementById(state.modalStack.at(-1)) || (document.body.classList.contains("welcoming") ? $("#welcome") : null);
      if (event.key === "Tab" && top) {
        const nodes = focusableElements(top);
        if (!nodes.length) return;
        const first = nodes[0], last = nodes.at(-1);
        if (event.shiftKey && (document.activeElement === first || !top.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !top.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
      }
      if (event.target.matches('[data-lightbox]') && ["Enter", " "].includes(event.key)) {
        event.preventDefault(); openLightbox(event.target.dataset.lightbox, event.target.dataset.caption);
      }
      if (event.target.matches('[data-story-index]') && ["Enter", " "].includes(event.key)) {
        event.preventDefault(); openStories(Number(event.target.dataset.storyIndex) || 0);
      }
      if (event.target.matches("[data-stage]") && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const tabs = $$("[data-stage]").filter(tab => !tab.classList.contains("hidden"));
        const current = tabs.indexOf(event.target);
        const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
          : (current + (event.key === "ArrowLeft" ? -1 : 1) + tabs.length) % tabs.length;
        setStage(tabs[next].dataset.stage);
        tabs[next].focus();
        return;
      }
      if (top && top.id === "productModal" && state.stage === "photo" && !event.target.matches('input,select,textarea') && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.preventDefault(); stepMedia(event.key === "ArrowLeft" ? -1 : 1);
      }
      if (event.key !== "Escape") return;
      const search = $("#searchOverlay");
      if (search && !search.classList.contains("hidden") && !state.modalStack.length) {
        search.classList.add("hidden");
        $("#searchToggle").setAttribute("aria-expanded", "false");
        $("#searchToggle").focus();
        return;
      }
      closeTopModal();
    });

    const sections = ["home", "catalog", "manifesto", "channel"];
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          $$(".bottom-link[data-scroll]").forEach(link => link.classList.toggle("active", link.dataset.scroll === entry.target.id));
        }
      });
    }, { rootMargin: "-35% 0px -55% 0px", threshold: 0 });
    sections.forEach(id => { const element = document.getElementById(id); if (element) observer.observe(element); });
  }

  iconize();
  configureTelegram();
  bindEvents();
  setupHeroParallax();
  syncHeroPoster();
  setupHeroCarousel();
  setupOwnerTilt();
  setupClubLifter();
  renderDropEvent();
  renderCommunity();
  renderClub();
  updateCounters();
  startExperience();
  loadCatalog();
  refreshReturnPayment();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshReturnPayment(); });
  window.VorozhbitovShop = { state, openProduct, openCart };
})();
