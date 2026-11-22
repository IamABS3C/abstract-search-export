/* rocket-a.js: behaviour for the Abstract mark as a rocket. Drop-in, no dependencies. Pairs with rocket-a.css
 * and rocket-a.svg. Works as a classic script (window.RocketA) and as a CommonJS module.
 *
 *   RocketA.sprite(svgText?)        make sure <symbol id="rocket-a-mark"> is inline in this document
 *   RocketA.mark(size, opts?)       the plain, untilted mark as an <svg> (header logo, diagram nodes)
 *   RocketA.mount(host, opts?)      build one rocket inside host; returns its controller
 *   RocketA.track(ctl, area)        pointer-track: the engine gimbals toward the pointer or the focused control
 *   RocketA.fly(ctl, path)          move a free rocket along a cubic curve; resolves when it arrives
 *   RocketA.orbit(host, opts)       the long-wait loader: the rocket circles, the arc grows only on real steps
 *   RocketA.motion.ok               false when the system or html[data-motion="off"] asks for stillness
 *   RocketA.now                     the clock fly() and orbit() read; replace it to share one pausable app clock
 *
 * Rules this file enforces: the mark's paths are never touched (it is always one <use>); nothing is written
 * to the DOM per frame except custom properties on the rocket itself; with motion off every call still lands
 * on its final state, at once.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.RocketA = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  var VIEWBOX = "0 0 168.948 149.981", RATIO = 149.981 / 168.948, SVGNS = "http://www.w3.org/2000/svg";
  var doc = typeof document !== "undefined" ? document : null;
  var mq = typeof matchMedia === "function" ? matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };

  /* ------------------------------------------------------------------ motion gate */
  var motion = {
    get ok() { return !mq.matches && !(doc && doc.documentElement.getAttribute("data-motion") === "off"); },
    onChange: function (fn) {
      if (mq.addEventListener) mq.addEventListener("change", fn);
      if (doc && typeof MutationObserver === "function") new MutationObserver(fn).observe(doc.documentElement, { attributes: true, attributeFilter: ["data-motion"] });
    }
  };

  /* ------------------------------------------------------------------ easing: the CSS tokens, as functions */
  function bezier(x1, y1, x2, y2) {
    var cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx, cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    return function (x) {
      if (x <= 0) return 0; if (x >= 1) return 1;
      var t = x, i, d;
      for (i = 0; i < 6; i++) { d = ((ax * t + bx) * t + cx) * t - x; if (Math.abs(d) < 1e-5) break; t -= d / ((3 * ax * t + 2 * bx) * t + cx || 1e-6); }
      return ((ay * t + by) * t + cy) * t;
    };
  }
  var EASE = {
    travel: bezier(0.16, 1, 0.3, 1),        /* --ease-travel: arrivals */
    ignite: bezier(0.7, 0, 0.84, 0),        /* --ease-ignite: departures, power cuts */
    standard: bezier(0.2, 0, 0.2, 1),       /* --ease-standard */
    linear: function (t) { return t; }
  };

  /* ------------------------------------------------------------------ the inline sprite */
  function sprite(svgText) {
    if (!doc) return false;
    if (doc.getElementById("rocket-a-mark")) return true;
    if (!svgText) return false;
    var defs = /<defs>[\s\S]*?<\/defs>/.exec(svgText), sym = /<symbol[\s\S]*?<\/symbol>/.exec(svgText);
    if (!sym) return false;
    var holder = doc.createElement("div");
    /* Not display:none: a gradient inside a hidden SVG stops painting in some engines. */
    holder.innerHTML = '<svg xmlns="' + SVGNS + '" aria-hidden="true" focusable="false" style="position:absolute;width:0;height:0;overflow:hidden">' + (defs ? defs[0] : "") + sym[0] + "</svg>";
    doc.body.insertBefore(holder.firstChild, doc.body.firstChild);
    return true;
  }
  function useMark(cls) {
    var svg = doc.createElementNS(SVGNS, "svg"), use = doc.createElementNS(SVGNS, "use");
    svg.setAttribute("viewBox", VIEWBOX); svg.setAttribute("focusable", "false"); if (cls) svg.setAttribute("class", cls);
    use.setAttribute("href", "#rocket-a-mark"); svg.appendChild(use);
    return svg;
  }
  /** The plain mark. `label` names it for assistive technology; without one it is decorative. */
  function mark(size, opts) {
    var svg = useMark(opts && opts.className);
    svg.setAttribute("width", size); svg.setAttribute("height", Math.round(size * RATIO * 100) / 100);
    if (opts && opts.label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", opts.label); } else svg.setAttribute("aria-hidden", "true");
    return svg;
  }

  /* ------------------------------------------------------------------ one rocket */
  function span(cls) { var e = doc.createElement("span"); e.className = cls; return e; }
  function mount(host, opts) {
    opts = opts || {};
    var el = span("rocket-a"), body = span("rocket-a__body"), lean = span("rocket-a__lean"), engine = span("rocket-a__engine"), tag = span("rocket-a__tag");
    engine.appendChild(span("rocket-a__plume")); engine.appendChild(span("rocket-a__core"));
    lean.appendChild(span("rocket-a__bloom")); lean.appendChild(engine); lean.appendChild(useMark("rocket-a__mark"));
    lean.lastChild.setAttribute("aria-hidden", "true");
    body.appendChild(lean); el.appendChild(body);
    if (opts.clamps) { var c = span("rocket-a__clamps"); c.appendChild(doc.createElement("i")); c.appendChild(doc.createElement("i")); el.appendChild(c); }
    if (opts.pad) el.appendChild(span("rocket-a__pad"));
    tag.hidden = true; el.appendChild(tag);
    if (opts.free) el.setAttribute("data-free", "");
    if (opts.tagSide) el.setAttribute("data-tag", opts.tagSide);
    el.setAttribute("aria-hidden", "true");                 /* the state is always also written as text by the host */
    el.style.setProperty("--ra-size", (opts.size || 96) + "px");
    el.setAttribute("data-state", opts.state || "idle");
    host.appendChild(el);

    var ctl = {
      el: el,
      /** off | idle | ignite | liftoff | orbit | dock | hold | nogo */
      state: function (name) {
        if (name === undefined) return el.getAttribute("data-state");
        if (el.getAttribute("data-state") !== name) { el.style.removeProperty("--ra-thrust"); el.setAttribute("data-state", name); }
        return ctl;
      },
      thrust: function (v) { el.style.setProperty("--ra-thrust", String(Math.max(0, Math.min(1, v)))); return ctl; },
      stretch: function (v) { el.style.setProperty("--ra-stretch", String(Math.max(0, Math.min(0.5, v)))); return ctl; },
      heading: function (deg) { el.style.setProperty("--ra-heading", deg + "deg"); return ctl; },
      gimbal: function (gx) { el.style.setProperty("--ra-gx", motion.ok ? String(Math.max(-1, Math.min(1, gx))) : "0"); return ctl; },
      size: function (px) { el.style.setProperty("--ra-size", px + "px"); return ctl; },
      /** Viewport position of the mark's centre. Needs opts.free. */
      at: function (x, y) { el.style.setProperty("--ra-x", String(Math.round(x * 10) / 10)); el.style.setProperty("--ra-y", String(Math.round(y * 10) / 10)); ctl.x = x; ctl.y = y; return ctl; },
      /** The reason beside a holding or no-go rocket. Pass null to clear. The host also announces it as text. */
      tag: function (text) { if (text) { if (tag.textContent !== text) tag.textContent = text; tag.hidden = false; } else tag.hidden = true; return ctl; },
      destroy: function () { if (ctl._fly) ctl._fly.cancel(); if (el.parentNode) el.parentNode.removeChild(el); }
    };
    return ctl;
  }

  /* ------------------------------------------------------------------ pointer-track (gimbal) */
  function track(ctl, area) {
    var raf = 0, target = 0;
    function write() { raf = 0; ctl.gimbal(target); }
    function aim(clientX) {
      if (!motion.ok) return;
      var r = ctl.el.getBoundingClientRect(), a = area.getBoundingClientRect(), half = Math.max(1, a.width / 2);
      target = Math.max(-1, Math.min(1, (clientX - (r.left + r.width / 2)) / half));
      if (!raf) raf = requestAnimationFrame(write);
    }
    function move(e) { aim(e.clientX); }
    function focus(e) { var r = e.target.getBoundingClientRect && e.target.getBoundingClientRect(); if (r) aim(r.left + r.width / 2); }
    function leave() { target = 0; if (!raf) raf = requestAnimationFrame(write); }
    area.addEventListener("pointermove", move, { passive: true });
    area.addEventListener("pointerleave", leave);
    area.addEventListener("focusin", focus);
    return function stop() {
      area.removeEventListener("pointermove", move); area.removeEventListener("pointerleave", leave); area.removeEventListener("focusin", focus);
      if (raf) cancelAnimationFrame(raf); ctl.gimbal(0);
    };
  }

  /* ------------------------------------------------------------------ flight along a cubic curve */
  function cubic(p, t, out) {
    var u = 1 - t, a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, d = t * t * t;
    out[0] = a * p[0] + b * p[2] + c * p[4] + d * p[6]; out[1] = a * p[1] + b * p[3] + c * p[5] + d * p[7];
    out[2] = 3 * u * u * (p[2] - p[0]) + 6 * u * t * (p[4] - p[2]) + 3 * t * t * (p[6] - p[4]);
    out[3] = 3 * u * u * (p[3] - p[1]) + 6 * u * t * (p[5] - p[3]) + 3 * t * t * (p[7] - p[5]);
    return out;
  }
  /**
   * path: { from:[x,y], c1:[x,y], c2:[x,y], to:[x,y], duration (ms), ease ("travel" | "ignite" | "standard" | fn),
   *         heading ("auto" | degrees), onFrame(x, y, headingDeg, t) }
   * Returns a promise with .cancel(). With motion off it lands at once and still calls onFrame(…, 1).
   */
  function fly(ctl, path) {
    var p = [].concat(path.from, path.c1 || path.from, path.c2 || path.to, path.to), o = [0, 0, 0, 0];
    var ease = typeof path.ease === "function" ? path.ease : EASE[path.ease || "travel"], dur = Math.max(1, path.duration || 700);
    var raf = 0, done, cancelled = false;
    function pose(t) {
      cubic(p, t, o);
      var deg = path.heading === "auto" || path.heading === undefined ? Math.atan2(o[3], o[2]) * 180 / Math.PI + 90 : path.heading;
      ctl.at(o[0], o[1]).heading(Math.round(deg * 10) / 10);
      if (path.onFrame) path.onFrame(o[0], o[1], deg, t);
    }
    var promise = new Promise(function (resolve) {
      done = resolve;
      if (ctl._fly) ctl._fly.cancel();
      if (!motion.ok) { pose(1); resolve(true); return; }
      var t0 = 0;
      function step() {
        if (cancelled) return;
        var now = api.now();                               /* one timebase everywhere, whatever the frame callback reports */
        if (!t0) t0 = now;
        var k = Math.min(1, (now - t0) / dur);
        pose(ease(k));
        if (k < 1 && motion.ok) raf = requestAnimationFrame(step); else { if (k < 1) pose(1); ctl._fly = null; resolve(true); }
      }
      raf = requestAnimationFrame(step);
    });
    promise.cancel = function () { if (cancelled) return; cancelled = true; if (raf) cancelAnimationFrame(raf); if (ctl._fly === promise) ctl._fly = null; done(false); };
    ctl._fly = promise;
    return promise;
  }

  /* ------------------------------------------------------------------ the orbit loader
     An eccentric ellipse (e = 0.5) with the body at a focus, so the ship is quick past the near side and slow at
     the far side (Kepler, speed ratio 3 to 1). One tick per real step. The arc is the share of steps finished:
     it grows only when the host calls step(i), never with time. */
  function orbit(host, opts) {
    opts = opts || {};
    var n = Math.max(1, opts.steps || 4), period = opts.period || 3600, W = 200, H = 160, cx = 106, cy = 80, a = 80, e = 0.5, b = a * Math.sqrt(1 - e * e), fx = cx - a * e;
    var el = doc.createElement("div"); el.className = "rocket-orbit";
    var svg = doc.createElementNS(SVGNS, "svg"); svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.setAttribute("class", "rocket-orbit__svg"); svg.setAttribute("aria-hidden", "true");
    function node(name, cls, attrs) { var x = doc.createElementNS(SVGNS, name); x.setAttribute("class", cls); for (var k in attrs) x.setAttribute(k, attrs[k]); svg.appendChild(x); return x; }
    function solve(M) { var E = M, i; for (i = 0; i < 6; i++) E -= (E - e * Math.sin(E) - M) / (1 - e * Math.cos(E)); return E; }
    function point(E) { return [cx - a * Math.cos(E), cy - b * Math.sin(E)]; }          /* E = 0 is the near side, by the body */
    node("ellipse", "rocket-orbit__path", { cx: cx, cy: cy, rx: a, ry: b });
    var arc = node("path", "rocket-orbit__arc", { d: "" });
    node("circle", "rocket-orbit__halo", { cx: fx, cy: cy, r: 22 });
    node("circle", "rocket-orbit__planet", { cx: fx, cy: cy, r: 11 });
    var ticks = [], tickE = [], i;
    for (i = 0; i < n; i++) { tickE[i] = solve(2 * Math.PI * (i + 1) / n); var q = point(tickE[i]); ticks[i] = node("circle", "rocket-orbit__tick", { cx: q[0].toFixed(1), cy: q[1].toFixed(1), r: 3.4 }); }
    el.appendChild(svg);
    var ship = mount(el, { size: opts.size || 24, state: "orbit" }); ship.el.classList.add("rocket-orbit__ship");
    host.appendChild(el);

    var finished = 0, raf = 0, t0 = 0, stopped = false;
    function drawArc() {
      if (!finished) { arc.setAttribute("d", ""); return; }
      var end = finished >= n ? 2 * Math.PI : tickE[finished - 1], d = "", k, steps = Math.max(8, Math.round(end * 10)), q;
      for (k = 0; k <= steps; k++) { q = point(end * k / steps); d += (k ? "L" : "M") + q[0].toFixed(1) + " " + q[1].toFixed(1); }
      arc.setAttribute("d", d);
    }
    function place(E) {
      var q = point(E), dx = a * Math.sin(E), dy = -b * Math.cos(E);
      ship.el.style.left = (q[0] / W * 100).toFixed(2) + "%"; ship.el.style.top = (q[1] / H * 100).toFixed(2) + "%";
      ship.heading(Math.round(Math.atan2(dy, dx) * 180 / Math.PI + 90));
    }
    function loop() {
      raf = 0; if (stopped) return;
      var now = api.now();
      if (!t0) t0 = now;
      place(solve(2 * Math.PI * (((now - t0) % period) / period)));
      if (motion.ok) raf = requestAnimationFrame(loop);
    }
    function mark() { for (var k = 0; k < n; k++) { if (k < finished) ticks[k].setAttribute("data-done", ""); else ticks[k].removeAttribute("data-done"); if (k === finished) ticks[k].setAttribute("data-now", ""); else ticks[k].removeAttribute("data-now"); } drawArc(); }
    place(0); mark();
    if (motion.ok) raf = requestAnimationFrame(loop);
    return {
      el: el,
      /** Step i (0-based) really finished. */
      step: function (idx) { finished = Math.max(finished, Math.min(n, idx + 1)); mark(); if (!motion.ok) place(tickE[Math.min(n, finished) - 1]); },
      done: function () { finished = n; stopped = true; if (raf) cancelAnimationFrame(raf); mark(); el.setAttribute("data-done", ""); ship.state("off"); },
      fail: function () { stopped = true; if (raf) cancelAnimationFrame(raf); el.setAttribute("data-failed", ""); ship.state("off"); },
      destroy: function () { stopped = true; if (raf) cancelAnimationFrame(raf); if (el.parentNode) el.parentNode.removeChild(el); }
    };
  }

  var api = { sprite: sprite, mark: mark, mount: mount, track: track, fly: fly, orbit: orbit, motion: motion, ease: EASE, bezier: bezier, RATIO: RATIO, now: function () { return performance.now(); } };
  return api;
});
