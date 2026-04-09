import os
import hmac
import hashlib
import base64
import json
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# ── 設定 ──────────────────────────────────────────────────────
LINE_SECRET = os.environ.get("LINE_SECRET", "")
LINE_TOKEN = os.environ.get("LINE_TOKEN", "")
MINIMAX_KEY = os.environ.get("MINIMAX_KEY", "")

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
    print("MiniMax response:", data)  # 除錯用，確認回傳格式

    # 標準 OpenAI 格式（choices 不為 None）
    if data.get("choices"):
        content = data["choices"][0]["message"]["content"].strip()
        # 移除 <think>...</think> 思考過程
        import re
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content

    # MiniMax 自有格式（reply 欄位）
    if data.get("reply"):
        return data["reply"].strip()

    # 回傳錯誤訊息給顧客
    error_msg = data.get("base_resp", {}).get("status_msg") or data.get("error", {}).get("message", str(data))
    print("MiniMax error:", error_msg)
    return "抱歉，系統暫時無法回應，請稍後再試。"

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
        if event["type"] == "message" and event["message"]["type"] == "text":
            user_msg = event["message"]["text"]
            reply_token = event["replyToken"]
            reply = ask_minimax(user_msg)
            reply_message(reply_token, reply)

    return {"status": "ok"}

@app.get("/")
def root():
    return {"status": "LINE Bot is running"}
