#!/usr/bin/env python3
"""
News Alert Bot — RSS / Google News → 過濾 → Telegram 推送
設計俾 GitHub Actions 每 5–10 分鐘跑一次（亦可以喺本機 cron 跑）。

環境變數：
  TELEGRAM_BOT_TOKEN   BotFather 俾你嘅 token
  TELEGRAM_CHAT_ID     你個 chat id
選項：
  --dry-run            唔發 Telegram，淨係 print 出嚟
  --config PATH        預設 config.yaml
  --state PATH         預設 state/seen.json
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests
import yaml

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
STATE_KEEP_DAYS = 14
TG_LIMIT = 4000  # Telegram 上限 4096，留位


# ───────────────────────── 工具 ─────────────────────────
def gn_url(query: str, window: str = "1d") -> str:
    q = f"{query} when:{window}"
    return f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US:en"


def norm_title(t: str) -> str:
    # 去除 Google News 尾巴 " - Reuters" 同符號，用嚟跨來源去重
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def entry_epoch(e) -> float | None:
    for k in ("published_parsed", "updated_parsed"):
        v = e.get(k)
        if v:
            return calendar.timegm(v)
    return None


def word_re(words, case_sensitive=False):
    if not words:
        return None
    pat = r"(?<![A-Za-z0-9])(" + "|".join(re.escape(w) for w in words) + r")(?![A-Za-z0-9])"
    return re.compile(pat, 0 if case_sensitive else re.IGNORECASE)


# ───────────────────────── 狀態 ─────────────────────────
def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            pass
    return {"seen": {}, "titles": {}, "seeded_feeds": []}


def save_state(path: Path, st: dict) -> None:
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    st["titles"] = {k: v for k, v in st["titles"].items() if v >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, indent=0, sort_keys=True), "utf-8")


# ───────────────────────── 抓取 ─────────────────────────
def fetch(url: str) -> feedparser.FeedParserDict | None:
    ua = UA
    if "sec.gov" in url:  # SEC 規定 User-Agent 要有聯絡方式
        ua = os.environ.get("SEC_USER_AGENT", "")
        if not ua:
            print("  ! 跳過 SEC feed：未設定 SEC_USER_AGENT", file=sys.stderr)
            return None
    try:
        r = requests.get(url, headers={"User-Agent": ua, "Accept": "application/rss+xml,application/xml,*/*"},
                         timeout=20)
        if r.status_code != 200:
            print(f"  ! HTTP {r.status_code}: {url}", file=sys.stderr)
            return None
        return feedparser.parse(r.content)
    except requests.RequestException as ex:
        print(f"  ! {type(ex).__name__}: {url}", file=sys.stderr)
        return None


def fetch_patreon(campaign_id: str, count: int = 10):
    """Patreon 公開 API：只攞 post 標題、時間、連結（內容要會員登入先睇到）。"""
    from datetime import datetime
    url = (f"https://www.patreon.com/api/posts?filter[campaign_id]={campaign_id}"
           f"&filter[contains_exclusive_posts]=true&sort=-published_at"
           f"&fields[post]=title,published_at,url&page[count]={count}")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            print(f"  ! Patreon HTTP {r.status_code}", file=sys.stderr)
            return None
        entries = []
        for p in r.json().get("data", []):
            a = p.get("attributes", {})
            ts = datetime.fromisoformat(a["published_at"].replace("Z", "+00:00")).utctimetuple() \
                if a.get("published_at") else None
            entries.append(feedparser.FeedParserDict(
                id=f"patreon-{p['id']}", title=a.get("title", ""), link=a.get("url", ""),
                published_parsed=ts, summary=""))
        return feedparser.FeedParserDict(entries=entries)
    except (requests.RequestException, ValueError, KeyError) as ex:
        print(f"  ! Patreon {type(ex).__name__}", file=sys.stderr)
        return None


def fetch_x(username: str, state: dict, count: int = 5):
    """X 官方 API（pay-per-use）。用 since_id 只攞新 post，盡量慳錢。冇 X_BEARER_TOKEN 就跳過。"""
    from datetime import datetime
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        print(f"  ! 跳過 X @{username}：未設定 X_BEARER_TOKEN", file=sys.stderr)
        return None
    hdr = {"Authorization": f"Bearer {token}", "User-Agent": "news-alert-bot"}
    xs = state.setdefault("x", {}).setdefault(username.lower(), {})
    try:
        if not xs.get("id"):  # 第一次先查 user id，之後記住唔再查
            r = requests.get(f"https://api.x.com/2/users/by/username/{username}", headers=hdr, timeout=20)
            if r.status_code != 200:
                print(f"  ! X user lookup {r.status_code}: {r.text[:150]}", file=sys.stderr)
                return None
            xs["id"] = r.json()["data"]["id"]
        params = {"max_results": max(5, min(count, 100)), "tweet.fields": "created_at"}
        if xs.get("since_id"):
            params["since_id"] = xs["since_id"]
        r = requests.get(f"https://api.x.com/2/users/{xs['id']}/tweets", headers=hdr, params=params, timeout=20)
        if r.status_code != 200:
            print(f"  ! X timeline {r.status_code}: {r.text[:150]}", file=sys.stderr)
            return None
        js = r.json()
        if js.get("meta", {}).get("newest_id"):
            xs["since_id"] = js["meta"]["newest_id"]
        entries = []
        for t in js.get("data", []):
            ts = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).utctimetuple() \
                if t.get("created_at") else None
            text = re.sub(r"\s+", " ", t.get("text", "")).strip()
            entries.append(feedparser.FeedParserDict(
                id=f"x-{t['id']}", title=(text[:220] + "…") if len(text) > 220 else text,
                link=f"https://x.com/{username}/status/{t['id']}", published_parsed=ts, summary=text))
        return feedparser.FeedParserDict(entries=entries)
    except (requests.RequestException, ValueError, KeyError) as ex:
        print(f"  ! X {type(ex).__name__}", file=sys.stderr)
        return None


# ───────────────────────── 分類 ─────────────────────────
class Matcher:
    def __init__(self, cfg: dict):
        self.tickers = []  # (ticker, ticker_re, alias_re)
        for w in cfg.get("watchlist", []):
            t = str(w["ticker"])
            self.tickers.append((t, word_re([t], case_sensitive=True), word_re(w.get("aliases", []))))
        self.kw_re = word_re(cfg.get("keywords", []))
        self.excl = [re.compile(p, re.IGNORECASE) for p in cfg.get("exclude_patterns", [])]
        self.bull_re = word_re(cfg.get("bullish_words", []))
        self.bear_re = word_re(cfg.get("bearish_words", []))

    def tags(self, text: str) -> list[str]:
        out = []
        for t, tre, are in self.tickers:
            if (tre and tre.search(text)) or (are and are.search(text)):
                out.append(t)
        if self.kw_re:
            out += sorted({m.group(1) for m in self.kw_re.finditer(text)}, key=str.lower)
        return out

    def sentiment(self, title: str) -> str:
        b = len(self.bull_re.findall(title)) if self.bull_re else 0
        s = len(self.bear_re.findall(title)) if self.bear_re else 0
        return "📈" if b > s else "📉" if s > b else "▫️"


# ───────────────────────── Telegram ─────────────────────────
def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                         "disable_web_page_preview": "true"}, timeout=20)
            if r.status_code == 200:
                return True
            if r.status_code == 429:  # rate limited
                time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            print(f"  ! Telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return False
        except requests.RequestException as ex:
            print(f"  ! Telegram {type(ex).__name__}", file=sys.stderr)
            time.sleep(2)
    return False


def send_ntfy(topic: str, it: dict, server: str = "https://ntfy.sh") -> bool:
    # 用 JSON publish，避免 header 唔支援中文/emoji
    t = time.strftime("%H:%M", time.gmtime(it["ts"]))
    body = {"topic": topic, "title": f"{it['sent']} {it['source']} · {t} UTC",
            "message": it["title"] + (f"\n{it['ai']}" if it.get("ai") else "")
                       + (f"\n{' '.join('#' + x for x in it['tags'][:4])}" if it["tags"] else ""),
            "click": it["link"]}
    for attempt in range(4):
        try:
            r = requests.post(server.rstrip("/") + "/", json=body, timeout=20)
            if r.status_code == 200:
                return True
            if r.status_code == 429:  # ntfy.sh 限流：等一陣再試
                time.sleep(5 * (attempt + 1))
                continue
            print(f"  ! ntfy {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return False
        except requests.RequestException as ex:
            print(f"  ! ntfy {type(ex).__name__}", file=sys.stderr)
            time.sleep(3)
    return False


def send_discord(webhook: str, items: list[dict]) -> bool:
    lines = []
    for it in items:
        t = time.strftime("%H:%M", time.gmtime(it["ts"]))
        tags = " ".join(f"`{x}`" for x in it["tags"][:4])
        ai = f"\n{it['ai']}" if it.get("ai") else ""
        lines.append(f"{it['sent']} **{it['title']}**{ai}\n*{it['source']} · {t} UTC* {tags}\n<{it['link']}>")
    ok = True
    for msg in chunk_messages(lines, limit=1900):
        try:
            r = requests.post(webhook, json={"content": msg}, timeout=20)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 1)
                r = requests.post(webhook, json={"content": msg}, timeout=20)
            ok &= r.status_code in (200, 204)
        except requests.RequestException:
            ok = False
        time.sleep(1)
    return ok


def chunk_messages(lines: list[str], limit: int = TG_LIMIT) -> list[str]:
    msgs, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 2 > limit:
            msgs.append(cur)
            cur = ""
        cur += ln + "\n\n"
    if cur.strip():
        msgs.append(cur)
    return msgs


# ───────────────────────── 主流程 ─────────────────────────
SEC_ITEMS = {"1.01": "重大協議", "1.02": "終止協議", "2.01": "收購/出售資產", "2.02": "業績",
             "2.05": "重組/裁員", "2.06": "減值", "3.01": "除牌通知", "5.02": "高層變動",
             "7.01": "Reg FD 披露", "8.01": "其他事件"}


def sec_title(tag: str, summary: str) -> str:
    items = re.findall(r"Item (\d\.\d\d)", summary)
    desc = "、".join(SEC_ITEMS.get(i, i) for i in items if i != "9.01") or "其他"
    return f"{tag} 8-K：{desc}"


def run(cfg: dict, state: dict, fetcher=fetch, now: float | None = None) -> list[dict]:
    now = now or time.time()
    m = Matcher(cfg)
    scan_unmatched = cfg.get("llm", {}).get("enabled") and cfg.get("llm", {}).get("scan_unmatched")
    max_age = cfg.get("max_age_hours", 6) * 3600
    seeded = set(state.get("seeded_feeds", []))
    new_items = []

    for f in cfg.get("feeds", []):
        name = f["name"]
        if f.get("type") == "patreon":
            feed = fetch_patreon(str(f["campaign_id"]))
        elif f.get("type") == "x_user":
            feed = fetch_x(f["username"], state)
        else:
            feed = fetcher(gn_url(f["query"]) if f.get("type") == "google_news" else f["url"])
        if feed is None:
            continue
        first_time = name not in seeded
        cnt = 0
        for e in feed.entries:
            title = html.unescape(e.get("title", "")).strip()
            if not title:
                continue
            link = e.get("link", "")
            eid = h(e.get("id") or link or title)
            tkey = h(norm_title(title))
            if eid in state["seen"]:
                continue
            state["seen"][eid] = now
            dup_title = tkey in state["titles"]
            state["titles"].setdefault(tkey, now)
            if first_time or dup_title:
                continue  # 第一次見呢個 feed：靜默入庫，唔洗版
            ts = entry_epoch(e)
            if ts and now - ts > max_age:
                continue
            if any(p.search(title) for p in m.excl):
                continue
            summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("summary", ""))).strip()
            forced = [str(x) for x in f.get("tags", [])]
            if f.get("sec"):
                title = sec_title(forced[0] if forced else "", summary)
            tags = forced + [t for t in m.tags(f"{title} {summary}") if t not in forced]
            unmatched = False
            if f.get("filter", True) and not tags:
                if not scan_unmatched:
                    continue
                unmatched = True  # 冇命中關鍵字，交俾 AI 決定
            new_items.append({"source": name, "title": title, "link": link, "ts": ts or now,
                              "tags": tags, "sent": f.get("icon") or m.sentiment(title),
                              "summary": summary[:400], "unmatched": unmatched,
                              "skip_ai": bool(f.get("skip_ai")),
                              "discord_env": f.get("discord_webhook_env"),
                              "ai_required": bool(f.get("ai_required"))})
            cnt += 1
        if first_time:
            seeded.add(name)
            print(f"  · seeded {name} ({len(feed.entries)} items, 唔推)")
        else:
            print(f"  · {name}: {cnt} new")

    state["seeded_feeds"] = sorted(seeded)
    new_items.sort(key=lambda x: x["ts"])
    return new_items


DIR_ICON = {"bull": "📈", "bear": "📉", "mixed": "↔️", "neutral": "▫️"}


def apply_ai(items: list[dict], verdicts: list[dict | None] | None, cfg: dict) -> list[dict]:
    """套用 AI 判斷；verdicts=None（AI 失敗）→ 照推關鍵字命中嘅，標「AI未審」。"""
    llm = cfg.get("llm", {})
    min_score = int(llm.get("min_score", 3))
    drop_flags = set(llm.get("drop_flags", ["stale", "irrelevant", "duplicate"]))
    out = []
    if verdicts is None:
        for it in items:
            if not it.get("unmatched") and not it.get("ai_required"):
                it["ai"] = "⚠️AI未審"
                out.append(it)
        return out
    for it, v in zip(items, verdicts):
        if not v:  # AI 漏咗呢條：保守處理，同失敗一樣
            if not it.get("unmatched") and not it.get("ai_required"):
                it["ai"] = "⚠️AI未審"
                out.append(it)
            continue
        score, flags = int(v.get("score", 0) or 0), set(v.get("flags") or [])
        need = max(min_score, 4) if it.get("unmatched") else min_score
        keep = score >= need and not (flags & drop_flags)
        print(f"  {'✓' if keep else '✗'} [{score}] {it['title'][:70]} — {v.get('reason', '')} {sorted(flags)}")
        if not keep:
            continue
        it["sent"] = DIR_ICON.get(v.get("direction"), it["sent"])
        if v.get("tickers"):
            it["tags"] = [str(t) for t in v["tickers"]] + [t for t in it["tags"] if t not in v["tickers"]]
        it["ai"] = f"{'🔥' if score >= 5 else '⭐'}{score} {v.get('reason', '')}" + \
                   (f"（{'/'.join(sorted(flags))}）" if flags else "")
        it["score"] = score
        out.append(it)
    return out


def format_item(it: dict) -> str:
    t = time.strftime("%H:%M", time.gmtime(it["ts"]))
    tags = " ".join(f"#{re.sub(r'[^A-Za-z0-9_]', '_', x)}" for x in it["tags"][:4])
    ai = f"\n{html.escape(it['ai'])}" if it.get("ai") else ""
    return (f"{it['sent']} <b>{html.escape(it['title'])}</b>{ai}\n"
            f"<i>{html.escape(it['source'])} · {t} UTC</i> {tags}\n"
            f"<a href=\"{html.escape(it['link'], quote=True)}\">開原文</a>")


def push_all(items: list[dict], lines: list[str]) -> bool:
    env = os.environ.get
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    topic, discord = env("NTFY_TOPIC"), env("DISCORD_WEBHOOK_URL")
    ok = True
    if token and chat:
        for msg in chunk_messages(lines):
            ok &= send_telegram(token, chat, msg)
            time.sleep(1)
    if topic:
        for it in items:
            ok &= send_ntfy(topic, it, env("NTFY_SERVER") or "https://ntfy.sh")
            time.sleep(2)
    groups: dict[str, list] = {}
    for it in items:
        hook = env(it.get("discord_env") or "") or discord
        if hook:
            groups.setdefault(hook, []).append(it)
    for hook, its in groups.items():
        ok &= send_discord(hook, its)
    return ok


def ai_test(cfg: dict, hours: float, dry: bool) -> int:
    """唔理 state，攞過去 N 個鐘嘅新聞俾 AI 審，推最高分嘅（最多 8 條），唔儲 state。"""
    import llm_review
    cfg = dict(cfg, max_age_hours=hours)
    cfg["llm"] = dict(cfg.get("llm", {}), enabled=True)
    fake = {"seen": {}, "titles": {}, "seeded_feeds": [f["name"] for f in cfg.get("feeds", [])]}
    items = [i for i in run(cfg, fake) if not i.get("skip_ai")]
    items = items[-80:]  # 控制 token 用量
    print(f"AI 測試：{len(items)} 條候選（過去 {hours:g} 小時）")
    verdicts = llm_review.review(items, cfg)
    now = time.time()
    if verdicts is None:
        head = {"source": "AI 測試", "title": f"❌ AI 審核失敗：{llm_review.LAST_ERROR or '未知原因'}",
                "link": "https://aistudio.google.com/", "ts": now, "tags": [], "sent": "🧪"}
        kept = []
    else:
        kept = apply_ai(items, verdicts, cfg)
        kept = sorted(kept, key=lambda x: (x.get("score", 0), x["ts"]))[-8:]
        dist = {}
        for v in verdicts:
            if v:
                dist[int(v.get("score", 0) or 0)] = dist.get(int(v.get("score", 0) or 0), 0) + 1
        d = " ".join(f"{k}分×{dist[k]}" for k in sorted(dist, reverse=True))
        head = {"source": "AI 測試", "ts": now, "tags": [], "sent": "🧪",
                "link": "https://github.com/edward0922927-dev/news-alert-bot/actions",
                "title": f"✅ [{llm_review.CHOSEN_MODEL}] {len(items)} 條候選 → 推 {len(kept)} 條（最高分）。分佈：{d}"}
    out = [head] + kept
    lines = [format_item(i) for i in out]
    if dry:
        print("\n".join(re.sub(r"<[^>]+>", "", ln) for ln in lines))
        return 0
    return 0 if push_all(out, lines) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default="state/seen.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ai-test", type=float, metavar="HOURS",
                    help="測試 AI：攞過去 N 個鐘嘅新聞俾 AI 審，推最高分幾條；唔改 state")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text("utf-8"))
    if a.ai_test:
        return ai_test(cfg, a.ai_test, a.dry_run)
    st_path = Path(a.state)
    state = load_state(st_path)

    items = run(cfg, state)
    if cfg.get("data_alerts", {}).get("enabled"):
        import data_alerts
        try:
            items += data_alerts.check(cfg, state)
        except Exception as ex:  # 數據出事唔好拖死新聞推送
            print(f"  ! 數據警報：{type(ex).__name__}: {ex}", file=sys.stderr)
    to_review = [i for i in items if not i.get("skip_ai")]
    if to_review and cfg.get("llm", {}).get("enabled"):
        import llm_review
        n0 = len(to_review)
        kept = apply_ai(to_review, llm_review.review(to_review, cfg), cfg)
        print(f"  AI 審核：{n0} → {len(kept)}")
        items = sorted([i for i in items if i.get("skip_ai")] + kept, key=lambda x: x["ts"])
    cap = cfg.get("max_items_per_run", 25)
    overflow = max(0, len(items) - cap)
    items = items[-cap:]  # 保留最新嘅

    lines = [format_item(i) for i in items]
    if overflow:
        lines.insert(0, f"⚠️ 呢輪有 {overflow} 條較舊新聞略過咗")

    if lines:
        if a.dry_run:
            print("\n".join(re.sub(r"<[^>]+>", "", ln) for ln in lines))
        else:
            env = os.environ.get
            token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
            topic, discord = env("NTFY_TOPIC"), env("DISCORD_WEBHOOK_URL")
            if not ((token and chat) or topic or discord or env("DISCORD_WEBHOOK_AJU")):
                print("未設定任何推送渠道（Telegram / ntfy / Discord 揀一個）", file=sys.stderr)
                return 1
            ok = True
            if token and chat:
                for msg in chunk_messages(lines):
                    ok &= send_telegram(token, chat, msg)
                    time.sleep(1)
            if topic:
                for it in items:
                    ok &= send_ntfy(topic, it, env("NTFY_SERVER") or "https://ntfy.sh")
                    time.sleep(2)
            # Discord：feed 可以用 discord_webhook_env 指定另一個頻道（例如阿豬專用）
            groups: dict[str, list] = {}
            for it in items:
                hook = env(it.get("discord_env") or "") or discord
                if hook:
                    groups.setdefault(hook, []).append(it)
            for hook, its in groups.items():
                ok &= send_discord(hook, its)
            if not ok:
                print("推送失敗，唔儲 state，下輪重試", file=sys.stderr)
                return 1
    print(f"done: {len(items)} pushed")
    save_state(st_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
