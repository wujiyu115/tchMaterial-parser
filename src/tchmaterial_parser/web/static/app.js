/* 国家中小学智慧教育平台 资源下载工具 · Web 前端
 *
 * 视图：教材库 / 浏览资源 / 下载任务 / 下载记录 / 设置。
 * 全部使用原生 ES 模块化前的普通脚本，无需构建步骤，镜像内直接托管。
 */
"use strict";

const API = {
  stats: () => "/api/stats",
  catalog: (q, refresh) => `/api/catalog?q=${encodeURIComponent(q || "")}${refresh ? "&refresh=1" : ""}`,
  catalogChildren: (path, refresh) =>
    `/api/catalog/children?path=${encodeURIComponent(path || "")}${refresh ? "&refresh=1" : ""}`,
  parse: () => "/api/parse",
  downloads: () => "/api/downloads",
  stop: (id) => `/api/downloads/${encodeURIComponent(id)}/stop`,
  stopAll: () => "/api/downloads/stop-all",
  clearFinished: () => "/api/downloads/clear-finished",
  library: (q) => `/api/library?q=${encodeURIComponent(q || "")}`,
  libraryFile: (file, inline) => `/api/library/file?relative_path=${encodeURIComponent(file)}${inline ? "&inline=1" : ""}`,
  thumb: (file) => `/api/library/thumbnail?relative_path=${encodeURIComponent(file)}`,
  deleteFile: (file) => `/api/library/file?relative_path=${encodeURIComponent(file)}`,
  records: () => "/api/records",
  clearRecords: () => "/api/records",
  settings: () => "/api/settings",
  saveSettings: () => "/api/settings",
  token: () => "/api/token",
  health: () => "/api/health",
};

const STATE = {
  view: "library",
  theme: "light",
  query: "",
  library: [],
  catalog: [],
  // 浏览视图的分类逐层展开缓存：path -> 子项列表
  branches: {},
  expanded: new Set(),
  loadingPaths: new Set(),
  picked: [],
  tasks: [],
  records: [],
  settings: null,
  loading: false,
  pollTimer: null,
  historyStack: [],
};

const VIEW_META = {
  library: { title: "教材库", subtitle: "已下载的电子课本，点击卡片即可下载或预览" },
  browse: { title: "浏览资源", subtitle: "按学段、学科、版本浏览平台教材，勾选后可批量下载" },
  tasks: { title: "下载任务", subtitle: "当前进程内的下载进度与实时速率" },
  records: { title: "下载记录", subtitle: "全部历史任务，包含失败原因与保存位置" },
  settings: { title: "设置", subtitle: "登录凭据、书签开关与数据目录" },
};

/* ---------------------------------------------------------------- 工具函数 */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function formatBytes(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = size / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function formatDate(epochSeconds) {
  if (!epochSeconds) return "—";
  const date = new Date(epochSeconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function toast(message, kind = "") {
  const stack = $("#toasts");
  const node = document.createElement("div");
  node.className = `toast ${kind}`.trim();
  node.textContent = message;
  stack.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateY(6px)";
    node.style.transition = "opacity .2s ease, transform .2s ease";
    setTimeout(() => node.remove(), 220);
  }, kind === "err" ? 7000 : 3800);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    payload = await response.json().catch(() => null);
  }
  if (!response.ok) {
    const detail = payload && (payload.detail || payload.message);
    throw new Error(typeof detail === "string" ? detail : `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

function setStatus(text, right = "") {
  $("#status-text").textContent = text;
  $("#status-right").textContent = right;
}

/* ---------------------------------------------------------------- 主题 */

function applyTheme(theme) {
  STATE.theme = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = STATE.theme;
  try { localStorage.setItem("tchm-theme", STATE.theme); } catch (_) { /* 隐私模式下忽略 */ }
  $("#theme-icon").textContent = STATE.theme === "dark" ? "☀️" : "🌙";
  $("#theme-label").textContent = STATE.theme === "dark" ? "浅色模式" : "深色模式";
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("tchm-theme"); } catch (_) { /* ignore */ }
  if (!saved) {
    saved = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  applyTheme(saved);
}

/* ---------------------------------------------------------------- 导航 */

function setView(view, { push = true } = {}) {
  if (!VIEW_META[view]) return;
  if (push && STATE.view !== view) STATE.historyStack.push(STATE.view);
  STATE.view = view;
  STATE.query = "";
  $("#search-input").value = "";

  $$(".nav-item[data-view]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === view);
  });
  $("#view-title").textContent = VIEW_META[view].title;
  $("#view-subtitle").textContent = VIEW_META[view].subtitle;
  $("#search-input").placeholder = view === "browse" ? "搜索学段、学科、教材名…" : "搜索教材…";
  $("#search-input").parentElement.hidden = ["tasks", "settings"].includes(view);

  render();
  // 切换视图时懒加载该视图的数据
  if (view === "browse" && !STATE.query && !STATE.branches[""]) {
    loadBranch("");
  } else if (view === "records") {
    loadRecords().then(() => { if (STATE.view === "records") render(); });
  } else if (view === "tasks") {
    loadTasks().then(() => { if (STATE.view === "tasks") render(); });
  } else if (view === "settings") {
    loadSettings().then(() => { if (STATE.view === "settings") render(); });
  } else if (view === "library") {
    loadLibrary().then(() => { if (STATE.view === "library") render(); });
  }
}

/* ---------------------------------------------------------------- 数据加载 */

async function loadStats() {
  try {
    const stats = await request(API.stats());
    $("#side-meta").innerHTML =
      `教材 ${stats.library_count} 本<br>占用 ${formatBytes(stats.library_size)}` +
      (stats.active ? `<br>进行中 ${stats.active}` : "");
    setStatus(
      stats.active ? `正在下载 ${stats.active} 个任务` : "就绪",
      STATE.settings ? `版本 ${STATE.settings.version}` : ""
    );
  } catch (_) { /* 概览失败不影响主流程 */ }
}

async function loadLibrary() {
  STATE.loading = true;
  try {
    const data = await request(API.library(STATE.view === "library" ? STATE.query : ""));
    STATE.library = data.items || [];
  } catch (error) {
    STATE.library = [];
    toast(error.message, "err");
  } finally {
    STATE.loading = false;
  }
}

async function loadCatalog(force = false) {
  try {
    setStatus("正在获取资源目录…");
    const data = await request(API.catalog(STATE.query, force));
    STATE.catalog = data.items || [];
    setStatus(`搜索到 ${data.leaves} 本教材${data.truncated ? "（仅显示前 300 项）" : ""}`);
  } catch (error) {
    STATE.catalog = [];
    toast(error.message, "err");
    setStatus("资源目录获取失败");
  }
}

async function loadBranch(path, force = false) { // 懒加载某一层分类的子项
  if (STATE.loadingPaths.has(path)) return;
  if (STATE.branches[path] && !force) return;
  STATE.loadingPaths.add(path);
  try {
    const data = await request(API.catalogChildren(path, force));
    STATE.branches[path] = data.items || [];
  } catch (error) {
    STATE.branches[path] = [];
    toast(error.message, "err");
  } finally {
    STATE.loadingPaths.delete(path);
    if (STATE.view === "browse") render();
  }
}

async function loadTasks() {
  try {
    const data = await request(API.downloads());
    STATE.tasks = data.tasks || [];
    const active = STATE.tasks.filter((task) => !["completed", "skipped", "failed", "stopped"].includes(task.status)).length;
    const badge = $("#task-badge");
    badge.hidden = active === 0;
    badge.textContent = String(active);
  } catch (_) { /* 轮询失败静默重试 */ }
}

async function loadRecords() {
  try {
    const data = await request(API.records());
    STATE.records = data.records || [];
  } catch (error) {
    STATE.records = [];
    toast(error.message, "err");
  }
}

async function loadSettings() {
  try {
    STATE.settings = await request(API.settings());
  } catch (error) {
    toast(error.message, "err");
  }
}

/* ---------------------------------------------------------------- 渲染：教材库 */

const STATUS_CLASS = {
  completed: "ok", skipped: "ok", downloading: "", queued: "warn",
  bookmarking: "warn", failed: "err", stopped: "warn", parsing: "warn",
};

function libraryCard(item) {
  const isPdf = item.file_format === "pdf";
  const cover = isPdf
    ? `<img src="${API.thumb(item.file)}" alt="" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'cover-fallback',textContent:'📕'}))">`
    : `<span class="cover-fallback">${item.file_format === "mp3" ? "🎧" : "📄"}</span>`;
  return `
    <article class="book-card">
      <div class="book-cover">
        ${cover}
        <span class="format-tag">${esc(item.file_format || "file")}</span>
      </div>
      <div class="book-body">
        <h3 class="book-title" title="${esc(item.title)}">${esc(item.title)}</h3>
        <div class="book-meta">
          <span>${formatBytes(item.size)}</span>
          ${item.edition ? `<span>${esc(item.edition)}</span>` : ""}
          <span>${formatDate(item.modified_at)}</span>
        </div>
        <div class="book-actions">
          <a class="card-btn primary" href="${API.libraryFile(item.file)}" download="${esc(item.name)}">⬇ 下载</a>
          ${isPdf ? `<a class="card-btn ghost" href="${API.libraryFile(item.file, true)}" target="_blank" rel="noopener">预览</a>` : ""}
          <button class="card-btn ghost danger" type="button" data-delete="${esc(item.file)}" title="从磁盘删除">🗑</button>
        </div>
      </div>
    </article>`;
}

function renderLibrary() {
  const root = $("#view-root");
  if (STATE.loading && !STATE.library.length) {
    root.innerHTML = `<div class="grid">${'<div class="skeleton cover"></div>'.repeat(8)}</div>`;
    return;
  }
  if (!STATE.library.length) {
    const searching = Boolean(STATE.query);
    root.innerHTML = `
      <div class="empty">
        <span class="emoji">${searching ? "🔍" : "📚"}</span>
        <strong>${searching ? "没有匹配的教材" : "教材库还是空的"}</strong>
        <p>${searching
          ? `没有找到包含 “${esc(STATE.query)}” 的教材，试试其他关键词。`
          : "去「浏览资源」选择教材开始下载，或粘贴资源页面链接；下载完成的教材会自动出现在这里。"}</p>
        ${searching ? "" : '<button class="btn primary" type="button" data-goto="browse">去浏览资源</button>'}
      </div>`;
    return;
  }
  root.innerHTML = `
    <div class="section-head">
      <h2>全部教材</h2>
      <span>${STATE.library.length} 本 · ${formatBytes(STATE.library.reduce((sum, item) => sum + item.size, 0))}</span>
    </div>
    <div class="grid">${STATE.library.map(libraryCard).join("")}</div>`;
}

/* ---------------------------------------------------------------- 渲染：浏览资源 */

function treeRows(items, depth = 0) { // 递归渲染可展开的分类树
  return items.map((item) => {
    const expanded = STATE.expanded.has(item.path);
    const loading = STATE.loadingPaths.has(item.path);
    const children = expanded ? (STATE.branches[item.path] || []) : [];
    const picked = item.is_leaf && STATE.picked.some((entry) => entry.url === item.url);
    return `
      <div class="tree-row ${item.is_leaf ? "is-leaf" : ""} ${picked ? "is-active" : ""}"
           data-path="${esc(item.path)}" data-leaf="${item.is_leaf ? "1" : "0"}"
           data-url="${esc(item.url || "")}" data-name="${esc(item.name)}"
           style="padding-left:${9 + depth * 15}px" title="${esc(item.name)}">
        <span class="caret">${item.is_leaf ? (picked ? "✓" : "·") : loading ? "…" : expanded ? "▾" : "▸"}</span>
        <span class="tree-name">${esc(item.name)}</span>
        <span class="tree-count">${item.is_leaf ? "" : item.count}</span>
      </div>
      ${children.length ? treeRows(children, depth + 1) : ""}`;
  }).join("");
}

function renderBrowse() {
  const root = "#view-root";
  const searching = Boolean(STATE.query);
  const source = searching ? (STATE.catalog || []) : (STATE.branches[""] || []);
  const body = searching
    ? (source.length
        ? `<div class="tree">${source.map((item) => `
            <div class="tree-row is-leaf" data-leaf="1" data-url="${esc(item.url || "")}" data-name="${esc(item.name)}"
                 data-path="${esc(item.path)}"
                 ${STATE.picked.some((p) => p.url === item.url) ? 'class="tree-row is-leaf is-active"' : ""}
                 title="${esc(item.path.replace(/:/g, " / "))}">
              <span class="caret">${STATE.picked.some((p) => p.url === item.url) ? "✓" : "·"}</span>
              <span class="tree-name">${esc(item.name)}</span>
              <span class="tree-count">${esc(item.path.split(":").slice(-3, -1).join(" / "))}</span>
            </div>`).join("")}</div>`
        : `<div class="empty"><span class="emoji">🔍</span><strong>没有匹配的资源</strong><p>换个关键词试试，或清空搜索框逐层浏览。</p></div>`)
    : (source.length
        ? `<div class="tree">${treeRows(source)}</div>`
        : `<div class="empty"><span class="emoji">🧭</span><strong>资源目录加载中</strong><p>首次加载需要从平台拉取目录，请稍候…</p></div>`);

  $(root).innerHTML = `
    <div class="browse-layout">
      <section class="panel">
        <div class="panel-head">
          <h3>${searching ? "搜索结果" : "资源目录"}</h3>
          <span class="pill">${searching ? `${source.length} 本` : "逐层展开"}</span>
        </div>
        <div class="panel-body scroll" id="tree-body">${body}</div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>待下载列表</h3>
          <button class="card-btn ghost danger" type="button" data-clear-picked ${STATE.picked.length ? "" : "disabled"}>清空</button>
        </div>
        <div class="panel-body">
          <div class="stack">
            <div>
              <span class="field-label">已选教材（${STATE.picked.length}）</span>
              ${STATE.picked.length ? `
                <ul class="picked-list">
                  ${STATE.picked.map((item) => `
                    <li class="picked-item">
                      <span title="${esc(item.name)}">${esc(item.name)}</span>
                      <button type="button" data-unpick="${esc(item.url)}" title="移除">×</button>
                    </li>`).join("")}
                </ul>` : `<div class="hint">在左侧逐层展开分类并点击教材，即可加入待下载列表；也可以直接在下方粘贴资源页面链接。</div>`}
            </div>

            <div>
              <span class="field-label">资源页面链接（每行一个，可选）</span>
              <textarea class="url-input" id="url-input" placeholder="https://basic.smartedu.cn/tchMaterial/detail?contentType=assets_document&amp;contentId=..."></textarea>
            </div>

            <label class="switch">
              <input type="checkbox" id="bookmark-toggle" ${STATE.settings && STATE.settings.bookmarks === false ? "" : "checked"}>
              <span class="track"></span>
              <span>为 PDF 添加章节书签</span>
            </label>

            <div class="row end">
              <button class="btn" type="button" id="parse-btn">仅解析</button>
              <button class="btn primary" type="button" id="download-btn">开始下载</button>
            </div>
          </div>
        </div>
      </section>
    </div>`;
}

function collectUrls() {
  const manual = ($("#url-input")?.value || "")
    .split("\n").map((line) => line.trim()).filter(Boolean);
  const picked = STATE.picked.map((item) => item.url);
  return Array.from(new Set([...manual, ...picked]));
}

/* ---------------------------------------------------------------- 渲染：任务 */

function taskCard(task) {
  const status = task.status || "queued";
  const label = task.status_label || (STATE.labels && STATE.labels[status]) || status;
  const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
  const done = ["completed", "skipped"].includes(status);
  const failed = status === "failed";
  const active = !["completed", "skipped", "failed", "stopped"].includes(status);
  const sizeText = task.total_size
    ? `${formatBytes(task.downloaded_size || 0)} / ${formatBytes(task.total_size)}`
    : formatBytes(task.downloaded_size || 0);
  const speed = task.speed ? `${formatBytes(task.speed)}/s` : "";

  return `
    <article class="task">
      <div class="task-top">
        <span class="task-title" title="${esc(task.title || task.file || "")}">${esc(task.title || task.file || "未知任务")}</span>
        <span class="pill ${STATUS_CLASS[status] || ""}">${esc(label)}</span>
      </div>
      <div class="progress ${done ? "is-done" : failed ? "is-error" : ""}"><i style="width:${progress}%"></i></div>
      <div class="task-meta">
        <span>${progress.toFixed(1)}%</span>
        <span>${sizeText}</span>
        ${speed ? `<span>${speed}</span>` : ""}
        ${task.file ? `<span title="${esc(task.file)}">${esc(task.file)}</span>` : ""}
      </div>
      ${task.error ? `<div class="hint" style="border-left-color:var(--err)">${esc(task.error)}</div>` : ""}
      <div class="row">
        ${active ? `<button class="card-btn danger" type="button" data-stop="${esc(task.id)}">停止</button>` : ""}
        ${done && task.file ? `<a class="card-btn primary" href="${API.libraryFile(task.file)}" download>下载文件</a>` : ""}
      </div>
    </article>`;
}

function renderTasks() {
  const root = $("#view-root");
  if (!STATE.tasks.length) {
    root.innerHTML = `
      <div class="empty">
        <span class="emoji">📥</span>
        <strong>当前没有下载任务</strong>
        <p>从「浏览资源」选择教材即可开始下载。历史任务可以在「下载记录」中查看。</p>
        <button class="btn primary" type="button" data-goto="browse">去浏览资源</button>
      </div>`;
    return;
  }
  const active = STATE.tasks.filter((task) => !["completed", "skipped", "failed", "stopped"].includes(task.status)).length;
  root.innerHTML = `
    <div class="section-head">
      <h2>任务队列</h2>
      <span>${STATE.tasks.length} 个任务 · ${active} 个进行中</span>
    </div>
    <div class="row" style="margin-bottom:12px">
      <button class="btn danger" type="button" id="stop-all" ${active ? "" : "disabled"}>全部停止</button>
      <button class="btn" type="button" id="clear-finished">清理已结束</button>
    </div>
    <div class="task-list">${STATE.tasks.map(taskCard).join("")}</div>`;
}

/* ---------------------------------------------------------------- 渲染：记录 */

function renderRecords() {
  const root = $("#view-root");
  if (!STATE.records.length) {
    root.innerHTML = `
      <div class="empty">
        <span class="emoji">🗂️</span>
        <strong>还没有下载记录</strong>
        <p>每次下载都会在这里留下记录，包含保存位置、文件大小与失败原因。</p>
      </div>`;
    return;
  }
  const rows = STATE.records.map((record) => {
    const status = record.status || "";
    const label = record.status_label || (STATE.labels && STATE.labels[status]) || status;
    const file = record.file || "";
    return `
      <tr>
        <td title="${esc(record.title || "")}">${esc(record.title || record.file || "—")}</td>
        <td><span class="pill ${STATUS_CLASS[status] || ""}">${esc(label)}</span></td>
        <td class="num">${record.size ? formatBytes(record.size) : "—"}</td>
        <td class="num" title="${esc(file)}">${file ? esc(file.split("/").pop()) : "—"}</td>
        <td class="num">${formatDate(record.finished_at || record.created_at)}</td>
        <td>
          ${file && status !== "failed" ? `<a class="card-btn" href="${API.libraryFile(file)}" download>下载</a>` : ""}
          ${record.error ? `<span class="pill err" title="${esc(record.error)}">原因</span>` : ""}
        </td>
      </tr>`;
  }).join("");

  root.innerHTML = `
    <div class="section-head">
      <h2>历史记录</h2>
      <div class="row">
        <span>共 ${STATE.records.length} 条</span>
        <button class="btn danger" type="button" id="clear-records">清空记录</button>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>教材</th><th>状态</th><th>大小</th><th>文件</th><th>时间</th><th>操作</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="hint" style="margin-top:12px">清空记录不会删除已下载的教材文件；教材文件位于挂载目录的 <code>downloads/</code> 下。</div>`;
}

/* ---------------------------------------------------------------- 渲染：设置 */

function renderSettings() {
  const root = $("#view-root");
  const settings = STATE.settings;
  if (!settings) {
    root.innerHTML = `<div class="empty"><span class="emoji">⚙️</span><strong>正在读取设置…</strong></div>`;
    return;
  }
  root.innerHTML = `
    <div class="browse-layout">
      <section class="panel">
        <div class="panel-head"><h3>登录凭据</h3>
          <span class="pill ${settings.logged_in ? "ok" : "warn"}">${settings.logged_in ? "已登录" : "未登录"}</span>
        </div>
        <div class="panel-body">
          <div class="stack">
            <div class="hint">
              未登录时可以下载公开教材；部分私有资源需要有效的 Access Token。
              在浏览器登录<strong>国家中小学智慧教育平台</strong>后，按下方步骤获取凭据并粘贴。
            </div>
            <div>
              <span class="field-label">凭据 JSON（access_token、mac_key、diff 三项）</span>
              <textarea class="url-input" id="token-input" placeholder='{"access_token":"…","mac_key":"…","diff":0}'></textarea>
            </div>
            <div class="row end">
              <button class="btn" type="button" id="token-clear">清除凭据</button>
              <button class="btn primary" type="button" id="token-save">保存凭据</button>
            </div>
            <details>
              <summary style="cursor:pointer;color:var(--text-muted);font-size:12.5px">如何获取？</summary>
              <div class="hint" style="margin-top:9px">
                1. 打开 <a href="https://auth.smartedu.cn/uias/login" target="_blank" rel="noopener">登录页面</a> 并完成登录；<br>
                2. 在平台页面按 <code>F12</code> 打开开发者工具，切换到「控制台」；<br>
                3. 粘贴并执行下面的脚本，复制输出的整段 JSON：<br>
                <textarea class="url-input" id="token-script" readonly style="min-height:150px;margin-top:8px"></textarea>
              </div>
            </details>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h3>下载与存储</h3></div>
        <div class="panel-body">
          <div class="stack">
            <label class="switch">
              <input type="checkbox" id="default-bookmarks" ${settings.bookmarks === false ? "" : "checked"}>
              <span class="track"></span><span>默认添加 PDF 章节书签</span>
            </label>
            <div>
              <span class="field-label">数据目录</span>
              <div class="picked-item"><span title="${esc(settings.data_dir)}">${esc(settings.data_dir)}</span></div>
            </div>
            <div>
              <span class="field-label">教材目录</span>
              <div class="picked-item"><span title="${esc(settings.downloads_dir)}">${esc(settings.downloads_dir)}</span></div>
            </div>
            <div class="hint">
              在 Docker 中把宿主机目录挂载到 <code>${esc(settings.data_dir)}</code>，
              即可让下载的教材与记录随容器重建而保留。
            </div>
            <div class="row"><span class="pill">版本 ${esc(settings.version)}</span></div>
          </div>
        </div>
      </section>
    </div>`;

  $("#token-script").value = TOKEN_SCRIPT;
}

const TOKEN_SCRIPT = `(function () {
  const authKey = Object.keys(localStorage).find(
    key => /^ND_UC_AUTH-[^&]+&[^&]+&token$/.test(key),
  );
  if (!authKey) {
    console.error("未找到登录凭据，请确保已登录！");
    return;
  }
  const tokenData = JSON.parse(localStorage.getItem(authKey));
  const { access_token, mac_key, diff } = JSON.parse(tokenData.value);
  console.log(JSON.stringify({ access_token, mac_key, diff }));
})();`;

/* ---------------------------------------------------------------- 渲染入口 */

function render() {
  if (STATE.view === "library") renderLibrary();
  else if (STATE.view === "browse") renderBrowse();
  else if (STATE.view === "tasks") renderTasks();
  else if (STATE.view === "records") renderRecords();
  else if (STATE.view === "settings") renderSettings();
}

async function refreshCurrent({ silent = false } = {}) {
  if (!silent) setStatus("正在刷新…");
  if (STATE.view === "library") await loadLibrary();
  else if (STATE.view === "browse") {
    STATE.branches = {};
    STATE.expanded.clear();
    await loadBranch("", true);
  }
  else if (STATE.view === "tasks") await loadTasks();
  else if (STATE.view === "records") await loadRecords();
  else if (STATE.view === "settings") await loadSettings();
  render();
  await loadStats();
  if (!silent) setStatus("就绪");
}

/* ---------------------------------------------------------------- 动作 */

async function startDownload() {
  const urls = collectUrls();
  if (!urls.length) {
    toast("请先选择教材，或粘贴资源页面链接", "warn");
    return;
  }
  const button = $("#download-btn");
  button.disabled = true;
  button.textContent = "正在解析…";
  try {
    const bookmarks = $("#bookmark-toggle")?.checked !== false;
    const data = await request(API.downloads(), {
      method: "POST",
      body: JSON.stringify({ urls, bookmarks }),
    });
    const failed = data.failed || [];
    toast(`已创建 ${data.created} 个下载任务`, "ok");
    if (failed.length) toast(`${failed.length} 条链接解析失败`, "warn");
    STATE.picked = [];
    setView("tasks");
  } catch (error) {
    toast(error.message, "err");
    button.disabled = false;
    button.textContent = "开始下载";
  }
}

async function parseOnly() {
  const urls = collectUrls();
  if (!urls.length) {
    toast("请先选择教材，或粘贴资源页面链接", "warn");
    return;
  }
  const button = $("#parse-btn");
  button.disabled = true;
  button.textContent = "解析中…";
  try {
    const data = await request(API.parse(), {
      method: "POST",
      body: JSON.stringify({ urls, bookmarks: $("#bookmark-toggle")?.checked !== false }),
    });
    const lines = (data.resources || []).map((item) => `${item.title}（${item.file_format}）`).join("\n");
    if (lines) {
      await navigator.clipboard.writeText((data.resources || []).map((item) => item.url).join("\n")).catch(() => {});
      toast(`解析到 ${data.resources.length} 个资源，直链已复制到剪贴板`, "ok");
      setStatus(`解析到 ${data.resources.length} 个资源`, lines.split("\n")[0] || "");
    } else {
      toast("未能解析出可下载的资源", "warn");
    }
    if ((data.failed || []).length) toast(`${data.failed.length} 条链接解析失败`, "warn");
  } catch (error) {
    toast(error.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = "仅解析";
  }
}

async function deleteLibraryFile(file) {
  if (!window.confirm(`确定要从磁盘删除这本教材吗？\n\n${file}\n\n此操作不可撤销。`)) return;
  try {
    await request(API.deleteFile(file), { method: "DELETE" });
    toast("已删除教材文件", "ok");
    await refreshCurrent({ silent: true });
  } catch (error) {
    toast(error.message, "err");
  }
}

async function saveToken() {
  const token = $("#token-input")?.value || "";
  try {
    const data = await request(API.token(), { method: "POST", body: JSON.stringify({ token }) });
    toast(data.logged_in ? "登录凭据已保存" : "登录凭据已清除", "ok");
    $("#token-input").value = "";
    await loadSettings();
    render();
    await loadStats();
  } catch (error) {
    toast(error.message, "err");
  }
}

/* ---------------------------------------------------------------- 事件绑定 */

function bindEvents() {
  $("#nav").addEventListener("click", (event) => {
    const button = event.target.closest(".nav-item[data-view]");
    if (button) setView(button.dataset.view);
  });

  $("#theme-toggle").addEventListener("click", () => {
    applyTheme(STATE.theme === "dark" ? "light" : "dark");
  });

  $("#refresh-btn").addEventListener("click", () => refreshCurrent());

  let searchTimer = null;
  $("#search-input").addEventListener("input", (event) => {
    STATE.query = event.target.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      if (STATE.view === "browse") { await loadCatalog(); render(); }
      else if (STATE.view === "library") { await loadLibrary(); render(); }
    }, 260);
  });

  // 全局事件委托，避免视图重绘后重复绑定
  $("#view-root").addEventListener("click", async (event) => {
    const target = event.target;

    const goto = target.closest("[data-goto]");
    if (goto) { setView(goto.dataset.goto); return; }

    const del = target.closest("[data-delete]");
    if (del) { await deleteLibraryFile(del.dataset.delete); return; }

    const stop = target.closest("[data-stop]");
    if (stop) {
      try {
        await request(API.stop(stop.dataset.stop), { method: "POST" });
        toast("已请求停止该任务", "ok");
        await loadTasks(); render();
      } catch (error) { toast(error.message, "err"); }
      return;
    }

    if (target.closest("#stop-all")) {
      try {
        const data = await request(API.stopAll(), { method: "POST" });
        toast(`已请求停止 ${data.stopped} 个任务`, "ok");
        await loadTasks(); render();
      } catch (error) { toast(error.message, "err"); }
      return;
    }

    if (target.closest("#clear-finished")) {
      try {
        await request(API.clearFinished(), { method: "POST" });
        await loadTasks(); render();
      } catch (error) { toast(error.message, "err"); }
      return;
    }

    if (target.closest("#clear-records")) {
      if (!window.confirm("确定清空全部下载记录吗？已下载的教材文件不会被删除。")) return;
      try {
        await request(API.clearRecords(), { method: "DELETE" });
        await loadRecords(); render();
        toast("下载记录已清空", "ok");
      } catch (error) { toast(error.message, "err"); }
      return;
    }

    if (target.closest("[data-clear-picked]")) {
      STATE.picked = []; render(); return;
    }

    const unpick = target.closest("[data-unpick]");
    if (unpick) {
      STATE.picked = STATE.picked.filter((item) => item.url !== unpick.dataset.unpick);
      render(); return;
    }

    const row = target.closest(".tree-row");
    if (row) {
      const path = row.dataset.path || "";
      if (row.dataset.leaf === "1" && row.dataset.url) {
        const url = row.dataset.url;
        if (STATE.picked.some((item) => item.url === url)) {
          STATE.picked = STATE.picked.filter((item) => item.url !== url);
        } else {
          STATE.picked.push({ url, name: row.dataset.name });
        }
        render();
      } else if (path) {
        // 分类节点：切换展开并懒加载子项
        if (STATE.expanded.has(path)) {
          STATE.expanded.delete(path);
          render();
        } else {
          STATE.expanded.add(path);
          if (!STATE.branches[path]) {
            render();
            await loadBranch(path);
          } else {
            render();
          }
        }
      }
      return;
    }

    if (target.closest("#download-btn")) { await startDownload(); return; }
    if (target.closest("#parse-btn")) { await parseOnly(); return; }

    if (target.closest("#token-save")) {
      await saveToken();
      return;
    }
    if (target.closest("#token-clear")) {
      if (!window.confirm("确定清除已保存的登录凭据吗？")) return;
      try {
        await request(API.token(), { method: "POST", body: JSON.stringify({ token: "" }) });
        toast("登录凭据已清除", "ok");
        await loadSettings(); render();
      } catch (error) { toast(error.message, "err"); }
      return;
    }
  });

  $("#view-root").addEventListener("change", async (event) => {
    const target = event.target;
    if (target.id === "default-bookmarks") {
      try {
        STATE.settings = await request(API.saveSettings(), {
          method: "PUT",
          body: JSON.stringify({ bookmarks: target.checked }),
        });
        toast("设置已保存", "ok");
      } catch (error) { toast(error.message, "err"); }
    }
  });

  // 键盘：Esc 返回上一视图
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && STATE.historyStack.length) {
      setView(STATE.historyStack.pop() || "library", { push: false });
    }
  });
}

/* ---------------------------------------------------------------- 轮询 */

function startPolling() {
  clearInterval(STATE.pollTimer);
  STATE.pollTimer = setInterval(async () => {
    await loadTasks();
    const badge = $("#task-badge");
    if (STATE.view === "tasks") render();
    else if (STATE.view === "library" && STATE.tasks.some((task) => task.status === "completed")) {
      // 任务完成时自动刷新教材库，让新教材及时出现
      STATE.tasks = STATE.tasks.filter((task) => task.status !== "completed");
      await loadLibrary(); render();
    }
    await loadStats();
    if (badge.hidden && STATE.view !== "tasks") return;
  }, 1500);
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  initTheme();
  bindEvents();
  await loadSettings();
  await loadLibrary();
  setView("library", { push: false });
  await loadStats();
  startPolling();
}

document.addEventListener("DOMContentLoaded", boot);
