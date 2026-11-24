async function sendBark(settings, title, body) {
  let baseUrl = String(settings.barkUrl || "https://api.day.app").trim().replace(/\/+$/, "");
  let key = String(settings.barkKey || "").trim();

  // 兼容直接把完整链接（如 https://bark.youranzi.de/你的Key）填在 barkUrl 的情况
  if (!key) {
    const u = new URL(baseUrl);
    const parts = u.pathname.split("/").filter(Boolean);
    if (parts.length >= 1) {
      key = parts[0];
      baseUrl = u.origin;
    }
  }
  if (!key) throw new Error("未配置 Bark 密钥");

  const res = await fetch(`${baseUrl}/push`, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify({
      device_key: key,
      title,
      body,
      group: "QDII监控"
    })
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`Bark 返回 ${res.status}: ${txt.slice(0, 120)}`);
  }
}

export async function onRequestPost({ request, env }) {
  const secret = String(env.INTERNAL_SECRET || "").trim();
  const auth = request.headers.get("Authorization") || "";
  if (!secret || auth !== `Bearer ${secret}`) {
    return Response.json({ ok: false, msg: "Unauthorized" }, { status: 401 });
  }
  if (!env.QDII_AUTH) {
    return Response.json({ ok: false, msg: "未绑定 QDII_AUTH" }, { status: 500 });
  }

  const payload = await request.json().catch(() => ({}));
  const taskId = payload.taskId || "qdii-push-quota";
  const rawSettings = await env.QDII_AUTH.get("push_settings");
  if (!rawSettings) {
    return Response.json({ ok: true, msg: "尚未在网页端保存推送配置，跳过" });
  }
  const settings = JSON.parse(rawSettings);
  const origin = new URL(request.url).origin;

  try {
    // 1. 额度变化检查
    if (taskId === "qdii-push-quota") {
      const res = await fetch(`${origin}/data/funds.json?t=${Date.now()}`);
      if (!res.ok) throw new Error(`读取 funds.json 失败 (${res.status})`);
      const data = await res.json();
      const funds = Array.isArray(data) ? data : (data.funds || data.list || []);

      const prevRaw = await env.QDII_AUTH.get("snapshot_quota");
      const prevMap = prevRaw ? JSON.parse(prevRaw) : null;
      const currMap = {};
      const changes = [];

      for (const f of funds) {
        const code = f.code || f.id || f.name;
        const quota = String(f.quota ?? f.limit ?? f.status ?? "");
        if (!code) continue;
        currMap[code] = quota;
        if (prevMap && code in prevMap && prevMap[code] !== quota) {
          changes.push(`${f.name || code}: ${prevMap[code] || "无"} → ${quota || "无"}`);
        }
      }

      await env.QDII_AUTH.put("snapshot_quota", JSON.stringify(currMap));

      if (!prevMap) {
        return Response.json({ ok: true, msg: `首次初始化额度快照（共 ${Object.keys(currMap).length} 只基金），下次起监测变动` });
      }
      if (!changes.length) {
        return Response.json({ ok: true, msg: "本次检查无额度变化，未推送" });
      }

      const content = changes.slice(0, 10).join("\n") + (changes.length > 10 ? `\n...等共 ${changes.length} 只` : "");
      await sendBark(settings, `QDII 额度变动 (${changes.length}只)`, content);
      return Response.json({ ok: true, msg: `已推送 Bark：${changes.length} 只基金额度变动` });
    }

    // 2. 场内高溢价检查
    if (taskId === "qdii-push-premium") {
      const threshold = Number(settings.premiumThreshold ?? 3);
      const res = await fetch(`${origin}/data/funds.json?t=${Date.now()}`);
      if (!res.ok) throw new Error(`读取 funds.json 失败 (${res.status})`);
      const data = await res.json();
      const funds = Array.isArray(data) ? data : (data.funds || data.list || []);

      const alerts = [];
      for (const f of funds) {
        const p = Number(f.premium ?? f.premiumRate ?? f.discount_rt);
        if (!Number.isNaN(p) && p >= threshold) {
          alerts.push(`${f.name || f.code}: 溢价 ${p.toFixed(2)}%`);
        }
      }

      if (!alerts.length) {
        return Response.json({ ok: true, msg: `无溢价 ≥ ${threshold}% 的基金，未推送` });
      }

      await sendBark(settings, `QDII 场内高溢价提醒 (≥${threshold}%)`, alerts.slice(0, 12).join("\n"));
      return Response.json({ ok: true, msg: `已推送 Bark：${alerts.length} 只高溢价基金` });
    }

    // 3. 美股收盘播报
    if (taskId === "qdii-push-market") {
      const res = await fetch(`${origin}/data/market.json?t=${Date.now()}`);
      if (!res.ok) throw new Error(`读取 market.json 失败 (${res.status})`);
      const m = await res.json();
      const summary = m.summary || m.text || `标普/纳指数据已更新 (${m.date || m.updatedAt || "今日"})`;
      await sendBark(settings, "美股大盘与情绪日报", String(summary).slice(0, 400));
      return Response.json({ ok: true, msg: "已推送 Bark：美股收盘日报" });
    }

    return Response.json({ ok: false, msg: `未知任务 ID: ${taskId}` }, { status: 400 });
  } catch (e) {
    return Response.json({ ok: false, msg: String(e?.message || e) }, { status: 500 });
  }
}