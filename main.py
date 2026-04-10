import os
import hmac
import hashlib
import base64
import json
import re
import asyncio
import time
from collections import deque
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# ── 設定 ──────────────────────────────────────────────────────
LINE_SECRET = os.environ.get("LINE_SECRET", "")
LINE_TOKEN  = os.environ.get("LINE_TOKEN", "")
MINIMAX_KEY = os.environ.get("MINIMAX_KEY", "")

BUFFER_SECONDS   = 5      # 等待訊息的秒數
HUMAN_TIMEOUT_HR = 2      # 人工模式自動逾時（小時）
DONE_KEYWORD     = "/done" # 真人輸入此指令解除人工模式

RATE_LIMIT_COUNT  = 10    # 時間窗內最多幾條
RATE_LIMIT_WINDOW = 60    # 時間窗（秒）

# 訊息 buffer（key: user_id）
_pending_messages: dict[str, list[str]] = {}
_pending_tokens:   dict[str, str]       = {}
_pending_tasks:    dict[str, asyncio.Task] = {}

# 人工模式（key: user_id, value: 進入時間 timestamp）
_human_mode: dict[str, float] = {}

# Rate limit（key: user_id, value: 訊息時間戳 deque）
_rate_timestamps: dict[str, deque] = {}
# 已發送過警告的用戶（避免重複警告）
_rate_warned: set[str] = set()

# 餐廳資料
RESTAURANT_INFO = """
你是新象園婚宴會館的 LINE 客服助理，請用繁體中文回覆顧客，語氣親切有禮，回覆簡短不冗長。

## 基本資料
- 餐廳名稱：新象園婚宴會館
- 電話：05-2398979
- 地址：嘉義縣中埔鄉和睦村司公廍3-19號
- 營業時間：平日及假日均為 11:00~14:00、16:00~21:00
- 4月公休日：4/7、4/29（若遇公休日則隔日依序回覆）

## 菜單與價格
- 桌菜方案：3000／3500／4000／5000／6000／8000／10000（需加一成服務費）
- 素食選項：套餐或合菜
- 飲料：大部分 60 元，可提供酒水單參考

## 訂位方式
- 電話訂位：05-2398979
- 線上訂位：請留下姓名與聯絡電話；若為遊覽車或旅行社，請留下車名／旅行社名及領隊電話（領隊未定請留旅行社電話）

## 喜宴場地
- 可容納桌數：38 桌
- 停車場：多（免費）
- 無障礙設施：有
- 包廂：
  - 大象廳：20 人桌，低消 5000
  - 小象廳：12 人桌 × 2 桌，低消 5000
  - 龍鳳廳：15 人 1 桌，低消 5000
- 若需舞台且桌數 15 桌以下，酌收場地費 28000；無需舞台則無此限

## 常見問題
- 可以自帶蛋糕嗎？→ 可以
- 有提供喜帖嗎？→ 沒有，但有提供禮金簿
- 可以場地布置嗎？→ 可以
- 有香檳塔嗎？需加價嗎？→ 有，不需加價
- 文定儀式場地需另外嗎？→ 通常在舞台前，需告知人數，無需加價
- 旅行社標案需提供公司資料嗎？→ 是的，請提供
- 發票怎麼開？→ 旅行社發票需外加 5% 稅金，為手開發票
- 有無開瓶費？→ 若攜帶與酒水單重複品項，每桌酌收 500 元
- 可以試菜嗎？→ 可以；喜宴試菜 85 折，團體試菜無折扣
- 生日優惠？→ 每桌 5000 以上招待豬腳麵線（每桌一份，需提早預約）
- 可以帶寵物嗎？→ 可以；喜宴時請幫毛孩穿尿布或用推車，以免隨地便溺
- 可以刷卡嗎？→ 團體及喜宴僅收現金
- 如何匯訂？→ 喜宴訂金 20000 元；下訂後若需改期請聯繫人員；婚禮前 3 個月簽訂宴會資訊，桌數及細節於宴客前 2 週告知即可
- 婚禮有什麼方案？→ 早鳥優惠：下訂 2027 年婚期，免收一成服務費

## 關鍵字優先回覆
遇到以下關鍵字，請優先照此回答：
- 訂位／我要訂位／想訂位 → 「您好！訂位請來電 05-2398979，或留下您的姓名與聯絡電話，我們將盡快與您確認。」
- 地址／在哪／怎麼去 → 「我們位於嘉義縣中埔鄉和睦村司公廍3-19號，歡迎來訪！」
- 營業時間／幾點開 → 「我們平日及假日均為 11:00~14:00、16:00~21:00。」
- 電話／聯絡 → 「請來電 05-2398979，我們很樂意為您服務！」

---
回覆規則：
1. 問題在上述資料範圍內，直接簡短回答。
2. 問題複雜、有客訴或無法回答，請回覆：「您好，這個問題我幫您轉交給專人處理，請稍候，我們會盡快回覆您。」
"""

# ── 人工模式工具函數 ────────────────────────────────────────────
def is_human_mode(user_id: str) -> bool:
    if user_id not in _human_mode:
        return False
    elapsed_hr = (time.time() - _human_mode[user_id]) / 3600
    if elapsed_hr >= HUMAN_TIMEOUT_HR:
        _human_mode.pop(user_id, None)
        print(f"[human_mode] {user_id} 逾時自動解除")
        return False
    return True

def enable_human_mode(user_id: str):
    _human_mode[user_id] = time.time()
    print(f"[human_mode] {user_id} 進入人工模式")

def disable_human_mode(user_id: str):
    _human_mode.pop(user_id, None)
    print(f"[human_mode] {user_id} 解除人工模式")

# ── Rate limit 工具函數 ─────────────────────────────────────────
def is_rate_limited(user_id: str) -> bool:
    now = time.time()
    q = _rate_timestamps.setdefault(user_id, deque())

    # 移除時間窗外的舊紀錄
    while q and now - q[0] > RATE_LIMIT_WINDOW:
        q.popleft()

    if len(q) >= RATE_LIMIT_COUNT:
        return True

    q.append(now)
    _rate_warned.discard(user_id)  # 進入新時間窗，重置警告狀態
    return False

# ── 驗證 LINE 簽名 ─────────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    hash = hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(hash).decode()
    return hmac.compare_digest(expected, signature)

# ── 回覆 LINE 訊息 ─────────────────────────────────────────────
def reply_message(reply_token: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
    )

# ── MiniMax 回覆 ────────────────────────────────────────────────
def ask_minimax(user_message: str) -> str:
    response = requests.post(
        "https://api.minimax.io/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {MINIMAX_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "MiniMax-M2.7",
            "messages": [
                {"role": "system", "content": RESTAURANT_INFO},
                {"role": "user", "content": user_message},
            ],
        },
        timeout=30,
    )
    data = response.json()
    print("MiniMax response:", data)

    if data.get("choices"):
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content

    if data.get("reply"):
        return data["reply"].strip()

    error_msg = data.get("base_resp", {}).get("status_msg") or data.get("error", {}).get("message", str(data))
    print("MiniMax error:", error_msg)
    return "抱歉，系統暫時無法回應，請稍後再試。"

# ── 等待後合併訊息並回覆 ────────────────────────────────────────
async def process_buffered(user_id: str):
    await asyncio.sleep(BUFFER_SECONDS)

    messages    = _pending_messages.pop(user_id, [])
    reply_token = _pending_tokens.pop(user_id, None)
    _pending_tasks.pop(user_id, None)

    if not messages or not reply_token:
        return

    # 人工模式：不回覆，讓真人處理
    if is_human_mode(user_id):
        print(f"[human_mode] {user_id} 人工模式中，略過 AI 回覆")
        return

    combined = "\n".join(messages)
    print(f"[buffer] user={user_id} messages={messages}")

    try:
        reply = await asyncio.to_thread(ask_minimax, combined)
    except Exception as e:
        print(f"[error] ask_minimax 失敗：{e}")
        await asyncio.to_thread(
            reply_message, reply_token,
            "抱歉，系統目前忙碌中，請稍後再試，或直接來電 05-2398979。"
        )
        return

    # AI 判斷需要轉人工 → 啟動人工模式
    HANDOFF_PHRASES = ["幫您轉交給專人", "轉交給專人處理"]
    if any(phrase in reply for phrase in HANDOFF_PHRASES):
        enable_human_mode(user_id)

    await asyncio.to_thread(reply_message, reply_token, reply)

# ── Webhook ────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    # TODO: 上線後開回簽名驗證
    # if not verify_signature(body, signature):
    #     raise HTTPException(status_code=400, detail="Invalid signature")

    events = json.loads(body)["events"]
    for event in events:
        if event["type"] != "message" or event["message"]["type"] != "text":
            continue

        user_id     = event["source"]["userId"]
        user_msg    = event["message"]["text"]
        reply_token = event["replyToken"]

        # Rate limit 檢查
        if is_rate_limited(user_id):
            if user_id not in _rate_warned:
                _rate_warned.add(user_id)
                await asyncio.to_thread(
                    reply_message, reply_token,
                    "您傳送訊息的速度太快，請稍後再試。"
                )
            continue

        # 真人輸入 /done → 解除人工模式（不累積進 buffer）
        if user_msg.strip() == DONE_KEYWORD:
            disable_human_mode(user_id)
            await asyncio.to_thread(reply_message, reply_token, "已恢復 AI 自動回覆。")
            continue

        # 累積訊息，保留最新的 reply_token
        _pending_messages.setdefault(user_id, []).append(user_msg)
        _pending_tokens[user_id] = reply_token

        # 取消舊 task，重新計時
        if user_id in _pending_tasks:
            _pending_tasks[user_id].cancel()

        task = asyncio.create_task(process_buffered(user_id))
        _pending_tasks[user_id] = task

    return {"status": "ok"}

@app.get("/")
def root():
    return {"status": "LINE Bot is running"}
