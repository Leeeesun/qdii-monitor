// Cloudflare Pages Function: /api/auth
// 支持：账号密码登录、带邀请码注册、通行密钥 (WebAuthn/Passkey ES256) 列表、绑定、删除与免密登录
// 依赖绑定：KV 命名空间 AUTH_KV；环境变量 INVITE_CODES（逗号分隔）、JWT_SECRET

const encoder = new TextEncoder();

function jsonRes(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" }
  });
}

function toHex(buffer) {
  return [...new Uint8Array(buffer)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64Url(bytes) {
  let bin = "";
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (let i = 0; i < arr.length; i++) bin += String.fromCharCode(arr[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlToBytes(b64) {
  const pad = b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64.length % 4)) % 4);
  const bin = atob(pad);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function toBase64Url(str) {
  return bytesToBase64Url(encoder.encode(str));
}

function fromBase64Url(b64) {
  return new TextDecoder().decode(base64UrlToBytes(b64));
}

async function sha256(data) {
  const bytes = typeof data === "string" ? encoder.encode(data) : data;
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}

async function hashPassword(password, salt) {
  return toHex(await sha256(`${salt}:${password}`));
}

async function signToken(payload, secret) {
  const body = toBase64Url(JSON.stringify(payload));
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
  return `${body}.${toHex(sig)}`;
}

async function verifyToken(token, secret) {
  if (!token || !token.includes(".")) return null;
  const [body, sig] = token.split(".");
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
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

function derToP1363(der) {
  const bytes = new Uint8Array(der);
  if (bytes[0] !== 0x30) throw new Error("Invalid DER signature");
  let offset = 2;
  if (bytes[1] & 0x80) offset += bytes[1] & 0x7f;

  if (bytes[offset] !== 0x02) throw new Error("Invalid DER r");
  const rLen = bytes[offset + 1];
  const rStart = offset + 2;
  const r = bytes.slice(rStart, rStart + rLen);

  offset = rStart + rLen;
  if (bytes[offset] !== 0x02) throw new Error("Invalid DER s");
  const sLen = bytes[offset + 1];
  const sStart = offset + 2;
  const s = bytes.slice(sStart, sStart + sLen);

  const out = new Uint8Array(64);
  const rTrim = r.length > 32 ? r.slice(r.length - 32) : r;
  const sTrim = s.length > 32 ? s.slice(s.length - 32) : s;
  out.set(rTrim, 32 - rTrim.length);
  out.set(sTrim, 64 - sTrim.length);
  return out;
}

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

async function getAuthedUser(request, env) {
  const secret = env.JWT_SECRET || "qdii-monitor-default-secret-change-me";
  const auth = request.headers.get("Authorization") || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  return await verifyToken(token, secret);
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const user = await getAuthedUser(request, env);
  if (!user) return jsonRes({ ok: false, message: "登录已失效" }, 401);
  return jsonRes({ ok: true, user });
}

export async function onRequestPost(context) {
  const { request, env } = context;
  const secret = env.JWT_SECRET || "qdii-monitor-default-secret-change-me";
  const rawCodes = env.INVITE_CODES || "";
  const validCodes = rawCodes.split(/[,，\s]+/).map(c => c.trim().toUpperCase()).filter(Boolean);

  if (!env.AUTH_KV) {
    return jsonRes({ ok: false, message: "尚未在 Cloudflare 后台绑定 AUTH_KV 数据库" }, 500);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonRes({ ok: false, message: "请求格式错误" }, 400);
  }

  const action = body.action;
  const url = new URL(request.url);
  const rpId = url.hostname;
  const expectedOrigin = url.origin;
  const exp = Date.now() + 7 * 24 * 3600 * 1000;

  // 1. 新用户注册（一次性邀请码）
  if (action === "register") {
    const username = String(body.username || "").trim();
    const password = String(body.password || "");
    const inviteCode = String(body.code || "").trim().toUpperCase();
    if (!inviteCode || !validCodes.includes(inviteCode)) {
      return jsonRes({ ok: false, message: "注册失败：请输入有效的邀请码" }, 403);
    }
    const inviteUsedKey = `used_invite:${inviteCode}`;
    if (await env.AUTH_KV.get(inviteUsedKey)) {
      return jsonRes({ ok: false, message: "注册失败：该邀请码已被使用" }, 403);
    }
    if (username.length < 2 || username.length > 20) {
      return jsonRes({ ok: false, message: "用户名长度需在 2 到 20 位之间" }, 400);
    }
    if (password.length < 6) {
      return jsonRes({ ok: false, message: "密码长度至少需要 6 位" }, 400);
    }
    const key = `user:${username.toLowerCase()}`;
    if (await env.AUTH_KV.get(key)) {
      return jsonRes({ ok: false, message: "该用户名已被注册，请直接登录" }, 409);
    }
    const salt = toHex(crypto.getRandomValues(new Uint8Array(8)));
    const hash = await hashPassword(password, salt);
    const nowIso = new Date().toISOString();
    await env.AUTH_KV.put(
      key,
      JSON.stringify({ username, salt, hash, invite_code: inviteCode, passkeys: [], created_at: nowIso })
    );
    await env.AUTH_KV.put(
      inviteUsedKey,
      JSON.stringify({ used_by: username, used_at: nowIso })
    );
    const user = { username, role: "user", exp };
    const token = await signToken(user, secret);
    return jsonRes({ ok: true, token, user });
  }

  // 2. 账号密码登录
  if (action === "login") {
    const username = String(body.username || "").trim();
    const password = String(body.password || "");
    if (!username || !password) return jsonRes({ ok: false, message: "请输入用户名和密码" }, 400);
    const key = `user:${username.toLowerCase()}`;
    const recordRaw = await env.AUTH_KV.get(key);
    if (!recordRaw) return jsonRes({ ok: false, message: "账号不存在或密码错误" }, 401);
    const record = JSON.parse(recordRaw);
    const hash = await hashPassword(password, record.salt);
    if (hash !== record.hash) return jsonRes({ ok: false, message: "账号不存在或密码错误" }, 401);
    const user = { username: record.username, role: "user", exp };
    const token = await signToken(user, secret);
    return jsonRes({ ok: true, token, user });
  }

  // 3. 通行密钥列表（需已登录）
  if (action === "passkey_list") {
    const curUser = await getAuthedUser(request, env);
    if (!curUser) return jsonRes({ ok: false, message: "登录已失效" }, 401);

    const uKey = `user:${curUser.username.toLowerCase()}`;
    const uRaw = await env.AUTH_KV.get(uKey);
    const uRec = uRaw ? JSON.parse(uRaw) : {};
    const ids = Array.isArray(uRec.passkeys) ? uRec.passkeys : [];
    const list = [];

    for (const id of ids) {
      const pkRaw = await env.AUTH_KV.get(`passkey:${id}`);
      if (pkRaw) {
        const pk = JSON.parse(pkRaw);
        list.push({
          id: pk.id,
          label: pk.label || "设备通行密钥",
          created_at: pk.created_at || ""
        });
      }
    }
    return jsonRes({ ok: true, passkeys: list });
  }

  // 4. 删除指定通行密钥（需已登录）
  if (action === "passkey_delete") {
    const curUser = await getAuthedUser(request, env);
    if (!curUser) return jsonRes({ ok: false, message: "登录已失效" }, 401);

    const targetId = String(body.id || "").trim();
    if (!targetId) return jsonRes({ ok: false, message: "缺少通行密钥 ID" }, 400);

    const pkRaw = await env.AUTH_KV.get(`passkey:${targetId}`);
    if (pkRaw) {
      const pk = JSON.parse(pkRaw);
      if (pk.username !== curUser.username) {
        return jsonRes({ ok: false, message: "无权删除他人的通行密钥" }, 403);
      }
      await env.AUTH_KV.delete(`passkey:${targetId}`);
    }

    const uKey = `user:${curUser.username.toLowerCase()}`;
    const uRaw = await env.AUTH_KV.get(uKey);
    if (uRaw) {
      const uRec = JSON.parse(uRaw);
      uRec.passkeys = (uRec.passkeys || []).filter(x => x !== targetId);
      await env.AUTH_KV.put(uKey, JSON.stringify(uRec));
    }
    return jsonRes({ ok: true, message: "已删除通行密钥" });
  }

  // 5. 通行密钥：获取绑定参数（需已登录）
  if (action === "passkey_reg_options") {
    const curUser = await getAuthedUser(request, env);
    if (!curUser) return jsonRes({ ok: false, message: "请先登录后再绑定通行密钥" }, 401);

    const challenge = bytesToBase64Url(crypto.getRandomValues(new Uint8Array(32)));
    await env.AUTH_KV.put(
      `chal:${challenge}`,
      JSON.stringify({ type: "reg", username: curUser.username, label: String(body.label || "").trim() }),
      { expirationTtl: 300 }
    );

    const userRaw = await env.AUTH_KV.get(`user:${curUser.username.toLowerCase()}`);
    const userRec = userRaw ? JSON.parse(userRaw) : { passkeys: [] };
    return jsonRes({
      ok: true,
      options: {
        challenge,
        rp: { name: "QDII 监控台", id: rpId },
        user: {
          id: toBase64Url(curUser.username.toLowerCase()),
          name: curUser.username,
          displayName: curUser.username
        },
        excludeCredentials: (userRec.passkeys || []).map(id => ({ id, type: "public-key" }))
      }
    });
  }

  // 6. 通行密钥：验证并保存绑定
  if (action === "passkey_reg_verify") {
    const curUser = await getAuthedUser(request, env);
    if (!curUser) return jsonRes({ ok: false, message: "登录状态已失效" }, 401);

    try {
      const { id, clientDataJSON, authenticatorData, publicKeySpki, label } = body;
      if (!id || !clientDataJSON || !authenticatorData || !publicKeySpki) {
        return jsonRes({ ok: false, message: "通行密钥参数不完整" }, 400);
      }

      const clientData = JSON.parse(fromBase64Url(clientDataJSON));
      if (clientData.type !== "webauthn.create" || clientData.origin !== expectedOrigin) {
        return jsonRes({ ok: false, message: "域名或验证类型不匹配" }, 400);
      }

      const chalKey = `chal:${clientData.challenge}`;
      const chalRaw = await env.AUTH_KV.get(chalKey);
      if (!chalRaw) return jsonRes({ ok: false, message: "验证已超时，请重试" }, 400);
      await env.AUTH_KV.delete(chalKey);
      const chalObj = JSON.parse(chalRaw);
      if (chalObj.type !== "reg" || chalObj.username !== curUser.username) {
        return jsonRes({ ok: false, message: "验证身份不匹配" }, 403);
      }

      const authDataBytes = base64UrlToBytes(authenticatorData);
      const rpHash = await sha256(rpId);
      if (!bytesEqual(authDataBytes.slice(0, 32), rpHash) || (authDataBytes[32] & 0x01) === 0) {
        return jsonRes({ ok: false, message: "设备验证数据校验失败" }, 400);
      }

      await crypto.subtle.importKey(
        "spki", base64UrlToBytes(publicKeySpki),
        { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]
      );

      const finalLabel = String(label || chalObj.label || "").trim() || "本机通行密钥";
      await env.AUTH_KV.put(
        `passkey:${id}`,
        JSON.stringify({ id, username: curUser.username, label: finalLabel, publicKeySpki, created_at: new Date().toISOString() })
      );

      const uKey = `user:${curUser.username.toLowerCase()}`;
      const uRaw = await env.AUTH_KV.get(uKey);
      if (uRaw) {
        const uRec = JSON.parse(uRaw);
        uRec.passkeys = Array.from(new Set([...(uRec.passkeys || []), id]));
        await env.AUTH_KV.put(uKey, JSON.stringify(uRec));
      }

      return jsonRes({ ok: true, message: "通行密钥已添加，下次可直接免密登录" });
    } catch (e) {
      return jsonRes({ ok: false, message: "通行密钥添加失败：" + e.message }, 400);
    }
  }

  // 7. 通行密钥：获取登录挑战
  if (action === "passkey_auth_options") {
    const challenge = bytesToBase64Url(crypto.getRandomValues(new Uint8Array(32)));
    await env.AUTH_KV.put(`chal:${challenge}`, JSON.stringify({ type: "auth" }), { expirationTtl: 300 });
    return jsonRes({ ok: true, options: { challenge, rpId } });
  }

  // 8. 通行密钥：验证签名并签发登录凭证
  if (action === "passkey_auth_verify") {
    try {
      const { id, clientDataJSON, authenticatorData, signature } = body;
      if (!id || !clientDataJSON || !authenticatorData || !signature) {
        return jsonRes({ ok: false, message: "登录凭证参数不完整" }, 400);
      }

      const clientDataBytes = base64UrlToBytes(clientDataJSON);
      const clientData = JSON.parse(new TextDecoder().decode(clientDataBytes));
      if (clientData.type !== "webauthn.get" || clientData.origin !== expectedOrigin) {
        return jsonRes({ ok: false, message: "域名或验证类型不匹配" }, 400);
      }

      const chalKey = `chal:${clientData.challenge}`;
      const chalRaw = await env.AUTH_KV.get(chalKey);
      if (!chalRaw) return jsonRes({ ok: false, message: "登录验证已超时，请重试" }, 400);
      await env.AUTH_KV.delete(chalKey);

      const pkRaw = await env.AUTH_KV.get(`passkey:${id}`);
      if (!pkRaw) return jsonRes({ ok: false, message: "该通行密钥已被移除或未绑定" }, 401);
      const pkRec = JSON.parse(pkRaw);

      const authDataBytes = base64UrlToBytes(authenticatorData);
      const rpHash = await sha256(rpId);
      if (!bytesEqual(authDataBytes.slice(0, 32), rpHash) || (authDataBytes[32] & 0x01) === 0) {
        return jsonRes({ ok: false, message: "设备验证数据无效" }, 401);
      }

      const clientDataHash = await sha256(clientDataBytes);
      const signedData = new Uint8Array(authDataBytes.length + clientDataHash.length);
      signedData.set(authDataBytes, 0);
      signedData.set(clientDataHash, authDataBytes.length);

      const pubKey = await crypto.subtle.importKey(
        "spki", base64UrlToBytes(pkRec.publicKeySpki),
        { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]
      );
      const rawSig = derToP1363(base64UrlToBytes(signature));
      const valid = await crypto.subtle.verify(
        { name: "ECDSA", hash: "SHA-256" }, pubKey, rawSig, signedData
      );
      if (!valid) return jsonRes({ ok: false, message: "通行密钥签名校验失败" }, 401);

      const user = { username: pkRec.username, role: "user", exp };
      const token = await signToken(user, secret);
      return jsonRes({ ok: true, token, user });
    } catch (e) {
      return jsonRes({ ok: false, message: "通行密钥登录失败：" + e.message }, 401);
    }
  }

  // 9. 云端同步：读取当前用户的推送设置与收藏夹
  if (action === "sync_get") {
    const curUser = await getAuthedUser(request, env);
    if (!curUser) return jsonRes({ ok: false, message: "登录已失效" }, 401);
    const uKey = `user:${curUser.username.toLowerCase()}`;
    const uRaw = await env.AUTH_KV.get(uKey);
    const uRec = uRaw ? JSON.parse(uRaw) : {};
    return jsonRes({
      ok: true,
      prefs: uRec.prefs || null,
      favorites: Array.isArray(uRec.favorites) ? uRec.favorites : null
    });
  }

  // 10. 云端同步：保存当前用户的推送设置与/或收藏夹
  if (action === "sync_set") {
    const curUser = await getAuthedUser(request, env);
    if (!curUser) return jsonRes({ ok: false, message: "登录已失效" }, 401);
    const uKey = `user:${curUser.username.toLowerCase()}`;
    const uRaw = await env.AUTH_KV.get(uKey);
    if (!uRaw) return jsonRes({ ok: false, message: "用户记录不存在" }, 404);
    const uRec = JSON.parse(uRaw);
    if (body.prefs && typeof body.prefs === "object") {
      uRec.prefs = body.prefs;
    }
    if (Array.isArray(body.favorites)) {
      uRec.favorites = body.favorites.slice(0, 200);
    }
    await env.AUTH_KV.put(uKey, JSON.stringify(uRec));
    return jsonRes({ ok: true, message: "已同步至云端" });
  }

  return jsonRes({ ok: false, message: "未知操作" }, 400);
}
