const TASK_DEFS = {
  "qdii-push-quota": {
    name: "QDII 推送 - 额度变化",
    defaultHour: 9,
    defaultMinute: 30,
    defaultDays: [1, 2, 3, 4, 5],
    defaultHolidayMode: "cn_workday"
  },
  "qdii-push-premium": {
    name: "QDII 推送 - 场内高溢价",
    defaultHour: 10,
    defaultMinute: 0,
    defaultDays: [1, 2, 3, 4, 5],
    defaultHolidayMode: "cn_workday"
  },
  "qdii-push-market": {
    name: "QDII 推送 - 美股收盘",
    defaultHour: 6,
    defaultMinute: 45,
    defaultDays: [2, 3, 4, 5, 6],
    defaultHolidayMode: "none"
  }
};

const encoder = new TextEncoder();

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

function fromBase64Url(b64) {
  return new TextDecoder().decode(base64UrlToBytes(b64));
}

function hexToBytes(hex) {
  if (typeof hex !== "string" || hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

async function verifyToken(token, secret) {
  if (!token || !token.includes(".")) return null;
  const [body, sig] = token.split(".");
  const sigBytes = hexToBytes(sig);
  if (!sigBytes) return null;
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const expected = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
  if (!bytesEqual(sigBytes, new Uint8Array(expected))) return null;
  try {
    const payload = JSON.parse(fromBase64Url(body));
    if (!payload.exp || typeof payload.exp !== "number" || Date.now() > payload.exp) return null;
    return payload;
  } catch {
    return null;
  }
}

function isSuperAdmin(username, env) {
  const u = String(username || "").trim().toLowerCase();
  if (!u) return false;
  const extra = String(env.ADMIN_USERS || "lesun")
    .split(/[,，\s]+/)
    .map(s => s.trim().toLowerCase())
    .filter(Boolean);
  return u === "lesun" || extra.includes(u);
}

// 推送配置涉及 Bark 密钥与调度中心任务，必须是已登录的管理员才能读写。
// 未配置 JWT_SECRET 时直接拒绝（fail closed），绝不用硬编码默认值。
async function getAuthedAdmin(request, env) {
  const secret = env.JWT_SECRET || "";
  if (!secret) return null;
  const auth = request.headers.get("Authorization") || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  const user = await verifyToken(token, secret);
  if (!user) return null;
  if (!isSuperAdmin(user.username, env)) return null;
  return user;
}

function parseTimeStr(str, fallbackH, fallbackM) {
  const m = String(str || "").match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return { hour: fallbackH, minute: fallbackM };
  const hour = Math.min(23, Math.max(0, Number(m[1])));
  const rawMin = Number(m[2]);
  const minute = [0, 15, 30, 45].includes(rawMin) ? rawMin : Math.floor(rawMin / 15) * 15;
  return { hour, minute };
}

function formatTimeStr(hour, minute) {
  return `${String(hour ?? 9).padStart(2, "0")}:${String(minute ?? 0).padStart(2, "0")}`;
}

async function fetchHubTasks(env) {
  const base = (env.CRON_HUB_BASE || "https://cron.youranzi.de").replace(/\/+$/, "");
  const res = await fetch(`${base}/api/internal/tasks?prefix=qdii-push-`, {
    headers: { Authorization: `Bearer ${env.INTERNAL_SECRET || ""}` }
  });
  if (!res.ok) return {};
  const data = await res.json();
  const map = {};
  for (const t of data.tasks || []) map[t.id] = t;
  return map;
}

async function upsertHubTask(env, taskId, payload) {
  const base = (env.CRON_HUB_BASE || "https://cron.youranzi.de").replace(/\/+$/, "");
  const res = await fetch(`${base}/api/internal/tasks/${encodeURIComponent(taskId)}`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${env.INTERNAL_SECRET || ""}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`同步 ${taskId} 失败 (${res.status}): ${errText}`);
  }
  return res.json();
}

export async function onRequestGet({ request, env }) {
  const admin = await getAuthedAdmin(request, env);
  if (!admin) {
    return Response.json({ ok: false, message: "未登录或无管理员权限" }, { status: 401 });
  }
  if (!env.AUTH_KV) {
    return Response.json({ ok: false, message: "未绑定 AUTH_KV" }, { status: 500 });
  }

  const raw = await env.AUTH_KV.get("push_settings");
  const local = raw ? JSON.parse(raw) : {
    barkUrl: "",
    barkKey: "",
    premiumThreshold: 3,
    quotaEnabled: false,
    quotaTime: "09:30",
    premiumEnabled: false,
    premiumTime: "10:00",
    marketEnabled: false,
    marketTime: "06:45"
  };

  // 从调度中心拉取最新时间与开关（以调度中心为唯一数据源）
  try {
    const hubMap = await fetchHubTasks(env);
    if (hubMap["qdii-push-quota"]) {
      const t = hubMap["qdii-push-quota"];
      local.quotaEnabled = Boolean(t.enabled);
      local.quotaTime = formatTimeStr(t.hour, t.minute);
    }
    if (hubMap["qdii-push-premium"]) {
      const t = hubMap["qdii-push-premium"];
      local.premiumEnabled = Boolean(t.enabled);
      local.premiumTime = formatTimeStr(t.hour, t.minute);
    }
    if (hubMap["qdii-push-market"]) {
      const t = hubMap["qdii-push-market"];
      local.marketEnabled = Boolean(t.enabled);
      local.marketTime = formatTimeStr(t.hour, t.minute);
    }
  } catch (e) {
    return Response.json({ ok: true, settings: local, warning: "读取调度中心失败，已显示本地缓存" });
  }

  return Response.json({ ok: true, settings: local });
}

export async function onRequestPost({ request, env }) {
  const admin = await getAuthedAdmin(request, env);
  if (!admin) {
    return Response.json({ ok: false, message: "未登录或无管理员权限" }, { status: 401 });
  }
  if (!env.AUTH_KV) {
    return Response.json({ ok: false, message: "未绑定 AUTH_KV" }, { status: 500 });
  }
  if (!env.INTERNAL_SECRET) {
    return Response.json({ ok: false, message: "未配置 INTERNAL_SECRET" }, { status: 500 });
  }

  const body = await request.json();
  const origin = new URL(request.url).origin;
  const cronUrl = `${origin}/api/cron`;
  const authHeaderJson = JSON.stringify({ Authorization: `Bearer ${env.INTERNAL_SECRET}` });

  // 1. 业务配置写入 AUTH_KV
  await env.AUTH_KV.put("push_settings", JSON.stringify(body));

  // 2. 定时规则与开关同步到 cron.youranzi.de
  const syncList = [
    { id: "qdii-push-quota", enabled: Boolean(body.quotaEnabled), time: body.quotaTime },
    { id: "qdii-push-premium", enabled: Boolean(body.premiumEnabled), time: body.premiumTime },
    { id: "qdii-push-market", enabled: Boolean(body.marketEnabled), time: body.marketTime }
  ];

  const hubMap = await fetchHubTasks(env).catch(() => ({}));

  for (const item of syncList) {
    const def = TASK_DEFS[item.id];
    const existing = hubMap[item.id];
    const { hour, minute } = parseTimeStr(item.time, def.defaultHour, def.defaultMinute);

    await upsertHubTask(env, item.id, {
      name: existing?.name || def.name,
      taskType: "http",
      httpMethod: "POST",
      httpUrl: cronUrl,
      httpHeaders: authHeaderJson,
      httpBody: JSON.stringify({ taskId: item.id }),
      timezone: existing?.timezone || "Asia/Shanghai",
      hour,
      minute,
      scheduleType: existing?.scheduleType || "weekly",
      days: existing?.days || def.defaultDays,
      interval: existing?.interval || 1,
      holidayMode: existing?.holidayMode || def.defaultHolidayMode,
      holidayCountries: existing?.holidayCountries || [],
      enabled: item.enabled
    });
  }

  return Response.json({ ok: true, message: "配置已保存并同步至调度中心" });
}
