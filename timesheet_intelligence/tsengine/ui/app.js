/* Timesheet Intelligence — calendar UI (vanilla JS, no build step). */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["", "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

let REPORT = null;
let SELECTED = 0;

function fmtH(v) { return v === null || v === undefined ? "—" : (Math.round(v * 100) / 100); }

async function loadLatest() {
  try {
    const r = await fetch("/api/report");
    if (r.ok) { REPORT = await r.json(); render(); }
  } catch (e) { /* no report yet */ }
}

async function runProcess() {
  const folder = $("#folder").value.trim();
  const month = +$("#month").value, year = +$("#year").value;
  if (!folder) { setStatus("Enter a folder path", true); return; }
  setStatus('<span class="spinner"></span> processing…');
  $("#run").disabled = true;
  try {
    const r = await fetch("/api/process", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, month, year })
    });
    if (!r.ok) { const e = await r.json(); setStatus("Error: " + (e.detail || r.status), true); return; }
    REPORT = await r.json();
    SELECTED = 0;
    setStatus("Done · " + new Date().toLocaleTimeString());
    render();
  } catch (e) { setStatus("Error: " + e.message, true); }
  finally { $("#run").disabled = false; }
}

function setStatus(html, err) {
  const el = $("#status"); el.innerHTML = html; el.style.color = err ? "var(--flag)" : "var(--muted)";
}

function render() {
  if (!REPORT) return;
  renderSummary();
  renderEmployeeList();
  renderDetail();
}

function renderSummary() {
  const s = REPORT.stats ? REPORT.stats : computeStats();
  const rows = [
    ["Period", `${MONTHS[REPORT.month]} ${REPORT.year}`],
    ["Employees", REPORT.employees.length],
    ["Files processed", `${REPORT.files_processed}/${REPORT.files_seen}`],
    ["Total hours", fmtH(REPORT.employees.reduce((a, e) => a + (e.monthly_total || 0), 0))],
    ["Issues flagged", REPORT.employees.reduce((a, e) => a + countIssues(e), 0)],
    ["Unprocessed", (REPORT.unprocessed || []).length],
    ["LLM used", REPORT.llm_used ? "yes" : "no (deterministic)"],
  ];
  $("#summary").innerHTML = rows.map(([k, v]) =>
    `<div class="stat-row"><span>${k}</span><b>${v}</b></div>`).join("");
}
function computeStats() { return {}; }
function countIssues(e) {
  let n = (e.issues || []).length;
  (e.days || []).forEach(d => n += (d.issues || []).length);
  return n;
}

function renderEmployeeList() {
  const host = $("#employees");
  if (!REPORT.employees.length) { host.innerHTML = `<div class="emp-item"><span class="sub">No employees found</span></div>`; return; }
  // alphabetical by employee name; keep original index for selection/data
  const ordered = REPORT.employees.map((e, i) => ({ e, i }))
    .sort((a, b) => (a.e.employee_name || "Unknown")
      .localeCompare(b.e.employee_name || "Unknown", undefined, { sensitivity: "base" }));
  host.innerHTML = ordered.map(({ e, i }) => {
    const issues = countIssues(e);
    return `<div class="emp-item ${i === SELECTED ? "active" : ""}" data-i="${i}">
      <div><div class="nm">${esc(e.employee_name || "Unknown")}</div>
      <div class="sub">${esc((e.clients || []).join(", ") || "—")}</div>
      ${issues ? `<div class="warn">⚑ ${issues} flag(s)</div>` : ""}</div>
      <div class="hrs">${fmtH(e.monthly_total)}h</div></div>`;
  }).join("");
  $$(".emp-item", host).forEach(el => el.onclick = () => {
    SELECTED = +el.dataset.i; render();
  });
}

function renderDetail() {
  const e = REPORT.employees[SELECTED];
  const host = $("#detail");
  if (!e) {
    host.innerHTML = `<div class="empty-state">No employee selected.</div>`;
    if ((REPORT.unprocessed || []).length) host.innerHTML += renderUnprocessed();
    return;
  }
  host.innerHTML = renderHeader(e) + renderLegend() + renderCalendar(e) +
    renderBreakdown(e) + renderIssues(e) + renderUnprocessed();
  bindCells(e);
}

function renderHeader(e) {
  const chips = []
    .concat((e.clients || []).map(c => `<span class="chip">🏢 ${esc(c)}</span>`))
    .concat((e.projects || []).slice(0, 4).map(p => `<span class="chip">📁 ${esc(p)}</span>`))
    .concat((e.extraction_methods || []).map(m => `<span class="chip">⚙︎ ${esc(m)}</span>`));
  const nfiles = (e.source_files || []).length;
  return `<div class="emp-header"><div class="top">
    <div><h2>${esc(e.employee_name || "Unknown employee")}</h2>
      <div class="sub" style="color:var(--muted)">${e.employee_id ? "ID " + esc(e.employee_id) + " · " : ""}
        ${e.days_worked} day(s) worked · confidence ${e.confidence}</div>
      <div class="chips">${chips.join("")}</div></div>
    <div class="totals">
      <div class="t reg"><div class="v">${fmtH(e.monthly_regular)}</div><div class="l">Regular</div></div>
      <div class="t ot"><div class="v">${fmtH(e.monthly_overtime)}</div><div class="l">Overtime</div></div>
      <div class="t tot"><div class="v">${fmtH(e.monthly_total)}</div><div class="l">Total hrs</div></div>
    </div></div>
    ${nfiles ? `<button class="primary pv-btn" onclick="openPreview(${SELECTED})">📄 Preview source${nfiles > 1 ? " (" + nfiles + ")" : ""}</button>` : ""}
    </div>`;
}

function renderLegend() {
  return `<div class="legend">
    <span><i class="dot" style="background:var(--panel2);border:1px solid var(--line)"></i> Worked</span>
    <span><i class="dot" style="background:var(--weekend)"></i> Weekend</span>
    <span><i class="dot" style="background:rgba(163,113,247,.4)"></i> Holiday</span>
    <span><i class="dot" style="background:#6b4a1a"></i> Missing</span>
    <span><i class="dot" style="background:var(--ot)"></i> Overtime</span>
    <span><i class="dot" style="background:var(--flag)"></i> Flagged</span></div>`;
}

function renderCalendar(e) {
  const month = REPORT.month, year = REPORT.year;
  const first = new Date(year, month - 1, 1);
  let lead = (first.getDay() + 6) % 7; // Monday-first
  const byDom = {};
  (e.days || []).forEach(d => { byDom[+d.date.slice(8, 10)] = d; });
  const ndays = new Date(year, month, 0).getDate();

  let cells = "";
  for (let i = 0; i < lead; i++) cells += `<div class="cell empty"></div>`;
  for (let dom = 1; dom <= ndays; dom++) {
    const d = byDom[dom];
    if (!d) { cells += `<div class="cell empty"></div>`; continue; }
    const cls = ["cell"];
    if (d.is_weekend) cls.push("weekend");
    if (d.is_holiday) cls.push("holiday");
    const hasData = d.total_hours != null || d.regular_hours != null;
    const missing = !hasData && !d.is_weekend && !d.is_holiday;
    if (missing) cls.push("missing");
    if ((d.overtime_hours || 0) > 0) cls.push("ot");
    if ((d.issues || []).some(x => x.severity !== "info")) cls.push("flagged");
    let hrs = "";
    if (hasData) {
      hrs = `<div class="hrs"><span class="reg">${fmtH(d.regular_hours ?? d.total_hours)}h</span>` +
        ((d.overtime_hours || 0) > 0 ? ` <span class="ot">+${fmtH(d.overtime_hours)}</span>` : "") + `</div>`;
    } else if (d.is_holiday) hrs = `<div class="lbl">${esc(d.note || "Holiday")}</div>`;
    else if (d.is_weekend) hrs = `<div class="lbl wknd">Weekend</div>`;
    else hrs = `<div class="lbl wknd" style="color:#b8860b">— missing —</div>`;
    cells += `<div class="${cls.join(" ")}" data-dom="${dom}">
      <div class="dom">${dom}</div><div class="wd">${WD[(new Date(year, month - 1, dom).getDay() + 6) % 7]}</div>
      ${hrs}</div>`;
  }
  const heads = WD.map(w => `<div class="cal-head">${w}</div>`).join("");
  return `<div class="calendar"><div class="cal-grid">${heads}${cells}</div></div>`;
}

function bindCells(e) {
  const byDom = {};
  (e.days || []).forEach(d => { byDom[+d.date.slice(8, 10)] = d; });
  $$(".cell[data-dom]").forEach(el => el.onclick = () => openDay(byDom[+el.dataset.dom], e));
}

function renderBreakdown(e) {
  const bd = e.client_breakdown || [];
  if (!bd.length) return "";
  const rows = bd.map(b => `<tr><td>${esc(b.client || "—")}</td><td>${esc(b.project || "—")}</td>
    <td>${fmtH(b.regular_hours)}</td><td>${fmtH(b.overtime_hours)}</td>
    <td><b>${fmtH(b.total_hours)}</b></td><td>${b.days_worked}</td></tr>`).join("");
  return `<div class="section"><h3>Client / Project breakdown</h3>
    <table class="bd"><tr><th>Client</th><th>Project</th><th>Regular</th><th>OT</th><th>Total</th><th>Days</th></tr>
    ${rows}</table></div>`;
}

function renderIssues(e) {
  const all = [];
  (e.issues || []).forEach(i => all.push(i));
  (e.days || []).forEach(d => (d.issues || []).forEach(i => all.push(i)));
  (e.weekly_totals || []).forEach(w => { });
  if (!all.length) return `<div class="section"><h3>Data quality</h3><div class="sub" style="color:var(--ok)">✓ No issues flagged</div></div>`;
  const order = { error: 0, warning: 1, info: 2 };
  all.sort((a, b) => (order[a.severity] - order[b.severity]) || ((a.date || "") < (b.date || "") ? -1 : 1));
  const rows = all.map(i => `<div class="issue">
    <span class="badge ${i.severity}">${i.code}</span>
    <div><div class="msg">${i.date ? `<b>${i.date}</b> — ` : ""}${esc(i.message)}</div>
    ${(i.sources || []).length ? `<div class="src">${i.sources.map(s => srcLink(s)).join(" · ")}</div>` : ""}</div>
  </div>`).join("");
  return `<div class="section"><h3>Data quality · ${all.length} issue(s)</h3>${rows}</div>`;
}

function renderUnprocessed() {
  const u = REPORT.unprocessed || [];
  if (!u.length) return "";
  const rows = u.map(x => `<div class="issue"><span class="badge warning">SKIPPED</span>
    <div><div class="msg">${esc(x.file)}</div><div class="src">${esc(x.reason)}</div></div></div>`).join("");
  return `<div class="section"><h3>Unprocessed files · ${u.length}</h3>${rows}</div>`;
}

function srcLink(s) {
  const label = s.file + (s.sheet ? " · " + s.sheet : "") + (s.page ? " · p" + s.page : "") +
    (s.cell ? " · " + s.cell : (s.row ? " · row" + s.row : ""));
  return `<span class="src-link" onclick='openEvidence(${JSON.stringify(s)})'>${esc(label)}</span>`;
}

function openDay(d, e) {
  if (!d) return;
  const issues = (d.issues || []).map(i =>
    `<div class="issue"><span class="badge ${i.severity}">${i.code}</span><div class="msg">${esc(i.message)}</div></div>`).join("");
  const sources = (d.sources || []).map(s => srcLink(s)).join("<br/>") || "—";
  $("#modal-title").textContent = `${d.date} (${d.weekday})`;
  $("#modal-body").innerHTML = `
    <div class="kv">
      <div class="k">Regular</div><div>${fmtH(d.regular_hours)} h</div>
      <div class="k">Overtime</div><div>${fmtH(d.overtime_hours)} h</div>
      <div class="k">Total</div><div><b>${fmtH(d.total_hours)} h</b></div>
      <div class="k">Type</div><div>${d.is_holiday ? "Holiday — " + esc(d.note || "") : d.is_weekend ? "Weekend" : "Working day"}</div>
      <div class="k">Project</div><div>${esc(d.project || "—")}</div>
      <div class="k">Note</div><div>${esc(d.note || "—")}</div>
    </div>
    ${issues ? `<h4>Issues</h4>${issues}` : ""}
    <h4>Source evidence</h4><div class="src">${sources}</div>
    ${d.raw ? `<h4>Raw extracted</h4><div class="raw">${esc(d.raw)}</div>` : ""}`;
  showModal();
}

async function openEvidence(s) {
  const q = new URLSearchParams({ file: s.file });
  if (s.page) q.set("page", s.page);
  const url = "/api/evidence?" + q.toString();
  $("#modal-title").textContent = "Evidence · " + s.file;
  $("#modal-body").innerHTML = `<div class="kv">
      <div class="k">File</div><div>${esc(s.file)}</div>
      ${s.sheet ? `<div class="k">Sheet</div><div>${esc(s.sheet)}</div>` : ""}
      ${s.page ? `<div class="k">Page</div><div>${s.page}</div>` : ""}
      ${s.row ? `<div class="k">Row</div><div>${s.row}</div>` : ""}
      ${s.cell ? `<div class="k">Cell</div><div>${esc(s.cell)}</div>` : ""}
      ${s.extractor ? `<div class="k">Extractor</div><div>${esc(s.extractor)}</div>` : ""}
    </div>
    <a class="src-link" href="${url}" target="_blank">Open source file ↗</a>
    <div id="ev-img"></div>`;
  showModal();
  // try to show an image preview if the evidence is renderable
  try {
    const r = await fetch(url, { method: "GET" });
    const ct = r.headers.get("content-type") || "";
    if (ct.startsWith("image/")) {
      const blob = await r.blob();
      $("#ev-img").innerHTML = `<img src="${URL.createObjectURL(blob)}" />`;
    }
  } catch (e) { }
}
window.openEvidence = openEvidence;

/* ---- split-screen source preview (calendar left, PDF right) ---- */
function previewUrl(file) { return "/api/preview?file=" + encodeURIComponent(file); }

function openPreview(idx) {
  const e = REPORT.employees[idx]; if (!e) return;
  const files = e.source_files || []; if (!files.length) return;
  const sel = $("#pv-file");
  sel.innerHTML = files.map(f => `<option value="${esc(f)}">${esc(f.split("/").pop())}</option>`).join("");
  loadPreviewFile(files[0]);
  $("#preview-pane").classList.remove("hidden");
  $("#main").classList.add("split");
}
async function loadPreviewFile(file) {
  $("#pv-open").href = previewUrl(file);     // native PDF (full quality) in a new tab
  const frame = $("#pv-frame");
  frame.scrollTop = 0;
  frame.innerHTML = `<div class="pv-loading">Rendering preview…</div>`;
  try {
    const r = await fetch("/api/preview_pages?file=" + encodeURIComponent(file));
    if (!r.ok) throw new Error("render " + r.status);
    const d = await r.json();
    frame.innerHTML = (d.urls || []).map((u, i) =>
      `<img src="${u}" loading="lazy" alt="page ${i + 1}" />`).join("")
      || `<div class="pv-loading">empty preview</div>`;
    applyZoom();
  } catch (err) {
    frame.innerHTML = `<div class="pv-loading">Couldn't render an inline preview.<br/>
      <a class="src-link" href="${previewUrl(file)}" target="_blank">Open the PDF ↗</a></div>`;
  }
}
function closePreview() {
  $("#preview-pane").classList.add("hidden");
  $("#main").classList.remove("split");
  $("#pv-frame").innerHTML = "";
}
window.openPreview = openPreview;

/* ---- PDF zoom ---- */
let PV_ZOOM = 1;
function applyZoom() {
  $("#pv-frame").style.setProperty("--pv-zoom", PV_ZOOM);
  $("#pv-zoom-lvl").textContent = Math.round(PV_ZOOM * 100) + "%";
}
function pvZoom(factor) { PV_ZOOM = Math.min(4, Math.max(0.4, PV_ZOOM * factor)); applyZoom(); }
function pvZoomFit() { PV_ZOOM = 1; applyZoom(); }

function showModal() { $("#modal-bg").classList.add("show"); }
function hideModal() { $("#modal-bg").classList.remove("show"); }
function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

$("#run").onclick = runProcess;
$("#modal-close").onclick = hideModal;
$("#modal-bg").onclick = (e) => { if (e.target.id === "modal-bg") hideModal(); };
$("#pv-close").onclick = closePreview;
$("#pv-file").onchange = (e) => loadPreviewFile(e.target.value);
$("#pv-zoom-in").onclick = () => pvZoom(1.25);
$("#pv-zoom-out").onclick = () => pvZoom(0.8);
$("#pv-zoom-fit").onclick = pvZoomFit;
// ctrl/cmd + scroll to zoom inside the preview
$("#pv-frame").addEventListener("wheel", (e) => {
  if (e.ctrlKey || e.metaKey) { e.preventDefault(); pvZoom(e.deltaY < 0 ? 1.1 : 0.9); }
}, { passive: false });
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { if (!$("#preview-pane").classList.contains("hidden")) closePreview(); else hideModal(); }
  if ((e.ctrlKey || e.metaKey) && !$("#preview-pane").classList.contains("hidden")) {
    if (e.key === "=" || e.key === "+") { e.preventDefault(); pvZoom(1.25); }
    if (e.key === "-") { e.preventDefault(); pvZoom(0.8); }
    if (e.key === "0") { e.preventDefault(); pvZoomFit(); }
  }
});
loadLatest();
