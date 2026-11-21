import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
import akshare as ak
import pandas as pd
import requests
from bs4 import BeautifulSoup

# 筛选规则：匹配纳指、标普，同时剔除美元份额、纯场内ETF及纯行业主题基金
NASDAQ_KW = r"纳斯达克|纳指"
SP500_KW = r"标普"
EXCLUDE_KW = r"美元|现汇|现钞|ETF(?!联接)|港币|消费|生物|医药|信息|科技精选|油气|医疗"

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


def parse_percent(text):
    if not text:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", str(text))
    return round(float(m.group(1)), 4) if m else 0.0


def format_limit(val, status):
    if "暂停" in str(status):
        return "暂停申购", 0
    try:
        num = float(val)
        if pd.isna(num):
            return "以平台为准", -1
        if num >= 1e9:
            return "不限额", 999999999
        return f"{int(num):,} 元", int(num)
    except (ValueError, TypeError):
        return "未知", -1


def fetch_fund_fees(code):
    """抓取天天基金费率页：管理费、托管费、销售服务费与申购费率"""
    url = f"https://fundf10.eastmoney.com/jjfl_{code}.html"
    res = requests.get(url, headers=HEADERS, timeout=12)
    res.encoding = "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    mgmt_m = re.search(r"管理费率\s*[:：]?\s*([\d.]+)%", text)
    cust_m = re.search(r"托管费率\s*[:：]?\s*([\d.]+)%", text)
    serv_m = re.search(r"销售服务费率\s*[:：]?\s*([\d.]+)%", text)

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


def fetch_latest_limit_notice(code):
    """查询最新限购公告标题，辅助识别直销与代销渠道差异"""
    url = "https://api.fund.eastmoney.com/f10/JJGG"
    params = {"fundcode": code, "pageIndex": 1, "pageSize": 10, "type": 0}
    try:
        res = requests.get(url, params=params, headers=HEADERS, timeout=8)
        items = res.json().get("Data", [])
        for item in items:
            title = item.get("TITLE", "")
            if any(k in title for k in ["大额申购", "暂停申购", "恢复申购", "申购限额"]):
                return {
                    "title": title,
                    "date": str(item.get("PUBLISHDATE", ""))[:10],
                    "has_channel_diff": ("直销" in title or "代销" in title or "非直销" in title)
                }
    except Exception:
        pass
    return {"title": "", "date": "", "has_channel_diff": False}


def main():
    os.makedirs("data", exist_ok=True)
    fees_cache = load_json(CACHE_FILE, {})
    direct_overrides = load_json(OVERRIDE_FILE, {})

    purchase_df = fetch_with_retry(ak.fund_purchase_em)
    rank_df = fetch_with_retry(ak.fund_open_fund_rank_em, symbol="QDII")

    # 统一补齐 6 位基金代码，防止合并丢失
    purchase_df["基金代码"] = purchase_df["基金代码"].astype(str).str.zfill(6)
    rank_df["基金代码"] = rank_df["基金代码"].astype(str).str.zfill(6)

    df = pd.merge(
        purchase_df[["基金代码", "基金简称", "申购状态", "日累计限定金额", "手续费"]],
        rank_df[["基金代码", "近1年", "日期", "单位净值"]],
        on="基金代码",
        how="inner"
    )

    is_nasdaq = df["基金简称"].str.contains(NASDAQ_KW, regex=True, na=False)
    is_sp500 = df["基金简称"].str.contains(SP500_KW, regex=True, na=False)
    not_excluded = ~df["基金简称"].str.contains(EXCLUDE_KW, regex=True, na=False)

    filtered = df[(is_nasdaq | is_sp500) & not_excluded].copy()
    today_utc = datetime.now(timezone.utc)

    nasdaq_list = []
    sp500_list = []

    for _, row in filtered.iterrows():
        code = row["基金代码"]
        name = str(row["基金简称"])
        category = "nasdaq" if re.search(NASDAQ_KW, name) else "sp500"

        # 1. 代销额度
        agency_text, agency_num = format_limit(row["日累计限定金额"], row["申购状态"])

        # 2. 费率数据（7天缓存防限流）
        fee_info = fees_cache.get(code)
        need_refresh = True
        if fee_info and "cached_at" in fee_info:
            try:
                cached_dt = datetime.strptime(fee_info["cached_at"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (today_utc - cached_dt).days < CACHE_TTL_DAYS:
                    need_refresh = False
            except Exception:
                pass

        if need_refresh:
            try:
                fee_info = fetch_fund_fees(code)
                fees_cache[code] = fee_info
                time.sleep(0.35)
            except Exception:
                fee_info = fee_info or {
                    "mgmt_fee": 0.0, "custody_fee": 0.0, "mgmt_custody_fee": 0.0,
                    "service_fee": 0.0, "purchase_fee_orig": 0.0,
                    "purchase_fee": parse_percent(row["手续费"])
                }

        purchase_fee = fee_info["purchase_fee"]
        if purchase_fee == 0.0 and not name.upper().endswith("C"):
            fallback_fee = parse_percent(row["手续费"])
            if fallback_fee > 0:
                purchase_fee = fallback_fee

        # 3. 直销额度判定
        notice = fetch_latest_limit_notice(code)
        time.sleep(0.1)
        if code in direct_overrides:
            ov = direct_overrides[code]
            direct_num = ov["limit_num"]
            direct_text = ov["limit_text"]
            direct_source = "manual"
            direct_note = ov.get("note", "已手动校准")
        elif notice["has_channel_diff"]:
            direct_num = agency_num
            direct_text = "渠道有差异(见公告)"
            direct_source = "notice_diff"
            direct_note = notice["title"]
        else:
            direct_num = agency_num
            direct_text = agency_text
            direct_source = "same_as_agency"
            direct_note = "未见渠道区分公告，默认同代销"

        y1 = row["近1年"]
        y1_val = round(float(y1), 2) if pd.notna(y1) and str(y1).strip() != "" else None

        # 首年综合持有成本 = 1折优惠申购费率 + 管理费 + 托管费 + 销售服务费
        first_year_cost = round(
            purchase_fee + fee_info["mgmt_custody_fee"] + fee_info["service_fee"], 4
        )

        record = {
            "code": code,
            "name": name,
            "share_class": "C" if name.upper().endswith("C") else ("A" if name.upper().endswith("A") else "其他"),
            "status": str(row["申购状态"]),
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
            "nav_date": str(row["日期"]),
            "notice_title": notice["title"],
            "notice_date": notice["date"],
            "notice_url": f"https://fundf10.eastmoney.com/jjgg_{code}.html"
        }

        if category == "nasdaq":
            nasdaq_list.append(record)
        else:
            sp500_list.append(record)

    sort_fn = lambda x: (-x["agency_limit_num"], x["first_year_cost"])
    nasdaq_list.sort(key=sort_fn)
    sp500_list.sort(key=sort_fn)

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
