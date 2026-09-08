(() => {
  "use strict";

  const FALLBACK_CATALOG = {
    brand: {
      name: "ВОРОЖБИТОВ",
      descriptor: "сила и честь / городская форма",
      drop: "ВЫПУСК",
      welcome_image: "assets/welcome.jpg",
      store_image: "assets/store.jpg"
    },
    channel_url: "https://t.me/+XufFz8GGR0o3Njky",
    privacy_url: "",
    categories: [
      { id: "drop", name: "Выпуск" },
      { id: "hoodie", name: "Худи" },
      { id: "tee", name: "Футболки" },
      { id: "bottom", name: "Низ" },
      { id: "access", name: "Аксессуары" }
    ],
    products: [
      { id: "honor-hoodie", category: "drop", name: "СИЛА И ЧЕСТЬ", price: "11 900 ₽", sizes: ["S", "M", "L", "XL"], description: "Главный сигнал выпуска. Тяжёлый футер, принт «Сила и честь» на груди.", signature: "Сила и честь. Это не слоган на витрине — это то, с чем выходишь из дома.", print: "СИЛА И\nЧЕСТЬ", shape: "hoodie", spin: ["assets/honor-hoodie.jpg", "assets/spin/honor-01.jpg", "assets/spin/honor-02.jpg", "assets/spin/honor-03.jpg", "assets/spin/honor-04.jpg", "assets/spin/honor-05.jpg", "assets/spin/honor-06.jpg", "assets/spin/honor-07.jpg"], image: "assets/honor-hoodie.jpg", images: ["assets/honor-hoodie.jpg", "assets/look-ring.jpg"], badge: "ВЫПУСК", material: "100% хлопок · 400 г/м²", fit: "Объёмный крой", details: ["Принт СИЛА И ЧЕСТЬ", "Красная V на кромке"], stock_label: "Осталось мало", active: true },
      { id: "year-tee-1993", category: "drop", name: "1993", price: "4 900 ₽", sizes: ["S", "M", "L", "XL"], description: "Год, с которого всё началось. Плотный свободный крой, цифра 1993 на груди.", signature: "1993. Носи как дату, не как принт.", print: "1993", shape: "tee", spin: ["assets/year-tee.jpg", "assets/spin/year-01.jpg", "assets/spin/year-02.jpg", "assets/spin/year-04.jpg"], image: "assets/year-tee.jpg", images: ["assets/year-tee.jpg"], badge: "ЛИМИТ", material: "100% хлопок · 240 г/м²", fit: "Свободный крой", details: ["Крупный 1993"], stock_label: "Последний тираж", active: true },
      { id: "drop-hoodie-001", category: "drop", name: "ХУДИ", price: "9 900 ₽", sizes: ["M", "L", "XL"], description: "Тяжёлое полотно, объёмный силуэт, двойная строчка.", signature: "Тяжёлая. Как надо. Партия маленькая — потом не будет.", print: "V", shape: "hoodie", spin: ["assets/hero-drop.jpg", "assets/spin/honor-02.jpg", "assets/spin/honor-04.jpg", "assets/spin/honor-06.jpg"], image: "assets/hero-drop.jpg", images: ["assets/hero-drop.jpg", "assets/look-ring.jpg"], badge: "ЛИМИТ", material: "100% хлопок · 400 г/м²", fit: "Объёмный крой", details: ["Футер 3-нитка"], stock_label: "Последний тираж", active: true },
      { id: "drop-tee-001", category: "drop", name: "ФУТБОЛКА", price: "4 900 ₽", sizes: ["S", "M", "L", "XL"], description: "Плотный хлопок, свободный крой, минимальный сигнал.", signature: "База. Без крика. Свой считывает.", print: "V", shape: "tee", spin: ["assets/base-tee.jpg", "assets/spin/year-02.jpg", "assets/spin/year-04.jpg"], image: "assets/base-tee.jpg", images: ["assets/base-tee.jpg"], badge: "ВЫПУСК", material: "100% хлопок · 240 г/м²", fit: "Свободный крой", details: ["Плотный хлопок"], stock_label: "Осталось мало", active: true },
      { id: "field-tee-001", category: "tee", name: "ОДИН В ПОЛЕ", price: "4 400 ₽", sizes: ["S", "M", "L", "XL"], description: "Один в поле. Даже если все разошлись.", signature: "Один в поле. Даже если все разошлись.", print: "ОДИН\nВ ПОЛЕ", shape: "tee", spin: ["assets/field-tee.jpg", "assets/spin/year-02.jpg", "assets/spin/year-04.jpg"], image: "assets/field-tee.jpg", images: ["assets/field-tee.jpg"], badge: "СИГНАЛ", material: "100% хлопок · 240 г/м²", fit: "Свободный крой", details: ["Принт ОДИН В ПОЛЕ"], stock_label: "В наличии", active: true },
      { id: "hoodie-heavy-002", category: "hoodie", name: "ТЯЖЁЛОЕ ХУДИ", price: "10 500 ₽", sizes: ["S", "M", "L", "XL"], description: "400 г/м². Держит форму и темп города.", signature: "400 грамм. Держит форму, когда город не держит.", print: "V", shape: "hoodie", spin: ["assets/heavy-hoodie.jpg", "assets/spin/honor-02.jpg", "assets/spin/honor-04.jpg", "assets/spin/honor-06.jpg"], image: "assets/heavy-hoodie.jpg", images: ["assets/heavy-hoodie.jpg"], badge: "БАЗА", material: "100% хлопок · 400 г/м²", fit: "Свободный крой", details: ["Мягкий начес"], stock_label: "В наличии", active: true },
      { id: "tee-basic-002", category: "tee", name: "НА КАЖДЫЙ ДЕНЬ", price: "3 900 ₽", sizes: ["S", "M", "L", "XL"], description: "База на каждый день.", signature: "На каждый день. Снимать не хочется.", print: "V", shape: "tee", spin: ["assets/base-tee.jpg", "assets/spin/year-02.jpg", "assets/spin/year-04.jpg"], image: "assets/base-tee.jpg", images: ["assets/base-tee.jpg"], badge: "ДЕНЬ", material: "100% хлопок · 240 г/м²", fit: "Свободный крой", details: ["Плотная горловина"], stock_label: "В наличии", active: true },
      { id: "cargo-city-001", category: "bottom", name: "КАРГО", price: "8 500 ₽", sizes: ["S", "M", "L"], description: "Свободные карго. Город не бережёт — эти выдержат.", signature: "Карманы не для красоты. Город не бережёт.", print: "V", shape: "cargo", spin: ["assets/city-cargo.jpg", "assets/look-street.jpg"], image: "assets/city-cargo.jpg", images: ["assets/city-cargo.jpg", "assets/look-street.jpg"], badge: "ГОРОД", material: "Плотный хлопок", fit: "Свободная посадка", details: ["6 карманов"], stock_label: "Мало размеров", active: true },
      { id: "cap-logo-001", category: "access", name: "КЕПКА V", price: "3 200 ₽", sizes: ["ОДИН"], description: "Чёрная шестиклинка, красная V.", signature: "V на лбу. Свой узнает. Чужой не обязан.", print: "V", shape: "cap", spin: ["assets/v-cap.jpg", "assets/logo-cap.jpg"], image: "assets/v-cap.jpg", images: ["assets/v-cap.jpg"], badge: "СИГНАЛ", material: "100% хлопок", fit: "Регулируемый размер", details: ["Вышитая красная V"], stock_label: "В наличии", active: true }
    ],
    lookbook: [
      { image: "assets/look-street.jpg", caption: "ГОРОД" },
      { image: "assets/look-ring.jpg", caption: "РИНГ / 400" },
      { image: "assets/store-rack.jpg", caption: "ВЕШАЛКА" },
      { image: "assets/hero-drop.jpg", caption: "ХУДИ" }
    ]
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

  const DROP_END = new Date("2026-09-26T21:00:00+03:00").getTime();

  const state = {
    data: FALLBACK_CATALOG,
    category: "all",
    search: "",
    stock: "all",
    sizeFilter: "all",
    sort: "featured",
    view: "all",
    cart: loadJSON("vorozhbitov_cart", []),
    saved: loadJSON("vorozhbitov_saved", []),
    viewed: loadJSON("vorozhbitov_viewed", []),
    profile: loadJSON("vorozhbitov_profile", {
      name: "", phone: "", city: "", address: "", entrance: "",
      deliver: "СДЭК", size: "", height: "", note: ""
    }),
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
    return loadJSON("vorozhbitov_orders", []).slice(0, 8).map(row => {
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
        ? `<button type="button" class="profile-order-cancel" data-cancel-order="${Number(row.id)}">Отменить</button>`
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
      showToast("Заявка отменена.");
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
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("visible"), 2800);
  }

  function scrollToId(id) {
    const element = document.getElementById(id);
    if (element) element.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function syncBackButton() {
    if (!tg || !tg.BackButton) return;
    try {
      if (state.modalStack.length) tg.BackButton.show();
      else tg.BackButton.hide();
    } catch (_) { /* old clients */ }
  }

  function openModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.classList.remove("hidden");
    document.body.classList.add("modal-open");
    if (!state.modalStack.includes(id)) state.modalStack.push(id);
    syncBackButton();
    syncMainButton();
  }

  function closeModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.classList.add("hidden");
    state.modalStack = state.modalStack.filter(item => item !== id);
    if (!state.modalStack.length) document.body.classList.remove("modal-open");
    if (id === "productModal") stop3D();
    if (id === "payModal") stopPayPoll();
    if (id === "teaserModal") closeTeaser();
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
    viewer.setProduct(product);
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
    const all = [{ id: "all", name: "Все вещи" }, ...state.data.categories];
    root.innerHTML = all.map(category => `<button class="category-chip ${state.category === category.id ? "active" : ""}" data-category="${escapeHTML(category.id)}" type="button" role="tab" aria-selected="${state.category === category.id}">${escapeHTML(category.name)}</button>`).join("");
  }

  function filteredProducts() {
    const search = state.search.trim().toLowerCase();
    let list = state.data.products.filter(product => {
      if (product.active === false) return false;
      if (state.view === "saved" && !state.saved.includes(product.id)) return false;
      if (state.category !== "all" && product.category !== state.category) return false;
      if (state.stock === "limited" && !isLimited(product)) return false;
      if (state.stock === "available" && isLimited(product)) return false;
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
        <span class="badge-3d">ОБЗОР</span>
        <button class="product-save ${saved ? "saved" : ""}" data-save-id="${escapeHTML(product.id)}" type="button" aria-label="${saved ? "Удалить из сохранённых" : "Сохранить"}"><span class="icon" data-icon="bookmark"></span></button>
        <span class="product-hover">ОБЗОР <b>↗</b></span>
      </div>
      <div class="product-info">
        <div class="product-topline"><span>${escapeHTML(categoryName(product.category))}</span><span class="product-stock">${escapeHTML(product.stock_label || "В наличии")}</span></div>
        <h3>${escapeHTML(product.name)}</h3>
        ${product.signature ? `<p class="product-sign">${escapeHTML(product.signature)}</p>` : ""}
        <div class="product-bottom"><strong class="product-price">${escapeHTML(product.price)}</strong><span class="product-fit">${escapeHTML(product.fit || "Свободный крой")}</span></div>
      </div>
    </article>`;
  }

  function renderProducts() {
    const products = filteredProducts();
    const root = $("#productGrid");
    const label = state.view === "saved" ? "СОХР." : "ВЕЩИ";
    $("#productCount").textContent = `${products.length.toString().padStart(2, "0")} ${label}`;
    root.innerHTML = products.map(renderProductCard).join("");
    $("#emptyState").classList.toggle("hidden", products.length > 0);
    const banner = $("#viewBanner");
    banner.classList.toggle("hidden", state.view !== "saved");
    $("#viewBannerTitle").textContent = "СОХРАНЁННЫЕ";
    iconize(root);
    updateCounters();
  }

  function renderLookbook() {
    const items = (state.data.lookbook || []).filter(item => item.image || item.photo_url);
    const fallback = FALLBACK_CATALOG.lookbook;
    const list = items.length ? items : fallback;
    $("#lookbookGrid").innerHTML = list.map(item => {
      const src = item.image || item.photo_url;
      const caption = item.caption || "ЗАМЕТКА";
      return `<figure class="lookbook-card" data-lightbox="${escapeHTML(src)}" data-caption="${escapeHTML(caption)}"><img src="${escapeHTML(src)}" alt="${escapeHTML(caption)}" loading="lazy"><figcaption><span>ЗАМЕТКА</span><strong>${escapeHTML(caption)}</strong></figcaption></figure>`;
    }).join("") + `<div class="lookbook-note"><span class="red-slash">//</span><p>Кадры выпуска. Посадка, ткань, крой.</p><button class="button button-outline" data-scroll="catalog" type="button">В ВИТРИНУ <span>↗</span></button></div>`;
    $("#lookbookGrid").querySelector("[data-scroll]")?.addEventListener("click", () => scrollToId("catalog"));
  }

  function renderGallery(product) {
    const images = imagesFor(product);
    state.galleryIndex = 0;
    $("#sheetImage").src = images[0];
    $("#sheetImage").alt = product.name;
    const dots = $("#galleryDots");
    dots.innerHTML = images.length > 1
      ? images.map((_, index) => `<button type="button" data-gallery="${index}" class="${index === 0 ? "active" : ""}" aria-label="Фото ${index + 1}"></button>`).join("")
      : "";
  }

  function setGallery(index) {
    const product = state.currentProduct;
    if (!product) return;
    const images = imagesFor(product);
    state.galleryIndex = (index + images.length) % images.length;
    $("#sheetImage").src = images[state.galleryIndex];
    $$("#galleryDots button").forEach((node, i) => node.classList.toggle("active", i === state.galleryIndex));
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
    $("#sizeList").innerHTML = (product.sizes || []).map(size => `<button class="size-button ${state.selectedSize === size ? "selected" : ""}" data-size="${escapeHTML(size)}" type="button">${escapeHTML(size)}</button>`).join("");
    $("#sizeHint").textContent = state.selectedSize ? `Размер ${state.selectedSize} выбран.` : "Выбери размер.";
    $("#sizeHint").classList.remove("error");
    $("#sheetDetails").innerHTML = `<strong>ДЕТАЛИ</strong><br>${(product.details || []).map(escapeHTML).join(" · ")}`;
    renderPersonalization(product);
    const saved = state.saved.includes(product.id);
    $("#sheetSave").classList.toggle("saved", saved);
    $("#sheetSave").setAttribute("aria-label", saved ? "Удалить из сохранённых" : "Сохранить");
    const related = state.data.products.filter(item => item.id !== product.id && item.category === product.category && item.active !== false).slice(0, 2);
    $("#related").innerHTML = related.length
      ? `<h4>С ЭТИМ БЕРУТ</h4>${related.map(item => `<article class="related-card" data-related="${escapeHTML(item.id)}"><img src="${escapeHTML(imageFor(item))}" alt="${escapeHTML(item.name)}"><span>${escapeHTML(item.name)}</span></article>`).join("")}`
      : "";
    iconize($("#productModal"));
  }

  function rememberViewed(id) {
    state.viewed = [id, ...state.viewed.filter(item => item !== id)].slice(0, 8);
    saveJSON("vorozhbitov_viewed", state.viewed);
  }

  function openProduct(id) {
    const product = productById(id);
    if (!product) return;
    rememberViewed(id);
    renderSheet(product);
    openModal("productModal");
    window.requestAnimationFrame(() => setStage("3d"));
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
    const product = state.currentProduct;
    if (!product) return;
    if (!state.selectedSize) {
      $("#sizeHint").textContent = "Сначала выбери размер.";
      $("#sizeHint").classList.add("error");
      haptic("error");
      return;
    }
    const person = personValue(product);
    if (person === false) return;
    // Разные номера жетона — разные строки заявки, поэтому номер входит в ключ.
    const key = `${product.id}::${state.selectedSize}${person ? `::${person}` : ""}`;
    const existing = state.cart.find(item => item.key === key);
    if (existing) existing.qty += state.qty;
    else state.cart.push({ key, id: product.id, size: state.selectedSize, qty: state.qty, person: person || "" });
    saveJSON("vorozhbitov_cart", state.cart);
    updateCounters();
    closeModal("productModal");
    haptic("success");
    showToast("Добавлено в заявку.");
    setTimeout(() => openCart(), 160);
  }

  function sendWaitlist() {
    const product = state.currentProduct;
    if (!product) return;
    if (!state.selectedSize) {
      $("#sizeHint").textContent = "Выбери размер, который ждёшь.";
      $("#sizeHint").classList.add("error");
      return;
    }
    const list = loadJSON("vorozhbitov_waitlist", []);
    const key = `${product.id}::${state.selectedSize}`;
    if (!list.includes(key)) list.push(key);
    saveJSON("vorozhbitov_waitlist", list);
    const payload = { type: "waitlist", product_id: product.id, size: state.selectedSize };
    if (tg && typeof tg.sendData === "function") {
      try {
        tg.sendData(JSON.stringify(payload));
        haptic("medium");
        return;
      } catch (_) { /* stay on the page and show toast */ }
    }
    showToast("Размер записан. В Telegram напишем, когда вернётся.");
    haptic("medium");
  }

  function cartItems() {
    return state.cart.map(item => ({ item, product: productById(item.id) })).filter(pair => pair.product);
  }

  function cartTotal() {
    return cartItems().reduce((sum, pair) => sum + priceNumber(pair.product.price) * pair.item.qty, 0);
  }

  function renderCart() {
    const pairs = cartItems();
    const content = $("#cartContent");
    if (!pairs.length) {
      content.innerHTML = `<div class="cart-empty"><span class="empty-mark">∅</span><h3>Заявка пока пустая.</h3><p>Добавь вещь из выпуска — здесь соберём всё перед оплатой.</p></div>`;
      $("#checkoutForm").classList.add("hidden");
      return;
    }
    content.innerHTML = pairs.map(({ item, product }) => `<div class="cart-line" data-cart-key="${escapeHTML(item.key)}"><img class="cart-line-image" src="${escapeHTML(imageFor(product))}" alt="${escapeHTML(product.name)}"><div class="cart-line-name"><strong>${escapeHTML(product.name)}</strong><small>Размер: ${escapeHTML(item.size)} · ${escapeHTML(product.price)}</small><div class="qty-control"><button data-qty="minus" type="button" aria-label="Уменьшить">−</button><span>${item.qty}</span><button data-qty="plus" type="button" aria-label="Увеличить">+</button></div></div><div class="cart-line-end"><strong>${rubles(priceNumber(product.price) * item.qty)}</strong><button class="remove-line" data-remove-key="${escapeHTML(item.key)}" type="button">УДАЛИТЬ</button></div></div>`).join("");
    $("#cartTotal").textContent = rubles(cartTotal());
    $("#checkoutForm").classList.remove("hidden");
  }

  function openCart() {
    applyProfileToCheckout();
    renderCart();
    openModal("cartModal");
  }

  function updateCartItem(key, delta) {
    const line = state.cart.find(item => item.key === key);
    if (!line) return;
    line.qty += delta;
    if (line.qty <= 0) state.cart = state.cart.filter(item => item.key !== key);
    saveJSON("vorozhbitov_cart", state.cart);
    renderCart();
    updateCounters();
  }

  function removeCartItem(key) {
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
    const cartOpen = state.modalStack.includes("cartModal");
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
    const pairs = cartItems();
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
      request_id: `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
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
    const history = loadJSON("vorozhbitov_orders", []);
    history.unshift({ at: Date.now(), total: cartTotal(), items: payload.items });
    saveJSON("vorozhbitov_orders", history.slice(0, 12));

    const button = $("#submitOrder");
    const previous = button ? button.innerHTML : "";
    if (button) {
      button.disabled = true;
      button.textContent = "СОБИРАЮ СЧЁТ…";
    }

    const initData = tg && tg.initData;
    if (initData) {
      try {
        const response = await fetch("/api/checkout", {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-Telegram-Init-Data": initData
          },
          body: JSON.stringify(payload)
        });
        const data = await response.json().catch(() => ({}));
        if (response.ok && data.ok) {
          state.cart = [];
          saveJSON("vorozhbitov_cart", state.cart);
          updateCounters();
          haptic("success");
          showPaySheet(data);
          return;
        }
        if (response.status !== 401) {
          showToast(data.error || "Не получилось создать оплату.");
          haptic("error");
          return;
        }
      } catch (_) { /* fall back to sendData */ }
      finally {
        if (button) {
          button.disabled = false;
          button.innerHTML = previous || "ПЕРЕЙТИ К ОПЛАТЕ <span>↗</span>";
        }
      }
    } else if (button) {
      button.disabled = false;
      button.innerHTML = previous || "ПЕРЕЙТИ К ОПЛАТЕ <span>↗</span>";
    }

    if (tg && typeof tg.sendData === "function") {
      try { tg.sendData(JSON.stringify(payload)); }
      catch (_) {
        showToast("Не получилось отправить. Попробуй ещё раз.");
        haptic("error");
        return;
      }
    }
    state.cart = [];
    saveJSON("vorozhbitov_cart", state.cart);
    updateCounters();
    closeModal("cartModal");
    $("#successToast").classList.remove("hidden");
    haptic("success");
    showToast(tg ? "Заявка ушла. Оплата — в чате с ботом." : "Демо-заявка. В Telegram откроется оплата.");
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
    let size = "XL";
    if (height < 172) size = "S";
    else if (height < 178) size = "M";
    else if (height < 186) size = "L";
    result.classList.remove("error");
    result.textContent = `Рост ${height} см → бери ${size}. Если любишь свободнее — на размер больше.`;
  }

  function tickDrop() {
    const node = $("#dropTimer");
    if (!node) return;
    const diff = DROP_END - Date.now();
    if (diff <= 0) {
      node.textContent = "ВЫПУСК";
      return;
    }
    const days = Math.floor(diff / 86400000);
    const hours = Math.floor((diff % 86400000) / 3600000);
    const mins = Math.floor((diff % 3600000) / 60000);
    const secs = Math.floor((diff % 60000) / 1000);
    const pad = value => String(value).padStart(2, "0");
    node.textContent = days > 0 ? `${days}д ${pad(hours)}:${pad(mins)}` : `${pad(hours)}:${pad(mins)}:${pad(secs)}`;
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
    sessionStorage.setItem("vorozhbitov_entered", "1");
    $("#welcome").classList.add("hidden");
    document.body.classList.remove("welcoming", "booting");
    haptic("medium");
  }

  function startExperience() {
    const boot = $("#boot");
    const welcome = $("#welcome");
    const skipWelcome = sessionStorage.getItem("vorozhbitov_entered") === "1";
    window.setTimeout(() => {
      document.body.classList.remove("booting");
      if (boot) boot.classList.add("hidden");
      if (skipWelcome) {
        welcome.classList.add("hidden");
        document.body.classList.remove("welcoming");
      } else {
        welcome.classList.remove("hidden");
        document.body.classList.add("welcoming");
      }
    }, 1400);
  }

  async function loadCatalog() {
    try {
      const response = await fetch("/api/catalog", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("catalog unavailable");
      const remote = await response.json();
      if (Array.isArray(remote.products) && Array.isArray(remote.categories)) {
        state.data = {
          ...FALLBACK_CATALOG,
          ...remote,
          products: remote.products.filter(product => product.active !== false),
          lookbook: Array.isArray(remote.lookbook) && remote.lookbook.length ? remote.lookbook : FALLBACK_CATALOG.lookbook,
          privacy_url: remote.privacy_url || FALLBACK_CATALOG.privacy_url || ""
        };
        const privacy = document.getElementById("privacyLink");
        if (privacy && state.data.privacy_url) {
          privacy.href = state.data.privacy_url;
          privacy.classList.remove("hidden");
        }
      }
    } catch (_) {
      state.data = FALLBACK_CATALOG;
    }
    applyMedia();
    renderCategoryChips();
    renderProducts();
    renderLookbook();
  }

  /* --- Видео бренда: hero-петля и тизер --- */

  function mediaConfig() {
    const media = (state.data && state.data.media) || {};
    return {
      heroLoop: media.hero_loop || "assets/video/hero-loop.mp4",
      heroPoster: media.hero_poster || "assets/video/hero-poster.jpg",
      teaser: media.teaser || "assets/video/teaser.mp4",
      teaserPoster: media.teaser_poster || "assets/video/teaser-poster.jpg",
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
    const poster = $("#teaserPoster");
    if (poster) poster.src = media.teaserPoster;
    if ($("#teaserTitle")) $("#teaserTitle").textContent = media.title;
    if ($("#teaserCaption")) $("#teaserCaption").textContent = media.caption;
    const teaserVideo = $("#teaserVideo");
    if (teaserVideo) teaserVideo.poster = media.teaserPoster;
    setupHeroVideo(media);
  }

  function setupHeroVideo(media) {
    const video = $("#heroVideo");
    const fallback = $("#heroFallback");
    const playButton = $("#heroPlay");
    if (!video) return;
    video.poster = media.heroPoster;
    if (saverMode() || reducedMotion()) {
      // Показываем постер съёмки вместо анимации.
      if (fallback) fallback.src = media.heroPoster;
      return;
    }
    if (!video.dataset.src) {
      video.dataset.src = media.heroLoop;
      video.src = media.heroLoop;
      video.load();
    }
    video.classList.remove("hidden");
    if (fallback) fallback.classList.add("hidden");

    const tryPlay = () => {
      const attempt = video.play();
      if (attempt && typeof attempt.catch === "function") {
        // iOS WKWebView может отклонить автоплей — тогда показываем кнопку.
        attempt.then(() => playButton && playButton.classList.add("hidden"))
               .catch(() => playButton && playButton.classList.remove("hidden"));
      }
    };
    tryPlay();

    if (playButton && !playButton.dataset.bound) {
      playButton.dataset.bound = "1";
      playButton.addEventListener("click", () => {
        tryPlay();
        haptic("light");
      });
    }
    // Не крутим видео за пределами экрана и в свёрнутом приложении.
    if ("IntersectionObserver" in window && !video.dataset.observed) {
      video.dataset.observed = "1";
      const observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (entry.isIntersecting) tryPlay();
          else video.pause();
        });
      }, { threshold: 0.15 });
      observer.observe(video);
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) video.pause();
        else if (!state.modalStack.includes("teaserModal")) tryPlay();
      });
    }
  }

  function openTeaser() {
    const media = mediaConfig();
    const video = $("#teaserVideo");
    const hero = $("#heroVideo");
    if (hero) hero.pause();
    if (video) {
      if (!video.src) video.src = media.teaser;
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
    const hero = $("#heroVideo");
    if (hero && !saverMode() && !reducedMotion()) {
      const attempt = hero.play();
      if (attempt && typeof attempt.catch === "function") attempt.catch(() => {});
    }
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

  function bindEvents() {
    $("#enterShop").addEventListener("click", enterShop);

    if ($("#teaserCard")) $("#teaserCard").addEventListener("click", openTeaser);
    if ($("#teaserToStory")) $("#teaserToStory").addEventListener("click", shareTeaserToStory);
    if ($("#teaserToShop")) $("#teaserToShop").addEventListener("click", () => {
      closeModal("teaserModal");
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
      $$(".size-button", $("#sizeList")).forEach(node => node.classList.toggle("selected", node === button));
      $("#sizeHint").textContent = `Размер ${state.selectedSize} выбран.`;
      $("#sizeHint").classList.remove("error");
      haptic("light");
    });

    $("#galleryDots").addEventListener("click", event => {
      const button = event.target.closest("[data-gallery]");
      if (button) setGallery(Number(button.dataset.gallery));
    });
    $("#sheetImage").addEventListener("click", () => {
      if (state.currentProduct && imagesFor(state.currentProduct).length > 1) setGallery(state.galleryIndex + 1);
    });

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

    $("#filterToggle").addEventListener("click", () => $("#filterPanel").classList.toggle("hidden"));
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
      $$("#sizeFilters .filter-chip").forEach(node => node.classList.toggle("active", node === button));
      state.sizeFilter = button.dataset.sizeFilter || "all";
      updateFilterCount();
      renderProducts();
    });
    $("#sortSelect").addEventListener("change", event => {
      state.sort = event.target.value;
      renderProducts();
    });
    $("#resetFilters").addEventListener("click", () => {
      state.stock = "all";
      state.sizeFilter = "all";
      state.search = "";
      state.category = "all";
      state.view = "all";
      state.sort = "featured";
      $("#searchInput").value = "";
      $("#sortSelect").value = "featured";
      $("#clearSearch").classList.add("hidden");
      $$("#filterOptions .filter-chip").forEach(node => node.classList.toggle("active", node.dataset.stock === "all"));
      $$("#sizeFilters .filter-chip").forEach(node => node.classList.toggle("active", node.dataset.sizeFilter === "all"));
      updateFilterCount();
      renderCategoryChips();
      renderProducts();
    });
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

    $$("[data-scroll]").forEach(button => button.addEventListener("click", () => scrollToId(button.dataset.scroll)));
    $$("[data-close]").forEach(button => button.addEventListener("click", () => closeModal(button.dataset.close)));
    $$(".modal-backdrop").forEach(backdrop => backdrop.addEventListener("click", event => {
      if (event.target === backdrop) closeModal(backdrop.id);
    }));
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      const search = $("#searchOverlay");
      if (search && !search.classList.contains("hidden") && !state.modalStack.length) {
        search.classList.add("hidden");
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
  tickDrop();
  window.setInterval(tickDrop, 1000);
  window.VorozhbitovShop = { state, openProduct, openCart };
})();
