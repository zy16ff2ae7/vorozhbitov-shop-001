(() => {
  "use strict";

  const Core = window.ShopCore;
  const FALLBACK_CATALOG = {
    brand: { name: "ВОРОЖБИТОВ", descriptor: "Сила и честь", drop: "ВЫПУСК 001" },
    channel_url: "https://t.me/+XufFz8GGR0o3Njky",
    privacy_url: "",
    categories: [], products: [], lookbook: []
  };

  const ICONS = {
    bookmark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4.5A2.5 2.5 0 0 1 8.5 2h7A2.5 2.5 0 0 1 18 4.5V22l-6-3.8L6 22V4.5Z"/></svg>',
    bag: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 8h14l1 13H4L5 8Z"/><path d="M8.5 9V6a3.5 3.5 0 0 1 7 0v3"/></svg>',
    sliders: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/><path d="M8 4v4M15 10v4M10 16v4"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.6"/><path d="m16 16 5 5"/></svg>',
    share: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.8 7.5-4.3M8.2 13.2l7.5 4.3"/></svg>',
    home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 10.8 9-7.3 9 7.3V21H3V10.8Z"/><path d="M9 21v-6h6v6"/></svg>',
    grid: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>',
    pin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s7-6.2 7-11.2A7 7 0 0 0 5 9.8C5 14.8 12 21 12 21Z"/><circle cx="12" cy="9.8" r="2.2"/></svg>',
    user: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.4"/><path d="M5 20c1.2-3.8 3.8-5.6 7-5.6s5.8 1.8 7 5.6"/></svg>'
  };


  const state = {
    data: FALLBACK_CATALOG,
    category: "all",
    search: "",
    stock: "all",
    sizeFilter: "all",
    sort: "featured",
    view: "all",
    cart: Core.cleanCart(loadJSON("vorozhbitov_cart", [])),
    saved: Core.stringList(loadJSON("vorozhbitov_saved", [])),
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
    deliver: "СДЭК",
    toastTimer: null,
    modalStack: [],
    viewer: null,
    stage: "3d"
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

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
    return list.map(String);
  }

  function imageFor(product) {
    return imagesFor(product)[0];
  }

  function productById(id) {
    return state.data.products.find(product => product.id === id);
  }

  function priceNumber(price) {
    const parsed = Number(String(price || "0").replace(/[^0-9]/g, ""));
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function rubles(value) {
    return `${Math.round(value).toLocaleString("ru-RU")} ₽`;
  }

  function isLimited(product) {
    return /limited|лимит|последн|мало|выпуск|drop|дроп/i.test(`${product.badge || ""} ${product.stock_label || ""}`);
  }

  function telegramUser() {
    return (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || null;
  }

  function profileScore(profile) {
    return ["name", "phone", "city", "address", "size"].reduce((n, key) => n + (String(profile[key] || "").trim() ? 1 : 0), 0);
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
      height: ($("#profileHeight") && $("#profileHeight").value.trim()) || "",
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
    if ($("#profileHeight")) $("#profileHeight").value = profile.height || "";
    if ($("#profileNote")) $("#profileNote").value = profile.note || "";
    const letter = (profile.name || (user && user.first_name) || "V").trim().charAt(0).toUpperCase();
    if ($("#profileAvatar")) $("#profileAvatar").textContent = letter || "V";
    if ($("#profileHello")) $("#profileHello").textContent = profile.name || (user && user.first_name) || "Свой";
    if ($("#profileHandle")) {
      $("#profileHandle").textContent = user && user.username ? `@${user.username}` : "Данные для заявки и доставки";
    }
    if ($("#profileFill")) $("#profileFill").textContent = `${profileScore(profile)} / 5`;
    $$("#profileDeliver .filter-chip").forEach(node => node.classList.toggle("active", node.dataset.profileDeliver === (profile.deliver || "СДЭК")));
    $$("#profileSizeRow .filter-chip").forEach(node => node.classList.toggle("active", node.dataset.profileSize === profile.size));
    if ($("#profileSavedN")) $("#profileSavedN").textContent = String(state.saved.length);
    if ($("#profileCartN")) $("#profileCartN").textContent = String(state.cart.reduce((sum, item) => sum + Number(item.qty || 0), 0));
  }

  function formatOrderWhen(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  }

  function localOrdersAsCards() {
    const stored = loadJSON("vorozhbitov_orders", []);
    return (Array.isArray(stored) ? stored : []).filter(row => row && Array.isArray(row.items)).slice(0, 8).map(row => {
      const names = (row.items || []).map(item => {
        const product = productById(item.product_id);
        const label = product ? product.name : "Вещь";
        return item.size ? `${label} · ${item.size}` : label;
      }).filter(Boolean);
      const quantity = (row.items || []).reduce((sum, item) => sum + Number(item.quantity || 1), 0);
      return {
        id: null,
        status: "local",
        status_label: "на устройстве",
        product_name: names.join(", ") || "Заявка",
        size: "",
        quantity,
        created_at: row.at,
        can_cancel: false
      };
    });
  }

  function renderProfileOrders(orders, source) {
    const box = $("#profileOrders");
    if (!box) return;
    if (!orders.length) {
      box.innerHTML = source === "bot"
        ? `<p class="profile-empty">Заявок пока нет. Когда оплатишь из витрины — статус появится здесь.</p>`
        : `<p class="profile-empty">Заявок с этого устройства пока нет. В Telegram из бота подтянутся статусы: ждёт оплаты, оплачена, отменена.</p>`;
      return;
    }
    const caption = source === "bot" ? "Заявки" : "На этом устройстве";
    box.innerHTML = `<p class="profile-caption">${caption}</p>` + orders.map(row => {
      const status = row.status || "new";
      const when = formatOrderWhen(row.created_at || row.at);
      const size = row.size ? ` · ${row.size}` : "";
      const qty = Number(row.quantity || 1);
      const pay = row.can_pay && row.payment_id
        ? `<button type="button" class="profile-order-pay" data-pay-id="${escapeHTML(row.payment_id)}">Оплатить</button>`
        : "";
      const cancel = row.can_cancel
        ? `<button type="button" class="profile-order-cancel" data-cancel-order="${Number(row.id)}">Отменить всю заявку</button>`
        : "";
      return `<article class="profile-order" data-status="${escapeHTML(status)}">
        <div class="profile-order-main">
          <strong>${escapeHTML(row.product_name || "Вещь")}${escapeHTML(size)}</strong>
          <small>${escapeHTML(when)}${qty > 1 ? ` · ${qty} шт.` : ""}${row.amount_label ? ` · ${escapeHTML(row.amount_label)}` : ""}</small>
        </div>
        <div class="profile-order-side">
          <span class="profile-order-status is-${escapeHTML(status)}">${escapeHTML(row.status_label || status)}</span>
          ${pay}${cancel}
        </div>
      </article>`;
    }).join("");
  }

  async function loadProfileOrders() {
    const box = $("#profileOrders");
    if (!box) return;
    box.innerHTML = `<p class="profile-empty">Загружаю заявки…</p>`;
    const initData = tg && tg.initData;
    if (initData) {
      try {
        const response = await fetch("/api/my-orders", {
          headers: { Accept: "application/json", "X-Telegram-Init-Data": initData }
        });
        if (response.ok) {
          const payload = await response.json();
          renderProfileOrders(payload.orders || [], "bot");
          return;
        }
      } catch (_) { /* fall back to device history */ }
    }
    renderProfileOrders(localOrdersAsCards(), "local");
  }

  async function cancelProfileOrder(orderId) {
    const initData = tg && tg.initData;
    if (!initData) {
      showToast("Отмена — в чате с ботом, «Мои заявки».");
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
      showToast("Все позиции общей заявки отменены.");
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
      const bits = [profile.address, profile.entrance, profile.note].filter(Boolean);
      $("#checkoutNote").value = bits.join(" · ");
    }
    if (profile.deliver) {
      state.deliver = profile.deliver;
      $$("#deliverRow .filter-chip").forEach(node => node.classList.toggle("active", node.dataset.deliver === profile.deliver));
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
    showToast("Профиль сохранён. В заявке подставится сам.");
  }

  function openProfile() {
    fillProfileForm();
    loadProfileOrders();
    openModal("profileModal");
    haptic("light");
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.innerHTML = '<span class="toast-mark" aria-hidden="true">V</span><span>' + escapeHTML(message) + "</span>";
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
    if (top !== "teaserModal" || document.hidden) $("#teaserVideo").pause();
    syncWelcomeVideo();
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
    if (id === "productModal") stop3D();
    if (id === "payModal") stopPayPoll();
    if (id === "teaserModal") closeTeaser();
    if (id === "storiesModal") stopStories();
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

  function setStage(mode, silent) {
    state.stage = mode;
    const canvas = $("#view3d");
    const photo = $("#sheetImage");
    const hint = $("#view3dHint");
    const dots = $("#galleryDots");
    if ($("#stage3d")) $("#stage3d").classList.toggle("active", mode === "3d");
    if ($("#stagePhoto")) $("#stagePhoto").classList.toggle("active", mode === "photo");
    if (canvas) canvas.classList.toggle("hidden", mode !== "3d");
    if (photo) photo.classList.toggle("hidden", mode !== "photo");
    if (hint) hint.classList.toggle("hidden", mode !== "3d");
    if (dots) dots.classList.toggle("hidden", mode !== "photo");
    $("#spinToggle").classList.toggle("hidden", mode !== "3d");
    $$("[data-stage]").forEach(button => button.setAttribute("aria-selected", String(button.dataset.stage === mode)));
    updateMediaPosition();
    if (silent) return;
    if (mode === "3d" && state.currentProduct) start3D(state.currentProduct);
    else stop3D();
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
    const visible = state.data.categories.filter(category => state.data.products.some(product => product.active !== false && product.category === category.id));
    const all = [{ id: "all", name: "Все вещи" }, ...visible];
    root.innerHTML = all.map(category => `<button class="category-chip ${state.category === category.id ? "active" : ""}" data-category="${escapeHTML(category.id)}" type="button" aria-pressed="${state.category === category.id}">${escapeHTML(category.name)}</button>`).join("");
  }

  function filteredProducts() {
    const search = state.search.trim().toLowerCase();
    let list = state.data.products.filter(product => {
      if (product.active === false) return false;
      if (state.view === "saved" && !state.saved.includes(product.id)) return false;
      if (state.category !== "all" && product.category !== state.category) return false;
      if (state.stock === "limited" && !isLimited(product)) return false;
      if (state.stock === "available" && !Core.available(product)) return false;
      if (state.sizeFilter !== "all" && !(product.sizes || []).includes(state.sizeFilter)) return false;
      if (search && !`${product.name} ${product.description} ${product.badge} ${product.material} ${product.signature || ""} ${product.print || ""}`.toLowerCase().includes(search)) return false;
      return true;
    });
    if (state.sort === "price-asc") list = [...list].sort((a, b) => priceNumber(a.price) - priceNumber(b.price));
    else if (state.sort === "price-desc") list = [...list].sort((a, b) => priceNumber(b.price) - priceNumber(a.price));
    else if (state.sort === "limited") list = [...list].sort((a, b) => Number(isLimited(b)) - Number(isLimited(a)));
    return list;
  }

  function renderProductCard(product, index) {
    const saved = state.saved.includes(product.id);
    return `<article class="product-card" data-product-id="${escapeHTML(product.id)}" style="--i:${index}">
      <div class="product-image">
        <img src="${escapeHTML(imageFor(product))}" alt="${escapeHTML(product.name)}" loading="lazy">
        <span class="product-badge">${escapeHTML(product.badge || "БАЗА")}</span>
        <span class="badge-3d">360°</span>
        <button class="product-save ${saved ? "saved" : ""}" data-save-id="${escapeHTML(product.id)}" type="button" aria-label="${saved ? "Удалить из сохранённых" : "Сохранить"}"><span class="icon" data-icon="bookmark"></span></button>
        <span class="product-hover">РАССМОТРЕТЬ <b>↗</b></span>
      </div>
      <div class="product-info">
        <div class="product-topline"><span>${escapeHTML(categoryName(product.category))}</span><span class="product-stock">${escapeHTML(product.stock_label || "В наличии")}</span></div>
        <h3><button type="button" class="product-open" aria-label="Открыть ${escapeHTML(product.name)}">${escapeHTML(product.name)}</button></h3>
        <div class="product-sizes">${(product.sizes || []).map(escapeHTML).join(" · ") || "Ждём пополнение"}</div>
        <div class="product-bottom"><strong class="product-price">${escapeHTML(product.price)}</strong><span class="product-fit">${escapeHTML(product.fit || "Свободный крой")}</span></div>
      </div>
    </article>`;
  }

  function renderProducts() {
    const products = filteredProducts();
    const root = $("#productGrid");
    $("#productCount").textContent = Core.productCount(products.length);
    root.innerHTML = products.map(renderProductCard).join("");
    $("#emptyState").classList.toggle("hidden", products.length > 0 || !state.catalogReady);
    const banner = $("#viewBanner");
    banner.classList.toggle("hidden", state.view !== "saved");
    $("#viewBannerTitle").textContent = "СОХРАНЁННЫЕ";
    iconize(root);
    updateCounters();
  }

  function lookbookList() {
    const items = (state.data.lookbook || []).filter(item => item.image || item.photo_url);
    return items.length ? items : FALLBACK_CATALOG.lookbook;
  }

  function renderLookbook() {
    const list = lookbookList();
    $("#lookbookGrid").innerHTML = list.map((item, index) => {
      const src = item.image || item.photo_url;
      const caption = item.caption || "ЗАМЕТКА";
      return `<figure class="lookbook-card" tabindex="0" role="button" data-story-index="${index}" data-caption="${escapeHTML(caption)}"><img src="${escapeHTML(src)}" alt="${escapeHTML(caption)}" loading="lazy"><figcaption><strong>${escapeHTML(caption)}</strong></figcaption></figure>`;
    }).join("") + `<div class="lookbook-note"><span class="red-slash">//</span><p>Кадры выпуска. Посадка, ткань, крой.</p><button class="button button-outline" data-scroll="catalog" type="button">В ВИТРИНУ <span>↗</span></button></div>`;
    $("#lookbookGrid").querySelector("[data-scroll]")?.addEventListener("click", () => scrollToId("catalog"));
  }

  function galleryLabel(product, index) {
    return (product.image_labels || [])[index] || `Фото ${index + 1}`;
  }

  function updateMediaPosition() {
    const viewer = state.viewer;
    const count = state.stage === "3d" && viewer ? viewer.frames.length : imagesFor(state.currentProduct || {}).length;
    const index = state.stage === "3d" && viewer ? viewer.frameIndex() : state.galleryIndex;
    $("#mediaPosition").textContent = `${String(index + 1).padStart(2, "0")} / ${String(count).padStart(2, "0")}`;
    $("#mediaPrev").disabled = count < 2;
    $("#mediaNext").disabled = count < 2;
    $("#spinToggle").disabled = count < 2;
    const rotating = Boolean(viewer && viewer.rotating);
    $("#spinToggle").textContent = rotating ? "Остановить" : "Вращать";
    $("#spinToggle").setAttribute("aria-pressed", String(rotating));
  }

  function renderGallery(product) {
    const images = imagesFor(product);
    state.galleryIndex = 0;
    $("#sheetImage").src = images[0];
    $("#sheetImage").alt = `${product.name} — ${galleryLabel(product, 0)}`;
    $("#galleryDots").innerHTML = images.map((src, index) => `<button type="button" data-gallery="${index}" class="${index === 0 ? "active" : ""}" aria-pressed="${index === 0}" aria-label="${escapeHTML(galleryLabel(product, index))}"><img src="${escapeHTML(src)}" alt="" loading="lazy" decoding="async"></button>`).join("");
  }

  function setGallery(index) {
    const product = state.currentProduct;
    if (!product) return;
    const images = imagesFor(product);
    state.galleryIndex = (index + images.length) % images.length;
    $("#sheetImage").src = images[state.galleryIndex];
    $("#sheetImage").alt = `${product.name} — ${galleryLabel(product, state.galleryIndex)}`;
    $$("#galleryDots button").forEach((node, i) => {
      node.classList.toggle("active", i === state.galleryIndex);
      node.setAttribute("aria-pressed", String(i === state.galleryIndex));
    });
    updateMediaPosition();
  }

  function stepMedia(delta) {
    if (state.stage === "3d" && state.viewer) state.viewer.step(delta);
    else setGallery(state.galleryIndex + delta);
    updateMediaPosition();
  }

  function zoomProduct() {
    const product = state.currentProduct;
    if (!product) return;
    const src = state.stage === "3d" && state.viewer ? state.viewer.currentSource() : imagesFor(product)[state.galleryIndex];
    openLightbox(src, `${product.name} · ${state.stage === "photo" ? galleryLabel(product, state.galleryIndex) : "Обзор 360°"}`);
  }

  function updatePurchaseSummary() {
    const product = state.currentProduct;
    $("#purchaseSummary").textContent = product ? `${product.price} · ${state.selectedSize ? `размер ${state.selectedSize}` : "Выбери размер"}` : "Выбери размер";
    const blocked = !product || !Core.available(product);
    $("#addToCartButton").disabled = blocked;
    $("#addToCartButton").innerHTML = blocked ? "ЖДЁМ ПОПОЛНЕНИЕ" : state.selectedSize ? 'ДОБАВИТЬ В ЗАЯВКУ <span>+</span>' : 'ВЫБРАТЬ РАЗМЕР <span>↑</span>';
  }

  function renderSheet(product) {
    state.currentProduct = product;
    const preferred = (state.profile.size || "").trim();
    state.selectedSize = preferred && (product.sizes || []).includes(preferred)
      ? preferred
      : ((product.sizes || []).length === 1 ? product.sizes[0] : null);
    state.qty = 1;
    $("#qtyValue").textContent = "1";
    renderGallery(product);
    $("#sheetBadge").textContent = product.badge || "БАЗА";
    $("#sheetKicker").textContent = `${categoryName(product.category).toUpperCase()} / ${product.fit || "БАЗА"}`;
    $("#sheetTitle").textContent = product.name;
    const sign = $("#sheetSign");
    if (sign) {
      sign.textContent = product.signature || "Сила и честь. Бери размер, пока есть.";
    }
    $("#sheetPrice").textContent = product.price;
    $("#sheetDescription").textContent = product.description;
    $("#sheetFacts").innerHTML = `<div class="fact-row"><span>Материал</span><span>${escapeHTML(product.material || "Плотный хлопок")}</span></div><div class="fact-row"><span>Посадка</span><span>${escapeHTML(product.fit || "Свободная")}</span></div><div class="fact-row"><span>Статус</span><span>${escapeHTML(product.stock_label || "В наличии")}</span></div>`;
    $("#sizeList").innerHTML = (product.sizes || []).map(size => `<button class="size-button ${state.selectedSize === size ? "selected" : ""}" data-size="${escapeHTML(size)}" aria-pressed="${state.selectedSize === size}" type="button">${escapeHTML(size)}</button>`).join("");
    $("#sizeHint").textContent = state.selectedSize ? `Размер ${state.selectedSize} выбран.` : "Выбери размер.";
    $("#sizeHint").classList.remove("error");
    $("#sheetDetails").innerHTML = `<strong>ДЕТАЛИ</strong><br>${(product.details || []).map(escapeHTML).join(" · ")}`;
    renderPersonalization(product);
    sheetProduct = product;
    drawPersonPreview();
    const singleSize = (product.sizes || []).length === 1 ? product.sizes[0] : "";
    const letterSizes = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL"];
    $("#sizeGuideButton").classList.toggle("hidden", singleSize !== "" && !letterSizes.includes(singleSize));
    const tryon = $("#tryonButton");
    if (tryon) tryon.classList.toggle("hidden", !(product.sizes || []).some(size => letterSizes.includes(size)));
    const saved = state.saved.includes(product.id);
    $("#sheetSave").classList.toggle("saved", saved);
    $("#sheetSave").setAttribute("aria-label", saved ? "Удалить из сохранённых" : "Сохранить");
    const others = state.data.products.filter(item => item.id !== product.id && item.active !== false);
    const related = others.filter(item => item.category === product.category).slice(0, 2);
    if (!related.length) related.push(...others.slice(0, 2));
    $("#related").innerHTML = related.length
      ? `<h4>С ЭТИМ БЕРУТ</h4>${related.map(item => `<button type="button" class="related-card" data-related="${escapeHTML(item.id)}"><img src="${escapeHTML(imageFor(item))}" alt="${escapeHTML(item.name)}"><span>${escapeHTML(item.name)}</span></button>`).join("")}`
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
    $(".product-sheet").scrollTop = 0;
    if (Number.isInteger(photoIndex)) {
      setStage("photo");
      setGallery(photoIndex);
    } else setStage("3d");
    haptic("light");
  }

  function toggleSaved(id) {
    if (state.saved.includes(id)) {
      state.saved = state.saved.filter(item => item !== id);
      showToast("Убрано из сохранённых.");
    } else {
      state.saved.push(id);
      showToast("Сохранено. Вернёшься — вещь будет ждать.");
      haptic("medium");
    }
    saveJSON("vorozhbitov_saved", state.saved);
    renderProducts();
    if (state.currentProduct && state.currentProduct.id === id) {
      $("#sheetSave").classList.toggle("saved", state.saved.includes(id));
      $("#sheetSave").setAttribute("aria-label", state.saved.includes(id) ? "Удалить из сохранённых" : "Сохранить");
    }
  }

  function showSaved() {
    if (!state.saved.length) return showToast("Пока ничего не сохранено.");
    state.view = "saved";
    state.category = "all";
    state.search = "";
    $("#searchInput").value = "";
    $("#clearSearch").classList.add("hidden");
    renderCategoryChips();
    renderProducts();
    scrollToId("catalog");
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
    if (state.checkoutPending) return showToast("Подожди подтверждения заявки. Корзина сохранена.");
    const product = state.currentProduct;
    if (!product) return;
    if (!state.selectedSize) {
      $("#sizeHint").textContent = "Сначала выбери размер.";
      $(".size-block").scrollIntoView({block:"center", behavior:reducedMotion() ? "instant" : "smooth"});
      $("#sizeList button")?.focus({preventScroll:true});
      $("#sizeHint").classList.add("error");
      haptic("error");
      return;
    }
    const person = personValue(product);
    if (person === false) return;
    // Разные номера жетона — разные строки заявки, поэтому номер входит в ключ.
    const key = `${product.id}::${state.selectedSize}${person ? `::${person}` : ""}`;
    const existing = state.cart.find(item => item.key === key);
    if (existing && existing.qty + state.qty > Core.MAX_QUANTITY) return showToast("В одной позиции — не больше 20 штук.");
    if (!existing && state.cart.length >= 20) return showToast("В одной заявке — не больше 20 позиций.");
    if (existing) existing.qty += state.qty;
    else state.cart.push({ key, id: product.id, size: state.selectedSize, qty: state.qty, person: person || "" });
    saveJSON("vorozhbitov_cart", state.cart);
    updateCounters();
    closeModal("productModal");
    haptic("success");
    showToast("Добавлено в заявку.");
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
    return cartItems().reduce((sum, pair) => sum + priceNumber(pair.product.price) * pair.item.qty, 0);
  }

  function renderCart() {
    const content = $("#cartContent");
    if (!state.catalogReady) {
      content.innerHTML = `<div class="cart-empty"><h3>Сверяем корзину с каталогом.</h3><p>Сохранённые вещи на месте. Загрузим цены и размеры перед оформлением.</p><button class="button button-outline" data-cart-retry type="button">ПОВТОРИТЬ ЗАГРУЗКУ</button></div>`;
      $("#checkoutForm").classList.add("hidden");
      return;
    }
    if (!state.cart.length) {
      content.innerHTML = `<div class="cart-empty"><span class="empty-mark">∅</span><h3>Заявка пока пустая.</h3><p>Тихо. Даже ткань не шуршит. Загляни в витрину — выпуск короткий.</p><button class="button button-outline" data-cart-shop type="button">К ВИТРИНЕ</button></div>`;
      $("#checkoutForm").classList.add("hidden");
      return;
    }
    let unavailable = false;
    content.innerHTML = state.cart.map(item => {
      const product = productById(item.id);
      const ready = product && Core.available(product) && product.sizes.includes(item.size);
      if (!ready) unavailable = true;
      const image = product ? `<img class="cart-line-image" src="${escapeHTML(imageFor(product))}" alt="">` : '<span class="cart-line-image unavailable-mark" aria-hidden="true">—</span>';
      return `<div class="cart-line ${ready ? "" : "is-unavailable"}" data-cart-key="${escapeHTML(item.key)}">${image}<div class="cart-line-name"><strong>${escapeHTML(product ? product.name : "Вещь недоступна")}</strong><small>Размер: ${escapeHTML(item.size)}${item.person ? ` · Номер: ${escapeHTML(item.person)}` : ""}</small>${ready ? `<div class="qty-control"><button data-qty="minus" type="button" aria-label="Уменьшить">−</button><span>${item.qty}</span><button data-qty="plus" type="button" aria-label="Увеличить" ${item.qty >= Core.MAX_QUANTITY ? "disabled" : ""}>+</button></div>` : '<small class="unavailable-note">Удали эту позицию или выбери доступный размер.</small>'}</div><div class="cart-line-end"><strong>${ready ? rubles(priceNumber(product.price) * item.qty) : "Недоступно"}</strong><button class="remove-line" data-remove-key="${escapeHTML(item.key)}" type="button">УДАЛИТЬ</button></div></div>`;
    }).join("");
    if (unavailable) content.insertAdjacentHTML("beforeend", '<p class="cart-warning" role="status">Состав изменился. Удали недоступные позиции, чтобы продолжить.</p>');
    $("#cartTotal").textContent = rubles(cartTotal());
    $("#submitOrder").disabled = unavailable || state.checkoutPending;
    $$("#cartContent button, #checkoutForm input, #checkoutForm .filter-chip").forEach(node => { node.disabled = state.checkoutPending; });
    $$("#cartContent [data-qty=plus]").forEach(node => { const item = state.cart.find(row => row.key === node.closest("[data-cart-key]").dataset.cartKey); node.disabled = state.checkoutPending || item.qty >= Core.MAX_QUANTITY; });
    $("#checkoutForm").classList.remove("hidden");
  }

  function openCart() {
    applyProfileToCheckout();
    renderCart();
    openModal("cartModal");
  }

  function updateCartItem(key, delta) {
    if (state.checkoutPending) return showToast("Подожди подтверждения заявки. Корзина сохранена.");
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
    if (state.checkoutPending) return showToast("Подожди подтверждения заявки. Корзина сохранена.");
    state.cart = state.cart.filter(item => item.key !== key);
    saveJSON("vorozhbitov_cart", state.cart);
    renderCart();
    updateCounters();
    showToast("Вещь убрана из заявки.");
  }

  let mainButtonBound = false;

  function syncMainButton() {
    if (!tg || !tg.MainButton) return;
    const cartCount = state.cart.reduce((sum, item) => sum + Number(item.qty || 0), 0);
    const cartOpen = state.modalStack.length > 0 || document.body.classList.contains("welcoming") || document.body.classList.contains("booting");
    try {
      if (cartCount > 0 && !cartOpen) {
        tg.MainButton.setText(cartCount === 1 ? "ЗАЯВКА" : `ЗАЯВКА · ${cartCount}`);
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
    const savedCount = state.saved.length;
    [$("#cartCount"), $("#bottomCartCount")].forEach(node => {
      if (node) { node.textContent = cartCount; node.classList.toggle("hidden", cartCount === 0); }
    });
    const saved = $("#savedCount");
    if (saved) { saved.textContent = savedCount; saved.classList.toggle("hidden", savedCount === 0); }
    if ($("#profileSavedN")) $("#profileSavedN").textContent = String(savedCount);
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

  function stopPayPoll() {
    if (payPollTimer) {
      clearInterval(payPollTimer);
      payPollTimer = null;
    }
  }

  function markPaidUi() {
    stopPayPoll();
    haptic("success");
    closeModal("payModal");
    const toast = $("#successToast");
    if (toast) toast.classList.remove("hidden");
    showToast("Оплата прошла.");
    loadProfileOrders();
  }

  function startPayPoll(paymentId) {
    stopPayPoll();
    payWatchId = paymentId || "";
    const initData = tg && tg.initData;
    if (!payWatchId || !initData) return;
    payPollTimer = setInterval(async () => {
      try {
        const response = await fetch("/api/my-orders", {
          headers: { Accept: "application/json", "X-Telegram-Init-Data": initData }
        });
        if (!response.ok) return;
        const payload = await response.json();
        const paid = (payload.orders || []).some(row => row.payment_id === payWatchId && row.status === "paid");
        if (paid) markPaidUi();
      } catch (_) { /* keep waiting */ }
    }, 4000);
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
        tg.openInvoice(url, status => {
          if (status === "paid") markPaidUi();
          else if (status === "cancelled") showToast("Оплата отменена.");
          else showToast("Не получилось оплатить. Попробуй другой способ.");
        });
        return;
      } catch (_) { /* fall through to link */ }
    }
    if (url && openPayLink(url)) {
      showToast("Окно оплаты открыто. Статус обновится сам.");
      return;
    }
    showToast("Этот способ сейчас недоступен. Кнопки также в чате с ботом.");
  }

  function showPaySheet(data) {
    const amount = $("#payAmount");
    if (amount) amount.textContent = data.amount_label || "—";
    const linesBox = $("#payLines");
    if (linesBox) {
      const lines = Array.isArray(data.lines) ? data.lines : [];
      linesBox.innerHTML = lines.map(line => {
        const qty = Number(line.quantity || 1);
        const extra = qty > 1 ? ` · ${qty} шт.` : "";
        return `<li><span>${escapeHTML(line.name || "Вещь")}${line.size ? ` · ${escapeHTML(line.size)}` : ""}</span><span>${escapeHTML(extra)}</span></li>`;
      }).join("");
      linesBox.classList.toggle("hidden", !lines.length);
    }
    const box = $("#payMethods");
    if (box) {
      const methods = Array.isArray(data.methods) ? data.methods : [];
      if (!methods.length) {
        box.innerHTML = `<p class="pay-lead">Способы оплаты придут сообщением в чат с ботом.</p>`;
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
    closeModal("cartModal");
    closeModal("profileModal");
    openModal("payModal");
    startPayPoll(data.payment_id || "");
  }

  async function resumePayment(paymentId) {
    const initData = tg && tg.initData;
    if (!initData) {
      showToast("Оплата — в чате с ботом, «Мои заявки».");
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
    } catch (_) {
      showToast("Сеть не ответила. Попробуй ещё раз.");
    }
  }

  async function submitOrder() {
    if (state.checkoutPending || ($("#submitOrder") && $("#submitOrder").disabled)) return;
    const pairs = cartItems();
    if (!state.catalogReady) return showToast("Подожди загрузку каталога.");
    if (pairs.length !== state.cart.length || pairs.some(({item, product}) => !Core.available(product) || !product.sizes.includes(item.size))) { renderCart(); return showToast("Проверь недоступные позиции в корзине."); }
    const name = $("#checkoutName").value.trim();
    const phone = $("#checkoutPhone").value.trim();
    const city = $("#checkoutCity").value.trim();
    const address = ($("#checkoutAddress") && $("#checkoutAddress").value.trim()) || "";
    const note = $("#checkoutNote").value.trim();
    const consent = $("#checkoutConsent").checked;
    if (!pairs.length) return showToast("Добавь хотя бы одну вещь.");
    if (name.length < 2) return showToast("Напиши имя — менеджеру нужно знать, как обратиться.");
    if (phone.replace(/\D/g, "").length < 10) return showToast("Проверь номер телефона.");
    if (city.length < 2) return showToast("Укажи город.");
    if (!consent) return showToast("Подтверди согласие на обработку данных.");

    persistProfile({
      ...state.profile,
      name, phone, city,
      address: address || state.profile.address,
      note: note || state.profile.note,
      deliver: state.deliver
    }, false);
    const payload = {
      type: "order",
      customer: {
        name, phone, city,
        address: address || state.profile.address || "",
        entrance: state.profile.entrance || "",
        deliver: state.deliver,
        note
      },
      consent: true,
      items: pairs.map(({ item, product }) => {
        const line = { product_id: product.id, size: item.size, quantity: item.qty };
        if (item.person) line.person = item.person;
        return line;
      })
    };
    const button = $("#submitOrder");
    const previous = button ? button.innerHTML : "";
    state.checkoutPending = true;
    renderCart();
    if (button) {
      button.disabled = true;
      button.textContent = "СОБИРАЮ СЧЁТ…";
    }
    try {
      const user = telegramUser();
      const data = await checkoutClient.submit(payload, tg && tg.initData, user ? String(user.id) : "");
      const storedHistory = loadJSON("vorozhbitov_orders", []);
      const history = Array.isArray(storedHistory) ? storedHistory : [];
      history.unshift({ at: Date.now(), total: data.amount_rub, items: payload.items });
      saveJSON("vorozhbitov_orders", history.slice(0, 12));
      state.cart = [];
      saveJSON("vorozhbitov_cart", state.cart);
      updateCounters();
      if (data.status === "awaiting_payment") {
        haptic("success");
        showPaySheet(data);
      } else {
        closeModal("cartModal");
        openProfile();
        showToast(data.status === "paid" ? "Эта заявка уже оплачена." : "Заявка уже обработана. Проверь её статус.");
      }
    } catch (error) {
      showToast(error.message || "Нет подтверждения от сервера. Корзина сохранена.");
      haptic("error");
    } finally {
      state.checkoutPending = false;
      if (button) button.innerHTML = previous;
      renderCart();
    }
  }

  function openChannel() {
    const url = state.data.channel_url || FALLBACK_CATALOG.channel_url;
    if (tg && typeof tg.openTelegramLink === "function" && /^https:\/\/t\.me\//.test(url)) tg.openTelegramLink(url);
    else window.open(url, "_blank", "noopener,noreferrer");
  }

  async function shareSignal() {
    const text = "Я в закрытом выпуске ВОРОЖБИТОВ. Сила и честь:";
    const url = state.data.channel_url || FALLBACK_CATALOG.channel_url;
    const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`;
    if (tg && typeof tg.openTelegramLink === "function") return tg.openTelegramLink(shareUrl);
    try {
      await navigator.clipboard.writeText(`${text} ${url}`);
      showToast("Ссылка скопирована. Отправь своим.");
    } catch (_) {
      window.open(shareUrl, "_blank", "noopener,noreferrer");
    }
  }

  function openLightbox(src, caption) {
    $("#lightboxViewport").classList.remove("zoomed");
    $("#lightboxViewport").scrollTo(0, 0);
    $("#lightboxZoom").textContent = "Увеличить +";
    $("#lightboxZoom").setAttribute("aria-pressed", "false");
    $("#lightboxImage").src = src;
    $("#lightboxImage").alt = caption || "";
    $("#lightboxCaption").textContent = caption || "";
    openModal("lightbox");
  }

  function adviseSize() {
    const height = Number($("#heightInput").value);
    const result = $("#adviseResult");
    if (!height || height < 140 || height > 220) {
      result.textContent = "Напиши рост, например 178.";
      result.classList.add("error");
      return;
    }
    let size = "XXL";
    if (height < 172) size = "S";
    else if (height < 178) size = "M";
    else if (height < 186) size = "L";
    else if (height < 194) size = "XL";
    result.classList.remove("error");
    result.textContent = `При росте ${height} см ориентир — ${size}. Посадка зависит от обхвата груди и модели: сравни замеры со своей футболкой.`;
  }

  function configureTelegram() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      if (typeof tg.setHeaderColor === "function") tg.setHeaderColor("#070708");
      if (typeof tg.setBackgroundColor === "function") tg.setBackgroundColor("#070708");
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
    if (welcome.classList.contains("is-leaving")) return;
    // Заставка уходит вверх, а не пропадает рывком.
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
    }, reducedMotion() ? 0 : 520);

  }

  function startExperience() {
    const boot = $("#boot");
    const welcome = $("#welcome");
    syncModalLayers();
    window.setTimeout(() => {
      document.body.classList.remove("booting");
      boot.classList.add("hidden");
      welcome.classList.remove("hidden");
      document.body.classList.add("welcoming");
      syncModalLayers();
      $("#enterShop").focus({ preventScroll: true });
      if (state.catalogReady) setupWelcomeVideo(mediaConfig());
    }, reducedMotion() ? 0 : 600);
  }

  async function loadCatalog() {
    if (state.catalogLoading) return;
    state.catalogLoading = true;
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
        lookbook: Array.isArray(remote.lookbook) ? remote.lookbook : [],
        privacy_url: remote.privacy_url || ""
      };
      state.catalogReady = true;
      const privacy = $("#privacyLink");
      if (privacy && state.data.privacy_url) {
        privacy.href = state.data.privacy_url;
        privacy.classList.remove("hidden");
      }
      $("#sizeFilters").innerHTML = ["all", ...Core.sizesFor(state.data.products)].map(size => `<button class="filter-chip ${size === state.sizeFilter ? "active" : ""}" data-size-filter="${escapeHTML(size)}" type="button" aria-pressed="${size === state.sizeFilter}">${size === "all" ? "Любой" : escapeHTML(size)}</button>`).join("");
      $("#profileSizeRow").innerHTML = Core.sizesFor(state.data.products).filter(size => size !== "ОДИН" && size !== "ONE SIZE").map(size => `<button class="filter-chip" data-profile-size="${escapeHTML(size)}" type="button">${escapeHTML(size)}</button>`).join("");
      applyMedia();
      renderCategoryChips();
      renderProducts();
      renderLookbook();
      if (state.modalStack.includes("cartModal")) renderCart();
    } catch (_) {
      $("#catalogError").classList.remove("hidden");
      renderProducts();
    } finally {
      clearTimeout(timeout);
      state.catalogLoading = false;
      $("#catalogStatus").classList.add("hidden");
      $("#productGrid").setAttribute("aria-busy", "false");
    }
  }

  /* --- Видео бренда: hero-петля и тизер --- */

  function mediaConfig() {
    const media = (state.data && state.data.media) || {};
    return {
      welcomeLoop: media.welcome_loop || "assets/video/welcome-final-30s.mp4",
      welcomePoster: media.welcome_poster || "assets/drop/hero-sila-chest-v1.jpg",
      teaser: media.teaser || "assets/video/campaign-v6.mp4",
      title: media.teaser_title || "СИЛА И ЧЕСТЬ",
      caption: media.teaser_caption || "Выпуск 001 · Никита Ворожбитов",
      storyUrl: media.teaser_story_url || ""
    };
  }

  function saverMode() {
    // Экономия трафика или медленная сеть — оставляем постер, видео не грузим.
    const conn = navigator.connection || {};
    if (conn.saveData) return true;
    return /(^|-)2g$/.test(String(conn.effectiveType || ""));
  }

  function reducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function applyMedia() {
    const media = mediaConfig();
    if (state.data.media && state.data.media.hero_image) $("#heroFallback").src = state.data.media.hero_image;
    if ($("#teaserTitle")) $("#teaserTitle").textContent = media.title;
    if ($("#teaserCaption")) $("#teaserCaption").textContent = media.caption;
    setupWelcomeVideo(media);
    // Тираж на заставке берём из каталога, а не пишем руками.
    const stock = $("#welcomeStock");
    const lead = (state.data.products || []).find(p => (p.featured || p.real_photos) && p.stock_label);
    if (stock && lead) stock.textContent = String(lead.stock_label).toUpperCase();
  }

  const welcomePlayback = { source: "", ready: false, paused: false, failed: false, bound: false, pending: false };

  function welcomeCanPlay() {
    const welcome = $("#welcome");
    return welcomePlayback.ready && !welcomePlayback.paused && !welcomePlayback.failed
      && !saverMode() && !reducedMotion() && !document.hidden && !state.modalStack.length
      && !welcome.classList.contains("hidden") && !welcome.classList.contains("is-leaving");
  }

  function syncWelcomeVideo() {
    const video = $("#welcomeVideo");
    const control = $("#welcomeMotion");
    control.classList.toggle("hidden", !welcomePlayback.ready || welcomePlayback.failed || saverMode() || reducedMotion());
    control.setAttribute("aria-label", welcomePlayback.paused ? "Продолжить видео" : "Остановить видео");
    control.setAttribute("aria-pressed", String(welcomePlayback.paused));
    $("#welcomeMotionIcon").textContent = welcomePlayback.paused ? "▷" : "Ⅱ";
    const canPlay = welcomeCanPlay();
    const fallback = $("#welcomeFallback");
    if (fallback) fallback.classList.toggle("hidden", canPlay);
    if (!canPlay) {
      video.pause();
      if (saverMode() || reducedMotion()) video.classList.remove("is-playing");
      return;
    }
    if (video.getAttribute("src") !== welcomePlayback.source) video.src = welcomePlayback.source;
    if (!video.paused || welcomePlayback.pending) return;
    welcomePlayback.pending = true;
    const attempt = video.play();
    Promise.resolve(attempt).catch(error => {
      if (error.name !== "AbortError" && welcomeCanPlay()) {
        welcomePlayback.paused = true;
        syncWelcomeVideo();
      }
    }).finally(() => {
      welcomePlayback.pending = false;
      if (!welcomeCanPlay()) video.pause();
      else if (video.paused) syncWelcomeVideo();
    });
  }

  function setupWelcomeVideo(media) {
    const video = $("#welcomeVideo");
    if (welcomePlayback.source !== media.welcomeLoop) {
      video.pause();
      video.classList.remove("is-playing");
      welcomePlayback.source = media.welcomeLoop;
      welcomePlayback.failed = false;
    }
    $("#welcomeFallback").src = media.welcomePoster;
    welcomePlayback.ready = Boolean(media.welcomeLoop);
    if (!welcomePlayback.bound) {
      welcomePlayback.bound = true;
      $("#welcomeMotion").addEventListener("click", () => {
        welcomePlayback.paused = !welcomePlayback.paused;
        syncWelcomeVideo();
      });
      video.addEventListener("playing", () => {
        if (welcomeCanPlay()) video.classList.add("is-playing");
        else video.pause();
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
    }
    syncWelcomeVideo();
  }

  function stopWelcomeVideo() {
    const video = $("#welcomeVideo");
    video.pause();
    video.classList.remove("is-playing");
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
    state.view = "all";
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

  function openTeaser() {
    const media = mediaConfig();
    const video = $("#teaserVideo");
    const welcome = $("#welcomeVideo");
    if (welcome) welcome.pause();
    $("#teaserReplay").classList.add("hidden");
    if (video) {
      if (video.getAttribute("src") !== media.teaser) video.src = media.teaser;
      video.currentTime = 0;
    }
    const story = $("#teaserToStory");
    // Кнопка сторис есть только на клиентах Bot API 7.8+ и с публичным URL.
    if (story) story.classList.toggle("hidden", !(tg && typeof tg.shareToStory === "function" && media.storyUrl));
    openModal("teaserModal");
    if (video) {
      const attempt = video.play();
      if (attempt && typeof attempt.catch === "function") attempt.catch(() => {});
    }
    haptic("light");
  }

  function closeTeaser() {
    const video = $("#teaserVideo");
    if (video) {
      video.pause();
      video.currentTime = 0;
    }
    $("#teaserReplay").classList.add("hidden");
    syncWelcomeVideo();
  }

  function shareTeaserToStory() {
    const media = mediaConfig();
    if (!media.storyUrl || !tg || typeof tg.shareToStory !== "function") {
      showToast("Сторис доступны в свежем Telegram.");
      return;
    }
    try {
      tg.shareToStory(media.storyUrl, { text: `${media.title} — выпуск 001` });
      haptic("success");
    } catch (_) {
      showToast("Не получилось открыть сторис.");
    }
  }


  /* --- Скелетоны каталога --- */

  function renderSkeletons() {
    const root = $("#productGrid");
    if (!root || root.children.length) return;
    const card = '<article class="product-card skeleton-card" aria-hidden="true"><div class="sk-media"></div><div class="sk-line"></div><div class="sk-line short"></div></article>';
    root.innerHTML = card + card;
  }

  /* --- Reveal-анимации секций и hero-tilt --- */

  function initMotion() {
    ["catalog", "store", "lookbook"].forEach(id => {
      const section = document.getElementById(id);
      if (section) section.classList.add("reveal");
    });
    if ("IntersectionObserver" in window && !reducedMotion()) {
      const observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (entry.isIntersecting) { entry.target.classList.add("in"); observer.unobserve(entry.target); }
        });
      }, { threshold: 0.12 });
      $$(".reveal").forEach(node => observer.observe(node));
    } else {
      $$(".reveal").forEach(node => node.classList.add("in"));
    }
    const heroMedia = $(".hero-media");
    const fine = window.matchMedia && matchMedia("(hover: hover) and (pointer: fine)").matches;
    if (!heroMedia || !fine || reducedMotion()) return;
    const img = heroMedia.querySelector("img");
    if (!img) return;
    heroMedia.addEventListener("mousemove", event => {
      const rect = heroMedia.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width - 0.5;
      const y = (event.clientY - rect.top) / rect.height - 0.5;
      img.style.setProperty("--tilt-y", (-x * 6).toFixed(2) + "deg");
      img.style.setProperty("--tilt-x", (y * 6).toFixed(2) + "deg");
      img.style.setProperty("--tilt-s", "1.04");
    });
    heroMedia.addEventListener("mouseleave", () => {
      img.style.setProperty("--tilt-x", "0deg");
      img.style.setProperty("--tilt-y", "0deg");
      img.style.setProperty("--tilt-s", "1");
    });
  }

  /* --- Lookbook-истории --- */

  const stories = { index: 0, timer: 0, list: [] };

  function storyTarget(item) {
    if (/ЗНАК|ЖЕТ/i.test(String(item.caption || ""))) return { id: "tag-sila-i-chest", label: "СМОТРЕТЬ ЖЕТОН" };
    return { id: "tee-sila-i-chest", label: "СМОТРЕТЬ ФУТБОЛКУ" };
  }

  function openStories(index) {
    stories.list = lookbookList();
    if (!stories.list.length) return;
    stories.index = Math.min(Math.max(index || 0, 0), stories.list.length - 1);
    openModal("storiesModal");
    showStory(stories.index);
    haptic("light");
  }

  function showStory(index) {
    const list = stories.list.length ? stories.list : lookbookList();
    stories.list = list;
    if (!list.length) return;
    stories.index = (index + list.length) % list.length;
    const item = list[stories.index];
    const caption = item.caption || "ЗАМЕТКА";
    const img = $("#storiesImage");
    img.alt = caption;
    img.src = item.image || item.photo_url || "";
    $("#storiesCaption").textContent = caption;
    const target = storyTarget(item);
    const cta = $("#storiesCta");
    if (productById(target.id)) {
      cta.textContent = target.label;
      cta.dataset.productId = target.id;
      cta.classList.remove("hidden");
    } else {
      cta.classList.add("hidden");
    }
    $("#storiesProgress").innerHTML = list.map((_, i) => `<i class="${i < stories.index ? "done" : i === stories.index ? "active" : ""}"><b></b></i>`).join("");
    const next = list[(stories.index + 1) % list.length];
    if (next && (next.image || next.photo_url)) { const pre = new Image(); pre.src = next.image || next.photo_url; }
    clearTimeout(stories.timer);
    stories.timer = 0;
    if (reducedMotion()) return;
    const fill = $("#storiesProgress i.active b");
    if (fill) {
      fill.style.transition = "none";
      fill.style.width = "0";
      requestAnimationFrame(() => requestAnimationFrame(() => {
        fill.style.transition = "width 5s linear";
        fill.style.width = "100%";
      }));
    }
    stories.timer = setTimeout(() => {
      if (stories.index >= list.length - 1) closeModal("storiesModal");
      else showStory(stories.index + 1);
    }, 5000);
  }

  function stopStories() {
    clearTimeout(stories.timer);
    stories.timer = 0;
  }

  /* --- Квиз «Найди посадку» --- */

  const QUIZ_SIZES = ["S", "M", "L", "XL", "XXL"];
  const QUIZ_HEIGHTS = [
    { label: "До 172", size: "S" },
    { label: "173–178", size: "M" },
    { label: "179–186", size: "L" },
    { label: "187–194", size: "XL" },
    { label: "Выше 194", size: "XXL" }
  ];
  const quiz = { step: 0, height: "", build: "", fit: "" };

  function openQuiz() {
    quiz.step = 0;
    quiz.height = "";
    quiz.build = "";
    quiz.fit = "";
    openModal("quizModal");
    renderQuiz();
  }

  function renderQuiz() {
    const steps = [
      { key: "height", q: "Твой рост?", options: QUIZ_HEIGHTS.map(entry => entry.label) },
      { key: "build", q: "Телосложение?", options: ["Худощавое", "Среднее", "Крепкое"] },
      { key: "fit", q: "Как должно сидеть?", options: ["Впритык", "Как обычно", "Свободно / оверсайз"] }
    ];
    const body = $("#quizBody");
    if (quiz.step < steps.length) {
      const current = steps[quiz.step];
      $("#quizStep").textContent = `Вопрос ${quiz.step + 1} из 3`;
      body.innerHTML = `<p class="quiz-q">${escapeHTML(current.q)}</p><div class="quiz-options">` +
        current.options.map(option => `<button class="button button-outline" data-quiz="${escapeHTML(option)}" type="button">${escapeHTML(option)}</button>`).join("") +
        "</div>" + (quiz.step ? '<button class="quiz-back" data-quiz-back type="button">← НАЗАД</button>' : "");
      return;
    }
    const base = QUIZ_HEIGHTS.find(entry => entry.label === quiz.height) || QUIZ_HEIGHTS[1];
    let at = QUIZ_SIZES.indexOf(base.size);
    if (quiz.build === "Худощавое") at -= 1;
    if (quiz.build === "Крепкое") at += 1;
    if (quiz.fit === "Впритык") at -= 1;
    if (quiz.fit === "Свободно / оверсайз") at += 1;
    const size = QUIZ_SIZES[Math.min(Math.max(at, 0), QUIZ_SIZES.length - 1)];
    $("#quizStep").textContent = "Готово";
    const picks = (state.data.products || []).filter(item => item.active !== false && (item.sizes || []).includes(size)).slice(0, 3);
    body.innerHTML = `<p class="quiz-q">Твоя посадка —</p><div class="quiz-result-size">${escapeHTML(size)}</div>` +
      `<p class="quiz-result-note">Рост ${escapeHTML(quiz.height.toLowerCase())} · ${escapeHTML(quiz.build.toLowerCase())} · ${escapeHTML(quiz.fit.toLowerCase())}.</p>` +
      `<div class="quiz-options"><button class="button" data-quiz-apply="${escapeHTML(size)}" type="button">ВЗЯТЬ ${escapeHTML(size)}</button></div>` +
      (picks.length ? `<p class="quiz-q quiz-sub">В этом размере:</p>` + picks.map(item =>
        `<button class="quiz-pick" data-quiz-open="${escapeHTML(item.id)}" type="button"><img src="${escapeHTML(imageFor(item))}" alt="" loading="lazy"><span><b>${escapeHTML(item.name)}</b><span>${escapeHTML(item.price || "")}</span></span></button>`).join("") : "");
  }

  function applyQuizSize(size) {
    state.profile.size = size;
    saveJSON("vorozhbitov_profile", state.profile);
    const button = document.querySelector(`#sizeList [data-size="${size}"]`);
    if (button) {
      state.selectedSize = size;
      $$(".size-button", $("#sizeList")).forEach(node => { node.classList.toggle("selected", node === button); node.setAttribute("aria-pressed", String(node === button)); });
      updatePurchaseSummary();
      $("#sizeHint").textContent = `Размер ${size} выбран.`;
      $("#sizeHint").classList.remove("error");
    }
    closeModal("quizModal");
    showToast(`Размер ${size} записан.`);
    haptic("success");
  }

  /* --- Конфигуратор гравировки жетона --- */

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
      ctx.font = `700 ${Math.round(h * 0.13)}px "Arial Narrow", Arial, sans-serif`;
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
    img.src = "assets/spin/tag-sich-01.jpg";
  }

  /* --- Примерка футболки по фото --- */

  const tryon = { photo: null, tee: null, teeReady: false, scale: 0.8, dx: 0, dy: 0, drag: null };

  function openTryon() {
    openModal("tryonModal");
    if (!tryon.tee && !tryon.teeReady) {
      const img = new Image();
      img.onload = () => { tryon.tee = img; tryon.teeReady = true; drawTryon(); };
      img.src = "assets/tryon-tee.jpg";
    } else {
      drawTryon();
    }
  }

  function drawTryon() {
    const canvas = $("#tryonCanvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const w = canvas.width, h = canvas.height;
    ctx.globalCompositeOperation = "source-over";
    ctx.fillStyle = "#141416";
    ctx.fillRect(0, 0, w, h);
    if (tryon.photo) {
      const iw = tryon.photo.naturalWidth, ih = tryon.photo.naturalHeight;
      const scale = Math.max(w / iw, h / ih);
      const dw = iw * scale, dh = ih * scale;
      ctx.drawImage(tryon.photo, (w - dw) / 2, (h - dh) / 2, dw, dh);
    }
    if (tryon.photo && tryon.teeReady && tryon.tee) {
      const tw = w * tryon.scale;
      const th = tw * (tryon.tee.naturalHeight / tryon.tee.naturalWidth);
      ctx.globalCompositeOperation = "multiply";
      ctx.drawImage(tryon.tee, (w - tw) / 2 + tryon.dx, h * 0.10 + tryon.dy, tw, th);
      ctx.globalCompositeOperation = "source-over";
    }
  }

  function bindEvents() {
    $("#retryCatalog").addEventListener("click", loadCatalog);
    initMotion();
    renderSkeletons();
    $("#quizOpenButton").addEventListener("click", openQuiz);
    $("#quizBody").addEventListener("click", event => {
      const back = event.target.closest("[data-quiz-back]");
      if (back) { quiz.step = Math.max(quiz.step - 1, 0); renderQuiz(); return; }
      const option = event.target.closest("[data-quiz]");
      if (option) {
        quiz[["height", "build", "fit"][Math.min(quiz.step, 2)]] = option.dataset.quiz;
        quiz.step += 1;
        renderQuiz();
        haptic("light");
        return;
      }
      const apply = event.target.closest("[data-quiz-apply]");
      if (apply) { applyQuizSize(apply.dataset.quizApply); return; }
      const pick = event.target.closest("[data-quiz-open]");
      if (pick) { closeModal("quizModal"); openProduct(pick.dataset.quizOpen); }
    });
    $("#storiesPrev").addEventListener("click", () => { showStory(stories.index - 1); haptic("light"); });
    $("#storiesNext").addEventListener("click", () => { showStory(stories.index + 1); haptic("light"); });
    $("#storiesCta").addEventListener("click", event => {
      const id = event.currentTarget.dataset.productId;
      closeModal("storiesModal");
      if (id) openProduct(id);
    });
    $("#tryonButton").addEventListener("click", openTryon);
    $("#tryonFile").addEventListener("change", event => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      const img = new Image();
      img.onload = () => {
        tryon.photo = img;
        tryon.dx = 0;
        tryon.dy = 0;
        $("#tryonUpload").classList.add("hidden");
        $("#tryonChange").classList.remove("hidden");
        $("#tryonDownload").disabled = false;
        drawTryon();
        haptic("light");
      };
      img.src = URL.createObjectURL(file);
    });
    $("#tryonChange").addEventListener("click", () => $("#tryonFile").click());
    $("#tryonScale").addEventListener("input", event => { tryon.scale = Number(event.target.value) / 100; drawTryon(); });
    $("#tryonDownload").addEventListener("click", () => {
      const canvas = $("#tryonCanvas");
      if (!canvas || !tryon.photo) return;
      const link = document.createElement("a");
      link.download = "vorozhbitov-tryon.png";
      link.href = canvas.toDataURL("image/png");
      link.click();
      haptic("success");
    });
    const tryCanvas = $("#tryonCanvas");
    tryCanvas.addEventListener("pointerdown", event => {
      if (!tryon.photo) return;
      try { tryCanvas.setPointerCapture(event.pointerId); } catch (_) { /* noop */ }
      tryon.drag = { x: event.clientX - tryon.dx, y: event.clientY - tryon.dy };
    });
    tryCanvas.addEventListener("pointermove", event => {
      if (!tryon.drag) return;
      tryon.dx = Math.min(Math.max(event.clientX - tryon.drag.x, -180), 180);
      tryon.dy = Math.min(Math.max(event.clientY - tryon.drag.y, -240), 240);
      drawTryon();
    });
    tryCanvas.addEventListener("pointerup", () => { tryon.drag = null; });
    tryCanvas.addEventListener("pointercancel", () => { tryon.drag = null; });
    const personInput = $("#personInput");
    if (personInput) personInput.addEventListener("input", drawPersonPreview);
    $("#heroProductButton").addEventListener("click", () => {
      browseCollection();
      scrollToId("catalog");
    });
    $("#heroWatchFilm").addEventListener("click", openTeaser);
    $("#zoomProduct").addEventListener("click", zoomProduct);
    $("#mediaPrev").addEventListener("click", () => stepMedia(-1));
    $("#mediaNext").addEventListener("click", () => stepMedia(1));
    $("#spinToggle").addEventListener("click", () => {
      if (state.viewer) state.viewer.setRotating(!state.viewer.rotating);
      updateMediaPosition();
    });
    $("#view3d").addEventListener("framechange", updateMediaPosition);
    $("#lightboxZoom").addEventListener("click", () => {
      const zoomed = $("#lightboxViewport").classList.toggle("zoomed");
      $("#lightboxZoom").setAttribute("aria-pressed", String(zoomed));
      $("#lightboxZoom").textContent = zoomed ? "Весь кадр −" : "Увеличить +";
      if (!zoomed) $("#lightboxViewport").scrollTo(0, 0);
    });
    $$('[data-detail-product]').forEach(button => button.addEventListener('click', () => openProduct(button.dataset.detailProduct, Number(button.dataset.detailPhoto))));
    document.addEventListener("visibilitychange", syncModalLayers);
    $("#enterShop").addEventListener("click", enterShop);
    const watchTeaser = $("#watchTeaser");
    if (watchTeaser) {
      watchTeaser.addEventListener("click", () => {
        enterShop();
        openTeaser();
      });
    }

    if ($("#teaserCard")) $("#teaserCard").addEventListener("click", openTeaser);
    if ($("#teaserToStory")) $("#teaserToStory").addEventListener("click", shareTeaserToStory);
    $("#teaserVideo").addEventListener("ended", () => {
      $("#teaserReplay").classList.remove("hidden");
    });
    $("#teaserVideo").addEventListener("play", () => {
      if (document.activeElement === $("#teaserReplay")) {
        $("#teaserVideo").focus({ preventScroll: true });
      }
      $("#teaserReplay").classList.add("hidden");
    });
    $("#teaserReplay").addEventListener("click", () => {
      const video = $("#teaserVideo");
      video.currentTime = 0;
      video.play().catch(() => showToast("Нажми ▶ в плеере, чтобы повторить ролик."));
    });
    if ($("#teaserToShop")) $("#teaserToShop").addEventListener("click", () => {
      closeModal("teaserModal");
      browseCollection();
      scrollToId("catalog");
    });

    $$("[data-stage]").forEach(button => button.addEventListener("click", () => {
      setStage(button.dataset.stage);
      haptic("light");
    }));

    $("#categoryChips").addEventListener("click", event => {
      const button = event.target.closest("[data-category]");
      if (!button) return;
      state.category = button.dataset.category;
      state.view = "all";
      haptic("light");
      renderCategoryChips();
      renderProducts();
    });

    $("#productGrid").addEventListener("click", event => {
      const saveButton = event.target.closest("[data-save-id]");
      if (saveButton) { event.stopPropagation(); return toggleSaved(saveButton.dataset.saveId); }
      const card = event.target.closest("[data-product-id]");
      if (card) openProduct(card.dataset.productId);
    });

    $("#sizeList").addEventListener("click", event => {
      const button = event.target.closest("[data-size]");
      if (!button) return;
      state.selectedSize = button.dataset.size;
      $$(".size-button", $("#sizeList")).forEach(node => { node.classList.toggle("selected", node === button); node.setAttribute("aria-pressed", String(node === button)); });
      updatePurchaseSummary();
      $("#sizeHint").textContent = `Размер ${state.selectedSize} выбран.`;
      $("#sizeHint").classList.remove("error");
      haptic("light");
    });

    $("#galleryDots").addEventListener("click", event => {
      const button = event.target.closest("[data-gallery]");
      if (button) setGallery(Number(button.dataset.gallery));
    });
    $("#sheetImage").addEventListener("click", zoomProduct);

    $("#sheetQty").addEventListener("click", event => {
      if (event.target.closest("[data-sheet-qty='plus']")) state.qty = Math.min(20, state.qty + 1);
      if (event.target.closest("[data-sheet-qty='minus']")) state.qty = Math.max(1, state.qty - 1);
      $("#qtyValue").textContent = String(state.qty);
    });

    $("#related").addEventListener("click", event => {
      const card = event.target.closest("[data-related]");
      if (card) openProduct(card.dataset.related);
    });

    $("#sheetSave").addEventListener("click", () => { if (state.currentProduct) toggleSaved(state.currentProduct.id); });
    $("#addToCartButton").addEventListener("click", addToCart);
    $("#waitlistButton").addEventListener("click", sendWaitlist);
    $("#cartButton").addEventListener("click", openCart);
    $("#bottomCartButton").addEventListener("click", openCart);
    $("#savedButton").addEventListener("click", showSaved);
    if ($("#profileButton")) $("#profileButton").addEventListener("click", openProfile);
    if ($("#saveProfile")) $("#saveProfile").addEventListener("click", saveProfile);
    if ($("#profileOrders")) $("#profileOrders").addEventListener("click", event => {
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
    if ($("#profileSavedBtn")) $("#profileSavedBtn").addEventListener("click", () => { closeModal("profileModal"); showSaved(); });
    if ($("#profileCartBtn")) $("#profileCartBtn").addEventListener("click", () => { closeModal("profileModal"); openCart(); });
    if ($("#profileDeliver")) $("#profileDeliver").addEventListener("click", event => {
      const button = event.target.closest("[data-profile-deliver]");
      if (!button) return;
      state.profile.deliver = button.dataset.profileDeliver;
      $$("#profileDeliver .filter-chip").forEach(node => node.classList.toggle("active", node === button));
    });
    if ($("#profileSizeRow")) $("#profileSizeRow").addEventListener("click", event => {
      const button = event.target.closest("[data-profile-size]");
      if (!button) return;
      state.profile.size = state.profile.size === button.dataset.profileSize ? "" : button.dataset.profileSize;
      $$("#profileSizeRow .filter-chip").forEach(node => node.classList.toggle("active", node.dataset.profileSize === state.profile.size));
    });
    $("#clearView").addEventListener("click", () => { state.view = "all"; renderProducts(); });

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
    $("#searchInput").addEventListener("input", event => {
      state.search = event.target.value;
      state.view = "all";
      $("#clearSearch").classList.toggle("hidden", !state.search);
      renderProducts();
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
    $("#submitOrder").addEventListener("click", submitOrder);
    if ($("#payMethods")) $("#payMethods").addEventListener("click", event => {
      const button = event.target.closest("[data-pay-method]");
      if (!button) return;
      startPayMethod(button.dataset.payMethod, button.dataset.payUrl || "", button.dataset.payKind || "link");
    });
    $("#channelButton").addEventListener("click", openChannel);
    $("#shareButton").addEventListener("click", shareSignal);
    $("#sizeGuideButton").addEventListener("click", () => openModal("sizeGuideModal"));
    $("#adviseSize").addEventListener("click", adviseSize);
    $("#closeToast").addEventListener("click", () => $("#successToast").classList.add("hidden"));

    $("#deliverRow").addEventListener("click", event => {
      const button = event.target.closest("[data-deliver]");
      if (!button) return;
      state.deliver = button.dataset.deliver;
      $$("#deliverRow .filter-chip").forEach(node => node.classList.toggle("active", node === button));
    });

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

    const sections = ["home", "catalog", "store", "lookbook"];
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
  updateCounters();
  startExperience();
  loadCatalog();
  window.VorozhbitovShop = { state, openProduct, openCart };
})();
