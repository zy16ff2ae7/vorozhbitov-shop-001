(() => {
  "use strict";

  function framesOf(product) {
    if (product && Array.isArray(product.spin) && product.spin.length) {
      return product.spin.map(String);
    }
    const list = [];
    const push = src => {
      const value = String(src || "");
      if (value && !list.includes(value)) list.push(value);
    };
    if (product) {
      push(product.image);
      if (Array.isArray(product.images)) product.images.forEach(push);
    }
    return list.length ? list : ["assets/base-tee.jpg"];
  }

  function Viewer(root) {
    this.root = root;
    this.frames = [];
    this.index = 0;
    this.spin = 0;
    this.vel = 0;
    this.dragging = false;
    this.lastX = 0;
    this.alive = false;
    this.raf = 0;
    this.onDown = this.onDown.bind(this);
    this.onMove = this.onMove.bind(this);
    this.onUp = this.onUp.bind(this);
    this.onWheel = this.onWheel.bind(this);
    root.innerHTML = `<img class="spin-frame" alt="" draggable="false">`;
    this.img = root.querySelector(".spin-frame");
    root.addEventListener("pointerdown", this.onDown);
    window.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onUp);
    window.addEventListener("pointercancel", this.onUp);
    root.addEventListener("wheel", this.onWheel, { passive: false });
  }

  Viewer.prototype.setProduct = function (product) {
    this.product = product;
    this.frames = framesOf(product);
    this.index = 0;
    this.vel = 0;
    this.spin = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? 0
      : (this.frames.length > 1 ? 0.035 : 0);
    this.frames.forEach(src => { const preload = new Image(); preload.src = src; });
    if (this.img) this.img.alt = (product && product.name) || "";
    this.show();
  };

  Viewer.prototype.show = function () {
    const total = this.frames.length;
    if (!total || !this.img) return;
    const frame = ((Math.round(this.index) % total) + total) % total;
    const src = this.frames[frame];
    if (this.img.getAttribute("src") !== src) this.img.src = src;
  };

  Viewer.prototype.apply = function () {
    this.show();
  };

  Viewer.prototype.tick = function () {
    if (!this.alive) return;
    this.raf = requestAnimationFrame(() => this.tick());
    if (!this.dragging && this.frames.length > 1) {
      this.index += this.spin + this.vel;
      this.vel *= 0.92;
    }
    const total = this.frames.length;
    if (total) {
      while (this.index >= total) this.index -= total;
      while (this.index < 0) this.index += total;
    }
    this.show();
  };

  Viewer.prototype.onDown = function (event) {
    if (event.button != null && event.button !== 0) return;
    this.dragging = true;
    this.spin = 0;
    this.lastX = event.clientX;
    try { this.root.setPointerCapture(event.pointerId); } catch (_) { /* old */ }
  };

  Viewer.prototype.onMove = function (event) {
    if (!this.dragging || this.frames.length < 2) return;
    const dx = event.clientX - this.lastX;
    this.lastX = event.clientX;
    this.index += dx / 28;
    this.vel = dx / 80;
  };

  Viewer.prototype.onUp = function () {
    this.dragging = false;
  };

  Viewer.prototype.onWheel = function (event) {
    if (this.frames.length < 2) return;
    event.preventDefault();
    this.spin = 0;
    this.index += event.deltaY > 0 ? 1 : -1;
    this.show();
  };

  Viewer.prototype.start = function () {
    if (this.alive) return;
    this.alive = true;
    this.tick();
  };

  Viewer.prototype.stop = function () {
    this.alive = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
  };

  Viewer.prototype.resize = function () {};

  Viewer.prototype.destroy = function () {
    this.stop();
    this.root.removeEventListener("pointerdown", this.onDown);
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onUp);
    window.removeEventListener("pointercancel", this.onUp);
    this.root.removeEventListener("wheel", this.onWheel);
  };

  window.Vorozhbitov3D = { Viewer };
})();
