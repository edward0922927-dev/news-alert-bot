"""
AI 審核層：將過咗關鍵字篩選嘅候選新聞一次過交俾 LLM 判斷「值唔值得推」。

支援：
  provider: gemini     → 環境變數 GEMINI_API_KEY（Google AI Studio 免費額度）
  provider: anthropic  → 環境變數 ANTHROPIC_API_KEY（收費，但每月大約幾毫子）

失敗時 fail-open：返回 None，主程式照用關鍵字結果推送（標記「AI未審」），唔會漏新聞。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import requests

LAST_ERROR = ""  # 最近一次失敗原因（俾測試模式顯示）

PROMPT = """你係一個交易新聞分流分析員，幫一位做 Stage Analysis / VCP 嘅交易員過濾新聞推送。
今日 UTC 日期：{today}
Watchlist：{watchlist}

以下係候選新聞（JSON）。逐條判斷，嚴格啲 —— 寧願少推，唔好洗版。

評分 score（1–5）：
5 = 即時影響價格嘅硬消息（業績/指引、評級大改、監管/出口管制、停牌、重大併購、FOMC 決定、CPI/非農數據）
4 = 明確相關嘅重要新聞（大客戶/訂單、產品延誤/召回、管理層變動、重要宏觀政策）
3 = 相關但影響一般（行業動態、可信嘅報道）
2 = 觀點文章、評論、重溫舊聞、「Stock Movers」類綜合
1 = 唔相關／標錯 ticker／廣告／垃圾

判斷重點：
- 新唔新：標題提及嘅年份或事件係咪舊嘢重炒（例如今年係 {year}，標題講 {prev_year} 年或更早嘅事 → 標 stale）
- ticker 標得啱唔啱：例如 SpaceX / xAI 新聞除非直接影響 Tesla，否則唔應該當 TSLA
- 係咪觀點文（Seeking Alpha 作者文、「Buy/Sell」標題）→ opinion，最多 2–3 分
- 傳聞（"sources say"、"NYT says"、"reportedly"）→ 標 rumor，但如果重要照樣可以高分
- 標記為 "unmatched" 嘅新聞冇命中關鍵字，只有真正重要先俾 ≥4 分

只輸出 JSON array，每條一個 object，唔好有其他文字：
[{{"id": "...", "score": 1-5, "direction": "bull|bear|neutral|mixed",
   "tickers": ["NVDA"], "flags": ["stale","opinion","rumor","duplicate","mis-tagged","irrelevant"],
   "reason": "≤25字繁體中文，講點解重要／點解唔重要"}}]

候選新聞：
{items}
"""


CHOSEN_MODEL = ""  # 實際用咗邊個型號（俾測試模式顯示）


def pick_gemini_model(key: str) -> str:
    """問 Google 有邊啲型號，揀最新、非 preview、非 lite 嘅 Flash（揀唔到就退而求其次）。"""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"pageSize": 1000}, headers={"x-goog-api-key": key}, timeout=30)
    r.raise_for_status()
    best = None
    for m in r.json().get("models", []):
        name = m.get("name", "").split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        mm = re.match(r"^gemini-(\d+(?:\.\d+)?)-flash(-lite)?(?:-(.+))?$", name)
        if not mm or re.search(r"image|tts|live|audio|native|embed|vision", name):
            continue
        suffix = mm.group(3) or ""
        unstable = bool(re.search(r"preview|exp", suffix))
        rank = (float(mm.group(1)), not unstable, not mm.group(2), suffix == "")
        if best is None or rank > best[0]:
            best = (rank, name)
    if not best:
        raise RuntimeError("搵唔到可用嘅 Gemini Flash 型號")
    return best[1]


def _call_gemini(prompt: str, model: str, key: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
    r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=60)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_anthropic(prompt: str, model: str, key: str) -> str:
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                      json={"model": model, "max_tokens": 4000, "temperature": 0.1,
                            "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def _parse(text: str) -> list[dict]:
    text = text.strip()
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0) if m else text)


def review(items: list[dict], cfg: dict, now: float | None = None) -> list[dict] | None:
    """返回同 items 對應嘅 verdict list；失敗返回 None。"""
    llm = cfg.get("llm", {})
    provider = llm.get("provider", "gemini")
    model = llm.get("model") or ("auto" if provider == "gemini" else "claude-haiku-5-5")
    key = os.environ.get("GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY")
    global LAST_ERROR
    if not key:
        LAST_ERROR = f"未設定 {'GEMINI_API_KEY' if provider == 'gemini' else 'ANTHROPIC_API_KEY'}"
        print(f"  ! LLM: {LAST_ERROR}，跳過 AI 審核", file=sys.stderr)
        return None

    global CHOSEN_MODEL
    if provider == "gemini" and model == "auto":
        try:
            model = pick_gemini_model(key)
        except Exception as ex:
            LAST_ERROR = f"揀型號失敗 {type(ex).__name__}: {str(ex)[:150]}"
            print(f"  ! LLM: {LAST_ERROR}", file=sys.stderr)
            return None
    CHOSEN_MODEL = model
    print(f"  · LLM 型號：{model}")
    now = now or time.time()
    payload = [{"id": str(i), "source": it["source"], "title": it["title"],
                "summary": it.get("summary", "")[:300], "keyword_tags": it["tags"],
                "unmatched": it.get("unmatched", False),
                "published_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(it["ts"]))}
               for i, it in enumerate(items)]
    year = int(time.strftime("%Y", time.gmtime(now)))
    wl = ", ".join(f"{w['ticker']} ({'/'.join(w.get('aliases', []))})" for w in cfg.get("watchlist", []))
    batch = int(llm.get("batch_size", 40))
    verdicts: dict[str, dict] = {}

    for start in range(0, len(payload), batch):
        chunk = payload[start:start + batch]
        prompt = PROMPT.format(today=time.strftime("%Y-%m-%d", time.gmtime(now)), year=year,
                               prev_year=year - 1, watchlist=wl,
                               items=json.dumps(chunk, ensure_ascii=False, indent=1))
        for attempt in range(2):
            try:
                fn = _call_gemini if provider == "gemini" else _call_anthropic
                for v in _parse(fn(prompt, model, key)):
                    verdicts[str(v.get("id"))] = v
                break
            except Exception as ex:  # 網絡、429、JSON 唔合格
                LAST_ERROR = f"{type(ex).__name__}: {str(ex)[:150]}"
                code = getattr(getattr(ex, "response", None), "status_code", None)
                if provider == "gemini" and code == 404 and attempt == 0:  # 型號退役：自動換
                    try:
                        model = CHOSEN_MODEL = pick_gemini_model(key)
                        print(f"  · 型號 404，改用 {model}")
                        continue
                    except Exception:
                        pass
                print(f"  ! LLM {provider} 失敗（第 {attempt + 1} 次）：{type(ex).__name__}: {str(ex)[:150]}",
                      file=sys.stderr)
                time.sleep(5)
        else:
            return None

    return [verdicts.get(str(i)) for i in range(len(items))]
