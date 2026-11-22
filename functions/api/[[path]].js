// Serpentine accounts API (Cloudflare Pages Functions + D1).
//
// Zero-knowledge design: the browser derives two keys from the password. One ("auth") proves who you are;
// the server stores only a salted hash of it. The other never leaves the browser and unlocks the data key
// that encrypts every project. The server stores ciphertext it cannot read.

const SESSION_DAYS = 30;
const MAX_ITEM_BYTES = 25 * 1024 * 1024;
const MAX_USER_BYTES = 200 * 1024 * 1024;
const MAX_ITEMS = 1000;
const CHUNK = 900 * 1024; // D1 rows top out at 2 MB
const KINDS = new Set(["project", "styles"]);
const MAX_SHARE_BYTES = 25 * 1024 * 1024;
const MAX_SHARES = 200;
const SHARE_DAYS = new Set([1, 7, 30, 90, 365]);

const enc = new TextEncoder();
const b64 = {
  encode(bytes) {
    let s = "";
    const u = new Uint8Array(bytes);
    for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
    return btoa(s);
  },
  decode(str) {
    const bin = atob(str);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  },
};
const hex = (buf) => [...new Uint8Array(buf)].map((x) => x.toString(16).padStart(2, "0")).join("");
const randomHex = (n) => hex(crypto.getRandomValues(new Uint8Array(n)));
async function sha256Hex(...parts) {
  const all = parts.map((p) => (typeof p === "string" ? enc.encode(p) : p));
  const buf = new Uint8Array(all.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of all) { buf.set(p, o); o += p.length; }
  return hex(await crypto.subtle.digest("SHA-256", buf));
}
function sameString(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", "cache-control": "no-store", ...headers } });

function cookie(token, maxAge) {
  return `sp_session=${token}; Path=/api; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;
}
function readCookie(request) {
  const m = (request.headers.get("cookie") || "").match(/(?:^|;\s*)sp_session=([A-Za-z0-9_-]+)/);
  return m ? m[1] : null;
}

// ---- validation
const EMAIL = /^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,}$/;
function email(v) {
  const e = String(v || "").trim().toLowerCase();
  if (!EMAIL.test(e) || e.length > 254) throw new HttpError(400, "Enter a valid email address.");
  return e;
}
function key32(v, what) {
  try {
    if (typeof v === "string" && b64.decode(v).length === 32) return v;
  } catch (e) { /* fall through */ }
  throw new HttpError(400, `Malformed ${what}.`);
}
function blob(v, what, max = 512) {
  if (typeof v !== "string" || v.length < 16 || v.length > max) throw new HttpError(400, `Malformed ${what}.`);
  try { b64.decode(v); } catch (e) { throw new HttpError(400, `Malformed ${what}.`); }
  return v;
}

// ---- rate limiting (failed sign-ins per email and per IP)
async function tooMany(env, keys, limit, windowMs) {
  const since = Date.now() - windowMs;
  for (const k of keys) {
    const r = await env.DB.prepare("SELECT COUNT(*) AS n FROM attempts WHERE key = ? AND at > ?").bind(k, since).first();
    if (r.n >= limit) return true;
  }
  return false;
}
async function noteFailure(env, keys) {
  const now = Date.now();
  await env.DB.batch(keys.map((k) => env.DB.prepare("INSERT INTO attempts (key, at) VALUES (?, ?)").bind(k, now)));
  if (Math.random() < 0.05) await env.DB.prepare("DELETE FROM attempts WHERE at < ?").bind(now - 86400000).run();
}

// ---- sessions
async function newSession(env, userId) {
  const token = b64.encode(crypto.getRandomValues(new Uint8Array(32))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const expires = Date.now() + SESSION_DAYS * 86400000;
  await env.DB.prepare("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)").bind(await sha256Hex(token), userId, expires).run();
  if (Math.random() < 0.05) await env.DB.prepare("DELETE FROM sessions WHERE expires_at < ?").bind(Date.now()).run();
  return cookie(token, SESSION_DAYS * 86400);
}
async function currentUser(env, request) {
  const token = readCookie(request);
  if (!token) return null;
  const row = await env.DB.prepare(
    "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?"
  ).bind(await sha256Hex(token), Date.now()).first();
  return row || null;
}
async function requireUser(env, request) {
  const u = await currentUser(env, request);
  if (!u) throw new HttpError(401, "Please sign in again.");
  return u;
}
async function checkAuth(user, auth) {
  return sameString(await sha256Hex(user.auth_salt, b64.decode(key32(auth, "key"))), user.auth_hash);
}
async function usage(env, userId) {
  const r = await env.DB.prepare("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM items WHERE user_id = ?").bind(userId).first();
  return { items: r.n, bytes: r.bytes, max_bytes: MAX_USER_BYTES };
}
const publicUser = async (env, u) => ({ email: u.email, wrapped_dek: u.wrapped_dek, created_at: u.created_at, usage: await usage(env, u.id) });

// ---- routes
async function route(request, env, path) {
  const method = request.method;
  if (method !== "GET" && method !== "HEAD") {
    // CSRF: browsers can't send this header cross-site without a preflight we never approve
    if (request.headers.get("x-serpentine") !== "1") throw new HttpError(403, "Missing request header.");
  }
  const body = method === "POST" || method === "PUT" || method === "DELETE" ? await request.json().catch(() => ({})) : {};
  const ip = request.headers.get("cf-connecting-ip") || "unknown";

  if (path === "health") return json({ ok: true, accounts: true });

  if (path === "signup" && method === "POST") {
    const e = email(body.email);
    if (await tooMany(env, ["ip-signup:" + ip], 20, 3600000)) throw new HttpError(429, "Too many new accounts from here. Try again later.");
    const exists = await env.DB.prepare("SELECT 1 FROM users WHERE email = ?").bind(e).first();
    if (exists) throw new HttpError(409, "An account with that email already exists. Sign in instead.");
    const id = crypto.randomUUID();
    const salt = randomHex(16), rsalt = randomHex(16);
    await env.DB.prepare(
      "INSERT INTO users (id, email, auth_hash, auth_salt, wrapped_dek, recovery_hash, recovery_salt, recovery_wrapped_dek, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ).bind(id, e, await sha256Hex(salt, b64.decode(key32(body.auth, "key"))), salt, blob(body.wrapped_dek, "key"),
      await sha256Hex(rsalt, b64.decode(key32(body.recovery_auth, "recovery key"))), rsalt, blob(body.recovery_wrapped_dek, "key"), Date.now()).run();
    await noteFailure(env, ["ip-signup:" + ip]); // counts toward the sign-up limit
    const u = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(id).first();
    return json(await publicUser(env, u), 200, { "set-cookie": await newSession(env, id) });
  }

  if (path === "login" && method === "POST") {
    const e = email(body.email);
    const keys = ["email:" + e, "ip:" + ip];
    if (await tooMany(env, keys.slice(0, 1), 10, 900000) || await tooMany(env, keys.slice(1), 50, 900000)) {
      throw new HttpError(429, "Too many attempts. Wait 15 minutes and try again.");
    }
    const u = await env.DB.prepare("SELECT * FROM users WHERE email = ?").bind(e).first();
    if (!u || !(await checkAuth(u, body.auth))) {
      await noteFailure(env, keys);
      throw new HttpError(401, "Wrong email or password.");
    }
    return json(await publicUser(env, u), 200, { "set-cookie": await newSession(env, u.id) });
  }

  if (path === "logout" && method === "POST") {
    const token = readCookie(request);
    if (token) await env.DB.prepare("DELETE FROM sessions WHERE token_hash = ?").bind(await sha256Hex(token)).run();
    return json({ ok: true }, 200, { "set-cookie": cookie("x", 0) });
  }

  if (path === "me" && method === "GET") {
    const u = await currentUser(env, request);
    return json(u ? await publicUser(env, u) : { signed_in: false });
  }

  // Forgotten password: prove you hold the recovery key, get the data key wrapped with it, set a new password.
  if (path === "recover" && method === "POST") {
    const e = email(body.email);
    const keys = ["recover:" + e, "ip:" + ip];
    if (await tooMany(env, keys.slice(0, 1), 10, 3600000) || await tooMany(env, keys.slice(1), 50, 900000)) {
      throw new HttpError(429, "Too many attempts. Try again later.");
    }
    const u = await env.DB.prepare("SELECT * FROM users WHERE email = ?").bind(e).first();
    const ok = u && sameString(await sha256Hex(u.recovery_salt, b64.decode(key32(body.recovery_auth, "recovery key"))), u.recovery_hash);
    if (!ok) {
      await noteFailure(env, keys);
      throw new HttpError(401, "That recovery key doesn't match this email.");
    }
    if (!body.auth) return json({ recovery_wrapped_dek: u.recovery_wrapped_dek });
    const salt = randomHex(16);
    await env.DB.batch([
      env.DB.prepare("UPDATE users SET auth_hash = ?, auth_salt = ?, wrapped_dek = ? WHERE id = ?")
        .bind(await sha256Hex(salt, b64.decode(key32(body.auth, "key"))), salt, blob(body.wrapped_dek, "key"), u.id),
      env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(u.id),
    ]);
    const fresh = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(u.id).first();
    return json(await publicUser(env, fresh), 200, { "set-cookie": await newSession(env, u.id) });
  }

  // Everything below needs a session
  // Shared links: anyone with the link can fetch the ciphertext; the key lives only in the link's #fragment,
  // which browsers never send to the server.
  const sm = path.match(/^share\/([0-9a-f]{32})$/);
  if (sm && method === "GET") {
    if (await tooMany(env, ["share404:" + ip], 60, 3600000)) throw new HttpError(429, "Too many requests. Try again later.");
    const sh = await env.DB.prepare("SELECT * FROM shares WHERE id = ? AND expires_at > ?").bind(sm[1], Date.now()).first();
    if (!sh) {
      await noteFailure(env, ["share404:" + ip]);
      throw new HttpError(404, "This link has expired or was removed by the person who shared it.");
    }
    const rows = (await env.DB.prepare("SELECT data FROM share_chunks WHERE share_id = ? ORDER BY idx").bind(sh.id).all()).results;
    const parts = rows.map((r) => new Uint8Array(r.data));
    const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let o = 0;
    for (const p of parts) { all.set(p, o); o += p.length; }
    await env.DB.prepare("UPDATE shares SET views = views + 1 WHERE id = ?").bind(sh.id).run();
    return json({ data: b64.encode(all), created_at: sh.created_at, expires_at: sh.expires_at });
  }

  const user = await requireUser(env, request);

  if (path === "password" && method === "POST") {
    if (!(await checkAuth(user, body.old_auth))) throw new HttpError(401, "Your current password is wrong.");
    const salt = randomHex(16);
    await env.DB.batch([
      env.DB.prepare("UPDATE users SET auth_hash = ?, auth_salt = ?, wrapped_dek = ? WHERE id = ?")
        .bind(await sha256Hex(salt, b64.decode(key32(body.auth, "key"))), salt, blob(body.wrapped_dek, "key"), user.id),
      env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(user.id),
    ]);
    return json({ ok: true }, 200, { "set-cookie": await newSession(env, user.id) });
  }

  if (path === "recovery-key" && method === "POST") {
    if (!(await checkAuth(user, body.auth))) throw new HttpError(401, "Your password is wrong.");
    const rsalt = randomHex(16);
    await env.DB.prepare("UPDATE users SET recovery_hash = ?, recovery_salt = ?, recovery_wrapped_dek = ? WHERE id = ?")
      .bind(await sha256Hex(rsalt, b64.decode(key32(body.recovery_auth, "recovery key"))), rsalt, blob(body.recovery_wrapped_dek, "key"), user.id).run();
    return json({ ok: true });
  }

  if (path === "account" && method === "DELETE") {
    if (!(await checkAuth(user, body.auth))) throw new HttpError(401, "Your password is wrong.");
    await env.DB.batch([
      env.DB.prepare("DELETE FROM chunks WHERE user_id = ?").bind(user.id),
      env.DB.prepare("DELETE FROM share_chunks WHERE share_id IN (SELECT id FROM shares WHERE user_id = ?)").bind(user.id),
      env.DB.prepare("DELETE FROM shares WHERE user_id = ?").bind(user.id),
      env.DB.prepare("DELETE FROM items WHERE user_id = ?").bind(user.id),
      env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(user.id),
      env.DB.prepare("DELETE FROM users WHERE id = ?").bind(user.id),
    ]);
    return json({ ok: true }, 200, { "set-cookie": cookie("x", 0) });
  }

  if (path === "items" && method === "GET") {
    const kind = new URL(request.url).searchParams.get("kind");
    const q = kind
      ? env.DB.prepare("SELECT id, kind, meta, size, updated_at FROM items WHERE user_id = ? AND kind = ? ORDER BY updated_at DESC").bind(user.id, kind)
      : env.DB.prepare("SELECT id, kind, meta, size, updated_at FROM items WHERE user_id = ? ORDER BY updated_at DESC").bind(user.id);
    return json({ items: (await q.all()).results, usage: await usage(env, user.id) });
  }

  if (path === "shares" && method === "GET") {
    const rows = (await env.DB.prepare("SELECT id, size, created_at, expires_at, views FROM shares WHERE user_id = ? AND expires_at > ? ORDER BY created_at DESC")
      .bind(user.id, Date.now()).all()).results;
    return json({ shares: rows });
  }
  if (path === "shares" && method === "POST") {
    if (typeof body.data !== "string") throw new HttpError(400, "Missing data.");
    const days = SHARE_DAYS.has(Number(body.days)) ? Number(body.days) : 30;
    const data = b64.decode(body.data);
    if (data.length > MAX_SHARE_BYTES) throw new HttpError(413, "This project is too large to share (over 25 MB, even compressed).");
    if (await tooMany(env, ["share:" + user.id], 100, 86400000)) throw new HttpError(429, "You've made a lot of links today. Try again tomorrow.");
    const now = Date.now();
    const active = await env.DB.prepare("SELECT COUNT(*) AS n FROM shares WHERE user_id = ? AND expires_at > ?").bind(user.id, now).first();
    if (active.n >= MAX_SHARES) throw new HttpError(413, "You have 200 active links. Remove some in Share → Your links first.");
    if (Math.random() < 0.1) {
      await env.DB.batch([
        env.DB.prepare("DELETE FROM share_chunks WHERE share_id IN (SELECT id FROM shares WHERE expires_at < ?)").bind(now),
        env.DB.prepare("DELETE FROM shares WHERE expires_at < ?").bind(now),
      ]);
    }
    const id = randomHex(16);
    const n = Math.max(1, Math.ceil(data.length / CHUNK));
    const expires = now + days * 86400000;
    const stmts = [env.DB.prepare("INSERT INTO shares (id, user_id, size, chunks, created_at, expires_at, views) VALUES (?, ?, ?, ?, ?, ?, 0)")
      .bind(id, user.id, data.length, n, now, expires)];
    for (let i = 0; i < n; i++) stmts.push(env.DB.prepare("INSERT INTO share_chunks (share_id, idx, data) VALUES (?, ?, ?)").bind(id, i, data.subarray(i * CHUNK, (i + 1) * CHUNK)));
    await env.DB.batch(stmts);
    await noteFailure(env, ["share:" + user.id]); // counts toward the daily limit
    return json({ id, created_at: now, expires_at: expires });
  }
  const dm = path.match(/^shares\/([0-9a-f]{32})$/);
  if (dm && method === "DELETE") {
    const own = await env.DB.prepare("SELECT id FROM shares WHERE id = ? AND user_id = ?").bind(dm[1], user.id).first();
    if (!own) throw new HttpError(404, "Link not found.");
    await env.DB.batch([
      env.DB.prepare("DELETE FROM share_chunks WHERE share_id = ?").bind(dm[1]),
      env.DB.prepare("DELETE FROM shares WHERE id = ?").bind(dm[1]),
    ]);
    return json({ ok: true });
  }

  const m = path.match(/^items\/([A-Za-z0-9_-]{1,64})$/);
  if (m) {
    const id = m[1];
    if (method === "GET") {
      const it = await env.DB.prepare("SELECT * FROM items WHERE user_id = ? AND id = ?").bind(user.id, id).first();
      if (!it) throw new HttpError(404, "Not found. It may have been deleted on another device.");
      const rows = (await env.DB.prepare("SELECT data FROM chunks WHERE user_id = ? AND item_id = ? ORDER BY idx").bind(user.id, id).all()).results;
      const parts = rows.map((r) => new Uint8Array(r.data));
      const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
      let o = 0;
      for (const p of parts) { all.set(p, o); o += p.length; }
      return json({ id, kind: it.kind, meta: it.meta, data: b64.encode(all), updated_at: it.updated_at });
    }
    if (method === "PUT") {
      if (!KINDS.has(body.kind)) throw new HttpError(400, "Unknown item type.");
      const meta = blob(body.meta, "metadata", 8192);
      if (typeof body.data !== "string") throw new HttpError(400, "Missing data.");
      const data = b64.decode(body.data);
      if (data.length > MAX_ITEM_BYTES) throw new HttpError(413, "This project is too large to store online (over 25 MB, even compressed).");
      const old = await env.DB.prepare("SELECT size, updated_at FROM items WHERE user_id = ? AND id = ?").bind(user.id, id).first();
      if (old && body.base_updated && Number(body.base_updated) !== old.updated_at && !body.force) {
        return json({ error: "This project was changed on another device or tab.", conflict: true, updated_at: old.updated_at }, 409);
      }
      const u = await usage(env, user.id);
      if (!old && u.items >= MAX_ITEMS) throw new HttpError(413, "You've reached the limit of 1000 saved items.");
      if (u.bytes - (old ? old.size : 0) + data.length > MAX_USER_BYTES) throw new HttpError(413, "Your online storage is full (200 MB). Delete some projects first.");
      const now = Math.max(Date.now(), old ? old.updated_at + 1 : 0);
      const n = Math.max(1, Math.ceil(data.length / CHUNK));
      const stmts = [
        env.DB.prepare("DELETE FROM chunks WHERE user_id = ? AND item_id = ?").bind(user.id, id),
        env.DB.prepare("INSERT OR REPLACE INTO items (user_id, id, kind, meta, size, chunks, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
          .bind(user.id, id, body.kind, meta, data.length, n, now),
      ];
      for (let i = 0; i < n; i++) {
        stmts.push(env.DB.prepare("INSERT INTO chunks (user_id, item_id, idx, data) VALUES (?, ?, ?, ?)").bind(user.id, id, i, data.subarray(i * CHUNK, (i + 1) * CHUNK)));
      }
      await env.DB.batch(stmts);
      return json({ id, updated_at: now, usage: await usage(env, user.id) });
    }
    if (method === "DELETE") {
      await env.DB.batch([
        env.DB.prepare("DELETE FROM chunks WHERE user_id = ? AND item_id = ?").bind(user.id, id),
        env.DB.prepare("DELETE FROM items WHERE user_id = ? AND id = ?").bind(user.id, id),
      ]);
      return json({ ok: true, usage: await usage(env, user.id) });
    }
  }
  throw new HttpError(404, "Unknown request.");
}

export async function onRequest(context) {
  const { request, env, params } = context;
  const path = (Array.isArray(params.path) ? params.path : [params.path]).filter(Boolean).join("/");
  try {
    if (!env.DB) throw new HttpError(503, "Accounts aren't set up on this server.");
    return await route(request, env, path);
  } catch (e) {
    if (e instanceof HttpError) return json({ error: e.message }, e.status);
    console.error(e);
    return json({ error: "Something went wrong on the server. Please try again." }, 500);
  }
}
