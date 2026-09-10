(() => {
  "use strict";

  function framesOf(product) {
    const candidates = product && Array.isArray(product.spin) && product.spin.length
      ? product.spin : [product && product.image, ...((product && product.images) || [])];
    const frames = [...new Set(candidates.filter(src => typeof src === "string" && src))];
    return frames.length ? frames : ["assets/base-tee.jpg"];
  }

  function Viewer(root) {
    this.root = root;
    this.frames = [];
    this.index = 0;
    this.vel = 0;
    this.rotating = false;
    this.dragging = false;
    this.alive = false;
    this.raf = 0;
    this.lastTime = 0;
    this.preloads = new Map();
    for (const name of ["onDown", "onMove", "onUp", "onWheel", "onKey", "tick"]) this[name] = this[name].bind(this);
    root.innerHTML = '<img class="spin-frame" alt="" draggable="false" decoding="async"><span class="spin-error hidden" role="status">Этот ракурс не загрузился. Попробуй следующий.</span>';
    this.img = root.querySelector(".spin-frame");
    this.error = root.querySelector(".spin-error");
    this.img.onload = () => { root.setAttribute("aria-busy", "false"); this.error.classList.add("hidden"); };
    this.img.onerror = () => { root.setAttribute("aria-busy", "false"); this.error.classList.remove("hidden"); };
    root.addEventListener("pointerdown", this.onDown);
    window.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onUp);
    window.addEventListener("pointercancel", this.onUp);
    root.addEventListener("wheel", this.onWheel, { passive: false });
    root.addEventListener("keydown", this.onKey);
  }

  Viewer.prototype.setProduct = function (product) {
    this.product = product;
    this.frames = framesOf(product);
    this.index = 0;
    this.vel = 0;
    this.rotating = false;
    this.preloads.clear();
    this.img.alt = `${product.name || "Вещь"} — обзор с разных сторон`;
    this.show();
  };

  Viewer.prototype.frameIndex = function () {
    const count = this.frames.length;
    return count ? ((Math.round(this.index) % count) + count) % count : 0;
  };

  Viewer.prototype.currentSource = function () { return this.frames[this.frameIndex()]; };

  Viewer.prototype.show = function () {
    if (!this.frames.length) return;
    const index = this.frameIndex();
    const src = this.frames[index];
    if (this.img.getAttribute("src") !== src) {
      this.root.setAttribute("aria-busy", "true");
      this.error.classList.add("hidden");
      this.img.src = src;
      this.root.dispatchEvent(new CustomEvent("framechange", { detail: { index, total: this.frames.length } }));
    }
    // Only adjacent angles are prefetched; the catalog never loads every spin at startup.
    for (const offset of [-1, 1]) {
      const next = this.frames[(index + offset + this.frames.length) % this.frames.length];
      if (!this.preloads.has(next)) {
        const image = new Image();
        image.src = next;
        this.preloads.set(next, image);
      }
    }
  };

  Viewer.prototype.schedule = function () {
    if (this.alive && !this.raf && !this.dragging && (this.rotating || Math.abs(this.vel) > .01)) this.raf = requestAnimationFrame(this.tick);
  };

  Viewer.prototype.tick = function (time) {
    this.raf = 0;
    if (!this.alive) return;
    const elapsed = this.lastTime ? Math.min((time - this.lastTime) / 1000, .05) : 1 / 60;
    this.lastTime = time;
    if (!this.dragging && this.frames.length > 1) {
      this.index += ((this.rotating ? .8 : 0) + this.vel) * elapsed;
      this.vel *= Math.exp(-7 * elapsed);
      this.index = (this.index + this.frames.length) % this.frames.length;
    }
    this.show();
    this.schedule();
  };

  Viewer.prototype.setRotating = function (enabled) {
    this.rotating = Boolean(enabled) && this.frames.length > 1;
    this.vel = 0;
    this.lastTime = 0;
    this.root.dispatchEvent(new CustomEvent("framechange"));
    this.schedule();
  };

  Viewer.prototype.step = function (delta) {
    this.setRotating(false);
    this.index = this.frameIndex() + delta;
    this.show();
  };

  Viewer.prototype.onDown = function (event) {
    if (!this.alive || this.frames.length < 2 || (event.button != null && event.button !== 0)) return;
    this.dragging = true;
    this.axis = null;
    this.startX = this.lastX = event.clientX;
    this.startY = event.clientY;
    this.setRotating(false);
  };

  Viewer.prototype.onMove = function (event) {
    if (!this.dragging) return;
    if (!this.axis) {
      const dx = Math.abs(event.clientX - this.startX), dy = Math.abs(event.clientY - this.startY);
      if (Math.max(dx, dy) < 6) return;
      this.axis = dx > dy ? "horizontal" : "vertical";
      if (this.axis === "vertical") { this.dragging = false; return; }
      try { this.root.setPointerCapture(event.pointerId); } catch (_) { /* pointer ended */ }
    }
    const dx = event.clientX - this.lastX;
    this.lastX = event.clientX;
    this.index += dx / 42;
    const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.vel = reduce ? 0 : Math.max(-5, Math.min(5, dx / 6));
    this.show();
    if (this.img && !reduce) {
      const shift = Math.max(-14, Math.min(14, (event.clientX - this.startX) * 0.12));
      this.img.style.transform = "translateX(" + shift.toFixed(1) + "px)";
    }
  };

  Viewer.prototype.onUp = function () {
    if (!this.dragging) return;
    this.dragging = false;
    this.lastTime = 0;
    this.schedule();
    if (this.img) this.img.style.transform = "";
  };

  Viewer.prototype.onWheel = function (event) {
    if (!this.alive || this.frames.length < 2) return;
    // Vertical wheel gestures keep scrolling the product sheet.
    const delta = event.shiftKey ? event.deltaY : event.deltaX;
    if (Math.abs(delta) < 1 || (!event.shiftKey && Math.abs(event.deltaY) > Math.abs(event.deltaX))) return;
    event.preventDefault();
    this.step(delta > 0 ? 1 : -1);
  };

  Viewer.prototype.onKey = function (event) {
    if (!this.alive || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") { this.index = 0; this.step(0); }
    else if (event.key === "End") { this.index = this.frames.length - 1; this.step(0); }
    else this.step(event.key === "ArrowLeft" ? -1 : 1);
  };

  Viewer.prototype.start = function () { this.alive = true; this.lastTime = 0; this.show(); this.schedule(); };
  Viewer.prototype.stop = function () {
    this.alive = false;
    this.dragging = false;
    this.vel = 0;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
  };
  Viewer.prototype.resize = function () {};
  Viewer.prototype.apply = function () { this.show(); };
  Viewer.prototype.destroy = function () {
    this.stop();
    this.preloads.clear();
    this.root.removeEventListener("pointerdown", this.onDown);
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onUp);
    window.removeEventListener("pointercancel", this.onUp);
    this.root.removeEventListener("wheel", this.onWheel);
    this.root.removeEventListener("keydown", this.onKey);
  };

  window.Vorozhbitov3D = { Viewer };
})();
