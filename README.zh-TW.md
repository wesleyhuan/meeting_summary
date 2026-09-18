[English](README.md) | 繁體中文

# Meeting Summary

錄下電腦上任何一場會議 —— Webex、Google Meet、Teams 都可以 —— 取得即時字幕浮動視窗、標註發言者的完整逐字稿，以及由你自己的 Claude 訂閱寫出的會議摘要。

所有處理都在本機完成。音訊不會離開你的電腦：擷取、語音偵測、語音轉文字全部在本機執行，逐字稿存放在本機的 SQLite 檔案中。摘要則透過 MCP 使用**你現有的 Claude Desktop 訂閱** —— 不需要 API key，也不會按會議計費。

> **僅支援 Windows。** 擷取*其他人*發出的聲音必須依賴 WASAPI loopback，這是 Windows 專屬的機制。其餘部分都可以移植，唯獨這一塊不行。

---

## 功能

- **同時擷取通話的雙方。** 你的麥克風*以及*系統音訊輸出，分成兩條獨立的串流 —— 因此即使會議軟體從不把對方的聲音交給你，遠端與會者一樣會被轉成文字。
- **即時字幕顯示在置頂浮動視窗**，浮在會議視窗上方，不必切換視窗就能跟著讀。
- **儲存標註發言者的逐字稿**，每場會議一份，以 `you` 與 `other` 標示，合併成單一時間軸。
- **用你自己的 AI 訂閱做摘要。** 內建的 MCP server 讓 Claude Desktop 能讀取你的逐字稿並把摘要寫回來 —— 你只要開口要求即可。
- **瀏覽器 dashboard** 可以開始／結束會議、瀏覽過往逐字稿與摘要，並選擇麥克風、語言與 Whisper 模型大小。

## 架構

三個彼此獨立的行程共用同一個 SQLite 資料庫。任何一個都不需要其他兩個正在執行。

```
┌──────────────────────────┐        ┌─────────────────────────────┐
│  Overlay (tkinter)       │◄──WS───│  Helper service (FastAPI)   │
│  always-on-top captions  │        │                             │
└──────────────────────────┘        │  mic + system-loopback      │
                                     │  capture → VAD → Whisper    │
┌──────────────────────────┐◄──WS───│  → SQLite → WebSocket       │
│  Dashboard (browser)     │◄─REST──│                             │
│  meetings, transcripts,  │        └──────────────┬──────────────┘
│  summaries, settings     │                       │
└──────────────────────────┘             ┌─────────▼──────────┐
                                          │  SQLite            │
                                          │  meetings,         │
                                          │  transcript_segments,│
                                          │  summaries, settings│
                                          └─────────▲──────────┘
┌──────────────────────────┐                        │
│  Claude Desktop          │──── MCP (stdio) ───────┘
│  (your subscription)     │     mcp_server.py
└──────────────────────────┘
```

- **Helper service** —— 實際做事的部分：擷取音訊、VAD、Whisper 語音轉文字、寫入 SQLite、透過 WebSocket 推送。
- **Overlay** —— 置頂字幕視窗，透過 WebSocket 接收字幕。
- **Dashboard** —— 瀏覽器介面，用 REST 與 WebSocket 和 helper 溝通。
- **Claude Desktop** —— 透過 stdio 啟動 `mcp_server.py`，直接讀寫同一個資料庫。

字幕是在**停頓時**（約 0.8 秒的靜音）才定稿，而不是逐字跳出來 —— 語音轉文字跑在專屬的 queue 上，因此永遠不會卡住音訊擷取。

## 系統需求

- Windows
- Python 3.11 以上
- 可用的麥克風，以及喇叭／耳機（系統音訊是從你的預設輸出裝置以 loopback 方式擷取）
- 摘要功能需要：[Claude Desktop](https://claude.ai/download) 與有效的訂閱

## 安裝

```bash
git clone https://github.com/wesleyhuan/meeting_summary.git
cd meeting_summary
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

第一次錄製會議時會下載 `faster-whisper` 模型（只有一次，需要網路）。之後就完全離線運作。

> 本專案相依的是 `webrtcvad-wheels` 而非 `webrtcvad` —— 兩者是同一個套件，只是前者提供預先編譯好的 Windows wheel，所以你不需要安裝 Microsoft C++ Build Tools。

## 執行

**Helper service** —— 負責實際工作（擷取、轉文字、儲存）：

```bash
.venv\Scripts\python.exe -m uvicorn helper.main:app --port 8000
```

**Dashboard** —— helper 執行中時開啟 <http://localhost:8000/>。共有三個分頁：

- *Live* —— 開始／結束會議，並即時看到字幕進來
- *Meetings* —— 過往會議、逐字稿，以及任何已產生的摘要
- *Settings* —— 麥克風、語音轉文字語言、Whisper 模型大小，以及摘要所使用的 prompt

**Overlay** —— 選用的置頂字幕列，請在另一個終端機視窗執行：

```bash
.venv\Scripts\python.exe -m overlay.overlay
```

可以自由拖曳位置。先點一下視窗，再按 `Esc` 關閉（它是無邊框視窗，必須先取得焦點）。

## 透過 Claude Desktop 產生摘要

把下列內容加進 `%APPDATA%\Claude\claude_desktop_config.json` 的 `mcpServers` 物件中，並把路徑換成你 clone 這個 repo 的實際位置：

```json
"livesubtitle": {
  "command": "C:\\path\\to\\meeting_summary\\.venv\\Scripts\\python.exe",
  "args": ["C:\\path\\to\\meeting_summary\\mcp_server.py"]
}
```

如果該檔案裡已經有其他 MCP server，請把這段當成**新增的一個 key**，不要覆蓋整個物件。兩個路徑都必須是絕對路徑，而且 `command` 必須指向專案的 venv Python，因為這個 server 會 import 本 repo 內的模組。

接著**完整結束 Claude Desktop**（從系統匣結束，不是只關閉視窗），重新開啟後對它說類似 *「summarize my last meeting」* 的話。它會找到那場會議、讀取逐字稿、依照你在 Settings 設定的 prompt 產生摘要並寫回資料庫 —— 結果會出現在 dashboard 的 Meetings 分頁。

這個 server 提供四個工具：`list_meetings`、`get_meeting_transcript`、`get_summary_prompt`、`save_meeting_summary`。

## 測試

```bash
.venv\Scripts\python.exe -m pytest -v
```

共 87 個測試，涵蓋資料儲存、重新取樣、VAD 斷句、語音轉文字結果解析、擷取裝置接線、pipeline 協調、REST 與 WebSocket API、MCP 工具（包含對真實子行程進行完整的 stdio 往返測試），以及 overlay 的字幕格式化。

實體音訊硬體、WASAPI loopback 裝置，以及 Whisper 模型本身並不在自動化測試範圍內 —— 這些部分靠人工驗證。

## 現況與已知限制

本專案分階段開發，前三個階段已完成並可正常運作：

1. ✅ 核心擷取、語音轉文字、資料儲存，以及即時字幕浮動視窗
2. ✅ 瀏覽器 dashboard
3. ✅ 以訂閱制產生摘要的 MCP server
4. ⬜ 選用的 API key 直連摘要，給沒有 MCP client 的使用者

已知限制：

- 同一時間只能進行一場會議。
- 發言者標籤只有 `you` 與 `other` —— 無法區分多位遠端與會者各自是誰。
- 錄製需手動開始，不會自動偵測通話何時開始。
- Loopback 擷取一律使用你的預設輸出裝置，目前還無法選擇。
- Overlay 必須先點一下才能用 `Esc` 關閉。

## 專案結構

```
helper/          FastAPI 服務：音訊擷取、VAD、Whisper、SQLite、WebSocket、dashboard
overlay/         tkinter 置頂字幕視窗
mcp_server.py    給 Claude Desktop 使用的 stdio MCP server
tests/           pytest 測試
docs/            設計規格與各階段實作計畫
index.html       最初的獨立原型（瀏覽器 Web Speech API）—— 已被取代
python_subtitle.py  最初的獨立原型（CLI，僅麥克風）—— 已被取代
```

`index.html` 與 `python_subtitle.py` 是這個專案最早的原型。它們仍然可以單獨執行，但只聽得到你的麥克風，並不屬於目前這套系統的一部分。
