/* launch.js: the hidden launch mode.

   The tool is a calm, familiar dark app by default. Launch mode is a presentation layer only: a night sky behind the
   shell, the official Abstract mark as a rocket (rocket-a.js, never redrawn), a launch sequence once per session,
   the result tabs drawn as stations on a constellation, and a flight tag beside some headings. It never changes a
   number, a control, a layout or an export, and with reduced motion every moment shows its final frame.

   Ways in: click the Abstract mark five times, or type "launch" anywhere outside a text field. Nothing on screen offers it.
   Way out: the "Launch mode · Exit" chip, either way in again, or Escape during the sequence (which only skips it).
   Remembered per browser (localStorage); the sequence plays once per browser session (sessionStorage).
   Everything visual is scoped under :root[data-launch="on"] in launch.css. No inline style or script (strict CSP):
   positions are set through the CSSOM, and the mark's sprite is built with DOM calls, never from an HTML string. */
"use strict";
(function () {
  const KEY = "abx:launch", SEEN = "abx:launched", NS = "http://www.w3.org/2000/svg";
  const root = document.documentElement;
  const mq = window.matchMedia ? matchMedia("(prefers-reduced-motion: reduce)") : {matches: false};
  const motionOk = () => !mq.matches && root.getAttribute("data-motion") !== "off";
  const q = s => document.querySelector(s);
  let on = false, ready = false, sky = null, ctx = null, stars = [], raf = 0, warp = 0, seq = null, docked = null;
  let clicks = [], typed = "";

  function read(store, k) { try { return window[store].getItem(k); } catch (e) { return null; } }
  function write(store, k, v) { try { if (v == null) window[store].removeItem(k); else window[store].setItem(k, v); } catch (e) { /* blocked */ } }

  /* ---------- the official mark, as an inline sprite (rocket-a.js reads #rocket-a-mark) ---------- */
  async function sprite() {
    if (document.getElementById("rocket-a-mark")) return true;
    try {
      const text = await (await fetch("/static/rocket-a.svg", {credentials: "omit", cache: "force-cache"})).text();
      const src = new DOMParser().parseFromString(text, "image/svg+xml");
      const sym = src.querySelector("symbol"), defs = src.querySelector("defs");
      if (!sym) return false;
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("class", "ra-sprite"); svg.setAttribute("aria-hidden", "true"); svg.setAttribute("focusable", "false");
      if (defs) svg.appendChild(document.importNode(defs, true));
      svg.appendChild(document.importNode(sym, true));
      document.body.insertBefore(svg, document.body.firstChild);
      return true;
    } catch (e) { return false; }
  }

  /* ---------- the sky ---------- */
  function sizeSky() {
    if (!sky) return;
    const d = Math.min(2, window.devicePixelRatio || 1);
    sky.width = Math.max(1, Math.round(innerWidth * d)); sky.height = Math.max(1, Math.round(innerHeight * d));
    ctx.setTransform(d, 0, 0, d, 0, 0);
    if (!stars.length) {
      for (let i = 0; i < 360; i++) stars.push({x: Math.random(), y: Math.random(), z: Math.random() * 0.9 + 0.1,
        tw: Math.random() * 6.28, hue: Math.random() < 0.08 ? 1 : Math.random() < 0.1 ? 2 : 0});
    }
    drawSky();
  }
  function drawSky() {
    const w = innerWidth, h = innerHeight, t = performance.now() / 1000, moving = motionOk();
    ctx.clearRect(0, 0, w, h);
    const g = ctx.createRadialGradient(w * 0.62, h * 0.18, 0, w * 0.62, h * 0.18, Math.max(w, h) * 0.55);
    g.addColorStop(0, "rgba(255,33,107,0.10)"); g.addColorStop(1, "rgba(255,33,107,0)");
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    const b = ctx.createRadialGradient(w * 0.12, h * 0.9, 0, w * 0.12, h * 0.9, Math.max(w, h) * 0.45);
    b.addColorStop(0, "rgba(46,155,240,0.06)"); b.addColorStop(1, "rgba(46,155,240,0)");
    ctx.fillStyle = b; ctx.fillRect(0, 0, w, h);
    for (const s of stars) {
      const x = s.x * w, y = s.y * h;
      const a = 0.22 + 0.45 * s.z * (moving ? 0.7 + 0.3 * Math.sin(t * (0.6 + s.z) + s.tw) : 1);
      const col = s.hue === 1 ? "255,120,170" : s.hue === 2 ? "150,200,255" : "255,255,255";
      if (warp > 0) {
        ctx.strokeStyle = "rgba(" + col + "," + (a * 0.9) + ")"; ctx.lineWidth = s.z * 1.2;
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, y - warp * 54 * s.z); ctx.stroke();
      } else {
        const r = s.z * 1.2; ctx.fillStyle = "rgba(" + col + "," + a + ")"; ctx.fillRect(x - r / 2, y - r / 2, r, r);
      }
    }
  }
  function loop() {
    if (warp > 0) stars.forEach(s => { s.y = (s.y + warp * 0.012 * s.z) % 1; });
    drawSky();
    raf = on && motionOk() && !document.hidden ? requestAnimationFrame(loop) : 0;
  }
  function startSky() {
    if (!sky) {
      sky = document.createElement("canvas"); sky.id = "launch-sky"; sky.setAttribute("aria-hidden", "true");
      document.body.insertBefore(sky, document.body.firstChild); ctx = sky.getContext("2d");
    }
    sizeSky();
    if (!raf && motionOk()) raf = requestAnimationFrame(loop);
  }
  function stopSky() { if (raf) cancelAnimationFrame(raf); raf = 0; if (sky) { sky.remove(); sky = null; ctx = null; } }

  /* ---------- the result tabs as stations on a constellation ---------- */
  function drawLines() {
    const nav = q("#rtabs"); if (!nav) return;
    let svg = q("#launch-lines");
    if (!on) { if (svg) svg.remove(); return; }
    if (!svg) { svg = document.createElementNS(NS, "svg"); svg.id = "launch-lines"; svg.setAttribute("aria-hidden", "true"); nav.appendChild(svg); }
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const box = nav.getBoundingClientRect();
    svg.setAttribute("viewBox", "0 0 " + Math.max(1, Math.round(box.width)) + " " + Math.max(1, Math.round(box.height)));
    const pts = Array.from(nav.querySelectorAll("button")).map(b => {
      const r = b.getBoundingClientRect();
      return {x: r.left - box.left + r.width / 2, y: r.bottom - box.top - 1, on: b.classList.contains("on")};
    });
    for (let i = 1; i < pts.length; i++) {
      if (Math.abs(pts[i].y - pts[i - 1].y) > 8) continue;           /* a wrapped row starts a new constellation */
      const l = document.createElementNS(NS, "line");
      l.setAttribute("x1", pts[i - 1].x); l.setAttribute("y1", pts[i - 1].y); l.setAttribute("x2", pts[i].x); l.setAttribute("y2", pts[i].y);
      l.setAttribute("class", "ll"); svg.appendChild(l);
    }
    pts.forEach(p => {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", p.x); c.setAttribute("cy", p.y); c.setAttribute("r", p.on ? 3.2 : 2);
      c.setAttribute("class", p.on ? "st on" : "st"); svg.appendChild(c);
    });
  }

  /* ---------- the docked rocket beside the page title ---------- */
  function dock() {
    if (!window.RocketA || !q("#rhead")) return;
    let slot = q("#launch-dock");
    if (!slot) { slot = document.createElement("span"); slot.id = "launch-dock"; slot.setAttribute("aria-hidden", "true"); q("#rhead").appendChild(slot); }
    if (!docked) docked = RocketA.mount(slot, {size: 30, state: "dock"});
  }
  function undock() { if (docked) { docked.destroy(); docked = null; } const s = q("#launch-dock"); if (s) s.remove(); }

  /* ---------- the launch sequence (once per browser session) ---------- */
  function sequence(force) {
    if (!window.RocketA) return dock();
    if ((!force && read("sessionStorage", SEEN) === "1") || !motionOk()) return dock();
    write("sessionStorage", SEEN, "1");
    undock();
    const box = document.createElement("div"); box.id = "launch-seq"; box.setAttribute("role", "status"); box.setAttribute("aria-live", "polite");
    const count = document.createElement("div"); count.className = "ls-count";
    const skip = document.createElement("button"); skip.type = "button"; skip.className = "tbtn ls-skip"; skip.textContent = "Skip";
    box.appendChild(count); box.appendChild(skip); document.body.appendChild(box);
    const ship = RocketA.mount(document.body, {size: 96, state: "idle", free: true});
    const W = innerWidth, H = innerHeight, pad = [W / 2, H * 0.72];
    ship.at(pad[0], pad[1]);
    const timers = [];
    let stopped = false;
    const later = (fn, ms) => timers.push(setTimeout(() => { if (!stopped) fn(); }, ms));
    const say = (big, small) => { count.textContent = ""; count.appendChild(document.createTextNode(big));
      const s = document.createElement("small"); s.textContent = small; count.appendChild(s); };
    function finish() {
      if (stopped) return; stopped = true; timers.forEach(clearTimeout); warp = 0;
      if (ship._fly) ship._fly.cancel();
      ship.destroy(); box.remove(); seq = null; dock(); drawLines();
      document.removeEventListener("keydown", onKey, true);
    }
    function onKey(e) { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(); } }
    document.addEventListener("keydown", onKey, true);
    skip.addEventListener("click", finish);
    seq = finish;
    skip.focus({preventScroll: true});
    say("T-3", "Fuelling");
    later(() => { say("T-2", "Ignition"); ship.state("ignite"); }, 650);
    later(() => { say("T-1", "Clamps release"); }, 1300);
    later(() => {
      say("Away", "Clear of the tower"); ship.state("liftoff"); ship.stretch(0.4); warp = 1;
      RocketA.fly(ship, {from: pad, c1: [pad[0], H * 0.3], c2: [pad[0] + W * 0.04, -H * 0.1], to: [pad[0] + W * 0.06, -120],
        duration: 1200, heading: "auto", ease: "ignite"});
    }, 1950);
    later(() => { box.classList.add("ls-done"); warp = 0.35; }, 2900);
    later(() => {
      warp = 0; ship.stretch(0); ship.size(30);
      const t = q("#rhead"), r = t ? t.getBoundingClientRect() : {right: W * 0.8, top: 80};
      const to = [Math.min(W - 30, r.right - 24), r.top + 22];
      RocketA.fly(ship, {from: [W * 0.92, -60], c1: [W * 0.85, H * 0.12], c2: [to[0] + 90, to[1] - 60], to: to, duration: 1000, heading: "auto"})
        .then(() => { ship.state("dock"); ship.heading(0); finish(); });
    }, 3300);
  }

  /* ---------- on and off ---------- */
  async function set(v, opts) {
    on = !!v; write("localStorage", KEY, on ? "on" : null);
    if (on) root.setAttribute("data-launch", "on"); else root.removeAttribute("data-launch");
    const chip = q("#launch-chip"); if (chip) chip.hidden = !on;
    if (!on) { if (seq) seq(); stopSky(); undock(); drawLines(); return; }
    startSky();
    ready = ready || await sprite();
    drawLines();
    if (ready) sequence(opts && opts.replay);
    if (opts && opts.say && typeof announce === "function") announce("Launch mode on. It changes only how the page looks.");
  }
  function toggle() { set(!on, {say: true}); if (!on && typeof announce === "function") announce("Launch mode off."); }

  /* ---------- the hidden ways in ---------- */
  document.addEventListener("click", e => {
    if (e.target.closest("#launch-chip")) return set(false);
    if (e.target.closest("#bar .logo")) {
      const now = Date.now(); clicks = clicks.filter(t => now - t < 2500); clicks.push(now);
      if (clicks.length >= 5) { clicks = []; toggle(); }
    }
  });
  document.addEventListener("keydown", e => {
    const t = e.target, field = t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
    if (field || e.ctrlKey || e.metaKey || e.altKey || !/^[a-z]$/i.test(e.key)) { if (!field && e.key.length === 1) typed = ""; return; }
    typed = (typed + e.key.toLowerCase()).slice(-6);
    if (typed === "launch") { typed = ""; if (!on) set(true, {say: true}); }
  });
  let rz = 0;
  window.addEventListener("resize", () => { cancelAnimationFrame(rz); rz = requestAnimationFrame(() => { if (on) { sizeSky(); drawLines(); } }); });
  document.addEventListener("visibilitychange", () => { if (on && !document.hidden && !raf && motionOk()) raf = requestAnimationFrame(loop); });
  if (mq.addEventListener) mq.addEventListener("change", () => { if (on) { drawSky(); if (!motionOk() && seq) seq(); } });
  const nav = q("#rtabs");
  if (nav && window.MutationObserver) new MutationObserver(() => { if (on) drawLines(); }).observe(nav, {subtree: true, attributes: true, attributeFilter: ["class"]});

  window.abxLaunch = {set: set, toggle: toggle, get on() { return on; }};
  if (read("localStorage", KEY) === "on") set(true);
})();
