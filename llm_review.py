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

AI 晶片需求鏈（交易員最關心，呢類消息可以扭轉成個晶片需求）：
- Hyperscaler（MSFT/GOOGL/AMZN/META/ORCL）加減 capex、取消/縮減/延遲算力或數據中心合約、轉用自家晶片 → 4–5
- AI 融資鏈出現壓力：AI 公司或數據中心發債困難、信貸息差/CDS 擴闊、private credit 收緊、監管審查數據中心貸款、
  OpenAI/CoreWeave/Oracle 等高負債買家嘅資金問題、vendor/circular financing 被質疑 → 4–5
- 供應鏈瓶頸或競爭格局改變：HBM/DRAM 價格、CoWoS 產能、TSMC/Samsung 搶 HBM、出口管制 → 4
- 一般 AI 行業評論、未落實嘅計劃、重複報道 → 2–3
- 大科技公司同 AI/雲端/晶片無關嘅新聞（例如 Amazon 零售、Google 反壟斷官司未影響 capex）→ 1–2

Crypto（交易員本身喺 crypto 行業工作）：
- 現貨 ETF 單日大額資金流入／流出、新 ETF 批出／拒絕 → 4–5
- 監管／立法落實（SEC/CFTC 行動、市場結構法案、穩定幣法、國家級 BTC 儲備）→ 4–5
- 交易所／DeFi 被黑、暫停提款、破產、穩定幣脫鈎或儲備問題 → 5（規模細嘅 DeFi 被黑 → 3）
- 大額清算、MSTR 等公司大手買入／賣出 BTC、指數剔除風險、礦企大規模拋售 → 4
- 宏觀對 crypto 嘅直接影響（美元、息口、流動性）→ 3–4
- 價格評論、「price prediction」、技術分析文、山寨幣宣傳、空投、NFT → 1–2
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
    return gemini_candidates(key)[0]


def gemini_candidates(key: str, n: int = 4) -> list[str]:
    """問 Google 有邊啲型號，按「新、穩定、非 lite」排序，返回頭 n 個 Flash 型號。"""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"pageSize": 1000}, headers={"x-goog-api-key": key}, timeout=15)
    r.raise_for_status()
    ranked = []
    for m in r.json().get("models", []):
        name = m.get("name", "").split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        mm = re.match(r"^gemini-(\d+(?:\.\d+)?)-flash(-lite)?(?:-(.+))?$", name)
        if not mm or re.search(r"image|tts|live|audio|native|embed|vision", name):
            continue
        suffix = mm.group(3) or ""
        unstable = bool(re.search(r"preview|exp", suffix))
        ranked.append(((float(mm.group(1)), not unstable, not mm.group(2), suffix == ""), name))
    if not ranked:
        raise RuntimeError("搵唔到可用嘅 Gemini Flash 型號")
    ranked.sort(reverse=True)
    out = [ranked[0][1]]
    # 後備：同代 lite、上一代穩定版 —— 503 通常係單一型號爆滿，換型號就得
    for _, name in ranked[1:]:
        if name not in out:
            out.append(name)
        if len(out) >= n:
            break
    return out


def _call_gemini(prompt: str, model: str, key: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
    r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=35)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_anthropic(prompt: str, model: str, key: str) -> str:
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=35,
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
    models = [model]
    if provider == "gemini":
        try:
            cands = gemini_candidates(key)
            models = cands if model == "auto" else [model] + [c for c in cands if c != model]
        except Exception as ex:
            if model == "auto":
                LAST_ERROR = f"揀型號失敗 {type(ex).__name__}: {str(ex)[:150]}"
                print(f"  ! LLM: {LAST_ERROR}", file=sys.stderr)
                return None
    print(f"  · LLM 候選型號：{models}")
    now = now or time.time()
    payload = [{"id": str(i), "source": it["source"], "title": it["title"],
                "summary": it.get("summary", "")[:300], "keyword_tags": it["tags"],
                "unmatched": it.get("unmatched", False),
                "published_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(it["ts"]))}
               for i, it in enumerate(items)]
    year = int(time.strftime("%Y", time.gmtime(now)))
    wl = "; ".join(f"{w['ticker']} ({'/'.join(w.get('aliases', []))})" + (f"〔{w['note']}〕" if w.get("note") else "")
                   for w in cfg.get("watchlist", []))
    batch = int(llm.get("batch_size", 40))
    deadline = time.time() + float(llm.get("max_seconds", 90))  # 唔好拖死成個 workflow
    verdicts: dict[str, dict] = {}

    for start in range(0, len(payload), batch):
        chunk = payload[start:start + batch]
        prompt = PROMPT.format(today=time.strftime("%Y-%m-%d", time.gmtime(now)), year=year,
                               prev_year=year - 1, watchlist=wl,
                               items=json.dumps(chunk, ensure_ascii=False, indent=1))
        fn = _call_gemini if provider == "gemini" else _call_anthropic
        done = False
        for mdl in models:
            for attempt in range(3):
                if time.time() > deadline:
                    LAST_ERROR = f"超時（{LAST_ERROR}）"
                    return None
                try:
                    for v in _parse(fn(prompt, mdl, key)):
                        verdicts[str(v.get("id"))] = v
                    CHOSEN_MODEL = mdl
                    models = [mdl] + [m for m in models if m != mdl]  # 下一批先用呢個
                    done = True
                    break
                except Exception as ex:  # 網絡、429、5xx、JSON 唔合格
                    LAST_ERROR = f"[{mdl}] {type(ex).__name__}: {str(ex)[:150]}"
                    code = getattr(getattr(ex, "response", None), "status_code", None)
                    print(f"  ! LLM {mdl} 失敗（第 {attempt + 1} 次）：{LAST_ERROR}", file=sys.stderr)
                    if code in (400, 401, 403):
                        return None  # key／請求有問題，換型號都冇用
                    if code == 404:
                        break        # 型號退役：即刻換下一個
                    if attempt < 2:
                        time.sleep(4 * (attempt + 1))
            if done:
                break
        if not done:
            return None

    return [verdicts.get(str(i)) for i in range(len(items))]
