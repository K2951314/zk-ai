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

  function token() {
    return localStorage.getItem(KEY) || "";
  }

  function setToken(value) {
    localStorage.setItem(KEY, value);
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
      "background:#fff;color:#1a2230;border-radius:12px;padding:18px 20px;" +
      "width:100%;max-width:360px;box-shadow:0 8px 30px rgba(0,0,0,.25);" +
      "font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;";
    box.innerHTML =
      '<div style="font-weight:600;margin-bottom:6px">🔑 需要管理令牌</div>' +
      '<div class="zkui-login-msg" style="color:#5f6b7a;font-size:12.5px;margin-bottom:10px"></div>' +
      '<input type="password" placeholder="粘贴 ZKAI_ADMIN_TOKEN" ' +
      'style="width:100%;box-sizing:border-box;font-size:16px;padding:10px 12px;' +
      'border:1px solid #cfd6df;border-radius:8px;margin-bottom:10px;">' +
      '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button type="button" class="zkui-cancel" ' +
      'style="padding:9px 14px;border:1px solid #cfd6df;background:#fff;border-radius:8px;cursor:pointer;">取消</button>' +
      '<button type="button" class="zkui-save" ' +
      'style="padding:9px 14px;border:0;background:#2563eb;color:#fff;border-radius:8px;cursor:pointer;">保存并重试</button>' +
      "</div>" +
      '<div style="color:#8a94a3;font-size:11.5px;margin-top:8px">令牌只存在本机浏览器，不会发往网关以外的地址。</div>';
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
  };
})();
