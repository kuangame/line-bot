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

# 餐廳資料（之後補上）
RESTAURANT_INFO = """
你是一位餐廳客服助理，請用繁體中文回覆顧客。
以下是餐廳基本資料：

- 餐廳名稱：新象園婚宴會館
- 地址：嘉義縣中埔鄉和睦村司公廍3-19號
- 電話：05-2398979
- 營業時間：（待補充）
- 菜單與價格：（待補充）
- 訂位方式：（待補充）
- 常見問題：（待補充）

---
回覆規則：
1. 如果顧客的問題在上述資料範圍內，直接回答。
2. 如果問題複雜（如客訴、特殊需求、無法回答），請回覆：「您好，這個問題我幫您轉交給專人處理，請稍候，我們會盡快回覆您。」
3. 回覆簡短有禮，不要太冗長。
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

    reply = await asyncio.to_thread(ask_minimax, combined)

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
