import json
import os
import re
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import requests
import yfinance as yf

OUTPUT_FILE = "data/market.json"
MAG7 = {
    "NVDA": "英伟达",
    "AAPL": "苹果",
    "MSFT": "微软",
    "AMZN": "亚马逊",
    "GOOGL": "谷歌",
    "META": "Meta",
    "TSLA": "特斯拉"
}


def get_close_series(ticker, period="18mo"):
    df = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=False)
    if df.empty:
        return pd.Series(dtype=float)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return close.dropna()


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def fetch_cnn_fear_greed():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://edition.cnn.com/markets/fear-and-greed"
    }
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            fg = res.json().get("fear_and_greed", {})
            score = fg.get("score")
            rating = fg.get("rating", "")
            if score is not None:
                return round(float(score), 1), str(rating)
    except Exception:
        pass
    return None, ""


def build_drawdown_series(close_series, days=252):
    sub = close_series.tail(days)
    rolling_max = sub.cummax()
    dd = ((sub - rolling_max) / rolling_max * 100).round(2)
    dates = [idx.strftime("%Y-%m-%d") for idx in sub.index]
    prices = [round(float(v), 2) for v in sub.values]
    dd_vals = [float(v) for v in dd.values]
    max_dd = min(dd_vals)
    max_dd_date = dates[dd_vals.index(max_dd)]
    return {
        "dates": dates,
        "prices": prices,
        "drawdowns": dd_vals,
        "current_dd": dd_vals[-1],
        "max_dd": max_dd,
        "max_dd_date": max_dd_date,
        "peak_price": round(float(rolling_max.iloc[-1]), 2)
    }


def eval_light(value, green_cond, red_cond):
    if value is None:
        return "yellow", "数据暂缺"
    if green_cond(value):
        return "green", "机会区间"
    if red_cond(value):
        return "red", "偏热谨慎"
    return "yellow", "中性观察"


def make_indicator(name, value_text, rule, detail, value, green_cond, red_cond):
    light, status_text = eval_light(value, green_cond, red_cond)
    return {
        "name": name,
        "value_text": value_text,
        "rule": rule,
        "detail": detail,
        "light": light,
        "status_text": status_text
    }


def generate_rule_commentary(summary, mag7_list, overall_light, ndx_dd, ma200_bias, tnx_chg_bp):
    ndx = summary["ndx"]
    spx = summary["spx"]
    dir_word = "收涨" if ndx["chg_pct"] >= 0 else "收跌"

    mag_desc = ""
    if mag7_list:
        mag_sorted = sorted(mag7_list, key=lambda x: x["chg_pct"], reverse=True)
        leader = mag_sorted[0]
        laggard = mag_sorted[-1]
        mag_desc = f"权重科技股方面，{leader['name']}({leader['symbol']}) 领涨 {leader['chg_pct']:+.2f}%，{laggard['name']}({laggard['symbol']}) 表现相对偏弱（{laggard['chg_pct']:+.2f}%）。"

    rate_desc = (
        f"10年期美债收益率回落 {abs(tnx_chg_bp):.1f} bp 至 {summary['tnx']['close']:.2f}%，对科技股估值形成支撑"
        if tnx_chg_bp <= 0
        else f"10年期美债收益率上行 {abs(tnx_chg_bp):.1f} bp 至 {summary['tnx']['close']:.2f}%，对高估值成长股构成一定压制"
    )

    if ndx_dd <= -10:
        pos_desc = f"当前纳指100较近一年高点回撤 {ndx_dd:.2f}%，已进入中深度回调区域，左侧分批布局赔率较高。"
    elif ndx_dd <= -3:
        pos_desc = f"当前纳指100较近一年高点回撤 {ndx_dd:.2f}%，处于常规震荡消化区间，适合保持既有定投节奏。"
    else:
        pos_desc = f"当前纳指100距离近一年高点仅差 {abs(ndx_dd):.2f}%，趋势强劲但短期安全边际偏薄，追高需控制单笔仓位。"

    light_map = {
        "green": "综合情绪亮起绿灯，市场恐慌情绪升温，可重点关注下方高额度、低费率基金适度增加定投额。",
        "yellow": "综合情绪维持黄灯中性状态，建议按原计划纪律定投，无需情绪化操作。",
        "red": "综合情绪亮起红灯，市场偏热或乖离偏大，建议维持基础定投或暂停额外加仓。"
    }

    return {
        "source": "rule",
        "headline": f"纳指100{dir_word} {abs(ndx['chg_pct']):.2f}%，报 {ndx['close']:,.2f} 点",
        "paragraphs": [
            f"行情概览：隔夜纳斯达克100指数{dir_word} {abs(ndx['chg_pct']):.2f}%（标普500变动 {spx['chg_pct']:+.2f}%）。{mag_desc}宏观层面，{rate_desc}。",
            f"技术与水位：指数现价较200日均线乖离率为 {ma200_bias:+.2f}%。{pos_desc}",
            f"定投参考：{light_map[overall_light]}"
        ]
    }


def generate_ai_commentary(payload):
    """可选：若在 GitHub Secrets 配置了 LLM_API_KEY 则自动调用大模型撰写点评"""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "deepseek-chat")

    prompt = (
        "你是理性克制的指数基金投研助手。请根据以下最新美股收盘 JSON 数据，撰写今日纳斯达克100收盘解读。"
        "要求：输出严格 JSON，包含 headline（20字内标题）和 paragraphs（3段字符串数组：1.行情与权重股/美债驱动 2.技术面与回撤水位 3.定投节奏建议），"
        "语言简洁客观，不夸张、不荐股、不使用任何 Emoji。\n\n"
        f"数据：{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        res = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "response_format": {"type": "json_object"}
            },
            timeout=25
        )
        if res.status_code != 200:
            return None
        content = res.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        parsed = json.loads(content)
        if parsed.get("headline") and isinstance(parsed.get("paragraphs"), list):
            parsed["source"] = "ai"
            return parsed
    except Exception:
        pass
    return None


def main():
    os.makedirs("data", exist_ok=True)

    ndx_s = get_close_series("^NDX", "18mo")
    spx_s = get_close_series("^GSPC", "18mo")
    vxn_s = get_close_series("^VXN", "3mo")
    tnx_s = get_close_series("^TNX", "1mo")

    def latest_stat(s):
        cur, prev = float(s.iloc[-1]), float(s.iloc[-2])
        chg_pct = round((cur - prev) / prev * 100, 2)
        return {"close": round(cur, 2), "chg_pct": chg_pct, "date": s.index[-1].strftime("%Y-%m-%d")}

    ndx_stat = latest_stat(ndx_s)
    spx_stat = latest_stat(spx_s)
    vxn_val = round(float(vxn_s.iloc[-1]), 2) if not vxn_s.empty else None
    tnx_cur = round(float(tnx_s.iloc[-1]), 2) if not tnx_s.empty else 0.0
    tnx_chg_bp = round(float(tnx_s.iloc[-1] - tnx_s.iloc[-2]) * 100, 1) if len(tnx_s) >= 2 else 0.0

    mag7_list = []
    for sym, cn_name in MAG7.items():
        s = get_close_series(sym, "5d")
        if len(s) >= 2:
            pct = round((float(s.iloc[-1]) - float(s.iloc[-2])) / float(s.iloc[-2]) * 100, 2)
            mag7_list.append({"symbol": sym, "name": cn_name, "close": round(float(s.iloc[-1]), 2), "chg_pct": pct})

    ma200 = float(ndx_s.rolling(200).mean().iloc[-1])
    ma60 = float(ndx_s.rolling(60).mean().iloc[-1])
    ma200_bias = round((ndx_stat["close"] - ma200) / ma200 * 100, 2)
    rsi14 = calc_rsi(ndx_s, 14)

    ndx_dd_data = build_drawdown_series(ndx_s, days=252)
    spx_dd_data = build_drawdown_series(spx_s, days=252)
    fg_score, fg_rating = fetch_cnn_fear_greed()

    indicators = [
        make_indicator(
            "CNN 恐慌与贪婪指数",
            f"{fg_score}" if fg_score is not None else "暂缺",
            "绿灯: 小于35 | 黄灯: 35-65 | 红灯: 大于65",
            f"当前情绪评级：{fg_rating or '未获取'}",
            fg_score, lambda v: v < 35, lambda v: v > 65
        ),
        make_indicator(
            "纳指波动率 (VXN)",
            f"{vxn_val}" if vxn_val is not None else "暂缺",
            "绿灯: 大于25 | 黄灯: 16-25 | 红灯: 小于16",
            "反映市场对纳指100未来30天波动的恐慌定价",
            vxn_val, lambda v: v > 25, lambda v: v < 16
        ),
        make_indicator(
            "纳指100 年线乖离率",
            f"{ma200_bias:+.2f}%",
            "绿灯: 小于0% | 黄灯: 0%-12% | 红灯: 大于12%",
            f"200日均线 {ma200:,.0f} / 60日均线 {ma60:,.0f}",
            ma200_bias, lambda v: v < 0, lambda v: v > 12
        ),
        make_indicator(
            "纳指100 强弱指标 (RSI14)",
            f"{rsi14}",
            "绿灯: 小于35 | 黄灯: 35-70 | 红灯: 大于70",
            "衡量短期超买与超卖动能",
            rsi14, lambda v: v < 35, lambda v: v > 70
        ),
        make_indicator(
            "年度动态回撤深度",
            f"{ndx_dd_data['current_dd']:.2f}%",
            "绿灯: 低于-10% | 黄灯: -10%至-3% | 红灯: 浅于-3%",
            f"近一年峰值 {ndx_dd_data['peak_price']:,.2f}，最大回撤 {ndx_dd_data['max_dd']:.2f}%",
            ndx_dd_data["current_dd"], lambda v: v <= -10, lambda v: v > -3
        )
    ]

    score_map = {"green": 1, "yellow": 0, "red": -1}
    total_score = sum(score_map[i["light"]] for i in indicators)
    if total_score >= 2:
        overall_light, overall_label = "green", "绿灯 · 左侧机会 / 适合积极定投"
    elif total_score <= -2:
        overall_light, overall_label = "red", "红灯 · 情绪偏热 / 严控追高节奏"
    else:
        overall_light, overall_label = "yellow", "黄灯 · 情绪中性 / 保持常规定投"

    summary = {
        "ndx": ndx_stat,
        "spx": spx_stat,
        "tnx": {"close": tnx_cur, "chg_bp": tnx_chg_bp},
        "ma200": round(ma200, 2),
        "ma60": round(ma60, 2)
    }

    ai_payload = {
        "summary": summary,
        "mag7": mag7_list,
        "ndx_drawdown": ndx_dd_data["current_dd"],
        "overall_sentiment": overall_label,
        "indicators": [{k: i[k] for k in ("name", "value_text", "light")} for i in indicators]
    }
    commentary = generate_ai_commentary(ai_payload) or generate_rule_commentary(
        summary, mag7_list, overall_light, ndx_dd_data["current_dd"], ma200_bias, tnx_chg_bp
    )

    tz_bj = timezone(timedelta(hours=8))
    output = {
        "updated_at": datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": ndx_stat["date"],
        "summary": summary,
        "mag7": mag7_list,
        "sentiment": {
            "overall_light": overall_light,
            "overall_label": overall_label,
            "indicators": indicators
        },
        "commentary": commentary,
        "drawdown": {
            "ndx": ndx_dd_data,
            "spx": spx_dd_data
        }
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
