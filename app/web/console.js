
"use strict";
/* ================= 基础工具 ================= */
const $ = sel => document.querySelector(sel);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
/* ================= 手机版 Tab 栅 + 抽屉 =================
 * 手机上靠下面四个指可及的 Tab + 抽屉（“更多”）转站；
 * Tab 由 JS 从现有导航派生，不另写一份清单。
 * 表格里每个 td 打上 data-label 后交给 CSS 排版，JS 只负责打标签。
 * ==================================================================== */
const MNAV = [
  { view: "overview", ico: "📊", label: "总览" },
  { view: "models",   ico: "🧩", label: "模型" },
  { view: "burner",   ico: "🔥", label: "消耗器" },
  { view: "more",     ico: "•••", label: "更多" },
];

function mnavBuild() {
  const bar = $("#mnav");
  if (!bar) return;
  bar.innerHTML = MNAV.map(m =>
    `<a href="javascript:void 0" data-mnav="${m.view}"><span class="ico">${m.ico}</span>${m.label}</a>`
  ).join("");
  bar.querySelectorAll("a[data-mnav]").forEach(a => {
    a.onclick = () => {
      const v = a.dataset.mnav;
      if (v === "more") { mnavMore(); return; }
      switchView(v);
    };
  });
}

function mnavPanel(html) {
  const d = $("#drawer");
  if (!d) return;
  d.innerHTML = html;
  d.classList.add("open");
}

function mnavClose() {
  const d = $("#drawer");
  if (d) d.classList.remove("open");
}

function mnavMore() {
  // 「更多」抽：桌面侧栏的其余入口，一行一个，挤指够得到
  const items = [
    { view: "pool", ico: "🔑", label: "凭证池" },
    { view: "requests", ico: "📨", label: "请求记录" },
    { view: "usage", ico: "📈", label: "用量统计" },
    { view: "report", ico: "🩺", label: "体检" },
    { href: "/ui/agent", ico: "🤖", label: "Agent 任务台" },
  ];
  const rows = items.map(m => {
    const attrs = m.href ? `href="${m.href}"` : `href="javascript:void 0" data-go="${m.view}"`;
    return `<a class="mnav-row" ${attrs}><span class="ico">${m.ico}</span>`
      + `<span class="grow" style="text-align:left">${m.label}</span><span class="muted">›</span></a>`;
  }).join("");
  mnavPanel(`<div class="dr-head"><h2 class="grow">更多</h2>`
    + `<button class="mini" id="mnav-close">✕ 关闭</button></div>`
    + `<div class="mnav-list">${rows}</div>`);
  const closeBtn = $("#mnav-close");
  if (closeBtn) closeBtn.onclick = mnavClose;
  document.querySelectorAll("#drawer a[data-go]").forEach(a => {
    a.onclick = () => { mnavClose(); switchView(a.dataset.go); };
  });
}

function mnavSync() {
  const bar = $("#mnav");
  if (!bar) return;
  bar.querySelectorAll("a[data-mnav]").forEach(a => {
    a.classList.toggle("on", a.dataset.mnav === state.view);
  });
}

/** 手机上把长页面改成手风琴：每个 h4.sec 开头的片段包成可折叠的一块。

    消耗器页 3500+px，全展开时运营者得滚半天才找得到想要的那一节。
    默认展开第一个（通常是状态/总览），其余折起来。
*/
function accordionSections(root) {
  if (!window.matchMedia("(max-width: 860px)").matches) return;
  const scope = root || document;
  scope.querySelectorAll("section.view h4.sec").forEach((h) => {
    if (h.dataset.acc === "1") return;
    h.dataset.acc = "1";
    // 默认展开「这个视图里的第一个」而不是全站第一个：
    // forEach 索引跨所有 section，用它判断会让后面几个视图永远折起。
    const view = h.closest("section.view");
    const isFirst = view && view.querySelector("h4.sec") === h;
    // 收集这个 h 之后、下一个 h4.sec 之前的所有兄彆
    const body = [];
    let n = h.nextElementSibling;
    while (n && !(n.tagName === "H4" && n.classList.contains("sec"))) {
      body.push(n);
      n = n.nextElementSibling;
    }
    if (!body.length) return;
    const sec = document.createElement("div");
    sec.className = "msec" + (isFirst ? " open" : "");
    h.parentNode.insertBefore(sec, h);
    sec.innerHTML = `<button class="msec-head" type="button">
        <span class="msec-ico">📎</span>
        <span class="msec-title">${esc(h.textContent || "")}</span>
        <span class="msec-arrow">›</span>
      </button><div class="msec-body"></div>`;
    const hold = sec.querySelector(".msec-body");
    body.forEach(el => hold.appendChild(el));
    sec.querySelector(".msec-head").onclick = () => sec.classList.toggle("open");
  });
}

/** 给表格里每个 td 打上 data-label（取自 thead 的 th）。
 *  已经有的不覆盖：那些是 JS 自己写好的更明确的名字。 */
function tableCards() {
  if (window.matchMedia("(max-width: 860px)").matches === false) return;
  document.querySelectorAll(".tablewrap table").forEach(table => {
    const heads = [...table.querySelectorAll("thead th")].map(th =>
      (th.textContent || "").trim());
    if (!heads.length) return;
    [...table.querySelectorAll("tbody tr")].forEach(tr => {
      [...tr.children].forEach((td, i) => {
        if (!(td instanceof HTMLTableCellElement)) return;
        if (td.dataset.label !== undefined) return;
        td.dataset.label = heads[i] || "";
      });
    });
  });
}

const state = {
  view: "overview",
  token: localStorage.getItem("zkai_admin_token") || "",
  timer: null,
  poolAt: 0,
  req: { offset: 0, size: 50, total: 0 },
  providers: [],
  usageDays: 7,
};

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/** DB 时间戳是「无时区 UTC」——补上 Z 再按本地时区展示。 */
function fmtTime(iso) {
  if (!iso) return "-";
  let s = String(iso);
  if (s.includes("T") && !/([Zz]|[+-]\d\d:?\d\d)$/.test(s)) s += "Z";
  const d = new Date(s);
  if (isNaN(d)) return s;
  // 今天以内只显小时分秒（省空间），超过就带日期——
  // 否则手机上看到一个 10:44:41 完全无法判断是否过期。
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const opts = {
    hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  };
  if (!sameDay) { opts.month = "numeric"; opts.day = "numeric"; }
  return d.toLocaleString("zh-CN", opts);
}
function fmtEpoch(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}
function ago(iso) {
  let s = String(iso || "");
  if (s.includes("T") && !/([Zz]|[+-]\d\d:?\d\d)$/.test(s)) s += "Z";
  const d = new Date(s); if (isNaN(d)) return "-";
  const sec = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (sec < 60) return Math.round(sec) + " 秒前";
  if (sec < 3600) return Math.round(sec / 60) + " 分钟前";
  if (sec < 86400) return (sec / 3600).toFixed(1) + " 小时前";
  return (sec / 86400).toFixed(1) + " 天前";
}
function num(n) { return (n ?? 0).toLocaleString("zh-CN"); }
function pct(v) { return v == null ? "-" : (v * 100).toFixed(1) + "%"; }

/** 消耗器账号名简称：SENSENOVA_API_KEY_02 → S_02，SENSENOVA_API_KEY → S_01。
 *  全名太长（23 字符），表格里每行都显示会挤掉有用信息的空间。
 *  title 属性保留全名，鼠标悬停可见。 */
function sName(full) {
  const s = String(full || "");
  const m = s.match(/SENSENOVA_API_KEY(?:_(\d+))?$/);
  if (!m) return s;
  return "S_" + (m[1] ? m[1].padStart(2, "0") : "01");
}
function dur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m" + (sec % 60 ? (sec % 60) + "s" : "");
  return Math.floor(sec / 3600) + "h" + Math.floor((sec % 3600) / 60) + "m";
}
const STATUS_PILL = {
  healthy: ["green", "正常"], cooldown: ["amber", "冷却"],
  unhealthy: ["red", "异常"], disabled: ["gray", "禁用"],
  success: ["green", "成功"], error: ["red", "失败"],
  cancelled: ["gray", "取消"], pending: ["blue", "进行中"],
};
/** 已同步到 YAML 文件时告诉用户写到了哪里；模板模式（无本地文件）时提醒。 */
function syncedNote(resp) {
  const s = resp && resp.synced;
  if (s) return `（已同步 ${s}）`;
  if (s === null) return "（模板模式：只存数据库，未改文件）";
  return "";
}
/* ================= 弹窗基座（唯一出入口） ================= */
/*
 * 2026-09-30：修两个「点了没反应」——新建供应商后不自动进入加 Key、模型市场
 * 点添加没反应。根因是七个 .modal-mask 原本共用一个 z-index（55），谁在上层
 * 只看 DOM 顺序；子弹窗（#addkey-modal / #model-modal）写在父弹窗
 * （#providers-modal / #market-modal）**前面**，于是打开后立刻被盖住。
 *
 * 现在两层防护，缺一不可：
 *   ① CSS：按父子关系分层（见 .modal-mask 附近的注释）；
 *   ② JS：openModal 打开子弹窗前先关掉其它遮罩，绝不留两层。
 * ② 是必须的：只靠 ① 会得到「两层半透明遮罩叠着」的界面——能点了，但看不清，
 * 且关掉外层后里层还在，运营者以为没关干净。
 *
 * 用法：openModal("#addkey-modal") / closeModal("#addkey-modal")，
 * 不要再去写 classList.add("show")。
 */
const MODAL_IDS = [
  "#model-modal", "#alias-modal", "#limits-modal", "#market-modal",
  "#addkey-modal", "#providers-modal", "#chatgpt-modal", "#auth",
];
/** 打开一个弹窗，并关掉其它所有遮罩（父弹窗由调用方负责收尾）。 */
function openModal(sel) {
  const target = $(sel);
  if (!target) return;
  MODAL_IDS.filter(id => id !== sel).forEach(id => {
    const el = $(id);
    if (el) el.classList.remove("show");
  });
  target.classList.add("show");
}
/** 关掉除 keep 以外的全部弹窗；keep 通常传正在展示的那个，避免误伤。 */
function closeModals(keep) {
  MODAL_IDS.filter(id => id !== keep).forEach(id => {
    const el = $(id);
    if (el) el.classList.remove("show");
  });
}
function closeModal(sel) {
  const el = $(sel);
  if (el) el.classList.remove("show");
}

function pill(status) {
  const [cls, label] = STATUS_PILL[status] || ["gray", status || "-"];
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}
function rankBadge(n) {
  const k = n === 1 ? "rank-1" : n === 2 ? "rank-2" : n === 3 ? "rank-3" : "rank-rest";
  return `<span class="rank ${k}">${n}</span>`;
}


async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers);
  if (state.token) headers["X-Admin-Token"] = state.token;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (resp.status === 401) { showAuth(); throw new Error("管理令牌无效或缺失，请重新粘贴令牌"); }
  let data = null;
  try { data = await resp.json(); } catch { /* 无 body */ }
  if (!resp.ok) {
    const msg = data && data.error && data.error.message
      ? data.error.message : (data && data.detail && data.detail.error
        ? data.detail.error.message : "HTTP " + resp.status);
    throw new Error(path + " → " + msg);
  }
  return data;
}

/* ================= 视图注册 ================= */
const LOADERS = {
  overview: loadOverview, pool: loadPool, requests: loadRequests,
  usage: loadUsage, models: loadModels, burner: loadBurner, report: loadReport,
};
function switchView(name) {
  if (state.view === "burner" && name !== "burner") blStop();
  if (state.view === "report" && name !== "report") reportStop();
  state.view = name;
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll("section.view").forEach(s =>
    s.classList.toggle("active", s.id === "view-" + name));
  refresh();
}
async function refresh() {
  const fn = LOADERS[state.view];
  if (!fn) return;
  try { await fn(); }
  catch (err) { toast(err.message, "err"); }
  // 手机上把表格变卡片：每次重绘后表格都是新的，标签要重打
  tableCards();
  accordionSections();
  mnavSync();
}
function setupTimer() {
  clearInterval(state.timer);
  const sec = Number($("#auto-interval").value);
  if (sec > 0) state.timer = setInterval(refresh, sec * 1000);
}
setInterval(tickCountdowns, 1000);
function tickCountdowns() {
  document.querySelectorAll("[data-cd-until]").forEach(el => {
    const left = Number(el.dataset.cdUntil) - Date.now() / 1000;
    if (left <= 0) { el.textContent = "已到期"; el.className = "muted"; }
    else { el.textContent = dur(left); el.className = "small" ; el.style.color = "var(--warn)"; }
  });
}

/* ================= 总览 ================= */
/* “需要处理”面板：把配置层面的坏味道直接摊在首页，每行带一个一键修复动作。
   判断只用 /health 与 /admin/providers 已经给出的字段，不做额外请求。 */
function attentionPanel(providers, warnings, config) {
  const cfg = config || {};
  const rows = [];
  (providers || []).forEach(p => {
    if (!p.enabled) return;
    const creds = p.credentials || [];
    if (p.requires_credential && !creds.length) {
      rows.push({
        level: "err", pid: p.id,
        msg: `${p.id} 还没有任何 Key——请求永远走不到它`,
        fix: "key", label: "➕ 去加 Key",
      });
      return;
    }
    const noSecret = creds.filter(c => c.secret_source === "missing");
    const keyProblem = (p.requires_credential && !creds.length) || noSecret.length;
    if (noSecret.length) {
      rows.push({
        level: "err", pid: p.id,
        msg: `${p.id} 有 ${noSecret.length} 把 Key 没配上实际值（${noSecret
          .slice(0, 2).map(c => esc(c.secret_ref || c.id)).join("、")}${noSecret.length > 2 ? " 等" : ""} 在 .env 里是空的）`,
        fix: "key", label: "✏️ 去填 Key",
      });
    }
    if (!(p.models || []).length) {
      rows.push({
        level: "warn", pid: p.id,
        msg: `${p.id} 一个模型都没挂上——它能调，但没有任何模型走它`,
        fix: "model", label: "🧺 去模型市场挑",
      });
    }
    // 可用性只在“Key 层面没问题”时补充：没配 Key 的供应商已经报过了，
    // 再说一遍“都在冷却”只是同一件事的第二副面孔。
    if (!keyProblem && creds.length && (p.models || []).length && !p.available) {
      const off = creds.filter(c => c.status === "cooldown" || c.status === "disabled" || !c.enabled).length;
      if (off === creds.length) {
        rows.push({
          level: "warn", pid: p.id,
          msg: `${p.id} 的全部 ${creds.length} 把 Key 都在冷却/禁用中，暂时不可用`,
          fix: "warm", label: "🔥 清冷却",
        });
      }
    }
  });
  (warnings || []).forEach(w => rows.push({ level: "warn", msg: `配置警告：${esc(w)}` }));
  // 凭据要读的 Key 不在 .env 里：本机好用，换机会静默丢掉。这条以前无处显示，
  // 只有跑 scripts/check_config.py 才知道——2026-09-29 就在这台机器上抓到一把。
  const gaps = cfg.credential_env_gaps || {};
  const gapRows = Object.entries(gaps).map(([name, ids]) => ({
    level: "warn", fix: "envgap", name, label: "📋 怎么补",
    msg: `${esc((ids || []).join("、"))} 读的 ${esc(name)} 不在 .env 里` +
      "——本机能用，但迁移包只带 .env，换机会丢这把 Key",
    // 直接从 OS 环境变量里取值，全程不回显；改 .env 前先备份
    cmd: [
      `$v=[Environment]::GetEnvironmentVariable("${esc(name)}","User")`,
      `if (-not $v) { $v=[Environment]::GetEnvironmentVariable("${esc(name)}","Machine") }`,
      `Add-Content -Path .env -Value "${esc(name)}=$v"`,
    ].join("\n"),
  }));
  rows.push(...gapRows);
  if (!rows.length) return "";

  const worst = rows.reduce((acc, r) =>
    (acc === "err" || r.level === "err") ? "err" : acc, rows[0].level);
  const ico = { err: "⛔", warn: "⚠️", ok: "✅" }[worst] || "⚠️";
  return `<div class="attention${worst === "err" ? " is-err" : ""}">
    <h3>${ico} 需要处理（${rows.length}）</h3>
    ${rows.map((r, i) => `<div class="att-row">
      <span class="att-ico">${r.level === "err" ? "⛔" : "⚠️"}</span>
      <span class="att-msg">${r.msg}</span>
      ${r.fix === "warm"
        ? `<button class="mini" data-fix="warm">🔥 清冷却</button>`
        : `<button class="mini primary" data-fix="${r.fix}" data-pid="${esc(r.pid || "")}">${r.label}</button>`}
    </div>${r.cmd
      ? `<div class="att-fix" id="att-fix-${i}" hidden>
           <pre>${r.cmd}</pre>
           <p class="muted">值从 OS 环境变量里取，全程不回显；改 .env 前先备份，
             改完跑一次 <code>scripts\\check_config.py</code> 确认这条警告消失。</p>
         </div>`
      : ""}`).join("")}
  </div>`;
}

function renderServerLine(health) {
  const dep = health.deploy || {};
  const bits = [
    `${health.app} v${health.version}`,
    `数据库 ${health.database.ok ? "正常" : "异常"}`,
    `已运行 ${dur(health.uptime_seconds)}`,
  ];
  // 部署在服务器上时把入口形态说出来：运营者第一件事就是确认该抄哪个 base_url
  if (dep.is_remote) {
    bits.push(`服务器模式（入口 ${dep.public_base_url || "?"}）`);
  } else {
    bits.push("本机模式");
  }
  // 改完 YAML 会不会自己生效，是运营者最该知道的一条状态：
  // 关着的时候他每改一次配置都得记得去点「重载配置」，而他不会记得。
  bits.push(health.config_watch
    ? `配置自动生效${health.config_watch.reload_count ? `（已自动 reload ${health.config_watch.reload_count} 次）` : ""}`
    : "配置需手动重载");
  $("#server-line").textContent = bits.join(" · ");
  // 小圆点也在这里一并给：它以前在 /admin/* 之后才赋值，
  // 未登录时会一直停在红点——而那并不表示网关挂了。
  const dot = $("#server-dot");
  if (dot) dot.className = "dot " + (health.status === "healthy" ? "on" : "off");
}


async function loadOverview() {
  // /health 不带 token 也能通（Caddy 对它有 Referer/Sec-Fetch-Site 白名单），
  // 而 /admin/* 要令牌。所以「网关活着吗」必须与「数据取到了吗」分开渲染：
  // 合成一个 Promise.all 的话，未登录时两个 admin 调用 401 抛出，下面整段
  // 都不执行，状态行停在初始的「未连接」——运营者以为服务器挂了，
  // 其实只是没填令牌。这就是「访问服务器显示未连接」的根因。
  let health;
  try {
    health = await fetch("/health").then(r => r.json());
  } catch (err) {
    $("#server-dot").className = "dot off";
    $("#server-line").textContent = "网关连不上（" + err.message + "）";
    throw err;
  }
  renderServerLine(health);

  // 数据区：401 时 api() 已经弹过登录框，这里只把错误带到页面底部
  const [stats, providers] = await Promise.all([
    api("/admin/stats?days=7&recent=10"),
    api("/admin/providers"),
  ]);
  state.providers = providers.data;
  $("#server-dot").className = "dot " + (health.status === "healthy" ? "on" : "off");

  const sideVer = $("#side-version");
  const sideUp = $("#side-uptime");
  if (sideVer) sideVer.textContent = `v${health.version}`;
  if (sideUp) sideUp.textContent = `已运行 ${dur(health.uptime_seconds)}`;

  // 被自动隔离的部署：必须显式告知，否则一个渠道悄悄从路由里消失，
  // 会被当成路由 bug 去查（2026-09-29 加，与「健康检查只查目录」同批修的）。
  const quarantined = health.quarantined_deployments || {};
  const qNames = Object.keys(quarantined);
  const qBanner = $("#quarantine-banner");
  if (qBanner) {
    if (qNames.length) {
      qBanner.style.display = "";
      qBanner.innerHTML = `<span class="pill amber">⚠ 已自动隔离 ${qNames.length} 个部署</span>
        <span class="small muted">连续失败达阈值后自动屏蔽，修好后任意一次成功即自动恢复：</span>
        ${qNames.map(n => {
          const q = quarantined[n];
          const left = q.quarantined ? `还剩 ${dur(q.quarantine_seconds_left)}` : "观察中";
          return `<span class="chip" title="连续失败 ${q.consecutive_failures} 次，最后错误 ${esc(zhType(q.last_error_type))}">${esc(n)} · ${esc(left)}</span>`;
        }).join(" ")}`;
    } else {
      qBanner.style.display = "none";
      qBanner.innerHTML = "";
    }
  }

  const pool = stats.pool || {};
  const total60 = Object.values(stats.error_rates || {}).reduce((a, r) => a + r.attempts, 0);
  const fail60 = Object.values(stats.error_rates || {}).reduce((a, r) => a + r.failures, 0);

  const kpi = (label, value, sub, tone) => `<div class="kpi${tone ? " is-" + tone : ""}">
    <div class="k-label">${esc(label)}</div>
    <div class="k-value">${value}</div>
    <div class="k-sub">${esc(sub || "")}</div></div>`;

  const providerCards = providers.data.map(p => {
    const byStatus = {};
    (p.credentials || []).forEach(c => byStatus[c.status] = (byStatus[c.status] || 0) + 1);
    const chips = Object.entries(byStatus)
      .map(([s, n]) => pill(s) + "×" + n).join(" ");
    return `<div class="card prov-card">
      <div class="p-head">
        <span class="dot ${p.available ? "on" : "off"}"></span>
        <span class="p-name">${esc(p.id)}</span>
        <span class="chip">${esc(p.type)}</span>
        <span class="grow"></span>
        ${p.available ? pill("healthy") : pill("disabled")}
      </div>
      <div class="p-url" title="${esc(p.base_url)}">${esc(p.base_url)}</div>
      <div class="row-flex" style="margin-bottom:7px">${chips || '<span class="muted small">暂无凭据</span>'}</div>
      <div class="p-models">${(p.models || []).length ? "挂载模型：" + (p.models || []).map(esc).join(" · ") : "（该供应商没有挂载任何模型）"}</div>
    </div>`;
  }).join("");

  const recent = (stats.recent_requests || []).map(r => `
    <tr class="row-click" data-rid="${esc(r.id)}">
      <td class="muted">${ago(r.started_at)}</td>
      <td>${esc(r.requested_model)}</td>
      <td class="muted">${esc(r.resolved_model || "-")}</td>
      <td>${esc(r.provider || "-")}<span class="muted">/${esc(r.credential_id || "-")}</span>
      <td>${pill(r.status)}</td>
      <td class="muted" title="${esc(r.error_type || "")}">${r.error_type ? esc(zhType(r.error_type)) : ""}</td>
      <td>${r.latency_ms ? Math.round(r.latency_ms) + "ms" : "-"}</td>
    </tr>`).join("");

  $("#view-overview").innerHTML = `
    <div class="view-head">
      <h2>总览</h2>
      <span class="desc">一眼看清供应商可用性、Key 池健康度，以及还需要手动处理的配置问题</span>
    </div>
    <div class="toolbar">
      <button class="primary" id="ov-health">▶ 立即健康检查</button>
      <button id="ov-reload">↻ 重载配置</button>
      <button id="ov-clear">🧹 清空全部冷却</button>
    </div>
    ${attentionPanel(providers.data, health.config?.warnings || [], health.config)}
    <div class="grid kpis">
      ${kpi("可用凭据", `${pool.usable ?? "-"}/${pool.total ?? "-"}`,
        `正常 ${byCount(pool.by_status, "healthy")} · 冷却 ${byCount(pool.by_status, "cooldown")}
        · 异常 ${byCount(pool.by_status, "unhealthy")} · 禁用 ${byCount(pool.by_status, "disabled")}`, "ok")}
      ${kpi("60 分钟失败率", total60 ? pct(fail60 / total60) : "-",
        `上游尝试 ${num(total60)} 次 · 失败 ${num(fail60)} 次`,
        total60 && fail60 / total60 > 0.2 ? "err" : total60 && fail60 ? "warn" : "ok")}
    </div>
    <h4 class="sec">供应商</h4>
    <div class="grid cards">${providerCards}</div>
    <div id="ov-health-result"></div>
    <h4 class="sec">最近请求（点击任意一行看完整明细）</h4>
    <div class="tablewrap"><table>
      <thead><tr><th>时间</th><th>请求模型</th><th>实际模型</th><th>供应商/凭据</th><th>状态</th><th>错误</th><th>延迟</th></tr></thead>
      <tbody class="clickable">${recent || '<tr><td colspan="7" class="muted">暂无记录</td></tr>'}</tbody>
    </table></div>`;

  bindRowClicks($("#view-overview"));
  // 需要处理面板：每行一个可一键跳转的修复动作
  document.querySelectorAll("[data-fix]").forEach(b => b.onclick = async () => {
    const pid = b.dataset.pid || "";
    if (b.dataset.fix === "key") openAddKeyModal(pid);
    else if (b.dataset.fix === "model") openMarketModal(pid);
    else if (b.dataset.fix === "envgap") {
      // 同行的下一块就是命令：就地展开，不用 toast（4 秒后命令就没了）
      const row = b.closest(".att-row");
      const block = row && row.nextElementSibling;
      if (block && block.classList.contains("att-fix")) block.hidden = !block.hidden;
    }
    else if (b.dataset.fix === "warm") {
      const r = await api("/admin/credentials/cooldowns/clear", { method: "POST" });
      toast(`已解除 ${r.cleared} 把凭据的冷却`, "ok");
      refresh();
    }
  });
  $("#ov-health").onclick = async e => {
    e.target.disabled = true;
    try {
      const r = await api("/admin/health/check", { method: "POST", json: {} });
      $("#ov-health-result").innerHTML = `<h4 class="sec">健康检查结果（${esc(r.kind)} · ${dur((r.duration_ms || 0) / 1000)}）</h4>
        <div class="tablewrap"><table><thead><tr><th>凭据</th><th>结果</th><th>延迟</th><th>错误</th></tr></thead><tbody>
        ${r.results.map(x => `<tr><td>${esc(x.provider_id)}/${esc(x.credential_id || "-")}</td>
          <td>${x.ok ? pill("healthy") : pill("unhealthy")}</td>
          <td>${Math.round(x.latency_ms)}ms</td>
          <td class="muted ell" title="${esc(x.detail || "")}">${esc(x.error_type ? zhErr(x.error_type, x.detail) : "")}</td></tr>`).join("")}
        </tbody></table></div>`;
      toast(`健康检查完成：${r.ok} 正常 / ${r.failed} 失败`, r.failed ? "err" : "ok");
    } finally { e.target.disabled = false; refresh(); }
  };
  $("#ov-reload").onclick = async () => {
    const r = await api("/admin/config/reload", { method: "POST" });
    toast(`配置已热重载：${r.models} 模型 / ${r.providers.length} 供应商`, "ok"); refresh();
  };
  $("#ov-clear").onclick = async () => {
    const r = await api("/admin/credentials/cooldowns/clear", { method: "POST" });
    toast(`已解除 ${r.cleared} 把凭据的冷却`, "ok"); refresh();
  };
}
function byCount(map, k) { return (map && map[k]) ?? 0; }
function fmtK(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : num(n); }
function fmtTokens(n) { n = Number(n) || 0;
  return n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : num(n); }
const CNY_RATE = 7.2;   // USD→CNY 展示换算，牌价以 USD 存储，这里只是给人看的近似
function fmtCost(v) {
  v = Number(v) || 0;
  if (v <= 0) return "$0";
  // 大额给两位小数，极小额保留有效位，避免一片 $0.0000
  const usd = v >= 0.01 ? "$" + v.toFixed(2) : "$" + v.toPrecision(2);
  return `${usd} ≈¥${(v * CNY_RATE >= 0.01 ? (v * CNY_RATE).toFixed(2) : (v * CNY_RATE).toPrecision(2))}`;
}

/* ---- 中文翻译层：错误类型 + 上游错误原文 → 中文 ----
 * error_type 机器码保持不变（日志/API 稳定），只翻译给人看的展示层。 */
const ERR_TYPE_ZH = {
  rate_limit_error: "限流（429）",
  no_available_credential: "暂无可用 Key",
  no_available_deployment: "无可用部署",
  timeout: "超时",
  connection_error: "连接失败",
  upstream_error: "上游错误",
  unknown_error: "未知错误",
  invalid_request_error: "请求参数错误",
  unsupported_parameter: "不支持的参数",
  authentication_error: "Key 认证失败（401）",
  permission_denied: "无权限（403）",
  model_not_found: "模型不存在",
  context_length_exceeded: "上下文超长",
  content_filter: "内容被安全策略拦截",
  overloaded: "上游过载（529）",
  conflict_error: "请求冲突",
  client_disconnected: "客户端断开",
  no_stream_meta: "流式元数据缺失",
  provider_unavailable: "供应商不可用",
  config_error: "配置错误",
  invalid_alias: "别名无效",
  internal_error: "网关内部错误",
  zkai_error: "网关错误",
};
function zhType(type) {
  if (!type) return "-";
  return ERR_TYPE_ZH[type] ? `${ERR_TYPE_ZH[type]}` : type;
}
/** 把上游/网关的英文错误详情翻译成中文（保留原文在 tooltip）。 */
const ERR_DETAIL_PATTERNS = [
  [/inference exceeds tpm\/?rpm limit/i, "超出该账号每分钟限流（tpm/rpm），稍候自动恢复"],
  [/token plan entitlement exhausted/i, "套餐额度已耗尽（5 小时/周窗口重置）"],
  [/upstream returned HTTP 429/i, "上游返回 429 限流"],
  [/no usable credential for provider ['"]?([\w-]+)['"]?/i, "供应商 $1 当前没有可用的 Key"],
  [/pool recovers in ~(\d+)s/i, "约 $1 秒后可重试"],
  [/timed out after ([\d.]+)s/i, "超过 $1 秒无响应（超时）"],
  [/timeout while opening stream/i, "建立流式连接超时"],
  [/stream connection failed/i, "流式连接失败"],
  [/connection failed/i, "连接失败"],
  [/stream dropped/i, "流式连接中断"],
  [/environment variable (\S+) is not set/i, "环境变量 $1 未设置"],
  [/validationerror/i, "网关解析上游返回格式失败"],
  [/all (\d+) attempt\(s\) failed for model ['"]([^'"]+)['"]:?\s*(.*)/i, "模型 $2 的 $1 次尝试全部失败：$3"],
  [/disabled by operator/i, "已被手动禁用"],
  [/disabled in config/i, "配置中禁用"],
  [/invalid credential \(401\)/i, "Key 无效（401 认证失败）"],
  [/permission denied \(403\)/i, "无权限（403）"],
  [/returned a non-JSON body/i, "上游返回了非 JSON 内容"],
];
function zhDetail(detail) {
  if (!detail) return "-";
  let out = String(detail);
  for (const [re, zh] of ERR_DETAIL_PATTERNS) {
    if (re.test(out)) { out = out.replace(re, zh); break; }
  }
  return out;
}
/** 组合展示：类型中文 + 详情中文；英文原文留给 title 悬停查看。 */
function zhErr(type, detail) {
  const t = zhType(type);
  if (!detail) return t;
  const d = zhDetail(detail);
  return d === t ? t : (t === "-" ? d : `${t}：${d}`);
}
function bindRowClicks(root) {
  root.querySelectorAll("tr.row-click").forEach(tr =>
    tr.onclick = () => openDrawer(tr.dataset.rid));
}

/* ================= 凭据池 ================= */
async function loadPool() {
  const resp = await api("/admin/credentials");
  state.poolAt = Date.now() / 1000;
  const aff = resp.affinity || {};
  // 每把 Key 被多少通会话钉住
  const pinned = {};
  Object.values(aff.bindings || {}).forEach(list =>
    list.forEach(b => pinned[b.credential_id] = (pinned[b.credential_id] || 0) + 1));
  const rows = resp.data.map(c => {
    const until = (c.status === "cooldown" && c.cooldown_until)
      ? ` data-cd-until="${c.cooldown_until}"` : "";
    const cd = c.status === "cooldown" ? `<span${until}></span>` : "-";
    const pin = pinned[c.id] ? ` <span class="pill blue" title="钉住的会话数">📌${pinned[c.id]}</span>` : "";
    const quota = (c.rate_limits || []).map(q => {
      const win = q.window_seconds >= 86400 ? `${q.window_seconds / 86400}天`
        : q.window_seconds >= 3600 ? `${q.window_seconds / 3600}h`
        : q.window_seconds >= 60 ? `${q.window_seconds / 60}m` : `${q.window_seconds}s`;
      const dims = [];
      let ratio = 0;
      if (q.max_requests != null) {
        dims.push(`${q.used_requests}/${q.max_requests}次`);
        ratio = Math.max(ratio, q.max_requests ? q.used_requests / q.max_requests : 0);
      }
      if (q.max_tokens != null) {
        dims.push(`${fmtTokens(q.used_tokens)}/${fmtTokens(q.max_tokens)}tok`);
        ratio = Math.max(ratio, q.max_tokens ? q.used_tokens / q.max_tokens : 0);
      }
      const cls = ratio >= 1 ? "red" : ratio >= 0.8 ? "amber" : "green";
      const scopeZh = { credential: "Key", account: "账号", provider: "供应商" }[q.scope] || q.scope;
      return `<span class="pill ${cls}" title="主动配额（${scopeZh} ${esc(q.bucket)}）：${dims.join(" · ")}">${win} ${dims.join(" · ")}</span>`;
    }).join(" ");
    const actions = [];
    if (c.status !== "disabled") actions.push(`<button class="mini" data-act="disable" data-id="${esc(c.id)}">禁用</button>`);
    else actions.push(`<button class="mini" data-act="enable" data-id="${esc(c.id)}">启用</button>`);
    if (c.status === "cooldown" || c.status === "unhealthy")
      actions.push(`<button class="mini" data-act="enable" data-id="${esc(c.id)}">解除冷却</button>`);
    return `<tr><td class="acts"><span class="cell-id"><b>${esc(c.id)}</b>${pin}</span></td>
      <td class="cell-sub">${c.tags.map(esc).join(" ")}</td>
      <td>${esc(c.provider_id)}</td>
      <td>${pill(c.status)}</td>
      <td>${c.priority}</td>
      <td>${quota || '<span class="muted">-</span>'}</td>
      <td>${c.success_count}</td>
      <td>${c.failure_count}</td>
      <td>${c.rate_limit_count}</td>
      <td>${cd}</td>
      <td class="muted ell" title="${esc(c.last_error_detail || "")}${esc(c.disabled_reason || "")}">
        ${esc(c.last_error_type ? zhErr(c.last_error_type, c.last_error_detail) : (c.disabled_reason ? zhDetail(c.disabled_reason) : "-"))}</td>
      <td class="muted small" title="${esc(c.secret_ref || "")}">${esc(c.secret_masked || c.secret_source)}</td>
      <td class="acts-end">${actions.length ? `<div class="row-flex">${actions.join("")}</div>` : ""}</td>
    </tr>`;
  }).join("");
  const s = resp.stats;
  const totalPins = Object.values(pinned).reduce((a, b) => a + b, 0);
  $("#view-pool").innerHTML = `
    <div class="view-head">
      <h2>凭据池</h2>
      <span class="desc">每把 Key 的状态、优先级、配额用量与冷却倒计时；可逐把启用 / 禁用 / 解除冷却</span>
    </div>
    <div class="toolbar">
      <span class="small muted">轮转策略：${esc(s.rotation)}</span>
      <span class="pill ${aff.enabled ? "green" : "gray"}">会话亲和 ${aff.enabled ? "开" : "关"}${aff.enabled ? ` · ${totalPins} 通会话 · TTL ${dur(aff.ttl_seconds || 0)}` : ""}</span>
      <span class="pill blue">可用 ${s.usable}/${s.total}</span>
      <span class="pill green">成功 ${s.success}</span>
      <span class="pill red">失败 ${s.failure}</span>
      <span class="pill amber">限流 ${s.rate_limits}</span>
      <span class="grow"></span>
      <button id="pool-add-key">➕ 加 Key</button>
      <button id="pool-providers">🏢 供应商</button>
      <button id="pool-limits">⚙ 限额</button>
      <button id="pool-clear-all">🧹 全部清冷却</button>
    </div>
    <div class="tablewrap"><table>
      <thead><tr><th class="acts">操作</th><th>凭据</th><th>供应商</th><th>状态</th><th>优先级</th><th>配额用量</th><th>成功</th><th>失败</th>
      <th>限流</th><th>冷却剩余</th><th>最后错误</th><th>密钥</th><th class="acts-end">操作</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="13" class="muted">无凭据</td></tr>'}</tbody>
    </table></div>
    <div class="note-block">📌 = 当前钉在这把 Key 上的会话数。同一通对话会优先复用上一步成功的 Key
    （命中供应商侧前缀缓存，历史不重算）；这把 Key 冷却 / 故障时自动换下一把，并在成功瞬间把锚点带过去。<br>
    冷却规则：分钟限流走指数退避（60 → 900 秒）；额度耗尽（token plan exhausted）平铺休 30 分钟。</div>`;
  tickCountdowns();
  $("#pool-add-key").onclick = openAddKeyModal;
  $("#pool-providers").onclick = openProvidersModal;
  $("#pool-limits").onclick = openLimitsModal;
  $("#pool-clear-all").onclick = async () => {
    const r = await api("/admin/credentials/cooldowns/clear", { method: "POST" });
    toast(`已解除 ${r.cleared} 把`, "ok"); loadPool();
  };
  document.querySelectorAll("#view-pool button[data-act]").forEach(btn =>
    btn.onclick = async () => {
      await api(`/admin/credentials/${encodeURIComponent(btn.dataset.id)}/${btn.dataset.act}`, { method: "POST" });
      toast(`${btn.dataset.id} 已${btn.dataset.act === "enable" ? "启用" : "禁用"}`, "ok");
      loadPool();
    });
}

/* ================= 限额编辑器（供应商主动配额） ================= */
const LIMIT_WINDOWS = [
  [60, "1 分钟"], [300, "5 分钟"], [3600, "1 小时"],
  [18000, "5 小时"], [86400, "1 天"], [604800, "7 天"],
];
const LIMIT_SCOPES = [
  ["credential", "每把 Key"], ["account", "按账号（account-* 标签共享）"], ["provider", "全供应商共享"],
];
function renderLimitsModal() {
  const modal = $("#limits-modal");
  const collect = (card) => Array.from(card.querySelectorAll("[data-rule]")).map(row => {
    const f = name => row.querySelector(`[data-f="${name}"]`);
    const winSel = f("window");
    const win = winSel.value === "custom" ? Number(f("window_custom").value) : Number(winSel.value);
    return {
      scope: f("scope").value,
      window_seconds: win,
      max_requests: Number(f("max_requests").value) || 0,
      max_tokens: Number(f("max_tokens").value) || 0,
    };
  });
  const cards = (state.limitsProviders || []).map(p => {
    const rules = state.limitsEdit[p.id] || [];
    const rows = rules.map((r, i) => {
      const known = LIMIT_WINDOWS.some(w => w[0] === Number(r.window_seconds));
      const winSel = LIMIT_WINDOWS.map(([sec, label]) =>
        `<option value="${sec}" ${Number(r.window_seconds) === sec ? "selected" : ""}>${label}</option>`).join("");
      const scopeSel = LIMIT_SCOPES.map(([v, label]) =>
        `<option value="${v}" ${(r.scope || "credential") === v ? "selected" : ""}>${label}</option>`).join("");
      return `<div class="dep-card" data-rule="${i}">
        <div class="row-flex">
          <label class="small muted" style="display:flex;gap:4px;align-items:center">作用域<select data-f="scope">${scopeSel}</select></label>
          <label class="small muted" style="display:flex;gap:4px;align-items:center">窗口<select data-f="window">${winSel}<option value="custom" ${known ? "" : "selected"}>自定义…</option></select></label>
          <input type="number" data-f="window_custom" style="width:92px;display:${known ? "none" : "inline-block"}" value="${esc(r.window_seconds)}" placeholder="秒">
          <label class="small muted" style="display:flex;gap:4px;align-items:center">次数上限<input type="number" data-f="max_requests" style="width:110px" value="${r.max_requests ?? 0}" min="0"></label>
          <label class="small muted" style="display:flex;gap:4px;align-items:center">Token 上限<input type="number" data-f="max_tokens" style="width:130px" value="${r.max_tokens ?? 0}" min="0"></label>
          <span class="grow"></span>
          <button class="mini" data-rule-del="${i}">✕ 删除</button>
        </div>
        <div class="small muted">0 = 该项不限；两项可同设，按更紧的一项拦。Token 按请求完成后的真实用量计。</div>
      </div>`;
    }).join("");
    const src = p.rate_limits_source === "console"
      ? '<span class="pill blue" title="控制台改动优先于 YAML">控制台</span>'
      : '<span class="pill gray">YAML</span>';
    return `<div class="dep-card" data-pid="${esc(p.id)}">
      <div class="row-flex">
        <b>${esc(p.id)}</b>${src}<span class="grow"></span>
        <button class="mini" data-rule-add="${esc(p.id)}">➕ 加规则</button>
        ${p.rate_limits_source === "console" ? `<button class="mini" data-limit-reset="${esc(p.id)}">↺ 恢复 YAML</button>` : ""}
        <button class="primary mini" data-limit-save="${esc(p.id)}">💾 保存生效</button>
      </div>
      ${rows || '<div class="small muted" style="margin-top:6px">未设限额（不限流）</div>'}
    </div>`;
  }).join("");
  modal.innerHTML = `<div class="card wide">
    <h3>主动配额限额</h3>
    <div class="modal-sub">到线的 Key 会被直接跳过，不再发出注定 429 的请求。改动保存后立即生效并写入数据库（优先于 YAML，重启不丢）；想改回 YAML 里的值，点「↺ 恢复 YAML」。</div>
    ${cards}
    <div class="modal-actions"><span class="grow"></span><button id="lm-close">关闭</button></div>
  </div>`;
  openModal("#limits-modal");
  $("#lm-close").onclick = () => closeModal("#limits-modal");
  modal.querySelectorAll("[data-rule-del]").forEach(b => b.onclick = () => {
    const card = b.closest("[data-pid]");
    const pid = card.dataset.pid;
    state.limitsEdit[pid] = collect(card);
    state.limitsEdit[pid].splice(Number(b.dataset.ruleDel), 1);
    renderLimitsModal();
  });
  modal.querySelectorAll("[data-rule-add]").forEach(b => b.onclick = () => {
    const card = b.closest("[data-pid]");
    const pid = b.dataset.ruleAdd;
    state.limitsEdit[pid] = collect(card);
    (state.limitsEdit[pid] = state.limitsEdit[pid] || []).push(
      { scope: "credential", window_seconds: 60, max_requests: 40, max_tokens: 0 });
    renderLimitsModal();
  });
  modal.querySelectorAll("[data-f='window']").forEach(sel => sel.onchange = () => {
    const custom = sel.value === "custom";
    const input = sel.closest(".dep-card").querySelector("[data-f='window_custom']");
    input.style.display = custom ? "inline-block" : "none";
    if (custom) input.focus();
  });
  modal.querySelectorAll("[data-limit-save]").forEach(btn => btn.onclick = async () => {
    const pid = btn.dataset.limitSave;
    const card = btn.closest("[data-pid]");
    const rules = collect(card).map(r => {
      const out = { scope: r.scope, window_seconds: r.window_seconds };
      if (r.max_requests > 0) out.max_requests = r.max_requests;
      if (r.max_tokens > 0) out.max_tokens = r.max_tokens;
      return out;
    });
    const bad = rules.find(r => !r.max_requests && !r.max_tokens);
    if (bad) { toast("每条规则至少要设一个上限（次数或 Token）", "err"); return; }
    const r = await api(`/admin/providers/${encodeURIComponent(pid)}/limits`,
      { method: "PUT", json: { rules } });
    if (r.error) { toast(r.error.message || "保存失败", "err"); return; }
    toast(`${pid} 限额已生效${syncedNote(r)}`, "ok");
    openLimitsModal();
  });
  modal.querySelectorAll("[data-limit-reset]").forEach(btn => btn.onclick = async () => {
    const pid = btn.dataset.limitReset;
    if (!confirm(`恢复 ${pid} 的限额为 providers.yaml 中的值？（控制台改动将被丢弃）`)) return;
    const r = await api(`/admin/providers/${encodeURIComponent(pid)}/limits`, { method: "DELETE" });
    if (r.error) { toast(r.error.message || "恢复失败", "err"); return; }
    toast(`${pid} 已恢复 YAML 限额${syncedNote(r)}`, "ok");
    openLimitsModal();
  });
}
/* ---------- 凭据池：供应商管理 ---------- */
function openProvidersModal() {
  const modal = $("#providers-modal");
  const renderList = () => {
    const providers = state.providers || [];
    const rows = providers.map(p => {
      const limits = (p.rate_limits || []).length ? "pill green" : "pill gray";
      const limitCount = (p.rate_limits || []).length;
      return `<tr><td class="acts"><div class="row-flex"><button class="mini" data-edit-provider="${esc(p.id)}" title="编辑供应商 ${esc(p.id)}">编辑</button><button class="mini danger" data-del-provider="${esc(p.id)}" title="删除供应商 ${esc(p.id)}">删除</button></div></td>
        <td class="name"><b>${esc(p.id)}</b><span class="sub">${esc(p.type)}</span></td>
        <td class="small muted ell" title="${esc(p.base_url)}" style="max-width:220px">${esc(p.base_url)}</td>
        <td>${p.enabled ? pill("healthy") : pill("disabled")}</td>
        <td class="small">${p.timeout}s</td>
        <td>${limitCount ? `<span class="pill green" title="已设限额">${limitCount} 条限额</span>` : '<span class="muted">-</span>'}</td>

      </tr>`;
    }).join("");
    modal.innerHTML = `<div class="card wide">
      <h3>🏢 供应商管理</h3>
      <div class="modal-sub">供应商 = 上游协议 + 接入地址。Key 在「➕ 加 Key」里单独添加；在这里改动不会碰到已配好的 Key。</div>
      <div class="row-flex" style="margin-bottom:10px">
        <button class="primary" id="pv-new">➕ 新建供应商</button>
        <span class="grow"></span>
        <button id="pv-close">关闭</button>
      </div>
      <div class="tablewrap" style="max-height:55vh"><table>
        <thead><tr><th class="acts">操作</th><th>供应商</th><th>接入地址</th><th>状态</th><th>超时</th><th>限额</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7" class="muted">无供应商</td></tr>'}</tbody>
      </table></div>
    </div>`;
    openModal("#providers-modal");
    $("#pv-close").onclick = () => closeModal("#providers-modal");
    $("#pv-new").onclick = () => openProviderForm(null);
    document.querySelectorAll("[data-edit-provider]").forEach(b =>
      b.onclick = () => openProviderForm(b.dataset.editProvider));
    document.querySelectorAll("[data-del-provider]").forEach(b =>
      b.onclick = async () => {
        const id = b.dataset.delProvider;
        if (!confirm(`确定删除供应商 ${id}？（会同步删掉它的 Key；有模型引用时会被拒绝）`)) return;
        const r = await api(`/admin/providers/${encodeURIComponent(id)}`, { method: "DELETE" });
        if (r.error) { toast(r.error.message || "删除失败", "err"); return; }
        toast(`供应商 ${id} 已删除${syncedNote(r)}`, "ok");
        state.providers = (await api("/admin/providers")).data || [];
        renderList();
      });
  };
  renderList();
}
function openProviderForm(pid) {
  const src = pid ? (state.providers || []).find(p => p.id === pid) : null;
  const p = src || {id: "", type: "openai_compatible", base_url: "", enabled: true, timeout: 60};
  const types = ["openai_compatible", "openai", "anthropic", "gemini", "openrouter", "ollama"];
  const modal = $("#providers-modal");
  modal.innerHTML = `<div class="card">
    <h3>${pid ? "编辑供应商" : "新建供应商"}</h3>
    <div class="modal-sub">${pid ? "保存后立即生效，并同步写回 providers.yaml。" : "保存后立即生效、写回 providers.yaml，紧接着会弹出加 Key 的窗口。"}</div>
    <div class="formgrid">
      <label>供应商 ID（唯一，自己起）<input type="text" id="pf-id" value="${esc(p.id)}" ${pid ? "disabled" : ""} placeholder="如 my-openai"></label>
      <label>协议类型
        <select id="pf-type">${types.map(t => `<option ${t === p.type ? "selected" : ""}>${t}</option>`).join("")}</select>
        <div class="small muted">绝大多数用 openai_compatible；anthropic 才用 anthropic</div></label>
      <label style="grid-column:1/-1">接入地址（base_url，必须带 /v1 结尾）
        <input type="text" id="pf-url" value="${esc(p.base_url)}" placeholder="https://api.example.com/v1"></label>
      <label>超时（秒）<input type="number" id="pf-timeout" value="${p.timeout ?? 60}"></label>
      <label>启用 <select id="pf-enabled">
        <option value="true" ${p.enabled !== false ? "selected" : ""}>启用</option>
        <option value="false" ${p.enabled === false ? "selected" : ""}>禁用</option></select></label>
    </div>
    <div class="small muted" style="margin-top:8px">Key 不在这里加——保存后去「凭据池 → ➕ 加 Key」。</div>
    <div class="modal-actions">
      <button class="primary" id="pf-save">💾 保存</button>
      <button id="pf-cancel">取消</button>
    </div>
  </div>`;
  $("#pf-cancel").onclick = () => openProvidersModal();
  $("#pf-save").onclick = async () => {
    const payload = {
      id: $("#pf-id").value.trim(),
      type: $("#pf-type").value,
      base_url: $("#pf-url").value.trim(),
      timeout: Number($("#pf-timeout").value || 60),
      enabled: $("#pf-enabled").value === "true",
    };
    if (!payload.id) { toast("供应商 ID 不能为空", "err"); return; }
    if (!payload.base_url) { toast("接入地址不能为空", "err"); return; }
    const r = await api("/admin/providers", { method: "POST", json: payload });
    if (r.error) { toast(r.error.message || "保存失败", "err"); return; }
    toast(`供应商 ${payload.id} 已保存${syncedNote(r)}`, "ok");
    state.providers = (await api("/admin/providers")).data || [];
    // 新建供应商后紧接着加 Key——否则回头还得先想起「供应商不能直接用」。
    // 只调 openAddKeyModal：它内部的 openModal 会先关掉供应商列表，
    // 不留两层遮罩。2026-09-30 之前此处会先把列表重绘一次再开加 Key，
    // 于是加 Key 窗口被列表压在下面，看起来「点了没反应」。
    if (r.created) openAddKeyModal(payload.id);
    else openProvidersModal();
  };
}
/* ---------- 凭据池：加 Key ---------- */
async function openAddKeyModal(presetPid) {
  const resp = await api("/admin/providers");
  const providers = (resp.data || []).filter(p => p.requires_credential);
  if (!providers.length) { toast("没有可加 Key 的供应商（都要凭据）", "err"); return; }
  const modal = $("#addkey-modal");
  modal.innerHTML = `<div class="card wide">
    <h3>➕ 给供应商加 Key</h3>
    <div class="modal-sub">Key 会写进 <code>.env</code>（已被 gitignore），<code>providers.yaml</code> 里只记「变量名」——
    换机器、重建环境时不用重新配。选好供应商后，ID 和变量名会自动帮你起好。</div>
    <div class="formgrid">
      <label>供应商
        <select id="ak-provider">${providers.map(p => `<option value="${esc(p.id)}" ${p.id === presetPid ? "selected" : ""}>${esc(p.id)}</option>`).join("")}</select>
      </label>
      <label>Key 的 ID（唯一短名，不是 Key 本身）
        <input type="text" id="ak-id" placeholder="如 sensenova-10"></label>
      <label>环境变量名（.env 里的那个）
        <input type="text" id="ak-env" placeholder="如 SENSENOVA_API_KEY_01"></label>
      <label>Key 值（写进 .env，不会存到 YAML）
        <input type="password" id="ak-value" placeholder="粘贴 Key，如 sk-…" autocomplete="off"></label>
      <label>优先级（越小越优先，默认 100）
        <input type="number" id="ak-priority" value="100"></label>
    </div>
    <div class="modal-actions">
      <button class="primary" id="ak-save">💾 保存</button>
      <button id="ak-cancel">取消</button>
    </div>
  </div>`;
  openModal("#addkey-modal");
  // 自动命名：切供应商时按已有条目递增，省掉两次「起名字」的思考
  const suggestNames = () => {
    const p = providers.find(x => x.id === $("#ak-provider").value);
    if (!p) return;
    const n = (p.credentials || []).length + 1;
    const pad = String(n).padStart(2, "0");
    $("#ak-id").value = `${p.id.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-${pad}`;
    $("#ak-env").value = `${p.id.toUpperCase().replace(/[^A-Z0-9]+/g, "_")}_API_KEY${n > 1 ? "_" + pad : ""}`;
  };
  $("#ak-provider").onchange = suggestNames;
  suggestNames();
  $("#ak-cancel").onclick = () => closeModal("#addkey-modal");
  $("#ak-save").onclick = async () => {
    const payload = {
      id: $("#ak-id").value.trim(),
      env_var: $("#ak-env").value.trim() || null,
      priority: Number($("#ak-priority").value || 100),
    };
    const value = $("#ak-value").value.trim();
    if (!payload.id) { toast("Key 的 ID 不能为空", "err"); return; }
    if (!payload.env_var) { toast("环境变量名不能为空（Key 值写 .env，这里只记名字）", "err"); return; }
    if (value) { payload.value = value; payload.write_env = true; }
    try {
      const pid = $("#ak-provider").value;
      const r = await api(`/admin/providers/${encodeURIComponent(pid)}/credentials`,
        { method: "POST", json: payload });
      if (r.error) { toast(r.error.message || "保存失败", "err"); return; }
      toast(`已加 ${payload.id} 到 ${pid}${syncedNote(r)}${r.env_synced ? "，Key 已写入 " + r.env_synced : ""}`, "ok");
      closeModal("#addkey-modal");
      loadPool();
    } catch (err) { toast(err.message, "err"); }
  };
}

async function openLimitsModal() {
  const resp = await api("/admin/providers");
  state.limitsProviders = resp.data || [];
  state.limitsEdit = {};
  state.limitsProviders.forEach(p => {
    state.limitsEdit[p.id] = (p.rate_limits || []).map(r => ({ ...r }));
  });
  renderLimitsModal();
}

/* ================= 请求记录 ================= */
function reqFilterParams() {
  const p = new URLSearchParams();
  // First render happens before the filter bar exists - guard against null.
  const g = id => { const el = $(id); return el ? (el.value || "").trim() : ""; };
  if (g("#f-status")) p.set("status", g("#f-status"));
  if (g("#f-provider")) p.set("provider", g("#f-provider"));
  if (g("#f-alias")) p.set("alias", g("#f-alias"));
  if (g("#f-model")) p.set("model", g("#f-model"));
  if (g("#f-error")) p.set("error_type", g("#f-error"));
  if (g("#f-q")) p.set("q", g("#f-q"));
  p.set("limit", state.req.size);
  p.set("offset", state.req.offset);
  return p;
}
async function loadRequests(keepFilters) {
  const prev = keepFilters ? collectFilters() : null;
  const p = reqFilterParams();
  const resp = await api("/admin/requests?" + p.toString());
  state.req.total = resp.total;
  if (prev) restoreFilters(prev);
  const opts = (arr, sel) => ["<option value=''>全部</option>"]
    .concat(arr.map(x => `<option ${x === sel ? "selected" : ""}>${esc(x)}</option>`)).join("");
  const rows = resp.data.map(r => `<tr class="row-click" data-rid="${esc(r.id)}">
      <td class="muted">${ago(r.started_at)}<div class="small muted">${fmtTime(r.started_at)}</div></td>
      <td>${esc(r.requested_model)}</td>
      <td class="muted">${esc(r.resolved_model || "-")}</td>
      <td>${esc(r.alias || "-")}</td>
      <td>${esc(r.provider || "-")}<span class="muted">/${esc(r.credential_id || "-")}</span></td>
      <td>${r.stream ? "流式" : "普通"}</td>
      <td>${pill(r.status)}</td>
      <td>${r.http_status ?? "-"}</td>
      <td class="muted ell" style="max-width:170px" title="${esc(r.error_type || "")}">${esc(r.error_type ? zhType(r.error_type) : "-")}</td>
      <td>${r.attempt_count}${r.fallback_used ? " ↪" : ""}</td>
      <td>${r.latency_ms ? Math.round(r.latency_ms) + "ms" : "-"}</td>
      <td>${fmtK(r.total_tokens)}</td>
      <td>${r.total_tokens ? fmtCost(r.cost_usd) : "-"}</td>
    </tr>`).join("");
  const page = Math.floor(state.req.offset / state.req.size) + 1;
  const pages = Math.max(1, Math.ceil(resp.total / state.req.size));
  $("#view-requests").innerHTML = `
    <div class="view-head">
      <h2>请求记录</h2>
      <span class="desc">每一次请求的路由落点、每次尝试的上游错误原文；点击任意一行看完整明细</span>
    </div>
    <div class="filters">
      <select id="f-status">${opts(["success","error","cancelled","pending"], keepFilters ? prev.status : "")}</select>
      <select id="f-provider">${opts(state.providers.map(x => x.id), keepFilters ? prev.provider : "")}</select>
      <input id="f-alias" size="10" placeholder="别名" value="${keepFilters ? esc(prev.alias) : ""}">
      <input id="f-model" size="14" placeholder="模型（模糊）" value="${keepFilters ? esc(prev.model) : ""}">
      <input id="f-error" size="16" placeholder="错误类型" value="${keepFilters ? esc(prev.error) : ""}">
      <input id="f-q" size="22" placeholder="关键词：id / 模型 / 凭据 / 错误" value="${keepFilters ? esc(prev.q) : ""}">
      <button class="primary" id="r-go">查询</button>
      <button id="r-reset">重置</button>
    </div>
    <div class="tablewrap"><table>
      <thead><tr><th>时间</th><th>请求模型</th><th>实际模型</th><th>别名</th><th>供应商/凭据</th>
      <th>模式</th><th>状态</th><th>HTTP</th><th>错误</th><th>尝试</th><th>延迟</th><th>Tokens</th><th>成本</th></tr></thead>
      <tbody class="clickable">${rows || '<tr><td colspan="13" class="muted">无匹配记录</td></tr>'}</tbody>
    </table></div>
    <div class="toolbar" style="margin-top:10px">
      <button id="r-prev" ${state.req.offset <= 0 ? "disabled" : ""}>← 上一页</button>
      <span class="muted small">第 ${page}/${pages} 页 · 共 ${num(resp.total)} 条</span>
      <button id="r-next" ${state.req.offset + state.req.size >= resp.total ? "disabled" : ""}>下一页 →</button>
      <span class="grow"></span>
      <span class="small muted">↪ 表示发生了故障转移</span>
    </div>`;
  $("#r-go").onclick = () => { state.req.offset = 0; loadRequests(true); };
  $("#r-reset").onclick = () => {
    ["#f-status","#f-provider","#f-alias","#f-model","#f-error","#f-q"].forEach(id => $(id).value = "");
    state.req.offset = 0; loadRequests();
  };
  $("#r-prev").onclick = () => { state.req.offset = Math.max(0, state.req.offset - state.req.size); loadRequests(true); };
  $("#r-next").onclick = () => { state.req.offset += state.req.size; loadRequests(true); };
  bindRowClicks($("#view-requests"));
}
function collectFilters() {
  const g = id => ($(id) ? ($(id).value || "").trim() : "");
  return { status: g("#f-status"), provider: g("#f-provider"), alias: g("#f-alias"),
           model: g("#f-model"), error: g("#f-error"), q: g("#f-q") };
}
function restoreFilters(prev) {
  const s = (id, v) => { if ($(id)) $(id).value = v; };
  s("#f-status", prev.status); s("#f-provider", prev.provider); s("#f-alias", prev.alias);
  s("#f-model", prev.model); s("#f-error", prev.error); s("#f-q", prev.q);
}

/* ---------- 详情抽屉 ---------- */
async function openDrawer(requestId) {
  if (!requestId) return;
  try {
    const d = await api("/admin/requests/" + encodeURIComponent(requestId));
    const r = d.request || {};
    const attempts = (d.attempts || []).map(a => `<tr>
      <td>${rankBadge(a.attempt_number)}</td>
      <td>${esc(a.provider)}<div class="small muted">${esc(a.model || "")}</div></td>
      <td>${esc(a.credential_id || "-")}</td>
      <td>${pill(a.status)}</td>
      <td>${a.http_status ?? "-"}</td>
      <td class="muted" title="${esc(a.error_type || "")}">${esc(a.error_type ? zhType(a.error_type) : "")}</td>
      <td class="ell small" style="max-width:220px" title="${esc(a.detail || "")}">${esc(a.detail ? zhDetail(a.detail) : "")}</td>
      <td>${a.latency_ms ? Math.round(a.latency_ms) + "ms" : "-"}</td>
      <td>${fmtK((a.input_tokens || 0) + (a.output_tokens || 0))}</td>
    </tr>`).join("");
    $("#drawer").innerHTML = `
      <div class="dr-head">
        <h2 class="grow">请求详情</h2><button class="mini" id="dr-close">✕ 关闭</button></div>
      <dl class="detail">
        <dt>请求 ID</dt><dd>${esc(r.id || requestId)}</dd>
        <dt>请求模型</dt><dd>${esc(r.requested_model || "-")}${r.alias ? `（别名 ${esc(r.alias)}）` : ""}</dd>
        <dt>实际模型</dt><dd>${esc(r.resolved_model || "-")}</dd>
        <dt>落点</dt><dd>${esc(r.provider || "-")} / ${esc(r.deployment_id || "-")} / ${esc(r.credential_id || "-")}</dd>
        <dt>状态</dt><dd>${pill(r.status)} ${r.http_status ?? ""} ${r.error_type ? "· " + esc(zhType(r.error_type)) : ""}</dd>
        <dt>流式 / 尝试</dt><dd>${r.stream ? "流式" : "普通"} · ${r.attempt_count ?? 0} 次${r.fallback_used ? " · 有故障转移" : ""}</dd>
        <dt>延迟 / Tokens</dt><dd>${r.latency_ms ? Math.round(r.latency_ms) + "ms" : "-"} ·
          入 ${num(r.input_tokens)} / 出 ${num(r.output_tokens)} / 共 ${num(r.total_tokens)}</dd>
        <dt>等价成本</dt><dd>${fmtCost(r.cost_usd)}<span class="muted small"> · 按牌价折算，免费渠道即省下的费用</span></dd>
        <dt>客户端</dt><dd class="small">${esc(r.client_ip || "-")} · ${esc((r.user_agent || "").slice(0, 70))}</dd>
        <dt>时间</dt><dd>${fmtTime(r.started_at)} → ${fmtTime(r.finished_at)}</dd>
      </dl>
      ${r.routing_reason ? `<details class="raw"><summary>路由决策</summary><pre>${esc(r.routing_reason)}</pre></details>` : ""}
      <h4 class="sec">尝试明细（共 ${(d.attempts || []).length} 次）</h4>
      <div class="tablewrap"><table>
        <thead><tr><th>#</th><th>供应商</th><th>凭据</th><th>结果</th><th>HTTP</th><th>错误</th><th>详情</th><th>延迟</th><th>Tokens</th></tr></thead>
        <tbody>${attempts || '<tr><td colspan="9" class="muted">无尝试记录（可能是修复前的流式请求）</td></tr>'}</tbody>
      </table></div>`;
    $("#drawer").classList.add("open");
    $("#dr-close").onclick = () => $("#drawer").classList.remove("open");
  } catch (err) { toast(err.message, "err"); }
}

/* ================= 用量统计 ================= */
async function loadUsage() {
  const stats = await api(`/admin/stats?days=${state.usageDays}&recent=0`);
  const u = stats.usage || {};
  const kpi = (label, value, sub, tone) => `<div class="kpi${tone ? " is-" + tone : ""}">
    <div class="k-label">${esc(label)}</div>
    <div class="k-value">${value}</div>
    <div class="k-sub">${esc(sub || "")}</div></div>`;
  const maxDay = Math.max(1, ...(stats.daily || []).map(x => x.tokens));
  const daily = (stats.daily || []).map(x => `<tr>
      <td>${esc(x.day)}</td><td>${num(x.requests)}</td>
      <td><div class="row-flex"><div class="bar" style="width:${Math.max(4, x.tokens / maxDay * 100)}px"><i></i></div>
      <span class="muted small">${fmtK(x.tokens)}</span></div></td></tr>`).join("");
  const rows = (arr, first) => arr.map(x => `<tr><td>${esc(x[first]) || "—"}</td>
      <td>${num(x.requests)}</td><td>${fmtK(x.tokens)}</td>
      <td>${fmtCost(x.cost_usd)}</td></tr>`).join("");
  // 按别名：io 比是全站判断「这条链是否在过度重读历史」的抓手。
  // 全站基线约 132:1（2026-09-26 实测）；某条链显著高于它，通常是路由把它死钉在
  // 一个慢模型上（zk-k3 实测 640:1，吃掉全站 input 的 72%）。
  // >200:1 标黄、>500:1 标红：颜色只是提示，阈值写在文案里，不靠猜。
  const aliasRows = arr => arr.map(x => {
    const r = x.io_ratio || 0;
    const tone = r > 500 ? "is-bad" : r > 200 ? "is-warn" : "";
    const note = r > 500 ? "（严重：每轮几乎都在重读历史）"
      : r > 200 ? "（偏高：看看是不是死钉了单个模型）" : "";
    return `<tr class="${tone}"><td>${esc(x.alias)}${note ? `<span class="muted small">${note}</span>` : ""}</td>
      <td>${num(x.requests)}</td><td>${fmtK(x.input_tokens)}</td>
      <td>${fmtK(x.output_tokens)}</td><td>${r ? num(r) + ":1" : "—"}</td>
      <td>${fmtCost(x.cost_usd)}</td></tr>`;
  }).join("");
  $("#view-usage").innerHTML = `
    <div class="view-head">
      <h2>用量统计</h2>
      <span class="desc">Token 与等价成本（按公开牌价折算，免费渠道即省下的钱）</span>
    </div>
    <div class="toolbar">
      <span class="muted small">时间窗口</span>
      <select id="u-days">${[1, 7, 30].map(d => `<option value="${d}" ${d === state.usageDays ? "selected" : ""}>近 ${d} 天</option>`).join("")}</select>
      <span class="grow"></span>
      <button id="u-backfill" title="把历史零成本记录按当前牌价补算">🧮 回填历史成本</button>
    </div>
    <div class="grid kpis">
      ${kpi("请求数", num(u.requests), `窗口 ${stats.window_days} 天`)}
      ${kpi("输入 Tokens", fmtK(u.input_tokens), num(u.input_tokens) + " 精确值")}
      ${kpi("输出 Tokens", fmtK(u.output_tokens), num(u.output_tokens) + " 精确值")}
      ${kpi("等价成本", fmtCost(u.cost_usd), "按公开牌价折算；免费渠道=省下的钱，付费渠道=真实成本")}
    </div>
    <h4 class="sec">按天</h4>
    <div class="tablewrap"><table><thead><tr><th>日期</th><th>请求数</th><th>Tokens</th></tr></thead>
      <tbody>${daily || '<tr><td colspan="3" class="muted">暂无数据</td></tr>'}</tbody></table></div>
    <h4 class="sec">按别名（客户端选了哪条链）</h4>
    <div class="tablewrap"><table><thead><tr>
      <th>别名</th><th>请求数</th><th>输入 Tokens</th><th>输出 Tokens</th>
      <th>输入/输出</th><th>成本</th></tr></thead>
      <tbody>${aliasRows(u.by_alias || [])}</tbody></table></div>
    <h4 class="sec">按供应商</h4>
    <div class="tablewrap"><table><thead><tr><th>供应商</th><th>请求数</th><th>Tokens</th><th>成本</th></tr></thead>
      <tbody>${rows(u.by_provider || [], "provider")}</tbody></table></div>
    <h4 class="sec">按模型（Top）</h4>
    <div class="tablewrap"><table><thead><tr><th>模型</th><th>请求数</th><th>Tokens</th><th>成本</th></tr></thead>
      <tbody>${rows(u.top_models || [], "model")}</tbody></table></div>
    <h4 class="sec">按凭据（哪把 Key 在干活）</h4>
    <div class="tablewrap"><table><thead><tr><th>凭据</th><th>请求数</th><th>Tokens</th><th>成本</th></tr></thead>
      <tbody>${rows(u.by_credential || [], "credential_id")}</tbody></table></div>`;
  $("#u-days").onchange = e => { state.usageDays = Number(e.target.value); loadUsage(); };
  $("#u-backfill").onclick = async () => {
    const r = await api("/admin/usage/backfill-cost", { method: "POST" });
    toast(r.updated ? `已回填 ${r.updated} 条，合计 $${r.total_cost_usd}` : "没有需要回填的记录", "ok");
    loadUsage();
  };
}

/* ================= 模型与别名 ================= */
const CAP_DIMS = ["coding","reasoning","tool_use","vision","long_context","structured_output","speed","cost"];
const CAP_ZH = {coding:"编程", reasoning:"推理", tool_use:"工具调用", vision:"视觉",
  long_context:"长上下文", structured_output:"结构化输出", speed:"速度", cost:"便宜度"};

async function loadModels() {
  if (!(state.providers || []).length) {
    try { state.providers = (await api("/admin/providers")).data || []; } catch (e) {}
  }
  const [m, al] = await Promise.all([api("/admin/models"), api("/admin/aliases")]);
  state.modelsCache = m.data || [];
  state.aliasesCache = al.data || {};
  const modelRows = (m.data || []).map(x => {
    const deps = (x.deployments || []).map(d =>
      `<div>${esc(d.provider_id)} → <b>${esc(d.upstream_model)}</b>
       <span class="chip">p${d.priority}</span>
       <span class="chip ${d.input_cost_per_mtok || d.output_cost_per_mtok ? "" : "muted"}" title="牌价 USD/百万token（输入/输出）">
         💲${d.input_cost_per_mtok ?? 0}/${d.output_cost_per_mtok ?? 0}</span>
       ${d.enabled ? "" : " " + pill("disabled")}</div>`).join("");
    return `<tr><td class="acts"><div class="row-flex"><button class="mini" data-edit-model="${esc(x.id)}" title="编辑模型 ${esc(x.id)}">编辑</button><button class="mini danger" data-del-model="${esc(x.id)}" title="删除模型 ${esc(x.id)}">删除</button></div></td>
      <td class="name"><b>${esc(x.id)}</b>${x.description ? `<span class="sub">${esc(x.description)}</span>` : ""}</td>
      <td>${x.enabled ? pill("healthy") : pill("disabled")}</td>
      <td>${fmtK(x.context_window)}</td><td>${deps || "-"}</td>
    </tr>`;  }).join("");
  const aliasRows = Object.entries(al.data || {}).map(([name, a]) => `
    <tr><td class="acts"><div class="row-flex"><button class="mini" data-edit-alias="${esc(name)}" title="编辑别名 ${esc(name)}">编辑</button><button class="mini danger" data-del-alias="${esc(name)}" title="删除别名 ${esc(name)}">删除</button></div></td>
    <td class="name"><b>${esc(name)}</b>${a.description ? `<span class="sub">${esc(a.description)}</span>` : ""}</td>
    <td>${esc(a.strategy)}</td>
    <td>${(a.targets || []).map(esc).join(" → ")}</td>
    <td class="small">${a.front_model
        ? `<span class="chip" title="接口模型：永远排第一；关闭后越过首位按正常权重排序">📌 ${esc(a.front_model)}</span>`
        : `<span class="chip muted" title="未设接口模型：完全按能力权重排序">权重排序</span>`}</td>
    <td class="small muted">${esc(((al.resolved || {})[name] || []).join(", "))}</td>
    <td>${a.enabled ? pill("healthy") : pill("disabled")}</td>
    </tr>`).join("");
  const warnings = (m.config_warnings || []).length
    ? `<div class="toolbar"><span class="pill amber">⚠ 配置警告 ${m.config_warnings.length} 条</span>
       <details class="raw grow"><summary>展开</summary><pre>${m.config_warnings.map(esc).join("\n")}</pre></details></div>` : "";
  $("#view-models").innerHTML = `
    <div class="view-head">
      <h2>模型与别名</h2>
      <span class="desc">模型 → 部署 → 供应商全景；别名的目标链顺序即优先级</span>
    </div>
    ${warnings}
    <div class="toolbar"><h4 class="sec grow" style="margin:0">模型（${(m.data || []).length}）</h4>
      <button class="primary" id="md-market">🧺 模型市场（从供应商导入）</button>
      <button id="md-new">➕ 手动新建</button></div>
    <div class="tablewrap"><table><thead><tr><th class="acts">操作</th><th>模型</th><th>状态</th><th>上下文</th><th>部署（故障转移顺序）</th></tr></thead>
      <tbody>${modelRows}</tbody></table></div>
    <div class="toolbar" style="margin-top:22px"><h4 class="sec grow" style="margin:0">别名（对外稳定入口）</h4>
      <button class="primary" id="al-new">➕ 新建别名</button></div>
    <div class="tablewrap"><table><thead><tr><th class="acts">操作</th><th>别名</th><th>策略</th><th>目标链</th><th>接口模型</th><th>解析结果</th><th>状态</th></tr></thead>
      <tbody>${aliasRows}</tbody></table></div>
    <h4 class="sec">路由预览（不改配置直接看某个请求会怎么走）</h4>
    <div class="card"><div class="row-flex">
      <input id="pv-model" size="16" value="zk-auto" placeholder="模型/别名">
      <input id="pv-prompt" size="26" value="写一个快速排序并解释" placeholder="模拟 prompt">
      <label class="small muted">tools <input id="pv-tools" type="number" value="2" min="0" max="20" style="width:56px"></label>
      <label class="small muted"><input id="pv-json" type="checkbox"> JSON 模式</label>
      <button class="primary" id="pv-go">预览</button>
    </div><div id="pv-out" style="margin-top:10px"></div></div>`;
  $("#pv-go").onclick = async () => {
    const p = new URLSearchParams({
      model: $("#pv-model").value.trim() || "zk-auto",
      prompt: $("#pv-prompt").value, tools: $("#pv-tools").value,
      json_mode: $("#pv-json").checked ? "true" : "false",
    });
    const r = await api("/admin/router/preview?" + p.toString());
    if (r.error) { $("#pv-out").innerHTML = `<span class="pill red">${esc(r.error.message)}</span>`; return; }
    const plan = (r.plan || []).map((c, i) => `
      <tr><td>${rankBadge(Number(c.order ?? i + 1))}</td><td>${esc(c.deployment_id || c.id || "-")}</td>
      <td>${esc(c.provider_id || c.provider || "-")}</td><td>${esc(c.model || "-")}</td>
      <td>${c.eligible ? pill("healthy") : pill("disabled")}</td></tr>`).join("");
    $("#pv-out").innerHTML = `
      <div class="small muted" style="margin-bottom:6px">${esc(r.reason || "")}
      ${r.alias ? ` · 别名 ${esc(r.alias)}` : ""} · 策略 ${esc(r.strategy || "-")}</div>
      <div class="tablewrap"><table><thead><tr><th>#</th><th>部署</th><th>供应商</th><th>上游模型</th><th>入选</th></tr></thead>
      <tbody>${plan}</tbody></table></div>
      <details class="raw"><summary>原始数据</summary><pre>${esc(JSON.stringify(r, null, 1).slice(0, 4000))}</pre></details>`;
  };
  $("#md-new").onclick = () => openModelModal(null);
  $("#md-market").onclick = openMarketModal;
  $("#al-new").onclick = () => openAliasModal(null);
  document.querySelectorAll("[data-edit-model]").forEach(b =>
    b.onclick = () => openModelModal(b.dataset.editModel));
  document.querySelectorAll("[data-del-model]").forEach(b =>
    b.onclick = () => deleteModel(b.dataset.delModel));
  document.querySelectorAll("[data-edit-alias]").forEach(b =>
    b.onclick = () => openAliasModal(b.dataset.editAlias));
  document.querySelectorAll("[data-del-alias]").forEach(b =>
    b.onclick = () => deleteAlias(b.dataset.delAlias));
}

/* ---------- 模型编辑弹窗 ---------- */
function providerOptions(sel) {
  return (state.providers || []).map(p =>
    `<option value="${esc(p.id)}" ${p.id === sel ? "selected" : ""}>${esc(p.id)}</option>`).join("");
}
function depCardHtml(d, idx) {
  d = d || {};
  return `<div class="dep-card" data-dep-idx="${idx}">
    <div class="row-flex">
      <b class="small">部署 #${idx + 1}</b><span class="grow"></span>
      <button class="mini" data-dep-del="${idx}">删除此部署</button>
    </div>
    <div class="formgrid">
      <label>部署 ID<input type="text" data-df="id" value="${esc(d.id || "")}" placeholder="如 glm53-nvidia"></label>
      <label>供应商<select data-df="provider_id">${providerOptions(d.provider_id)}</select></label>
      <label>上游模型名<input type="text" data-df="model" value="${esc(d.upstream_model || d.model || "")}" placeholder="如 z-ai/glm-5.3"></label>
      <label>优先级<input type="number" data-df="priority" value="${d.priority ?? 100}"></label>
      <label>上下文窗口<input type="number" data-df="context_window" value="${d.context_window ?? 128000}"></label>
      <label>最大输出 tokens<input type="number" data-df="max_output_tokens" value="${d.max_output_tokens ?? ""}" placeholder="可空"></label>
      <label>输入牌价 ($/Mtok)<input type="number" step="0.01" data-df="input_cost_per_mtok" value="${d.input_cost_per_mtok ?? 0}"></label>
      <label>输出牌价 ($/Mtok)<input type="number" step="0.01" data-df="output_cost_per_mtok" value="${d.output_cost_per_mtok ?? 0}"></label>
      <label>启用 <select data-df="enabled">
        <option value="true" ${d.enabled !== false ? "selected" : ""}>启用</option>
        <option value="false" ${d.enabled === false ? "selected" : ""}>禁用</option></select></label>
    </div>
  </div>`;
}
function openModelModal(modelId, prefill) {
  // prefill._from_market 由市场点「＋ 添加」打上：保存完要回市场并重探，
  // 否则列表的「已收录」还是探测时的旧值，看起来像没加成功。
  const fromMarket = !modelId && !!(prefill && prefill._from_market);
  const src = modelId ? (state.modelsCache || []).find(x => x.id === modelId) : null;
  // prefill: 来自模型市场的预设（modelId 为 null 时）——直接填好表单，用户只微调
  const m = src || prefill || {id: "", enabled: true, context_window: 128000, capabilities: {}, deployments: []};
  const facts = prefill?.discovered || {};   // 供应商 API 实测（可能为空）
  const factsNote = prefill?.facts_note || [];
  const caps = Object.fromEntries(CAP_DIMS.map(k => [k, (m.capabilities || {})[k] ?? 5]));
  let deps = (m.deployments || []).map(d => Object.assign({}, d));
  if (!deps.length && !modelId) deps = [{}];
  const modal = $("#model-modal");
  modal.innerHTML = `<div class="card wide">
    <h3>${modelId ? "编辑模型" : (prefill ? "从模型市场新建模型" : "新建模型")}</h3>
    <div class="modal-sub">改完立即生效，并同步写回配置文件（重启后仍在）。</div>
    ${factsNote.length ? `
      <div class="facts-card">
        <div class="f-title">📋 供应商实测能力（表单已按这些实测值预填）</div>
        <table class="small"><tbody>
          ${factsNote.map(f => `<tr>
            <td style="white-space:nowrap;width:96px">${esc(f.label)}</td>
            <td style="white-space:nowrap;width:150px"><b>${esc(f.value)}</b></td>
            <td class="muted">${esc(f.meaning)}</td></tr>`).join("")}
        </tbody></table>
      </div>` : ""}
    <div class="formgrid">
      <label>模型 ID（对外名）<input type="text" id="mm-id" value="${esc(m.id)}" ${modelId ? "disabled" : ""} placeholder="如 glm-5.3"></label>
      <label>显示名<input type="text" id="mm-name" value="${esc(m.display_name || "")}" placeholder="可空"></label>
      <label>归属<input type="text" id="mm-owner" value="${esc(m.owned_by || "")}" placeholder="如 z-ai"></label>
      <label>上下文窗口${facts.context_window ? ' <span class="chip" title="上游实测值">实测 ' + fmtK(facts.context_window) + '</span>' : ""}
        <input type="number" id="mm-ctx" value="${m.context_window ?? 128000}" ${facts.context_window ? 'title="供应商 API 给出的上限，通常不用改"' : ""}></label>
      <label>启用 <select id="mm-enabled">
        <option value="true" ${m.enabled !== false ? "selected" : ""}>启用</option>
        <option value="false" ${m.enabled === false ? "selected" : ""}>禁用</option></select></label>
      <label style="grid-column:1/-1">描述<input type="text" id="mm-desc" value="${esc(m.description || "")}" placeholder="可空"></label>
    </div>
    <h4 class="sec">能力评分（0–10，cost 越大代表越便宜）</h4>
    <div class="capgrid">${CAP_DIMS.map(k => {
      // 上游只说一次实话的地方就不让手填：视觉/上下文由实测决定
      const locked = (k === "vision" && facts.vision_input !== undefined) ||
                     (k === "reasoning" && facts.reasoning !== undefined);
      if (locked) {
        const known = k === "vision" ? facts.vision_input : facts.reasoning;
        return `<label>${CAP_ZH[k]}
          <input type="number" min="0" max="10" step="0.5" data-cap="${k}" value="${caps[k]}" disabled
            title="供应商已${known ? "支持" : "不支持"}，此项由实测决定">
          <span class="chip muted">实测${known ? "支持" : "不支持"}</span></label>`;
      }
      return `<label>${CAP_ZH[k]}<input type="number" min="0" max="10" step="0.5" data-cap="${k}" value="${caps[k]}"></label>`;
    }).join("")}
    </div>
    ${facts.reasoning_effort?.length ? `
      <div class="small muted" style="margin-top:8px">该模型的思考强度可调：${facts.reasoning_effort.map(esc).join(" / ")}
      <span title="请求时用 reasoning_effort 参数指定，越强越慢也越准">（是什么？）</span></div>` : ""}
    <h4 class="sec">部署（哪个供应商、上游模型叫什么）</h4>
    <div id="mm-deps"></div>
    <div class="toolbar" style="margin-top:10px"><button id="mm-add-dep">➕ 加部署</button></div>
    <div class="modal-actions">
      <button class="primary" id="mm-save">💾 保存</button>
      <button id="mm-cancel">取消</button>
    </div>
  </div>`;
  openModal("#model-modal");
  const renderDeps = () => {
    $("#mm-deps").innerHTML = deps.map((d, i) => depCardHtml(d, i)).join("");
    document.querySelectorAll("[data-dep-del]").forEach(b => b.onclick = () => {
      collectDeps(); deps.splice(Number(b.dataset.depDel), 1); renderDeps();
    });
  };
  const collectDeps = () => {
    document.querySelectorAll("#mm-deps .dep-card").forEach((card, i) => {
      const f = k => card.querySelector(`[data-df="${k}"]`);
      const numv = k => { const v = f(k).value.trim(); return v === "" ? null : Number(v); };
      deps[i] = {
        id: f("id").value.trim(),
        provider_id: f("provider_id").value,
        model: f("model").value.trim(),
        priority: Number(f("priority").value || 100),
        context_window: Number(f("context_window").value || 128000),
        max_output_tokens: numv("max_output_tokens"),
        input_cost_per_mtok: Number(f("input_cost_per_mtok").value || 0),
        output_cost_per_mtok: Number(f("output_cost_per_mtok").value || 0),
        enabled: f("enabled").value === "true",
      };
    });
  };
  $("#mm-add-dep").onclick = () => { collectDeps(); deps.push({}); renderDeps(); };
  renderDeps();
  $("#mm-cancel").onclick = () => closeModal("#model-modal");
  $("#mm-save").onclick = async () => {
    collectDeps();
    const caps2 = {};
    document.querySelectorAll("[data-cap]").forEach(i => caps2[i.dataset.cap] = Number(i.value));
    const payload = {
      id: $("#mm-id").value.trim(),
      display_name: $("#mm-name").value.trim() || null,
      owned_by: $("#mm-owner").value.trim() || null,
      description: $("#mm-desc").value.trim() || null,
      enabled: $("#mm-enabled").value === "true",
      context_window: Number($("#mm-ctx").value || 128000),
      capabilities: caps2,
      deployments: deps.filter(d => d.id && d.model),
    };
    if (!payload.id) { toast("模型 ID 不能为空", "err"); return; }
    try {
      const r = await api("/admin/models", { method: "POST", json: payload });
      if (r.error) { toast(r.error.message || "保存失败", "err"); return; }
      toast(`模型 ${payload.id} 已保存${syncedNote(r)}`, "ok");
      // 从市场来的：回市场并重探（already_added 是探测时算的，不重探
      // 就还是旧值，运营者会以为没加成功）。其余路径照旧关表单。
      if (fromMarket) {
        closeModals();
        openMarketModal($("#mk-provider") ? $("#mk-provider").value : "");
      } else {
        closeModal("#model-modal");
      }
      loadModels();
    } catch (err) { toast(err.message, "err"); }
  };
}
async function deleteModel(modelId) {
  if (!confirm(`确定删除模型 ${modelId}？（别名里若引用需先编辑掉；删除会同步改掉本地配置文件）`)) return;
  try {
    const r = await api("/admin/models/" + encodeURIComponent(modelId), { method: "DELETE" });
    if (r.error) { toast(r.error.message || "删除失败", "err"); return; }
    toast(`模型 ${modelId} 已删除${syncedNote(r)}`, "ok");
    loadModels();
  } catch (err) { toast(err.message, "err"); }
}

/* ---------- 模型市场：从供应商导入 ---------- */
async function openMarketModal(presetPid) {
  const modal = $("#market-modal");
  // 只列有可用凭据的供应商：list_models 靠 Key 才能列，没有的列了也是 401
  const providers = (state.providers || []).filter(p => p.requires_credential && p.available);
  if (!providers.length) { toast("没有可用供应商（先给某个供应商配好能用的 Key）", "err"); return; }
  const first = presetPid && providers.some(p => p.id === presetPid) ? presetPid : providers[0].id;
  modal.innerHTML = `<div class="card wide">
    <h3>🧺 模型市场</h3>
    <div class="modal-sub">选一个供应商，点「探测」列出它的模型目录（目录不大时会**自动验证**一遍）——
      <b>目录里有 ≠ 你账号能调</b>（实测：商汤列了 deepseek-v4.1-flash 但套餐没有；NVIDIA 列的 glm-5.3 直接挂死）。</div>
    <div class="row-flex" style="margin-bottom:10px">
      <label class="small muted">供应商
        <select id="mk-provider">${providers.map(p => `<option value="${esc(p.id)}" ${p.id === first ? "selected" : ""}>${esc(p.id)}</option>`).join("")}</select>
      </label>
      <button class="primary" id="mk-load">🔍 探测可用模型</button>
      <button id="mk-verify" disabled title="对列出的模型各发一次最小真实请求，看哪些你账号真能调">🔬 验证可调用</button>
      <span class="grow"></span>
      <button id="mk-close">关闭</button>
    </div>
    <div id="mk-list" class="small muted">选供应商后点「探测」列出它能调的所有模型，已收录的会预填上下文/能力/价格。</div>
  </div>`;
  openModal("#market-modal");
  $("#mk-close").onclick = () => closeModal("#market-modal");
  // upstream_model -> 验证结论。点「验证可调用」后填充，用来在表里标注。
  let verifyMap = {};
  let lastItems = [];
  // 目录不大就替运营者跑一遍验证。
  // 2026-09-29 的教训：探针能判「这个 ID 能不能调」，但运营者**不会主动点**，
  // 于是死 ID（deepseek-v4.1-flash 403）和能用的（deepseek-flash 200）在列表里
  // 长得一模一样，只能去翻官方公告才分得清。默认替他跑，而不是等他点。
  const AUTO_VERIFY_MAX = 20;
  const VERIFY_CAP = 40;
  async function runVerify(models) {
    const pid = $("#mk-provider").value;
    const btn = $("#mk-verify");
    btn.disabled = true; btn.textContent = `🔬 验证中（${models.length} 个，每个最多 20 秒）…`;
    try {
      const r = await api(`/admin/providers/${encodeURIComponent(pid)}/models/verify`,
                          { method: "POST", json: { models, timeout_seconds: 20 } });
      if (r.error) { toast(r.error.message || "验证失败", "err"); return; }
      verifyMap = {};
      (r.results || []).forEach(x => { verifyMap[x.upstream_model] = x; });
      const dead = (r.checked || 0) - (r.usable || 0);
      toast(`验证完成：${r.usable}/${r.checked} 可调用` + (dead ? `，${dead} 个不可用` : ""),
            dead ? "err" : "ok");
      if (typeof window.__mkRerender === "function") window.__mkRerender();
    } catch (err) { toast(err.message, "err"); }
    finally { btn.disabled = false; btn.textContent = "🔬 验证可调用"; }
  }
  $("#mk-verify").onclick = () => {
    const models = lastItems.map(x => x.upstream_model).slice(0, VERIFY_CAP);
    if (!models.length) { toast("先点「探测可用模型」", "err"); return; }
    runVerify(models);
  };
  $("#mk-load").onclick = async () => {
    const pid = $("#mk-provider").value;
    $("#mk-list").innerHTML = '<span class="muted">探测中…</span>';
    try {
      const r = await api(`/admin/providers/${encodeURIComponent(pid)}/models`);
      let items = r.data || [];
      if (!items.length) {
        $("#mk-list").innerHTML = `<span class="pill amber">${esc(r.note || "这个供应商没有可列的模型")}</span>`;
        return;
      }
      const VERDICT = {
        ok: ["healthy", "可调用"],
        hang: ["danger", "挂死"],
        timeout: ["danger", "响应超时"],
        not_found: ["danger", "上游没有"],
        no_entitlement: ["danger", "套餐无权限"],
        rate_limited: ["amber", "限流中"],
        error: ["amber", "报错"],
      };
      const render = (arr) => arr.map((x, idx) => {
        const v = verifyMap[x.upstream_model];
        const [tone, label] = v ? (VERDICT[v.verdict] || ["amber", v.verdict]) : ["gray", "未验证"];
        const cell = v
          ? `<span class="pill ${tone}" title="${esc(v.message || "")}">${v.ok ? "✅" : "⛔"} ${esc(label)}</span>`
          : `<span class="pill gray" title="点上方「验证可调用」">未验证</span>`;
        return `
        <tr class="${x.already_added ? "muted" : ""}">
          <td class="acts">${x.already_added
            ? '<span class="pill gray">已收录</span>'
            : `<button class="mini primary" data-add-model="${idx}" ${v && !v.ok ? 'disabled title="实测不可调用，别加"' : ""}>＋ 添加</button>`}</td>
          <td class="name"><b>${esc(x.suggested_model_id)}</b><span class="sub">${esc(x.upstream_model)}</span></td>
          <td class="small">${cell}</td>
          <td class="small">${fmtK(x.discovered?.context_window ?? x.preset.context_window)}
            ${x.discovered_known ? '<span class="chip" title="来自供应商 API 实测">实测</span>' : '<span class="chip muted" title="没有实测数据，按经验值预估">预估</span>'}</td>
          <td class="small muted">${esc(x.preset.description)}</td>
          <td class="small">${x.preset.input_price ? "$" + x.preset.input_price + "/" + x.preset.output_price : "-"}</td>
        </tr>`;
      }).join("");
      lastItems = items;
      $("#mk-verify").disabled = false;
      window.__mkRerender = () => renderList($("#mk-q") ? $("#mk-q").value : "");
      const renderList = (q) => {
        const filtered = q
          ? items.filter(x =>
              (x.suggested_model_id + " " + x.upstream_model + " " + (x.preset.description || ""))
                .toLowerCase().includes(q.toLowerCase()))
          : items;
        $("#mk-rows").innerHTML = render(filtered) ||
          `<tr><td colspan="6" class="muted">没有匹配「${esc(q)}」的模型</td></tr>`;
        document.querySelectorAll("[data-add-model]").forEach(b => b.onclick = () => {
          const x = filtered[Number(b.dataset.addModel)];
          const p = x.preset;
          const providerId = $("#mk-provider").value;
          openModelModal(null, { _from_market: true,
            id: x.suggested_model_id,
            display_name: p.display_name,
            owned_by: providerId,
            description: p.description,
            enabled: true,
            context_window: x.discovered?.context_window || p.context_window,
            capabilities: p.capabilities,
            // 实测事实随表单一起带过去：表单据此决定哪些参数可调、哪些直接定值
            discovered: x.discovered,
            facts_note: x.facts_note,
            deployments: [{
              id: `${x.suggested_model_id}-${providerId}`,
              provider_id: providerId,
              model: x.upstream_model,
              priority: 100,
              context_window: x.discovered?.context_window || p.context_window,
              max_output_tokens: 32768,
              input_cost_per_mtok: p.input_price,
              output_cost_per_mtok: p.output_price,
            }],
            presetDescription: p.description,
          });
        });
      };
      $("#mk-list").innerHTML = `
        <div class="row-flex" style="margin-bottom:8px">
          <input id="mk-q" size="22" placeholder="搜索模型名 / 说明…" style="flex:1">
          <span class="small muted" id="mk-count"></span>
        </div>
        <div class="tablewrap" style="max-height:55vh"><table>
          <thead><tr><th class="acts">操作</th><th>模型</th><th>可调用</th><th>上下文</th><th>说明</th><th>牌价 $/Mtok</th></tr></thead>
          <tbody id="mk-rows"></tbody>
        </table></div>`;
      const updateCount = () => {
        const q = $("#mk-q").value.trim();
        const filtered = q
          ? items.filter(x =>
              (x.suggested_model_id + " " + x.upstream_model + " " + (x.preset.description || ""))
                .toLowerCase().includes(q.toLowerCase()))
          : items;
        $("#mk-count").textContent = `${filtered.length} / ${items.length} 个 · ${items.filter(x => x.already_added).length} 已收录`;
        renderList(q);
      };
      $("#mk-q").oninput = updateCount;
      updateCount();
      // 目录不大就自动验证一遍（理由见 AUTO_VERIFY_MAX 的注释）
      if (items.length <= AUTO_VERIFY_MAX) {
        runVerify(items.map(x => x.upstream_model).slice(0, VERIFY_CAP));
      } else {
        $("#mk-verify").title =
          `目录有 ${items.length} 个模型，超过自动验证上限 ${AUTO_VERIFY_MAX} 个`
          + `——用搜索框缩小范围后点「验证可调用」`;
      }
    } catch (err) { $("#mk-list").innerHTML = `<span class="pill red">${esc(err.message)}</span>`; }
  };
  // Handlers are wired before this trigger - clicking first would be a no-op.
  $("#mk-provider").onchange = () => $("#mk-load").click();
  // 从「需要处理」点进来时直接探测，省掉那一次点击
  if (presetPid) $("#mk-load").click();
}

/* ---------- 别名编辑弹窗 ---------- */
function openAliasModal(name) {
  const src = name ? (state.aliasesCache || {})[name] : null;
  const a = src || {strategy: "capability", targets: [], enabled: true, description: "", weights: {}, requires: {}};
  const modal = $("#alias-modal");
  modal.innerHTML = `<div class="card wide">
    <h3>${name ? "编辑别名" : "新建别名"}</h3>
    <div class="modal-sub">别名是客户端唯一需要记住的名字；改它，调用方一行代码都不用动。</div>
    <div class="formgrid">
      <label>别名<input type="text" id="am-name" value="${esc(name || "")}" ${name ? "disabled" : ""} placeholder="如 zk-auto"></label>
      <label>策略<select id="am-strategy">${["capability","priority","speed","cost","round_robin","weighted"]
        .map(s => `<option ${s === a.strategy ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label>启用 <select id="am-enabled">
        <option value="true" ${a.enabled !== false ? "selected" : ""}>启用</option>
        <option value="false" ${a.enabled === false ? "selected" : ""}>禁用</option></select></label>
      <label style="grid-column:1/-1">目标链（每行一个模型/别名，顺序即优先级）
        <textarea id="am-targets" rows="5" style="width:100%">${esc((a.targets || []).join("\n"))}</textarea></label>
      <label style="grid-column:1/-1">描述<input type="text" id="am-desc" value="${esc(a.description || "")}" placeholder="可空"></label>
      <div style="grid-column:1/-1;border:1px solid var(--border);border-radius:var(--radius-sm);padding:11px 13px">
        <div class="fmrow">
          <label class="fmswitch" title="开启后所选模型永远排第一；关闭后越过首位，按正常权重排序">
            <input type="checkbox" id="am-fm-on" ${a.front_model ? "checked" : ""}>
            <span class="fmtrack"></span>
            <span class="fmlabel">接口模型（首位）</span>
          </label>
          <select id="am-fm-model" style="flex:1;min-width:220px" ${a.front_model ? "" : "disabled"}>
            ${(a.targets || []).map(t => `<option ${t === a.front_model ? "selected" : ""}>${esc(t)}</option>`).join("")}
          </select>
        </div>
        <div class="hint" style="margin-top:8px" id="am-fm-hint">
          开启：所选模型<b>永远排第一</b>先回应，其余仍按能力权重排序——接口模型挂掉/超时才往下走。<br>
          关闭：<b>越过首位</b>，完全按正常权重挑选，等价于不配这一项。
        </div>
      </div>
      <label>打分权重（可选，k=v 每行一个）<textarea id="am-weights" rows="3" style="width:100%"
        placeholder="coding=6&#10;tool_use=2">${esc(Object.entries(a.weights || {}).map(([k,v]) => `${k}=${v}`).join("\n"))}</textarea></label>
      <label>硬门槛 requires（可选，同上）<textarea id="am-requires" rows="3" style="width:100%"
        placeholder="vision=8">${esc(Object.entries(a.requires || {}).map(([k,v]) => `${k}=${v}`).join("\n"))}</textarea></label>
    </div>
    <div class="modal-actions">
      <button class="primary" id="am-save">💾 保存</button>
      <button id="am-cancel">取消</button>
    </div>
  </div>`;
  openModal("#alias-modal");
  $("#am-cancel").onclick = () => closeModal("#alias-modal");
  // 接口模型：关闭时下拉框禁用（保留上次选择，免得再开时重新挑），
  // 保存时 off = 不送 front_model，服务端就回到纯权重排序。
  const fmOn = $("#am-fm-on"), fmModel = $("#am-fm-model");
  fmOn.onchange = () => {
    fmModel.disabled = !fmOn.checked;
    // 开着但没得选（目标是别名而非模型）时给个可见提示，别让人以为存上了
    $("#am-fm-hint").style.color = fmOn.checked && !fmModel.value ? "var(--err, #e5534b)" : "";
  };
  const parseKV = id => {
    const out = {};
    $(id).value.split("\n").forEach(line => {
      const t = line.trim(); if (!t) return;
      const [k, v] = t.split("=").map(s => s.trim());
      if (k && v !== undefined && v !== "") out[k] = Number(v);
    });
    return out;
  };
  $("#am-save").onclick = async () => {
    const payload = {
      name: $("#am-name").value.trim(),
      strategy: $("#am-strategy").value,
      enabled: $("#am-enabled").value === "true",
      description: $("#am-desc").value.trim() || null,
      targets: $("#am-targets").value.split("\n").map(s => s.trim()).filter(Boolean),
      weights: parseKV("#am-weights"),
      requires: parseKV("#am-requires"),
    };
    // 接口模型：只在开关打开时送。关闭 = 不送 = 越过首位用正常权重模型。
    if (fmOn.checked && fmModel.value) payload.front_model = fmModel.value;
    if (!payload.name) { toast("别名不能为空", "err"); return; }
    if (payload.front_model && !payload.targets.includes(payload.front_model)) {
      toast("接口模型必须是目标链里的一员，否则它没有可用部署", "err");
      return;
    }
    try {
      const r = await api("/admin/aliases", { method: "POST", json: payload });
      if (r.error) { toast(r.error.message || "保存失败", "err"); return; }
      toast(`别名 ${payload.name} 已保存${syncedNote(r)}`, "ok");
      closeModal("#alias-modal");
      loadModels();
    } catch (err) { toast(err.message, "err"); }
  };
}
async function deleteAlias(name) {
  if (!confirm(`确定删除别名 ${name}？客户端再用这个名字会报未知模型；删除会同步改掉本地配置文件。`)) return;
  try {
    const r = await api("/admin/aliases/" + encodeURIComponent(name), { method: "DELETE" });
    if (r.error) { toast(r.error.message || "删除失败", "err"); return; }
    toast(`别名 ${name} 已删除${syncedNote(r)}`, "ok");
    loadModels();
  } catch (err) { toast(err.message, "err"); }
}

/* ================= 客户端接入（本机 config.toml / 公网调用） =================
 * 一个面板两种形态：
 *   - 网关在服务器上：看不到本机 ~/.codex，写 config.toml 没有意义。面板默认
 *     渲染「公网调用」——base_url、令牌取值命令、五种客户端的可复制片段；
 *     本机那段整块隐藏（不是禁用：禁用的按钮仍会让人以为点一下就能成）。
 *   - 网关在本机：保持原来的「写入 ~/.codex/config.toml」为主。
 * ==================================================================== */
let cgState = null; // GET /admin/chatgpt 的快照

const CG_CLIENTS = [
  { id: "openai", name: "OpenAI SDK / LangChain" },
  { id: "curl", name: "curl / PowerShell" },
  { id: "zcode", name: "ZCode（openai-compatible）" },
  { id: "claude", name: "Claude Code（Anthropic 协议）" },
  { id: "chatgpt", name: "ChatGPT / Codex 桌面版" },
];

function cgDeploy() { return (cgState && cgState.deploy) || {}; }

function cgPublicUrl() {
  const d = cgDeploy();
  if (d.client_base_url) return d.client_base_url;
  const port = (cgState && cgState.gateway && cgState.gateway.port) || 8317;
  return "http://127.0.0.1:" + port + "/v1";
}

function cgTokenName() {
  return (cgState && cgState.env_key_name) || "ZKAI_API_TOKEN";
}

function cgTokenValueHint() {
  // 占位提示：不把令牌写进页面 HTML（任何能打开 /ui 的人都能看源码）。
  // 复制时从当前会话的管理令牌取真值（见 cgTokenValue）。
  return "<与服务器 /opt/zkai/.env 里的 ZKAI_API_TOKEN 同值>";
}

function cgTokenValue() {
  // 当前会话的管理令牌就是 ZKAI_API_TOKEN 同值——登录时运营者已输入过，
  // 这里直接复用，不用去服务器 .env 里读。
  return state.token || "";
}

function cgModel() {
  return (cgState && cgState.desired && cgState.desired.model) || "zk-auto";
}
function cgCatalogueRows() {
  const list = (cgState && cgState.catalogue) || [];
  if (!list.length) return '<tr><td colspan="2" class="muted">暂无可用模型</td></tr>';
  return list.map(m => {
    const ctx = m.context_window ? " · " + Math.round(m.context_window / 1024) + "K 上下文" : "";
    return `<tr><td><code>${esc(m.name)}</code></td>`
      + `<td class="muted small">${esc(m.resolves_to)}${ctx}</td></tr>`;
  }).join("");
}

function cgSnippet(client) {
  const url = cgPublicUrl();
  const host = url.replace(/\/v1\/?$/, "");
  const model = cgModel();
  const tkn = cgTokenName();
  const env = "$" + tkn;
  switch (client) {
    case "openai":
      return [
        "from openai import OpenAI",
        "import os",
        "",
        "client = OpenAI(",
        `    base_url="${url}",`,
        `    api_key=os.environ["${tkn}"],  # 网关只校验非空，真实凭据在服务端`,
        ")",
        "",
        "resp = client.chat.completions.create(",
        `    model="${model}",`,
        '    messages=[{"role": "user", "content": "你好"}],',
        "    max_tokens=512,",
        ")",
        "print(resp.choices[0].message.content)",
      ].join("\n");
    case "curl":
      return [
        `curl ${url}/chat/completions \\`,
        `  -H "Authorization: Bearer ${env}" \\`,
        '  -H "Content-Type: application/json" \\',
        "  -d '{",
        `    "model": "${model}",`,
        '    "messages": [{"role": "user", "content": "你好"}],',
        '    "max_tokens": 512',
        "  }'",
        "",
        "# PowerShell 等价（-TimeoutSec 420：网关单次最坏要跑几百秒）",
        `Invoke-RestMethod "${url}/chat/completions" -Method POST \\`,
        `  -Headers @{ Authorization = "Bearer ${env}" } \\`,
        '  -ContentType "application/json; charset=utf-8" \\',
        `  -Body '{"model":"${model}","messages":[{"role":"user","content":"你好"}]}' \\`,
        "  -TimeoutSec 420",
      ].join("\n");
    case "zcode":
      return [
        "# ~/.zcode/v2/config.json 的 provider 键下加这一段（kind 固定 openai-compatible）",
        "# apiKey 随便填：网关不校客户端 Key，只校服务端。下面必须是合法 JSON:",
        "{",
        '  "name": "ZK-AI Gateway",',
        '  "kind": "openai-compatible",',
        '  "source": "custom",',
        '  "options": {',
        '    "apiKey": "any",',
        `    "baseURL": "${url}",`,
        '    "apiKeyRequired": true',
        "  },",
        '  "models": {',
        `    "${model}": { "limit": { "context": 1000000, "output": 32768 },`,
        '      "modalities": { "input": ["text"], "output": ["text"] } } }'
        + "}",
      ].join("\n");
    
    case "claude":
      return [
        "# 网关提供原生 Anthropic 协议端点，Claude Code 直接指过来",
        `export ANTHROPIC_BASE_URL="${host}"`,
        "export ANTHROPIC_AUTH_TOKEN=\"" + "<与服务器 .env 的 " + tkn + " 同值>" + "\"",
        `export ANTHROPIC_MODEL="${model}"`,
        `export ANTHROPIC_SMALL_FAST_MODEL="${model}"`,
        "",
        "claude   # 之后照常用",
        "",
        "# 请求走 /v1/messages：网关翻译成上游 OpenAI 方言，再把结果转回 Anthropic 事件流",
      ].join("\n");
    case "chatgpt":
      return [
        "# ~/.codex/config.toml（ChatGPT app 自己拥有的文件，只加一个 provider 表）",
        `model                  = "${model}"`,
        'model_provider         = "zkai"',
        'model_reasoning_effort = "max"',
        "",
        "[model_providers.zkai]",
        'name       = "ZK-AI"',
        `base_url   = "${url}"`,
        'wire_api   = "responses"',
        `env_key    = "${tkn}"`,
        "",
        "# Key 走环境变量，不写进 config.toml（桌面版从 explorer 启动，读不到 shell 变量）：",
        `setx ${tkn} "与服务器 .env 同值"   # 然后重启桌面版（含托盘退出）`,
      ].join("\n");
    default:
      return "";
  }
}

async function cgCopy(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMsg, "ok");
  } catch {
    const w = window.open("", "_blank");
    if (w) { w.document.write("<pre>" + esc(text) + "</pre>"); w.document.close(); }
    toast("浏览器不允许自动复制，已弹出内容供手动全选", "err");
  }
}

function cgRenderSnippet() {
  const box = $("#cg-snippet");
  if (box) box.textContent = cgSnippet($("#cg-client").value);
}
function cgToggleMode() {
  const official = $("#cg-mode").value === "official";
  $("#cg-official-model").closest("label").style.display = official ? "" : "none";
  $("#cg-official-note").style.display = official ? "" : "none";
}

function cgFormValues() {
  return {
    mode: $("#cg-mode").value,
    model: $("#cg-model").value.trim(),
    model_provider: $("#cg-provider").value.trim(),
    provider_display: $("#cg-display").value.trim(),
    base_url: $("#cg-baseurl").value.trim(),
    wire_api: $("#cg-wire").value,
    env_key: $("#cg-envkey").value.trim(),
    model_reasoning_effort: $("#cg-effort").value,
    official_model: $("#cg-official-model").value.trim(),
  };
}

function cgDiff(v) {
  const d = cgState.desired || {};
  const disk = cgState.disk || {};
  const rows = [];
  const cmp = (label, key, want) => {
    const cur = String(d[key] ?? disk[key] ?? "");
    if ((want || "") !== cur) rows.push({ label, old: cur || "（空）", new: want || "（删除该行）" });
  };
  cmp("model", "model", v.model);
  cmp("model_provider", "model_provider", v.mode === "official" ? "" : v.model_provider);
  cmp("provider_display", "provider_display", v.provider_display);
  cmp("base_url", "base_url", v.base_url);
  cmp("wire_api", "wire_api", v.wire_api);
  cmp("env_key", "env_key", v.env_key);
  cmp("model_reasoning_effort", "model_reasoning_effort", v.model_reasoning_effort);
  if (v.mode === "official") cmp("official_model", "official_model", v.official_model);
  return rows;
}
async function openChatGptModal() {
  const modal = $("#chatgpt-modal");
  modal.innerHTML = `<div class="card wide">
    <h3>🌐 客户端接入 <button class="mini modal-x" id="cg-x" title="关闭">✕</button></h3>
    <div class="modal-sub">网关现在跑在服务器上：把 <b>base_url</b> 和 <b>令牌</b> 交给客户端就能调。
      本机 <code>~/.codex/config.toml</code> 的改写只在网关就在这台机器上跑时才可用。</div>
    <div id="cg-loading" class="small muted">读取中…</div>
    <div id="cg-body" style="display:none">
      <div class="card" style="padding:12px 14px;margin-bottom:14px">
        <div class="small" id="cg-status" style="line-height:1.9"></div>
        <div class="small muted" id="cg-path" style="margin-top:4px;word-break:break-all"></div>
      </div>

      <h4 class="sec">公网调用（复制即用）</h4>
      <div class="formgrid">
        <label class="wide">base_url<input id="cg-pub-base" readonly></label>
        <label class="wide">令牌（环境变量名 = 值）<input id="cg-pub-token" readonly></label>
      </div>
      <div class="modal-actions" style="margin-top:0">
        <button id="cg-btn-copyurl">📋 复制 base_url</button>
        <button id="cg-btn-copytoken">📋 复制取值命令</button>
        <span class="small muted" id="cg-token-note"></span>
      </div>

      <h4 class="sec">客户端配置片段</h4>
      <div class="formgrid">
        <label class="wide">客户端<select id="cg-client"></select></label>
      </div>
      <div class="card" style="padding:0;margin-top:8px;position:relative">
        <button class="mini" id="cg-btn-copysnip" style="position:absolute;top:8px;right:8px;z-index:2">📋 复制</button>
        <pre id="cg-snippet" style="margin:0;padding:14px;overflow:auto;max-height:280px;font-size:var(--fs-12);line-height:1.6"></pre>
      </div>

      <h4 class="sec">可用模型（别名 → 实际模型）</h4>
      <div class="tablewrap" style="max-height:200px;overflow:auto">
        <table><thead><tr><th>名字</th><th>解析到</th></tr></thead>
        <tbody id="cg-catalogue"></tbody></table>
      </div>
      <div class="small muted" style="margin-top:6px">
        别名会自动挑模型、换 Key、失败自动转移；点名具体模型名则只用那一个。
      </div>

      <div id="cg-local-zone">
        <hr class="sep">
        <h4 class="sec">本机 config.toml（仅网关就在这台机器上时有效）</h4>
        <div class="modal-sub" style="margin-bottom:12px" id="cg-local-note"></div>
        <div class="formgrid">
          <label>接入方式
            <select id="cg-mode">
              <option value="zk-ai">走 ZK-AI 网关</option>
              <option value="official">切回 ChatGPT 官方</option>
            </select>
          </label>
          <label>模型 <input id="cg-model" list="cg-model-list" placeholder="zk-auto"></label>
          <datalist id="cg-model-list"></datalist>
          <label>官方模型名 <input id="cg-official-model" placeholder="gpt-5.6-terra"></label>
          <label>provider 表名 <input id="cg-provider" placeholder="zkai"></label>
          <label>表内显示名 <input id="cg-display" placeholder="ZK-AI"></label>
          <label>base_url <input id="cg-baseurl" placeholder="http://127.0.0.1:8317/v1"></label>
          <label>wire_api
            <select id="cg-wire"><option value="responses">responses</option><option value="chat">chat</option></select>
          </label>
          <label>env_key <input id="cg-envkey" placeholder="ZKAI_API_TOKEN"></label>
          <label>推理强度
            <select id="cg-effort">
              <option value="">（不写这一行）</option>
              <option>minimal</option><option>low</option><option>medium</option><option>high</option><option>max</option>
            </select>
          </label>
        </div>
        <div id="cg-official-note" class="small muted" style="display:none;margin-top:8px">
          切回官方 = 删掉 config.toml 里的 <code>model_provider</code> 行、把 <code>model</code> 改成官方模型名；
          provider 表保留不删，随时可以切回来。
        </div>
        <div id="cg-preview" style="display:none;margin-top:12px"></div>
        <div class="modal-actions">
          <button id="cg-btn-preview">🔍 预览变更</button>
          <button class="primary" id="cg-btn-save">💾 保存并写入</button>
          <button id="cg-btn-reapply" style="display:none">↻ 重新写入本机</button>
          <button id="cg-btn-syncenv" style="display:none" title="把网关 .env 里的令牌写进 Windows 用户级环境变量（桌面版从 explorer 启动只认这个）">🔧 同步令牌到用户环境变量</button>
          <span class="grow"></span>
        </div>
      </div>

      <hr class="sep">
      <h4 class="sec">热切换模型</h4>
      <div class="modal-sub" style="margin-bottom:12px">
        桌面版发的模型是 <code>zk-auto</code>；下面换模型 = 把所选模型提到 <code>zk-auto</code>
        链路最前面，<strong>下一条消息立即生效</strong>，不用重启桌面版。远程调用方同样是下一个请求即生效。
      </div>
      <div class="formgrid">
        <label class="wide">当前链首模型 <select id="cg-hot-model"></select></label>
      </div>
      <div class="modal-actions">
        <button class="primary" id="cg-hot-save">热切换</button>
        <span class="grow"></span>
        <span class="small muted" id="cg-chain"></span>
      </div>
      <div class="modal-actions" style="margin-top:14px">
        <span class="grow"></span>
        <button id="cg-cancel">关闭</button>
      </div>
    </div>
  </div>`;
  openModal("#chatgpt-modal");
  $("#cg-x").onclick = () => closeModal("#chatgpt-modal");
  $("#cg-cancel").onclick = () => closeModal("#chatgpt-modal");
  $("#cg-mode").onchange = cgToggleMode;
  $("#cg-btn-preview").onclick = cgPreview;
  $("#cg-btn-save").onclick = cgSave;
  $("#cg-btn-reapply").onclick = cgReapply;
  $("#cg-btn-syncenv").onclick = cgSyncEnv;
  $("#cg-hot-save").onclick = cgHotSwap;
  $("#cg-client").onchange = cgRenderSnippet;
  $("#cg-btn-copyurl").onclick = () => cgCopy(cgPublicUrl(), "base_url 已复制");
  $("#cg-btn-copytoken").onclick = () => {
    const tok = cgTokenValue();
    if (!tok) { toast("当前会话没有管理令牌——先点右上角 🔑 登录", "err"); return; }
    cgCopy(`${cgTokenName()}="${tok}"`, "令牌已复制（当前会话值）");
  };
  $("#cg-btn-copysnip").onclick = () => cgCopy($("#cg-snippet").textContent, "配置片段已复制");
  try {
    const r = await api("/admin/chatgpt");
    cgState = r;
    $("#cg-loading").style.display = "none";
    $("#cg-body").style.display = "";
    cgFill(r);
  } catch (err) {
    $("#cg-loading").textContent = "读取失败: " + err.message;
  }
}
function cgFill(r) {
  const disk = r.disk || {};
  const d = r.desired || {};
  const gw = r.gateway || { port: 8317 };
  const dep = r.deploy || {};
  // 优先用已保存的期望配置；没保存过就按盘现状预填（首次打开 = 当前工作状态）
  const val = (want, fallback) => (want || fallback || "");
  $("#cg-mode").value = val(d.mode, "zk-ai");
  $("#cg-model").value = val(d.model, disk.model || "zk-auto");
  $("#cg-official-model").value = val(d.official_model, "gpt-5.6-terra");
  $("#cg-provider").value = val(d.model_provider, disk.model_provider || "zkai");
  $("#cg-display").value = val(d.provider_display, "ZK-AI");
  $("#cg-baseurl").value = val(d.base_url, disk.base_url || ("http://127.0.0.1:" + gw.port + "/v1"));
  $("#cg-wire").value = val(d.wire_api, disk.wire_api || "responses");
  $("#cg-envkey").value = val(d.env_key, disk.env_key || "ZKAI_API_TOKEN");
  $("#cg-effort").value = val(d.model_reasoning_effort, disk.model_reasoning_effort || "max");
  cgToggleMode();

  $("#cg-model-list").innerHTML = (r.choices || []).map(c =>
    `<option value="${esc(c)}">`).join("");
  $("#cg-catalogue").innerHTML = cgCatalogueRows();

  // ---- 公网区 ----
  $("#cg-pub-base").value = cgPublicUrl();
  $("#cg-pub-token").value = `${cgTokenName()}="${cgTokenValueHint()}"`;
  $("#cg-token-note").textContent = dep.api_token_set
    ? "网关已设 ZKAI_API_TOKEN。点「📋 复制取值命令」复制的是当前会话的管理令牌（同值），可直接粘贴给客户端。"
    : "网关未设 ZKAI_API_TOKEN，当前不校验客户端令牌（仅适合回环监听）。";

  // ---- 本机区：远程部署时整块隐藏 ----
  const localZone = $("#cg-local-zone");
  const localNote = $("#cg-local-note");
  if (dep.is_remote || dep.can_write_local === false) {
    localZone.style.display = "none";
    localNote.innerHTML =
      `网关在服务器上（监听 ${esc(dep.listen || "?")}），打不到这台电脑的 <code>~/.codex/config.toml</code>，`
      + `所以「写入本机」不可用。要在桌面版接入，请在<b>你自己的电脑</b>上把「ChatGPT / Codex 桌面版」那段`
      + `片段写进它的 config.toml，并 <code>setx ${esc(cgTokenName())} "同值"</code> 配好令牌。`;
  } else {
    localZone.style.display = "";
    localNote.innerHTML =
      "这一区写本机 <code>~/.codex/config.toml</code>（app 自己的文件，"
      + "mcp_servers / plugins / projects 等段落原样保留）。";
  }

  $("#cg-client").innerHTML = CG_CLIENTS.map(c =>
    `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  $("#cg-client").value = "openai";
  cgRenderSnippet();

  // ---- 状态行 ----
  const bits = [];
  bits.push(r.config_exists ? "config.toml：存在" : "config.toml：不存在（保存后会新建）");
  bits.push(r.desired ? "期望配置：已保存" : "期望配置：未保存");
  const drift = (r.drift || []).length;
  bits.push(drift ? `本机与期望配置有 <strong style="color:var(--err)">${drift}</strong> 处不一致`
                  : "本机与期望配置一致");
  if (r.config_parse_error) {
    bits.push(`<span style="color:var(--err)">config.toml 解析失败：${esc(r.config_parse_error)}</span>`);
  }
  const auth = r.auth || {};
  if (auth.exists) {
    bits.push(auth.has_openai_key ? "auth.json：有 OpenAI 登录态" : "auth.json：存在（无 OpenAI Key）");
  }
  $("#cg-status").innerHTML = bits.join(" · ");
  $("#cg-path").textContent = dep.codex_config_path || r.path || "";

  $("#cg-btn-syncenv").style.display =
    (dep.is_remote || r.env_key_visible === false) ? "" : "none";
  if (r.env_key_matches_gateway === false) {
    $("#cg-status").innerHTML +=
      ` · <span style="color:var(--err)">用户环境变量里的 ${esc(r.env_key_name || "")} 与网关令牌不一致</span>`;
  }

  // ---- 热切换 ----
  const sel = $("#cg-hot-model");
  sel.innerHTML = (r.choices || []).map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  const eff = r.effective_model || "zk-auto";
  if (eff) sel.value = eff;
  $("#cg-chain").textContent = `链路：${(r.targets || []).join(" → ") || "（无）"}`;
}
function cgPreview() {
  const v = cgFormValues();
  if (!v.model) { toast("模型不能为空", "err"); return; }
  const rows = cgDiff(v);
  const box = $("#cg-preview");
  let html = `<div class="card" style="padding:10px 12px;background:var(--bg)">
    <div class="small" style="margin-bottom:6px"><strong>将写入 ${esc(cgState ? cgState.path : "~/.codex/config.toml")}</strong>`
    + `（旧文件先备份为 config.toml.bak-时间戳；mcp_servers / plugins / projects 等 app 自己的字段原样保留）</div>`;
  if (!rows.length) {
    html += `<div class="small muted">与盘现状一致，写入是无变化（不会新建备份）。</div>`;
  } else {
    html += `<table style="width:100%;font-size:var(--fs-12);border-collapse:collapse">
      <tr class="small muted"><td style="padding:2px 8px 2px 0">键</td><td>现值</td><td>改成</td></tr>`
      + rows.map(r2 =>
        `<tr><td style="padding:2px 8px 2px 0"><code>${esc(r2.label)}</code></td>`
        + `<td style="color:var(--muted)">${esc(r2.old)}</td>`
        + `<td><strong>${esc(r2.new)}</strong></td></tr>`).join("")
      + `</table>`;
  }
  html += `</div>`;
  box.innerHTML = html;
  box.style.display = "";
}

async function cgRefresh() {
  try {
    cgState = await api("/admin/chatgpt");
    cgFill(cgState);
  } catch (err) { toast(err.message, "err"); }
}

async function cgSave() {
  const v = cgFormValues();
  if (!v.model) { toast("模型不能为空", "err"); return; }
  const dep = cgDeploy();
  if (dep.is_remote || dep.can_write_local === false) {
    toast("网关在服务器上，写不到这台电脑的 config.toml——请按上面的片段在自己电脑上配置", "err");
    return;
  }
  const rows = cgDiff(v);
  if (!confirm(`确认写入？\n${rows.length
    ? rows.map(r2 => `${r2.label}: ${r2.old} → ${r2.new}`).join("\n")
    : "（与盘一致，无变化）"}`)) return;
  try {
    const r = await api("/admin/chatgpt/client", { method: "PUT", json: { ...v, apply: true } });
    const parts = [`期望配置已保存（${r.saved}）`];
    if (r.applied) {
      parts.push(r.no_op ? "config.toml 无变化"
        : `config.toml 已更新 ${r.apply_result.changes.length} 处（备份：${r.apply_result.backup || "无"}）`);
    }
    toast(parts.join("；"), "ok");
    (r.warnings || []).forEach(w => toast("提醒：" + w, "err"));
    await cgRefresh();
  } catch (err) { toast(err.message, "err"); }
}

async function cgReapply() {
  try {
    const r = await api("/admin/chatgpt/apply", { method: "POST" });
    toast(r.no_op ? "config.toml 已是最新"
      : `已按期望配置重写 config.toml（${r.apply_result.changes.length} 处）`, "ok");
    await cgRefresh();
  } catch (err) { toast(err.message, "err"); }
}

async function cgSyncEnv() {
  try {
    const r = await api("/admin/chatgpt/sync-env", { method: "POST" });
    toast(r.message + (r.verified ? "" : "（回读校验未通过，请手动 setx）"), r.verified ? "ok" : "err");
    await cgRefresh();
  } catch (err) { toast(err.message, "err"); }
}

async function cgHotSwap() {
  const model = $("#cg-hot-model").value.trim();
  if (!model) { toast("模型不能为空", "err"); return; }
  try {
    await api("/admin/chatgpt", { method: "POST", json: { model } });
    toast(`已切换为 ${model}，下一条消息生效`, "ok");
    await cgRefresh();
  } catch (err) { toast(err.message, "err"); }
}/* ================= 令牌 / 启动 ================= */
function showAuth() { $("#auth").classList.add("show"); $("#auth-token").value = state.token; $("#auth-token").focus(); }
$("#auth-save").onclick = () => {
  state.token = $("#auth-token").value.trim();
  localStorage.setItem("zkai_admin_token", state.token);
  $("#auth").classList.remove("show"); refresh();
};
$("#auth-cancel").onclick = () => $("#auth").classList.remove("show");
$("#btn-token").onclick = showAuth;
$("#btn-chatgpt").onclick = openChatGptModal;
$("#btn-refresh").onclick = () => refresh();
$("#auto-interval").onchange = setupTimer;
/* 主题切换：浅色（默认，对齐参考项目）/ 暗色，localStorage 持久化 */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $("#btn-theme").textContent = t === "dark" ? "☀️" : "🌙";
  localStorage.setItem("zkai_theme", t);
}
$("#btn-theme").onclick = () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
applyTheme(localStorage.getItem("zkai_theme") || "light");
document.querySelectorAll("#tabs button[data-view]").forEach(b => b.onclick = () => switchView(b.dataset.view));
mnavBuild();
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  $("#drawer").classList.remove("open");
  const open = MODAL_IDS.map(id => $(id)).find(el => el && el.classList.contains("show"));
  if (open) open.classList.remove("show");
});
/* 点遮罩空白处关闭弹窗（点在卡片内部不触发） */
MODAL_IDS.forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("click", e => { if (e.target === el) el.classList.remove("show"); });
});
// 窗口拉宽回桌面时把卡片标签拉回表格（避免留下永久的 data-label）
let _mResize = null;
window.addEventListener("resize", () => {
  clearTimeout(_mResize);
  _mResize = setTimeout(() => {
    if (!window.matchMedia("(max-width: 860px)").matches) {
      document.querySelectorAll("[data-label]").forEach(el => { delete el.dataset.label; });
    } else {
      tableCards();
    }
  }, 150);
});

/* URL 参数：
 *   ?token=xxx   管理令牌（书签直达，自动记住，随后从地址栏抹掉）
 *   ?view=pool   默认打开哪个视图（总览 / 凭据池 / 请求记录 / 用量统计 / 模型与别名），
 *                让「模型与别名」这种深页也能直接收藏、直接分享 */
const urlToken = new URLSearchParams(location.search).get("token");
if (urlToken) { state.token = urlToken; localStorage.setItem("zkai_admin_token", urlToken); }
const urlView = new URLSearchParams(location.search).get("view");
// 从 /ui/report 旧链接跳转过来的：自动切到体检视图
const gotoReport = sessionStorage.getItem("zkai_goto_report");
if (gotoReport) sessionStorage.removeItem("zkai_goto_report");
const initialView = gotoReport ? "report"
  : (urlView && LOADERS[urlView]) ? urlView : state.view;
history.replaceState(null, "", "/ui");
switchView(initialView);
/* ================= 积分消耗器 ================= */
/* 独立进程（scripts/burn_sensenova.py）烧商汤 Flash-Lite 专属池积分，1:1 折算成
   kimi-k3 可用积分。它不经网关，所以这里单独一页：左边是每账号的窗口账
   （什么时候该停、什么时候满血），右边是 config/burner.yaml 的可调参数。
   改配置只写 YAML，重启消耗器才生效——页面里说清楚，避免以为改了立刻见效。 */
const BURNER_LABELS = {
  model: "模型（只能是 Flash-lite）",
  concurrency: "全局并发上限",
  per_account_start: "单账号起始并发",
  per_account_max: "单账号并发上限",
  starve_after: "饥饿救济阈值（秒）",
  starve_grace: "重启宽限期（秒）",
  rate_park_after: "限流停靠：连续 429 次数",
  rate_park_seconds: "限流停靠时长（秒）",
  connect_timeout: "连接超时（秒）",
  read_timeout: "读取超时（秒）",
  max_tokens: "单次输出上限",
  filler_chars: "输入填充字符数",
  rate_in: "输入费率（积分/百万token）",
  rate_out: "输出费率（积分/百万token）",
  safety_margin: "安全系数",
  window_credits: "5h 窗口积分上限",
  weekly_credits: "周积分上限",
  pool_total_credits: "累计绝对上限（0=关）",
  quota_park_hours: "额度耗尽停靠时长（小时）",
  only: "只烧这些 Key",
  anchors: "5h 窗口锚点",
  week_anchors: "周窗口锚点（按账号）",
  week_anchor: "全局周锚点（回落值）",
  account_groups: "同账号 Key 分组",
  cooldown_base: "429 首次冷却（秒）",
  cooldown_max: "429 冷却上限（秒）",
  summary_interval: "汇总打印间隔（秒）",
  max_seconds: "最长运行秒数（0=常驻）",
};
const BURNER_NUM = new Set(["concurrency","per_account_start","per_account_max","max_tokens",
  "filler_chars","rate_in","rate_out","safety_margin","window_credits","weekly_credits",
  "pool_total_credits","quota_park_hours","cooldown_base","cooldown_max",
  "summary_interval","max_seconds"]);
const BURNER_WIDE = new Set(["only","anchors","week_anchors","account_groups"]);

/* 配置表单分组（h5 小标题；别用 h4.sec——手机手风琴按它切片，会再切一刀）。
   新增键没列入任何组时落到最后的「其它」。 */
const BURNER_GROUPS = [
  ["烧什么", ["model", "max_tokens", "filler_chars"]],
  ["并发与限流", ["concurrency", "per_account_start", "per_account_max",
    "starve_after", "starve_grace", "rate_park_after", "rate_park_seconds",
    "cooldown_base", "cooldown_max"]],
  ["预算与费率", ["rate_in", "rate_out", "safety_margin", "window_credits",
    "weekly_credits", "pool_total_credits", "quota_park_hours"]],
  ["账号与窗口", ["only", "account_groups", "anchors", "week_anchors", "week_anchor"]],
  ["其它", ["connect_timeout", "read_timeout", "summary_interval", "max_seconds"]],
];

function burnerField(key, value) {
  const label = BURNER_LABELS[key] || key;
  if (BURNER_WIDE.has(key)) {
    return `<label class="field" style="grid-column:1/-1"><span>${esc(label)}</span>
      <textarea id="bf-${key}" rows="2">${esc(value ?? "")}</textarea></label>`;
  }
  const type = BURNER_NUM.has(key) ? "number" : "text";
  const step = BURNER_NUM.has(key) ? "any" : null;
  return `<label class="field"><span>${esc(label)}</span>
    <input id="bf-${key}" type="${type}" ${step ? `step="${step}"` : ""} value="${esc(value ?? "")}"></label>`;
}

function burnerFields(keys, cfg) {
  const seen = new Set();
  const parts = [];
  for (const [title, members] of BURNER_GROUPS) {
    const cells = members.filter(k => keys.includes(k));
    if (!cells.length) continue;
    cells.forEach(k => seen.add(k));
    parts.push(`<h5 style="grid-column:1/-1;margin:14px 0 2px;font-size:var(--fs-12);color:var(--muted)">${esc(title)}</h5>`
      + cells.map(k => burnerField(k, cfg[k])).join(""));
  }
  const rest = keys.filter(k => !seen.has(k));
  if (rest.length) parts.push(rest.map(k => burnerField(k, cfg[k])).join(""));
  return parts.join("");
}

const burnLog = { es: null, follow: true, retries: 0, timer: null };

/* epoch -> "MM-DD HH:MM"，和窗口账表格里周锚点输入框的格式一致：
   运营者看到下次重置，填进输入框就能直接对上，不用换算。 */
function cgBoundaryText(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadBurner() {
  const d = await api("/admin/burner");
  const L = d.ledger || {};
  const cost = d.request_cost || 0;
  const kpi = (label, value, sub, tone) => `<div class="kpi${tone ? " is-" + tone : ""}">
    <div class="k-label">${esc(label)}</div><div class="k-value">${value}</div>
    <div class="k-sub">${esc(sub || "")}</div></div>`;
  const warn = (d.warnings || []).map(w =>
    `<div class="att-row is-warn"><span class="att-ico">⚠️</span><span>${esc(w)}</span></div>`).join("");
  // 全局汇总：所有账号的周烧量加在一起，算出全局剩余比例和最高风险账号
  const accts = d.accounts || [];
  const totalBurned = accts.reduce((s, a) => s + (a.burned_week || 0), 0);
  const totalCap = accts.reduce((s, a) => s + (a.cap_week || 0), 0);
  const totalLeft = Math.max(0, totalCap - totalBurned);
  const totalPct = totalCap ? Math.min(100, totalBurned / totalCap * 100) : 0;
  // 最高风险账号：周烧比例最高的那个
  const riskAcct = accts.map(a => ({
    name: sName(a.name), pct: a.cap_week ? a.burned_week / a.cap_week * 100 : 0,
    left: a.left_week || 0, parked: a.parked, absolute_capped: a.absolute_capped,
  })).sort((a, b) => b.pct - a.pct)[0];
  const riskTone = riskAcct && riskAcct.pct >= 95 ? "err" : riskAcct && riskAcct.pct >= 80 ? "warn" : "ok";
  const riskText = riskAcct
    ? `${riskAcct.name} ${riskAcct.pct.toFixed(0)}%`
      + (riskAcct.pct >= 95 ? ` · 仅剩 ${num(Math.round(riskAcct.left))}` : "")
      + (riskAcct.parked ? " · 停靠中" : "")
    : "—";
  // 安全系数风险说明：0.95 = 5% 缓冲，说人话
  const sm = L.safety_margin || 0;
  const smTone = sm >= 0.95 ? "err" : sm >= 0.85 ? "warn" : "ok";
  const smSub = sm >= 0.95
    ? `缓冲仅 ${(1 - sm) * 100 | 0}%（${num(Math.round(600000 * (1 - sm)))} 积分），费率偏低就会烧穿`
    : sm >= 0.85
      ? `缓冲 ${(1 - sm) * 100 | 0}%（${num(Math.round(600000 * (1 - sm)))} 积分），正常`
      : `缓冲充足（${(1 - sm) * 100 | 0}%）`;
  const rows = (d.accounts || []).map(a => {
    const pct5 = a.cap_5h ? Math.min(100, a.burned_5h / a.cap_5h * 100) : 0;
    const pctW = a.cap_week ? Math.min(100, a.burned_week / a.cap_week * 100) : 0;
    const bW = a.next_week_boundary ? cgBoundaryText(a.next_week_boundary) : "—";
    const waTs = a.week_anchor_ts || 0;
    const waInit = waTs ? cgBoundaryText(waTs) : "";
    const tag = a.absolute_capped ? `<span class="tag is-err">已达上限</span>`
      : a.parked ? `<span class="tag is-warn">停靠</span>`
      : a.anchored_5h ? `<span class="tag is-ok">锚点</span>`
      : `<span class="tag">滚动</span>`;
    const rowCls = pctW >= 90 ? ' class="is-err"' : pctW >= 75 ? ' class="is-warn"' : '';
    const burning = a.parked ? "" : "checked";
    return `<tr${rowCls} data-name="${esc(a.name)}">
      <td><b title="${esc(a.name)}">${esc(sName(a.name))}</b><div class="muted small">${tag}</div></td>
      <td><div class="row-flex"><div class="bar" style="width:${Math.max(4, pct5 * 1.2)}px"><i></i></div></div>
        <input class="rec-in" data-f="left_5h" type="number" step="any" min="0"
          value="${num(Math.round(a.left_5h))}" title="剩余 ${Math.round(a.left_5h)} / 熔断 ${Math.round(a.cap_5h)}；填 0 = 停止该账号"
          style="width:110px;font-size:var(--fs-12)">
        <div class="muted small">/ ${num(Math.round(a.cap_5h))} · ≈${num(a.requests_left_5h)} 条</div></td>
      <td><div class="row-flex"><div class="bar" style="width:${Math.max(4, pctW * 1.2)}px"><i></i></div></div>
        <input class="rec-in" data-f="left_week" type="number" step="any" min="0"
          value="${num(Math.round(a.left_week))}" title="剩余 ${Math.round(a.left_week)} / 熔断 ${Math.round(a.cap_week)}；填 0 = 停止该账号"
          style="width:110px;font-size:var(--fs-12)">
        <div class="muted small">/ ${num(Math.round(a.cap_week))}</div></td>
      <td><input class="rec-in" data-f="week_anchor" type="text"
          value="${esc(waInit)}" placeholder="（全局）"
          title="周锚点：照抄商汤控制台的「周刷新」列，填 10月14日 18:10 或 10-14 18:10；留空 = 用全局锚点"
          style="width:120px;font-size:var(--fs-12)">
        <div class="muted small" title="下次重置时间（只读，由锚点算出）">下次重置 ${bW}</div></td>
      <td class="small">目标 ${num(a.target)} · 成功 ${num(a.ok)}
        · <span title="与同实例其他账号抢每分钟配额，窗口滑过就恢复">限流 ${num(a.freq_hits ?? 0)}</span>
        · <span title="专属池+通用池都扣完了，停靠到周刷新">额度用尽 ${num(a.quota_hits ?? 0)}</span>
        ${a.starved ? '<span class="tag is-warn">已救济</span>' : ''}
        ${a.rate_parked ? `<span class="tag is-err" title="连续 ${a.rate_streak} 次 429，停手到 ${new Date(a.rate_park_until * 1000).toLocaleString('zh-CN', {hour12:false})}；不是号坏了，到期自动重试">限流停靠</span>` : ''}
        <div class="muted small">tokens ${fmtK(a.tokens_in)} / ${fmtK(a.tokens_out)}</div>
        <label class="row-flex small" style="gap:4px;margin-top:4px"><input type="checkbox" class="rec-in" data-f="burning" ${burning}
          title="取消勾选 = 这个账号停靠一个窗口周期"> 参与</label></td></tr>`;
  }).join("");
  const fields = burnerFields(d.form_fields || [], d.config || {});
  $("#view-burner").innerHTML = `
    <div class="view-head">
      <h2>积分消耗器</h2>
      <span class="desc">后台烧商汤 Flash-Lite 专属池积分（1:1 折算成 kimi-k3 可用积分）。
        配置在 <code>config/burner.yaml</code>，改完<strong>重启消耗器</strong>才生效</span>
    </div>
    ${warn ? `<div class="attention">${warn}</div>` : ""}
    <div class="grid kpis">
      ${kpi("本周剩余", `${num(Math.round(totalLeft))}<span class="muted small"> / ${num(Math.round(totalCap))}</span>`,
        `已烧 ${num(Math.round(totalBurned))} 积分（${totalPct.toFixed(0)}%）· ${num(accts.length)} 个账号 · 更新于 ${esc(L.saved_at || "—")}`,
        totalPct >= 80 ? "err" : totalPct >= 50 ? "warn" : "ok")}
      ${kpi("最高风险账号", riskText,
        riskAcct && riskAcct.pct >= 95 ? "已逼近熔断线，专属池可能溢出扣通用池" : "周烧比例最高的账号",
        riskTone)}
      ${kpi("单条请求", cost + " 积分", "按当前填充量与输出上限估算")}
      ${kpi("费率", `入${num(L.rate_in)} / 出${num(L.rate_out)}`, "积分每百万 token；账本口径")}
      ${kpi("安全系数", sm, smSub, smTone)}
    </div>
    <h4 class="sec">每账号窗口账</h4>
    <div class="row-flex" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">
      <span class="tag">限流＝挤不进每分钟配额，窗口滑过自恢复</span>
      <span class="tag is-warn">额度用尽＝积分烧完，停靠到周刷新</span>
      <span class="tag is-err">限流停靠＝连续 429 主动停手，到点自动重试</span>
      <details class="small" style="flex:1 1 100%"><summary style="cursor:pointer" class="muted">这三种状态详细说明 / 周锚点怎么填</summary>
        <div class="note" style="margin-top:6px">商汤把两种完全不同的情况都返回 <code>429</code>：
          <b>限流</b>＝<code>tpm exhausted</code>，跟同实例其他账号抢每分钟配额，<b>不是号坏了</b>；
          <b>额度用尽</b>＝<code>entitlement exhausted</code>，专属池+通用池都扣完了。
          看到某个号「一直限流」，是它在饿——网关会给饿超过 5 分钟的号临时借一点并发
          （显示「已救济」），成功一次就收回。「<b>限流停靠</b>」是连续撞 limit 满阈值后主动停手，
          省下每次冷却期满去撞一次的纯空转。
          <br><br><b>周锚点（下次重置）</b>：直接在表格「下次重置」列填 <code>月-日 时刻</code>
          （例 <code>10-14 18:10</code> 或 <code>10月14日 18:10</code>）——
          <b>照抄商汤控制台的「周刷新」那一列</b>，不用自己算星期几。留空 = 用全局锚点。</div>
      </details>
    </div>
    <div class="tablewrap"><table><thead><tr><th>账号</th><th>5h 剩余</th><th>本周剩余</th>
      <th>下次重置</th><th>状态 / 参与</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="muted">暂无数据（消耗器可能还没跑过）</td></tr>'}</tbody></table></div>
    <div class="row-flex" style="gap:10px;align-items:center;margin:10px 0;flex-wrap:wrap">
      <button class="mini" id="rec-btn-diff">预演改动</button>
      <button class="primary mini" id="rec-btn-apply">保存改动并重启</button>
      <span class="muted small" id="rec-hint">改剩余 = 校准账本；填 0 = 停止该账号；改下次重置 = 对齐官网</span>
      <span class="muted small" id="rec-diff" style="display:none"></span>
    </div>
    <h4 class="sec">实时日志</h4>
    <div class="note">消耗器每分钟落一条汇总；异常与限流会立刻出现。
      断线会自动重连（指数退避），你也可以暂停滚动去翻历史。</div>
    <div class="card" style="padding:0">
      <div class="row-flex" style="gap:8px;padding:10px 12px;border-bottom:1px solid var(--border);align-items:center;flex-wrap:wrap">
        <span class="dot off" id="bl-dot"></span>
        <span class="small" id="bl-state">未连接</span>
        <span class="grow"></span>
        <button class="mini" id="bl-follow" title="关掉后新日志不会自动滚到底">⏸ 暂停滚动</button>
        <button class="mini" id="bl-copy">📋 复制</button>
        <button class="mini" id="bl-clear">🧹 清屏</button>
      </div>
      <pre id="bl-log" style="margin:0;padding:12px;height:280px;overflow:auto;font-size:var(--fs-12);line-height:1.55;white-space:pre-wrap;word-break:break-all"></pre>
    </div>

    <details class="card" style="margin-top:14px" open>
      <summary style="cursor:pointer;font-weight:600;padding:2px 0">🔧 费率校准 & 只烧这些 Key</summary>
      <div class="note" style="margin-top:8px">账本的「已烧」是按费率<b>估算</b>的，商汤后台的「实扣」才是真的。
        把后台某段时段的实扣积分填进来，按账本同期 token 反推费率。
        <b>时段必须对齐</b>：账本记的是「累计」，实扣也必须是同样范围的累计。</div>
      <div class="formgrid" style="margin-top:10px">
        <label>实扣积分（累计）<input id="cal-actual" type="number" step="any" min="0" placeholder="如 120000"></label>
        <label>只算这个账号（可空）<input id="cal-account" placeholder="如 S_02（留空算全部）"></label>
      </div>
      <div class="modal-actions">
        <button id="cal-btn">算出建议费率</button>
        <button class="primary" id="cal-adopt" style="display:none">采纳并重启</button>
        <span class="muted small" id="cal-out"></span>
      </div>
      <div class="small" style="margin:14px 0 4px;font-weight:600">只烧这些 Key（only）<span class="muted">—— 取消勾选 = 不烧该账号</span></div>
      <div id="rec-only" class="row-flex" style="flex-wrap:wrap;gap:8px"></div>
    </details>

    <details class="card" style="margin-top:14px">
      <summary style="cursor:pointer;font-weight:600;padding:2px 0">⚙ 高级配置（27 项，默认收起）</summary>
      <div class="note" style="margin-top:10px">改完点保存会写入 <code>config/burner.yaml</code> 并重启消耗器生效。
        热更键（并发 / 费率 / 预算 / 锚点）立即生效，重启键（model / only / account_groups / 窗口锚点）要重启才生效——
        页面里改这两者走的是「账号核对」流程，不是这里。</div>
      <div class="cfg-grid" style="margin-top:12px">${fields}</div>
      <div class="modal-actions">
        <button id="bf-save" class="primary">保存并重启</button>
        <span class="muted small">写入 config/burner.yaml（保留注释），并让托盘重启消耗器使配置生效（约 3 秒）</span>
      </div>
    </details>`;
  $("#bf-save").onclick = saveBurnerConfig;
  $("#cal-btn").onclick = calRun;
  // 日志 SSE 只连一次：loadBurner 会被自动刷新反复调用，每次重连会把
  // 历史重发一遍并让滚动位置乱跳。
  if (!burnLog.es && !burnLog.timer) blWire();
  if (!recState) loadReconcile();
}

/* ================= 消耗器：实时日志（SSE + 降级轮询） ================= */

function blSetState(text, ok) {
  const dot = $("#bl-dot"), st = $("#bl-state");
  if (!dot || !st) return;
  dot.className = "dot " + (ok ? "on" : "off");
  st.textContent = text;
}

function blAppend(line) {
  const box = $("#bl-log");
  if (!box) return;
  box.textContent += (box.textContent ? "\n" : "") + line;
  // 只保留尾部 2000 行：日志一晚上能长到几万行，全留着会把浏览器拖死
  const lines = box.textContent.split("\n");
  if (lines.length > 2000) box.textContent = lines.slice(-2000).join("\n");
  if (burnLog.follow) box.scrollTop = box.scrollHeight;
}

function blConnect() {
  if (burnLog.es) { burnLog.es.close(); burnLog.es = null; }
  blSetState("连接中…", false);
  // 退化路径：老浏览器没有 EventSource 时直接轮询，别让日志区永远空着
  if (typeof EventSource === "undefined") { blFallback(); return; }
  let es;
  try {
    es = new EventSource("/admin/burner/log/stream?lines=120&within_seconds=86400");
  } catch {
    blFallback();
    return;
  }
  burnLog.es = es;
  es.onopen = () => { burnLog.retries = 0; blSetState("实时跟随中", true); };
  es.onmessage = ev => { if (ev.data) blAppend(ev.data); };
  es.onerror = () => {
    es.close();
    burnLog.es = null;
    // 指数退避重连：1s、2s、4s…封顶 30s。代理把 SSE 挡成一次性返回时
    // onerror 会立刻触发，退避避免把服务器打到 100% CPU。
    const wait = Math.min(30000, 1000 * 2 ** burnLog.retries);
    burnLog.retries += 1;
    // 同一地址重试两次仍失败：多半是代理不支持 SSE，转轮询而不是死磕
    if (burnLog.retries >= 2) { blFallback(); return; }
    blSetState(`已断开，${wait / 1000 | 0} 秒后重连（第 ${burnLog.retries} 次）`, false);
    clearTimeout(burnLog.timer);
    burnLog.timer = setTimeout(blConnect, wait);
  };
}

function blFallback() {
  // EventSource 整个不可用（老浏览器 / 代理把 SSE 挡死）：退回 2 秒轮询一次。
  blSetState("SSE 不可用，降级轮询中", false);
  clearInterval(burnLog.timer);
  burnLog.timer = setInterval(async () => {
    try {
      const r = await fetch("/admin/burner/log/tail?lines=30", {
        headers: state.token ? { "X-Admin-Token": state.token } : {},
      });
      if (!r.ok) { blSetState("日志读取失败 HTTP " + r.status, false); return; }
      const text = await r.text();
      const lines = text.trimEnd().split("\n");
      const last = lines[lines.length - 1];
      if (last && last !== blFallback.last) { blAppend(last); blFallback.last = last; }
      blSetState("轮询中（最后一行）", true);
    } catch { blSetState("读取失败，重试中", false); }
  }, 2000);
}

function blStop() {
  if (burnLog.es) { burnLog.es.close(); burnLog.es = null; }
  clearTimeout(burnLog.timer);
  clearInterval(burnLog.timer);
  blSetState("已停止", false);
}

function blWire() {
  const followBtn = $("#bl-follow");
  if (followBtn) followBtn.onclick = () => {
    burnLog.follow = !burnLog.follow;
    followBtn.textContent = burnLog.follow ? "⏸ 暂停滚动" : "▶ 恢复滚动";
    if (burnLog.follow) { const b = $("#bl-log"); if (b) b.scrollTop = b.scrollHeight; }
  };
  const copyBtn = $("#bl-copy");
  if (copyBtn) copyBtn.onclick = () =>
    cgCopy($("#bl-log").textContent, "日志已复制");
  const clearBtn = $("#bl-clear");
  if (clearBtn) clearBtn.onclick = () => { $("#bl-log").textContent = ""; };
  blConnect();
}
/* ================= 消耗器：账号核对与费率校准 =================
 * 表单值只在内存里；「预演」把表单 POST 给 /admin/burner/reconcile，
 * 拿到逐账号 diff 后弹确认框，再 PUT 落盘并重启。
 * 这样运营者改错一个数字能在写盘前看见后果——账本写错不会自愈。
 * ==================================================================== */
let recState = null;

function recNum(v) { return v === null || v === undefined || v === "" ? "" : String(v); }

async function loadReconcile() {
  // 表格行由 loadBurner 用 snapshot 数据渲染（可编辑 input 已内联），
  // 这里只负责加载 only 复选框列表 + 绑定保存/预演按钮。
  // 消耗器没跑时账本文件还在，snapshot 和 reconcile 都能正常返回数据。
  try {
    recState = await api("/admin/burner/reconcile");
    const only = recState.config || {};
    const onlyEl = $("#rec-only");
    if (onlyEl) onlyEl.innerHTML = (only.only || []).map(k =>
      `<label class="row-flex small" style="gap:4px" title="${esc(k)}"><input type="checkbox" class="rec-only" value="${esc(k)}" checked> ${esc(sName(k))}</label>`
    ).join("") || '<span class="muted small">没有可选的 Key</span>';
    const hint = $("#rec-hint");
    if (hint) hint.textContent = recState.ledger_saved_at
      ? `账本更新于 ${recState.ledger_saved_at}`
      : "账本还没有数据";
    const bd = $("#rec-btn-diff"), ba = $("#rec-btn-apply");
    if (bd) bd.onclick = recDiff;
    if (ba) ba.onclick = recApply;
  } catch (err) {
    const hint = $("#rec-hint");
    if (hint) hint.textContent = "读取账本失败: " + err.message;
  }
}

function recCollect() {
  // 从主表格（#view-burner 内的 table）收集每行可编辑字段
  const accounts = [];
  document.querySelectorAll("#view-burner table tbody tr[data-name]").forEach(tr => {
    const item = { name: tr.dataset.name };
    tr.querySelectorAll(".rec-in").forEach(el => {
      const f = el.dataset.f;
      if (el.type === "checkbox") item[f] = el.checked;
      else item[f] = el.value.trim();
    });
    accounts.push(item);
  });
  const only = [];
  document.querySelectorAll(".rec-only").forEach(el => { if (el.checked) only.push(el.value); });
  const patch = { accounts };
  if (only.length) patch.only = only;
  return patch;
}

function recDiff() {
  recPreview(recCollect());
}

async function recPreview(patch) {
  try {
    const d = await api("/admin/burner/reconcile", { method: "POST", json: patch });
    const rows = (d.accounts || []).filter(a =>
      ["credits_total", "left_5h", "left_week"].some(f => a[f] && a[f].delta !== null && a[f].delta !== undefined)
      || (a.anchor && a.anchor.filled && a.anchor.filled !== a.anchor.current)
      || (a.week_anchor && a.week_anchor.filled && a.week_anchor.filled !== a.week_anchor.current)
      || a.burning === false);
    let html = "";
    if (!rows.length) {
      html = '<div class="small muted">没有改动。</div>';
    } else {
      html = '<table style="width:100%;font-size:var(--fs-12);border-collapse:collapse"><tr class="small muted">'
        + "<td>账号</td><td>字段</td><td>账本</td><td>你填的</td><td>差</td></tr>"
        + rows.map(a => {
          const cells = [];
          const push = (label, o) => cells.push(`<tr><td title="${esc(a.name)}">${esc(sName(a.name))}</td><td>${label}</td>`
            + `<td class="muted">${esc(String(o.current ?? ""))}</td>`
            + `<td><strong>${esc(String(o.filled ?? o.current ?? ""))}</strong></td>`
            + `<td style="color:${o.notable ? "var(--err)" : "inherit"}">${o.delta ?? ""}</td></tr>`);
          ["credits_total", "left_5h", "left_week"].forEach(f => {
            if (a[f] && a[f].delta !== null && a[f].delta !== undefined) push(f, a[f]);
          });
          if (a.anchor && a.anchor.filled && a.anchor.filled !== a.anchor.current) push("锚点", a.anchor);
          if (a.week_anchor && a.week_anchor.filled && a.week_anchor.filled !== a.week_anchor.current) push("周锚点", a.week_anchor);
          if (a.burning === false) cells.push(`<tr><td title="${esc(a.name)}">${esc(sName(a.name))}</td><td>参与</td><td class="muted">烧</td><td><strong>停靠</strong></td><td></td></tr>`);
          return cells.join("");
        }).join("") + "</table>";
    }
    const cfg = d.config_changes || {};
    const cfgKeys = Object.keys(cfg);
    if (cfgKeys.length) {
      html += `<div class="small" style="margin-top:8px">配置改动：${cfgKeys.map(k =>
        `<code>${esc(k)}</code>`).join("、")}`
        + (d.restart_keys && d.restart_keys.length
          ? `　<span style="color:var(--warn)">（${d.restart_keys.map(esc).join("、")} 需要重启才生效）</span>` : "") + "</div>";
    }
    $("#rec-diff").innerHTML = html;
    $("#rec-diff").style.display = "";
    return d;
  } catch (err) { toast(err.message, "err"); return null; }
}

async function recApply() {
  const patch = recCollect();
  const d = await recPreview(patch);
  if (!d) return;
  const cfgKeys = Object.keys(d.config_changes || {});
  const willRestart = cfgKeys.length > 0;
  if (!confirm(`确认保存？\n`
    + `将写入消耗器账本${cfgKeys.length ? "和 burner.yaml（" + cfgKeys.join("、") + "）" : ""}\n`
    + (willRestart ? "改的是 only/anchors 这类键，保存后会自动重启消耗器（正在飞的两三秒请求会被断）。\n" : "")
    + "\n按「确定」继续。")) return;
  try {
    const r = await api("/admin/burner/reconcile?restart=true", { method: "PUT", json: patch });
    const n = (r.reconcile.ledger_written || []).length;
    const m = (r.reconcile.config_written || []).length;
    const rs = r.restart || {};
    toast(`已保存：账本 ${n} 处、配置 ${m} 处；${rs.message || ""}`, rs.ok ? "ok" : "err");
    if (!rs.ok && rs.command) toast("请手动执行：" + rs.command, "err");
    await loadBurner();
    setTimeout(loadBurner, 4000);
  } catch (err) { toast(err.message, "err"); }
}

async function calRun() {
  const actual = Number($("#cal-actual").value);
  const account = $("#cal-account").value.trim();
  if (!actual) { toast("先填实扣积分", "err"); return; }
  $("#cal-out").textContent = "计算中…";
  $("#cal-adopt").style.display = "none";
  try {
    const r = await api("/admin/burner/calibrate", {
      method: "POST", json: { actual_credits: actual, account },
    });
    const c = r.calibration;
    $("#cal-out").innerHTML =
      `建议 <code>入 ${c.suggested.rate_in} / 出 ${c.suggested.rate_out}</code>`
      + `　当前账本 <code>入 ${c.current.rate_in} / 出 ${c.current.rate_out}</code>`
      + `　账本 token 入 ${Math.round(c.ledger_tokens.tokens_in / 1e3)}K / 出 ${Math.round(c.ledger_tokens.tokens_out / 1e3)}K`
      + `<div class="muted small" style="margin-top:4px">${esc(c.caveat)}</div>`;
    $("#cal-adopt").style.display = "";
    $("#cal-adopt").onclick = () => calAdopt(actual, account);
  } catch (err) {
    $("#cal-out").textContent = "";
    toast(err.message, "err");
  }
}

async function calAdopt(actual, account) {
  if (!confirm(`采纳建议费率并写入 burner.yaml + 账本，然后重启消耗器？\n`
    + "重启只影响正在飞的两三秒请求。")) return;
  try {
    const r = await api("/admin/burner/calibrate", {
      method: "POST", json: { actual_credits: actual, account, adopt: true },
    });
    const rs = r.calibration.restart || {};
    toast(`已采纳：入 ${r.calibration.suggested.rate_in} / 出 ${r.calibration.suggested.rate_out}；${rs.message || ""}`,
      rs.ok ? "ok" : "err");
    await loadBurner();
    setTimeout(loadBurner, 4000);
  } catch (err) { toast(err.message, "err"); }
}
async function saveBurnerConfig() {
  const patch = {};
  document.querySelectorAll("#view-burner [id^='bf-']").forEach(el => {
    const key = el.id.slice(3);
    let v = el.value;
    if (BURNER_NUM.has(key)) { v = v.trim() === "" ? 0 : Number(v); if (Number.isNaN(v)) return toast(key + " 不是数字", "err"); }
    patch[key] = v;
  });
  const btn = $("#bf-save");
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    // restart=true：消耗器只在自己启动时读配置，不重启这次保存就不生效。
    // 重启由托盘心跳异步完成（约 3s），这里只承诺「已请求」。
    await api("/admin/burner/config?restart=true", { method: "PUT", body: JSON.stringify(patch) });
    toast("已保存，正在重启消耗器（约 3 秒后生效）", "ok");
    await loadBurner();
    setTimeout(loadBurner, 4000);
  } catch (e) { toast(e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = "保存并重启"; }
}

/* ================= 体检视图（原 report.html，2026-10-07 改为内置） =================
   把网关自己的库翻译成运营者看得懂的「谁在干活、慢在哪、为什么派给它」。
   数据来自 /health + /admin/stats + /admin/requests，全程只读。
   实时区 10 秒一刷（只读 /health），历史区切天数时才重拉。 */
const reportState = { timer: null, days: "7" };

function reportStop() {
  if (reportState.timer) { clearInterval(reportState.timer); reportState.timer = null; }
}

function reportMs(ms) {
  if (ms >= 60000) return (ms / 60000).toFixed(1) + " 分";
  if (ms >= 1000) return (ms / 1000).toFixed(1) + " 秒";
  return Math.round(ms) + " 毫秒";
}
function reportDur(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s >= 3600) return (s / 3600).toFixed(1) + " 小时";
  if (s >= 60) return Math.round(s / 60) + " 分";
  return s + " 秒";
}
function reportAgoSec(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s >= 86400) return Math.round(s / 86400) + " 天前";
  if (s >= 3600) return Math.round(s / 3600) + " 小时前";
  if (s >= 60) return Math.round(s / 60) + " 分钟前";
  return s + " 秒前";
}

/* 模型 -> 人话标签：帮非程序员认出谁是谁、谁快谁慢 */
function reportLabel(model) {
  if (/kimi-k3/i.test(model)) return 'Kimi K3<span class="tag k">旗舰·慢但强</span>';
  if (/glm-5\.3-flash/i.test(model)) return 'GLM-5.3 Flash<span class="tag f">快·便宜</span>';
  if (/glm-5\.3/i.test(model)) return 'GLM-5.3<span class="tag f">快·便宜</span>';
  if (/step-5/i.test(model)) return 'Step 5<span class="tag f">快·便宜</span>';
  if (/deepseek/i.test(model)) return 'DeepSeek V4<span class="tag f">快·便宜</span>';
  if (/flash-lite|qwen|nemotron/i.test(model)) return '轻量模型<span class="tag f">快·便宜</span>';
  return esc(model);
}

async function loadReport() {
  $("#view-report").innerHTML = `
    <div class="view-head">
      <h2>体检</h2>
      <span class="desc">一眼看穿：请求慢在哪、派给了谁、为什么派给它。数据来自网关自己的库，全程只读。</span>
    </div>
    <div class="report-live" id="report-live">
      <div class="report-live-head">
        <span class="report-live-dot" id="report-live-dot"></span>
        <h2>此刻在跑</h2>
        <span class="report-live-meta" id="report-live-meta">正在读…</span>
      </div>
      <div class="report-live-grid" id="report-live-grid">
        <div class="report-live-cell"><div class="l">读取中</div></div>
      </div>
      <div class="report-alerts" id="report-alerts"></div>
    </div>
    <div class="card" style="padding:12px 16px">
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <span class="small muted" style="margin:0">历史分析范围</span>
        <label style="display:flex;align-items:center;gap:6px">
          <select id="report-days"><option>1</option><option selected>7</option><option>30</option></select>
          天
        </label>
        <button id="report-go">刷新</button>
        <span class="small muted" style="margin:0">页面打开时已自动跑过一次</span>
      </div>
    </div>
    <div id="report-out"></div>`;

  $("#report-go").onclick = reportLoadHistory;
  $("#report-days").onchange = reportLoadHistory;

  await reportLoadLive();
  await reportLoadHistory();
  // loadReport 会被自动刷新反复调用——定时器只设一次
  if (!reportState.timer) {
    reportState.timer = setInterval(reportLoadLive, 10000);
  }
}

async function reportLoadLive() {
  try {
    // /health 不需要 token，直接 fetch
    const h = await fetch("/health").then(r => r.json());
    reportRenderLive(h);
  } catch (e) {
    const grid = $("#report-live-grid");
    if (grid) grid.innerHTML =
      `<div class="report-live-cell err"><div class="l">实时状态</div>
       <div class="v" style="font-size:13px">读不到 /health</div>
       <div class="s">${esc(e.message)}</div></div>`;
  }
}

function reportRenderLive(h) {
  const c = h.credentials || {};
  const q = h.quarantined_deployments || {};
  const qids = Object.keys(q);
  const cfg = h.config || {};
  const gaps = cfg.credential_env_gaps || {};
  const gapIds = Object.keys(gaps);
  const stale = cfg.stale_files || [];
  const warns = cfg.warnings || [];
  const cw = h.config_watch || {};
  const byStatus = c.by_status || {};
  const total = c.requests || 0;
  const succ = c.success || 0;
  const fail = c.failure || 0;
  const rl = c.rate_limits || 0;
  const idle = Math.max(0, total - succ - fail - rl);
  const rate = succ / Math.max(1, total);
  const providers = h.providers || {};

  const busy = total > 0;
  const dot = $("#report-live-dot");
  if (dot) dot.className = "report-live-dot" + (busy ? " busy" : " paused");
  const meta = $("#report-live-meta");
  if (meta) meta.textContent =
    `已运行 ${reportDur(h.uptime_seconds)} · 本进程累计 ${total} 次调用 · 更新于 ` +
    new Date().toLocaleTimeString("zh-CN", { hour12: false });

  const cell = (cls, l, v, s) =>
    `<div class="report-live-cell ${cls}"><div class="l">${l}</div>
     <div class="v">${v}</div><div class="s">${s}</div></div>`;
  const pct = x => (x / Math.max(1, total) * 100).toFixed(1) + "%";

  const grid = $("#report-live-grid");
  if (grid) grid.innerHTML = [
    cell("", "本进程成功率", (rate * 100).toFixed(0) + "%",
      `${succ} 成 / ${fail} 败 / ${rl} 限流`),
    cell("", "凭据池", `${byStatus.healthy || 0} 把在线`,
      `${c.total || 0} 把共 ${byStatus.cooldown || 0} 冷却`),
    cell("", "供应商", `${providers.available || 0}/${providers.total || 0} 可用`,
      `${h.models || 0} 个模型 · ${(h.aliases || []).length} 个别名`),
    cell("", "自动隔离", qids.length ? `${qids.length} 个部署` : "无",
      qids.length ? "连续失败被临时屏蔽" : "所有部署可路由"),
    `<div class="report-live-cell"><div class="l">本进程调用构成</div>
     <div class="report-rate">
      <i class="r-ok" style="width:${pct(succ)}" title="成功 ${pct(succ)}"></i>
      <i class="r-rl" style="width:${pct(rl)}" title="限流 ${pct(rl)}"></i>
      <i class="r-err" style="width:${pct(fail)}" title="失败 ${pct(fail)}"></i>
      <i class="r-idle" style="width:${pct(idle)}" title="其它 ${pct(idle)}"></i>
     </div>
     <div class="s">绿=成功 黄=限流 红=失败 灰=其它（流式中 / 未记账）</div></div>`,
  ].join("");

  // 告警
  const A = [];
  for (const id of qids) {
    const x = q[id];
    A.push({ t: "err",
      m: `<b>${esc(id)}</b> 被自动隔离（连续 ${x.consecutive_failures} 次 ` +
         `<code>${esc(x.last_error_type || "失败")}</code>），还有 <b>${reportDur(x.quarantine_seconds_left)}</b> 解除。` +
         `期间没有任何请求会去碰它——这就是省下每次 60 秒超时等待的那道闸。` });
  }
  for (const env of gapIds) {
    A.push({ t: "warn",
      m: `凭据 <b>${esc(gaps[env].join("、"))}</b> 找不到环境变量 <code>${esc(env)}</code>——` +
         `这把 Key 现在调不动，网关会跳过它。去 <code>.env</code> 补上，` +
         `或从控制台「凭据池」改用它已有的变量名。` });
  }
  for (const f of stale) {
    A.push({ t: "warn",
      m: `配置文件 <code>${esc(f)}</code> 被外部改过但网关还没 reload，现在用的是内存里的旧值。` +
         `控制台存一次配置会报 409 并提示先 reload。` });
  }
  if (cw.last_error) {
    A.push({ t: "err", m: `配置监听出错：<code>${esc(cw.last_error)}</code>（改 YAML 不会自动生效）` });
  } else if (cw.enabled) {
    A.push({ t: "warn", m: `配置热监听开着，最近一次 reload 在 ${reportAgoSec(cw.last_reload_seconds_ago)}。` });
  }
  for (const w of warns.slice(0, 3)) {
    A.push({ t: "warn", m: esc(typeof w === "string" ? w : JSON.stringify(w)) });
  }
  const alertsEl = $("#report-alerts");
  if (alertsEl) alertsEl.innerHTML = A.slice(0, 6).map(a =>
    `<div class="report-alert ${a.t}">${a.m}</div>`).join("") ||
    `<div class="report-alert warn" style="border-left-color:var(--ok);background:var(--ok-soft);color:var(--ok)">` +
    `没有告警：无隔离部署、无凭据缺口、配置已同步。</div>`;
}

async function reportLoadHistory() {
  const days = ($("#report-days") || {}).value || reportState.days;
  reportState.days = days;
  const out = $("#report-out");
  if (!out) return;
  out.innerHTML = '<div class="card">正在读网关数据…</div>';
  try {
    // 不按成功状态过滤：失败的请求才是体检要看的东西。
    const [stats, reqs] = await Promise.all([
      api(`/admin/stats?days=${days}&recent=0`),
      api(`/admin/requests?limit=500`),
    ]);
    reportRender(stats, reqs, days);
  } catch (e) {
    out.innerHTML = `<div class="card"><div class="note warn">
      <b>读不到数据：</b>${esc(e.message)}<br><br>
      最常见的原因：控制台还没登录（点右上角「🔑 令牌」填一次），
      或者网关没起来（双击 <span class="mono">scripts\\start_gateway.cmd</span>）。</div></div>`;
  }
}

function reportRender(stats, reqs, days) {
  const u = stats.usage || {};
  const list = reqs.data || [];
  const ok = list.filter(r => r.status === 'success');
  const bad = list.filter(r => r.status !== 'success');
  const slow = ok.filter(r => r.latency_ms > 30000).length;
  const verySlow = ok.filter(r => r.latency_ms > 60000).length;
  const avgLat = ok.length ? ok.reduce((s, r) => s + (r.latency_ms || 0), 0) / ok.length : 0;
  const maxLat = ok.reduce((m, r) => Math.max(m, r.latency_ms || 0), 0);

  const byModel = {};
  for (const r of list) {
    const k = r.resolved_model || r.requested_model || '?';
    byModel[k] = byModel[k] || {n: 0, tok: 0, lat: 0, max: 0};
    byModel[k].n++; byModel[k].tok += r.input_tokens || 0;
    byModel[k].lat += r.latency_ms || 0;
    byModel[k].max = Math.max(byModel[k].max, r.latency_ms || 0);
  }
  const maxTok = Math.max(1, ...Object.values(byModel).map(m => m.tok));
  const modelRows = Object.entries(byModel).sort((a, b) => b[1].tok - a[1].tok)
    .map(([m, v]) => `<tr>
      <td>${reportLabel(m)}</td><td class="n">${num(v.n)}</td>
      <td class="n">${num(v.tok)}</td>
      <td><div class="report-bar"><i style="width:${v.tok/maxTok*100}%"></i></div></td>
      <td class="n">${reportMs(v.lat/v.n)}</td><td class="n">${reportMs(v.max)}</td>
    </tr>`).join('');

  const slowBig = ok.filter(r => r.latency_ms > 30000 && (r.input_tokens || 0) > 100000).length;
  const slowSmall = ok.filter(r => r.latency_ms > 30000 && (r.input_tokens || 0) <= 100000).length;
  const failCount = bad.length;

  const kpi = (cls, l, v, s) => `<div class="kpi${cls ? " is-" + cls : ""}">
    <div class="k-label">${l}</div><div class="k-value">${v}</div>
    <div class="k-sub">${esc(s || "")}</div></div>`;

  $('report-out').innerHTML = `
  <div class="grid kpis">
    <div class="report-kpis-head">① 这段时间的大脉</div>
    ${kpi("", "请求数（近 " + days + " 天）", num(u.requests), "输入 " + num(u.input_tokens) + " tok")}
    ${kpi("", "平均等待", reportMs(avgLat), "最长 " + reportMs(maxLat))}
    ${kpi(slow > 0 ? "warn" : "ok", "等过 30 秒的", num(slow), "其中 " + num(verySlow) + " 次超过 1 分")}
    ${kpi("", "输出/输入 比",
      u.input_tokens ? (u.input_tokens/Math.max(1,u.output_tokens)).toFixed(0) + " : 1" : "—",
      "越高＝越多 token 花在读历史")}
  </div>

  <div class="card">
    <h4 class="sec">② 谁在干活、慢不慢</h4>
    <table><thead><tr><th>模型</th><th>请求数</th><th>输入 tokens</th>
      <th>占比</th><th>平均等</th><th>最长等</th></tr></thead>
      <tbody>${modelRows || '<tr><td colspan="6" class="muted">暂无数据</td></tr>'}</tbody></table>
    <div class="note">「平均等」是这个模型的真实响应时长。<b>Kimi K3 明显慢，是因为它本来就不是速度杯</b>
      （能力强但 speed 只有 6.0/10）；快的是 GLM / Step / DeepSeek 那几个（9.0+/10）。
      所以「慢」常常不是故障，是它分到了重活。</div>
  </div>

  <div class="card">
    <h4 class="sec">③ 慢的归因，失败另算</h4>
    <div class="note ${slowBig > slowSmall ? 'warn' : 'ok'}">
      最近 ${num(list.length)} 条里，成功的 ${num(ok.length)} 条：<b>${num(slowBig)} 条</b>又大又慢（超 10 万 token），
      <b>${num(slowSmall)} 条</b>不大但慢。<br>
      ${slowBig > slowSmall
        ? '→ <b>慢的主因是请求体量大</b>：一次要把几十万 token 喂进去，任何模型都快不了。这通常说明会话历史太长了——<b>适时开新会话</b>比什么优化都管用。'
        : '→ 慢的主因不在请求大小，更可能是上游网络或模型排队。这类慢，重启网关没有帮助。'}
      ${failCount ? '<br><br>另有 <b>' + num(failCount) + ' 条失败</b>——失败和慢是两回事，成因（限流 / 隔离 / 无凭据）看上方「此刻在跑」的告警。' : ''}
    </div>
    <div class="small muted">逐条明细（含每次尝试的上游错误原文）在<b>控制台「请求记录」页</b>，可按错误类型 / 供应商 / 别名筛，比这里的一屏更细。</div>
  </div>

  <div class="card">
    <h4 class="sec">④ 为什么派给这个模型（规则说人话）</h4>
    <table><thead><tr><th>如果你要…</th><th>网关会派</th><th>为什么</th></tr></thead><tbody>
      <tr><td>带图片的请求</td><td><b>Kimi K3</b> 或 <b>Step 5</b></td>
        <td>只有这两个「看得见图」（vision 能力 ≥8），其它模型结构上不可能入选</td></tr>
      <tr><td>超大请求（≥10 万 token）</td><td><b>GLM-5.3 / Step 5 / DeepSeek</b></td>
        <td>长上下文；K3 唯一的 1M 窗口已经吃满，机械长读该走空闲的链</td></tr>
      <tr><td>带工具、且上下文 ≥2.4 万 token</td><td><b>GLM-5.3 / Step 5 / DeepSeek</b></td>
        <td>重放几十个工具定义 + 几万历史，只为回一句「跑完了」——不需要旗舰推理</td></tr>
      <tr><td>短问短答、或正经推理</td><td><b>Kimi K3</b></td>
        <td>K3 有「接活标准」：请求本身得是强推理（证明 / 为什么 / 根因）才够格接</td></tr>
      <tr><td>客户端直接写 <span class="mono">zk-k3</span></td><td><b>只用 Kimi K3</b></td>
        <td>你明确指定就听你的，网关不插手</td></tr>
    </tbody></table>
    <div class="note ok">这套规则的用意：<b>强的模型只用来干它擅长的活</b>；
      跑命令、读日志、批量改写这类机械活交给又快又不要钱的模型。
      83% 的 token 走的是免费额度（商汤 / NVIDIA / 魔搭）。</div>
  </div>

  <details><summary class="sec" style="cursor:pointer;font-size:var(--fs-13);font-weight:600">⑤ 我想看更技术的（一次请求的完整决策过程）</summary>
    <div class="card" style="margin-top:10px">
      <p class="small muted">在项目目录跑这句，把任意一句话的决策打出来——选了谁、挡了谁、各多少分：</p>
      <pre class="mono">.venv\\Scripts\\python.exe -c "
from app.core.config import get_app_config
from app.models.request import ChatCompletionRequest, ChatMessage
from app.routing.router import Router
q = ChatCompletionRequest(model='zk-auto',
    messages=[ChatMessage(role='user', content='你的问题')])
for c in Router(get_app_config()).plan(q).candidates:
    print(c.model.id, c.provider.id, round(c.score,3),
          'OK' if c.eligible else 'BLOCKED')"
</pre>
      <p class="small muted">真实请求的决策原因，控制台「请求记录」每一行的 routing_reason 里也有。</p>
    </div>
  </details>`;
}
