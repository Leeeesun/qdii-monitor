import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
import akshare as ak
import pandas as pd
import requests
from bs4 import BeautifulSoup

NASDAQ_KW = r"纳斯达克|纳指"
SP500_KW = r"标普"
EXCLUDE_KW = r"美元|美钞|美汇|现汇|现钞|港币|港股|香港|中国|石油|天然气|消费|生物|医药|信息|科技精选|油气|医疗|黄金|商品|原油|房地产|红利"

LIMIT_TITLE_KWS = [
    "大额申购", "暂停申购", "恢复申购", "申购限额",
    "金额限制", "限制申购", "申购上限", "限额"
]

# 16 只主流纳指 / 标普场内 ETF（带交易所前缀，直接走腾讯极速行情接口，避免 akshare 挂起）
TRACKED_ETFS = [
    {"code": "159509", "mkt": "sz", "name": "景顺长城纳斯达克科技ETF(QDII)", "index": "纳斯达克科技市值加权"},
    {"code": "159501", "mkt": "sz", "name": "嘉实纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "159696", "mkt": "sz", "name": "易方达纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "513100", "mkt": "sh", "name": "国泰纳斯达克100ETF", "index": "纳斯达克100"},
    {"code": "159659", "mkt": "sz", "name": "招商纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "159941", "mkt": "sz", "name": "广发纳斯达克100ETF", "index": "纳斯达克100"},
    {"code": "513870", "mkt": "sh", "name": "富国纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "513300", "mkt": "sh", "name": "华夏纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "513390", "mkt": "sh", "name": "博时纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "159660", "mkt": "sz", "name": "汇添富纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "159632", "mkt": "sz", "name": "华安纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "513110", "mkt": "sh", "name": "华泰柏瑞纳斯达克100ETF", "index": "纳斯达克100"},
    {"code": "159513", "mkt": "sz", "name": "大成纳斯达克100ETF(QDII)", "index": "纳斯达克100"},
    {"code": "513500", "mkt": "sh", "name": "博时标普500ETF", "index": "标普500"},
    {"code": "159612", "mkt": "sz", "name": "国泰标普500ETF(QDII)", "index": "标普500"},
    {"code": "513650", "mkt": "sh", "name": "南方标普500ETF(QDII)", "index": "标普500"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/"
}

CACHE_FILE = "data/fees_cache.json"
OVERRIDE_FILE = "data/direct_overrides.json"
OUTPUT_FILE = "data/funds.json"
CACHE_TTL_DAYS = 7


def fetch_with_retry(func, retries=3, delay=3, **kwargs):
    for i in range(retries):
        try:
            return func(**kwargs)
        except Exception as e:
            if i == retries - 1:
                raise e
            time.sleep(delay)


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def parse_percent(val):
    if val is None or pd.isna(val):
        return 0.0
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", str(val))
    return round(float(m.group(1)), 4) if m else 0.0


def safe_float(val, digits=2):
    if val is None or pd.isna(val) or str(val).strip() in ("", "--", "-"):
        return None
    try:
        return round(float(str(val).replace("%", "").replace(",", "")), digits)
    except Exception:
        return None


def detect_share_class(name):
    m = re.search(r"(?:人民币|\)|\s)([ACDEFI])(?:人民币|\s|\(|$)", name.upper())
    if m:
        return m.group(1)
    if name.upper().endswith(("A", "C", "D", "E", "F", "I")):
        return name.upper()[-1]
    return "其他"


def format_limit(val, status):
    st = str(status)
    if "暂停" in st:
        return "暂停申购", 0
    try:
        num = float(val)
        if pd.isna(num):
            return "限大额(见公告)", -1
        if num >= 1e9:
            return "不限额", 999999999
        if num <= 0 and "限大额" in st:
            return "限大额(其他平台)", -1
        return f"{int(num):,} 元", int(num)
    except (ValueError, TypeError):
        return "未知", -1


def parse_cn_amount(num_str, unit):
    try:
        val = float(num_str.replace(",", ""))
        if unit == "万":
            val *= 10000
        elif unit == "千":
            val *= 1000
        return int(round(val))
    except Exception:
        return None


def extract_limit_from_text(text, share_class=""):
    if not text:
        return None
    clean = re.sub(r"\s+", "", text)
    if re.search(r"(?:取消|恢复)[^。；]{0,20}(?:大额申购|金额限制|申购限额)", clean) and not re.search(
        r"(?:不超过|不得超过|上限为|调整为|调整至|限制为|限定为|限额为|设定为)(?:人民币)?[\d,]+(?:\.\d+)?(?:万|千)?元", clean
    ):
        return 999999999

    patterns = []
    if share_class in ("A", "C", "D", "E", "F", "I"):
        patterns.append(
            rf"{share_class}类[^。；]{{0,80}}?(?:不超过|不得超过|上限为|调整为|调整至|限制为|限定为|限额为|设定为|金额限制为|金额为)(?:人民币)?([\d,]+(?:\.\d+)?)(万|千)?元"
        )
    patterns.extend([
        r"(?:单日单个基金账户|单个基金账户|每个基金账户|累计申购|大额申购|申购金额|申购限额|限制申购金额|金额限制)[^。；]{0,60}?(?:不超过|不得超过|上限为|调整为|调整至|限制为|限定为|限额为|设定为|为)(?:人民币)?([\d,]+(?:\.\d+)?)(万|千)?元",
        r"(?:不超过|不得超过|上限为|调整为|调整至|限额为|设定为)(?:人民币)?([\d,]+(?:\.\d+)?)(万|千)?元"
    ])
    for pat in patterns:
        m = re.search(pat, clean)
        if m:
            amt = parse_cn_amount(m.group(1), m.group(2))
            if amt and 1 <= amt <= 100000000:
                return amt
    return None


def fetch_notice_content(art_code, notices_cache):
    if not art_code:
        return ""
    if art_code in notices_cache:
        return notices_cache[art_code]
    url = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
    try:
        res = requests.get(
            url, params={"art_code": art_code, "client_source": "web", "page_index": 1},
            headers=HEADERS, timeout=8
        )
        data = res.json().get("data") or {}
        content = (data.get("notice_content") or data.get("attach_content") or "")[:3500]
        if content:
            notices_cache[art_code] = content
        return content
    except Exception:
        return ""


def fetch_latest_limit_notice(code, share_class, notices_cache, need_body=False):
    url = "https://api.fund.eastmoney.com/f10/JJGG"
    for notice_type in (5, 0):
        params = {"fundcode": code, "pageIndex": 1, "pageSize": 20, "type": notice_type}
        try:
            res = requests.get(url, params=params, headers=HEADERS, timeout=6)
            items = res.json().get("Data", [])
            for item in items:
                title = item.get("TITLE", "")
                if any(k in title for k in LIMIT_TITLE_KWS):
                    art_code = item.get("ID", "")
                    has_diff = ("直销" in title or "代销" in title or "非直销" in title)
                    parsed_limit = None
                    if need_body or has_diff:
                        body = fetch_notice_content(art_code, notices_cache)
                        parsed_limit = extract_limit_from_text(body, share_class)
                    return {
                        "title": title,
                        "date": str(item.get("PUBLISHDATE", ""))[:10],
                        "has_channel_diff": has_diff,
                        "parsed_limit": parsed_limit
                    }
            if items:
                break
        except Exception:
            pass
    return {"title": "", "date": "", "has_channel_diff": False, "parsed_limit": None}


def fetch_pingzhong_extra(code):
    """通过 pingzhongdata 一次获取：近1年收益、最新规模(亿)与近期净值序列"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    ret_1y, scale_yi, nav_map = None, None, {}
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        txt = res.text
        m_1y = re.search(r'var\s+syl_1n\s*=\s*"([^"]+)"', txt)
        if m_1y and m_1y.group(1).strip() != "":
            ret_1y = round(float(m_1y.group(1)), 2)
        m_sc = re.search(r"var\s+Data_fluctuationScale\s*=\s*(\{.*?\});", txt)
        if m_sc:
            sc_obj = json.loads(m_sc.group(1))
            series = sc_obj.get("series") or []
            if series and series[-1].get("y") is not None:
                scale_yi = round(float(series[-1]["y"]), 2)
        m_nav = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", txt)
        if m_nav:
            arr = json.loads(m_nav.group(1))[-120:]
            tz_bj = timezone(timedelta(hours=8))
            for item in arr:
                d_str = datetime.fromtimestamp(item["x"] / 1000, tz_bj).strftime("%Y-%m-%d")
                nav_map[d_str] = round(float(item["y"]), 4)
    except Exception:
        pass
    return ret_1y, scale_yi, nav_map


def fetch_fund_fees(code):
    url = f"https://fundf10.eastmoney.com/jjfl_{code}.html"
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.encoding = "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    mgmt_m = re.search(r"管理费率[^\d]{0,12}([\d.]+)%", text)
    cust_m = re.search(r"托管费率[^\d]{0,12}([\d.]+)%", text)
    serv_m = re.search(r"销售服务费率[^\d]{0,12}([\d.]+)%", text)

    mgmt = float(mgmt_m.group(1)) if mgmt_m else 0.0
    cust = float(cust_m.group(1)) if cust_m else 0.0
    serv = float(serv_m.group(1)) if serv_m else 0.0

    purchase_orig, purchase_disc = 0.0, 0.0
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if any("申购" in h for h in headers) and any("费率" in h for h in headers):
            rows = table.find_all("tr")
            if len(rows) > 1:
                cols = [td.get_text(" ", strip=True) for td in rows[1].find_all("td")]
                if cols:
                    rates = re.findall(r"(\d+(?:\.\d+)?)%", cols[-1])
                    if len(rates) >= 2:
                        purchase_orig, purchase_disc = float(rates[0]), float(rates[-1])
                    elif len(rates) == 1:
                        purchase_orig = purchase_disc = float(rates[0])
            break

    _, scale_yi, _ = fetch_pingzhong_extra(code)
    return {
        "mgmt_fee": round(mgmt, 4),
        "custody_fee": round(cust, 4),
        "mgmt_custody_fee": round(mgmt + cust, 4),
        "service_fee": round(serv, 4),
        "purchase_fee_orig": round(purchase_orig, 4),
        "purchase_fee": round(purchase_disc, 4),
        "scale": scale_yi,
        "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%d")
    }


def fetch_etf_quotes_fast():
    """通过腾讯极速行情接口一次性拉取 16 只场内 ETF 现价/涨跌/成交额（<0.5秒，绝不超时）"""
    syms = ",".join(f"{e['mkt']}{e['code']}" for e in TRACKED_ETFS)
    quotes = {}
    try:
        res = requests.get(f"https://qt.gtimg.cn/q={syms}", headers=HEADERS, timeout=8)
        for line in res.text.strip().splitlines():
            m = re.search(r'v_(?:sh|sz)(\d{6})="([^"]+)"', line)
            if not m:
                continue
            code = m.group(1)
            parts = m.group(2).split("~")
            if len(parts) > 38:
                price = safe_float(parts[3], 3)
                chg_pct = safe_float(parts[32], 2)
                # parts[37] 为成交额(万元)
                amt_wan = safe_float(parts[37], 2)
                turnover_yi = round(amt_wan / 10000, 2) if amt_wan is not None else None
                quotes[code] = {"price": price, "chg_pct": chg_pct, "turnover": turnover_yi}
    except Exception:
        pass
    return quotes


def fetch_etf_kline_fast(mkt, code):
    """通过腾讯日K接口拉取近 100 个交易日收盘价（带严格 6 秒超时）"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={mkt}{code},day,,,100,"
    kline = []
    try:
        res = requests.get(url, headers=HEADERS, timeout=6)
        data = res.json().get("data", {}).get(f"{mkt}{code}", {})
        rows = data.get("day") or data.get("qfqday") or []
        for r in rows:
            if len(r) >= 3:
                kline.append((r[0], float(r[2])))
    except Exception:
        pass
    return kline


def fetch_etf_list(fees_cache, today_utc):
    quotes = fetch_etf_quotes_fast()
    etf_results = []

    for meta in TRACKED_ETFS:
        code, mkt = meta["code"], meta["mkt"]
        q = quotes.get(code, {})
        price, chg_pct, turnover = q.get("price"), q.get("chg_pct"), q.get("turnover")

        fee_info = fees_cache.get(code)
        need_refresh = True
        if isinstance(fee_info, dict) and "cached_at" in fee_info and fee_info.get("mgmt_custody_fee", 0) > 0:
            try:
                cached_dt = datetime.strptime(fee_info["cached_at"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (today_utc - cached_dt).days < CACHE_TTL_DAYS and fee_info.get("scale") is not None:
                    need_refresh = False
            except Exception:
                pass
        if need_refresh:
            try:
                fee_info = fetch_fund_fees(code)
                if fee_info["mgmt_custody_fee"] > 0:
                    fees_cache[code] = fee_info
            except Exception:
                fee_info = fee_info or {"mgmt_custody_fee": 0.6, "scale": None}

        ret_1y, scale_fb, nav_map = fetch_pingzhong_extra(code)
        scale_val = fee_info.get("scale") if fee_info.get("scale") is not None else scale_fb

        kline = fetch_etf_kline_fast(mkt, code)
        prem_dates, prem_vals = [], []
        for d_str, close_p in kline:
            if d_str in nav_map and nav_map[d_str] > 0:
                prem_dates.append(d_str)
                prem_vals.append(round((close_p - nav_map[d_str]) / nav_map[d_str] * 100, 2))

        latest_nav_date = max(nav_map.keys()) if nav_map else ""
        latest_nav = nav_map.get(latest_nav_date)
        if price is None and kline:
            price = round(kline[-1][1], 3)

        premium = None
        if price and latest_nav and latest_nav > 0:
            premium = round((price - latest_nav) / latest_nav * 100, 2)
        elif prem_vals:
            premium = prem_vals[-1]

        prem_stats = {"min": None, "max": None, "percentile": None}
        if prem_vals and premium is not None:
            prem_stats["min"] = min(prem_vals)
            prem_stats["max"] = max(prem_vals)
            prem_stats["percentile"] = int(round(sum(1 for v in prem_vals if v <= premium) / len(prem_vals) * 100))

        etf_results.append({
            "code": code,
            "name": meta["name"],
            "index": meta["index"],
            "price": price,
            "nav": latest_nav,
            "nav_date": latest_nav_date,
            "chg_pct": chg_pct,
            "premium": premium,
            "scale": scale_val,
            "return_1y": ret_1y,
            "mgmt_custody_fee": fee_info.get("mgmt_custody_fee", 0.0),
            "turnover": turnover,
            "detail_url": f"https://fund.eastmoney.com/{code}.html",
            "premium_history": {"dates": prem_dates[-90:], "values": prem_vals[-90:], "stats": prem_stats}
        })

    etf_results.sort(key=lambda x: (x["premium"] if x["premium"] is not None else -999), reverse=True)
    return etf_results


def build_active_top5(purchase_df, rank_df):
    """筛选近一年涨幅 TOP 5 主动型 QDII 基金（排除被动指数/联接/债基/美元份额）"""
    try:
        qdii_rank = fetch_with_retry(ak.fund_open_fund_rank_em, symbol="QDII")
        qdii_rank["基金代码"] = qdii_rank["基金代码"].astype(str).str.zfill(6)
        m = pd.merge(
            qdii_rank[["基金代码", "基金简称", "近1年", "日期"]],
            purchase_df[["基金代码", "申购状态", "日累计限定金额"]],
            on="基金代码", how="left"
        )
        # 仅保留 A 类或无后缀人民币主动权益/混合 QDII
        exclude_passive = r"指数|ETF|联接|债|美元|美钞|美汇|现汇|现钞|港币|原油|黄金|商品|REIT|C$|D$|E$|I$"
        mask = ~m["基金简称"].str.contains(exclude_passive, regex=True, na=False)
        m = m[mask].copy()
        m["y1_num"] = pd.to_numeric(m["近1年"], errors="coerce")
        m = m.dropna(subset=["y1_num"]).sort_values("y1_num", ascending=False).head(5)

        top5 = []
        for idx, (_, r) in enumerate(m.iterrows(), 1):
            code = r["基金代码"]
            limit_text, _ = format_limit(r["日累计限定金额"], r["申购状态"])
            _, scale_yi, _ = fetch_pingzhong_extra(code)
            top5.append({
                "rank": idx,
                "code": code,
                "name": str(r["基金简称"]),
                "return_1y": round(float(r["y1_num"]), 2),
                "nav_date": str(r["日期"]),
                "scale": scale_yi,
                "limit_text": limit_text,
                "url": f"https://fundf10.eastmoney.com/jjgg_{code}_5.html"
            })
        return top5
    except Exception:
        return []


def main():
    os.makedirs("data", exist_ok=True)
    fees_cache = load_json(CACHE_FILE, {})
    notices_cache = fees_cache.get("__notices__", {})
    direct_overrides = load_json(OVERRIDE_FILE, {})

    purchase_df = fetch_with_retry(ak.fund_purchase_em)
    rank_df = fetch_with_retry(ak.fund_open_fund_rank_em, symbol="全部")

    purchase_df["基金代码"] = purchase_df["基金代码"].astype(str).str.zfill(6)
    rank_df["基金代码"] = rank_df["基金代码"].astype(str).str.zfill(6)

    rank_cols = ["基金代码", "近1年", "日期", "单位净值"]
    if "日增长率" in rank_df.columns:
        rank_cols.append("日增长率")

    df = pd.merge(
        purchase_df[["基金代码", "基金简称", "申购状态", "日累计限定金额", "手续费"]],
        rank_df[rank_cols].drop_duplicates(subset=["基金代码"]),
        on="基金代码", how="left"
    )

    is_nasdaq = df["基金简称"].str.contains(NASDAQ_KW, regex=True, na=False)
    is_sp500 = df["基金简称"].str.contains(SP500_KW, regex=True, na=False)
    not_excluded = ~df["基金简称"].str.contains(EXCLUDE_KW, regex=True, na=False)
    is_pure_etf = (
        df["基金简称"].str.contains("ETF", case=False, na=False)
        & ~df["基金简称"].str.contains("联接", na=False)
    ) | df["申购状态"].astype(str).str.contains("场内", na=False)

    filtered = df[(is_nasdaq | is_sp500) & not_excluded & ~is_pure_etf].copy()
    today_utc = datetime.now(timezone.utc)

    nasdaq_list, sp500_list = [], []

    for _, row in filtered.iterrows():
        code = row["基金代码"]
        name = str(row["基金简称"])
        category = "nasdaq" if re.search(NASDAQ_KW, name) else "sp500"
        share_class = detect_share_class(name)

        status_str = str(row["申购状态"])
        agency_text, agency_num = format_limit(row["日累计限定金额"], status_str)

        need_notice_body = (agency_num == -1 and "限大额" in status_str)
        notice = fetch_latest_limit_notice(code, share_class, notices_cache, need_body=need_notice_body)

        if need_notice_body:
            if notice["parsed_limit"] == 999999999:
                agency_num, agency_text, status_str = 999999999, "不限额", "其他平台可买(据公告)"
            elif notice["parsed_limit"]:
                agency_num = notice["parsed_limit"]
                agency_text, status_str = f"{agency_num:,} 元", "其他平台可买(据公告)"
            else:
                agency_text, status_str = "限大额(见公告)", "天天未代销·其他平台可买"

        fee_info = fees_cache.get(code)
        need_refresh = True
        if isinstance(fee_info, dict) and "cached_at" in fee_info and fee_info.get("mgmt_custody_fee", 0) > 0:
            try:
                cached_dt = datetime.strptime(fee_info["cached_at"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (today_utc - cached_dt).days < CACHE_TTL_DAYS and "scale" in fee_info:
                    need_refresh = False
            except Exception:
                pass

        if need_refresh:
            try:
                fee_info = fetch_fund_fees(code)
                if fee_info["mgmt_custody_fee"] > 0:
                    fees_cache[code] = fee_info
            except Exception:
                fee_info = fee_info or {
                    "mgmt_fee": 0.0, "custody_fee": 0.0, "mgmt_custody_fee": 0.0,
                    "service_fee": 0.0, "purchase_fee_orig": 0.0,
                    "purchase_fee": parse_percent(row["手续费"]), "scale": None
                }

        purchase_fee = fee_info["purchase_fee"]
        if purchase_fee == 0.0 and share_class == "A":
            fallback_fee = parse_percent(row["手续费"])
            if fallback_fee > 0:
                purchase_fee = fallback_fee
                if fee_info.get("purchase_fee_orig", 0) == 0:
                    fee_info["purchase_fee_orig"] = round(fallback_fee * 10, 2)

        if code in direct_overrides:
            ov = direct_overrides[code]
            direct_num, direct_text = ov["limit_num"], ov["limit_text"]
            direct_source, direct_note = "manual", ov.get("note", "已手动校准")
        elif notice["has_channel_diff"]:
            direct_num = notice["parsed_limit"] if notice["parsed_limit"] else agency_num
            direct_text = "不限额(见公告)" if direct_num == 999999999 else (
                f"{direct_num:,} 元(见公告)" if notice["parsed_limit"] else "渠道有差异(见公告)"
            )
            direct_source, direct_note = "notice_diff", notice["title"]
        else:
            direct_num, direct_text = agency_num, agency_text
            direct_source, direct_note = "same_as_agency", "未见渠道区分公告，默认同代销"

        y1_val = safe_float(row.get("近1年"), 2)
        daily_chg = safe_float(row.get("日增长率"), 2) if "日增长率" in row else None
        scale_val = fee_info.get("scale")
        if y1_val is None or scale_val is None:
            fb_1y, fb_scale, _ = fetch_pingzhong_extra(code)
            if y1_val is None:
                y1_val = fb_1y
            if scale_val is None:
                scale_val = fb_scale

        first_year_cost = round(purchase_fee + fee_info["mgmt_custody_fee"] + fee_info["service_fee"], 4)

        record = {
            "code": code, "name": name, "share_class": share_class, "status": status_str,
            "agency_limit_text": agency_text, "agency_limit_num": agency_num,
            "direct_limit_text": direct_text, "direct_limit_num": direct_num,
            "direct_source": direct_source, "direct_note": direct_note,
            "return_1y": y1_val, "daily_chg": daily_chg, "scale": scale_val,
            "mgmt_fee": fee_info["mgmt_fee"], "custody_fee": fee_info["custody_fee"],
            "mgmt_custody_fee": fee_info["mgmt_custody_fee"], "purchase_fee": purchase_fee,
            "purchase_fee_orig": fee_info["purchase_fee_orig"], "service_fee": fee_info["service_fee"],
            "first_year_cost": first_year_cost,
            "nav_date": str(row["日期"]) if pd.notna(row["日期"]) else "",
            "notice_title": notice["title"], "notice_date": notice["date"],
            "notice_url": f"https://fundf10.eastmoney.com/jjgg_{code}_5.html"
        }
        (nasdaq_list if category == "nasdaq" else sp500_list).append(record)

    sort_fn = lambda x: (-(0.5 if x["agency_limit_num"] == -1 else x["agency_limit_num"]), x["first_year_cost"])
    nasdaq_list.sort(key=sort_fn)
    sp500_list.sort(key=sort_fn)

    etf_list = fetch_etf_list(fees_cache, today_utc)
    active_top5 = build_active_top5(purchase_df, rank_df)

    fees_cache["__notices__"] = notices_cache
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(fees_cache, f, ensure_ascii=False, indent=2)

    tz_bj = timezone(timedelta(hours=8))
    output = {
        "updated_at": datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M:%S"),
        "nasdaq": nasdaq_list,
        "sp500": sp500_list,
        "etfs": etf_list,
        "active_top5": active_top5
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
