let settingsMode = "setup"; // "setup" | "reopen"

// ===================== الوضع الليلي/النهاري =====================
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const icon = theme === "dark" ? "☀️" : "🌙";
  document.querySelectorAll("#btn-theme-toggle, #btn-theme-toggle-setup").forEach((btn) => {
    btn.textContent = icon;
  });
}

function initTheme() {
  const saved = localStorage.getItem("theme") || "light";
  applyTheme(saved);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "light";
  const next = current === "dark" ? "light" : "dark";
  localStorage.setItem("theme", next);
  applyTheme(next);
}

initTheme();

function addRipple(btn, evt) {
  const rect = btn.getBoundingClientRect();
  const ripple = document.createElement("span");
  const size = Math.max(rect.width, rect.height);
  ripple.className = "ripple";
  ripple.style.width = ripple.style.height = size + "px";
  ripple.style.left = (evt.clientX - rect.left - size / 2) + "px";
  ripple.style.top = (evt.clientY - rect.top - size / 2) + "px";
  btn.appendChild(ripple);
  setTimeout(() => ripple.remove(), 600);
}

document.addEventListener("click", (evt) => {
  const btn = evt.target.closest(".btn");
  if (btn) addRipple(btn, evt);
});

function api() {
  return window.pywebview.api;
}

function setStatus(text) {
  const el = document.getElementById("status-bar");
  if (el) el.textContent = text;
}

function showScreen(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.add("hidden"));
  document.getElementById(id).classList.remove("hidden");
}

// ===================== الإعداد / الإعدادات =====================
function fillSetupForm(settings) {
  const form = document.getElementById("setup-form");
  for (const key of [
    "imap_host", "imap_port", "imap_user", "imap_inbox_folder", "imap_sent_folder",
    "postgres_host", "postgres_port", "postgres_db", "postgres_user",
    "storage_root", "scan_interval_hours",
  ]) {
    if (settings[key] !== undefined) form.elements[key].value = settings[key];
  }
  form.elements["autostart_enabled"].checked = !!settings.autostart_enabled;

  const setPlaceholder = (fieldId, has) => {
    document.getElementById(fieldId).placeholder = has
      ? "اتركه فارغاً للإبقاء على القيمة الحالية"
      : "";
  };
  setPlaceholder("imap_password_field", settings.has_imap_password);
  setPlaceholder("postgres_password_field", settings.has_postgres_password);
  setPlaceholder("gemini_key_field", settings.has_gemini_key);
}

async function openSetupScreen(mode) {
  settingsMode = mode;
  document.getElementById("setup-error").classList.add("hidden");
  document.getElementById("btn-cancel-settings").classList.toggle("hidden", mode !== "reopen");
  document.getElementById("btn-back-top").classList.toggle("hidden", mode !== "reopen");
  document.getElementById("setup-title").textContent =
    mode === "reopen" ? "الإعدادات" : "إعداد E-Mail AI AGENT";
  const settings = await api().get_settings();
  fillSetupForm(settings);
  showScreen("setup-screen");
}

function readSetupForm() {
  const form = document.getElementById("setup-form");
  const fd = new FormData(form);
  return {
    imap_host: fd.get("imap_host"),
    imap_port: fd.get("imap_port"),
    imap_user: fd.get("imap_user"),
    imap_password: fd.get("imap_password"),
    imap_inbox_folder: fd.get("imap_inbox_folder"),
    imap_sent_folder: fd.get("imap_sent_folder"),
    postgres_host: fd.get("postgres_host"),
    postgres_port: fd.get("postgres_port"),
    postgres_db: fd.get("postgres_db"),
    postgres_user: fd.get("postgres_user"),
    postgres_password: fd.get("postgres_password"),
    gemini_key: fd.get("gemini_key"),
    storage_root: fd.get("storage_root"),
    scan_interval_hours: fd.get("scan_interval_hours"),
    autostart_enabled: form.elements["autostart_enabled"].checked,
  };
}

async function initApp() {
  document.getElementById("btn-test-imap").addEventListener("click", async () => {
    const result = await api().test_imap(readSetupForm());
    alert(result.message);
  });
  document.getElementById("btn-test-pg").addEventListener("click", async () => {
    const result = await api().test_postgres(readSetupForm());
    alert(result.message);
  });
  document.getElementById("btn-browse").addEventListener("click", async () => {
    const path = await api().browse_storage_folder();
    if (path) document.querySelector('[name="storage_root"]').value = path;
  });
  document.getElementById("btn-back-top").addEventListener("click", () => {
    showScreen("app-screen");
  });
  document.getElementById("btn-cancel-settings").addEventListener("click", () => {
    showScreen("app-screen");
  });
  document.getElementById("setup-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const errorBox = document.getElementById("setup-error");
    errorBox.classList.add("hidden");
    const result = await api().save_settings(readSetupForm());
    if (!result.ok) {
      errorBox.textContent = result.error;
      errorBox.classList.remove("hidden");
      return;
    }
    if (settingsMode === "reopen") {
      showScreen("app-screen");
      setStatus("تم حفظ الإعدادات.");
    } else {
      await enterAppScreen();
      scanNow();
    }
  });

  document.getElementById("btn-open-settings").addEventListener("click", () => openSetupScreen("reopen"));
  document.getElementById("btn-theme-toggle").addEventListener("click", toggleTheme);
  document.getElementById("btn-theme-toggle-setup").addEventListener("click", toggleTheme);
  document.getElementById("btn-scan").addEventListener("click", () => scanNow());
  document.getElementById("btn-scan-setup").addEventListener("click", () => scanNow());

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.getElementById("btn-search-letters").addEventListener("click", loadLetters);
  document.getElementById("search-keyword").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadLetters();
  });
  document.getElementById("btn-prev-page").addEventListener("click", () => {
    lettersPage -= 1;
    renderLettersPage();
  });
  document.getElementById("btn-next-page").addEventListener("click", () => {
    lettersPage += 1;
    renderLettersPage();
  });

  document.getElementById("btn-send-chat").addEventListener("click", sendChat);
  document.getElementById("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendChat();
  });

  document.getElementById("btn-save-alias").addEventListener("click", saveAlias);

  setInterval(pollScanStatus, 1500);

  const status = await api().get_setup_status();
  if (status.setup_complete) {
    await enterAppScreen();
  } else {
    await openSetupScreen("setup");
  }
}

async function enterAppScreen() {
  showScreen("app-screen");
  await loadDashboardStats();
  await loadLetters();
  await loadAliases();
}

// ===================== لوحة التحكم =====================
async function loadDashboardStats() {
  const stats = await api().get_dashboard_stats();
  document.getElementById("stat-incoming").textContent = stats.incoming_today;
  document.getElementById("stat-outgoing").textContent = stats.outgoing_today;
  document.getElementById("stat-last-scan").textContent = stats.last_scan_at
    ? new Date(stats.last_scan_at).toLocaleString("ar-IQ")
    : "لم يُفحص بعد";
}

let lastScanStatus = null;

async function scanNow() {
  setStatus("جاري الفحص...");
  const btn = document.getElementById("btn-scan");
  btn.disabled = true;
  try {
    await api().scan_now_async();
  } finally {
    setTimeout(() => { btn.disabled = false; }, 1000);
  }
}

async function pollScanStatus() {
  if (!window.pywebview || !window.pywebview.api) return;
  const result = await api().get_scan_status();
  const text = result.status;
  if (text === lastScanStatus) return;
  // A failed scan may still have archived some letters before stopping.
  const justCompleted = (text.startsWith("اكتمل") || text.startsWith("تعذر")) && lastScanStatus !== null;
  lastScanStatus = text;
  setStatus(text);
  if (justCompleted) {
    loadDashboardStats();
    loadLetters();
  }
}

// ===================== التبويبات =====================
function switchTab(name) {
  // These buttons now also appear in the settings screen's header, so
  // switching a tab must first bring the main app screen back into view
  // (a no-op if it's already showing).
  showScreen("app-screen");
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
}

// ===================== الكتب المؤرشفة =====================
const LETTERS_PAGE_SIZE = 10;
let allLetters = [];
let lettersPage = 1;

async function loadLetters() {
  const keyword = document.getElementById("search-keyword").value.trim();
  const direction = document.getElementById("filter-direction").value;
  const result = await api().list_letters(keyword, direction, 1000);
  if (!result.ok) {
    setStatus("تعذر الاتصال بقاعدة البيانات: " + result.error);
    return;
  }
  allLetters = result.letters;
  lettersPage = 1;
  renderLettersPage();
}

function renderLettersPage() {
  const totalPages = Math.max(1, Math.ceil(allLetters.length / LETTERS_PAGE_SIZE));
  lettersPage = Math.min(Math.max(1, lettersPage), totalPages);
  const start = (lettersPage - 1) * LETTERS_PAGE_SIZE;
  const pageRows = allLetters.slice(start, start + LETTERS_PAGE_SIZE);

  const tbody = document.getElementById("letters-tbody");
  tbody.innerHTML = "";
  for (const row of pageRows) {
    const tr = document.createElement("tr");
    const badgeClass = row.direction === "incoming" ? "badge-incoming" : "badge-outgoing";
    const badgeText = row.direction === "incoming" ? "وارد" : "صادر";
    tr.innerHTML = `
      <td>${escapeHtml(row.title)}</td>
      <td><span class="badge ${badgeClass}">${badgeText}</span></td>
      <td>${escapeHtml(row.department)}</td>
      <td>${escapeHtml(row.email_date)}</td>
      <td class="email-cell">${escapeHtml(row.source_email)}</td>
      <td class="email-cell">${escapeHtml(row.dest_email)}</td>
      <td class="row-actions">
        <button data-action="open-file">فتح الملف</button>
        <button data-action="open-folder">فتح المجلد</button>
      </td>
    `;
    tr.querySelector('[data-action="open-file"]').addEventListener("click", async () => {
      const r = await api().open_file(row.primary_file_path);
      if (!r.ok) alert(r.error);
    });
    tr.querySelector('[data-action="open-folder"]').addEventListener("click", async () => {
      const r = await api().open_folder(row.folder_path);
      if (!r.ok) alert(r.error);
    });
    tbody.appendChild(tr);
  }

  document.getElementById("page-indicator").textContent =
    allLetters.length === 0 ? "لا توجد نتائج" : `صفحة ${lettersPage} من ${totalPages} (${allLetters.length} كتاب)`;
  document.getElementById("btn-prev-page").disabled = lettersPage <= 1;
  document.getElementById("btn-next-page").disabled = lettersPage >= totalPages;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

// ===================== الدردشة =====================
function appendChatMessage(who, text, cssClass) {
  const display = document.getElementById("chat-display");
  const empty = document.getElementById("chat-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "chat-msg " + cssClass;
  div.innerHTML = `<span class="who">${escapeHtml(who)}:</span> <span>${escapeHtml(text)}</span>`;
  display.appendChild(div);
  display.scrollTop = display.scrollHeight;
}

async function sendChat() {
  const input = document.getElementById("chat-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  appendChatMessage("أنت", question, "user");
  const result = await api().ask(question);
  appendChatMessage("الوكيل", result.answer, "agent");

  const resultsBox = document.getElementById("chat-results");
  resultsBox.innerHTML = "";
  for (const letter of (result.letters || []).slice(0, 5)) {
    if (!letter.file_path) continue;
    const btn = document.createElement("button");
    btn.className = "btn btn-ghost";
    btn.textContent = "فتح: " + letter.title;
    btn.addEventListener("click", async () => {
      const r = await api().open_file(letter.file_path);
      if (!r.ok) alert(r.error);
    });
    resultsBox.appendChild(btn);
  }
}

// ===================== أسماء الأقسام =====================
async function loadAliases() {
  const aliases = await api().list_aliases();
  const tbody = document.getElementById("aliases-tbody");
  tbody.innerHTML = "";
  for (const row of aliases) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(row.email)}</td>
      <td>${escapeHtml(row.department)}</td>
      <td class="row-actions"><button data-action="delete">حذف</button></td>
    `;
    tr.querySelector('[data-action="delete"]').addEventListener("click", async () => {
      await api().delete_alias(row.email);
      loadAliases();
    });
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      document.getElementById("alias-email").value = row.email;
      document.getElementById("alias-department").value = row.department;
    });
    tbody.appendChild(tr);
  }
}

async function saveAlias() {
  const email = document.getElementById("alias-email").value.trim();
  const department = document.getElementById("alias-department").value.trim();
  if (!email || !department) {
    alert("الرجاء إدخال البريد الإلكتروني واسم القسم.");
    return;
  }
  await api().save_alias(email, department);
  document.getElementById("alias-email").value = "";
  document.getElementById("alias-department").value = "";
  loadAliases();
}

window.addEventListener("pywebviewready", initApp);
