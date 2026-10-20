"use strict";
/* Qiscus Export window. Talks only to the local qexport process that served this page. */

// ------------------------------------------------------------------ token & transport

const TOKEN = (() => {
  const m = location.hash.match(/t=([\w-]+)/);
  try {
    if (m) {
      sessionStorage.setItem("qx-token", m[1]);
      history.replaceState(null, "", location.pathname);   // keep the token out of view
    }
    return (m && m[1]) || sessionStorage.getItem("qx-token") || "";
  } catch (_) {
    return m ? m[1] : "";
  }
})();

let state = null;
let lastStateAt = 0;

async function api(path, body) {
  const opts = { method: body === undefined ? "GET" : "POST", headers: { "X-QX-Token": TOKEN } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (_) {
    throw new Error("Qiscus Export has stopped. Open it again from the desktop icon.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function act(fn, okMessage) {
  try {
    const r = await fn();
    if (okMessage) toast(typeof okMessage === "function" ? okMessage(r) : okMessage);
    pollSoon();
    return r;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

function startStream() {
  let failures = 0;
  const es = new EventSource(`/api/events?t=${encodeURIComponent(TOKEN)}`);
  es.onmessage = (ev) => { failures = 0; apply(JSON.parse(ev.data)); };
  es.addEventListener("bye", () => { es.close(); showStopped(); });
  es.onerror = () => {
    failures += 1;
    if (failures > 3) { es.close(); setTimeout(startPolling, 1000); }
  };
}

let pollTimer = null;
function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 2000);
  poll();
}
async function poll() {
  try { apply(await api("/api/state")); } catch (_) { if (Date.now() - lastStateAt > 15000) showStopped(); }
}
function pollSoon() { setTimeout(poll, 150); }

setInterval(() => { api("/api/heartbeat", {}).catch(() => {}); }, 5000);
api("/api/heartbeat", {}).catch(() => {});

window.addEventListener("beforeunload", (e) => {
  if (state && activeTasks().length) { e.preventDefault(); e.returnValue = ""; }
});

// ------------------------------------------------------------------ helpers

const $ = (id) => document.getElementById(id);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
const num = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
function duration(s) {
  if (s == null || !isFinite(s)) return "";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const hrs = Math.floor(m / 60);
  return `${hrs} h ${m % 60} min`;
}
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `Today ${time}`;
  const y = new Date(today); y.setDate(today.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `Yesterday ${time}`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })} ${time}`;
}
const mb = (bytes) => `${(bytes / 1e6).toFixed(1)} MB`;
function rate(r) {
  if (!r) return "";
  return r >= 1 ? `${r.toFixed(1)} per second` : `${Math.round(r * 60)} per minute`;
}
function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
function toast(msg, isError) {
  const t = h("div", { class: `toast${isError ? " error" : ""}`, role: isError ? "alert" : "status" }, msg);
  $("toasts").append(t);
  setTimeout(() => t.remove(), isError ? 9000 : 4500);
}
async function copy(text, label) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const r = await api("/api/clipboard", { text }).catch(() => ({ ok: false }));
    if (!r.ok) { toast("Couldn't copy. Select the text and press Ctrl+C.", true); return; }
  }
  toast(`${label} copied.`);
}
const activeTasks = () => (state ? state.tasks.filter((t) => ["running", "paused", "pending"].includes(t.state)) : []);

// ------------------------------------------------------------------ render

function apply(s) {
  state = s;
  lastStateAt = Date.now();
  renderHeader(s);
  renderBanners(s);
  renderStatus(s);
  renderTasks(s);
  renderFiles(s);
  renderLogs(s);
}

function renderHeader(s) {
  setText($("appcode"), s.app_code ? `Qiscus app ${s.app_code}` : "Not connected to Qiscus yet");
  setText($("version"), `qexport ${s.version}`);
  const pill = $("login-pill");
  const ls = s.login.state;
  const map = {
    ok: ["Login: OK", "ok"], checking: ["Login: checking…", ""], unknown: ["Login: checking…", ""],
    missing: ["Log in to Qiscus", "warn"], failed: ["Login: not working", "error"],
    expired: ["Login expired", "error"],
  };
  const [label, cls] = map[ls] || map.unknown;
  setText(pill, label);
  pill.className = `pill ${cls}`;
}

let bannerKey = "";
function renderBanners(s) {
  const items = [];
  if (s.login.state === "expired" || s.login.state === "failed")
    items.push(["error", "The Qiscus login stopped working. Everything downloaded so far is safe.", "Refresh login", openLogin]);
  else if (!s.logged_in)
    items.push(["warn", "Connect qexport to your Qiscus admin account to download.", "Log in", openLogin]);
  if (s.blocked && s.logged_in && s.blocked !== s.verdict.detail) items.push(["busy", s.blocked]);
  if (s.integrity.state === "checking") items.push(["busy", "Checking the downloaded data file…"]);
  const key = JSON.stringify(items.map((i) => i.slice(0, 3)));
  if (key === bannerKey) return;
  bannerKey = key;
  $("banners").replaceChildren(...items.map(([lvl, text, btn, fn]) =>
    h("div", { class: `banner ${lvl}`, role: lvl === "error" ? "alert" : "status" },
      h("p", {}, text), btn ? h("button", { class: "btn small", type: "button", onclick: fn }, btn) : null)));
}

function renderStatus(s) {
  const v = s.verdict;
  $("status").className = `card status lvl-${v.level}`;
  setText($("verdict-headline"), v.headline);
  setText($("verdict-detail"), v.detail || "");
  const notesKey = JSON.stringify(v.notes || []);
  if ($("verdict-notes").dataset.key !== notesKey) {
    $("verdict-notes").dataset.key = notesKey;
    $("verdict-notes").replaceChildren(...(v.notes || []).map((n) => h("li", {}, n)));
  }

  const st = s.store;
  const stats = [];
  if (st.exists) {
    const fetchable = st.jobs_total - st.jobs_not_sent;
    stats.push(["Conversations", `${num(st.rooms_done)} / ${num(st.rooms_total)}`,
      [st.rooms_pending && `${num(st.rooms_pending)} to go`, st.rooms_failed_total && `${num(st.rooms_failed_total)} failed`].filter(Boolean).join(" · ") || (st.rooms_listed ? "all downloaded" : "still listing")]);
    stats.push(["Messages", num(st.messages), ""]);
    stats.push(["Broadcast campaigns", `${num(st.jobs_done)} / ${num(fetchable)}`,
      [st.jobs_pending && `${num(st.jobs_pending)} to go`, st.jobs_failed_total && `${num(st.jobs_failed_total)} failed`,
        st.jobs_not_sent && `${num(st.jobs_not_sent)} not sent yet`].filter(Boolean).join(" · ") || "all downloaded"]);
    stats.push(["Recipients", num(st.broadcast_logs), ""]);
    stats.push(["Saved on this PC", `${st.size_mb} MB`, st.last_completed ? `last full run ${when(st.last_completed)}` : ""]);
  }
  const statsKey = JSON.stringify(stats);
  if ($("stats").dataset.key !== statsKey) {
    $("stats").dataset.key = statsKey;
    $("stats").replaceChildren(...stats.map(([k, val, extra]) =>
      h("div", { class: "stat" }, h("dt", {}, k), h("dd", {}, val, extra ? h("span", { class: "extra" }, extra) : null))));
  }

  // Primary buttons: label and availability follow the data.
  const downloading = activeTasks().some((t) => t.kind !== "workbook");
  const blocked = !!s.blocked;
  const dl = $("btn-download");
  const started = st.exists && (st.rooms_total || st.jobs_total);
  setText(dl, !started ? "Download everything" : (st.complete ? "Download again (check everything)" : "Continue download"));
  dl.disabled = downloading || blocked;
  dl.title = blocked ? s.blocked : downloading ? "A download is already running" : "";
  $("btn-update").disabled = downloading || blocked || !started;
  $("btn-update").classList.toggle("hidden", !started);
  const building = activeTasks().some((t) => t.kind === "workbook");
  $("btn-build").disabled = building || !started;
  $("btn-build").title = building ? "An Excel file is being built right now" : "Make an Excel file from what's downloaded, right now";
  const failed = st.exists ? st.rooms_failed_total + st.jobs_failed_total : 0;
  $("btn-retry").classList.toggle("hidden", !failed);
  setText($("btn-retry"), `Retry ${num(failed)} failed item${failed === 1 ? "" : "s"}`);
  $("btn-retry").disabled = downloading || blocked;
  dl.classList.toggle("primary", v.action === "download" || !started);
  $("btn-build").classList.toggle("primary", v.action === "build");
  $("btn-retry").classList.toggle("primary", v.action === "retry");

  const ig = s.integrity;
  const health = $("health");
  const healthText = {
    not_run: "", checking: "Checking data health…",
    ok: `Data health check: OK${ig.finished ? ` (${when(ig.finished)})` : ""}.`,
    damaged: `Data health check failed: ${ig.detail || ""}`,
  }[ig.state] || "";
  if (health.dataset.key !== ig.state + healthText) {
    health.dataset.key = ig.state + healthText;
    health.replaceChildren(healthText ? h("span", {}, healthText + " ") : "",
      ig.state !== "checking" && st.exists ? h("button", { class: "link", type: "button", onclick: () => act(() => api("/api/integrity", {}), "Checking the data file…") }, "Check data health") : null);
  }
}

// ---------------------------------------------------------------- tasks

const taskEls = new Map();
function renderTasks(s) {
  const shown = s.tasks.slice().reverse().filter((t, i) => ["running", "paused", "pending"].includes(t.state) || i < 8);
  const keep = new Set(shown.map((t) => t.id));
  for (const [id, el] of taskEls) if (!keep.has(id)) { el.remove(); taskEls.delete(id); }
  const box = $("tasks");
  shown.forEach((t, i) => {
    let el = taskEls.get(t.id);
    if (!el) { el = buildTask(t); taskEls.set(t.id, el); }
    updateTask(el, t);
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
  });
  $("tasks-empty").classList.toggle("hidden", shown.length > 0);
  const n = activeTasks().length;
  setText($("tasks-summary"), n ? `${n} running` : "");
}

const STATE_LABEL = { pending: "Starting", running: "Running", paused: "Paused", completed: "Done", failed: "Stopped", cancelled: "Cancelled" };

function buildTask(t) {
  const el = h("article", { class: "task", "data-id": t.id },
    h("div", { class: "task-head" }, h("span", { class: "task-title" }), h("span", { class: "badge" })),
    h("div", { class: "task-phase small muted" }),
    h("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100" }, h("span")),
    h("div", { class: "task-meta" }),
    h("div", { class: "task-current" }),
    h("p", { class: "task-outcome" }),
    h("div", { class: "task-actions" }),
    h("details", {}, h("summary", { class: "small" }, "Details"), h("ol", { class: "task-log" })));
  return el;
}

function updateTask(el, t) {
  const active = ["running", "paused", "pending"].includes(t.state);
  el.classList.toggle("done", !active);
  setText(el.querySelector(".task-title"), t.title + (t.auto ? " (automatic)" : ""));
  const badge = el.querySelector(".badge");
  const partial = t.state === "completed" && t.outcome === "partial";
  setText(badge, partial ? "Done, partial" : STATE_LABEL[t.state] || t.state);
  badge.className = `badge ${partial ? "partial" : t.state}`;
  setText(el.querySelector(".task-phase"), active ? t.phase : (t.finished_at ? `Ended ${when(t.finished_at)}` : ""));

  const bar = el.querySelector(".bar");
  const pct = t.total ? Math.min(100, Math.round((t.done / t.total) * 100)) : null;
  bar.classList.toggle("hidden", !active);
  bar.classList.toggle("indeterminate", active && pct == null && t.state === "running");
  bar.firstChild.style.width = pct == null ? "" : `${pct}%`;
  if (pct != null) bar.setAttribute("aria-valuenow", String(pct)); else bar.removeAttribute("aria-valuenow");

  const meta = [];
  if (active && t.total) meta.push(`${num(t.done)} of ${num(t.total)} (${pct}%)`);
  else if (active && t.done) meta.push(`${num(t.done)} so far`);
  if (active && t.state === "running" && t.rate) meta.push(rate(t.rate));
  if (active && t.state === "running" && t.eta_s) meta.push(`about ${duration(t.eta_s)} left`);
  if (t.counters.messages) meta.push(`${num(t.counters.messages)} messages`);
  if (t.counters.recipients) meta.push(`${num(t.counters.recipients)} recipients`);
  if (t.warnings) meta.push(`${num(t.warnings)} warning${t.warnings === 1 ? "" : "s"}`);
  const metaKey = meta.join("|");
  const metaEl = el.querySelector(".task-meta");
  if (metaEl.dataset.key !== metaKey) { metaEl.dataset.key = metaKey; metaEl.replaceChildren(...meta.map((m) => h("span", {}, m))); }
  setText(el.querySelector(".task-current"), active && t.current ? `Now: ${t.current}` : "");

  const out = el.querySelector(".task-outcome");
  let outText = "";
  if (!active) outText = [t.outcome_text, t.message].filter(Boolean).join(" ");
  else if (t.state === "paused") outText = "Paused. Nothing is being downloaded; press Resume to carry on.";
  setText(out, outText);
  out.className = `task-outcome ${t.state === "failed" ? "failed" : partial || t.outcome === "locked" ? "partial" : ""}`;

  const actions = [];
  if (t.state === "running" || t.state === "pending") actions.push(["Pause", "pause"]);
  if (t.state === "paused") actions.push(["Resume", "resume"]);
  if (active) actions.push(["Cancel", "cancel", "danger"]);
  if (t.can_retry_save) actions.push(["Retry saving", "retry-save", "primary"]);
  if (!active && t.report && !t.can_retry_save) actions.push(["Open Excel file", "open-file", "primary"], ["Show in folder", "reveal-file"]);
  if (!active && t.kind !== "workbook" && ["auth"].includes(t.outcome)) actions.push(["Refresh login", "login", "primary"]);
  if (!active && t.kind !== "workbook" && ["rate", "network", "interrupted", "cancelled"].includes(t.outcome)) actions.push(["Continue download", "continue"]);
  const actKey = actions.map((a) => a[1]).join("|");
  const actEl = el.querySelector(".task-actions");
  if (actEl.dataset.key !== actKey) {
    actEl.dataset.key = actKey;
    actEl.replaceChildren(...actions.map(([label, a, cls]) =>
      h("button", { class: `btn small ${cls || ""}`, type: "button", onclick: () => taskAction(t.id, a) }, label)));
  }

  const logKey = t.log.length + (t.log.length ? t.log[t.log.length - 1].join() : "");
  const logEl = el.querySelector(".task-log");
  if (logEl.dataset.key !== logKey) {
    logEl.dataset.key = logKey;
    logEl.replaceChildren(...t.log.slice(-40).map(([ts, m]) => h("li", {}, `${ts}  ${m}`)));
  }
}

async function taskAction(id, a) {
  const t = state.tasks.find((x) => x.id === id);
  if (a === "open-file") return act(() => api("/api/files/open", { name: t.report.name }));
  if (a === "reveal-file") return act(() => api("/api/files/reveal", { name: t.report.name }));
  if (a === "login") return openLogin();
  if (a === "continue") return act(() => api("/api/download", {}), (r) => `Started: ${r.started.join(", ")}.`);
  if (a === "cancel") {
    const ok = await confirmBox("Cancel this task?",
      [h("p", {}, t.kind === "workbook"
        ? "The Excel file being built is thrown away. Your downloaded data is not touched."
        : "It stops after the item it is on. Everything downloaded so far stays saved, and you can continue later.")],
      [["Keep running", "no", "ghost"], ["Cancel task", "yes", "danger solid"]]);
    if (ok !== "yes") return;
  }
  return act(() => api(`/api/tasks/${id}/${a}`, {}));
}

// ---------------------------------------------------------------- files

const fileEls = new Map();
let filesKey = "";
function renderFiles(s) {
  const key = JSON.stringify(s.files);
  if (key === filesKey) return;
  filesKey = key;
  const latestComplete = s.files.find((f) => f.complete);
  const box = $("files");
  fileEls.clear();
  box.replaceChildren(...s.files.slice(0, 40).map((f) => {
    const badge = f.complete === true ? ["Complete", "complete"] : f.complete === false ? ["Partial", "partial"] : ["Unknown", "unknown"];
    const counts = [f.conversations != null && `${num(f.conversations)} conversations`, f.messages != null && `${num(f.messages)} messages`,
      f.campaigns != null && `${num(f.campaigns)} campaigns`, f.recipients != null && `${num(f.recipients)} recipients`].filter(Boolean).join(" · ");
    const el = h("article", { class: `file${f === latestComplete ? " latest" : ""}` },
      h("div", { class: "file-head" },
        h("span", { class: "file-name" }, f.name),
        h("span", { class: `badge ${badge[1]}` }, f === latestComplete ? "Newest complete" : badge[0])),
      h("div", { class: "file-meta" }, `${when(f.modified)} · ${mb(f.size)}${counts ? " · " + counts : ""}`),
      f.complete === false ? h("div", { class: "file-why" }, f.completeness) : null,
      h("div", { class: "file-actions" },
        h("button", { class: "btn small primary", type: "button", onclick: () => act(() => api("/api/files/open", { name: f.name })) }, "Open"),
        h("button", { class: "btn small", type: "button", onclick: () => act(() => api("/api/files/reveal", { name: f.name })) }, "Show in folder"),
        h("button", { class: "btn small ghost", type: "button", onclick: () => copy(f.path, "File path") }, "Copy path"),
        h("button", { class: "btn small ghost danger", type: "button", onclick: () => deleteFiles([f.name], f) }, "Delete")));
    fileEls.set(f.name, el);
    return el;
  }));
  $("files-empty").classList.toggle("hidden", s.files.length > 0);
}

async function deleteFiles(names, single) {
  const body = [];
  if (single && single.complete) body.push(h("p", {}, "This is a complete export. Delete it only if you have a newer one or a copy elsewhere."));
  body.push(h("p", {}, names.length === 1 ? "This file goes to the Recycle Bin:" : `These ${names.length} files go to the Recycle Bin:`));
  body.push(h("ul", { class: "confirm-list" }, names.map((n) => h("li", {}, n))));
  body.push(h("p", { class: "muted small" }, "Your downloaded data is not affected; any Excel file can be rebuilt with Build Excel now."));
  const ok = await confirmBox(names.length === 1 ? "Delete this Excel file?" : "Delete old partial files?", body,
    [["Keep", "no", "ghost"], ["Move to Recycle Bin", "yes", "danger solid"]]);
  if (ok !== "yes") return;
  const r = await act(() => api("/api/files/delete", { names }));
  if (r) {
    if (r.deleted.length) toast(`Moved ${r.deleted.length} file${r.deleted.length === 1 ? "" : "s"} to the Recycle Bin.`);
    for (const e of r.errors) toast(e, true);
  }
}

// ---------------------------------------------------------------- logs

let logsKey = "";
let historyKey = "";
function renderLogs(s) {
  const a = s.activity;
  const k = a.length + (a.length ? a[a.length - 1].join() : "");
  if (k !== logsKey) {
    logsKey = k;
    const box = $("activity");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
    box.replaceChildren(...a.map(([ts, who, msg]) => h("li", {}, h("time", {}, ts), h("span", { class: "who" }, who), h("span", {}, msg))));
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
  const hk = JSON.stringify(s.history.slice(0, 3));
  if (hk !== historyKey) {
    historyKey = hk;
    $("history").replaceChildren(...(s.history.length ? s.history.map((x) =>
      h("li", {}, h("time", {}, when(x.finished_at).replace("Today ", "")), h("span", { class: "who" }, x.title),
        h("span", {}, [x.outcome_text, x.file ? `File: ${x.file}` : ""].filter(Boolean).join(" ")))) :
      [h("li", {}, h("span", {}), h("span", {}), h("span", { class: "muted" }, "No runs yet."))]));
  }
}

function switchTab(which) {
  const isAct = which === "activity";
  $("tab-activity").classList.toggle("active", isAct);
  $("tab-history").classList.toggle("active", !isAct);
  $("tab-activity").setAttribute("aria-selected", String(isAct));
  $("tab-history").setAttribute("aria-selected", String(!isAct));
  $("activity").classList.toggle("hidden", !isAct);
  $("history").classList.toggle("hidden", isAct);
}

// ---------------------------------------------------------------- dialogs

function confirmBox(title, body, buttons) {
  return new Promise((resolve) => {
    const dlg = $("dlg-confirm");
    $("confirm-title").textContent = title;
    $("confirm-body").replaceChildren(...body);
    $("confirm-actions").replaceChildren(...buttons.map(([label, value, cls]) =>
      h("button", { class: `btn ${cls || ""}`, value, type: "submit" }, label)));
    dlg.onclose = () => resolve(dlg.returnValue);
    dlg.returnValue = "";
    dlg.showModal();
  });
}

function openLogin() {
  if (state && !$("login-snippet").value) $("login-snippet").value = state.login_snippet;
  $("login-result").textContent = "";
  $("login-result").className = "result";
  $("login-continue").classList.add("hidden");
  $("dlg-login").showModal();
}

async function loginResult(promise) {
  const box = $("login-result");
  box.className = "result";
  box.textContent = "Checking the login with Qiscus…";
  try {
    const r = await promise;
    const ok = r.state === "ok";
    box.className = `result ${ok ? "ok" : "error"}`;
    box.textContent = ok
      ? `Connected. Both Qiscus services accepted the login.${r.not_admin ? " Note: this account is not an admin, so broadcast campaigns may be refused." : ""}`
      : `Saved, but Qiscus refused it (${[!r.omni && "dashboard", !r.sdk && "chat"].filter(Boolean).join(" and ")}). Make sure you are logged in, then run the snippet again.`;
    const resumable = ok && state && state.tasks.some((t) => t.outcome === "auth" && !["running", "paused", "pending"].includes(t.state));
    $("login-continue").classList.toggle("hidden", !resumable);
    pollSoon();
  } catch (e) {
    box.className = "result error";
    box.textContent = e.message;
  }
}

function openOptions() {
  const o = state.options;
  const f = $("options-form");
  for (const k of ["include_messages", "include_broadcasts", "include_recipients", "auto_workbook"]) f.elements[k].checked = !!o[k];
  f.elements.broadcasts_since.value = o.broadcasts_since || "";
  f.elements.workbook_dir.value = o.workbook_dir || "";
  f.elements.workbook_dir.placeholder = state.exports_dir;
  for (const k of ["rooms_rps", "messages_rps", "broadcast_rps"]) {
    f.elements[k].value = o[k] ?? "";
    f.elements[k].placeholder = String(state.defaults[k]);
  }
  $("options-error").textContent = "";
  $("dlg-options").showModal();
}

async function saveOptions() {
  const f = $("options-form");
  const changes = {};
  for (const k of ["include_messages", "include_broadcasts", "include_recipients", "auto_workbook"]) changes[k] = f.elements[k].checked;
  changes.broadcasts_since = f.elements.broadcasts_since.value || null;
  changes.workbook_dir = f.elements.workbook_dir.value.trim() || null;
  for (const k of ["rooms_rps", "messages_rps", "broadcast_rps"]) changes[k] = f.elements[k].value === "" ? null : f.elements[k].value;
  try {
    await api("/api/options", { changes });
    $("dlg-options").close();
    toast("Options saved. They apply to the next download or Excel file.");
    pollSoon();
  } catch (e) {
    $("options-error").textContent = e.message;
  }
}

async function closeApp() {
  const running = activeTasks();
  if (!running.length) {
    const ok = await confirmBox("Close Qiscus Export?", [h("p", {}, "Nothing is running. Your data stays saved on this PC.")],
      [["Cancel", "no", "ghost"], ["Close", "stop", "primary"]]);
    if (ok === "stop") return quit("stop");
    return;
  }
  const names = running.map((t) => t.title.toLowerCase());
  const choice = await confirmBox("Something is still running", [
    h("p", {}, `Still busy: ${names.join(", ")}.`),
    h("ul", { class: "notes" },
      h("li", {}, h("strong", {}, "Keep going in the background: "), "close this window; the work carries on and the app stops by itself when it's done. Open Qiscus Export again to check on it."),
      h("li", {}, h("strong", {}, "Stop safely: "), "each task stops after its current item. Everything downloaded so far stays saved and you can continue later.")),
  ], [["Don't close", "no", "ghost"], ["Stop safely and close", "stop", "danger"], ["Keep going in the background", "background", "primary"]]);
  if (choice === "background" || choice === "stop") return quit(choice);
}

async function quit(mode) {
  await api("/api/quit", { mode }).catch(() => {});
  if (mode === "stop") showStopped("Stopping safely… You can close this window once it says it has stopped.");
  else showStopped("Running in the background. You can close this window; open Qiscus Export again to check on it.", true);
  setTimeout(() => window.close(), 400);
}

function showStopped(msg, background) {
  const text = msg || "Qiscus Export has stopped. Your data is saved. Open it again from the desktop icon.";
  $("banners").replaceChildren(h("div", { class: `banner ${background ? "busy" : "ok"}`, role: "status" }, h("p", {}, text)));
  bannerKey = "stopped";
  if (!background) for (const b of document.querySelectorAll("main .btn")) b.disabled = true;
}

// ---------------------------------------------------------------- wiring

$("btn-download").addEventListener("click", () => act(() => api("/api/download", {}), (r) =>
  `Started: ${r.started.join(", ")}.${r.notes.length ? " " + r.notes.join(" ") : ""}`));
$("btn-update").addEventListener("click", () => act(() => api("/api/download", { update: true }), (r) =>
  `Checking for new activity: ${r.started.join(", ")}.`));
$("btn-build").addEventListener("click", () => act(() => api("/api/build", {}), "Building the Excel file from a snapshot of the data…"));
$("btn-retry").addEventListener("click", () => act(() => api("/api/retry-failed", {}), (r) => `Retrying ${num(r.reset)} item(s).`));
$("btn-options").addEventListener("click", openOptions);
$("options-save").addEventListener("click", saveOptions);
$("btn-close").addEventListener("click", closeApp);
$("login-pill").addEventListener("click", openLogin);
$("btn-open-folder").addEventListener("click", () => act(() => api("/api/folder", { which: "workbooks" })));
$("btn-store-folder").addEventListener("click", () => act(() => api("/api/folder", { which: "store" })));
$("btn-diagnostics").addEventListener("click", async () => {
  const r = await act(() => api("/api/diagnostics"));
  if (r) copy(r.text, "Diagnostics (no passwords or tokens)");
});
$("btn-delete-old").addEventListener("click", async () => {
  const r = await act(() => api("/api/files/old"));
  if (!r) return;
  if (!r.names.length) return toast("No old partial files to delete. Complete files and the newest file are always kept.");
  deleteFiles(r.names);
});
$("tab-activity").addEventListener("click", () => switchTab("activity"));
$("tab-history").addEventListener("click", () => switchTab("history"));
$("login-open").addEventListener("click", async () => {
  const r = await act(() => api("/api/login/start", {}));
  if (r) {
    $("login-snippet").value = r.snippet;
    toast(r.copied ? "Qiscus opened. The snippet is on your clipboard." : "Qiscus opened. Copy the snippet below.");
  }
});
$("login-copy-snippet").addEventListener("click", async () => {
  if (!$("login-snippet").value) {
    const r = await act(() => api("/api/login/start", {}));
    if (r) $("login-snippet").value = r.snippet;
  }
  copy($("login-snippet").value, "Snippet");
});
$("login-read").addEventListener("click", () => loginResult(api("/api/login/clipboard", {})));
$("login-use-paste").addEventListener("click", () => loginResult(api("/api/login/save", { text: $("login-paste").value })));
$("login-continue").addEventListener("click", () => {
  $("dlg-login").close();
  act(() => api("/api/download", {}), (r) => `Continuing: ${r.started.join(", ")}.`);
});

if (!TOKEN) showStopped("This window can't connect. Open Qiscus Export from the desktop icon.");
else startStream();
