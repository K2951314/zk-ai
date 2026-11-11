/* ZK-AI 独立页共享外壳（2026-11-08 手机远控 M1）。
 *
 * 背景：/ui/agent、/ui/events、/ui/taskspaces、/ui/models 是四个各自为政的
 * 微前端——五份 esc、五份 api、五种令牌处理。手机上从飞书卡片点进来时
 * localStorage 是空的，除了 /ui（console.js 有登录框）以外全是
 * 「HTTP 401」死路，页面上没有任何地方能输入令牌。这个文件把
 * 「令牌管理 + 带 401 识别的 api + 登录兜底」收成一份，各页按需取用。
 *
 * 约定：
 * - 只挂一个全局 ``ZKUI``（顶层 var，classic script 里即 window.ZKUI），
 *   避免与各页顶层声明撞名（顶层 const 撞名 = 整个 <script> 中止，
 *   见 docs/控制台前端约定.md）。行首 var 同时满足
 *   test_console_zh.py 的「每个 script 块必须有顶层声明」守卫。
 * - 令牌只存本机 localStorage，不写进页面 HTML、不进 URL。
 * - 全部面向用户的文案中文（tests/test_console_zh.py 会扫）。
 */
var ZKUI = (function () {
  "use strict";

  var KEY = "zkai_admin_token";

  /* 一次性直登：``?token=xxx`` 存进 localStorage，然后**从地址栏抹掉**。

     2026-11-11 补：原来只有 console.js（``/ui``）认这个参数，四个独立页
     （agent / events / taskspaces / models）全都不认。后果是
     ``deploy-server/README.md`` 教运营者的「打开 /ui/?token=…」在
     飞书卡片深链到 ``/ui/models?token=…`` 时就失效——页面显示
     「管理令牌缺失或无效」，而 URL 里明明带着令牌。

     放在 IIFE 顶部执行（早于各页脚本），这样令牌在第一次请求前就绪。
     用 replaceState 而非 pushState：令牌不该留在浏览器历史里，
     反代日志也已经记过一次了，别再让前进/后退按钮泄露。
  */
  (function consumeTokenFromUrl() {
    try {
      var params = new URLSearchParams(location.search);
      var fromUrl = params.get("token");
      if (!fromUrl) return;
      localStorage.setItem(KEY, fromUrl);
      params.delete("token");
      var query = params.toString();
      history.replaceState(null, "", location.pathname + (query ? "?" + query : "") + location.hash);
    } catch (e) {
      /* 隐私模式 / 老浏览器：忽略，退回 promptLogin 那条路 */
    }
  })();

  function token() {
    return localStorage.getItem(KEY) || "";
  }

  function setToken(value) {
    localStorage.setItem(KEY, value);
  }

  /* HTML 五字符全转义。四个独立页原来各写一份（连同 console.js 共五份），
     拼法还不一样（`s ?? ""` vs `value == null`）。收成一份，
     语义统一为「null/undefined → 空串，其余 String 后转义」。
     ⚠ 只转义这五个字符；拼进 HTML 属性或文本都够用，别拿它当 URL 编码
     （`encodeURIComponent` 是另一回事）。 */
  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/[&<>"']/g, function (ch) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
      });
  }

  /* 一条会自己消失的提示条。
     原来四页四种反馈：events 塞进工具栏的 hint span（把「点一行看详情」覆盖掉）、
     taskspaces 用 window.alert + confirm、models 用整块 .empty.error、
     agent/console 有真 toast。同一次 401 在四个页面有四种样子。
     现在统一成 toast；页面里如果有自己的状态行，那是页面的事，不代表错误反馈。 */
  function toast(message, kind) {
    var host = document.getElementById("zkui-toasts");
    if (!host) {
      host = document.createElement("div");
      host.id = "zkui-toasts";
      host.style.cssText =
        "position:fixed;right:16px;bottom:16px;z-index:10000;" +
        "display:flex;flex-direction:column;gap:8px;pointer-events:none;";
      document.body.appendChild(host);
    }
    var el = document.createElement("div");
    var bg = kind === "ok" ? "var(--ok)" : kind === "err" ? "var(--err)" : "var(--ink-2)";
    el.style.cssText =
      "pointer-events:auto;max-width:min(420px,90vw);padding:9px 14px;border-radius:9px;" +
      "background:" + bg + ";color:var(--bg);font-size:13px;line-height:1.55;" +
      "box-shadow:0 6px 22px rgba(0,0,0,.22);word-break:break-word;";
    el.textContent = String(message === null || message === undefined ? "" : message);
    host.appendChild(el);
    setTimeout(function () { el.remove(); }, 4200);
  }

  /* 今天之内只显时分秒，其它显月日 —— 只显示 10:44:41 让人无法判断是否过期
     （这是控制台一条老教训，收进共享外壳，四个独立页一起受益）。 */
  function fmtTime(iso) {
    if (!iso) return "-";
    var s = String(iso);
    // DB 里存的是「无时区 UTC」，补 Z 再按本地时区展示。
    if (s.indexOf("T") >= 0 && !/([Zz]|[+-]\d\d:?\d\d)$/.test(s)) s += "Z";
    var d = new Date(s);
    if (isNaN(d.getTime())) return "-";
    var p = function (n) { return String(n).padStart(2, "0"); };
    var hm = p(d.getHours()) + ":" + p(d.getMinutes());
    var now = new Date();
    var sameDay = d.getFullYear() === now.getFullYear()
      && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
    return sameDay ? hm + ":" + p(d.getSeconds()) : (d.getMonth() + 1) + "-" + p(d.getDate()) + " " + hm;
  }

  /* epoch 秒 → 本地完整时间串（后端有些列存秒、有些存 ISO 字符串）。 */
  function fmtEpoch(ts) {
    if (ts === null || ts === undefined || ts === "") return "-";
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return "-";
    var p = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
      + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  /* 时长（秒）→ 人话。52 秒 / 3.2 分 / 1.4 小时。 */
  function dur(sec) {
    var s = Math.max(0, Math.round(Number(sec) || 0));
    if (s >= 3600) return (s / 3600).toFixed(1) + " 小时";
    if (s >= 60) return Math.round(s / 60) + " 分 " + (s % 60) + " 秒";
    return s + " 秒";
  }

  /* 主题：iframe 里的子文档拿不到父文档的 data-theme，所以这里按
     「父文档优先 → 本机存储 → 系统偏好」三级回落。
     控制台（console.js）用 documentElement.dataset.theme 切主题，
     而 events.html 原来只靠 @media (prefers-color-scheme) —— 父文档是暗色
     而系统是亮色时，iframe 里的页会亮着，跟周围格格不入（2026-11-10 修）。 */
  function theme() {
    try {
      if (window.parent && window.parent !== window && window.parent.document) {
        var inherited = window.parent.document.documentElement.dataset.theme;
        if (inherited) return inherited;
      }
    } catch (e) { /* 跨域就忽略 */ }
    var own = localStorage.getItem("zkai_theme");
    if (own) return own;
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function authError() {
    var err = new Error("管理令牌缺失或无效");
    err.isAuth = true;
    err.name = "AuthError";
    return err;
  }

  /* 带 401 识别的 fetch：401 抛 isAuth 错误（页面据此弹登录框），
     其它错误先试结构化信封（{"error":{"message":…}}），再退原文。 */
  async function api(path, options) {
    var opts = options ? Object.assign({}, options) : {};
    var headers = Object.assign({}, opts.headers || {});
    var t = token();
    if (t) { headers["X-Admin-Token"] = t; }
    if (opts.body && !headers["Content-Type"]) { headers["Content-Type"] = "application/json"; }
    opts.headers = headers;
    var res = await fetch(path, opts);
    if (res.status === 401) { throw authError(); }
    if (!res.ok) {
      var msg = "请求失败：HTTP " + res.status;
      try {
        var text = await res.text();
        try {
          var body = JSON.parse(text);
          msg = (body && body.error && body.error.message) || body.detail || msg;
        } catch (e) {
          if (text) { msg = String(text).slice(0, 200); }
        }
      } catch (e2) { /* 保底用状态码 */ }
      var err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  /* 登录兜底：手机第一次打开（或令牌换过）时给一条能输入令牌的路。
     幂等：已开着就只更新提示文字并聚焦。保存后整页重载，令牌从此进
     localStorage——与 /ui 控制台同一把钥匙（同一个 storage key）。 */
  function promptLogin(message) {
    var overlay = document.getElementById("zkui-login");
    if (overlay) {
      var old = overlay.querySelector(".zkui-login-msg");
      if (old && message) { old.textContent = message; }
      var oldInput = overlay.querySelector("input");
      if (oldInput) { oldInput.focus(); }
      return;
    }
    overlay = document.createElement("div");
    overlay.id = "zkui-login";
    overlay.style.cssText =
      "position:fixed;inset:0;background:rgba(15,18,24,.55);" +
      "display:flex;align-items:center;justify-content:center;z-index:9999;padding:16px;";
    var box = document.createElement("div");
    box.style.cssText =
      "background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:12px;padding:18px 20px;" +
      "width:100%;max-width:360px;box-shadow:0 8px 30px rgba(0,0,0,.25);" +
      "font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;";
    box.innerHTML =
      '<div style="font-weight:600;margin-bottom:6px">🔑 需要管理令牌</div>' +
      '<div class="zkui-login-msg" style="color:var(--muted);font-size:12.5px;margin-bottom:10px"></div>' +
      '<input type="password" placeholder="粘贴 ZKAI_ADMIN_TOKEN" ' +
      'style="width:100%;box-sizing:border-box;font-size:16px;padding:10px 12px;' +
      'border:1px solid var(--border-strong);background:var(--surface);color:var(--text);border-radius:8px;margin-bottom:10px;">' +
      '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button type="button" class="zkui-cancel" ' +
      'style="padding:9px 14px;border:1px solid var(--border-strong);background:var(--surface);color:var(--text);border-radius:8px;cursor:pointer;">取消</button>' +
      '<button type="button" class="zkui-save" ' +
      'style="padding:9px 14px;border:0;background:var(--accent-solid);color:var(--on-accent);border-radius:8px;cursor:pointer;">保存并重试</button>' +
      "</div>" +
      '<div style="color:var(--muted);font-size:11.5px;margin-top:8px">令牌只存在本机浏览器，不会发往网关以外的地址。</div>';
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    if (message) { box.querySelector(".zkui-login-msg").textContent = message; }
    var input = box.querySelector("input");
    input.focus();
    box.querySelector(".zkui-cancel").addEventListener("click", function () {
      overlay.remove();
    });
    var save = function () {
      var value = input.value.trim();
      if (!value) {
        box.querySelector(".zkui-login-msg").textContent = "令牌不能为空";
        input.focus();
        return;
      }
      setToken(value);
      location.reload();
    };
    box.querySelector(".zkui-save").addEventListener("click", save);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { save(); }
    });
  }

  return {
    token: token,
    setToken: setToken,
    api: api,
    authError: authError,
    promptLogin: promptLogin,
    esc: esc,
    toast: toast,
    fmtTime: fmtTime,
    fmtEpoch: fmtEpoch,
    dur: dur,
    theme: theme,
  };
})();
