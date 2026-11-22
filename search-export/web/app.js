/* Abstract Search & Export: browser app. Talks only to the local server that launched it.
   Event values and server text are untrusted: everything rendered goes through esc() or textContent,
   and no markup carries an inline style or handler (the page runs under a strict CSP). */
"use strict";
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
/* Control and formatting characters (bidi overrides, zero-width, escapes) become visible, then HTML-escape. */
const visible = v => String(v == null ? "" : v).replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏‪-‮⁠-⁤⁦-⁯﻿]/g,
  c => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
const esc = v => visible(v).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = n => n == null ? "unknown" : Number(n).toLocaleString();
const bytes = n => { if (n == null) return ""; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i]; };
const dur = s => { s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + "h " : "") + (h || m ? m + "m " : "") + x + "s"; };

const S = {token: null, tenant: null, ops: {}, typeLabels: {}, presets: [], defaults: [], empty: null, help: [],
           home: "", sep: "/", spec: null, fields: [], datasets: [], prof: null, types: {}, fmt: "ndjson.gz",
           problems: [], problemsSeq: 0, validatedKey: null, validation: null, job: null, jobKey: "", lastStage: "",
           fails: 0, pollT: null, busy: false, pending: {}, confirmAct: null, exports: [],
           zeroKey: null, zeroCandidate: null, validating: false, typePending: {}, catalogPending: false, baseline: null};

/* ---------- transport ---------- */
function store(k, v) { try { if (v == null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } }
function stored(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }

async function api(path, opts) {
  opts = opts || {};
  const headers = {"X-ABX-Token": S.token || ""};
  if (opts.body) headers["Content-Type"] = "application/json";
  let r;
  try {
    r = await fetch("/api/" + path, {method: opts.method || (opts.body ? "POST" : "GET"), headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined, cache: "no-store", credentials: "omit"});
  } catch (e) {
    const err = new Error("The app is not responding. Is its terminal window still open?"); err.network = true; throw err;
  }
  let j = {};
  try { j = await r.json(); } catch (e) { j = {error: "Unexpected response from the app (HTTP " + r.status + ")."}; }
  if (!r.ok) {
    const err = new Error(j.error || ("HTTP " + r.status)); err.status = r.status; err.kind = j.kind; err.data = j;
    if (j.kind === "token") needLink("This page is no longer signed in to the app. The app was probably restarted.");
    if (j.kind === "busy") refreshJob();
    throw err;
  }
  return j;
}
function toast(msg, bad) {
  const t = $("#toast"); t.textContent = msg; t.className = "on" + (bad ? " bad" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.className = ""; }, bad ? 7000 : 3000);
}
function announce(msg) { const l = $("#live"); l.textContent = ""; setTimeout(() => { l.textContent = msg; }, 30); }
function notice(msg) { $("#notice-text").textContent = msg; $("#notice").hidden = !msg; }
function working(el, on, label) {
  if (on) { el.dataset.l = el.textContent; el.dataset.working = "1"; el.textContent = "";
    const sp = document.createElement("span"); sp.className = "spin"; sp.setAttribute("aria-hidden", "true");
    el.appendChild(sp); el.appendChild(document.createTextNode(label || "Working")); el.disabled = true; }
  else { if (el.dataset.l != null) el.textContent = el.dataset.l; delete el.dataset.working; el.disabled = false; refreshActions(); }
}
function sysBanner(kind, html) {
  const b = $("#sysbanner");
  if (!kind) { b.hidden = true; b.innerHTML = ""; b.className = "sysbanner"; return; }
  b.className = "sysbanner " + kind; b.innerHTML = html; b.hidden = false;
}
function locked(msg) {
  clearTimeout(S.pollT); S.pollT = null;
  $("#l-msg").textContent = msg; $("#locked").hidden = false;
}
/* No session: ask the app for a new link. With a terminal the person presses Enter there; without one (a container,
   a service) the app writes the link to its own log and says so. The answer never carries the link itself. */
async function needLink(msg) {
  locked(msg);
  try {
    const r = await fetch("/api/relink", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
      cache: "no-store", credentials: "omit"});
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.printed) { $("#l-how").hidden = true; locked(msg + " A new link has been written to the app's log: in Docker Desktop open the container and its Logs tab, or run \"docker logs abstract-search\" in a terminal. Open that link to continue."); }
  } catch (e) { /* the first message stands */ }
}

/* ---------- tabs ---------- */
function tab(name) {
  $$("#rtabs button").forEach(b => { const on = b.dataset.t === name; b.classList.toggle("on", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
  $$(".rview").forEach(v => v.classList.toggle("on", v.id === "v-" + name));
  if (name === "exports") loadExports();
  if (name === "fields") renderFieldGrid();
  if (name === "help" && !$("#help-body").textContent && S.help.length) openHelp(S.help[0].path);
  if (name === "tenant" && !S.caps && S.tenant) loadCaps(false);
  if (name === "apicalls") loadCalls(); else { clearTimeout(S.callsT); S.callsT = null; }
  if (typeof wbTab === "function") wbTab(name);
}

/* ---------- spec <-> controls ---------- */
function emptySpec() { const e = JSON.parse(JSON.stringify(S.empty)); e.fields = S.defaults.slice(); return e; }
function readSpec() {
  const sp = S.spec;
  sp.dataset = $("#dataset").value.trim();
  const tv = $("#time").value;
  sp.time = tv === "custom" ? {start: $("#t-start").value.trim(), end: $("#t-end").value.trim()} : {last: tv};
  sp.full_documents = $("#fulldocs").checked;
  sp.format = S.fmt;
  sp.storage = $("#storage").value || "auto";
  sp.match = $("#match").value === "any" ? "any" : "all";
  $$(".frow").forEach((row, i) => {
    const f = sp.filters[i]; if (!f) return;
    f.field = $(".ff", row).value.trim(); f.op = $(".fo", row).value; const v = $(".fv", row); f.value = v ? v.value : "";
    const cs = $(".fcs", row), ne = $(".fne", row); f.case_sensitive = cs ? cs.checked : false; f.or_empty = ne ? ne.checked : false;
  });
  return sp;
}
function writeSpec(sp) {
  S.spec = Object.assign(emptySpec(), sp || {});
  const s = S.spec;
  if (!["simple", "kql", "advanced"].includes(s.mode)) s.mode = "simple";   /* never turn "advanced" into another query */
  if (!Array.isArray(s.filters)) s.filters = [];
  if (!Array.isArray(s.any_groups)) s.any_groups = [];
  if (!Array.isArray(s.fields) || !s.fields.length) s.fields = S.defaults.slice();
  if (["ndjson.gz", "ndjson", "csv"].includes(s.format)) { S.fmt = s.format; setSeg("#fmt", S.fmt); }
  setSeg("#mode", s.mode);
  $("#dataset").value = s.dataset || "";
  const t = s.time || {};
  if (t.start || t.end) { $("#time").value = "custom"; $("#t-start").value = t.start || ""; $("#t-end").value = t.end || ""; }
  else $("#time").value = S.presets.includes(t.last) ? t.last : "24h";
  $("#custom-time").hidden = $("#time").value !== "custom";
  if ($("#time").value === "custom") syncPickers();
  $("#fulldocs").checked = !!s.full_documents;
  $("#storage").value = s.storage === "recent" ? "recent" : "auto"; storageHint();
  $("#match").value = s.match === "any" ? "any" : "all";
  $("#savename").value = s.name || "";
  showMode(); renderFilters(); renderFieldChips(); changed();
  S.baseline = dirtyKey();             /* after readSpec/renderFilters have normalised it */
}
/* The search as the user would lose it: a Load over a changed search asks first. */
function dirtyKey() { return JSON.stringify([S.spec, S.fmt]); }
function isDirty() { if (!S.spec) return false; readSpec(); return S.baseline != null && dirtyKey() !== S.baseline; }
/* one wording for the plan, the same in the browser, the guided terminal and the menu. */
function readyText(plan) {
  if (!plan) return "Ready to export.";
  const a = plan.archive;
  if (a) return "Ready to export: " + (plan.upper_bound ? "up to " : "") + fmt(plan.total) + " events from recent data, plus the part before " +
    String(a.end).slice(0, 10) + " from the archive (counted during the export" + (a.minutes ? ", roughly " + fmt(a.minutes) + " minutes" : "") + ").";
  return "Ready to export: " + (plan.upper_bound ? "up to " : "") + fmt(plan.total) + " events in about " + fmt(plan.slices) + (plan.slices === 1 ? " slice." : " slices.");
}
function readyWithTime(plan) { return readyText(plan) + (plan && plan.time_words ? " Estimated time: " + plan.time_words : ""); }
const STORAGE_HINT = {auto: "Recent data, plus the archive for anything older (that part runs as an archive job: minutes to an hour).",
  recent: "Fastest. Older events kept in the archive are not included."};
function storageHint() {
  const v = $("#storage").value, a = S.caps && S.caps.archive === false;
  $("#storage-hint").textContent = STORAGE_HINT[v] + (a && v !== "recent" ? " This tenant has no archive, so only recent data is searched." : "");
}
function setSeg(sel, v) { $$(sel + " button").forEach(b => { const on = b.dataset.v === v; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false"); }); }
function showMode() {
  const m = S.spec.mode;
  if (typeof wbShowMode === "function" && wbShowMode(m)) return;      /* the query tabs (workbench.js) */
  $("#m-simple").hidden = m !== "simple"; $("#m-advanced").hidden = m === "simple";
  if (m === "kql" && S.caps && S.caps.kql) {
    $("#adv-msg").textContent = "This search runs as KQL (available on this tenant): " + (S.spec.kql || "") +
      ". Validate checks it before any export. Choose Start a new search to build one with a dataset and filters instead.";
  } else if (m === "kql") {
    $("#adv-msg").textContent = "This saved search uses KQL, which is not available on this tenant. " +
      "Rebuild it with a dataset and filters: choose Start a new search, or use a search from Abstract to convert it.";
  } else if (m === "advanced") {
    const box = $("#adv-msg"), code = document.createElement("span"); code.className = "mono";
    code.textContent = "export --from-saved \"" + (S.spec.name || "<name>") + "\"";
    box.textContent = "This saved search was made for the command line. Run it there with ";
    box.appendChild(code);
    box.appendChild(document.createTextNode(", or choose Start a new search to build one here."));
  }
}
/* The one fingerprint an export must match: validation passed for exactly this search on this tenant.
   The file format is NOT part of it: it changes how the file is written, never which events match, so switching
   NDJSON.gz / NDJSON / CSV keeps a passed validation. */
function specKey() {
  const s = Object.assign({}, S.spec); delete s.name; delete s.format;
  return JSON.stringify([S.tenant && S.tenant.vendor_account_id, s]);
}
let changedT = null;
function changed() {                                   /* every edit ends here */
  if (!S.spec) return;
  readSpec(); summarizeLocal();
  /* The passed validation is REMEMBERED, not discarded: an edit that is undone (or a format switch, which is not
     part of the key) brings back the same search, and Export is available again without re-validating. */
  const k = specKey();
  if (S.validatedKey || S.zeroKey) {
    if (S.validatedKey === k || S.zeroKey === k) markValidationFresh(); else markValidationStale();
  }
  refreshActions();
  clearTimeout(changedT); changedT = setTimeout(checkProblems, 180);
}
async function checkProblems() {
  const seq = ++S.problemsSeq;
  try {
    const r = await api("problems", {body: {spec: S.spec, format: S.fmt}});
    if (seq !== S.problemsSeq) return;
    S.problems = r.problems || []; renderProblems();
    if (r.describe) $("#summary").textContent = r.describe.query + "   ·   " + r.describe.window;
  } catch (e) { if (!e.network && e.kind !== "token") { S.problems = []; renderProblems(); } }
}
function localProblems() {
  const out = [];
  if (S.qtab === "kql" && S.kqlState && S.kqlState.runs_as === "refused") out.push("This KQL cannot run: " + S.kqlState.reason);
  if (S.qtab === "kql" && !S.kqlState && S.spec && S.spec.mode !== "simple" && !(S.spec.kql || "").trim()) out.push("Write a KQL query, or use the builder.");
  return out;
}
/* A fresh, empty search is not an error: the problems list stays hidden until the search has something in it or the
   person tries Preview, Validate or Export (brand review, item 15). */
function searchIsFresh() {
  const s = S.spec || {};
  return !S.touched && !s.dataset && !(s.filters || []).length && !(s.any_groups || []).length && !String(s.kql || "").trim();
}
function renderProblems() {
  const all = localProblems().concat(S.spec.mode === "advanced" ? [] : S.problems), ul = $("#problems");
  ul.innerHTML = all.map(p => "<li>" + esc(p) + "</li>").join(""); ul.hidden = !all.length || searchIsFresh();
  refreshActions();
}
function summarizeLocal() {
  const s = S.spec;
  $("#title").textContent = s.name || "Search";
  if (s.mode === "advanced") $("#summary").textContent = "advanced condition (saved search, command line only)";
}
function markValidationFresh() {
  const v = $("#v-validate"), ban = $(".vstate", v);
  if (ban && S.validatedReadyHtml) { ban.className = S.validatedReadyClass; ban.innerHTML = S.validatedReadyHtml; }
}
function markValidationStale() {
  const v = $("#v-validate"), ban = $(".vstate", v);
  if (ban) { ban.className = "banner warn vstate"; ban.textContent = "The search changed since this validation. Validate again before exporting."; }
}

/* ---------- action availability ---------- */
function refreshActions() {
  if (!S.spec) return;
  const adv = S.spec.mode === "advanced", probs = localProblems().concat(adv ? [] : S.problems).length > 0;
  const k = specKey(), zeroOk = !!S.zeroKey && S.zeroKey === k;
  /* A validation is honoured for 15 minutes, the same span in which the export reuses the exact window Validate
     checked (abx_core Session._export_window); after that the numbers could drift, so validate again. */
  const fresh = Date.now() - (S.validatedAt || 0) < 15 * 60 * 1000;
  const validated = ((!!S.validatedKey && S.validatedKey === k) || zeroOk) && fresh;
  let why = "";
  if (S.busy) why = "An export is running. Preview, Validate and field learning are paused until it finishes, so they do not slow it down.";
  else if (adv) why = "This saved search runs only from the command line.";
  else if (probs) why = searchIsFresh() ? "Choose a dataset or add a filter to start." : "Fix the items above first.";
  else if (!S.tenant) why = "Connect to your tenant to preview, validate and export.";
  else if (!validated) why = (S.validatedKey === k && !fresh) ? "This validation is more than 15 minutes old. Validate again to enable Export."
                                                              : "Validate this search to enable Export.";
  const set = (sel, off, title) => { const b = $(sel); if (!b || b.dataset.working) return; b.disabled = off; b.title = off ? (title || why) : ""; };
  set("#a-preview", S.busy || adv || probs);
  set("#a-validate", S.busy || adv || probs);
  /* the sidebar Export only opens the Export tab (always available); only Start export starts one. */
  set("#a-export2", S.busy || adv || probs || !validated);
  if (S.qtab === "kql" && S.kqlState && S.kqlState.runs_as === "kql_result") {      /* a result table, not events */
    const t = "This query returns a result table, not events: Preview shows it, and Save result table keeps it.";
    set("#a-validate", true, t); set("#a-export2", true, t);
    why = t;
  }
  $("#a-export").classList.toggle("primary", validated && !S.busy && !adv && !probs);   /* the call to action once it can export */
  set("#b-reprof", S.busy, "An export is running. Field learning is paused until it finishes.");
  set("#b-connect", S.busy, "An export is running. Switching tenant waits until it finishes or is stopped.");
  set("#b-disconnect", S.busy, "An export is running. Disconnecting waits until it finishes or is stopped.");
  $$("[data-resume]").forEach(b => { b.disabled = S.busy; b.title = S.busy ? "An export is already running." : ""; });
  $("#action-note").textContent = why;
  $("#gate-note").textContent = S.busy ? "" : (why || (zeroOk ? "Nothing matched. Start export writes an empty file, as you chose."
    : readyWithTime(S.validation && S.validation.plan)));
  $("#a-stop").hidden = !S.busy;
}

/* ---------- filters ---------- */
function renderFilters() {
  const wrap = $("#filters"); wrap.innerHTML = "";
  const many = S.spec.filters.length > 1; $("#match-row").hidden = !many;
  $("#match-note").textContent = many && $("#match").value === "any" ? "at least one must match" : "all must match";
  S.spec.filters.forEach((f, i) => {
    const type = f.type || "String", ops = S.ops[type] || S.ops.String;
    let opDef = ops.find(o => o.op === f.op);
    if (!opDef) { opDef = ops[0]; f.op = opDef.op; }
    const id = "f" + i, row = document.createElement("div"); row.className = "frow";
    const pend = f.field && S.typePending[f.field];
    row.innerHTML = '<button class="x" type="button" aria-label="Remove this filter">×</button>' +
      '<div class="ftype">' + esc(S.typeLabels[type] || type) + ' field' + (pend ? '<span class="pendtype"> · type checked after the export</span>' : "") + '</div>' +
      '<label class="sr-only" for="' + id + 'f">Field</label><input type="text" class="ff" id="' + id + 'f" list="dl-fields" placeholder="field, for example user_name" autocomplete="off">' +
      '<label class="sr-only" for="' + id + 'o">Condition</label><select class="fo" id="' + id + 'o">' +
      ops.map(o => '<option value="' + esc(o.op) + '">' + esc(o.label) + '</option>').join("") + '</select>' +
      (opDef.list === null ? "" : '<label class="sr-only" for="' + id + 'v">Value</label><input type="text" class="fv" id="' + id + 'v" autocomplete="off" placeholder="' +
        (opDef.list === "range" ? "from, to" : opDef.list ? "values, comma-separated" : "value") + '">') +
      ((S.caseOps || []).includes(f.op) ? '<label class="chk fopt"><input type="checkbox" class="fcs"> Match case</label>' : "") +
      ((S.notOps || []).includes(f.op) ? '<label class="chk fopt"><input type="checkbox" class="fne"> or ' + esc(f.field || "the field") + ' has no value</label>' : "");
    $(".ff", row).value = f.field || ""; $(".fo", row).value = f.op; if ($(".fv", row)) $(".fv", row).value = f.value || "";
    if ($(".fcs", row)) { $(".fcs", row).checked = !!f.case_sensitive; $(".fcs", row).addEventListener("change", () => { readSpec(); changed(); }); }
    if ($(".fne", row)) { $(".fne", row).checked = !!f.or_empty; $(".fne", row).addEventListener("change", () => { readSpec(); changed(); }); }
    $(".x", row).addEventListener("click", () => { readSpec(); S.spec.filters.splice(i, 1); renderFilters(); changed(); });
    $(".ff", row).addEventListener("change", async e => {
      readSpec(); const name = e.target.value.trim(); changed(); if (!name) return;
      if (S.fields.length && !S.fields.includes(name) && !isExt(name)) { toast("'" + name + "' is not a field on your tenant", true); return; }
      if (!S.tenant) return;
      if (!S.types[name] && S.busy) {                 /* a type lookup is a search; wait for the export */
        S.typePending[name] = true; renderFilters();
        toast("An export is running. This field's type is checked when it finishes."); return; }
      try { await lookupType(name, i); }
      catch (err) { if (err.kind === "busy") { S.typePending[name] = true; renderFilters(); } else toast(err.message, true); }
    });
    $(".fo", row).addEventListener("change", () => { readSpec(); renderFilters(); changed(); });
    if ($(".fv", row)) $(".fv", row).addEventListener("input", changed);
    wrap.appendChild(row);
  });
  (S.spec.any_groups || []).forEach((g, gi) => {            /* "at least one of" groups, from a search copied from Abstract */
    const row = document.createElement("div"); row.className = "frow fgroup";
    const words = g.map(f => { const o = (S.ops[f.type || "String"] || S.ops.String).find(x => x.op === f.op);
      return (f.field || "") + " " + (o ? o.label : f.op) + (f.value ? ' "' + f.value + '"' : "") + (f.or_empty ? " (or no value)" : ""); });
    row.innerHTML = '<button class="x" type="button" aria-label="Remove this group">×</button><div class="ftype">at least one of</div><div class="gtext"></div>';
    $(".gtext", row).textContent = words.join("  OR  ");
    $(".x", row).addEventListener("click", () => { readSpec(); S.spec.any_groups.splice(gi, 1); renderFilters(); changed(); });
    wrap.appendChild(row);
  });
}

async function lookupType(name, i) {
  const t = S.types[name] || (await api("field-type?field=" + encodeURIComponent(name))).type;
  S.types[name] = t; delete S.typePending[name];
  const f = S.spec.filters[i];
  if (f && f.field === name && f.type !== t) { f.type = t; f.op = ""; }
  renderFilters(); changed();
}
/* After an export: the lookups and the dataset list that waited for it. */
async function resumePaused() {
  const names = Object.keys(S.typePending);
  for (const name of names) {
    const i = S.spec.filters.findIndex(f => f.field === name);
    if (i < 0) { delete S.typePending[name]; continue; }
    try { await lookupType(name, i); } catch (e) { if (e.kind === "busy") return; delete S.typePending[name]; toast(e.message, true); }
  }
  if (names.length) renderFilters();
  if (S.catalogPending && S.tenant) { S.catalogPending = false; loadDatasets(); }
}

/* ---------- fields ---------- */
function renderFieldChips() {
  const w = $("#fchips"); w.innerHTML = "";
  if (S.spec.full_documents) { w.innerHTML = '<span class="fchip fixed">every field of every event</span>'; return; }
  S.spec.fields.forEach((f, i) => {
    const fixed = f === "@timestamp" || f === "id", c = document.createElement("span"); c.className = "fchip" + (fixed ? " fixed" : "");
    c.appendChild(document.createTextNode(visible(f)));
    if (!fixed) { const b = document.createElement("button"); b.type = "button"; b.textContent = "×"; b.setAttribute("aria-label", "Remove field " + f);
      b.addEventListener("click", () => { S.spec.fields.splice(i, 1); renderFieldChips(); renderFieldGrid(); changed(); }); c.appendChild(b); }
    w.appendChild(c);
  });
}
function isExt(name) { return name.startsWith("ext.") && name.length > 4; }
function addField(name) {
  name = (name || "").trim(); if (!name) return;
  if (S.fields.length && !S.fields.includes(name) && !isExt(name)) { toast("'" + name + "' is not a field on your tenant", true); return; }
  if (!S.spec.fields.includes(name)) S.spec.fields.push(name);
  S.spec.full_documents = false; $("#fulldocs").checked = false;
  renderFieldChips(); renderFieldGrid(); changed();
}
function fieldPool() {
  if (S.prof && !$("#fall").checked) return S.prof.fields.map(f => ({name: f.name, cov: f.coverage}));
  return S.fields.map(f => ({name: f, cov: null}));
}
const GRID_CAP = 600;
function renderFieldGrid() {
  const q = $("#fsearch").value.toLowerCase(), g = $("#fgrid"); g.innerHTML = "";
  if (!S.tenant) { g.innerHTML = '<div class="empty">Connect to load your tenant\'s field catalog.</div>'; $("#fcount").textContent = ""; return; }
  const pool = fieldPool(), hits = pool.filter(f => f.name.toLowerCase().includes(q));
  $("#fcount").textContent = (S.prof && !$("#fall").checked
      ? hits.length + " fields seen in " + fmt(S.prof.sampled) + " sampled " + S.prof.dataset + " events (percent = how often each is populated)"
      : hits.length + " of " + S.fields.length + " tenant fields") +
    (hits.length > GRID_CAP ? ", showing " + fmt(GRID_CAP) + " of " + fmt(hits.length) + ". Search to narrow the list" : "") +
    ". Choose one to add it to the export.";
  hits.slice(0, GRID_CAP).forEach(f => {
    const b = document.createElement("button"); b.type = "button"; b.title = visible(f.name);
    b.textContent = visible(f.name) + (f.cov == null ? "" : "  ·  " + (f.cov >= 99.95 ? "100" : Number(f.cov).toFixed(f.cov < 10 ? 1 : 0)) + "%");
    if (S.spec.fields.includes(f.name)) { b.className = "in"; b.setAttribute("aria-pressed", "true"); }
    b.addEventListener("click", () => addField(f.name)); g.appendChild(b);
  });
}
function setDatalist() {
  const names = S.prof ? Array.from(new Set(S.prof.fields.map(f => f.name).concat(S.fields))) : S.fields;
  $("#dl-fields").innerHTML = names.map(x => '<option value="' + esc(x) + '">').join("");
}
const SRC = {"learned this session": "learned earlier in this session (kept in memory only)", learned: "learned just now (kept in memory only)"};
async function loadProfile(refresh) {
  const ds = $("#dataset").value.trim(), hint = $("#prof-hint");
  const known = S.datasets.find(d => d.name.toLowerCase() === ds.toLowerCase());
  if (!S.tenant || !ds || !known) { S.prof = null; setDatalist(); hint.textContent = ""; $("#prof-actions").hidden = true; renderFieldGrid(); return; }
  hint.innerHTML = '<span class="spin" aria-hidden="true"></span>' + (refresh ? "Sampling recent " + esc(known.name) + " events to learn its fields (10–30 seconds)…" : "Loading fields…");
  const btn = $("#b-reprof"); if (refresh) working(btn, true, "Learning");
  try {
    const p = await api("dataset-fields?dataset=" + encodeURIComponent(known.name) + (refresh ? "&refresh=1" : ""));
    if ($("#dataset").value.trim().toLowerCase() !== ds.toLowerCase()) return;     /* the user moved on */
    $("#prof-actions").hidden = false;
    if (p.none) { S.prof = null; $("#b-suggest").hidden = true; $("#b-allseen").hidden = true; btn.dataset.l = "Learn fields"; if (!refresh) btn.textContent = "Learn fields";
      hint.textContent = "No field profile for this dataset yet, so all tenant fields are shown. Learn fields samples recent events (10–30 seconds)."; }
    else if (!p.sampled) { S.prof = null; $("#b-suggest").hidden = true; $("#b-allseen").hidden = true; btn.dataset.l = "Try again"; if (!refresh) btn.textContent = "Try again";
      hint.textContent = "No events from this dataset in the last 30 days, so there is nothing to learn fields from. All tenant fields are shown."; }
    else { S.prof = p; $("#b-suggest").hidden = false; $("#b-allseen").hidden = false; btn.dataset.l = "Re-learn"; if (!refresh) btn.textContent = "Re-learn";
      const when = p.profiled ? " " + String(p.profiled).slice(0, 10) : "";
      hint.textContent = p.fields.length + " fields used by this dataset. Source: " + (SRC[p.source] || "profile") + when +
        ", from " + fmt(p.sampled) + " sampled events."; }
    setDatalist(); renderFieldGrid();
  } catch (e) {
    S.prof = null; setDatalist();
    hint.textContent = (e.kind === "busy" ? "" : "Could not load this dataset's fields: ") + e.message;
  } finally { if (refresh) working(btn, false); }
}

/* ---------- connection ---------- */
function renderChips() {
  const c = $("#chips");
  if (!S.tenant) { c.innerHTML = '<span class="chip off">Not connected</span>'; $("#b-connect").textContent = "Connect"; $("#b-connect").classList.add("primary"); $("#b-disconnect").hidden = true; return; }
  const t = S.tenant;
  c.innerHTML = '<span class="chip ok"><b>' + esc(t.name) + '</b>' + esc(t.vendor_account_id) + '</span>' +
    (t.production ? '<span class="chip prod">production</span>' : '<span class="chip">' + esc(String(t.base_url || "").replace("https://", "")) + '</span>');
  $("#b-connect").textContent = "Switch tenant"; $("#b-connect").classList.remove("primary"); $("#b-disconnect").hidden = false;   /* connected: a plain button, not the call to action */
}
/* ---------- use a search from Abstract (saved view or pasted search box text) ---------- */
let impResult = null;
function openImport() {
  if (!S.tenant) { toast("Connect to your tenant first.", true); return; }
  if (S.busy) { toast("An export is running. Converting a search waits until it finishes.", true); return; }
  modalReturn = document.activeElement; impResult = null;
  $("#imp").hidden = false; $("#i-err").textContent = ""; $("#i-out").innerHTML = ""; $("#i-use").disabled = true;
  $("#i-text").focus();
}
function closeImport() { $("#imp").hidden = true; impResult = null; if (modalReturn && modalReturn.focus) modalReturn.focus(); }
async function loadViews() {
  const b = $("#i-load"); working(b, true, "Loading");
  try {
    const r = await api("views");
    const vs = r.views || [];
    S.views = vs;
    const KIND = {kql: "KQL search", filters: "Filter builder", replay: "Replay", none: "Columns and time range only", unreadable: "Could not be read"};
    const groups = ["kql", "filters", "replay", "none", "unreadable"].map(k => [k, vs.filter(v => (v.origin || "kql") === k)]).filter(g => g[1].length);
    $("#i-view").innerHTML = '<option value="">' + (vs.length ? "Choose a saved view (" + vs.length + ")" : "No saved views on this tenant") + "</option>" +
      groups.map(([k, list]) => '<optgroup label="' + esc(KIND[k]) + " (" + list.length + ')">' +
        list.map(v => '<option value="' + esc(v.id) + '">' + esc(v.name) + "</option>").join("") + "</optgroup>").join("");
    const n = k => vs.filter(v => (v.origin || "kql") === k).length;
    $("#i-vcount").textContent = vs.length + " saved views, read live from this tenant: " + n("kql") + " with a KQL search, " +
      n("filters") + " built with the filter builder, " + (n("replay") ? n("replay") + " replay views, " : "") + n("none") + " with only columns and a time range" +
      (n("unreadable") ? ", " + n("unreadable") + " could not be read" : "") + ". Every one can be loaded; one with no filter needs a dataset or a filter added.";
    $("#i-checkall").hidden = !vs.length; $("#i-sync").hidden = !vs.length; $("#i-checks").innerHTML = "";
  } catch (e) { $("#i-err").textContent = e.message; }
  finally { working(b, false); }
}
/* Convert every saved view the way an export would, without running any: which ones can be used, and why not. */
async function checkAllViews() {
  const vs = S.views || [], b = $("#i-checkall"), out = $("#i-checks"); if (!vs.length) return;
  working(b, true, "Checking"); out.innerHTML = "";
  const rows = [];
  try {
    for (let i = 0; i < vs.length; i++) {
      $("#i-vcount").textContent = "Checking " + (i + 1) + " of " + vs.length + ": " + vs[i].name;
      try { const c = await api("import", {body: {view_id: vs[i].id}}); rows.push({v: vs[i], ok: !c.columns_only, cols: !!c.columns_only}); }
      catch (e) { if (e.kind === "busy" || e.network) throw e; rows.push({v: vs[i], ok: false, why: e.message}); }
    }
    const ok = rows.filter(r => r.ok).length, cols = rows.filter(r => r.cols).length;
    const bad = vs.length - ok - cols;
    $("#i-vcount").textContent = ok + " of " + vs.length + " saved views are ready to export" +
      (cols ? "; " + cols + (cols === 1 ? " holds" : " hold") + " only columns and a time range (load it and add a dataset or a filter)" : "") +
      (bad ? "; " + bad + " cannot be converted" : "") + ". Pick one to load it.";
    out.innerHTML = '<div class="tbl-wrap"><table><tbody>' + rows.map(r => "<tr><td>" + (r.ok || r.cols ? '<button class="tbtn" type="button" data-vpick="' +
      esc(r.v.id) + '">' + (r.ok ? "ready" : "columns only") + "</button>" : "cannot convert") +
      "</td><td>" + esc(r.v.name) + "</td><td>" + esc(r.ok ? ({kql: "KQL", filters: "filter builder", replay: "replay"}[r.v.origin] || "") : r.cols ? "add a dataset or a filter after loading" : r.why) + "</td></tr>").join("") + "</tbody></table></div>";
  } catch (e) { $("#i-err").textContent = e.message; }
  finally { working(b, false); }
}
/* Learn the fields of every dataset, one at a time, with a Stop button. Memory only, like Learn fields. */
let learnStop = false;
async function learnAll() {
  if (!S.tenant) { toast("Connect to your tenant first.", true); return; }
  if (S.busy) { toast("An export is running. Field learning is paused until it finishes.", true); return; }
  const b = $("#b-learnall"), st = $("#learnall-status"), out = $("#learnall-out");
  learnStop = false; working(b, true, "Learning"); $("#b-learnstop").hidden = false; out.innerHTML = "";
  const rows = [];
  try {
    if (!S.datasets.length) S.datasets = (await api("datasets")).datasets || [];
    const ds = S.datasets;
    for (let i = 0; i < ds.length && !learnStop; i++) {
      st.textContent = "Learning " + (i + 1) + " of " + ds.length + ": " + ds[i].name + "…";
      try { const p = await api("dataset-fields?dataset=" + encodeURIComponent(ds[i].name) + "&refresh=1");
        rows.push({name: ds[i].name, n: (p.fields || []).length, sampled: p.sampled || 0}); }
      catch (e) { if (e.kind === "busy" || e.network) throw e; rows.push({name: ds[i].name, err: e.message}); }
      out.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>Dataset</th><th>Fields</th><th>Sampled events</th></tr></thead><tbody>' +
        rows.map(r => "<tr><td>" + esc(r.name) + "</td><td>" + (r.err ? esc(r.err) : fmt(r.n)) + "</td><td>" +
          (r.err ? "" : r.sampled ? fmt(r.sampled) : "no events in 30 days") + "</td></tr>").join("") + "</tbody></table></div>";
    }
    st.textContent = (learnStop ? "Stopped after " + rows.length + " of " + ds.length + " datasets. " : "Learned the fields of " + rows.length + " datasets. ") +
      "Pick a dataset on the left to see its fields with coverage.";
    loadProfile(false);
  } catch (e) { st.textContent = e.message; }
  finally { working(b, false); $("#b-learnstop").hidden = true; }
}
async function doConvert() {
  const view = $("#i-view").value, text = $("#i-text").value.trim();
  $("#i-err").textContent = ""; $("#i-out").innerHTML = ""; $("#i-use").disabled = true; impResult = null;
  if (!view && !text) { $("#i-err").textContent = "Pick a saved view or paste a search first."; return; }
  const b = $("#i-convert"); working(b, true, "Converting");
  try {
    const r = await api("import", {body: view ? {view_id: view} : {text: text}});
    if ($("#i-view").value !== view || (!view && $("#i-text").value.trim() !== text)) return;   /* changed meanwhile */
    impResult = r;
    $("#i-out").innerHTML = '<div class="hint">This will run as (' + esc(r.source) + "):</div><ul>" +
      r.readable.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>" +
      (r.notes || []).map(n => '<div class="note">Note: ' + esc(n) + "</div>").join("");
    $("#i-use").disabled = false;
  } catch (e) { $("#i-err").textContent = e.message; }
  finally { working(b, false); }
}
function useImport() {
  if (!impResult) return;
  S.validatedKey = null; S.zeroKey = null;
  const sp = impResult.spec, cols = impResult.columns_only; writeSpec(sp);
  closeImport(); if (S.tenant) loadProfile(false);
  toast(cols ? "Loaded the view's columns and time range. Add a dataset or a filter, then Preview." : "Search converted. Choose Preview or Validate to check it.");
}
/* Save every saved view that loads as a saved search on this computer (conditions only, never events). */
async function syncViews() {
  const b = $("#i-sync"); working(b, true, "Saving");
  try {
    const r = await api("views-sync", {body: {}});
    $("#i-vcount").textContent = "Saved " + r.count + " views as saved searches on this computer. They are under Saved searches, and run with export --from-saved.";
    loadSaved();
  } catch (e) { $("#i-err").textContent = e.message; }
  finally { working(b, false); }
}
let modalReturn = null;
function openConnect() {
  if (S.busy) { toast("An export is running. Switching tenant waits until it finishes or is stopped.", true); return; }
  modalReturn = document.activeElement;
  $("#modal").hidden = false; $("#c-err").textContent = "";
  const st = S.settings || {}; $("#c-base").value = st.base_url || S.prodUrl || ""; if (st.vendor_account_id) $("#c-va").value = st.vendor_account_id;
  ($("#c-va").value ? $("#c-key") : $("#c-va")).focus();
}
function closeConnect() { $("#modal").hidden = true; $("#c-key").value = ""; if (modalReturn && modalReturn.focus) modalReturn.focus(); }
async function doConnect() {
  const b = $("#c-go"); working(b, true, "Connecting");
  try {
    const r = await api("connect", {body: {base_url: $("#c-base").value, vendor_account_id: $("#c-va").value, api_key: $("#c-key").value}});
    $("#c-key").value = ""; S.tenant = r.tenant; S.caps = null; S.validatedKey = null; S.zeroKey = null; S.types = {}; S.typePending = {};
    S.settings = {base_url: r.tenant.base_url, vendor_account_id: r.tenant.vendor_account_id};
    closeConnect(); renderChips(); toast("Connected to " + r.tenant.name);
    await loadCatalog(); changed();
  } catch (e) { $("#c-err").textContent = e.message; }
  finally { working(b, false); }
}
async function doDisconnect() {
  try { await api("disconnect", {body: {}}); } catch (e) { toast(e.message, true); return; }
  S.tenant = null; S.fields = []; S.datasets = []; S.prof = null; S.types = {}; S.validatedKey = null; S.caps = null;
  $("#caps-out").textContent = "";
  S.zeroKey = null; S.typePending = {}; S.catalogPending = false;
  setDatalist(); $("#dl-datasets").innerHTML = ""; $("#prof-hint").textContent = ""; $("#prof-actions").hidden = true;
  renderChips(); renderFieldGrid(); changed(); toast("Disconnected. The API key was removed from memory.");
}
/* The current search as a command line (for scheduling), copied to the clipboard when the browser allows it. */
async function copyCommand() {
  if (!S.spec) return; readSpec();
  try {
    const r = await api("command", {body: {spec: S.spec, format: S.fmt}});
    const text = (r.env ? r.env + "\n" : "") + r.command;
    const pre = $("#cmd-out"); pre.textContent = text; pre.hidden = false; $("#cmd-note").textContent = r.note;
    try { await navigator.clipboard.writeText(text); toast("Copied the command line."); }
    catch (e) { toast("Select the command below and copy it."); }
  } catch (e) { toast(e.message, true); }
}
/* API calls: what this app sent to Abstract's API. The server keeps method, path, request body (secrets redacted),
   status and time; never headers or what came back. curl is built here with the key as a variable for each shell. */
S.calls = []; S.callsFloor = 0; S.callsLast = 0;
async function loadCalls() {
  clearTimeout(S.callsT); S.callsT = null;
  try {
    const r = await api("api-calls?since=" + S.callsLast);
    S.callsInfo = r; S.callsLast = r.last;
    S.calls = S.calls.concat(r.calls.filter(c => c.n > S.callsFloor)).slice(-500);
    $("#ac-log").textContent = "Errors and failed calls are also written to " + r.log_file;
    renderCalls();
  } catch (e) { $("#ac-out").textContent = e.message; }
  if ($("#v-apicalls").classList.contains("on")) S.callsT = setTimeout(loadCalls, 2000);
}
function curlFor(c, shell) {
  const info = S.callsInfo || {}, url = (info.base_url || "") + c.path;
  const key = shell === "ps" ? "$env:ABSTRACT_API_KEY" : shell === "cmd" ? "%ABSTRACT_API_KEY%" : "$ABSTRACT_API_KEY";
  const exe = shell === "ps" ? "curl.exe" : "curl";
  const parts = [exe + " -sS -X " + c.method + ' "' + url + '"', '-H "Authorization: Bearer ' + key + '"',
    '-H "X-AS-Vendor-Account-ID: ' + (info.vendor_account_id || "") + '"'];
  if (c.body) parts.push('-H "Content-Type: application/json"', shell === "ps" ? '--data "@request.json"' : "--data @request.json");
  return parts.join(shell === "sh" ? " \\\n  " : shell === "ps" ? " `\n  " : " ^\n  ");
}
function renderCalls() {
  const out = $("#ac-out"), shell = $("#ac-shell").value;
  if (!S.calls.length) { out.innerHTML = '<div class="empty">No calls yet. Connect, preview, validate or export, and each call to the API appears here.</div>'; return; }
  const open = new Set($$("#ac-out details[open]").map(d => d.dataset.n));
  if (S.openCall) { open.add(String(S.openCall)); S.openCall = null; }
  out.innerHTML = S.calls.slice().reverse().map(c => {
    const ok = c.status && c.status < 400, meant = !ok && c.status && /^expected:/.test(c.note || "");
    let pretty = c.body;
    try { pretty = c.body ? JSON.stringify(JSON.parse(c.body), null, 2) : ""; } catch (e) { /* truncated: shown as sent */ }
    return '<details class="call" data-n="' + c.n + '"' + (open.has(String(c.n)) ? " open" : "") + '><summary><span class="mono muted">' +
      esc(c.t.slice(11, 19)) + '</span> <b class="mono">' + esc(c.method) + '</b> <span class="mono">' + esc(c.path) + '</span> <span class="pill ' +
      (ok ? "PASS" : meant ? "INFO" : "FAIL") + '">' + esc(c.status || "no response") + (meant ? " (expected)" : "") + '</span> <span class="mono muted">' + esc(c.ms) + " ms" +
      (c.attempt > 1 ? " · attempt " + esc(c.attempt) : "") + '</span></summary>' +
      (c.note ? '<div class="hint">' + esc(c.note) + '</div>' : "") +
      (c.headers ? '<div class="f">Headers sent (key masked)</div><pre class="console">' +
        esc(Object.entries(c.headers).map(([k, v]) => k + ": " + v).join("\n")) + '</pre>' : "") +
      (pretty ? '<div class="f">Request body</div><pre class="console">' + esc(pretty) + '</pre>' : "") +
      '<div class="f">Run it yourself</div><pre class="console">' + esc(curlFor(c, shell)) + '</pre>' +
      '<div class="row gap"><button class="tbtn" type="button" data-copy="' + c.n + '">Copy curl</button>' +
      (c.body ? '<button class="tbtn" type="button" data-save="' + c.n + '">Download request.json</button>' : "") + '</div>' +
      (c.body ? '<div class="hint">Save request.json in the folder where you run curl. Set ABSTRACT_API_KEY first, as in Part 3 of the guide.</div>' : "") +
      '</details>';
  }).join("");
}
$("#ac-shell").addEventListener("change", renderCalls);

/* The live feed under every tab: each API call as it happens, whatever you are doing (connect, lists, preview,
   validate, export). The key is never part of it: the server sends the headers already masked. */
S.feedLast = 0; S.feedLines = [];
function feedOpen() { try { return localStorage.getItem("abx-feed") !== "closed"; } catch (e) { return true; } }
function feedShow(open) {
  $("#lf-list").hidden = !open; $("#lf-toggle").textContent = open ? "Hide" : "Show";
  $("#lf-toggle").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("abx-feed", open ? "open" : "closed"); } catch (e) { /* a preference only */ }
}
async function feedPoll() {
  clearTimeout(S.feedT);
  if (S.token && !document.hidden) {
    try {
      const r = await api("api-calls?since=" + S.feedLast);
      S.feedLast = r.last; S.callsInfo = S.callsInfo || r;
      for (const c of r.calls) S.feedLines.push(c);
      S.feedLines = S.feedLines.slice(-8);
      const n = r.last; $("#lf-note").textContent = n ? " · " + fmt(n) + " so far · Authorization: Bearer ******** (masked)" : "";
      const list = $("#lf-list");
      if (!S.feedLines.length) list.textContent = "Each call to Abstract's API appears here as it happens.";
      else list.innerHTML = S.feedLines.slice().reverse().map(c => {          /* newest first */
        const ok = c.status && c.status < 400, meant = !ok && c.status && /^expected:/.test(c.note || "");
        return '<button type="button" class="lf-row" data-n="' + c.n + '"><span class="muted">' + esc(c.t.slice(11, 19)) + "</span> " +
          esc(c.method) + " " + esc(c.path) + ' <span class="' + (ok ? "lf-ok" : meant ? "lf-info" : "lf-bad") + '">' +
          esc(c.status || "no response") + (meant ? " expected" : "") + '</span> <span class="muted">' + esc(c.ms) + " ms</span></button>";
      }).join("");
    } catch (e) { /* the heartbeat reports a stopped app */ }
  }
  S.feedT = setTimeout(feedPoll, 1200);
}
$("#lf-toggle").addEventListener("click", () => feedShow($("#lf-list").hidden));
$("#lf-all").addEventListener("click", () => tab("apicalls"));
$("#lf-list").addEventListener("click", e => {
  const b = e.target.closest(".lf-row"); if (!b) return;
  S.openCall = b.dataset.n; tab("apicalls");
});
$("#ac-clear").addEventListener("click", () => { S.callsFloor = S.callsLast; S.calls = []; renderCalls(); });
$("#ac-out").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  const c = S.calls.find(x => String(x.n) === (b.dataset.copy || b.dataset.save)); if (!c) return;
  if (b.dataset.copy) {
    try { await navigator.clipboard.writeText(curlFor(c, $("#ac-shell").value)); toast("Copied the curl command."); }
    catch (err) { toast("Select the command and copy it."); }
  } else {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([c.body], {type: "application/json"})); a.download = "request.json";
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
});

/* What this tenant supports, checked live by the app (KQL, archive, complete events). Memory only. */
async function loadCaps(refresh) {
  const out = $("#caps-out"), b = $("#b-caps");
  if (!S.tenant) { out.textContent = "Connect to see what this tenant supports."; return; }
  out.innerHTML = '<span class="spin" aria-hidden="true"></span>Checking this tenant…'; if (refresh) working(b, true, "Checking");
  try {
    S.caps = await api("capabilities" + (refresh ? "?refresh=1" : ""));
    out.innerHTML = '<div class="tbl-wrap"><table><tbody>' + S.caps.items.map(i => "<tr><td>" + esc(i.label) + "</td><td>" +
      (i.status === "available" ? '<span class="chip ok">available</span>' : '<span class="chip">' + esc(i.status) + "</span>") +
      "</td><td>" + esc(i.detail) + "</td></tr>").join("") + "</tbody></table></div>";
    showMode();
  } catch (e) { out.textContent = (e.kind === "busy" ? "" : "Could not check the tenant: ") + e.message; }
  finally { if (refresh) working(b, false); }
}
async function loadCatalog() {
  loadCaps(false);
  try { const f = await api("fields"); S.fields = f.fields; setDatalist(); renderFieldGrid(); }
  catch (e) { toast("Could not load fields: " + e.message, true); }
  await loadDatasets();
}
async function loadDatasets() {
  $("#ds-hint").innerHTML = '<span class="spin" aria-hidden="true"></span>Loading datasets…';
  try {
    const d = await api("datasets"); S.datasets = d.datasets;
    $("#dl-datasets").innerHTML = S.datasets.map(x => '<option value="' + esc(x.name) + '">' +
      (x.events_24h != null ? esc(fmt(x.events_24h) + " events in 24h") : "") + '</option>').join("");
    $("#ds-hint").textContent = S.datasets.length + " datasets on this tenant. Start typing to pick one.";
    loadProfile(false);
  } catch (e) {
    if (e.kind === "busy") { S.catalogPending = true;          /* loads after the export */
      $("#ds-hint").textContent = "An export is running, so the dataset list loads when it finishes. You can type the name meanwhile."; }
    else $("#ds-hint").textContent = "Type the dataset (configuration) name. " + e.message;
  }
}

/* ---------- preview ---------- */
function need() { if (!S.tenant) { openConnect(); return false; } readSpec(); return true; }
function flatten(o, p, out) {                        /* keys are attacker-chosen: no prototype */
  out = out || Object.create(null); p = p || "";
  if (o && typeof o === "object" && !Array.isArray(o)) Object.keys(o).forEach(k => flatten(o[k], p + k + ".", out));
  else out[p.slice(0, -1)] = o;
  return out;
}
function cell(v) { return v == null ? "" : (typeof v === "object" ? JSON.stringify(v) : String(v)); }
function short(s, n) { s = String(s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

async function doPreview() {
  S.touched = true; renderProblems();
  if (typeof wbPreview === "function" && wbPreview()) return;       /* a KQL result table (workbench.js) */
  if (!need()) return; tab("preview"); const v = $("#v-preview"), b = $("#a-preview");
  working(b, true, "Searching"); v.innerHTML = '<div class="empty"><span class="spin" aria-hidden="true"></span>Searching…</div>';
  try {
    const r = await api("preview", {body: {spec: S.spec}});
    const cols = (r.columns || []).slice(0, 30);
    v.innerHTML = '<div class="stat"><div><b>' + esc(fmt(r.total)) + '</b><span>events match</span></div>' +
      '<div><b>' + esc(r.seconds) + 's</b><span>query time</span></div>' +
      '<div><b>' + esc(r.rows.length) + '</b><span>shown</span></div></div>' +
      '<div class="hint mono">' + esc(r.window[0] + " → " + r.window[1]) + '. Preview is a sample of at most 50 events, not the export.</div>' +
      (r.rows.length ? '<div class="tbl-wrap"><table><thead><tr>' + cols.map(c => '<th scope="col">' + esc(c) + '</th>').join("") +
        '</tr></thead><tbody>' + r.rows.map(row => { const f = flatten(row);
          return '<tr>' + cols.map(c => { const x = cell(f[c]); return '<td title="' + esc(short(x, 2000)) + '">' + esc(short(x, 300)) + '</td>'; }).join("") + '</tr>'; }).join("") +
        '</tbody></table></div>'
      : '<div class="empty">No events match in this window' + (r.zero_confirmed ? ", and a second search method also counted zero." : ".") +
        ' A zero is a claim: widen the time range and run Validate, which counts a second way, before treating it as an absence.</div>');
  } catch (e) { v.innerHTML = '<div class="banner bad">' + esc(e.message) + '</div>'; }
  finally { working(b, false); }
}

/* ---------- validate ---------- */
async function doValidate() {
  S.touched = true; renderProblems();
  if (!need()) return; tab("validate"); const v = $("#v-validate"), b = $("#a-validate");
  const key = specKey();
  working(b, true, "Validating"); S.validating = true; S.zeroKey = null;
  v.innerHTML = '<div class="empty" role="status"><span class="spin" aria-hidden="true"></span>Running every pre-export check: fields, filter proof, the cross-check and the export plan…' +
    '<div class="row gap center"><button class="tbtn danger" type="button" id="a-vstop">Stop validating</button></div></div>';
  try {
    const r = await api("validate", {body: {spec: S.spec, format: S.fmt}});
    if (r.cancelled) {                                  /* only the validate's own answer says so */
      S.validatedKey = null; S.validation = null;
      v.innerHTML = '<div class="banner warn vstate">Validation stopped. Nothing was checked to the end, and nothing was exported. Validate again when you are ready.</div>';
      announce("Validation stopped."); return;
    }
    S.validation = r;
    const counts = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}; (r.checks || []).forEach(c => { counts[c.status] = (counts[c.status] || 0) + 1; });
    const still = key === specKey();
    const total = r.plan ? r.plan.total : null;
    const ready = r.ready != null ? !!r.ready : !!(r.ok && total > 0);        /* gate on ready, not ok */
    const zero = !!r.ok && total === 0;
    let state;
    if (!r.ok) state = '<div class="banner bad vstate">Fix the failed checks before exporting.</div>';
    else if (!still) state = '<div class="banner warn vstate">The search changed while it was being validated. Validate again before exporting.</div>';
    else if (ready) state = '<div class="banner ok vstate">' + esc(readyWithTime(r.plan)) + '</div>';
    else if (zero) state = '<div class="banner warn vstate"><div>Nothing matched: 0 events in this window' +
        esc(r.zero_confirmed_by ? ", and a second method (" + r.zero_confirmed_by + ") also counted zero." : ". A second method did not confirm the zero.") +
        ' A zero is a claim: widen the time range, or check the dataset and filters, before treating it as an absence.</div>' +
        '<div class="row gap"><button class="tbtn" type="button" data-zero="1">Export anyway (empty file)</button></div></div>';
    else state = '<div class="banner warn vstate">Not ready to export. Check the warnings above.</div>';
    v.innerHTML = '<div class="hint">' + esc(counts.PASS + " passed · " + counts.WARN + " warnings · " + counts.FAIL + " failed · " + counts.INFO + " notes") +
      (r.window ? " · " + esc(r.window[0] + " → " + r.window[1]) : "") + '</div>' +
      '<ul class="checks">' + (r.checks || []).map(c => '<li class="check"><span class="pill ' + esc(c.status) + '">' + esc(c.status) + '</span><div><b>' +
        esc(c.name) + '</b><span>' + esc(c.detail) + '</span></div></li>').join("") + '</ul>' + state;
    S.validatedKey = (ready && still) ? key : null; S.validatedAt = Date.now();
    const vb = $(".vstate", v); S.validatedReadyClass = vb ? vb.className : ""; S.validatedReadyHtml = vb ? vb.innerHTML : "";
    S.zeroCandidate = (zero && still) ? key : null;
    announce(ready ? "Validation passed." : zero ? "Validation found nothing matched." : "Validation found problems.");
  } catch (e) { S.validatedKey = null; v.innerHTML = '<div class="banner bad">' + esc(e.message) + '</div>'; }
  finally { S.validating = false; working(b, false); refreshActions(); }
}
async function stopValidate(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
  try { const r = await api("validate-cancel", {body: {}});
    if (r.supported === false) { toast("This version of the tool cannot stop a validation. It finishes on its own.", true); if (btn) btn.hidden = true; } }
  catch (e) { toast(e.message, true); if (btn) { btn.disabled = false; btn.textContent = "Stop validating"; } }
}
function exportAnyway() {                               /* an empty export only by an explicit choice */
  if (!S.zeroCandidate || S.zeroCandidate !== specKey()) { toast("The search changed. Validate again first.", true); return; }
  S.zeroKey = S.zeroCandidate; refreshActions(); tab("export");
  toast("Nothing matched. Start export writes an empty file.");
}

/* ---------- export + job ---------- */
async function doExport() {
  if (!need()) return;
  const k = specKey();
  if ((S.validatedKey !== k && S.zeroKey !== k) || Date.now() - (S.validatedAt || 0) >= 15 * 60 * 1000) { toast("Validate this search first. Export is enabled once it passes.", true); tab("validate"); return; }
  tab("export");
  try { const r = await api("export", {body: {spec: S.spec, options: {format: S.fmt, merge: $("#merge").checked}}});
        S.jobKey = ""; renderJob(r.job); poll(true); }
  catch (e) { toast(e.message, true); }
}
async function doResume(runDir) {
  try { const r = await api("resume", {body: {run_dir: runDir}}); tab("export"); S.jobKey = ""; renderJob(r.job); poll(true); }
  catch (e) { toast(e.message, true); }
}
async function refreshJob() { try { const r = await api("job"); renderJob(r.job); } catch (e) { /* the heartbeat reports it */ } }
function poll(now) {
  clearTimeout(S.pollT);
  S.pollT = setTimeout(async () => {
    try {
      const r = await api("job");
      if (S.fails >= 3) sysBanner(null); S.fails = 0;
      const was = S.busy; renderJob(r.job);
      if (was && !S.busy) loadExports(true);
    } catch (e) {
      if (e.network) { S.fails++; if (S.fails >= 3) sysBanner("bad",
        "<b>The app has stopped.</b> Its terminal window was probably closed, or the computer slept. Start it again with " +
        '<span class="mono">python3 abstract_search.py --web</span> and open the new link. An unfinished export resumes from the Export history tab where it left off.'); }
      else if (e.kind === "token") return;
    }
    poll(false);
  }, now ? 50 : (S.busy ? 1500 : 10000));
}
const STAGE = s => { s = String(s || "working"); return s.charAt(0).toUpperCase() + s.slice(1); };
function renderJob(j) {
  S.job = j; const wasBusy = S.busy; S.busy = !!(j && j.running);
  const card = $("#job");
  if (!j) { card.hidden = true; if (wasBusy) resumePaused(); refreshActions(); return; }
  card.hidden = false;
  const p = j.progress || {}, res = j.result;
  const done = (p.rows_written || 0) + (p.current_slice_rows || 0);
  $("#j-spin").hidden = !j.running;
  $("#j-elapsed").textContent = dur(j.elapsed) + " elapsed";
  let stage = j.running ? STAGE(p.stage) : (res && [0, 3].includes(res.exit_code) ? "Done" : "Finished");
  if (j.running && j.kind === "resume") stage = "Resuming: " + stage;
  if (j.running && ["counting", "planning"].includes(p.stage) && (p.windows_counted || p.rows_planned))
    stage += " (" + fmt(p.windows_counted || 0) + " windows counted, " + fmt(p.rows_planned || 0) + " events so far)";
  $("#j-stage").textContent = stage;
  const exp = res ? res.rows_expected : p.rows_expected, wr = res ? res.rows_written : done;
  $("#j-rows").textContent = fmt(wr || 0) + (exp != null ? " / " + fmt(exp) : "");
  $("#j-rows-l").textContent = exp != null ? "events written / expected" : "events written";
  $("#j-slices").textContent = res && res.slices ? String(res.slices).replace("/", " / ") : fmt(p.complete || 0) + " / " + (p.slices != null ? fmt(p.slices) : "?");
  const bar = $("#j-bar"), fill = $("#j-fill");
  let pct = p.pct;
  if (res && [0, 3].includes(res.exit_code)) pct = 100;
  if (pct == null && !j.running) pct = null;
  if (pct == null) { bar.classList.toggle("indet", !!j.running); fill.style.width = j.running ? "" : "0%"; bar.removeAttribute("aria-valuenow");
    bar.setAttribute("aria-valuetext", j.running ? stage : "stopped"); $("#j-pct").textContent = j.running ? "…" : "–"; }
  else { bar.classList.remove("indet"); fill.style.width = Math.max(0, Math.min(100, pct)).toFixed(1) + "%"; bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    bar.setAttribute("aria-valuetext", Math.round(pct) + "%"); $("#j-pct").textContent = Number(pct).toFixed(pct >= 99.95 || pct < 10 ? 1 : 0).replace(/\.0$/, "") + "%"; }
  $("#j-eta").textContent = j.running ? (p.eta_words ? p.eta_words.replace(/ left$/, "") : "estimating…") : "–";
  bar.classList.toggle("ok", !!(res && res.exit_code === 0)); bar.classList.toggle("warn", !!(res && res.exit_code === 3));
  const log = $("#j-log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.textContent = (j.lines || []).map(visible).join("\n"); if (atEnd) log.scrollTop = log.scrollHeight;
  const key = j.id + ":" + (res ? "done:" + res.exit_code : "run");
  if (key !== S.jobKey) { S.jobKey = key; $("#j-result").innerHTML = res ? resultHtml(res, j) : ""; }
  if (j.running && p.stage !== S.lastStage) { S.lastStage = p.stage; announce("Export: " + STAGE(p.stage)); }
  if (!j.running && wasBusy) { announce("Export finished: " + stage + "."); resumePaused(); }
  if (!j.running) S.lastStage = "";
  refreshActions();
}
const LARGE_NOTE = '<div class="hint note">The file is already saved in the exports folder, so Download is optional: it makes a second copy through the browser. ' +
  "For multi-GB files, Open folder is better. Keep the terminal window open while an export runs; if the computer sleeps, Resume continues where it stopped.</div>";
function resultHtml(res, j) {
  const code = res.exit_code, n = fmt(res.rows_written), m = fmt(res.rows_expected);
  let head, cls, resume = false;
  if (code === 0 && res.zero_result) { cls = "warn"; head = "Complete: nothing matched, so the file is empty."; }
  else if (code === 0) { cls = "ok"; head = "Complete: " + n + " events written, matching the platform's total exactly."; }
  else if (code === 3) { cls = "warn"; head = (res.coverage_warning || "Partial: recent data only. Older data was not searched.") + " " + n + " events written from recent data."; }
  else if (code === 1 && !res.run_dir) { cls = "bad"; head = "Stopped: " + (res.error || "the export could not start.") + " Nothing was saved, so there is nothing to resume. Start the export again."; }
  else if (code === 1) { cls = "bad"; resume = true; head = "Incomplete: " + n + " of " + m + " events written. Resume continues from the first unfinished slice." + (res.error ? " " + res.error : ""); }
  else if (code === 130) { cls = "warn"; resume = !res.nothing_to_resume; head = res.nothing_to_resume ? (res.error || "Stopped before any events were written. There is nothing to resume.") :
    "Cancelled: " + n + " of " + m + " events written. Resume continues where it stopped."; }
  else { cls = "bad"; head = "Stopped: " + (res.error || "a check or error stopped the export before it finished."); }
  let out = '<div class="banner ' + cls + '">' + esc(head) + "</div>";
  if (res.stored_duplicates) out += '<div class="banner info small">' + esc(fmt(res.stored_duplicates) + " duplicate copies were removed; every event is exported once.") + "</div>";
  (Array.isArray(res.warnings) ? res.warnings : []).forEach(w => { if (code === 3 && w === res.coverage_warning) return;   /* already in the headline */
    out += '<div class="banner warn small">' + esc(w) + "</div>"; });
  if ((!res.warnings || !res.warnings.length) && Array.isArray(res.filter_unproven) && res.filter_unproven.length)
    out += '<div class="banner warn small">' + esc("Not proven to filter: " + res.filter_unproven.join(", ") + ". Check the file before relying on it.") + "</div>";
  if (res.id_conflicts) out += '<div class="banner warn small">' + esc(fmt(res.id_conflicts) + " events share an id with a different event. Both were written and counted.") + "</div>";
  const rd = res.run_dir, btns = [];                    /* null when core removed the folder */
  if (res.merged_path) btns.push('<button class="tbtn primary" type="button" data-dl="' + esc(res.merged_path) + '">Download ' + esc(dlName(res.merged_path)) + "</button>");
  if (rd && !res.nothing_to_resume) btns.push('<button class="tbtn" type="button" data-open="' + esc(rd) + '">Open folder</button>');
  if (resume && rd) btns.push('<button class="tbtn" type="button" data-resume="' + esc(rd) + '">Resume</button>');
  if (btns.length) out += '<div class="row gap wrap">' + btns.join("") + "</div>";
  if (res.merged_path) out += LARGE_NOTE;
  if (rd && !res.nothing_to_resume) out += '<div class="hint mono">' + esc(rd) + (res.slices ? " · slices " + esc(res.slices) : "") + "</div>";
  return out;
}
/* The name the browser saves it under: '<run folder>.<suffix>', the same rule as the server. */
function dlName(path) {
  const parts = String(path || "").split(/[\\/]/), base = parts.pop() || "", folder = parts.pop() || "";
  const dot = base.indexOf(".");
  return folder && dot > 0 ? folder + base.slice(dot) : base;
}
async function download(path) {
  try {
    const r = await api("ticket", {body: {path: path}});
    const a = document.createElement("a"); a.href = r.url; a.download = ""; a.rel = "noopener"; a.hidden = true;
    document.body.appendChild(a); a.click(); setTimeout(() => a.remove(), 1000);
    toast("Download started. The file is also in the exports folder.");
  } catch (e) { toast(e.message, true); }
}
async function openFolder(path) {
  try { const r = await api("open", {body: {path: path}}); if (r.info) notice(r.info); }
  catch (e) { toast(e.message, true); }
}

/* ---------- exports list ---------- */
const PILL = {COMPLETE: "PASS", PARTIAL: "WARN", RUNNING: "RUN", PLANNED: "INFO", INCOMPLETE: "WARN", FAILED: "FAIL"};
const STATUS_LABEL = {PLANNED: "Planned, not started (dry run, or stopped right after planning)"};
async function loadExports(quiet) {
  const o = $("#exports-out");
  if (!quiet) o.innerHTML = '<div class="empty"><span class="spin" aria-hidden="true"></span>Loading…</div>';
  try {
    const r = await api("exports"); S.home = r.home; S.exports = r.exports;
    const head = '<div class="row gap wrap"><button class="tbtn" type="button" data-open="' + esc(r.home) + '">Open exports folder</button>' +
      '<span class="hint mono">' + esc(r.home) + "</span></div>";
    if (!r.exports.length) { o.innerHTML = head + '<div class="empty">No exports yet. They are saved in the folder above.</div>'; return; }
    o.innerHTML = head + LARGE_NOTE + '<div class="tbl-wrap"><table class="exports"><thead><tr><th scope="col">Status</th><th scope="col">Export</th><th scope="col">Tenant</th>' +
      '<th scope="col" class="num">Events</th><th scope="col" class="num">File</th><th scope="col">Window (UTC)</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>' +
      r.exports.map(x => {
        const st = x.status || x.verdict || "INCOMPLETE", w = x.window || {};
        const partial = st === "PARTIAL" || (!!x.partial && st === "COMPLETE");   /* recent data only: older data was not searched */
        return "<tr><td><span class=\"pill " + (partial ? "WARN" : (PILL[st] || "WARN")) + '">' + esc(partial ? "PARTIAL" : (STATUS_LABEL[st] || st)) + "</span>" +
          (partial && x.partial_note ? '<div class="hint">' + esc(x.partial_note) + "</div>" : "") + "</td>" +
          '<td class="wrapcell">' + esc(x.name) + (x.stored_duplicates ? '<div class="hint">' + esc(fmt(x.stored_duplicates)) + " duplicate copies removed</div>" : "") + "</td>" +
          "<td>" + esc(x.tenant || "") + (x.tenant_id ? '<div class="hint mono">' + esc(x.tenant_id) + "</div>" : "") + "</td>" +
          '<td class="num">' + esc(fmt(x.rows_written)) + (st === "COMPLETE" || st === "PARTIAL" ? "" : " / " + esc(fmt(x.rows_expected))) + "</td>" +
          '<td class="num">' + (x.merged_bytes != null ? esc(bytes(x.merged_bytes)) :
            '<span class="muted">none</span>' + (st === "RUNNING" ? "" : '<div class="hint">Download needs the combined file</div>')) + "</td>" +
          '<td class="win">' + (w.start ? esc(String(w.start).slice(0, 16) + " →") + "<br>" + esc(String(w.end || "").slice(0, 16)) : "") + "</td>" +
          '<td class="acts">' +
          (x.merged_path && st !== "RUNNING" ? '<button class="lbtn" type="button" data-dl="' + esc(x.merged_path) + '">Download</button>' : "") +
          (x.resumable && st !== "RUNNING" ? '<button class="lbtn" type="button" data-resume="' + esc(x.run_dir) + '">Resume</button>' : "") +
          '<button class="lbtn" type="button" data-open="' + esc(x.run_dir) + '">Open folder</button>' +
          (st !== "RUNNING" ? '<button class="lbtn danger" type="button" data-del="' + esc(x.run_dir) + '">Delete</button>' : "") + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    refreshActions();
  } catch (e) { o.innerHTML = '<div class="banner bad">' + esc(e.message) + "</div>"; }
}

/* delete a finished, failed or planned export, confirmed inline in its own row. */
function askDelete(btn) {
  const cell = btn.closest("td"); if (!cell || cell.dataset.asking) return;
  cell.dataset.asking = "1"; cell.dataset.orig = cell.innerHTML;
  cell.innerHTML = '<div class="confirm inline" role="alertdialog" aria-label="Confirm delete">Delete this export folder and every file in it? This cannot be undone.' +
    '<div class="row"><button class="lbtn solid" type="button" data-del-yes="' + esc(btn.dataset.del) + '">Delete</button>' +
    '<button class="lbtn" type="button" data-del-no="1">Cancel</button></div></div>';
  $("[data-del-no]", cell).focus();
}
function cancelDelete(btn) {
  const cell = btn.closest("td"); if (!cell) return;
  cell.innerHTML = cell.dataset.orig || ""; delete cell.dataset.asking; delete cell.dataset.orig;
  const d = $("[data-del]", cell); if (d) d.focus();
}
async function doDelete(btn) {
  const runDir = btn.dataset.delYes; btn.disabled = true;
  try {
    await api("export-delete", {body: {run_dir: runDir}});
    toast("Deleted the export folder.");
    S.jobKey = ""; await refreshJob();                   /* the result card drops buttons for a folder that is gone */
  } catch (e) { toast(e.message, true); }
  loadExports(true);
}

/* ---------- saved searches ---------- */
async function loadSaved() {
  try { const r = await api("saved");
    $("#saved").innerHTML = '<option value="">Choose a saved search</option>' + r.saved.map(x => '<option value="' + esc(x.file) + '">' +
      esc(x.name + (x.mode === "advanced" ? " (command line only)" : x.mode === "kql" ? (S.caps && S.caps.kql ? " (KQL)" : " (KQL, not available here)") : "")) + "</option>").join("");
  } catch (e) { /* not fatal */ }
}
function ask(msg, yes, act) {
  S.confirmAct = act; $("#saved-confirm-msg").textContent = msg; $("#sc-yes").textContent = yes;
  $("#saved-confirm").hidden = false; $("#sc-yes").focus();
  $("#saved-confirm").scrollIntoView({block: "center"});      /* never left under the sticky action bar */
}
function closeAsk() { S.confirmAct = null; $("#saved-confirm").hidden = true; }
async function doSave(overwrite) {
  readSpec(); const name = $("#savename").value.trim();
  if (!name) { toast("Name the search first", true); $("#savename").focus(); return; }
  S.spec.name = name;
  try { await api("saved", {body: {spec: S.spec, overwrite: !!overwrite}}); closeAsk(); await loadSaved(); changed(); S.baseline = dirtyKey(); toast("Saved " + name); }
  catch (e) {
    if (e.kind === "exists") ask("A saved search called '" + name + "' already exists. Replace it?", "Replace", () => doSave(true));
    else toast(e.message, true);
  }
}

/* ---------- help ---------- */
async function openHelp(path) {
  $$("#help-nav button").forEach(b => { const on = b.dataset.p === path; b.classList.toggle("on", on); if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
  try { const r = await api("help?path=" + encodeURIComponent(path)); $("#help-body").innerHTML = md(r.text); $("#rbody").scrollTop = 0; }
  catch (e) { $("#help-body").innerHTML = '<div class="banner bad">' + esc(e.message) + "</div>"; }
}
function md(src) {                                   /* small, safe Markdown: escape first, then format */
  const lines = String(src).replace(/\r/g, "").split("\n"); let out = "", i = 0;
  const inline = t => esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, a, h) => /^https?:\/\//.test(h) ? '<a href="' + h + '" target="_blank" rel="noopener noreferrer">' + a + "</a>" : "<b>" + a + "</b>");
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) { const buf = []; i++; while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]); i++;
      out += '<pre tabindex="0"><code>' + esc(buf.join("\n")) + "</code></pre>"; continue; }
    let m;
    if ((m = /^(#{1,3})\s+(.*)$/.exec(l))) { const n = m[1].length + 1; out += "<h" + n + ">" + inline(m[2]) + "</h" + n + ">"; i++; continue; }
    if (/^\|/.test(l)) { const rows = []; while (i < lines.length && /^\|/.test(lines[i])) rows.push(lines[i++]);
      /* "\|" is a literal pipe inside a cell (GitHub's rule), so it is kept aside before splitting */
      const cells = r => r.replace(/\\\|/g, "\u0001").replace(/^\||\|$/g, "").split("|").map(c => c.trim().replace(/\u0001/g, "|"));
      out += "<div class='tbl-wrap'><table><thead><tr>" + cells(rows[0]).map(c => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>" +
        rows.slice(2).map(r => "<tr>" + cells(r).map(c => "<td>" + inline(c) + "</td>").join("") + "</tr>").join("") + "</tbody></table></div>"; continue; }
    if (/^\s*[-*]\s+/.test(l)) { out += "<ul>"; while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) out += "<li>" + inline(lines[i++].replace(/^\s*[-*]\s+/, "")) + "</li>"; out += "</ul>"; continue; }
    if (/^\s*\d+\.\s+/.test(l)) { out += "<ol>"; while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) out += "<li>" + inline(lines[i++].replace(/^\s*\d+\.\s+/, "")) + "</li>"; out += "</ol>"; continue; }
    if (/^>\s?/.test(l)) { const buf = []; while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, "")); out += "<blockquote>" + inline(buf.join(" ")) + "</blockquote>"; continue; }
    if (!l.trim()) { i++; continue; }
    const buf = []; while (i < lines.length && lines[i].trim() && !/^(#|```|\||\s*[-*]\s|\s*\d+\.\s|>)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) buf.push(lines[i++]);
    out += "<p>" + inline(buf.join(" ")) + "</p>";
  }
  return out;
}

/* ---------- start-up: exchange the one-time launch code for a session token ---------- */
async function signIn() {
  const qs = new URLSearchParams(location.search), code = qs.get("code");
  if (code) {
    history.replaceState(null, "", location.pathname);        /* the code never stays in the address bar */
    try {
      const r = await fetch("/api/session", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({code: code}), cache: "no-store", credentials: "omit"});
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.token) { S.token = j.token; store("abx_token", j.token); return true; }
      if (!stored("abx_token")) { needLink(j.error || "This link has already been used."); return false; }
    } catch (e) { locked("The app is not responding. Is its terminal window still open?"); return false; }
  }
  S.token = stored("abx_token");
  if (!S.token) { needLink("This page needs the link the app printed when it started."); return false; }
  return true;
}
async function init() {
  if (!(await signIn())) return;
  let st;
  try { st = await api("state"); }
  catch (e) { if (e.kind !== "token") locked(e.network ? "The app is not responding. Is its terminal window still open?" : e.message); return; }
  S.tenant = st.tenant; S.settings = st.settings || {}; S.ops = st.operators; S.presets = st.presets; S.typeLabels = st.type_labels || {};
  S.caseOps = st.case_ops || []; S.notOps = st.not_ops || [];
  S.defaults = st.default_fields; S.help = st.help; S.home = st.home; S.sep = st.sep || "/"; S.empty = st.empty_spec; S.prodUrl = st.prod_url;
  $("#time").innerHTML = S.presets.map(p => '<option value="' + esc(p) + '">Last ' + esc(p) + "</option>").join("") + '<option value="custom">Custom range (UTC)</option>';
  $("#help-nav").innerHTML = S.help.map(h => '<button type="button" data-p="' + esc(h.path) + '">' + esc(h.title) + "</button>").join("");
  $("#export-dest").textContent = "Saved to " + S.home + ". The browser never holds the export; it is written straight to this computer's disk.";
  writeSpec(st.initial_spec || emptySpec());
  renderChips(); renderJob(st.job);
  if (!st.initial_spec && !(st.job && st.job.running)) tab("start");
  if (S.tenant) loadCatalog(); else openConnect();
  loadSaved(); poll(false); feedShow(feedOpen()); feedPoll();
}

/* ---------- wiring (no inline handlers anywhere) ---------- */
document.addEventListener("click", e => {
  const t = e.target.closest("#rtabs [data-t]"); if (t) return tab(t.dataset.t);
  const seg = e.target.closest(".seg button");
  if (seg) {
    const grp = seg.parentElement.id; setSeg("#" + grp, seg.dataset.v);
    if (grp === "mode") { readSpec(); S.spec.mode = seg.dataset.v; showMode(); renderProblems(); }
    if (grp === "fmt") S.fmt = seg.dataset.v;
    changed(); return;
  }
  const dl = e.target.closest("[data-dl]"); if (dl) return download(dl.dataset.dl);
  const op = e.target.closest("[data-open]"); if (op) return openFolder(op.dataset.open);
  const rs = e.target.closest("[data-resume]"); if (rs && !rs.disabled) return doResume(rs.dataset.resume);
  const hp = e.target.closest("#help-nav button"); if (hp) return openHelp(hp.dataset.p);
  const vs = e.target.closest("#a-vstop"); if (vs && !vs.disabled) return stopValidate(vs);
  const zb = e.target.closest("[data-zero]"); if (zb) return exportAnyway();
  const dy = e.target.closest("[data-del-yes]"); if (dy && !dy.disabled) return doDelete(dy);
  const dn = e.target.closest("[data-del-no]"); if (dn) return cancelDelete(dn);
  const dd = e.target.closest("[data-del]"); if (dd) return askDelete(dd);
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("#modal").hidden) closeConnect();
  if (e.key === "Escape" && !$("#imp").hidden) closeImport();
  if (e.key === "Tab" && !$("#imp").hidden) {
    const f = $$("#imp select, #imp textarea, #imp button").filter(x => !x.disabled), first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  if (e.key === "Escape" && !$("#saved-confirm").hidden) closeAsk();
  if (e.key === "Tab" && !$("#modal").hidden) {                  /* keep focus inside the dialog */
    const f = $$("#modal input, #modal button").filter(x => !x.disabled), first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});
$("#b-connect").addEventListener("click", openConnect);
$("#b-disconnect").addEventListener("click", doDisconnect);
$("#c-cancel").addEventListener("click", closeConnect);
$("#c-go").addEventListener("click", doConnect);
$("#c-key").addEventListener("keydown", e => { if (e.key === "Enter") doConnect(); });
$("#b-help").addEventListener("click", () => tab("help"));
$("#notice-x").addEventListener("click", () => notice(""));
$("#b-import").addEventListener("click", openImport);
$("#i-cancel").addEventListener("click", closeImport);
$("#i-load").addEventListener("click", loadViews);
$("#i-convert").addEventListener("click", doConvert);
$("#i-use").addEventListener("click", useImport);
$("#i-checkall").addEventListener("click", checkAllViews);
$("#i-sync").addEventListener("click", syncViews);
$("#i-checks").addEventListener("click", e => { const id = e.target && e.target.dataset && e.target.dataset.vpick;
  if (id) { $("#i-view").value = id; doConvert(); } });
$("#b-learnall").addEventListener("click", learnAll);
$("#b-caps").addEventListener("click", () => loadCaps(true));
$("#a-cmd").addEventListener("click", copyCommand);
$("#b-learnstop").addEventListener("click", () => { learnStop = true; $("#b-learnstop").hidden = true; });
$("#i-view").addEventListener("change", () => { if ($("#i-view").value) { $("#i-text").value = ""; doConvert(); } });
$("#i-text").addEventListener("input", () => { $("#i-use").disabled = true; impResult = null; $("#i-out").innerHTML = ""; if ($("#i-text").value) $("#i-view").value = ""; });
$("#b-newsearch").addEventListener("click", () => { S.validatedKey = null; S.zeroKey = null; writeSpec(emptySpec()); showMode(); renderProblems(); });
$("#b-addf").addEventListener("click", () => { readSpec(); S.spec.filters.push({field: "", op: "IS_IN_LIST", type: "String", value: ""}); renderFilters(); changed(); $$(".frow .ff").pop().focus(); });
$("#addfield").addEventListener("change", e => { addField(e.target.value); e.target.value = ""; });
$("#addfield").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); addField(e.target.value); e.target.value = ""; } });
$("#fulldocs").addEventListener("change", () => { readSpec(); renderFieldChips(); changed(); });
$("#time").addEventListener("change", () => { $("#custom-time").hidden = $("#time").value !== "custom"; if ($("#time").value === "custom") syncPickers(); changed(); });

/* Date and time pickers for a custom range. The picker's value is read as UTC (the text box beside it shows the exact
   UTC time sent); the line under each shows the same moment in this computer's time zone. */
function utcText(v) { if (!v) return ""; const s = v.length === 16 ? v + ":00" : v.slice(0, 19); return s + "Z"; }
function pickerVal(t) { const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?/.exec(t || ""); return m ? m[1] + "T" + m[2] + (m[3] || ":00") : ""; }
function localNote(t) {
  const d = new Date(t); if (!t || isNaN(d)) return "";
  return "= " + d.toLocaleString(undefined, {dateStyle: "medium", timeStyle: "medium"}) + " in this computer's time zone";
}
function syncPickers() {
  for (const k of ["start", "end"]) {
    const t = $("#t-" + k).value.trim();
    $("#t-" + k + "-p").value = pickerVal(t); $("#t-" + k + "-local").textContent = localNote(t);
  }
  const now = new Date(Date.now() - 10 * 60000);
  const max = now.toISOString().slice(0, 19);
  $("#t-start-p").max = max; $("#t-end-p").max = max;
}
["start", "end"].forEach(k => {
  $("#t-" + k + "-p").addEventListener("change", () => { $("#t-" + k).value = utcText($("#t-" + k + "-p").value); syncPickers(); changed(); });
  $("#t-" + k).addEventListener("change", syncPickers);
});
$("#custom-time").addEventListener("click", e => {
  const b = e.target.closest("[data-q]"); if (!b) return;
  const day = 86400000, end0 = new Date(Date.now() - 10 * 60000), today = new Date(Date.UTC(end0.getUTCFullYear(), end0.getUTCMonth(), end0.getUTCDate()));
  let s, en;
  if (b.dataset.q === "today") { s = today; en = end0; }
  else if (b.dataset.q === "yesterday") { s = new Date(today - day); en = today; }
  else if (b.dataset.q === "7d") { s = new Date(today - 7 * day); en = today; }
  else if (b.dataset.q === "30d") { s = new Date(today - 30 * day); en = today; }
  else { en = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1)); s = new Date(Date.UTC(en.getUTCFullYear(), en.getUTCMonth() - 1, 1)); }
  $("#t-start").value = s.toISOString().slice(0, 19) + "Z"; $("#t-end").value = en.toISOString().slice(0, 19) + "Z";
  syncPickers(); changed();
});
$("#storage").addEventListener("change", () => { storageHint(); changed(); });
$("#match").addEventListener("change", () => { readSpec(); renderFilters(); changed(); });
["#dataset", "#t-start", "#t-end"].forEach(s => $(s).addEventListener("input", changed));
$("#merge").addEventListener("change", refreshActions);
$("#fsearch").addEventListener("input", renderFieldGrid);
$("#fall").addEventListener("change", renderFieldGrid);
$("#dataset").addEventListener("change", () => loadProfile(false));
$("#b-reprof").addEventListener("click", () => loadProfile(true));
$("#b-suggest").addEventListener("click", () => { if (!S.prof) return; S.spec.fields = S.prof.suggested.slice(); S.spec.full_documents = false;
  $("#fulldocs").checked = false; renderFieldChips(); renderFieldGrid(); changed(); toast(S.spec.fields.length + " suggested fields selected"); });
$("#b-allseen").addEventListener("click", () => { if (!S.prof) return;
  S.spec.fields = Array.from(new Set(["@timestamp", "id"].concat(S.prof.fields.map(f => f.name))));
  S.spec.full_documents = false; $("#fulldocs").checked = false; renderFieldChips(); renderFieldGrid(); changed();
  toast(S.spec.fields.length + " fields selected: every ACS and ext field seen in the sampled events"); });
$("#a-preview").addEventListener("click", doPreview);
$("#a-validate").addEventListener("click", doValidate);
$("#a-export").addEventListener("click", () => { S.touched = true; renderProblems(); tab("export"); });     /* opens the tab, never starts an export */
$("#a-export2").addEventListener("click", doExport);
$("#a-stop").addEventListener("click", async () => { try { await api("cancel", {body: {}}); toast("Stopping after the current page…"); poll(true); } catch (e) { toast(e.message, true); } });
async function doLoad(f) {
  try { const r = await api("saved", {body: {load: f}}); closeAsk(); S.validatedKey = null; S.zeroKey = null; writeSpec(r.spec);
        toast(r.spec.mode === "advanced" ? "Loaded " + (r.spec.name || f) + ". It runs only from the command line." : "Loaded " + (r.spec.name || f)); if (S.tenant) loadProfile(false); }
  catch (e) { toast(e.message, true); }
}
$("#b-load").addEventListener("click", () => { const f = $("#saved").value; if (!f) { toast("Choose a saved search first", true); return; }
  if (isDirty()) {                                        /* never replace unsaved changes silently */
    const label = $("#saved").selectedOptions[0].textContent;
    ask("Load '" + label + "'? It replaces the current search, and your changes to it are not saved.", "Load and replace", () => doLoad(f));
    return;
  }
  doLoad(f); });
$("#b-save").addEventListener("click", () => doSave(false));
$("#b-del").addEventListener("click", () => { const f = $("#saved").value; if (!f) { toast("Choose a saved search first", true); return; }
  const label = $("#saved").selectedOptions[0].textContent;
  ask("Delete the saved search '" + label + "'? This cannot be undone.", "Delete", async () => {
    try { await api("saved?file=" + encodeURIComponent(f), {method: "DELETE"}); closeAsk(); await loadSaved(); toast("Deleted"); } catch (e) { toast(e.message, true); } }); });
$("#sc-yes").addEventListener("click", () => { if (S.confirmAct) S.confirmAct(); });
$("#sc-no").addEventListener("click", closeAsk);
init().catch(e => toast("Could not start: " + e.message, true));
