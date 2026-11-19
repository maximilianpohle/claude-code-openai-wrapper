"""Self-contained browser page for the Claude login (served at /claude-login)."""

LOGIN_PAGE_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Login</title>
<style>
  :root {
    --bg: #f6f5f2; --card: #ffffff; --text: #1f1e1c; --muted: #6b6862;
    --border: #e3e0da; --accent: #c2410c; --accent-text: #ffffff;
    --ok-bg: #e8f5ec; --ok: #1e6b3a; --err-bg: #fbeaea; --err: #a32020;
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1a1918; --card: #242321; --text: #ece9e4; --muted: #a29e97;
      --border: #3a3835; --accent: #ea7a45; --accent-text: #1a1918;
      --ok-bg: #1c3326; --ok: #8fd6a8; --err-bg: #3a1f1f; --err: #f2a3a3;
      color-scheme: dark;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 3rem 1rem; background: var(--bg); color: var(--text);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  main { max-width: 34rem; margin: 0 auto; }
  h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
  .sub { color: var(--muted); margin: 0 0 1.5rem; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 1.25rem; margin-bottom: 1rem;
  }
  .step { font-size: .8rem; font-weight: 600; letter-spacing: .04em;
          text-transform: uppercase; color: var(--muted); margin: 0 0 .5rem; }
  .card p { margin: 0 0 .9rem; }
  button {
    font: inherit; font-weight: 600; border: 0; border-radius: 7px; cursor: pointer;
    padding: .6rem 1rem; background: var(--accent); color: var(--accent-text);
  }
  button.secondary { background: transparent; color: var(--text);
                     border: 1px solid var(--border); font-weight: 500; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  form { display: flex; gap: .5rem; flex-wrap: wrap; }
  input {
    flex: 1 1 14rem; min-width: 0; font: 14px ui-monospace, Consolas, monospace;
    padding: .6rem .7rem; border-radius: 7px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text);
  }
  #status { display: flex; align-items: center; justify-content: space-between;
            gap: 1rem; flex-wrap: wrap; }
  .dot { display: inline-block; width: .6rem; height: .6rem; border-radius: 50%;
         margin-right: .5rem; background: var(--muted); }
  .dot.on { background: var(--ok); }
  #msg { padding: .7rem .9rem; border-radius: 7px; margin-bottom: 1rem; }
  #msg.ok { background: var(--ok-bg); color: var(--ok); }
  #msg.err { background: var(--err-bg); color: var(--err); }
  #fallback { font-size: .9rem; word-break: break-all; }
  a { color: var(--accent); }
</style>
</head>
<body>
<main>
  <h1>Claude Login</h1>
  <p class="sub">Meldet diesen Wrapper mit deinem Claude-Abo an.</p>

  <div id="msg" hidden></div>

  <div class="card">
    <div id="status"><span><span class="dot" id="dot"></span><span id="status-text">Status wird geladen ...</span></span>
      <button class="secondary" id="logout" hidden>Abmelden</button></div>
  </div>

  <div class="card">
    <p class="step">Schritt 1</p>
    <p>Öffnet die Anmeldung bei Claude in einem neuen Tab.</p>
    <button id="start">Mit Claude anmelden</button>
    <p id="fallback" hidden>Tab nicht geöffnet? <a id="fallback-link" target="_blank" rel="noopener noreferrer">Anmeldelink öffnen</a></p>
  </div>

  <div class="card">
    <p class="step">Schritt 2</p>
    <p>Nach der Anmeldung zeigt Claude einen Code an. Kopiere ihn hierher (innerhalb von 10 Minuten).</p>
    <form id="complete-form">
      <input id="code" placeholder="Code einfügen" autocomplete="off" spellcheck="false" disabled>
      <button id="complete" type="submit" disabled>Abschließen</button>
    </form>
  </div>
</main>
<script>
  const $ = (id) => document.getElementById(id);

  async function api(method, path, body) {
    const options = { method, headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let data = {};
    try { data = await response.json(); } catch (e) { /* empty body */ }
    if (!response.ok) {
      const err = data.error || {};
      throw new Error(err.message || data.detail || "HTTP " + response.status);
    }
    return data;
  }

  function showMessage(text, kind) {
    const msg = $("msg");
    msg.textContent = text;
    msg.className = kind;
    msg.hidden = false;
  }

  function setStep2(enabled) {
    $("code").disabled = !enabled;
    $("complete").disabled = !enabled;
    if (enabled) $("code").focus();
  }

  const SOURCES = {
    api_login: "Angemeldet (über diese Seite)",
    environment: "Angemeldet (Token aus CLAUDE_CODE_OAUTH_TOKEN)",
    none: "Nicht angemeldet",
  };

  async function refreshStatus() {
    try {
      const s = await api("GET", "/v1/claude-login");
      $("status-text").textContent = SOURCES[s.oauth_token_source] || s.oauth_token_source;
      $("dot").classList.toggle("on", s.oauth_token_source !== "none");
      $("logout").hidden = s.oauth_token_source !== "api_login";
      setStep2(s.login_in_progress);
    } catch (e) {
      $("status-text").textContent = "Status nicht abrufbar: " + e.message;
    }
  }

  $("start").addEventListener("click", async () => {
    // Open the tab synchronously in the click handler, otherwise popup blockers interfere
    const tab = window.open("", "_blank");
    if (tab) tab.opener = null;
    $("start").disabled = true;
    $("msg").hidden = true;
    try {
      const { authorize_url } = await api("POST", "/v1/claude-login/start");
      $("fallback-link").href = authorize_url;
      $("fallback").hidden = false;
      if (tab) tab.location.href = authorize_url;
      setStep2(true);
    } catch (e) {
      if (tab) tab.close();
      showMessage("Login konnte nicht gestartet werden: " + e.message, "err");
    } finally {
      $("start").disabled = false;
    }
  });

  $("complete-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = $("code").value.trim();
    if (!code) return;
    $("complete").disabled = true;
    try {
      await api("POST", "/v1/claude-login/complete", { code });
      $("code").value = "";
      $("fallback").hidden = true;
      showMessage("Angemeldet. Der Wrapper nutzt ab sofort dein Claude-Konto.", "ok");
    } catch (e) {
      showMessage(e.message, "err");
    }
    await refreshStatus();
  });

  $("logout").addEventListener("click", async () => {
    try {
      await api("DELETE", "/v1/claude-login");
      showMessage("Abgemeldet.", "ok");
    } catch (e) {
      showMessage("Abmelden fehlgeschlagen: " + e.message, "err");
    }
    await refreshStatus();
  });

  refreshStatus();
</script>
</body>
</html>
"""
