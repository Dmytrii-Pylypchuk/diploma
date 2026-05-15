/**
 * Triton Sync — SPA Logic
 *
 * API endpoints used:
 *   GET  /api/health   → server status
 *   GET  /api/groups   → list of sync groups
 *   PUT  /api/groups   → save groups array
 *   POST /api/preview  → dry-run diff
 *   POST /api/sync     → live sync
 *   GET  /api/logs     → recent log lines
 */

"use strict";

/* ── Constants ─────────────────────────────────────────────── */
const API         = "";
const LOG_POLL_MS = 3000;
const LOG_LINES   = 200;

/* ── State ─────────────────────────────────────────────────── */
let _pollTimer  = null;
let _polling    = false;
let _groups     = [];
let _editingIdx = null;

/* ═══════════════════════════════════════════════════════════
   TOAST NOTIFICATIONS
   ═══════════════════════════════════════════════════════════ */

const TOAST_ICONS = { success: "✓", error: "✕", warn: "⚠", info: "ℹ" };

function toast(msg, type = "info", duration = 4000) {
  const container = document.getElementById("toastContainer");
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.innerHTML = `
    <span class="toast-icon">${TOAST_ICONS[type] ?? "•"}</span>
    <span class="toast-msg">${escHtml(msg)}</span>
  `;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add("hiding");
    el.addEventListener("animationend", () => el.remove(), { once: true });
  }, duration);
}

/* ═══════════════════════════════════════════════════════════
   BUTTON LOADING STATE
   ═══════════════════════════════════════════════════════════ */

function setLoading(btnOrId, loading) {
  const btn = typeof btnOrId === "string"
    ? document.getElementById(btnOrId)
    : btnOrId;
  if (!btn) return;
  btn.classList.toggle("loading", loading);
  btn.disabled = loading;
}

/* ═══════════════════════════════════════════════════════════
   HTTP UTILITIES
   ═══════════════════════════════════════════════════════════ */

async function apiFetch(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const body = await res.clone().json();
      if (body.error) msg = body.error;
    } catch {/* ignore */}
    throw new Error(msg);
  }
  return res;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ═══════════════════════════════════════════════════════════
   HEALTH / STATUS BADGE
   ═══════════════════════════════════════════════════════════ */

async function checkHealth() {
  const badge  = document.getElementById("statusBadge");
  const footer = document.getElementById("footerBackend");
  try {
    const res  = await apiFetch("/api/health");
    const data = await res.json();
    const backend = data.backend ?? "?";
    const cls = backend === "google" ? "badge-google"
              : backend === "mock"   ? "badge-mock"
              : "badge-idle";
    badge.className   = `badge ${cls}`;
    badge.textContent = `${backend.toUpperCase()} ${data.dry_run ? "· dry run" : "· live"}`;
    footer.textContent = `backend: ${backend} · груп у конфігурації: ${data.groups_count ?? "?"}`;
  } catch (err) {
    badge.className   = "badge badge-error";
    badge.textContent = "Сервер недоступний";
    footer.textContent = `Помилка: ${err.message}`;
  }
}

/* ═══════════════════════════════════════════════════════════
   GROUPS — load, render, datalist
   ═══════════════════════════════════════════════════════════ */

async function loadGroups() {
  try {
    const res  = await apiFetch("/api/groups");
    const data = await res.json();
    _groups = data.groups ?? [];
    renderGroupsTable();
    _fillGroupsSelect();
  } catch (err) {
    toast("Помилка завантаження груп: " + err.message, "error");
  }
}

function _fillGroupsSelect() {
  const sel = document.getElementById("groupEmailSelect");
  const prev = sel.value;
  sel.innerHTML = '<option value="">— оберіть групу —</option>';
  for (const g of _groups) {
    const opt = document.createElement("option");
    opt.value = g.group_email;
    opt.textContent = g.group_email;
    sel.appendChild(opt);
  }
  // restore previous selection or auto-select if only one group
  if (_groups.length === 1) {
    sel.value = _groups[0].group_email;
  } else if (prev && _groups.some(g => g.group_email === prev)) {
    sel.value = prev;
  }
}

function renderGroupsTable() {
  const tbody = document.getElementById("groupsTableBody");

  if (_groups.length === 0) {
    tbody.innerHTML =
      '<tr id="groupsEmpty"><td colspan="9" class="empty-row">Групи не додані</td></tr>';
    return;
  }

  tbody.innerHTML = _groups.map((g, i) => {
    const rule = g.rule ?? {};
    return `
      <tr>
        <td class="group-email-cell">${escHtml(g.group_email)}</td>
        <td class="group-rule-cell">${escHtml(rule.year ?? "—")}</td>
        <td class="group-rule-cell">${escHtml(String(rule.speciality_id ?? "—"))}</td>
        <td class="group-rule-cell">${escHtml(String(rule.course_id ?? "—"))}</td>
        <td class="group-rule-cell">${escHtml(rule.group_name ?? "—")}</td>
        <td class="group-rule-cell">${escHtml(String(rule.faculty_id ?? "—"))}</td>
        <td class="group-rule-cell">${escHtml(String(rule.education_form_id ?? "—"))}</td>
        <td class="group-rule-cell">${escHtml(String(rule.level_of_education_id ?? "—"))}</td>
        <td>
          <div class="row-actions">
            <button class="btn btn-ghost btn-sm"
                    onclick="openGroupModal(${i})"
                    title="Редагувати">✎</button>
            <button class="btn btn-ghost btn-sm btn-icon-danger"
                    onclick="deleteGroup(${i})"
                    title="Видалити">✕</button>
          </div>
        </td>
      </tr>`;
  }).join("");
}

/* ═══════════════════════════════════════════════════════════
   GROUP MODAL — open / close / save
   ═══════════════════════════════════════════════════════════ */

function openGroupModal(idx = null) {
  _editingIdx = (typeof idx === "number") ? idx : null;
  const modal = document.getElementById("groupModal");
  document.getElementById("modalTitle").textContent =
    _editingIdx !== null ? "Редагувати групу" : "Додати групу";

  if (_editingIdx !== null) {
    const g    = _groups[_editingIdx];
    const rule = g.rule ?? {};
    document.getElementById("fGroupEmail").value         = g.group_email ?? "";
    document.getElementById("fYear").value               = rule.year ?? "";
    document.getElementById("fSpeciality").value         = rule.speciality_id ?? "";
    document.getElementById("fCourseId").value           = rule.course_id ?? "";
    document.getElementById("fGroupName").value          = rule.group_name ?? "";
    document.getElementById("fFacultyId").value          = rule.faculty_id ?? "";
    document.getElementById("fEducationFormId").value    = rule.education_form_id ?? "";
    document.getElementById("fLevelOfEducationId").value = rule.level_of_education_id ?? "";
  } else {
    ["fGroupEmail", "fYear", "fSpeciality", "fCourseId", "fGroupName",
     "fFacultyId", "fEducationFormId", "fLevelOfEducationId"]
      .forEach(id => { document.getElementById(id).value = ""; });
  }

  modal.classList.remove("hidden");
  document.getElementById("fGroupEmail").focus();
}

function closeGroupModal() {
  document.getElementById("groupModal").classList.add("hidden");
  _editingIdx = null;
}

async function saveGroupModal() {
  const email          = document.getElementById("fGroupEmail").value.trim();
  const year           = document.getElementById("fYear").value.trim();
  const speciality     = document.getElementById("fSpeciality").value.trim();
  const courseId       = document.getElementById("fCourseId").value.trim();
  const groupName      = document.getElementById("fGroupName").value.trim();
  const facultyId      = document.getElementById("fFacultyId").value.trim();
  const educFormId     = document.getElementById("fEducationFormId").value.trim();
  const levelId        = document.getElementById("fLevelOfEducationId").value.trim();

  if (!email)      { toast("Email групи є обов'язковим", "warn"); return; }
  if (!email.includes("@")) { toast("Введіть коректний email групи", "warn"); return; }
  if (!year)       { toast("Рік набору є обов'язковим", "warn"); return; }
  if (!speciality) { toast("ID спеціальності є обов'язковим", "warn"); return; }

  const group = {
    group_email: email,
    rule: {
      speciality_id: parseInt(speciality, 10),
      year,
      ...(courseId  ? { course_id:              parseInt(courseId,  10) } : {}),
      ...(groupName ? { group_name:             groupName }               : {}),
      ...(facultyId ? { faculty_id:             parseInt(facultyId, 10) } : {}),
      ...(educFormId ? { education_form_id:     parseInt(educFormId, 10) } : {}),
      ...(levelId   ? { level_of_education_id: parseInt(levelId,   10) } : {}),
    },
  };

  // snapshot for rollback on error
  const snapshot = JSON.parse(JSON.stringify(_groups));

  if (_editingIdx !== null) {
    _groups[_editingIdx] = group;
  } else {
    _groups.push(group);
  }

  closeGroupModal();

  const ok = await saveGroups();
  if (ok) {
    renderGroupsTable();
    _fillGroupsSelect();
  } else {
    // rollback in-memory state so UI stays consistent with config
    _groups.length = 0;
    _groups.push(...snapshot);
    renderGroupsTable();
    _fillGroupsSelect();
  }
}

async function saveGroups() {
  try {
    await apiFetch("/api/groups", {
      method: "PUT",
      body: JSON.stringify(_groups),
    });
    document.getElementById("groupsSaved").textContent =
      `Збережено: ${new Date().toLocaleTimeString()}`;
    toast("Групи збережено", "success");
    await checkHealth();
    return true;
  } catch (err) {
    toast(`Помилка збереження груп: ${err.message}`, "error", 8000);
    return false;
  }
}

async function deleteGroup(idx) {
  const g = _groups[idx];
  if (!confirm(`Видалити групу ${g.group_email}?`)) return;
  const snapshot = JSON.parse(JSON.stringify(_groups));
  _groups.splice(idx, 1);
  const ok = await saveGroups();
  if (!ok) {
    _groups.length = 0;
    _groups.push(...snapshot);
  }
  renderGroupsTable();
  _fillGroupsSelect();
}

/* ═══════════════════════════════════════════════════════════
   DRY RUN TOGGLE
   ═══════════════════════════════════════════════════════════ */

function onDryRunChange() {
  const isDryRun = document.getElementById("dryRunCheck").checked;
  const btnSync  = document.getElementById("btnSync");
  btnSync.disabled = isDryRun;
  btnSync.title = isDryRun
    ? "Зніміть Dry run для активації"
    : "Виконати реальну синхронізацію";
}

/* ═══════════════════════════════════════════════════════════
   PREVIEW / SYNC
   ═══════════════════════════════════════════════════════════ */

function showProgress() {
  document.getElementById("progressBar").classList.remove("hidden");
}
function hideProgress() {
  document.getElementById("progressBar").classList.add("hidden");
}

function _renderEntries(containerId, emails, names) {
  const el = document.getElementById(containerId);
  if (!emails.length) { el.innerHTML = ""; return; }
  el.innerHTML = emails.map(email => {
    const name = names?.[email];
    return `<div class="diff-entry">
      ${name ? `<span class="diff-entry-name">${escHtml(name)}</span>` : ""}
      <span class="diff-entry-email">${escHtml(email)}</span>
    </div>`;
  }).join("");
}

function renderDiff(data) {
  const names = data.names ?? {};
  const cols = [
    { listId: "listAdd",       countId: "countAdd",       key: "to_add" },
    { listId: "listRemove",    countId: "countRemove",    key: "to_remove" },
    { listId: "listUnchanged", countId: "countUnchanged", key: "unchanged" },
    { listId: "listInvalid",   countId: "countInvalid",   key: "invalid_emails" },
  ];
  for (const { listId, countId, key } of cols) {
    const items = data[key] ?? [];
    _renderEntries(listId, items, names);
    document.getElementById(countId).textContent = items.length;
  }

  const s = data.stats ?? {};
  const statsBar  = document.getElementById("statsBar");
  document.getElementById("statsText").innerHTML = [
    s.students_from_db != null ? `<b>${s.students_from_db}</b> студентів з DB` : "",
    s.emails_resolved  != null ? `<b>${s.emails_resolved}</b> email з LDAP` : "",
    s.ldap_missing     != null && s.ldap_missing > 0
      ? `<b>${s.ldap_missing}</b> без email` : "",
    s.actual_members   != null ? `<b>${s.actual_members}</b> поточних членів` : "",
    s.backend          != null ? `Pipeline: <b>${s.backend}</b>` : "",
    s.dry_run          != null ? `Dry run: <b>${s.dry_run ? "так" : "ні"}</b>` : "",
  ].filter(Boolean).join(" &nbsp;·&nbsp; ");
  statsBar.classList.remove("hidden");
}

function clearDiff() {
  ["listAdd","listRemove","listUnchanged","listInvalid"].forEach(id => {
    document.getElementById(id).innerHTML = "";
  });
  ["countAdd","countRemove","countUnchanged","countInvalid"].forEach(id => {
    document.getElementById(id).textContent = "0";
  });
  document.getElementById("statsBar").classList.add("hidden");
}

async function _doSyncRequest(endpoint, btnId) {
  const groupEmail = document.getElementById("groupEmailSelect").value;
  if (!groupEmail) {
    toast("Оберіть групу зі списку", "warn");
    document.getElementById("groupEmailSelect").focus();
    return;
  }
  const dryRun = document.getElementById("dryRunCheck").checked;

  clearDiff();
  setLoading(btnId, true);
  setLoading(btnId === "btnPreview" ? "btnSync" : "btnPreview", true);
  showProgress();

  try {
    const res  = await apiFetch(endpoint, {
      method: "POST",
      body: JSON.stringify({ group_email: groupEmail, dry_run: dryRun }),
    });
    const data = await res.json();
    renderDiff(data);

    const added   = (data.to_add    ?? []).length;
    const removed = (data.to_remove ?? []).length;
    const same    = (data.unchanged ?? []).length;

    if (endpoint === "/api/preview") {
      toast(`Preview: +${added}, -${removed}, =${same}`, "info");
    } else {
      const action = dryRun ? "Dry sync" : "Sync";
      toast(`${action} завершено: +${added}, -${removed}, =${same}`, "success");
    }

    await loadLogs();
  } catch (err) {
    toast("Помилка: " + err.message, "error", 7000);
  } finally {
    hideProgress();
    setLoading("btnPreview", false);
    const btnSync = document.getElementById("btnSync");
    btnSync.disabled = document.getElementById("dryRunCheck").checked;
    btnSync.classList.remove("loading");
  }
}

async function runPreview() {
  await _doSyncRequest("/api/preview", "btnPreview");
}

async function runSync() {
  const dryRun = document.getElementById("dryRunCheck").checked;
  if (!dryRun) {
    const email = document.getElementById("groupEmailSelect").value;
    const ok = confirm(
      `Ви збираєтесь виконати РЕАЛЬНУ синхронізацію для групи:\n${email}\n\nПродовжити?`
    );
    if (!ok) return;
  }
  await _doSyncRequest("/api/sync", "btnSync");
}

/* ═══════════════════════════════════════════════════════════
   LOGS PANEL
   ═══════════════════════════════════════════════════════════ */

let _lastLogLineCount = 0;

async function loadLogs() {
  try {
    const res  = await apiFetch(`/api/logs?lines=${LOG_LINES}`);
    const data = await res.json();
    const lines = data.lines ?? [];
    const total = data.total ?? 0;

    if (lines.length === _lastLogLineCount && _lastLogLineCount > 0) return;
    _lastLogLineCount = lines.length;

    document.getElementById("logMeta").textContent =
      `${lines.length} рядків (всього у файлі: ${total})  ·  ${new Date().toLocaleTimeString()}`;

    const terminal = document.getElementById("logOutput");
    terminal.innerHTML = lines.map(line => {
      const lower = line.toLowerCase();
      let cls = "";
      if      (lower.includes("[error]"))   cls = "log-error";
      else if (lower.includes("[warning]") || lower.includes("[warn]")) cls = "log-warn";
      else if (lower.includes("[info]"))    cls = "log-info";
      else if (lower.includes("[debug]"))   cls = "log-debug";
      return `<span class="${cls}">${escHtml(line)}</span>`;
    }).join("\n");

    terminal.scrollTop = terminal.scrollHeight;
  } catch {/* silent during polling */}
}

function clearLogView() {
  document.getElementById("logOutput").innerHTML = "";
  document.getElementById("logMeta").textContent = "Вигляд очищено";
  _lastLogLineCount = 0;
}

function togglePolling() {
  _polling = !_polling;
  const btnPoll   = document.getElementById("btnPoll");
  const pollIcon  = document.getElementById("pollIcon");
  const pollLabel = document.getElementById("pollLabel");

  if (_polling) {
    pollIcon.textContent  = "⏸";
    pollLabel.textContent = "Пауза";
    btnPoll.classList.add("btn-primary");
    btnPoll.classList.remove("btn-secondary");
    _pollTimer = setInterval(loadLogs, LOG_POLL_MS);
    loadLogs();
  } else {
    pollIcon.textContent  = "▶";
    pollLabel.textContent = "Авто-оновлення";
    btnPoll.classList.remove("btn-primary");
    btnPoll.classList.add("btn-secondary");
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

/* ═══════════════════════════════════════════════════════════
   KEYBOARD: close modal on Escape
   ═══════════════════════════════════════════════════════════ */

document.addEventListener("keydown", e => {
  if (e.key === "Escape" &&
      !document.getElementById("groupModal").classList.contains("hidden")) {
    closeGroupModal();
  }
});

/* ═══════════════════════════════════════════════════════════
   INIT
   ═══════════════════════════════════════════════════════════ */

async function init() {
  onDryRunChange();
  await Promise.allSettled([
    checkHealth(),
    loadGroups(),
    loadLogs(),
  ]);
}

document.addEventListener("DOMContentLoaded", init);
