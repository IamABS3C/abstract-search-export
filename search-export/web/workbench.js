/* Abstract Search & Export 0.3.0: the query workbench (builder, KQL, plain English), fieldsets,
   saved-view changes, the review dialog and the platform endpoint list. Loaded after app.js and uses its helpers
   ($, esc, api, S, tab, readSpec, changed …). Everything rendered goes through esc() or textContent; nothing changes
   in Abstract without a plan from the app and an explicit confirm here. */
"use strict";
S.qtab = "builder"; S.kqlState = null; S.kqlSeq = 0;
S.fsets = null; S.fsSel = []; S.fsEdit = null; S.fsCov = {}; S.review = null; S.viewsTab = null;

const RUNS = {filters: "Exact export", kql: "Runs as KQL", kql_result: "KQL result table", refused: "Cannot run"};
const STORED = /^(ext\.|event\.parsed\.).+/;

/* ---------- query tabs ---------- */
function wbShowMode(m) {
  if (m === "advanced") { S.qtab = "builder"; paintQtabs(); return false; }   /* app.js shows the command-line note */
  if (m === "kql") {
    S.qtab = "kql";
    const ed = $("#kql-text");
    if (document.activeElement !== ed && ed.value.trim() !== (S.spec.kql || "").trim()) { ed.value = S.spec.kql || ""; gutter(); scheduleCheck(0); }
  }
  paintQtabs(); return true;
}
function paintQtabs() {
  ["builder", "kql", "nl"].forEach(q => { const b = $("#qt-" + q), on = S.qtab === q;
    b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; });
  $("#m-simple").hidden = S.qtab !== "builder"; $("#m-kql").hidden = S.qtab !== "kql"; $("#m-nl").hidden = S.qtab !== "nl";
  $("#m-advanced").hidden = true; $("#imp-group").hidden = S.qtab === "nl";
}
function setQtab(q) {
  if (q === S.qtab) return;
  S.qtab = q;
  if (q === "builder") {
    if (S.spec.mode === "kql") { readSpec(); S.spec.mode = "simple"; }
    S.kqlState = null;
  }
  if (q === "kql") {
    if ($("#kql-text").value.trim()) scheduleCheck(0);
    else if (S.spec.mode === "simple" && (S.spec.dataset || S.spec.filters.length)) builderToKql(true);
  }
  paintQtabs(); changed(); renderProblems();
}
$("#qtabs").addEventListener("click", e => { const b = e.target.closest("[data-q]"); if (b) setQtab(b.dataset.q); });
$("#qtabs").addEventListener("keydown", e => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const order = ["builder", "kql", "nl"], i = order.indexOf(S.qtab);
  const n = e.key === "Home" ? 0 : e.key === "End" ? 2 : (i + (e.key === "ArrowRight" ? 1 : 2)) % 3;
  e.preventDefault(); setQtab(order[n]); $("#qt-" + order[n]).focus();
});

/* ---------- KQL editor ---------- */
function gutter() {
  const n = Math.max(1, $("#kql-text").value.split("\n").length);
  $("#kql-gutter").textContent = Array.from({length: n}, (_, i) => i + 1).join("\n");
  $("#kql-gutter").scrollTop = $("#kql-text").scrollTop;
}
let checkT = null;
function scheduleCheck(ms) { clearTimeout(checkT); checkT = setTimeout(runCheck, ms == null ? 650 : ms); }
async function runCheck() {
  const text = $("#kql-text").value.trim(), out = $("#kql-badge"), seq = ++S.kqlSeq;
  S.kqlState = null; $("#kql-tobuilder").hidden = true;
  if (!text) { out.innerHTML = ""; refreshActions(); return; }
  if (!S.tenant) { out.innerHTML = '<span class="badge wait">Not checked</span><span class="why">Connect to check how this query runs on your tenant.</span>'; return; }
  if ((text.replace(/\\./g, "").match(/"/g) || []).length % 2) {    /* still typing a quoted value */
    out.innerHTML = '<span class="badge wait">Typing</span><span class="why">Close the quoted value and the check runs.</span>'; return;
  }
  out.innerHTML = '<span class="badge wait">Checking</span>';
  try {
    const r = await api("kql-check", {body: {kql: text}});
    if (seq !== S.kqlSeq || $("#kql-text").value.trim() !== text) return;
    applyCheck(r, text);
  } catch (e) { if (seq === S.kqlSeq) out.innerHTML = '<span class="badge refused">Not checked</span><span class="why">' + esc(e.message) + "</span>"; }
}
function badgeHtml(r) {
  let h = '<span class="badge ' + esc(r.runs_as) + '">' + esc(RUNS[r.runs_as] || r.runs_as) + '</span><span class="why">' + esc(r.reason) + "</span>";
  const items = [];
  (r.violations || []).forEach(v => items.push(esc(v.reason || v.kind || "") + (v.suggestion ? " <b>" + esc(v.suggestion) + "</b>" : "")));
  (r.warnings || []).forEach(w => items.push(esc(w)));
  const ref = r.server && r.server.refusal;
  if (ref && r.runs_as !== "filters" && ref.message) items.push("Abstract's filter builder cannot hold it: " + esc(ref.message));
  (r.readable || []).slice(0, 8).forEach(x => items.push(esc(x)));
  return h + (items.length ? "<ul>" + items.map(x => "<li>" + x + "</li>").join("") + "</ul>" : "");
}
function applyCheck(r, text) {
  S.kqlState = r;
  $("#kql-badge").innerHTML = badgeHtml(r);
  readSpec();
  if (r.runs_as === "filters") {
    Object.assign(S.spec, {mode: "simple", kql: r.kql, dataset: r.dataset || "", filters: r.filters || [], any_groups: r.any_groups || []});
    $("#dataset").value = r.dataset || ""; renderFilters();
    $("#kql-tobuilder").hidden = false;
  } else {
    S.spec.mode = "kql"; S.spec.kql = r.runs_as === "refused" ? text : r.kql;
  }
  changed(); renderProblems();
}
$("#kql-text").addEventListener("input", () => { gutter(); S.kqlState = null; $("#kql-badge").innerHTML = '<span class="badge wait">Checking</span>'; scheduleCheck(); suggest(); refreshActions(); });
$("#kql-text").addEventListener("scroll", () => { $("#kql-gutter").scrollTop = $("#kql-text").scrollTop; });
$("#kql-text").addEventListener("click", () => closeSuggest());
$("#kql-tobuilder").addEventListener("click", () => setQtab("builder"));
$("#b-showkql").addEventListener("click", () => builderToKql(false));
async function builderToKql(quiet) {
  if (!S.tenant) { if (!quiet) openConnect(); return; }
  readSpec();
  try {
    const r = await api("spec-kql", {body: {spec: Object.assign({}, S.spec, {mode: "simple"})}});
    if (!r.kql) { if (!quiet) toast(r.reason ? "This search has no exact KQL form: " + r.reason : "Add a dataset or a filter to the builder first.", true); return; }
    $("#kql-text").value = r.kql; gutter(); S.qtab = "kql"; paintQtabs(); scheduleCheck(0);
    if (!quiet) toast(r.source === "abstract" ? "The builder's search, as Abstract writes it in KQL." : "The builder's search, written as KQL.");
  } catch (e) { if (!quiet) toast(e.message, true); }
}

/* suggestions: fields, operators and stages as you type; field values inside quotes */
let sugItems = [], sugIdx = -1, valT = null;
function closeSuggest() { $("#kql-suggest").hidden = true; sugItems = []; sugIdx = -1; $("#kql-text").removeAttribute("aria-activedescendant"); }
function kqlWords() {
  if (S.kqlWords) return S.kqlWords;
  const w = new Map();
  ["where", "summarize", "project", "extend", "take", "top", "sort by", "order by", "distinct", "count()", "dcount(", "by",
   "and", "or", "not(", "ago(", "parse_json(", "tostring("].forEach(x => w.set(x, "stage"));
  S.kqlWords = w; return w;
}
function suggest() {
  const ed = $("#kql-text"), before = ed.value.slice(0, ed.selectionStart);
  const vm = /([A-Za-z_@][\w.@]*)\s*(==|=~|!=|!~|has|!has|contains|!contains|startswith|endswith|in~?\s*\(|!in~?\s*\()\s*(?:[^"]*,\s*)?"([^"]*)$/.exec(before);
  clearTimeout(valT);
  if (vm && S.tenant) {
    const asked = ed.value;
    valT = setTimeout(async () => {
      try {
        const r = await api("field-values?field=" + encodeURIComponent(vm[1]) + "&prefix=" + encodeURIComponent(vm[3]) +
          "&dataset=" + encodeURIComponent(S.spec.dataset || ""));
        if (ed.value !== asked) return;                  /* the user typed on: this answer is for older text */
        if (!r.values || !r.values.length) { closeSuggest(); return; }
        showSuggest(r.values.slice(0, 12).map(v => ({label: String(v), insert: String(v) + '"', from: vm[3].length, kind: "value"})));
      } catch (e) { closeSuggest(); }
    }, 300);
    return;
  }
  const m = /([A-Za-z_@!][\w.@]*)$/.exec(before);
  if (!m || m[1].length < 1) { closeSuggest(); return; }
  const tok = m[1].toLowerCase(), out = [];
  const fields = (S.fields || []).concat(Object.keys(S.fsCov || {}).filter(f => f.startsWith("ext.")));
  fields.forEach(f => { if (f.toLowerCase().startsWith(tok) && f.toLowerCase() !== tok) out.push({label: f, insert: f, from: m[1].length, kind: "field"}); });
  kqlWords().forEach((kind, wd) => { if (wd.toLowerCase().startsWith(tok) && wd.toLowerCase() !== tok) out.push({label: wd, insert: wd, from: m[1].length, kind: kind}); });
  if (!out.length) { closeSuggest(); return; }
  showSuggest(out.sort((a, b) => a.label.length - b.label.length).slice(0, 12));
}
function showSuggest(items) {
  sugItems = items; sugIdx = 0;
  const ul = $("#kql-suggest");
  ul.innerHTML = items.map((x, i) => '<li role="option" id="sg-' + i + '" aria-selected="' + (i === 0) + '" data-i="' + i + '"><span>' + esc(x.label) +
    '</span><span class="k">' + esc(x.kind) + "</span></li>").join("");
  ul.hidden = false; $("#kql-text").setAttribute("aria-activedescendant", "sg-0");
}
function moveSuggest(d) {
  if (!sugItems.length) return;
  sugIdx = (sugIdx + d + sugItems.length) % sugItems.length;
  $$("#kql-suggest li").forEach((li, i) => li.setAttribute("aria-selected", String(i === sugIdx)));
  $("#kql-text").setAttribute("aria-activedescendant", "sg-" + sugIdx);
  const li = $("#sg-" + sugIdx); if (li) li.scrollIntoView({block: "nearest"});
}
function acceptSuggest(i) {
  const x = sugItems[i == null ? sugIdx : i]; if (!x) return;
  const ed = $("#kql-text"), pos = ed.selectionStart, v = ed.value;
  ed.value = v.slice(0, pos - x.from) + x.insert + v.slice(pos);
  const at = pos - x.from + x.insert.length; ed.setSelectionRange(at, at);
  closeSuggest(); gutter(); scheduleCheck(); ed.focus();
}
$("#kql-suggest").addEventListener("mousedown", e => { const li = e.target.closest("li"); if (li) { e.preventDefault(); acceptSuggest(Number(li.dataset.i)); } });
$("#kql-text").addEventListener("keydown", e => {
  const open = !$("#kql-suggest").hidden && sugItems.length;
  if (open && e.key === "ArrowDown") { e.preventDefault(); moveSuggest(1); }
  else if (open && e.key === "ArrowUp") { e.preventDefault(); moveSuggest(-1); }
  else if (open && e.key === "Tab") { e.preventDefault(); acceptSuggest(); }
  else if (open && e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSuggest(); }
});
$("#kql-text").addEventListener("blur", () => setTimeout(closeSuggest, 150));

/* a KQL result table: preview it, then save it to the exports folder */
function wbPreview() {
  if (S.qtab !== "kql" || !S.kqlState || S.kqlState.runs_as !== "kql_result") return false;
  kqlPreview(); return true;
}
async function kqlPreview() {
  if (!need()) return; tab("preview");
  const v = $("#v-preview"), b = $("#a-preview"), kql = S.kqlState.kql;
  working(b, true, "Running"); v.innerHTML = '<div class="empty"><span class="spin" aria-hidden="true"></span>Running the KQL query on Abstract…</div>';
  try {
    const r = await api("kql-preview", {body: {kql: kql, time: S.spec.time, rows: 500}});
    const cols = (r.columns || []).slice(0, 40);
    v.innerHTML = '<div class="stat"><div><b>' + esc(fmt(r.returned)) + '</b><span>rows</span></div>' +
      (r.total != null ? '<div><b>' + esc(fmt(r.total)) + '</b><span>total the query reports</span></div>' : "") +
      '<div><b>' + esc(r.seconds) + 's</b><span>query time</span></div></div>' +
      '<div class="hint mono">' + esc(r.window[0] + " → " + r.window[1]) + "</div>" +
      '<div class="banner info small">' + esc(r.reason) + "</div>" +
      (r.rows.length ? '<div class="tbl-wrap"><table><thead><tr>' + cols.map(c => '<th scope="col">' + esc(c) + "</th>").join("") +
        "</tr></thead><tbody>" + r.rows.slice(0, 500).map(row => { const f = flatten(row);
          return "<tr>" + cols.map(c => { const x = cell(f[c]); return '<td title="' + esc(short(x, 2000)) + '">' + esc(short(x, 300)) + "</td>"; }).join("") + "</tr>"; }).join("") +
        "</tbody></table></div>" : '<div class="empty">The query returned no rows in this window. A zero is a claim: widen the time range, or check the field names, before treating it as an absence.</div>') +
      '<div class="card kres"><h3>Save the result table</h3><p class="hint">Saves up to 10,000 rows to a new folder in the exports folder, with the query beside it.</p>' +
      '<div class="row gap wrap"><label class="sr-only" for="kr-name">Name</label><input type="text" id="kr-name" placeholder="name, for example logins by user" autocomplete="off">' +
      '<label class="sr-only" for="kr-fmt">Format</label><select id="kr-fmt"><option value="csv">CSV</option><option value="json">JSON</option></select>' +
      '<button class="tbtn primary" type="button" id="kr-save">Save result table</button></div><div class="hint" id="kr-out" aria-live="polite"></div></div>';
  } catch (e) { v.innerHTML = '<div class="banner bad">' + esc(e.message) + "</div>"; }
  finally { working(b, false); }
}
document.addEventListener("click", async e => {
  const b = e.target.closest("#kr-save"); if (!b) return;
  working(b, true, "Saving");
  try {
    const r = await api("kql-save", {body: {kql: S.kqlState.kql, time: S.spec.time, format: $("#kr-fmt").value, name: $("#kr-name").value}});
    $("#kr-out").innerHTML = esc("Saved " + fmt(r.rows) + " rows" + (r.truncated ? " (the query had " + fmt(r.total) + "; the first 10,000 are saved)" : "") + " to " + r.file) +
      ' <button class="lbtn" type="button" data-open="' + esc(r.folder) + '">Open folder</button>';
    toast("Saved the result table.");
  } catch (err) { toast(err.message, true); }
  finally { working(b, false); }
});

/* ---------- plain English ---------- */
async function nlGo() {
  const text = $("#nl-text").value.trim(), out = $("#nl-out");
  if (!text) { toast("Describe what you are looking for first.", true); $("#nl-text").focus(); return; }
  if (!S.tenant) { openConnect(); return; }
  const b = $("#nl-go"); working(b, true, "Writing");
  out.innerHTML = '<div class="hint"><span class="spin" aria-hidden="true"></span>Asking Abstract\'s assistant…</div>';
  try {
    const r = await api("nl", {body: {text: text}});
    if (!r.kql) { out.innerHTML = '<div class="nlcard"><div class="src">No query written</div><p>' + esc(r.explanation || "The assistant could not turn that into a filter.") +
      "</p><p class=\"hint\">Try naming the field and value, or write the KQL yourself.</p></div>"; return; }
    S.nlLast = r;
    out.innerHTML = '<div class="nlcard"><div class="src">Written by Abstract\'s assistant. Read it before you run it.</div>' +
      (r.explanation ? "<p>" + esc(r.explanation) + "</p>" : "") + "<pre>" + esc(r.kql) + "</pre>" +
      (r.check ? '<div class="runs">' + badgeHtml(r.check) + "</div>" : "") +
      '<div class="row gap wrap"><button class="tbtn primary" type="button" id="nl-use">Use this query</button>' +
      '<button class="tbtn" type="button" id="nl-copy">Copy</button></div>' +
      '<p class="hint">Nothing has searched yet. Use this query puts it in the KQL tab; Preview runs it.</p></div>';
  } catch (e) { out.innerHTML = '<div class="banner bad small">' + esc(e.message) + "</div>"; }
  finally { working(b, false); }
}
$("#nl-go").addEventListener("click", nlGo);
$("#nl-out").addEventListener("click", async e => {
  if (e.target.closest("#nl-use") && S.nlLast) {
    $("#kql-text").value = S.nlLast.kql; gutter(); S.qtab = "kql"; paintQtabs();
    if (S.nlLast.check) applyCheck(S.nlLast.check, S.nlLast.kql); else scheduleCheck(0);
    $("#kql-text").focus(); toast("The query is in the KQL tab. Choose Preview to run it.");
  }
  if (e.target.closest("#nl-copy") && S.nlLast) {
    try { await navigator.clipboard.writeText(S.nlLast.kql); toast("Copied the query."); } catch (err) { toast("Select the query and copy it."); }
  }
});

/* ---------- start screen and tab hooks ---------- */
function wbTab(name) {
  if (name === "fieldsets" && !S.fsets) loadFieldsets();
  if (name === "views") loadViewsTab(false);
  if (name === "tenant") loadEndpoints();
}
$("#start-tiles").addEventListener("click", e => {
  const t = e.target.closest("[data-go]"); if (!t) return;
  const go = t.dataset.go;
  const focus = sel => setTimeout(() => { const el = $(sel); if (el) { el.scrollIntoView({block: "center"}); el.focus(); } }, 30);
  /* the tiles that start a search need the search panel: open it if it was collapsed (a narrow window starts so) */
  if (["find", "nl", "kql"].includes(go) && document.body.classList.contains("side-closed")) sideOpen(true, false, false);
  if (go === "find") { setQtab("builder"); tab("preview"); focus("#dataset"); }
  else if (go === "nl") { setQtab("nl"); tab("preview"); focus("#nl-text"); }
  else if (go === "kql") { setQtab("kql"); tab("preview"); focus("#kql-text"); }
  else if (go === "import") openImport();
  else tab(go);
});

/* ---------- review dialog: every change to Abstract is reviewed, then confirmed once ---------- */
const KIND_WORD = {fieldset: "fieldset", view: "saved view"};
function changeHtml(c) {
  if (Array.isArray(c.added)) {
    const parts = [];
    c.added.forEach(x => parts.push('<li class="add">Adds column ' + esc(x) + "</li>"));
    c.removed.forEach(x => parts.push('<li class="rem">Removes column ' + esc(x) + "</li>"));
    if (c.reordered) parts.push("<li>Changes the column order to: " + esc((c.after || []).join(", ")) + "</li>");
    return parts.join("");
  }
  const v = x => x == null || x === "" ? "(none)" : typeof x === "object" ? JSON.stringify(x) : String(x);
  return "<li><b>" + esc(c.key) + "</b>: " + esc(short(v(c.before), 160)) + " → " + esc(short(v(c.after), 400)) + "</li>";
}
let reviewReturn = null;
function openReview(plan) {
  S.review = plan; reviewReturn = document.activeElement;
  const what = KIND_WORD[plan.kind] || plan.kind, name = plan.name || "";
  $("#rv-title").textContent = {create: "Create " + what, update: "Update " + what, delete: "Delete " + what}[plan.op] + (name ? ": " + name : "");
  $("#rv-req").textContent = plan.request;
  let body = "";
  if (plan.op === "delete") body = '<div class="del">This deletes the ' + esc(what) + " " + esc(name) + " from Abstract for everyone on this tenant. This tool cannot bring it back.</div>";
  else if (plan.op === "create") {
    const a = plan.after || {};
    body = "<ul>" + ["name", "fields", "description", "share_level", "kql_query", "time_selection", "fieldset_ids"].filter(k => a[k] != null && a[k] !== "" && !(Array.isArray(a[k]) && !a[k].length))
      .map(k => "<li><b>" + esc(k) + "</b>: " + esc(short(Array.isArray(a[k]) ? a[k].join(", ") : typeof a[k] === "object" ? JSON.stringify(a[k]) : a[k], 600)) + "</li>").join("") +
      (a.condition ? "<li><b>condition</b>: the builder's filters, exactly as the export runs them</li>" : "") + "</ul>";
  } else body = "<ul>" + (plan.changes || []).map(changeHtml).join("") + "</ul>";
  if (plan.visibility) body += '<p class="vis"><b>Who sees it:</b> ' + esc(plan.visibility) + "</p>";
  body += (plan.warnings || []).length ? "<ul>" + plan.warnings.map(w => '<li class="note">' + esc(w) + "</li>").join("") + "</ul>" : "";
  body += '<p class="hint">This review works once and for ' + Math.round((plan.expires_in || 600) / 60) + " minutes. If someone changes the " + esc(what) +
    " in the meantime, the confirm is refused and you review again.</p>";
  $("#rv-body").innerHTML = body;
  $("#rv-raw").textContent = plan.body ? JSON.stringify(plan.body, null, 2) : "(no body)";
  $("#rv-raw-d").hidden = !plan.body;
  $("#rv-err").textContent = "";
  const go = $("#rv-go");
  go.textContent = {create: "Create " + what, update: "Save changes", delete: "Delete " + what}[plan.op];
  go.className = "tbtn " + (plan.op === "delete" ? "danger" : "primary");
  $("#review").hidden = false; $("#rv-cancel").focus();
}
function closeReview() { $("#review").hidden = true; S.review = null; if (reviewReturn && reviewReturn.focus) reviewReturn.focus(); }
async function confirmReview() {
  const p = S.review; if (!p) return;
  const b = $("#rv-go"); working(b, true, "Saving");
  try {
    const r = await api(p.kind === "fieldset" ? "fieldset-apply" : "view-apply", {body: {token: p.token}});
    closeReview();
    toast({create: "Created", update: "Updated", delete: "Deleted"}[p.op] + " the " + KIND_WORD[p.kind] + (p.name ? " " + p.name : "") + " in Abstract.");
    announce("Change saved in Abstract.");
    if (p.kind === "fieldset") { S.fsets = null; loadFieldsets(); if (p.op === "create" && r.id) S.fsEdit = null; }
    else loadViewsTab(true);
  } catch (e) { $("#rv-err").textContent = e.message; }
  finally { working(b, false); }
}
$("#rv-cancel").addEventListener("click", closeReview);
$("#rv-go").addEventListener("click", confirmReview);
async function plan(route, body) {
  if (!S.tenant) { openConnect(); return; }
  try { openReview(await api(route, {body: body})); } catch (e) { toast(e.message, true); }
}

/* ---------- fieldsets ---------- */
async function loadFieldsets() {
  const out = $("#fs-list");
  if (!S.tenant) { out.innerHTML = '<div class="empty">Connect to see this tenant\'s fieldsets.</div>'; return; }
  out.innerHTML = '<div class="hint"><span class="spin" aria-hidden="true"></span>Reading fieldsets…</div>';
  try {
    S.fsets = (await api("fieldsets")).fieldsets || [];
    if (!S.fsets.length) { out.innerHTML = '<div class="empty">No fieldsets on this tenant yet. Build one on the right: pick a dataset, choose its columns, and save it to Abstract.</div>'; fillViewFieldsets(); return; }
    out.innerHTML = '<div class="tbl-wrap"><table class="list"><tbody>' + S.fsets.map(f => "<tr><td><b>" + esc(f.name) + "</b>" +
      '<div class="hint">' + esc((f.share_level || "private") + " · " + f.fields.length + " columns" + (f.created_by ? " · " + f.created_by : "")) + "</div>" +
      '<div class="tags">' + f.fields.slice(0, 8).map(x => '<span class="tag">' + esc(x) + "</span>").join("") + (f.fields.length > 8 ? '<span class="tag">+' + (f.fields.length - 8) + "</span>" : "") + "</div></td>" +
      '<td class="acts"><button class="lbtn" type="button" data-fs-edit="' + esc(f.id) + '">Edit</button>' +
      '<button class="lbtn" type="button" data-fs-use="' + esc(f.id) + '">Use as export fields</button>' +
      '<button class="lbtn danger" type="button" data-fs-del="' + esc(f.id) + '">Delete</button></td></tr>').join("") + "</tbody></table></div>";
    fillViewFieldsets();
  } catch (e) { out.innerHTML = '<div class="banner bad small">' + esc(e.message) + "</div>"; }
}
function fsReset() {
  S.fsEdit = null; S.fsSel = []; $("#fs-title").textContent = "New fieldset"; $("#fs-save").textContent = "Review and save to Abstract";
  ["#fs-name", "#fs-desc"].forEach(s => { $(s).value = ""; }); $("#fs-share").value = "private"; renderSel();
}
function fsFill(f) {
  S.fsEdit = f; S.fsSel = f.fields.slice(); $("#fs-title").textContent = "Edit fieldset: " + f.name; $("#fs-save").textContent = "Review changes";
  $("#fs-name").value = f.name || ""; $("#fs-desc").value = f.description || ""; $("#fs-share").value = f.share_level === "public" ? "public" : "private"; renderSel();
}
function renderSel() {
  const ol = $("#fs-sel"); ol.innerHTML = "";
  S.fsSel.forEach((f, i) => {
    const li = document.createElement("li"); li.draggable = true; li.tabIndex = 0; li.dataset.i = String(i);
    const ok = !S.fields.length || S.fields.includes(f) || STORED.test(f);
    if (!ok) li.classList.add("warnf");
    li.setAttribute("aria-label", f + ", column " + (i + 1) + " of " + S.fsSel.length + (ok ? "" : ", not a stored field on this tenant"));
    li.innerHTML = '<span class="grip" aria-hidden="true">⋮⋮</span><span class="nm"></span>' +
      (S.fsCov[f] != null ? '<span class="cov">' + esc(Number(S.fsCov[f]).toFixed(0)) + "%</span>" : "") +
      '<button type="button" data-mv="-1" aria-label="Move ' + esc(f) + ' up">↑</button><button type="button" data-mv="1" aria-label="Move ' + esc(f) + ' down">↓</button>' +
      '<button type="button" data-rm="1" aria-label="Remove ' + esc(f) + '">×</button>';
    $(".nm", li).textContent = visible(f);
    ol.appendChild(li);
  });
  $$("#fs-pool input[type=checkbox]").forEach(cb => { cb.checked = S.fsSel.includes(cb.value); });
}
function moveSel(i, d) {
  const j = i + d; if (j < 0 || j >= S.fsSel.length) return;
  const x = S.fsSel.splice(i, 1)[0]; S.fsSel.splice(j, 0, x); renderSel();
  const li = $('#fs-sel li[data-i="' + j + '"]'); if (li) li.focus();
  announce(x + " moved to column " + (j + 1));
}
$("#fs-sel").addEventListener("click", e => {
  const li = e.target.closest("li"); if (!li) return; const i = Number(li.dataset.i);
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.rm) { S.fsSel.splice(i, 1); renderSel(); }
  else moveSel(i, Number(b.dataset.mv));
});
$("#fs-sel").addEventListener("keydown", e => {
  const li = e.target.closest("li"); if (!li || e.target !== li) return;
  if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); moveSel(Number(li.dataset.i), e.key === "ArrowUp" ? -1 : 1); }
  if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); S.fsSel.splice(Number(li.dataset.i), 1); renderSel(); }
});
let dragI = null;
$("#fs-sel").addEventListener("dragstart", e => { const li = e.target.closest("li"); if (!li) return; dragI = Number(li.dataset.i); li.classList.add("drag"); e.dataTransfer.effectAllowed = "move"; });
$("#fs-sel").addEventListener("dragend", e => { const li = e.target.closest("li"); if (li) li.classList.remove("drag"); });
$("#fs-sel").addEventListener("dragover", e => { e.preventDefault(); });
$("#fs-sel").addEventListener("drop", e => {
  e.preventDefault(); const li = e.target.closest("li"); if (dragI == null) return;
  const to = li ? Number(li.dataset.i) : S.fsSel.length - 1; const x = S.fsSel.splice(dragI, 1)[0]; S.fsSel.splice(to, 0, x); dragI = null; renderSel();
});
function fsAdd(name) {
  name = (name || "").trim(); if (!name) return;
  if (S.fields.length && !S.fields.includes(name) && !STORED.test(name)) {
    $("#fs-add-hint").className = "hint warn";
    $("#fs-add-hint").textContent = name + " is not a stored field on this tenant. A fieldset holds stored fields only (your tenant's fields, ext.* and event.parsed.*); a column computed by KQL cannot go in one.";
    return;
  }
  $("#fs-add-hint").className = "hint";
  $("#fs-add-hint").textContent = "Stored fields only: your tenant's fields, ext.* and event.parsed.*. A column computed by KQL cannot go in a fieldset.";
  if (!S.fsSel.includes(name)) S.fsSel.push(name); renderSel();
}
$("#fs-add-b").addEventListener("click", () => { fsAdd($("#fs-add").value); $("#fs-add").value = ""; });
$("#fs-add").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); fsAdd($("#fs-add").value); $("#fs-add").value = ""; } });
$("#fs-profile").addEventListener("click", async () => {
  if (!S.tenant) { openConnect(); return; }
  const want = $("#fs-ds").value.trim(), ds = (S.datasets || []).find(d => d.name.toLowerCase() === want.toLowerCase());
  if (!ds) { $("#fs-ds-hint").textContent = want ? "No dataset called " + want + " on this tenant. Start typing to pick one." : "Pick a dataset first."; return; }
  const b = $("#fs-profile"); working(b, true, "Learning");
  $("#fs-ds-hint").innerHTML = '<span class="spin" aria-hidden="true"></span>Sampling recent ' + esc(ds.name) + " events (10 to 30 seconds)…";
  try {
    const p = await api("dataset-fields?dataset=" + encodeURIComponent(ds.name) + "&refresh=1");
    if (!p.sampled) { $("#fs-ds-hint").textContent = "No events from " + ds.name + " in the last 30 days, so there is nothing to learn fields from."; $("#fs-pool").innerHTML = ""; return; }
    p.fields.forEach(f => { S.fsCov[f.name] = f.coverage; });
    $("#fs-ds-hint").textContent = p.fields.length + " fields seen in " + fmt(p.sampled) + " sampled events. Percent is how often each is filled. The suggested ones are ticked if you have no columns yet.";
    if (!S.fsSel.length) S.fsSel = p.suggested.filter(f => f !== "id");
    $("#fs-pool").innerHTML = '<div class="pool" role="group" aria-label="Fields of ' + esc(ds.name) + '">' + p.fields.map(f =>
      '<label><input type="checkbox" value="' + esc(f.name) + '"> <span>' + esc(f.name) + '</span><span class="covbar" aria-hidden="true"><i data-w="' +
      esc(Math.max(0, Math.min(100, f.coverage))) + '"></i></span><span class="pct">' + esc(Number(f.coverage).toFixed(f.coverage < 10 ? 1 : 0)) + "%</span></label>").join("") + "</div>";
    $$("#fs-pool .covbar i").forEach(i => { i.style.width = i.dataset.w + "%"; });
    renderSel();
  } catch (e) { $("#fs-ds-hint").textContent = e.message; }
  finally { working(b, false); }
});
$("#fs-pool").addEventListener("change", e => {
  const cb = e.target.closest("input[type=checkbox]"); if (!cb) return;
  if (cb.checked && !S.fsSel.includes(cb.value)) S.fsSel.push(cb.value);
  if (!cb.checked) S.fsSel = S.fsSel.filter(x => x !== cb.value);
  renderSel();
});
$("#fs-new").addEventListener("click", () => { fsReset(); $("#fs-name").focus(); });
$("#fs-clear").addEventListener("click", fsReset);
$("#fs-reload").addEventListener("click", () => { S.fsets = null; loadFieldsets(); });
$("#fs-useexport").addEventListener("click", () => useAsExport(S.fsSel));
function useAsExport(fields) {
  if (!fields.length) { toast("Pick some columns first.", true); return; }
  S.spec.fields = Array.from(new Set(["@timestamp", "id"].concat(fields))); S.spec.full_documents = false; $("#fulldocs").checked = false;
  renderFieldChips(); renderFieldGrid(); changed(); toast(S.spec.fields.length + " export fields set from the fieldset.");
}
$("#fs-save").addEventListener("click", () => {
  const data = {name: $("#fs-name").value.trim(), description: $("#fs-desc").value.trim() || null, share_level: $("#fs-share").value, fields: S.fsSel.slice()};
  if (!data.name) { toast("Give the fieldset a name.", true); $("#fs-name").focus(); return; }
  if (!data.fields.length) { toast("Add at least one column.", true); return; }
  plan("fieldset-plan", S.fsEdit ? {op: "update", id: S.fsEdit.id, data: data} : {op: "create", data: data});
});
$("#fs-list").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  const id = b.dataset.fsEdit || b.dataset.fsUse || b.dataset.fsDel, f = (S.fsets || []).find(x => x.id === id); if (!f) return;
  if (b.dataset.fsEdit) { fsFill(f); $("#fs-name").focus(); }
  if (b.dataset.fsUse) useAsExport(f.fields);
  if (b.dataset.fsDel) plan("fieldset-plan", {op: "delete", id: id});
});
function fillViewFieldsets() {
  const sel = $("#vw-fs"), cur = sel.value;
  sel.innerHTML = '<option value="">The export fields of this search</option>' + (S.fsets || []).map(f => '<option value="' + esc(f.id) + '">Fieldset: ' + esc(f.name) + "</option>").join("");
  sel.value = cur;
}

/* ---------- saved views: open, save, update, delete ---------- */
async function loadViewsTab(force) {
  const out = $("#vw-list");
  if (!S.tenant) { out.innerHTML = '<div class="empty">Connect to see this tenant\'s saved views.</div>'; return; }
  if (S.viewsTab && !force) { renderViewsTab(); return; }
  out.innerHTML = '<div class="hint"><span class="spin" aria-hidden="true"></span>Reading saved views…</div>';
  if (!S.fsets) loadFieldsets();
  try { S.viewsTab = (await api("views")).views || []; S.views = S.viewsTab; renderViewsTab(); }
  catch (e) { out.innerHTML = '<div class="banner bad small">' + esc(e.message) + "</div>"; }
}
const ORIGIN = {kql: "KQL", filters: "filter builder", replay: "replay", none: "columns only", unreadable: "could not be read"};
function renderViewsTab() {
  const q = $("#vw-filter").value.trim().toLowerCase(), vs = (S.viewsTab || []).filter(v => !q || v.name.toLowerCase().includes(q));
  if (!S.viewsTab.length) { $("#vw-list").innerHTML = '<div class="empty">No saved views on this tenant yet. Save the current search as one above.</div>'; return; }
  $("#vw-list").innerHTML = '<div class="hint">' + esc(vs.length + " of " + S.viewsTab.length + " saved views") + '</div><div class="tbl-wrap"><table class="list"><tbody>' +
    vs.map(v => "<tr><td><b>" + esc(v.name) + '</b><div class="hint">' + esc((ORIGIN[v.origin] || "KQL") + (v.columns ? " · " + v.columns + " columns" : "") +
      (v.problem ? " · " + v.problem : "")) + '</div><div class="vupd" data-vf="' + esc(v.id) + '" hidden></div></td>' +
      '<td class="acts"><button class="lbtn" type="button" data-vw-open="' + esc(v.id) + '"' + (v.problem ? " disabled" : "") + ">Open</button>" +
      '<button class="lbtn" type="button" data-vw-upd="' + esc(v.id) + '">Update…</button>' +
      '<button class="lbtn danger" type="button" data-vw-del="' + esc(v.id) + '">Delete</button></td></tr>').join("") + "</tbody></table></div>";
}
$("#vw-filter").addEventListener("input", () => { if (S.viewsTab) renderViewsTab(); });
$("#vw-reload").addEventListener("click", () => loadViewsTab(true));
$("#vw-save").addEventListener("click", () => {
  readSpec(); const name = $("#vw-name").value.trim();
  if (!name) { toast("Name the view first.", true); $("#vw-name").focus(); return; }
  const fs = $("#vw-fs").value;
  plan("view-plan", {op: "create", spec: S.spec, name: name, fieldset_ids: fs ? [fs] : null});
});
$("#vw-list").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b || b.disabled) return;
  if (b.dataset.vwOpen) {
    working(b, true, "Opening");
    try { const r = await api("import", {body: {view_id: b.dataset.vwOpen}}); S.validatedKey = null; S.zeroKey = null; writeSpec(r.spec); if (S.tenant) loadProfile(false);
      toast(r.columns_only ? "Loaded the view's columns and time range. Add a dataset or a filter, then Preview." : "Opened the view. Choose Preview or Validate."); }
    catch (err) { toast(err.message, true); } finally { working(b, false); }
  }
  if (b.dataset.vwDel) plan("view-plan", {op: "delete", id: b.dataset.vwDel});
  if (b.dataset.vwUpd) {
    const id = b.dataset.vwUpd, box = $('.vupd[data-vf="' + id + '"]'), v = S.viewsTab.find(x => x.id === id);
    if (!box.hidden) { box.hidden = true; return; }
    box.innerHTML = '<label class="f" for="vu-n-' + esc(id) + '">Name</label><input type="text" id="vu-n-' + esc(id) + '" autocomplete="off">' +
      '<label class="chk"><input type="checkbox" id="vu-s-' + esc(id) + '"> Replace its search with the search on the left</label>' +
      '<label class="f" for="vu-f-' + esc(id) + '">Columns</label><select id="vu-f-' + esc(id) + '"><option value="keep">Leave its columns as they are</option>' +
      (S.fsets || []).map(f => '<option value="' + esc(f.id) + '">Fieldset: ' + esc(f.name) + "</option>").join("") + "</select>" +
      '<div class="row gap"><button class="tbtn primary" type="button" data-vu-go="' + esc(id) + '">Review the update</button></div>';
    $("#vu-n-" + id).value = v ? v.name : ""; box.hidden = false; $("#vu-n-" + id).focus();
  }
  if (b.dataset.vuGo) {
    const id = b.dataset.vuGo, v = S.viewsTab.find(x => x.id === id), name = $("#vu-n-" + id).value.trim();
    const body = {op: "update", id: id};
    if (name && v && name !== v.name) body.name = name;
    if ($("#vu-s-" + id).checked) { readSpec(); body.spec = S.spec; }
    const f = $("#vu-f-" + id).value; if (f !== "keep") body.fieldset_ids = [f];
    plan("view-plan", body);
  }
});

/* ---------- platform endpoints ---------- */
async function loadEndpoints() {
  const out = $("#ep-out");
  try {
    const r = await api("endpoints"), P = {ok: "PASS", off: "WARN", error: "FAIL", unknown: "INFO"};
    const W = {ok: "works", off: "not on this tenant", error: "failed", unknown: "not used yet"};
    out.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th scope="col">Endpoint</th><th scope="col">Used for</th><th scope="col">Needed</th><th scope="col">This session</th></tr></thead><tbody>' +
      r.endpoints.map(x => '<tr><td class="mono">' + esc(x.method + " " + x.path) + "</td><td>" + esc(x.use) + "</td><td>" + (x.optional ? "optional" : "required") +
        '</td><td><span class="pill ' + (P[x.status] || "INFO") + '">' + esc(W[x.status] || x.status) + '</span> <span class="hint">' + esc(x.detail) + "</span></td></tr>").join("") +
      "</tbody></table></div>";
  } catch (e) { out.textContent = e.message; }
}
$("#b-endpoints").addEventListener("click", loadEndpoints);

/* ---------- keyboard: / focuses the search, Ctrl+Enter previews; dialogs close on Escape and keep focus ---------- */
document.addEventListener("keydown", e => {
  const inField = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "");
  const overlay = $$(".overlay").some(o => !o.hidden);
  if (e.key === "/" && !inField && !overlay && !e.ctrlKey && !e.metaKey) {
    e.preventDefault(); const el = $({kql: "#kql-text", nl: "#nl-text"}[S.qtab] || "#dataset"); if (el) el.focus();
  }
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !overlay) {
    e.preventDefault();
    if (e.target && e.target.id === "nl-text") return nlGo();
    const b = $("#a-preview"); if (b && !b.disabled) doPreview();
  }
  if (!$("#review").hidden) {
    if (e.key === "Escape") { e.preventDefault(); closeReview(); }
    if (e.key === "Tab") {
      const f = $$("#review button, #review summary").filter(x => !x.disabled && x.offsetParent !== null), first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
});
gutter();

/* ---------- 0.4.0: the search panel collapses (remembered in this browser) ---------- */
function sideOpen(open, focus, remember = true) {
  document.body.classList.toggle("side-closed", !open);
  $("#side-rail").hidden = open;
  $("#b-side").setAttribute("aria-expanded", open ? "true" : "false");
  $("#b-side").textContent = open ? "Hide search panel" : "Show search panel";
  $("#b-side-open").setAttribute("aria-expanded", open ? "true" : "false");
  if (remember) { try { if (open) localStorage.setItem("abx:side", "open"); else localStorage.setItem("abx:side", "closed"); } catch (e) { /* blocked */ } }
  if (focus) (open ? $("#b-side") : $("#b-side-open")).focus();
  if (window.abxLaunch && window.abxLaunch.on) window.dispatchEvent(new Event("resize"));
}
$("#b-side").addEventListener("click", () => sideOpen(document.body.classList.contains("side-closed"), true));
$("#b-side-open").addEventListener("click", () => sideOpen(true, true));
/* a narrow window (760 px or less) starts with the panel collapsed, so the results come first; a choice the person
   made in this browser wins */
(function () {
  let pref = null;
  try { pref = localStorage.getItem("abx:side"); } catch (e) { /* blocked */ }
  if (pref === "closed" || (pref !== "open" && window.matchMedia && window.matchMedia("(max-width:760px)").matches)) sideOpen(false, false, false);
})();
/* a shortcut that needs the panel opens it first */
document.addEventListener("keydown", e => {
  const inField = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "");
  if (e.key === "/" && !inField && document.body.classList.contains("side-closed")) sideOpen(true);
}, true);

/* ---------- dialogs added in 0.4.0: Escape closes, focus stays inside ---------- */
function trapIn(box, e) {
  const f = $$("button, input, select, textarea, summary, [tabindex]:not([tabindex='-1'])", box)
    .filter(x => !x.disabled && x.offsetParent !== null), first = f[0], last = f[f.length - 1];
  if (!first) return;
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}
document.addEventListener("keydown", e => {
  if (!$("#cc-review").hidden) {
    if (e.key === "Escape") { e.preventDefault(); ccClose(); }
    if (e.key === "Tab") trapIn($("#cc-review"), e);
  }
});

/* ---------- content as code: saved views and fieldsets as JSON (export reads; apply is review then confirm) ---------- */
S.cc = {what: "all", bundle: null, name: "", plan: null};
$("#cc-what").addEventListener("click", e => {
  const b = e.target.closest("button[data-v]"); if (!b) return;
  S.cc.what = b.dataset.v;
  $$("#cc-what button").forEach(x => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
});
document.addEventListener("click", e => {
  if (e.target.closest("[data-go-cc]")) { tab("views"); setTimeout(() => { const c = $("#cc"); c.scrollIntoView({block: "start"}); $("#cc-export").focus(); }, 40); }
});
function ccQuery() {
  /* views= / fieldsets= take names; with none, the app reads every view and fieldset and the page keeps the kind asked for */
  const names = $("#cc-names").value.split(",").map(x => x.trim()).filter(Boolean).join(",");
  if (!names) return "";
  const n = encodeURIComponent(names);
  return S.cc.what === "views" ? "?views=" + n : S.cc.what === "fieldsets" ? "?fieldsets=" + n : "?views=" + n + "&fieldsets=" + n;
}
function saveJson(obj, name) {
  const blob = new Blob([JSON.stringify(obj, null, 2) + "\n"], {type: "application/json"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
function stamp() { return new Date().toISOString().replace(/[-:]/g, "").slice(0, 15) + "Z"; }
$("#cc-export").addEventListener("click", async () => {
  if (!S.tenant) return openConnect();
  const b = $("#cc-export"), out = $("#cc-ex-out"); working(b, true, "Exporting");
  try {
    const r = await api("content-export" + ccQuery());
    const bundle = r.bundle || r;
    if (S.cc.what === "views") { bundle.fieldsets = []; if (bundle.source_ids) bundle.source_ids.fieldsets = {}; }
    if (S.cc.what === "fieldsets") { bundle.views = []; if (bundle.source_ids) bundle.source_ids.views = {}; }
    const c = {views: (bundle.views || []).length, fieldsets: (bundle.fieldsets || []).length};
    const fname = "abstract-content-" + String(S.tenant.vendor_account_id || "tenant") + "-" + stamp() + ".json";
    saveJson(bundle, fname);
    out.innerHTML = '<div class="banner ok small">Exported ' + esc(fmt(c.views || 0)) + " views and " + esc(fmt(c.fieldsets || 0)) +
      " fieldsets. Your browser saved " + esc(fname) + "." + (r.dir ? '</div><div class="hint">The app also wrote what it read as one file per item, ready for git, to <span class="mono">' + esc(r.dir) + "</span>." : "") + "</div>" +
      ((bundle.problems || []).length ? '<div class="banner warn small">' + esc(bundle.problems.length + " could not be exported: " + bundle.problems.map(x => typeof x === "string" ? x : (x.name || "") + (x.reason ? " (" + x.reason + ")" : "")).join("; ")) + "</div>" : "");
    announce("Bundle exported.");
  } catch (e) { out.innerHTML = '<div class="banner bad small">' + esc(e.message) + "</div>"; }
  finally { working(b, false); }
});
async function ccLoad(file) {
  const out = $("#cc-diff-out");
  S.cc.bundle = null; S.cc.name = ""; $("#cc-diff").disabled = true; $("#cc-plan").disabled = true;
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) { out.innerHTML = '<div class="banner bad small">That file is over 20 MB. A bundle of views and fieldsets is far smaller: check it is the right file.</div>'; return; }
  try {
    const obj = JSON.parse(await file.text());
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new Error("not a JSON object");
    S.cc.bundle = obj; S.cc.name = file.name;
    $("#cc-drop b").textContent = file.name;
    $("#cc-diff").disabled = false; $("#cc-plan").disabled = false;
    out.innerHTML = '<div class="hint">Loaded ' + esc(file.name) + ". Compare it with this tenant, or go straight to the review.</div>";
  } catch (e) {
    out.innerHTML = '<div class="banner bad small">' + esc(file.name) + " is not a bundle this tool can read (" + esc(e.message) + "). Choose the JSON file Export bundle saved.</div>";
  }
}
$("#cc-file").addEventListener("change", e => ccLoad(e.target.files && e.target.files[0]));
const drop = $("#cc-drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => ccLoad(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]));
const CC_WORD = {same: "same", changed: "changed", new: "new here", only_in_tenant: "only on this tenant", ambiguous: "two with this name", delete: "would be deleted"};
function ccItems(r) { return Array.isArray(r) ? r : (r.items || r.diff || r.changes || []); }
$("#cc-diff").addEventListener("click", async () => {
  if (!S.tenant) return openConnect();
  if (!S.cc.bundle) return;
  const b = $("#cc-diff"), out = $("#cc-diff-out"); working(b, true, "Comparing");
  try {
    const items = ccItems(await api("content-diff", {body: {bundle: S.cc.bundle}}));
    const n = k => items.filter(x => x.status === k).length;
    out.innerHTML = '<p class="hint">' + esc(items.length + " items: " + n("new") + " new here, " + n("changed") + " changed, " + n("same") + " the same, " +
      n("only_in_tenant") + " only on this tenant (kept unless you tick the delete option).") + "</p>" +
      (items.length ? '<div class="tbl-wrap"><table class="list diff"><thead><tr><th>Item</th><th>Status</th><th>What differs</th></tr></thead><tbody>' +
        items.map(x => "<tr><td><b>" + esc(x.name) + '</b><div class="hint">' + esc(x.kind === "view" ? "saved view" : x.kind) + "</div></td>" +
          '<td><span class="st ' + esc(x.status) + '">' + esc(CC_WORD[x.status] || x.status) + "</span></td><td>" +
          esc(x.reason || (x.changes || []).map(c => typeof c === "string" ? c : (c.key || "")).filter(Boolean).join(", ") || (x.status === "same" ? "nothing" : "")) + "</td></tr>").join("") +
        "</tbody></table></div>" : '<div class="empty">The bundle is empty.</div>');
  } catch (e) { out.innerHTML = '<div class="banner bad small">' + esc(e.message) + "</div>"; }
  finally { working(b, false); }
});
let ccReturn = null;
function ccClose() { $("#cc-review").hidden = true; S.cc.plan = null; if (ccReturn && ccReturn.focus) ccReturn.focus(); }
$("#cc-plan").addEventListener("click", async () => {
  if (!S.tenant) return openConnect();
  if (!S.cc.bundle) return;
  const prune = $("#cc-prune").checked, b = $("#cc-plan"); working(b, true, "Planning");
  try {
    const r = await api("content-plan", {body: {bundle: S.cc.bundle, prune: prune}});
    S.cc.plan = r; ccReturn = b;
    const items = r.items || [];
    const dels = items.filter(p => p.op === "delete");
    let html = "";
    if (dels.length) html += '<div class="prune-warn">This deletes ' + dels.length + " item" + (dels.length === 1 ? "" : "s") + " from Abstract for everyone on this tenant: " +
      esc(dels.map(p => p.name).join(", ")) + ". This tool cannot bring them back.</div>";
    const pw = (r.warnings || []).filter(w => !/^PRUNE/.test(w));
    if (pw.length) html += "<ul>" + pw.map(x => '<li class="note">' + esc(x) + "</li>").join("") + "</ul>";
    if ((r.skipped || []).length) html += '<p class="hint">Left out: ' + esc(r.skipped.map(x => typeof x === "string" ? x : (x.name || "") + (x.reason ? " (" + x.reason + ")" : "")).join("; ")) + "</p>";
    html += items.length ? "<ol>" + items.map(p => {
      const what = KIND_WORD[p.kind] || p.kind;
      const head = "<b>" + esc({create: "Create", update: "Update", delete: "Delete"}[p.op] + " " + what) + "</b> " + esc(p.name || "") +
        ' <span class="mono hint">' + esc(p.request || "") + "</span>";
      const ch = p.op === "update" ? "<ul>" + (p.changes || []).map(changeHtml).join("") + "</ul>" : "";
      const w = (p.warnings || []).length ? "<ul>" + p.warnings.map(x => '<li class="note">' + esc(x) + "</li>").join("") + "</ul>" : "";
      const vis = p.visibility ? '<div class="hint vis">Who sees it: ' + esc(p.visibility) + "</div>" : "";
      return "<li>" + head + ch + vis + w + "</li>";
    }).join("") + "</ol>" : '<div class="empty">Nothing to change: this tenant already matches the bundle.</div>';
    html += '<p class="hint">One confirm applies every item above, in order. It stops at the first one that fails and says which were done. The review works once and for ' +
      Math.round((r.expires_in || 600) / 60) + " minutes, and each item is read again just before it is changed.</p>";
    $("#cr-title").textContent = "Review the bundle: " + (S.cc.name || "content");
    $("#cr-body").innerHTML = html; $("#cr-err").textContent = "";
    const go = $("#cr-go");
    go.disabled = !items.length;
    go.textContent = dels.length ? "Apply, including " + dels.length + " delete" + (dels.length === 1 ? "" : "s") : "Apply " + items.length + " change" + (items.length === 1 ? "" : "s");
    go.className = "tbtn " + (dels.length ? "danger" : "primary");
    $("#cc-review").hidden = false; $("#cr-cancel").focus();
  } catch (e) { $("#cc-diff-out").innerHTML = '<div class="banner bad small">' + esc(e.message) + "</div>"; }
  finally { working(b, false); }
});
$("#cr-cancel").addEventListener("click", ccClose);
$("#cr-go").addEventListener("click", async () => {
  const p = S.cc.plan; if (!p) return;
  const b = $("#cr-go"); working(b, true, "Applying");
  try {
    const r = await api("content-apply", {body: {token: p.token}});
    const done = r.done || [], failed = r.failed ? [].concat(r.failed) : [], rest = r.not_attempted || [];
    ccClose();
    $("#cc-diff-out").innerHTML = '<div class="banner ' + (failed.length ? "warn" : "ok") + ' small">' +
      esc("Applied " + done.length + " change" + (done.length === 1 ? "" : "s") +
        (failed.length ? ". Stopped at " + failed.map(f => f.op + " " + (f.name || "") + (f.error ? ": " + f.error : "")).join("; ") : "") +
        (rest.length ? ". Not attempted: " + rest.map(x => x.op + " " + x.name).join(", ") : "") + ".") + "</div>";
    announce("Bundle applied.");
    S.fsets = null; if ($("#v-views").classList.contains("on")) loadViewsTab(true);
  } catch (e) { $("#cr-err").textContent = e.message; }
  finally { working(b, false); }
});

/* ---------- automation: the current search as a scheduled job ---------- */
$("#au-go").addEventListener("click", async () => {
  if (!S.spec) return;
  if (!S.tenant) return openConnect();
  readSpec();
  const b = $("#au-go"), out = $("#au-out");
  const at = $("#au-at").value.trim() || "00:20";
  if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(at)) { out.innerHTML = '<div class="banner bad small">Give the time as HH:MM in UTC, for example 00:20.</div>'; $("#au-at").focus(); return; }
  working(b, true, "Generating");
  try {
    const r = await api("automation", {body: {spec: S.spec, target: $("#au-target").value,
      options: {every: $("#au-when").value, at: at, job: $("#au-job").value.trim() || null, save: $("#au-save").checked}}});
    S.auFiles = r.files || [];
    const mode = m => typeof m === "number" ? m.toString(8) : String(m || "");
    out.innerHTML = (r.dir ? '<div class="banner ok small">Also written to <span class="mono">' + esc(r.dir) + "</span></div>" : "") +
      '<div class="files">' + S.auFiles.map((f, i) => '<div class="file"><div class="fh"><span>' + esc(f.path) + "</span>" +
      (f.mode ? '<span class="hint">mode ' + esc(mode(f.mode)) + "</span>" : "") + '<span class="sp"></span>' +
      '<button class="lbtn" type="button" data-au-copy="' + i + '">Copy</button><button class="lbtn" type="button" data-au-save="' + i + '">Download</button></div>' +
      '<pre tabindex="0">' + esc(f.text) + "</pre></div>").join("") + "</div>" +
      ((r.steps || []).length ? '<div class="f">Then</div><ol class="steps">' + r.steps.map(x => "<li>" + esc(x) + "</li>").join("") + "</ol>" : "") +
      ((r.notes || []).length ? r.notes.map(x => '<p class="hint note">' + esc(x) + "</p>").join("") : "");
    announce("Generated " + S.auFiles.length + " file" + (S.auFiles.length === 1 ? "" : "s") + ".");
  } catch (e) { out.innerHTML = '<div class="banner bad small">' + esc(e.message) + "</div>"; }
  finally { working(b, false); }
});
$("#au-out").addEventListener("click", async e => {
  const c = e.target.closest("[data-au-copy]"), s = e.target.closest("[data-au-save]");
  const f = (S.auFiles || [])[Number((c || s || {dataset: {}}).dataset.auCopy || (c || s || {dataset: {}}).dataset.auSave)];
  if (!f) return;
  if (c) { try { await navigator.clipboard.writeText(f.text); toast("Copied " + f.path + "."); } catch (err) { toast("Select the text and copy it."); } }
  if (s) {
    const blob = new Blob([f.text], {type: "text/plain"}); const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = f.path.split("/").pop(); document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
});

/* the shortcut hint names the key this computer uses: ⌘ on a Mac, Ctrl elsewhere */
(function () {
  const k = document.getElementById("kbd-mod");
  if (k && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "")) k.textContent = "⌘";
})();
