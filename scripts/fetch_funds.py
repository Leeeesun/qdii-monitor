import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
import akshare as ak
import pandas as pd
import requests
from bs4 import BeautifulSoup

# 筛选规则：严格聚焦美股纳指、标普宽基，排除港股、石油商品及美元/港币份额
NASDAQ_KW = r"纳斯达克|纳指"
SP500_KW = r"标普"
EXCLUDE_KW = r"美元|美钞|美汇|现汇|现钞|港币|港股|香港|中国|石油|天然气|消费|生物|医药|信息|科技精选|油气|医疗|黄金|商品|原油|房地产|红利"

LIMIT_TITLE_KWS = [
    "大额申购", "暂停申购", "恢复申购", "申购限额",
    "金额限制", "限制申购", "申购上限", "限额"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/"
}

CACHE_FILE = "data/fees_cache.json"
OVERRIDE_FILE = "data/direct_overrides.json"
OUTPUT_FILE = "data/funds.json"
CACHE_TTL_DAYS = 7


def fetch_with_retry(func, retries=3, delay=4, **kwargs):
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
    m = re.search(r"(\d+(?:\.\d+)?)", str(val))
    return round(float(m.group(1)), 4) if m else 0.0


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
    """从东方财富官方限购公告正文中提取人民币单日限额"""
    if not text:
        return None
    clean = re.sub(r"\s+", "", text)

    # 先判断是否为取消限购/恢复大额不限额公告
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
    """拉取东方财富公告正文，按公告 ID 缓存"""
    if not art_code:
        return ""
    if art_code in notices_cache:
        return notices_cache[art_code]
    url = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
    try:
        res = requests.get(
            url,
            params={"art_code": art_code, "client_source": "web", "page_index": 1},
            headers=HEADERS,
            timeout=10
        )
        data = res.json().get("data") or {}
        content = data.get("notice_content") or data.get("attach_content") or ""
        content = content[:3500]
        if content:
            notices_cache[art_code] = content
        return content
    except Exception:
        return ""


def fetch_latest_limit_notice(code, share_class, notices_cache, need_body=False):
    """直接查询东方财富【基金销售(type=5)】最新20条公告，避免被季报挤出"""
    url = "https://api.fund.eastmoney.com/f10/JJGG"
    for notice_type in (5, 0):
        params = {"fundcode": code, "pageIndex": 1, "pageSize": 20, "type": notice_type}
        try:
            res = requests.get(url, params=params, headers=HEADERS, timeout=8)
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
                        time.sleep(0.1)
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


def fetch_return_1y_fallback(code):
    """通过 pingzhongdata 补全暂停申购或未入榜基金的近1年收益"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        m = re.search(r'var\s+syl_1n\s*=\s*"([^"]+)"', res.text)
        if m and m.group(1).strip() != "":
            return round(float(m.group(1)), 2)
    except Exception:
        pass
    return None


def fetch_fund_fees(code):
    """抓取天天基金费率页：管理费、托管费、销售服务费与申购费率"""
    url = f"https://fundf10.eastmoney.com/jjfl_{code}.html"
    res = requests.get(url, headers=HEADERS, timeout=12)
    res.encoding = "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    mgmt_m = re.search(r"管理费率[^\d]{0,12}([\d.]+)%", text)
    cust_m = re.search(r"托管费率[^\d]{0,12}([\d.]+)%", text)
    serv_m = re.search(r"销售服务费率[^\d]{0,12}([\d.]+)%", text)

    mgmt = float(mgmt_m.group(1)) if mgmt_m else 0.0
    cust = float(cust_m.group(1)) if cust_m else 0.0
    serv = float(serv_m.group(1)) if serv_m else 0.0

    purchase_orig = 0.0
    purchase_disc = 0.0
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if any("申购" in h for h in headers) and any("费率" in h for h in headers):
            rows = table.find_all("tr")
            if len(rows) > 1:
                cols = [td.get_text(" ", strip=True) for td in rows[1].find_all("td")]
                if cols:
                    rates = re.findall(r"(\d+(?:\.\d+)?)%", cols[-1])
                    if len(rates) >= 2:
                        purchase_orig = float(rates[0])
                        purchase_disc = float(rates[-1])
                    elif len(rates) == 1:
                        purchase_orig = float(rates[0])
                        purchase_disc = float(rates[0])
            break

    return {
        "mgmt_fee": round(mgmt, 4),
        "custody_fee": round(cust, 4),
        "mgmt_custody_fee": round(mgmt + cust, 4),
        "service_fee": round(serv, 4),
        "purchase_fee_orig": round(purchase_orig, 4),
        "purchase_fee": round(purchase_disc, 4),
        "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%d")
    }


def main():
    os.makedirs("data", exist_ok=True)
    fees_cache = load_json(CACHE_FILE, {})
    notices_cache = fees_cache.get("__notices__", {})
    direct_overrides = load_json(OVERRIDE_FILE, {})

    purchase_df = fetch_with_retry(ak.fund_purchase_em)
    rank_df = fetch_with_retry(ak.fund_open_fund_rank_em, symbol="全部")

    purchase_df["基金代码"] = purchase_df["基金代码"].astype(str).str.zfill(6)
    rank_df["基金代码"] = rank_df["基金代码"].astype(str).str.zfill(6)

    df = pd.merge(
        purchase_df[["基金代码", "基金简称", "申购状态", "日累计限定金额", "手续费"]],
        rank_df[["基金代码", "近1年", "日期", "单位净值"]].drop_duplicates(subset=["基金代码"]),
        on="基金代码",
        how="left"
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

    nasdaq_list = []
    sp500_list = []

    for _, row in filtered.iterrows():
        code = row["基金代码"]
        name = str(row["基金简称"])
        category = "nasdaq" if re.search(NASDAQ_KW, name) else "sp500"
        share_class = detect_share_class(name)

        # 1. 代销额度初判
        status_str = str(row["申购状态"])
        agency_text, agency_num = format_limit(row["日累计限定金额"], status_str)

        need_notice_body = (agency_num == -1 and "限大额" in status_str)
        notice = fetch_latest_limit_notice(code, share_class, notices_cache, need_body=need_notice_body)
        time.sleep(0.08)

        if need_notice_body:
            if notice["parsed_limit"] == 999999999:
                agency_num = 999999999
                agency_text = "不限额"
                status_str = "其他平台可买(据公告)"
            elif notice["parsed_limit"]:
                agency_num = notice["parsed_limit"]
                agency_text = f"{agency_num:,} 元"
                status_str = "其他平台可买(据公告)"
            else:
                agency_text = "限大额(见公告)"
                status_str = "天天未代销·其他平台可买"

        # 2. 费率抓取（7天缓存）
        fee_info = fees_cache.get(code)
        need_refresh = True
        if isinstance(fee_info, dict) and "cached_at" in fee_info and fee_info.get("mgmt_custody_fee", 0) > 0:
            try:
                cached_dt = datetime.strptime(fee_info["cached_at"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (today_utc - cached_dt).days < CACHE_TTL_DAYS:
                    need_refresh = False
            except Exception:
                pass

        if need_refresh:
            try:
                fee_info = fetch_fund_fees(code)
                if fee_info["mgmt_custody_fee"] > 0:
                    fees_cache[code] = fee_info
                time.sleep(0.2)
            except Exception:
                fee_info = fee_info or {
                    "mgmt_fee": 0.0, "custody_fee": 0.0, "mgmt_custody_fee": 0.0,
                    "service_fee": 0.0, "purchase_fee_orig": 0.0,
                    "purchase_fee": parse_percent(row["手续费"])
                }

        purchase_fee = fee_info["purchase_fee"]
        if purchase_fee == 0.0 and share_class == "A":
            fallback_fee = parse_percent(row["手续费"])
            if fallback_fee > 0:
                purchase_fee = fallback_fee
                if fee_info.get("purchase_fee_orig", 0) == 0:
                    fee_info["purchase_fee_orig"] = round(fallback_fee * 10, 2)

        # 3. 直销额度判定（手动校准 > 公告渠道差异 > 同代销）
        if code in direct_overrides:
            ov = direct_overrides[code]
            direct_num = ov["limit_num"]
            direct_text = ov["limit_text"]
            direct_source = "manual"
            direct_note = ov.get("note", "已手动校准")
        elif notice["has_channel_diff"]:
            direct_num = notice["parsed_limit"] if notice["parsed_limit"] else agency_num
            direct_text = "不限额(见公告)" if direct_num == 999999999 else (
                f"{direct_num:,} 元(见公告)" if notice["parsed_limit"] else "渠道有差异(见公告)"
            )
            direct_source = "notice_diff"
            direct_note = notice["title"]
        else:
            direct_num = agency_num
            direct_text = agency_text
            direct_source = "same_as_agency"
            direct_note = "未见渠道区分公告，默认同代销"

        # 4. 近 1 年收益兜底补全
        y1 = row["近1年"]
        y1_val = round(float(y1), 2) if pd.notna(y1) and str(y1).strip() != "" else None
        if y1_val is None:
            y1_val = fetch_return_1y_fallback(code)
            time.sleep(0.08)

        first_year_cost = round(
            purchase_fee + fee_info["mgmt_custody_fee"] + fee_info["service_fee"], 4
        )

        record = {
            "code": code,
            "name": name,
            "share_class": share_class,
            "status": status_str,
            "agency_limit_text": agency_text,
            "agency_limit_num": agency_num,
            "direct_limit_text": direct_text,
            "direct_limit_num": direct_num,
            "direct_source": direct_source,
            "direct_note": direct_note,
            "return_1y": y1_val,
            "mgmt_fee": fee_info["mgmt_fee"],
            "custody_fee": fee_info["custody_fee"],
            "mgmt_custody_fee": fee_info["mgmt_custody_fee"],
            "purchase_fee": purchase_fee,
            "purchase_fee_orig": fee_info["purchase_fee_orig"],
            "service_fee": fee_info["service_fee"],
            "first_year_cost": first_year_cost,
            "nav_date": str(row["日期"]) if pd.notna(row["日期"]) else "",
            "notice_title": notice["title"],
            "notice_date": notice["date"],
            "notice_url": f"https://fundf10.eastmoney.com/jjgg_{code}_5.html"
        }

        if category == "nasdaq":
            nasdaq_list.append(record)
        else:
            sp500_list.append(record)

    # 排序时让“可买但未解析出数字(-1)”排在“暂停申购(0)”前面
    sort_fn = lambda x: (-(0.5 if x["agency_limit_num"] == -1 else x["agency_limit_num"]), x["first_year_cost"])
    nasdaq_list.sort(key=sort_fn)
    sp500_list.sort(key=sort_fn)

    fees_cache["__notices__"] = notices_cache
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(fees_cache, f, ensure_ascii=False, indent=2)

    tz_bj = timezone(timedelta(hours=8))
    output = {
        "updated_at": datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M:%S"),
        "nasdaq": nasdaq_list,
        "sp500": sp500_list
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
