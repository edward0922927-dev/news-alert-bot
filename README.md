# 📰 News Alert Bot（免費版）

15 個新聞來源 → 關鍵字過濾 → **AI 審核（可選）** → Telegram／ntfy／Discord 推送。

```
GitHub Actions（每 10 分鐘，雲端免費開一部 Linux 機）
  └─ 抓 RSS → 去重 → 關鍵字過濾 → Gemini 評分 1–5 → 推送 → 儲 state → 熄機
```
喺 GitHub Actions 每 10 分鐘跑一次，**唔使自己部機開住，$0**。

| 來源 | 途徑 | 實際延遲（估算） |
|---|---|---|
| Seeking Alpha | 官方 RSS（`market_currents.xml`、每隻股票嘅 feed） | 10–25 分鐘 |
| Reuters | Google News RSS（`site:reuters.com`） | 15–40 分鐘 |
| Bloomberg | Google News RSS（`site:bloomberg.com/news`） | 15–40 分鐘 |
| SEC 8-K（TSLA、NVDA） | SEC EDGAR 官方 Atom | 10–20 分鐘，通常早過新聞 |
| Fed | 官方新聞稿 RSS | 10–20 分鐘 |
| MarketWatch、Investing.com | 官方 RSS | 10–20 分鐘 |
| 韓媒（SK hynix） | Google News | 15–40 分鐘 |
| CoinDesk、The Block | 官方 RSS | 10–20 分鐘 |
| X/Twitter | ❌ 冇免費 API → 用 X app 開鐘仔通知 | 秒級 |

延遲 = GitHub cron 本身嘅延遲（經常遲 5–15 分鐘）＋ Google News 收錄時間。

---

## 設定（大約 10 分鐘）

### 1. 揀推送渠道（揀一個就得，可以同時開幾個）

| 渠道 | 要設嘅 Secret | 設定 | 特點 |
|---|---|---|---|
| **ntfy**（最簡單） | `NTFY_TOPIC` | 手機裝 ntfy app → 訂閱一個你自己作嘅長名（例如 `ed-news-8f3k2q9x`）→ 將同一個名放入 secret | 唔使開戶口；每條新聞一個通知，撳落去直接開原文。⚠️ topic 係公開嘅，靠個名難估先保密 |
| **Telegram** | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | 見下面 | 一個訊息整合幾條，方便回看 |
| **Discord** | `DISCORD_WEBHOOK_URL`（可加 `DISCORD_WEBHOOK_AJU`） | 見下面「Discord 設定」 | 同阿豬嘅 Discord 喺同一個 app 睇；可以分頻道 |

#### Discord 設定（大約 3 分鐘，建議用電腦）
1. Discord 左邊欄撳 **＋** → **Create My Own** → **For me and my friends** → 改名（例如 `Ed News`）
2. 開兩個文字頻道：`#news`（一般新聞）、`#阿豬`（阿豬新 post／新片）
3. 喺 `#news` 撳 ⚙️ **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**
   → 放入 GitHub secret `DISCORD_WEBHOOK_URL`
4. 喺 `#阿豬` 重複第 3 步 → 放入 `DISCORD_WEBHOOK_AJU`（唔設嘅話，阿豬啲通知會推去 `#news`）
5. 手機推送：喺你個 server 撳 server 名 → **Notification Settings** → 揀 **All Messages** ⚠️（要確認係咪預設已經開咗）
6. ⚠️ Webhook URL 等於密碼，有咗佢任何人都可以喺你個頻道出 post，唔好貼出去

**阿豬嘅會員 Discord 點樣一齊睇：**
- 最簡單：兩個 server 都喺同一個 Discord app，左邊欄撳一下就切換
- 如果阿豬個 server 有 **Announcement 頻道**（頻道名前面係📢 擴音器 icon）：hover 個頻道 → 撳 **Follow** → 揀你自己 server 嘅 `#阿豬`。佢 publish 嘅公告就會自動轉貼去你個頻道 ⚠️（要佢個 server 有用 Announcement 頻道先得）
- ❌ 唔建議用程式讀佢個 server：要 server 管理員加 bot 先得；用自己個帳號跑 script（self-bot）違反 Discord 條款，可能會被封號。而且會員內容本身係付費內容

#### Telegram 設定
1. Telegram 搵 **@BotFather** → `/newbot` → 改名 → 攞到 **token**（`123456:ABC...`）
2. 搵你個新 bot，撳 **Start**，隨便發一句說話俾佢
3. 瀏覽器開：`https://api.telegram.org/bot<你個TOKEN>/getUpdates`
   → 喺 `"chat":{"id":123456789` 搵到你嘅 **chat id**

### 2. 開 GitHub repo
1. github.com → New repository → 揀 **Public**（Actions 分鐘數免費，見下面嘅「限制」）
2. 將成個 folder 上載（包括 `.github/workflows/news-alert.yml` 同 `state/` 資料夾）
   - 網頁上載：Add file → Upload files → 拖成個 folder 入去
   - ⚠️ macOS Finder 預設會隱藏 `.github`，按 `Cmd+Shift+.` 顯示

### 3. 加 Secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret | 必要？ | 內容 |
|---|---|---|
| 推送渠道（第 1 步） | ✅ 最少一個 | — |
| `GEMINI_API_KEY` | AI 審核用 | aistudio.google.com → Get API key（免費，唔使信用卡） |
| `SEC_USER_AGENT` | SEC feed 用 | SEC 規定要有聯絡方式，格式例如 `Ed NewsBot your@email.com`；冇就自動跳過 SEC |
| `X_BEARER_TOKEN` | 可選 | 跟 X 帳戶用（收費，見「X / Twitter 帳戶」） |
| `ANTHROPIC_API_KEY` | 可選 | 改用 Claude 審核（`config.yaml` → `provider: anthropic`） |

### 4. 啟動
Repo → **Actions** → 揀 `news-alert` → **Run workflow**
- 第一次跑會將現有新聞**靜默入庫**（唔推，免得洗版）
- 之後每 10 分鐘自動跑，有新嘢先推

---

## 阿豬嘅渠道

| 渠道 | 抓到啲咩 | 設定 |
|---|---|---|
| Patreon（`patreon.com/ahju`） | 新 post 嘅**標題、時間、連結**；內容要登入會員先睇到 | 已經預設開咗，唔使 key |
| YouTube「阿豬 Ah Ju」 | 新片 | 已經預設開咗 |
| 會員 Discord | ❌ 唔可以自動抓（見上面 Discord 設定） | 用 Follow 功能，或者直接開 app 睇 |

- 兩條都設成 `skip_ai: true`，即係**唔經 AI 審核，一定推**，標 🐷
- 想將阿豬每星期嘅 watchlist 加入新聞過濾：喺 `config.yaml` 嘅 `watchlist` 加返啲 ticker 就得（需要你自己手動抄，因為 watchlist 係會員內容）

## X / Twitter 帳戶（例如 @trading_for_bj）

免費嘅 X→RSS 途徑（nitter、xcancel、rsshub 等）**全部已經失效**（2026-11 測試：HTTP 410／451／404／429）。

| 方法 | 成本 | 延遲 | 設定 |
|---|---|---|---|
| **X app 鐘仔通知**（推薦先用） | 免費 | 秒級 | 開佢個 profile → 撳 🔔 → **All posts** |
| 官方 API 入 bot | 每讀一個 post $0.005；一個帳戶每日 10 個 post 大約 **$1.5／月** ⚠️ | 10–20 分鐘 | 見下面 |

**官方 API 設定：**
1. developer.x.com → 登入 → Developer Console → 開 Project／App
2. **Keys and tokens** → 複製 **Bearer Token** → 放入 GitHub secret `X_BEARER_TOKEN`
3. 喺 Console 買 credits（pay-per-use 要預先買）
4. 跑一日之後返 Console 睇用量，確認同估算差唔多

- bot 用 `since_id` 只攞新 post，第一次跑記低 user id，之後唔再查
- ⚠️ 未確定「冇新 post 嘅查詢」會唔會收錢；如果用量明顯高過估算，將 cron 改慢（例如 `*/30`）
- 加其他帳戶：喺 `config.yaml` 複製 `X @trading_for_bj` 嗰段，改 `name` 同 `username`
- 呢部分係我用模擬回應測試嘅，未用真 token 試過

## AI 審核點運作

每輪將過咗關鍵字篩選嘅新聞**一次過**交俾 Gemini，逐條評分：

| 分數 | 意思 | 例子 |
|---|---|---|
| 5 🔥 | 即時影響價格嘅硬消息 | 業績、FOMC、出口管制、停牌 |
| 4 | 明確相關嘅重要新聞 | 大訂單、召回、高層變動 |
| 3 | 相關，影響一般 | 行業動態 |
| 2 | 觀點文、舊聞、綜合 | SA 作者「Buy」文、Stock Movers |
| 1 | 唔相關／標錯 ticker | SpaceX 新聞被標 TSLA |

- 推送條件：分數 ≥ `min_score`（預設 3），而且冇 `stale`／`irrelevant`／`duplicate`／`mis-tagged` 呢啲 flag
- `scan_unmatched: true`：冇命中關鍵字嘅新聞都交俾 AI 睇，要 **≥4 分**先推（用嚟捉關鍵字漏咗嘅大新聞）
- 每條推送會附 AI 嘅一句理由，例如「🔥5 FOMC 決議，即時影響全市場」
- **AI 判斷記錄**：Actions → 揀某次 run → `Fetch & push` 嘅 log，✓／✗ 加理由，可以 check 佢審得啱唔啱
- **AI 失敗**（冇 key、429、斷線）→ 照推關鍵字命中嘅新聞，標「⚠️AI未審」，唔會漏
- 用量：只喺有新聞先會 call，每日大約 30–80 次。Gemini 免費額度大約每日 1,000–1,500 次 ⚠️（以 AI Studio 顯示為準）。免費額度嘅內容 Google 可能用嚟訓練，不過新聞標題本身都係公開資料

## 自訂（只改 `config.yaml`）

| 想做 | 改邊度 |
|---|---|
| 加／減股票 | `watchlist`：加 `ticker` 同 `aliases` |
| 加某隻股票嘅 SA feed | `feeds` 加一條 `https://seekingalpha.com/api/sa/combined/XXXX.xml` |
| 宏觀事件提示 | `keywords`（FOMC、CPI、tariff…） |
| 太嘈 | 將 feed 改做 `filter: true`；或者喺 `exclude_patterns` 加 regex |
| 情緒 📈📉 唔準 | 改 `bullish_words`／`bearish_words` |
| 頻率 | `.github/workflows/news-alert.yml` 嘅 `cron`（最快 `*/5`） |
| AI 更嚴 | `llm.min_score: 4` |
| 關 AI | `llm.enabled: false` |
| 加 SEC 公司 | 複製 SEC TSLA 嗰段，改 `CIK=`（喺 sec.gov 搵公司）同 `tags` |

**Google News 搜尋語法：** `site:reuters.com (Tesla OR Nvidia)`、`site:bloomberg.com/news "SK hynix"`；可以用 `-` 排除字詞。

---

## 限制同注意事項

| 項目 | 說明 |
|---|---|
| GitHub 分鐘數 | Public repo 用標準 runner 免費。Private repo（Free plan）每月 2,000 分鐘；每 10 分鐘跑一次大約要 4,300 分鐘，**會超**。用 private 就將 cron 改做 `*/30`。⚠️ 以 GitHub 最新定價為準 |
| 60 日自動停用 | Public repo 如果 60 日冇 activity，GitHub 會停咗 scheduled workflow。bot 每次 commit `state/seen.json` 應該算 activity ⚠️（未證實）；就算俾人停咗，喺 Actions 頁撳 Enable 就得 |
| Seeking Alpha 403 | 如果 log 出 `HTTP 403`（SA 封咗 GitHub 嘅 IP），開返 `config.yaml` 最底被註解咗嘅「SA (via Google)」 |
| Google News 舊聞 | 偶然會將舊文章當新文章推出嚟（例如標題寫住 2024），無法完全避免 |
| `Musk` 別名 | SpaceX／xAI 新聞都會標 #TSLA；唔想要就喺 aliases 刪咗佢 |
| RSS 條款 | SA feed 列明只限個人、非商業用途 |

## 本機跑（可選，延遲可以壓到 1–5 分鐘）
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python news_bot.py --dry-run      # 先試，唔發送
# crontab -e：每 3 分鐘
*/3 * * * * cd /path/to/news-alert-bot && /usr/bin/python3 news_bot.py >> bot.log 2>&1
```
