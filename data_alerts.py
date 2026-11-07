"""
重要數據警報：國債息率、孳息曲線、VIX、信貸息差、美元。
數據每日更新一次（收市後），bot 每個鐘 check 一次，有新一日數據就按規則判斷。

來源（全部官方、免費）：
  treasury → 美國財政部每日孳息曲線 CSV（唔使 key）
  cboe     → CBOE 指數歷史 CSV（唔使 key）
  fred     → 聖路易斯聯儲 FRED（要免費 FRED_API_KEY；冇 key 就試公開 CSV，再唔得就跳過）

規則（每個 series 可以揀用邊啲）：
  change_1d / change_5d   1 日／5 日變動 ≥ X（unit=bp 時用基點；unit=pt 時用點數）
  change_1d_pct           1 日變動 ≥ X%
  zscore                  1 日變動 ≥ X 個標準差（同過去 120 日比）
  breakout_days           創 N 日新高／新低
  levels                  上穿／下穿某個水平
"""
from __future__ import annotations

import csv
import io
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta

import requests

UA = {"User-Agent": "Mozilla/5.0 (news-alert-bot)"}
_cache: dict[str, dict[str, float]] = {}


# ───────────────────────── 抓數據：每個函數返回 {date_iso: value} ─────────────────────────
def _treasury(column: str) -> dict[str, float]:
    key = f"treasury:{column}"
    if key in _cache:
        return _cache[key]
    out: dict[str, float] = {}
    y = date.today().year
    for yr in (y - 1, y):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{yr}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={yr}&page&_format=csv")
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.text)):
            v = row.get(column)
            if v not in (None, "", "N/A"):
                d = datetime.strptime(row["Date"], "%m/%d/%Y").date().isoformat()
                out[d] = float(v)
    _cache[key] = out
    return out


def _cboe(symbol: str) -> dict[str, float]:
    key = f"cboe:{symbol}"
    if key in _cache:
        return _cache[key]
    r = requests.get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv",
                     headers=UA, timeout=30)
    r.raise_for_status()
    out: dict[str, float] = {}
    cutoff = (date.today() - timedelta(days=800)).isoformat()
    for row in csv.reader(io.StringIO(r.text)):
        if not row or not row[0][:1].isdigit():
            continue
        d = datetime.strptime(row[0], "%m/%d/%Y").date().isoformat()
        if d >= cutoff:
            out[d] = float(row[-1] if len(row) == 2 else row[4])  # CLOSE
    _cache[key] = out
    return out


def _fred(series_id: str) -> dict[str, float]:
    key = f"fred:{series_id}"
    if key in _cache:
        return _cache[key]
    start = (date.today() - timedelta(days=800)).isoformat()
    api_key = os.environ.get("FRED_API_KEY")
    out: dict[str, float] = {}
    if api_key:
        r = requests.get("https://api.stlouisfed.org/fred/series/observations", headers=UA, timeout=30,
                         params={"series_id": series_id, "api_key": api_key, "file_type": "json",
                                 "observation_start": start})
        r.raise_for_status()
        for o in r.json().get("observations", []):
            if o["value"] not in (".", ""):
                out[o["date"]] = float(o["value"])
    else:  # 冇 key：試公開 CSV（有啲網絡環境會好慢／擋）
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}",
                         headers=UA, timeout=25)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))[1:]
        for d, v in (x for x in rows if len(x) == 2):
            if v not in (".", ""):
                out[d] = float(v)
    _cache[key] = out
    return out


def load_series(spec: dict) -> dict[str, float]:
    """支援 derived：例如 a - b（兩個同來源嘅 column/symbol 相減）。"""
    src = spec["source"]
    fn = {"treasury": _treasury, "cboe": _cboe, "fred": _fred}[src]
    if "minus" in spec:  # derived spread
        a, b = fn(spec["field"]), fn(spec["minus"])
        return {d: a[d] - b[d] for d in a if d in b}
    return fn(spec["field"])


# ───────────────────────── 規則判斷 ─────────────────────────
def _fmt_level(v: float, unit: str) -> str:
    if unit == "bp":  # 原始數值係 %，例如 4.52 或者 0.35（息差）
        return f"{v:.2f}%"
    return f"{v:.2f}"


def evaluate(spec: dict, series: dict[str, float], idx: int | None = None) -> tuple[list[str], dict]:
    """判斷第 idx 個觀察值（預設最新）有冇觸發規則。返回（觸發描述 list, 數值資訊）。"""
    dates = sorted(series)
    vals = [series[d] for d in dates]
    i = len(vals) - 1 if idx is None else idx
    if i < 6:
        return [], {}
    unit = spec.get("unit", "bp")
    mult = 100 if unit == "bp" else 1           # bp：% 差 × 100
    rules = spec.get("rules", {})
    v, p = vals[i], vals[i - 1]
    d1 = (v - p) * mult
    d5 = (v - vals[i - 5]) * mult
    pct = (v / p - 1) * 100 if p else 0
    u = "bp" if unit == "bp" else "點"
    hits = []

    if "change_1d" in rules and abs(d1) >= rules["change_1d"]:
        hits.append(("chg1d", f"1日{'急升' if d1 > 0 else '急跌'} {d1:+.0f}{u}（門檻 {rules['change_1d']}）"))
    if "change_5d" in rules and abs(d5) >= rules["change_5d"]:
        hits.append(("chg5d" + ("+" if d5 > 0 else "-"), f"5日{'累升' if d5 > 0 else '累跌'} {d5:+.0f}{u}（門檻 {rules['change_5d']}）"))
    if "change_1d_pct" in rules and abs(pct) >= rules["change_1d_pct"]:
        hits.append(("pct1d", f"1日{'急升' if pct > 0 else '急跌'} {pct:+.1f}%（門檻 {rules['change_1d_pct']}%）"))
    if "zscore" in rules:
        diffs = [(vals[k] - vals[k - 1]) * mult for k in range(max(1, i - 120), i)]
        if len(diffs) >= 30:
            sd = statistics.pstdev(diffs)
            if sd > 0 and abs(d1) / sd >= rules["zscore"]:
                hits.append(("z", f"異常波動 {abs(d1) / sd:.1f}σ（門檻 {rules['zscore']}σ）"))
    if "breakout_days" in rules:
        n = rules["breakout_days"]
        win = vals[max(0, i - n):i]
        if len(win) >= n * 0.8:
            if v > max(win):
                hits.append(("high", f"{n}日新高"))
            elif v < min(win):
                hits.append(("low", f"{n}日新低"))
    dirs = spec.get("level_dirs", "both")  # up = 只推上穿；down = 只推下穿
    for lv in rules.get("levels", []):
        if p < lv <= v and dirs in ("both", "up"):
            hits.append((f"lv{lv}+", spec["label_up"] if "label_up" in spec and lv == 0 else spec.get("label_up", "上穿") + f" {lv}"))
        elif p > lv >= v and dirs in ("both", "down"):
            hits.append((f"lv{lv}-", spec["label_down"] if "label_down" in spec and lv == 0 else spec.get("label_down", "下穿") + f" {lv}"))

    info = {"date": dates[i], "value": v, "d1": d1, "d5": d5, "pct": pct, "unit": u}
    return hits, info


def apply_cooldown(hits: list[tuple[str, str]], cool: dict, day: str, spec: dict) -> list[str]:
    """同一條規則（例如同一個水平、新高）喺冷卻期內唔再推；急升急跌類（chg/pct/z）冷卻 1 日。"""
    days = int(spec.get("cooldown_days", 10))
    out = []
    for key, text in hits:
        cd = 1 if key.startswith(("chg1d", "pct1d", "z")) else days
        last = cool.get(key)
        if last and (date.fromisoformat(day) - date.fromisoformat(last)).days < cd * 7 / 5:  # 交易日→日曆日
            continue
        cool[key] = day
        out.append(text)
    return out


# ───────────────────────── 主入口 ─────────────────────────
def check(cfg: dict, state: dict, now: float | None = None) -> list[dict]:
    dc = cfg.get("data_alerts", {})
    if not dc.get("enabled"):
        return []
    now = now or time.time()
    ds = state.setdefault("data", {})
    every = float(dc.get("check_every_minutes", 60)) * 60
    if now - ds.get("_last_check", 0) < every:
        return []
    ds["_last_check"] = now
    _cache.clear()
    out = []
    for spec in dc.get("series", []):
        sid = spec["id"]
        try:
            series = load_series(spec)
        except Exception as ex:
            print(f"  ! 數據 {sid}：{type(ex).__name__}: {str(ex)[:120]}", file=sys.stderr)
            continue
        if len(series) < 10:
            continue
        latest = max(series)
        rec = ds.setdefault(sid, {})
        if rec.get("last_date") == latest:
            continue
        first_time = "last_date" not in rec
        rec["last_date"] = latest
        if first_time:
            print(f"  · 數據 {sid}：seeded（{latest}）")
            continue
        raw, info = evaluate(spec, series)
        hits = apply_cooldown(raw, rec.setdefault("cool", {}), info.get("date", latest), spec)
        print(f"  · 數據 {sid} {latest} = {info.get('value')}：{hits or '冇觸發'}")
        if not hits:
            continue
        rising = info["d1"] > 0
        stress = rising if spec.get("rise_is_stress", True) else not rising
        lvl = _fmt_level(info["value"], spec.get("unit", "bp"))
        u = info["unit"]
        title = (f"{spec['name']} {lvl}（1日 {info['d1']:+.0f}{u}｜5日 {info['d5']:+.0f}{u}）"
                 if spec.get("unit", "bp") == "bp" else
                 (f"{spec['name']} {lvl}（1日 {info['d1']:+.2f}）" if "minus" in spec else
                  f"{spec['name']} {lvl}（1日 {info['d1']:+.2f}，{info['pct']:+.1f}%）"))
        out.append({"source": "📊 重要數據", "title": title, "link": spec.get("link", ""),
                    "ts": now, "tags": [sid], "sent": "🔴" if stress else "🟢",
                    "ai": "觸發：" + "、".join(hits) + f"（數據日期 {info['date']}）"
                          + (f"\n{spec['why']}" if spec.get("why") else ""),
                    "skip_ai": True, "summary": "", "unmatched": False})
    return out


def backtest(cfg: dict, days: int = 365) -> dict[str, list[tuple[str, list[str]]]]:
    """用歷史數據數返每個 series 過去 N 日會觸發幾多次（用嚟校準門檻）。"""
    res = {}
    for spec in cfg.get("data_alerts", {}).get("series", []):
        try:
            s = load_series(spec)
        except Exception as ex:
            res[spec["id"]] = [("ERR", [f"{type(ex).__name__}"])]
            continue
        dates = sorted(s)
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        hits, cool = [], {}
        for i, d in enumerate(dates):
            if d >= cutoff:
                raw, _ = evaluate(spec, s, i)
                h = apply_cooldown(raw, cool, d, spec)
                if h:
                    hits.append((d, h))
        res[spec["id"]] = hits
    return res
