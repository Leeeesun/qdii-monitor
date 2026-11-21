// Cloudflare Pages Function: /api/auth
// 依赖绑定：KV 命名空间 AUTH_KV；环境变量 INVITE_CODES（逗号分隔）、JWT_SECRET

const encoder = new TextEncoder();

function jsonRes(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" }
  });
}

function toHex(buffer) {
  return [...new Uint8Array(buffer)]
    .map(b => b.toString(16).padStart(2, "0"))
    .join("");
}

function toBase64Url(str) {
  const bytes = encoder.encode(str);
  let bin = "";
  bytes.forEach(b => (bin += String.fromCharCode(b)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(b64) {
  const pad = b64.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(pad);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

async function hashPassword(password, salt) {
  const data = encoder.encode(`${salt}:${password}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return toHex(digest);
}

async function signToken(payload, secret) {
  const body = toBase64Url(JSON.stringify(payload));
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
  return `${body}.${toHex(sig)}`;
}

async function verifyToken(token, secret) {
  if (!token || !token.includes(".")) return null;
  const [body, sig] = token.split(".");
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const expected = toHex(await crypto.subtle.sign("HMAC", key, encoder.encode(body)));
  if (sig !== expected) return null;
  try {
    const payload = JSON.parse(fromBase64Url(body));
    if (payload.exp && Date.now() > payload.exp) return null;
    return payload;
  } catch {
    return null;
  }
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const secret = env.JWT_SECRET || "qdii-monitor-default-secret-change-me";
  const auth = request.headers.get("Authorization") || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  const user = await verifyToken(token, secret);
  if (!user) return jsonRes({ ok: false, message: "登录已失效" }, 401);
  return jsonRes({ ok: true, user });
}

export async function onRequestPost(context) {
  const { request, env } = context;
  const secret = env.JWT_SECRET || "qdii-monitor-default-secret-change-me";
  const rawCodes = env.INVITE_CODES || "QDII2026,VIP888";
  const validCodes = rawCodes
    .split(/[,，\s]+/)
    .map(c => c.trim().toUpperCase())
    .filter(Boolean);

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonRes({ ok: false, message: "请求格式错误" }, 400);
  }

  const action = body.action;
  const exp = Date.now() + 7 * 24 * 3600 * 1000; // 7 天有效期


  if (!env.AUTH_KV) {
    return jsonRes(
      { ok: false, message: "尚未在 Cloudflare 后台绑定 AUTH_KV 数据库（当前可先使用邀请码登录）" },
      500
    );
  }

  const username = String(body.username || "").trim();
  const password = String(body.password || "");

  // 2. 新用户注册（强制校验邀请码）
  if (action === "register") {
    const inviteCode = String(body.code || "").trim().toUpperCase();
    if (!inviteCode || !validCodes.includes(inviteCode)) {
      return jsonRes({ ok: false, message: "注册失败：请输入有效的邀请码" }, 403);
    }
    if (username.length < 2 || username.length > 20) {
      return jsonRes({ ok: false, message: "用户名长度需在 2 到 20 位之间" }, 400);
    }
    if (password.length < 6) {
      return jsonRes({ ok: false, message: "密码长度至少需要 6 位" }, 400);
    }
    const key = `user:${username.toLowerCase()}`;
    const existing = await env.AUTH_KV.get(key);
    if (existing) {
      return jsonRes({ ok: false, message: "该用户名已被注册，请直接登录" }, 409);
    }
    const salt = toHex(crypto.getRandomValues(new Uint8Array(8)));
    const hash = await hashPassword(password, salt);
    await env.AUTH_KV.put(
      key,
      JSON.stringify({ username, salt, hash, invite_code: inviteCode, created_at: new Date().toISOString() })
    );
    const user = { username, role: "user", exp };
    const token = await signToken(user, secret);
    return jsonRes({ ok: true, token, user });
  }

  // 3. 账号密码登录
  if (action === "login") {
    if (!username || !password) {
      return jsonRes({ ok: false, message: "请输入用户名和密码" }, 400);
    }
    const key = `user:${username.toLowerCase()}`;
    const recordRaw = await env.AUTH_KV.get(key);
    if (!recordRaw) {
      return jsonRes({ ok: false, message: "账号不存在或密码错误" }, 401);
    }
    const record = JSON.parse(recordRaw);
    const hash = await hashPassword(password, record.salt);
    if (hash !== record.hash) {
      return jsonRes({ ok: false, message: "账号不存在或密码错误" }, 401);
    }
    const user = { username: record.username, role: "user", exp };
    const token = await signToken(user, secret);
    return jsonRes({ ok: true, token, user });
  }

  return jsonRes({ ok: false, message: "未知操作" }, 400);
}
