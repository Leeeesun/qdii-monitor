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
  if (!env.AUTH_KV) {
    return Response.json({ ok: false, msg: "未绑定 AUTH_KV" }, { status: 500 });
  }

  const payload = await request.json().catch(() => ({}));
  const taskId = payload.taskId || "qdii-push-quota";
  const rawSettings = await env.AUTH_KV.get("push_settings");
  if (!rawSettings) {
    return Response.json({ ok: true, msg: "尚未在网页端保存推送配置，跳过" });
  }
  const settings = JSON.parse(rawSettings);
  const origin = new URL(request.url).origin;

  try {
    // 1. 额度变化检查（读取 data.nasdaq + data.sp500 中的 agency_limit_text / direct_limit_text）
    if (taskId === "qdii-push-quota") {
      if (settings.quotaEnabled === false) {
        return Response.json({ ok: true, msg: "额度变化提醒已关闭，跳过" });
      }
      const res = await fetch(`${origin}/data/funds.json?t=${Date.now()}`);
      if (!res.ok) throw new Error(`读取 funds.json 失败 (${res.status})`);
      const data = await res.json();
      const offFunds = Array.isArray(data)
        ? data
        : [
            ...(Array.isArray(data.nasdaq) ? data.nasdaq.map(x => ({ ...x, _pool: "纳指" })) : []),
            ...(Array.isArray(data.sp500) ? data.sp500.map(x => ({ ...x, _pool: "标普" })) : []),
            ...(Array.isArray(data.funds) ? data.funds : []),
            ...(Array.isArray(data.list) ? data.list : [])
          ];

      const prevRaw = await env.AUTH_KV.get("snapshot_quota");
      const prevMap = prevRaw ? JSON.parse(prevRaw) : null;
      const currMap = {};
      const changes = [];

      // 分渠道比对：只报变了的渠道，并标明直销/代销
      for (const f of offFunds) {
        const code = f.code || f.id || f.name;
        const agencyQ = String(f.agency_limit_text ?? f.quota ?? f.limit ?? f.status ?? "");
        const directQ = String(f.direct_limit_text ?? "");
        if (!code) continue;
        currMap[code] = { agency: agencyQ, direct: directQ };
        if (!prevMap || !(code in prevMap)) continue;
        const prev = prevMap[code];
        // 兼容旧版快照（旧版是 "代销 | 直销" 字符串）
        let prevAgency = "";
        let prevDirect = "";
        if (typeof prev === "string") {
          const parts = prev.split(" | ");
          prevAgency = parts[0] ?? "";
          prevDirect = parts[1] ?? "";
        } else if (prev && typeof prev === "object") {
          prevAgency = String(prev.agency ?? "");
          prevDirect = String(prev.direct ?? "");
        }
        const name = f.name || code;
        const show = (v) => (v === "" ? "—" : v);
        if (prevAgency !== agencyQ) {
          changes.push(`${name}(${code}) 代销：${show(prevAgency)} -> ${show(agencyQ)}`);
        }
        if (prevDirect !== directQ) {
          changes.push(`${name}(${code}) 直销：${show(prevDirect)} -> ${show(directQ)}`);
        }
      }

      await env.AUTH_KV.put("snapshot_quota", JSON.stringify(currMap));

      if (!prevMap) {
        return Response.json({ ok: true, msg: `首次初始化额度快照（共 ${Object.keys(currMap).length} 只基金），下次起监测变动` });
      }
      if (!changes.length) {
        return Response.json({ ok: true, msg: "本次检查无额度变化，未推送" });
      }

      const details = changes.slice(0, 12).join("\n") + (changes.length > 12 ? `\n...另有 ${changes.length - 12} 项变化，请打开监控台查看` : "");
      const content = `检测到 ${changes.length} 项额度变动：\n${details}`;
      await sendBark(settings, `QDII 限额变化提醒（${changes.length}项）`, content);
      return Response.json({ ok: true, msg: `已推送 Bark：${changes.length} 项额度变动` });
    }

    // 2. 场内高溢价检查（读取 data.etfs 数组中的 premium 字段）
    if (taskId === "qdii-push-premium") {
      if (settings.premiumEnabled === false) {
        return Response.json({ ok: true, msg: "场内高溢价提醒已关闭，跳过" });
      }
      const threshold = Number(settings.premiumThreshold ?? 3);
      const res = await fetch(`${origin}/data/funds.json?t=${Date.now()}`);
      if (!res.ok) throw new Error(`读取 funds.json 失败 (${res.status})`);
      const data = await res.json();
      const etfs = Array.isArray(data) ? data : (data.etfs || data.funds || data.list || []);

      const alerts = [];
      for (const f of etfs) {
        const p = Number(f.premium ?? f.premiumRate ?? f.discount_rt);
        if (!Number.isNaN(p) && p >= threshold) {
          alerts.push(`${f.name || f.code} (${f.code || ""}): 溢价 ${p.toFixed(2)}%`);
        }
      }

      if (!alerts.length) {
        return Response.json({ ok: true, msg: `无溢价 ≥ ${threshold}% 的场内 ETF，未推送` });
      }

      await sendBark(settings, `QDII 场内高溢价提醒 (≥${threshold}%)`, alerts.slice(0, 12).join("\n"));
      return Response.json({ ok: true, msg: `已推送 Bark：${alerts.length} 只高溢价 ETF` });
    }

    // 3. 美股收盘播报（从 market.json 的 summary / sentiment / commentary 拼装可读摘要）
    if (taskId === "qdii-push-market") {
      if (settings.marketEnabled === false) {
        return Response.json({ ok: true, msg: "美股收盘播报已关闭，跳过" });
      }
      const res = await fetch(`${origin}/data/market.json?t=${Date.now()}`);
      if (!res.ok) throw new Error(`读取 market.json 失败 (${res.status})`);
      const m = await res.json();
      const ndx = m.summary?.ndx;
      const spx = m.summary?.spx;
      const vix = m.summary?.vix;
      const fg = m.summary?.fear_greed;
      const lightLabel = m.sentiment?.overall_label || "";
      const headline = m.commentary?.headline || "";

      const parts = [];
      if (m.trade_date) parts.push(`交易日: ${m.trade_date}`);
      if (ndx?.close != null) {
        const chg = ndx.chg_pct != null ? ` (${ndx.chg_pct > 0 ? "+" : ""}${Number(ndx.chg_pct).toFixed(2)}%)` : "";
        parts.push(`纳指100: ${ndx.close}${chg}`);
      }
      if (spx?.close != null) {
        const chg = spx.chg_pct != null ? ` (${spx.chg_pct > 0 ? "+" : ""}${Number(spx.chg_pct).toFixed(2)}%)` : "";
        parts.push(`标普500: ${spx.close}${chg}`);
      }
      if (vix?.close != null) parts.push(`VIX: ${vix.close}`);
      if (fg?.score != null) parts.push(`恐慌贪婪: ${fg.score}`);
      if (lightLabel) parts.push(`信号: ${lightLabel}`);
      if (headline) parts.push(`摘要: ${headline}`);

      const summaryText = parts.length
        ? parts.join(" · ")
        : (typeof m.summary === "string" ? m.summary : (m.text || `标普/纳指数据已更新 (${m.trade_date || m.date || "今日"})`));

      await sendBark(settings, "美股大盘与情绪日报", String(summaryText).slice(0, 450));
      return Response.json({ ok: true, msg: "已推送 Bark：美股收盘日报" });
    }

    return Response.json({ ok: false, msg: `未知任务 ID: ${taskId}` }, { status: 400 });
  } catch (e) {
    return Response.json({ ok: false, msg: String(e?.message || e) }, { status: 500 });
  }
}
