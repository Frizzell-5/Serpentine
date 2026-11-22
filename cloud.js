// Serpentine accounts: optional, end-to-end encrypted online storage for projects and saved styles.
//
// How the encryption works (all in this file, in the browser):
//   password + email --PBKDF2 (600,000 rounds)--> 64 bytes
//     first half  = "auth key": sent to the server, which stores only a salted hash of it
//     second half = "wrapping key": never leaves the browser
//   A random 256-bit data key encrypts every project (AES-GCM). The server keeps the data key only in
//   wrapped (encrypted) form: once under the wrapping key, once under a recovery key shown to the user.
// The server therefore stores ciphertext it cannot read. Forget the password and lose the recovery key,
// and the online data is gone for good: nobody, including the site's owner, can reset it.
"use strict";

const Cloud = (() => {
  const MAIN_SITE = "https://serpentine-4qu.pages.dev";
  const KDF_ROUNDS = 600000;
  const te = new TextEncoder();
  const td = new TextDecoder();

  let available = null; // does this server have the accounts API?
  let me = null; // {email, wrapped_dek, usage}
  let dek = null; // CryptoKey (non-extractable)
  let view = "signin";
  let busy = false;
  let notice = "";
  let items = [];
  let usage = null;
  let syncTimer = null;
  let syncing = false;
  let syncAgain = false;
  let lastSynced = null; // JSON of the last version saved, to skip saves when nothing changed
  let status = "off"; // off | saved | saving | pending | error | conflict | locked
  let statusText = "";
  let pendingRecovery = null; // recovery key string to show once
  let stylesTimer = null;
  let shares = [];
  let shareResult = null; // {url, expires_at}
  let shareDays = 30;

  // ---------------------------------------------------------------- bytes
  const toB64 = (bytes) => {
    let s = "";
    const u = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
    return btoa(s);
  };
  const fromB64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const concat = (...arrs) => {
    const out = new Uint8Array(arrs.reduce((n, a) => n + a.length, 0));
    let o = 0;
    for (const a of arrs) { out.set(a, o); o += a.length; }
    return out;
  };
  const sha256 = async (...parts) => new Uint8Array(await crypto.subtle.digest("SHA-256", concat(...parts.map((p) => (typeof p === "string" ? te.encode(p) : p)))));

  // Recovery keys: 32 random bytes as 52 Crockford base32 characters in groups of four
  const B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  function toB32(bytes) {
    let bits = 0, val = 0, out = "";
    for (const b of bytes) {
      val = (val << 8) | b;
      bits += 8;
      while (bits >= 5) { out += B32[(val >>> (bits - 5)) & 31]; bits -= 5; }
    }
    if (bits) out += B32[(val << (5 - bits)) & 31];
    return out.match(/.{1,4}/g).join("-");
  }
  function fromB32(text) {
    const clean = String(text).toUpperCase().replace(/[^0-9A-Z]/g, "").replace(/O/g, "0").replace(/[IL]/g, "1");
    let bits = 0, val = 0;
    const out = [];
    for (const ch of clean) {
      const v = B32.indexOf(ch);
      if (v < 0) throw new Error("bad character");
      val = (val << 5) | v;
      bits += 5;
      if (bits >= 8) { out.push((val >>> (bits - 8)) & 255); bits -= 8; }
    }
    if (out.length < 32) throw new Error("too short");
    return new Uint8Array(out.slice(0, 32));
  }

  // ---------------------------------------------------------------- crypto
  async function deriveFromPassword(email, password) {
    const salt = await sha256("serpentine-v1:" + email.trim().toLowerCase());
    const base = await crypto.subtle.importKey("raw", te.encode(password), "PBKDF2", false, ["deriveBits"]);
    const bits = new Uint8Array(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: KDF_ROUNDS }, base, 512));
    return { auth: toB64(bits.slice(0, 32)), wrap: await crypto.subtle.importKey("raw", bits.slice(32), "AES-GCM", false, ["encrypt", "decrypt"]) };
  }
  async function deriveFromRecovery(rk) {
    return { auth: toB64(await sha256("recovery-auth:", rk)), wrap: await crypto.subtle.importKey("raw", await sha256("recovery-wrap:", rk), "AES-GCM", false, ["encrypt", "decrypt"]) };
  }
  async function seal(key, bytes) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    return concat(iv, new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, bytes)));
  }
  async function open(key, bytes) {
    return new Uint8Array(await crypto.subtle.decrypt({ name: "AES-GCM", iv: bytes.slice(0, 12) }, key, bytes.slice(12)));
  }
  const wrapDek = async (wrapKey, raw) => toB64(await seal(wrapKey, raw));
  const unwrapDek = async (wrapKey, wrapped) => open(wrapKey, fromB64(wrapped)); // throws if the key is wrong
  const importDek = (raw) => crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);

  async function gzip(bytes) {
    if (typeof CompressionStream === "undefined") return concat(new Uint8Array([0]), bytes);
    const out = await new Response(new Blob([bytes]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer();
    return concat(new Uint8Array([1]), new Uint8Array(out));
  }
  async function gunzip(bytes) {
    if (bytes[0] === 0) return bytes.slice(1);
    return new Uint8Array(await new Response(new Blob([bytes.slice(1)]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer());
  }
  async function encryptJSON(obj, compress = true) {
    const raw = te.encode(JSON.stringify(obj));
    return toB64(await seal(dek, compress ? await gzip(raw) : concat(new Uint8Array([0]), raw)));
  }
  async function decryptJSON(b64) {
    return JSON.parse(td.decode(await gunzip(await open(dek, fromB64(b64)))));
  }

  // The unlocked data key survives reloads in IndexedDB as a non-extractable key (it can be used, not read out).
  function idb(mode, fn) {
    return new Promise((resolve) => {
      let req;
      try { req = indexedDB.open("serpentine-cloud", 1); } catch (e) { return resolve(null); }
      req.onupgradeneeded = () => req.result.createObjectStore("keys");
      req.onerror = () => resolve(null);
      req.onsuccess = () => {
        try {
          const tx = req.result.transaction("keys", mode);
          const r = fn(tx.objectStore("keys"));
          tx.oncomplete = () => resolve(r && "result" in r ? r.result : null);
          tx.onerror = () => resolve(null);
        } catch (e) { resolve(null); }
      };
    });
  }
  const keyGet = (email) => idb("readonly", (s) => s.get(email));
  const keySet = (email, k) => idb("readwrite", (s) => s.put(k, email));
  const keyClear = () => idb("readwrite", (s) => s.clear());

  // ---------------------------------------------------------------- API
  async function api(path, method = "GET", body) {
    let res;
    try {
      res = await fetch("/api/" + path, {
        method,
        credentials: "same-origin",
        headers: method === "GET" ? {} : { "content-type": "application/json", "x-serpentine": "1" },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      const err = new Error("Can't reach the server. Check your connection.");
      err.offline = true;
      throw err;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || "The server said no (" + res.status + ").");
      err.status = res.status;
      err.conflict = !!data.conflict;
      throw err;
    }
    return data;
  }

  // ---------------------------------------------------------------- share links
  const b64url = (b) => toB64(b).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const fromB64url = (s) => fromB64(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  async function createShare() {
    const raw = crypto.getRandomValues(new Uint8Array(32));
    const key = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt"]);
    const { board, ...p } = projectPayload();
    const payload = { ...p, board, sharedAt: Date.now() };
    const data = toB64(await seal(key, await gzip(te.encode(JSON.stringify(payload)))));
    const r = await api("shares", "POST", { data, days: shareDays });
    return { url: `${location.origin}${location.pathname}#s=${r.id}.${b64url(raw)}`, expires_at: r.expires_at };
  }
  async function openShareFromHash() {
    const m = location.hash.match(/^#s=([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$/);
    if (!m) return;
    window.history.replaceState(null, "", location.pathname + location.search);
    try {
      const r = await api("share/" + m[1]);
      const key = await crypto.subtle.importKey("raw", fromB64url(m[2]), "AES-GCM", false, ["decrypt"]);
      const obj = JSON.parse(td.decode(await gunzip(await open(key, fromB64(r.data)))));
      delete obj.sharedAt;
      await openProjectObject(obj, `Opened a shared copy of “${obj.name || "a project"}”. It's yours to change; the original isn't affected. ${KEYS.mod}Z brings back what you had open.`);
      state.cloudId = null;
      state.cloudUpdated = null;
      autosave();
      setStatus("off");
    } catch (e) {
      toast(e.status === 404 ? e.message : e.offline ? e.message : "This share link is incomplete or damaged. Ask for the full link again.");
    }
  }
  async function refreshShares() {
    try { shares = (await api("shares")).shares; } catch (e) { shares = []; }
    if (view === "share") render();
  }

  // ---------------------------------------------------------------- status
  function setStatus(s, text = "") {
    status = s;
    statusText = text;
    renderButton();
  }
  function renderButton() {
    const btn = $("#btn-account");
    if (!btn) return;
    const label = $(".acct-label", btn);
    const chip = $("#cloud-status");
    if (me) {
      label.textContent = me.email.split("@")[0];
      btn.title = "Signed in as " + me.email + ". Your online projects.";
      btn.classList.add("signed-in");
    } else {
      label.textContent = "Sign in";
      btn.title = "Sign in to save projects online, end-to-end encrypted";
      btn.classList.remove("signed-in");
    }
    const linked = !!state.cloudId;
    const show = me && (linked || status === "locked");
    chip.hidden = !show;
    if (!show) return;
    const map = {
      saved: ["ok", "Saved online"], saving: ["busy", "Saving…"], pending: ["busy", "Saving…"],
      error: ["bad", "Not saved"], conflict: ["bad", "Changed elsewhere"], locked: ["bad", "Locked"], off: ["", ""],
    };
    const [cls, txt] = map[status] || map.off;
    chip.className = "cloud-status " + cls;
    chip.textContent = statusText || txt;
    chip.title = status === "error" ? "Couldn't save online. Click to see why." : status === "conflict" ? "This project was changed in another tab or on another device. Click to choose which version to keep." : status === "locked" ? "Enter your password to unlock your online projects." : "End-to-end encrypted. Only you can read it.";
  }

  // ---------------------------------------------------------------- session
  async function init() {
    try {
      const h = await fetch("/api/health", { credentials: "same-origin" });
      available = h.ok && (await h.json().catch(() => ({}))).accounts === true;
    } catch (e) {
      available = false;
    }
    if (!available) {
      if (/github\.io$/.test(location.hostname) && !store("serpentine.moved.v1")) {
        const bar = document.createElement("div");
        bar.className = "moved-bar";
        bar.innerHTML = `Serpentine has a new home with free, end-to-end encrypted accounts: <a href="${MAIN_SITE}/app.html">${MAIN_SITE.replace("https://", "")}</a>. To bring this project along, press Save here, then Open the file there. <button type="button" class="link-btn">Dismiss</button>`;
        bar.querySelector("button").onclick = () => { store("serpentine.moved.v1", true); bar.remove(); };
        document.body.prepend(bar);
      }
      return renderButton();
    }
    try {
      const r = await api("me");
      if (r.email) {
        me = r;
        usage = r.usage;
        dek = await keyGet(r.email);
        if (dek) {
          await afterUnlock(false);
        } else {
          setStatus("locked");
        }
      } else if (state.cloudId) {
        setStatus("off");
      }
    } catch (e) { /* offline: keep working locally */ }
    renderButton();
  }
  async function afterUnlock(fresh) {
    setStatus(state.cloudId ? "saved" : "off");
    await pullStyles().catch(() => {});
    if (state.cloudId && !fresh) {
      // The local copy may be newer than the online one (e.g. edits made while offline): save it.
      lastSynced = null;
      changed();
    }
  }
  async function finishSignIn(r, password, fresh) {
    const keys = await deriveFromPassword(r.email, password);
    const raw = await unwrapDek(keys.wrap, r.wrapped_dek);
    dek = await importDek(raw);
    raw.fill(0);
    me = r;
    usage = r.usage;
    await keySet(r.email, dek);
    await afterUnlock(fresh);
  }

  // ---------------------------------------------------------------- saving
  function projectPayload() {
    const { cloudId, cloudUpdated, step, rtab, ...rest } = state;
    return rest;
  }
  function metaFor(p) {
    return { name: p.name, analysis: p.analysis, tables: p.datasets.length, panels: (p.board?.panels || []).length };
  }
  function ready() {
    return !!(me && dek);
  }
  function changed() {
    if (!ready() || !state.cloudId) return;
    clearTimeout(syncTimer);
    if (status !== "conflict") setStatus("pending");
    syncTimer = setTimeout(() => syncNow(), 2500);
  }
  async function syncNow(force = false) {
    if (!ready() || !state.cloudId) return;
    if (syncing) { syncAgain = true; return; }
    const payload = projectPayload();
    const text = JSON.stringify(payload);
    if (text === lastSynced && !force) { setStatus("saved"); return; }
    syncing = true;
    setStatus("saving");
    try {
      const r = await api("items/" + state.cloudId, "PUT", {
        kind: "project", meta: await encryptJSON(metaFor(payload), false), data: await encryptJSON(payload),
        base_updated: state.cloudUpdated || undefined, force,
      });
      state.cloudUpdated = r.updated_at;
      usage = r.usage;
      lastSynced = text;
      store(STORE_KEY, state);
      setStatus("saved");
    } catch (e) {
      if (e.conflict) setStatus("conflict");
      else if (e.status === 401) { me = null; dek = null; setStatus("off"); toast("You were signed out. Sign in again to keep saving online."); }
      else setStatus("error", e.offline ? "Offline" : "");
      if (!e.conflict && !e.offline && e.status !== 401) toast(e.message);
    } finally {
      syncing = false;
      if (syncAgain) { syncAgain = false; syncNow(); }
    }
  }
  // Save button / ⌘S while signed in
  async function saveCurrent() {
    if (!ready()) return false;
    if (!state.cloudId) {
      state.cloudId = crypto.randomUUID().replace(/-/g, "");
      state.cloudUpdated = null;
      lastSynced = null;
    }
    await syncNow(true);
    if (status === "saved") toast("Saved to your account · end-to-end encrypted");
    if (!$("#cloud").hidden) refresh();
    return true;
  }

  async function pullStyles() {
    let remote = [];
    try {
      const r = await api("items/styles");
      remote = (await decryptJSON(r.data)) || [];
    } catch (e) {
      if (e.status !== 404) throw e;
    }
    const local = store(STYLES_KEY) || [];
    const names = new Set(local.map((s) => s.name));
    const merged = local.concat(remote.filter((s) => s && s.name && !names.has(s.name)));
    store(STYLES_KEY, merged);
    if (JSON.stringify(merged) !== JSON.stringify(remote)) await pushStyles(merged);
    if (state.step === "style") renderStyle();
  }
  async function pushStyles(list) {
    await api("items/styles", "PUT", { kind: "styles", meta: await encryptJSON({ name: "Saved styles" }, false), data: await encryptJSON(list) });
  }
  function stylesChanged() {
    if (!ready()) return;
    clearTimeout(stylesTimer);
    stylesTimer = setTimeout(() => pushStyles(store(STYLES_KEY) || []).catch(() => {}), 1200);
  }

  // ---------------------------------------------------------------- dialog
  function openDialog(v) {
    view = v || (me ? (dek ? "projects" : "unlock") : "signin");
    notice = "";
    if (view === "share") { shareResult = null; refreshShares(); }
    $("#cloud").hidden = false;
    document.body.classList.add("board-open");
    render();
    if (view === "projects") refresh();
    setTimeout(() => $("#cloud input:not([type=hidden]), #cloud .cl-primary")?.focus(), 30);
  }
  function closeDialog() {
    if (view === "recovery" && pendingRecovery) return toast("Save your recovery key first, then tick the box.");
    $("#cloud").hidden = true;
    document.body.classList.remove("board-open");
  }
  async function refresh() {
    try {
      const r = await api("items?kind=project");
      usage = r.usage;
      items = await Promise.all(r.items.map(async (it) => {
        let meta = {};
        try { meta = await decryptJSON(it.meta); } catch (e) { meta = { name: "(can't decrypt)" }; }
        return { ...it, ...meta };
      }));
    } catch (e) {
      notice = e.message;
    }
    if (view === "projects" && !$("#cloud").hidden) render();
  }

  const fmtBytes = (n) => (n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB");
  function ago(ms) {
    const s = (Date.now() - ms) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    if (s < 86400 * 7) return Math.floor(s / 86400) + " d ago";
    return new Date(ms).toLocaleDateString();
  }
  const field = (id, label, type = "text", extra = "") => `<div class="field"><label for="${id}">${label}</label><input id="${id}" type="${type}" ${extra} spellcheck="false"></div>`;
  const note = () => (notice ? `<div class="callout ${/saved|changed|ready|unlocked/i.test(notice) ? "callout-ok" : "callout-bad"}">${esc(notice)}</div>` : "");
  const privacy = `<p class="fine cl-privacy"><b>End-to-end encrypted.</b> Your projects are encrypted on this computer with your password before they're uploaded. The server only stores scrambled data that nobody else can read, including whoever runs it.</p>`;

  function render() {
    const body = $("#cloud-body");
    const title = $("#cloud-title");
    if (!available) {
      title.textContent = "Accounts";
      body.innerHTML = `<p>Accounts work on Serpentine's main site, where your projects can be saved online, end-to-end encrypted.</p>
        <p class="fine">This copy of Serpentine (${esc(location.host || "a local file")}) has no server, so everything stays in this browser. You can still save projects as files.</p>
        <div class="btn-row"><a class="btn btn-primary cl-primary" href="${MAIN_SITE}/app.html">Open the main site</a></div>`;
      return;
    }
    const v = view;
    if (v === "signin" || v === "signup") {
      const up = v === "signup";
      title.textContent = up ? "Create an account" : "Sign in";
      body.innerHTML = `${note()}
        <form id="cl-form" class="cl-form" data-action="${v}" autocomplete="on">
          ${field("cl-email", "Email", "email", 'autocomplete="username" required')}
          ${field("cl-pass", "Password", "password", `autocomplete="${up ? "new-password" : "current-password"}" required minlength="${up ? 10 : 1}"`)}
          ${up ? field("cl-pass2", "Password again", "password", 'autocomplete="new-password" required') : ""}
          ${up ? `<span class="hint">At least 10 characters. A short sentence is easy to remember and hard to guess.</span>
            <label class="cl-check"><input type="checkbox" id="cl-understand" required> I understand that if I forget my password <b>and</b> lose my recovery key, my online projects can't be recovered by anyone.</label>` : ""}
          <button type="submit" class="btn btn-primary cl-primary"${busy ? " disabled" : ""}>${busy ? (up ? "Creating…" : "Signing in…") : up ? "Create account" : "Sign in"}</button>
        </form>
        <div class="cl-links">${up ? `Already have an account? <button type="button" class="link-btn" data-cl="signin">Sign in</button>`
          : `New here? <button type="button" class="link-btn" data-cl="signup">Create an account</button> · <button type="button" class="link-btn" data-cl="forgot">Forgot password?</button>`}</div>
        ${privacy}`;
      return;
    }
    if (v === "forgot") {
      title.textContent = "Reset your password";
      body.innerHTML = `${note()}
        <p class="fine">Because your data is encrypted with your password, a reset needs the recovery key you saved when you created your account.</p>
        <form id="cl-form" class="cl-form" data-action="forgot">
          ${field("cl-email", "Email", "email", 'autocomplete="username" required')}
          ${field("cl-rk", "Recovery key", "text", 'placeholder="XXXX-XXXX-XXXX-…" required autocomplete="off"')}
          ${field("cl-pass", "New password", "password", 'autocomplete="new-password" required minlength="10"')}
          ${field("cl-pass2", "New password again", "password", 'autocomplete="new-password" required')}
          <button type="submit" class="btn btn-primary cl-primary"${busy ? " disabled" : ""}>${busy ? "Resetting…" : "Reset password"}</button>
        </form>
        <div class="cl-links"><button type="button" class="link-btn" data-cl="signin">← Back to sign in</button></div>`;
      return;
    }
    if (v === "unlock") {
      title.textContent = "Unlock your projects";
      body.innerHTML = `${note()}<p>Signed in as <b>${esc(me.email)}</b>. Enter your password to decrypt your projects on this computer.</p>
        <form id="cl-form" class="cl-form" data-action="unlock">
          <input type="email" value="${esc(me.email)}" autocomplete="username" hidden>
          ${field("cl-pass", "Password", "password", 'autocomplete="current-password" required')}
          <button type="submit" class="btn btn-primary cl-primary"${busy ? " disabled" : ""}>${busy ? "Unlocking…" : "Unlock"}</button>
        </form>
        <div class="cl-links"><button type="button" class="link-btn" data-cl="forgot">Forgot password?</button> · <button type="button" class="link-btn" data-cl="logout">Sign out</button></div>`;
      return;
    }
    if (v === "recovery") {
      title.textContent = "Save your recovery key";
      body.innerHTML = `<p>This is the <b>only</b> way back into your projects if you forget your password. It's shown once. Download it or write it down, and keep it somewhere safe.</p>
        <div class="cl-rk" id="cl-rk-text">${esc(pendingRecovery)}</div>
        <div class="btn-row"><button type="button" class="btn btn-small" data-cl="rk-copy">Copy</button><button type="button" class="btn btn-small" data-cl="rk-download">Download as a text file</button></div>
        <label class="cl-check"><input type="checkbox" id="cl-rk-ok"> I've saved my recovery key somewhere safe.</label>
        <div class="btn-row"><button type="button" class="btn btn-primary cl-primary" data-cl="rk-done" disabled>Continue</button></div>`;
      return;
    }
    if (v === "share") {
      title.textContent = "Share a link";
      if (!me) {
        body.innerHTML = `<p>Share links let a colleague open this exact analysis (data, settings, style and board) in their browser, no account needed on their side.</p>
          <p class="fine">Sign in or create a free account to make links.</p>
          <div class="btn-row"><button type="button" class="btn btn-primary cl-primary" data-cl="signin">Sign in</button><button type="button" class="btn" data-cl="signup">Create an account</button></div>`;
        return;
      }
      const opt = (d, label) => `<option value="${d}"${shareDays === d ? " selected" : ""}>${label}</option>`;
      body.innerHTML = `${note()}
        <p>Anyone with the link can open a copy of <b>${esc(state.name)}</b> as it is now: data, settings, style and board. Changes they make don't affect yours.</p>
        <div class="cl-share-row">
          <div class="field"><label for="cl-days">Link works for</label><select id="cl-days">${opt(1, "1 day")}${opt(7, "7 days")}${opt(30, "30 days")}${opt(90, "90 days")}${opt(365, "1 year")}</select></div>
          <button type="button" class="btn btn-primary cl-primary" data-cl="share-make"${busy ? " disabled" : ""}>${busy ? "Encrypting…" : shareResult ? "Make another link" : "Create link"}</button>
        </div>
        ${shareResult ? `<div class="cl-share-out"><input type="text" id="cl-share-url" readonly value="${esc(shareResult.url)}"><button type="button" class="btn btn-small btn-primary" data-cl="share-copy">Copy</button></div>
          <span class="hint">Works until ${new Date(shareResult.expires_at).toLocaleDateString()}. Send the whole link: the part after # is the key.</span>` : ""}
        <p class="fine cl-privacy"><b>Encrypted:</b> the project is encrypted on this computer with a new random key that exists only in the link (after the #). Browsers never send that part to the server, so the server can't read what you share.</p>
        ${shares.length ? `<div class="section-title" style="margin-top:14px">Your links</div><div class="cl-list">${shares.map((sh) => `
          <div class="cl-item" data-id="${esc(sh.id)}"><div class="cl-item-main"><b>Link made ${ago(sh.created_at)}</b>
            <span class="fine">${fmtBytes(sh.size)} · opened ${sh.views} time${sh.views === 1 ? "" : "s"} · expires ${new Date(sh.expires_at).toLocaleDateString()}</span></div>
            <div class="btn-row"><button type="button" class="btn btn-small" data-cl="share-del">Remove</button></div></div>`).join("")}</div>
          <span class="hint">Removing a link stops it working immediately. Links can't be shown again, because the key is only in the link you copied.</span>` : ""}`;
      return;
    }
    if (v === "account") {
      title.textContent = "Account";
      const used = usage ? `${fmtBytes(usage.bytes)} of ${fmtBytes(usage.max_bytes)} used · ${usage.items} item${usage.items === 1 ? "" : "s"}` : "";
      body.innerHTML = `${note()}<p>Signed in as <b>${esc(me.email)}</b>${me.created_at ? " since " + new Date(me.created_at).toLocaleDateString() : ""}. ${used}</p>
        <details class="cl-sec"><summary>Change password</summary>
          <form id="cl-form-pw" class="cl-form" data-action="password">
            <input type="email" value="${esc(me.email)}" autocomplete="username" hidden>
            ${field("cl-old", "Current password", "password", 'autocomplete="current-password" required')}
            ${field("cl-pass", "New password", "password", 'autocomplete="new-password" required minlength="10"')}
            ${field("cl-pass2", "New password again", "password", 'autocomplete="new-password" required')}
            <button type="submit" class="btn btn-small btn-primary"${busy ? " disabled" : ""}>Change password</button>
          </form></details>
        <details class="cl-sec"><summary>Get a new recovery key</summary>
          <form id="cl-form-rk" class="cl-form" data-action="newrk">
            <p class="fine">Your old recovery key stops working.</p>
            ${field("cl-pass-rk", "Password", "password", 'autocomplete="current-password" required')}
            <button type="submit" class="btn btn-small"${busy ? " disabled" : ""}>Make a new recovery key</button>
          </form></details>
        <details class="cl-sec cl-danger"><summary>Delete account</summary>
          <form id="cl-form-del" class="cl-form" data-action="delete">
            <p class="fine">Deletes your account and every project stored online, permanently. Copies on this computer stay.</p>
            ${field("cl-pass-del", "Password", "password", 'autocomplete="current-password" required')}
            ${field("cl-confirm", 'Type DELETE to confirm', "text", 'required autocomplete="off"')}
            <button type="submit" class="btn btn-small btn-danger"${busy ? " disabled" : ""}>Delete my account</button>
          </form></details>
        <div class="cl-links"><button type="button" class="link-btn" data-cl="projects">← Back to my projects</button></div>`;
      return;
    }
    // projects
    title.textContent = "My projects";
    const used = usage ? Math.min(100, (usage.bytes / usage.max_bytes) * 100) : 0;
    const cur = state.cloudId;
    const conflict = status === "conflict" ? `<div class="callout callout-bad"><b>This project was changed somewhere else</b> (another tab or device) since you opened it.
      <div class="btn-row" style="margin-top:8px"><button type="button" class="btn btn-small btn-primary" data-cl="keep-mine">Keep this version</button><button type="button" class="btn btn-small" data-cl="take-theirs">Open the other version</button></div></div>` : "";
    body.innerHTML = `${note()}${conflict}
      <div class="cl-current">
        <div><span class="section-title">Open now</span><b>${esc(state.name)}</b>
          <span class="fine">${cur ? (status === "saved" ? "Saved online · changes save automatically" : status === "error" ? "Couldn't save online: " + esc(statusText || "try again") : status === "conflict" ? "Changed elsewhere" : "Saving…") : "Only on this computer"}</span></div>
        <div class="btn-row">
          ${cur ? `<button type="button" class="btn btn-small" data-cl="save">Save now</button>` : `<button type="button" class="btn btn-small btn-primary" data-cl="save">Save to my account</button>`}
          <button type="button" class="btn btn-small" data-cl="download">Download a copy</button>
        </div>
      </div>
      <div class="cl-list">${items.length ? items.map((it) => `
        <div class="cl-item${it.id === cur ? " current" : ""}" data-id="${esc(it.id)}">
          <div class="cl-item-main"><b>${esc(it.name || "Untitled")}</b>
            <span class="fine">${esc(ANALYSES.find((a) => a.id === it.analysis)?.title || "")}${it.tables != null ? " · " + it.tables + " table" + (it.tables === 1 ? "" : "s") : ""}${it.panels ? " · " + it.panels + " board panel" + (it.panels === 1 ? "" : "s") : ""} · ${ago(it.updated_at)} · ${fmtBytes(it.size)}</span></div>
          <div class="btn-row">${it.id === cur ? '<span class="cl-open-tag">Open</span>' : `<button type="button" class="btn btn-small" data-cl="open">Open</button>`}
            <button type="button" class="icon-btn" data-cl="del" aria-label="Delete ${esc(it.name || "project")}" title="Delete from your account">×</button></div>
        </div>`).join("") : `<div class="board-empty"><h3>No projects online yet</h3><p>Press <b>Save to my account</b> above, or the Save button (${KEYS.mod}S) any time. After that, changes save automatically.</p></div>`}
      </div>
      <div class="cl-usage"><div class="cl-bar"><i style="width:${used.toFixed(1)}%"></i></div><span class="fine">${usage ? fmtBytes(usage.bytes) + " of " + fmtBytes(usage.max_bytes) : ""}</span></div>
      <div class="cl-foot"><span class="fine">Signed in as <b>${esc(me.email)}</b></span>
        <div class="btn-row"><button type="button" class="btn btn-small" data-cl="account">Account</button><button type="button" class="btn btn-small" data-cl="logout">Sign out</button></div></div>
      <p class="fine cl-privacy"><b>End-to-end encrypted:</b> only you can read these projects.</p>`;
  }

  // ---------------------------------------------------------------- actions
  let vals = {};
  const val = (id) => vals[id] || "";
  function checkNewPassword() {
    const p = val("cl-pass");
    if (p.length < 10) throw new Error("Use at least 10 characters for your password.");
    if (p !== val("cl-pass2")) throw new Error("The two passwords don't match.");
    return p;
  }
  async function submit(action) {
    if (busy) return;
    // Read the form before anything redraws it
    vals = Object.fromEntries($$("#cloud-body input").filter((i) => i.id).map((i) => [i.id, i.type === "checkbox" ? (i.checked ? "1" : "") : i.value]));
    busy = true;
    notice = "";
    const btn = $(`#cloud-body form[data-action="${action}"] button[type=submit]`);
    if (btn) { btn.disabled = true; btn.textContent = action === "signup" ? "Creating…" : action === "unlock" ? "Unlocking…" : "Working…"; }
    try {
      if (action === "signup") {
        const email = val("cl-email").trim().toLowerCase();
        const password = checkNewPassword();
        const keys = await deriveFromPassword(email, password);
        const raw = crypto.getRandomValues(new Uint8Array(32));
        const rk = crypto.getRandomValues(new Uint8Array(32));
        const rkeys = await deriveFromRecovery(rk);
        const r = await api("signup", "POST", { email, auth: keys.auth, wrapped_dek: await wrapDek(keys.wrap, raw), recovery_auth: rkeys.auth, recovery_wrapped_dek: await wrapDek(rkeys.wrap, raw) });
        await finishSignIn(r, password, true);
        raw.fill(0);
        pendingRecovery = toB32(rk);
        rk.fill(0);
        view = "recovery";
      } else if (action === "signin") {
        const email = val("cl-email").trim().toLowerCase();
        const password = val("cl-pass");
        const keys = await deriveFromPassword(email, password);
        const r = await api("login", "POST", { email, auth: keys.auth });
        await finishSignIn(r, password, false);
        view = "projects";
        toast("Signed in as " + r.email);
        refresh();
      } else if (action === "unlock") {
        try {
          await finishSignIn(me, val("cl-pass"), false);
        } catch (e) {
          throw new Error("That password doesn't unlock this account.");
        }
        view = "projects";
        refresh();
      } else if (action === "forgot") {
        const email = val("cl-email").trim().toLowerCase();
        let rk;
        try { rk = fromB32(val("cl-rk")); } catch (e) { throw new Error("That doesn't look like a recovery key. It has 52 letters and numbers in groups of four."); }
        const password = checkNewPassword();
        const rkeys = await deriveFromRecovery(rk);
        const step1 = await api("recover", "POST", { email, recovery_auth: rkeys.auth });
        const raw = await unwrapDek(rkeys.wrap, step1.recovery_wrapped_dek);
        const keys = await deriveFromPassword(email, password);
        const r = await api("recover", "POST", { email, recovery_auth: rkeys.auth, auth: keys.auth, wrapped_dek: await wrapDek(keys.wrap, raw) });
        raw.fill(0);
        await finishSignIn(r, password, false);
        view = "projects";
        notice = "Password changed. Your recovery key still works.";
        refresh();
      } else if (action === "password") {
        const oldKeys = await deriveFromPassword(me.email, val("cl-old"));
        let raw;
        try { raw = await unwrapDek(oldKeys.wrap, me.wrapped_dek); } catch (e) { throw new Error("Your current password is wrong."); }
        const password = checkNewPassword();
        const keys = await deriveFromPassword(me.email, password);
        const wrapped = await wrapDek(keys.wrap, raw);
        raw.fill(0);
        await api("password", "POST", { old_auth: oldKeys.auth, auth: keys.auth, wrapped_dek: wrapped });
        me.wrapped_dek = wrapped;
        notice = "Password changed. Other devices will need to sign in again.";
      } else if (action === "newrk") {
        const keys = await deriveFromPassword(me.email, val("cl-pass-rk"));
        let raw;
        try { raw = await unwrapDek(keys.wrap, me.wrapped_dek); } catch (e) { throw new Error("Your password is wrong."); }
        const rk = crypto.getRandomValues(new Uint8Array(32));
        const rkeys = await deriveFromRecovery(rk);
        await api("recovery-key", "POST", { auth: keys.auth, recovery_auth: rkeys.auth, recovery_wrapped_dek: await wrapDek(rkeys.wrap, raw) });
        raw.fill(0);
        pendingRecovery = toB32(rk);
        view = "recovery";
      } else if (action === "delete") {
        if (val("cl-confirm").trim() !== "DELETE") throw new Error("Type DELETE in capitals to confirm.");
        const keys = await deriveFromPassword(me.email, val("cl-pass-del"));
        await api("account", "DELETE", { auth: keys.auth });
        await signedOut(false);
        toast("Your account and online projects were deleted.");
        view = "signin";
      }
    } catch (e) {
      notice = e.message || "Something went wrong.";
    } finally {
      busy = false;
      const keep = notice && !/changed|ready/i.test(notice) ? vals : {};
      const openSec = $$("#cloud-body details[open]").map((d) => [...d.parentNode.children].indexOf(d));
      render();
      // After an error, keep what was typed (and which account section was open)
      for (const [id, v] of Object.entries(keep)) {
        const el = document.getElementById(id);
        if (el && el.type !== "checkbox") el.value = v;
        else if (el) el.checked = !!v;
      }
      openSec.forEach((i) => { const d = $("#cloud-body").children[i]; if (d && d.tagName === "DETAILS") d.open = true; });
      vals = {};
    }
  }

  async function signedOut(callServer = true) {
    if (callServer) await api("logout", "POST").catch(() => {});
    await keyClear();
    const wasLinked = !!state.cloudId;
    me = null;
    dek = null;
    items = [];
    usage = null;
    lastSynced = null;
    clearTimeout(syncTimer);
    if (wasLinked) {
      // Don't leave a decrypted copy of an online project sitting in this browser
      const keepBoard = clone(BOARD_DEFAULTS);
      state = freshState();
      state.board = keepBoard;
      Object.keys(inspected).forEach((k) => delete inspected[k]);
      store(STORE_KEY, state);
      history.past = [];
      history.future = [];
      history.last = snapshot();
      renderAll();
      renderBoardCount();
      renderEmptyStage();
    }
    setStatus("off");
  }

  async function openItem(id) {
    try {
      notice = "";
      const r = await api("items/" + id);
      const obj = await decryptJSON(r.data);
      await openProjectObject(obj, "Opened “" + (obj.name || "project") + "” from your account");
      state.cloudId = id;
      state.cloudUpdated = r.updated_at;
      lastSynced = JSON.stringify(projectPayload());
      store(STORE_KEY, state);
      setStatus("saved");
      closeDialog();
    } catch (e) {
      notice = e.message;
      render();
    }
  }

  function bind() {
    $("#btn-account").addEventListener("click", () => openDialog());
    $("#cloud-status").addEventListener("click", () => openDialog());
    const box = $("#cloud");
    box.addEventListener("submit", (e) => {
      e.preventDefault();
      submit(e.target.dataset.action);
    });
    box.addEventListener("change", (e) => {
      if (e.target.id === "cl-rk-ok") $("[data-cl=rk-done]").disabled = !e.target.checked;
    });
    box.addEventListener("click", async (e) => {
      if (e.target === box || e.target.closest(".cloud-close")) return closeDialog();
      const b = e.target.closest("[data-cl]");
      if (!b) return;
      const a = b.dataset.cl;
      const id = b.closest("[data-id]")?.dataset.id;
      if (a === "share-make") {
        if (busy) return;
        busy = true; notice = ""; render();
        try {
          shareDays = Number($("#cl-days")?.value) || shareDays;
          shareResult = await createShare();
          busy = false;
          render();
          await copyText(shareResult.url, "Link copied. Paste it into an email or chat.");
          $("#cl-share-url")?.select();
          refreshShares();
        } catch (err) { busy = false; notice = err.message; render(); }
        return;
      }
      if (a === "share-copy") { $("#cl-share-url")?.select(); return copyText(shareResult.url, "Link copied"); }
      if (a === "share-del") {
        return armConfirm(b, "Remove?", async () => {
          try { await api("shares/" + id, "DELETE"); toast("Link removed. It no longer works."); refreshShares(); } catch (err) { toast(err.message); }
        });
      }
      if (["signin", "signup", "forgot", "account", "projects"].includes(a)) {
        view = a;
        notice = "";
        render();
        if (a === "projects") refresh();
        setTimeout(() => $("#cloud input:not([hidden])")?.focus(), 20);
        return;
      }
      if (a === "logout") { await signedOut(); view = "signin"; render(); return toast("Signed out"); }
      if (a === "save") return saveCurrent();
      if (a === "download") return downloadProject();
      if (a === "open") return openItem(id);
      if (a === "del") {
        return armConfirm(b, "Delete?", async () => {
          try {
            const r = await api("items/" + id, "DELETE");
            usage = r.usage;
            if (state.cloudId === id) { state.cloudId = null; state.cloudUpdated = null; autosave(); setStatus("off"); }
            toast("Deleted from your account");
            refresh();
          } catch (err) { toast(err.message); }
        });
      }
      if (a === "keep-mine") { setStatus("saving"); await syncNow(true); return render(); }
      if (a === "take-theirs") return openItem(state.cloudId);
      if (a === "rk-copy") return copyText(pendingRecovery, "Recovery key copied. Paste it somewhere safe.");
      if (a === "rk-download") {
        return download("serpentine-recovery-key.txt", `Serpentine recovery key\nAccount: ${me?.email}\nCreated: ${new Date().toLocaleString()}\n\n${pendingRecovery}\n\nThis key resets your password if you forget it. Keep it private and safe.\n`, "text/plain");
      }
      if (a === "rk-done") {
        pendingRecovery = null;
        view = "projects";
        notice = "Your account is ready.";
        render();
        refresh();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !box.hidden) closeDialog();
    });
    window.addEventListener("beforeunload", (e) => {
      if (ready() && state.cloudId && (status === "pending" || status === "saving")) {
        syncNow();
        e.preventDefault();
        e.returnValue = "";
      }
    });
  }

  bind();
  init().then(openShareFromHash);
  window.addEventListener("hashchange", openShareFromHash);
  return { ready, changed, saveCurrent, stylesChanged, open: openDialog, share: () => openDialog("share"), get available() { return available; }, get email() { return me?.email; } };
})();
