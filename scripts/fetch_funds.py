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
    "金额限制", "限制申购", "申购上限", "限额", "暂停", "节假日"
]

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

DEFAULT_BARK_URL = "https://bark.youranzi.de/3cdh5mj7wiRryZWuPAf84u"
CACHE_FILE = "data/fees_cache.json"
OVERRIDE_FILE = "data/direct_overrides.json"
AGENCY_OVERRIDE_FILE = "data/agency_overrides.json"
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


def _norm_cn_date(y, m, d):
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return ""


def extract_schedule_from_text(text, title="", pub_date=""):
    """从公告正文/标题中精准解析生效日期(effective_date)与自动恢复日期(resume_date)"""
    combined = re.sub(r"\s+", "", f"{title}。{text or ''}")
    effective_date = ""
    resume_date = ""
    no_further_notice = bool(re.search(r"不再(?:另行|单独)公告", combined))
    is_holiday_suspend = bool(re.search(r"(?:节假日|休市|非港股通|境外主要投资市场)", combined) and "暂停" in combined)

    # 1. 解析生效/暂停开始日期
    eff_pats = [
        r"(?:因|于|自)(\d{4})年(\d{1,2})月(\d{1,2})日(?:起|为境外|为非|暂停|将暂停|起暂停|起调整|起限制)",
        r"(\d{4})年(\d{1,2})月(\d{1,2})日[^。；]{0,20}?(?:暂停申购|暂停办理|将暂停)"
    ]
    for pat in eff_pats:
        m = re.search(pat, combined)
        if m:
            effective_date = _norm_cn_date(m.group(1), m.group(2), m.group(3))
            break

    # 2. 解析恢复办理日期
    res_pats = [
        r"(?:并于|自|于)(\d{4})年(\d{1,2})月(\d{1,2})日(?:起)?恢复(?:办理)?(?:上述|申购|正常)",
        r"恢复(?:办理)?[^。；]{0,18}?(\d{4})年(\d{1,2})月(\d{1,2})日"
    ]
    for pat in res_pats:
        m = re.search(pat, combined)
        if m:
            resume_date = _norm_cn_date(m.group(1), m.group(2), m.group(3))
            break

    return {
        "effective_date": effective_date,
        "resume_date": resume_date,
        "no_further_notice": no_further_notice,
        "is_holiday_suspend": is_holiday_suspend
    }


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


def _amount_in_segment(seg):
    """取分句里的第一个人民币限额（优先带“不超过/调整为/仍为/高于”等动词的那个）。"""
    m = re.search(
        r"(?:不超过|不得超过|上限为|调整为|调整至|限制为|限定为|限额为|设定为|仍为|高于)"
        r"(?:人民币)?([\d,]+(?:\.\d+)?)(万|千)?元", seg)
    if m:
        return parse_cn_amount(m.group(1), m.group(2))
    m = re.search(r"([\d,]+(?:\.\d+)?)(万|千)?元", seg)
    if m:
        return parse_cn_amount(m.group(1), m.group(2))
    return None


def extract_channel_limits(text, share_class=""):
    """从限额公告正文里按渠道分段解析限额。

    返回 {"direct": 直销限额|None, "agency": 代销限额|None,
           "default": 不分渠道的通用限额|None,
           "direct_any": 正文里出现的任一直销限额|None}。
    只有正文明确写了“直销渠道/代销机构”区分时 direct/agency 才有值；
    不分渠道的公告只会得到 default，调用方此时应沿用“同代销”的老口径，
    而不是把 default 当成直销数去覆盖代销数。
    分句若明确只写了别的份额（如“A类或C类”），不对本份额生效。
    """
    out = {"direct": None, "agency": None, "default": None, "direct_any": None}
    if not text:
        return out
    clean = re.sub(r"\s+", "", text)
    for clause in re.split(r"[。；]", clean):
        if not clause:
            continue
        has_direct = "直销" in clause
        has_agency = ("代销" in clause) or ("非直销" in clause)
        amt = _amount_in_segment(clause)
        if has_direct and amt is not None and out["direct_any"] is None:
            out["direct_any"] = amt
        classes = set(re.findall(r"([ACDEFI])类", clause))
        if share_class and classes and share_class not in classes:
            continue
        if has_direct and has_agency:
            # 同一句里两种渠道都出现：按关键词位置切开，各取各的数
            idx_d = clause.find("直销")
            idx_a = max(clause.find("代销"), clause.find("非直销"))
            if idx_d < idx_a:
                d_amt = _amount_in_segment(clause[idx_d:idx_a])
                a_amt = _amount_in_segment(clause[idx_a:])
            else:
                a_amt = _amount_in_segment(clause[idx_a:idx_d])
                d_amt = _amount_in_segment(clause[idx_d:])
            if d_amt is not None and out["direct"] is None:
                out["direct"] = d_amt
            if a_amt is not None and out["agency"] is None:
                out["agency"] = a_amt
        elif has_direct:
            if amt is not None and out["direct"] is None:
                out["direct"] = amt
        elif has_agency:
            if amt is not None and out["agency"] is None:
                out["agency"] = amt
        elif amt is not None and out["default"] is None:
            if re.search(r"(?:不超过|不得超过|限制|限额|调整|高于)", clause):
                out["default"] = amt
    return out


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
                    pub_date = str(item.get("PUBLISHDATE", ""))[:10]
                    need_schedule = any(k in title for k in ("暂停", "节假日", "恢复", "休市"))
                    # 限额公告的正文必拉：渠道区分（直销 vs 代销）只写在正文里，
                    # 标题里几乎从不出现，只看标题会把直销默认成代销。
                    body = fetch_notice_content(art_code, notices_cache)
                    parsed_limit = extract_limit_from_text(body, share_class)
                    channel_limits = extract_channel_limits(body, share_class)
                    has_diff = (
                        "直销" in title or "代销" in title or "非直销" in title
                        or channel_limits["direct"] is not None
                        or channel_limits["agency"] is not None
                    )
                    sched = extract_schedule_from_text(body, title, pub_date)
                    return {
                        "title": title,
                        "date": pub_date,
                        "has_channel_diff": has_diff,
                        "parsed_limit": parsed_limit,
                        "channel_limits": channel_limits,
                        "effective_date": sched["effective_date"],
                        "resume_date": sched["resume_date"],
                        "no_further_notice": sched["no_further_notice"],
                        "is_holiday_suspend": sched["is_holiday_suspend"]
                    }
            if items:
                break
        except Exception:
            pass
    return {
        "title": "", "date": "", "has_channel_diff": False, "parsed_limit": None,
        "channel_limits": {"direct": None, "agency": None, "default": None, "direct_any": None},
        "effective_date": "", "resume_date": "", "no_further_notice": False, "is_holiday_suspend": False
    }


def fetch_pingzhong_extra(code, full_nav=False):
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
            arr = json.loads(m_nav.group(1))
            if not full_nav:
                arr = arr[-120:]
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
                amt_wan = safe_float(parts[37], 2)
                turnover_yi = round(amt_wan / 10000, 2) if amt_wan is not None else None
                quotes[code] = {"price": price, "chg_pct": chg_pct, "turnover": turnover_yi}
    except Exception:
        pass
    return quotes


def fetch_etf_kline_fast(mkt, code):
    # 拉取近 1500 个交易日（约 6 年）以支持 15天/30天/90天/365天/全部 溢价率走势
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={mkt}{code},day,,,1500,"
    kline = []
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
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

        ret_1y, scale_fb, nav_map = fetch_pingzhong_extra(code, full_nav=True)
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
        tail90 = prem_vals[-90:]
        if tail90 and premium is not None:
            prem_stats["min"] = min(tail90)
            prem_stats["max"] = max(tail90)
            prem_stats["percentile"] = int(round(sum(1 for v in tail90 if v <= premium) / len(tail90) * 100))

        etf_results.append({
            "code": code, "name": meta["name"], "index": meta["index"],
            "price": price, "nav": latest_nav, "nav_date": latest_nav_date,
            "chg_pct": chg_pct, "premium": premium, "scale": scale_val,
            "return_1y": ret_1y, "mgmt_custody_fee": fee_info.get("mgmt_custody_fee", 0.0),
            "turnover": turnover, "detail_url": f"https://fund.eastmoney.com/{code}.html",
            "premium_history": {"dates": prem_dates, "values": prem_vals, "stats": prem_stats}
        })

    etf_results.sort(key=lambda x: (x["premium"] if x["premium"] is not None else -999), reverse=True)
    return etf_results


def build_active_top5(purchase_df, rank_df):
    try:
        qdii_rank = fetch_with_retry(ak.fund_open_fund_rank_em, symbol="QDII")
        qdii_rank["基金代码"] = qdii_rank["基金代码"].astype(str).str.zfill(6)
        m = pd.merge(
            qdii_rank[["基金代码", "基金简称", "近1年", "日期"]],
            purchase_df[["基金代码", "申购状态", "日累计限定金额"]],
            on="基金代码", how="left"
        )
        exclude_passive = r"指数|ETF|联接|债|美元|美钞|美汇|现汇|现钞|港币|原油|黄金|商品|REIT|C$|D$|E$|I$"
        m = m[~m["基金简称"].str.contains(exclude_passive, regex=True, na=False)].copy()
        m["y1_num"] = pd.to_numeric(m["近1年"], errors="coerce")
        m = m.dropna(subset=["y1_num"]).sort_values("y1_num", ascending=False).head(5)

        top5 = []
        for idx, (_, r) in enumerate(m.iterrows(), 1):
            code = r["基金代码"]
            limit_text, _ = format_limit(r["日累计限定金额"], r["申购状态"])
            _, scale_yi, _ = fetch_pingzhong_extra(code)
            top5.append({
                "rank": idx, "code": code, "name": str(r["基金简称"]),
                "return_1y": round(float(r["y1_num"]), 2), "nav_date": str(r["日期"]),
                "scale": scale_yi, "limit_text": limit_text,
                "url": f"https://fundf10.eastmoney.com/jjgg_{code}_5.html"
            })
        return top5
    except Exception:
        return []


DEFAULT_PUSH_CFG = {
    "bark_url": DEFAULT_BARK_URL,
    "quota": {
        "enabled": True,
        "time": "09:30",
        "template": "检测到 {count} 项额度变动：\n{details}"
    }
}


def _hhmm_to_mins(hhmm, fallback):
    try:
        parts = str(hhmm or fallback).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        p = fallback.split(":")
        return int(p[0]) * 60 + int(p[1])


def send_bark_quota_alert(changes, now_bj):
    if not changes:
        return

    file_cfg = load_json("data/push_config.json", {})
    q_cfg = {**DEFAULT_PUSH_CFG["quota"], **(file_cfg.get("quota") or {})}
    if not q_cfg.get("enabled", True):
        return

    bark_base = (os.getenv("BARK_URL") or file_cfg.get("bark_url") or DEFAULT_BARK_URL).strip().rstrip("/")
    if not bark_base:
        return

    force = os.getenv("FORCE_BARK", "").strip() == "1"
    cur_mins = now_bj.hour * 60 + now_bj.minute
    target_mins = _hhmm_to_mins(q_cfg.get("time"), "09:30")
    in_window = abs(cur_mins - target_mins) <= 20
    if not (force or in_window):
        return

    lines = [
        f"{c['name']}({c['code']}) {c['channel']}：{c['old']} -> {c['new']}"
        for c in changes[:12]
    ]
    if len(changes) > 12:
        lines.append(f"...另有 {len(changes) - 12} 项变化，请打开监控台查看")
    details_str = "\n".join(lines)

    tpl = str(q_cfg.get("template") or "").strip() or DEFAULT_PUSH_CFG["quota"]["template"]
    body = tpl.replace("{count}", str(len(changes))).replace("{details}", details_str)
    title = f"QDII 限额变化提醒（{len(changes)}项）"

    try:
        requests.post(
            f"{bark_base}/",
            json={
                "title": title,
                "body": body,
                "group": "QDII限额",
                "url": "https://qdii.youranzi.de/"
            },
            timeout=10
        )
    except Exception:
        pass


def main():
    os.makedirs("data", exist_ok=True)
    fees_cache = load_json(CACHE_FILE, {})
    notices_cache = fees_cache.get("__notices__", {})
    prev_limits = fees_cache.get("__last_limits__", {})
    normal_limits = fees_cache.get("__normal_limits__", {})
    direct_overrides = load_json(OVERRIDE_FILE, {})
    agency_overrides = load_json(AGENCY_OVERRIDE_FILE, {})

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
    tz_bj = timezone(timedelta(hours=8))
    now_bj = datetime.now(tz_bj)
    today_bj_str = now_bj.strftime("%Y-%m-%d")

    nasdaq_list, sp500_list = [], []
    new_limits = {}
    changes = []

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
            # 天天未代销时才用公告补代销数；公告里那个单个数可能是直销数
            # （如大成纳指表格里的 100 元就是直销），绝不能直接拿来当作代销。
            ch = notice.get("channel_limits") or {}
            if ch.get("agency"):
                agency_num = ch["agency"]
                agency_text, status_str = f"{agency_num:,} 元", "其他平台可买(据公告)"
            elif notice["parsed_limit"] == 999999999:
                agency_num, agency_text, status_str = 999999999, "不限额", "其他平台可买(据公告)"
            elif notice["parsed_limit"] and not ch.get("direct"):
                # 公告不分渠道，单个数就是通用限额，才能当作代销
                agency_num = notice["parsed_limit"]
                agency_text, status_str = f"{agency_num:,} 元", "其他平台可买(据公告)"
            else:
                agency_text, status_str = "限大额(见公告)", "天天未代销·其他平台可买"

        # ===== 精准生效时间与到期自动恢复逻辑 =====
        eff_date = notice.get("effective_date") or ""
        res_date = notice.get("resume_date") or ""
        notice_schedule = ""
        saved_normal = normal_limits.get(code)

        if eff_date and res_date:
            if today_bj_str < eff_date:
                # 预告期：未到暂停生效日，保持暂停前原额度
                notice_schedule = f"预告：{eff_date[5:]} 暂停，{res_date[5:]} 恢复"
                if agency_num == 0 and saved_normal and saved_normal.get("agency_num", 0) != 0:
                    agency_num = saved_normal["agency_num"]
                    agency_text = saved_normal["agency_text"]
                    status_str = saved_normal.get("status", "开放申购")
            elif eff_date <= today_bj_str < res_date:
                # 生效暂停期
                agency_num, agency_text, status_str = 0, "暂停申购", "节假日/临时暂停"
                notice_schedule = f"暂停中：{res_date[5:]} 自动恢复原额度"
            elif today_bj_str >= res_date:
                # 已到恢复日：若仍显示暂停（因不再另行公告），自动恢复暂停前额度
                if agency_num == 0 and saved_normal and saved_normal.get("agency_num", 0) != 0:
                    agency_num = saved_normal["agency_num"]
                    agency_text = saved_normal["agency_text"]
                    status_str = saved_normal.get("status", "开放申购")
                    notice_schedule = f"{res_date[5:]} 已自动恢复原额度"
                elif notice.get("no_further_notice"):
                    notice_schedule = f"{res_date[5:]} 已恢复申购"
        elif eff_date and today_bj_str < eff_date:
            notice_schedule = f"预告：{eff_date[5:]} 起生效"

        # 记录非暂停状态下的正常额度，供节假日/临时暂停到期后精准还原
        if agency_num != 0:
            normal_limits[code] = {
                "agency_num": agency_num,
                "agency_text": agency_text,
                "status": status_str
            }

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

        # 代销渠道覆盖：某些份额根本没有代销渠道（如 021000 南方纳指 I 类，
        # 实测仅直销），天天基金给的数其实是直销限额，不能当作代销额度。
        # limit_num 为 null 表示该渠道不存在，代销显示为“—”。
        # 覆盖在排期/暂停逻辑之后应用，所以暂停判断仍按天天基金的原始值走，
        # 且 normal_limits 里存的仍是原始值，下次恢复不受影响。
        agency_overridden = code in agency_overrides
        agency_num_raw, agency_text_raw = agency_num, agency_text
        agency_note = None
        if agency_overridden:
            ov_a = agency_overrides[code]
            agency_num = ov_a.get("limit_num")  # None = 无此渠道
            agency_text = ov_a.get("limit_text") or "—"
            agency_note = ov_a.get("note")

        # 直销优先级：公告分渠道解析 > 手动覆盖 > 默认同代销。
        # 手动覆盖只作兜底：公告能解析出分渠道限额时以公告为准，
        # 这样覆盖值过期后不会一直挡住新的公告数；公告解析不出时
        # （如天弘 D 类）才用覆盖值。
        ch = notice.get("channel_limits") or {}
        ch_direct, ch_default, ch_direct_any = ch.get("direct"), ch.get("default"), ch.get("direct_any")
        if (agency_num == 0 or agency_num_raw == 0) and not ch_direct:
            # 暂停：代销暂停时直销同为暂停，不再输出“渠道有差异(见公告)”
            direct_num, direct_text = 0, "暂停申购"
            direct_source, direct_note = "same_as_agency", "代销暂停，直销同为暂停"
        elif ch_direct:
            direct_num = ch_direct
            direct_text = "不限额(见公告)" if direct_num == 999999999 else f"{direct_num:,} 元"
            direct_source, direct_note = "notice_diff", notice["title"]
        elif code in direct_overrides:
            ov = direct_overrides[code]
            direct_num, direct_text = ov["limit_num"], ov["limit_text"]
            direct_source, direct_note = "manual", ov.get("note", "已手动校准")
        elif ch_direct is None and ch_default is not None and ch_direct_any is not None:
            # 公告分了渠道，但直销条款只列了别的份额（如建信直销 50 元只写 A/C，
            # D 类不在其中）：本份额直销用公告里的通用限额（如建信 D 类 10 元）
            direct_num, direct_text = ch_default, f"{ch_default:,} 元"
            direct_source, direct_note = "notice_diff", notice["title"]
        else:
            if agency_num is None:
                # 代销渠道不存在：天天基金的单个数其实是直销限额，
                # 直销直接用原始值，不写“同代销”
                direct_num, direct_text = agency_num_raw, agency_text_raw
                direct_source, direct_note = "manual", agency_note or "该份额无代销渠道，限额为直销限额"
            else:
                direct_num, direct_text = agency_num, agency_text
                direct_source, direct_note = "same_as_agency", "未见渠道区分公告，默认同代销"

        cur_pair = {"agency": agency_text, "direct": direct_text}
        new_limits[code] = cur_pair
        if prev_limits:
            old_pair = prev_limits.get(code)
            if old_pair:
                if old_pair.get("agency") != agency_text:
                    changes.append({"code": code, "name": name, "channel": "代销", "old": old_pair.get("agency"), "new": agency_text})
                if old_pair.get("direct") != direct_text:
                    changes.append({"code": code, "name": name, "channel": "直销", "old": old_pair.get("direct"), "new": direct_text})

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
            "agency_note": agency_note,
            "direct_limit_text": direct_text, "direct_limit_num": direct_num,
            "direct_source": direct_source, "direct_note": direct_note,
            "return_1y": y1_val, "daily_chg": daily_chg, "scale": scale_val,
            "mgmt_fee": fee_info["mgmt_fee"], "custody_fee": fee_info["custody_fee"],
            "mgmt_custody_fee": fee_info["mgmt_custody_fee"], "purchase_fee": purchase_fee,
            "purchase_fee_orig": fee_info["purchase_fee_orig"], "service_fee": fee_info["service_fee"],
            "first_year_cost": first_year_cost,
            "nav_date": str(row["日期"]) if pd.notna(row["日期"]) else "",
            "notice_title": notice["title"], "notice_date": notice["date"],
            "effective_date": eff_date, "resume_date": res_date,
            "notice_schedule": notice_schedule,
            "notice_url": f"https://fundf10.eastmoney.com/jjgg_{code}_5.html"
        }
        (nasdaq_list if category == "nasdaq" else sp500_list).append(record)

    sort_fn = lambda x: (-(0.5 if x["agency_limit_num"] in (-1, None) else x["agency_limit_num"]), x["first_year_cost"])
    nasdaq_list.sort(key=sort_fn)
    sp500_list.sort(key=sort_fn)

    etf_list = fetch_etf_list(fees_cache, today_utc)
    active_top5 = build_active_top5(purchase_df, rank_df)

    send_bark_quota_alert(changes, now_bj)

    fees_cache["__notices__"] = notices_cache
    fees_cache["__last_limits__"] = new_limits
    fees_cache["__normal_limits__"] = normal_limits
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(fees_cache, f, ensure_ascii=False, indent=2)

    output = {
        "updated_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "nasdaq": nasdaq_list,
        "sp500": sp500_list,
        "etfs": etf_list,
        "active_top5": active_top5
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
