import os
import hmac
import hashlib
import base64
import json
import requests
from fastapi import FastAPI, Request, HTTPException
from google import genai

app = FastAPI()

# ── 設定 ──────────────────────────────────────────────────────
LINE_SECRET = os.environ.get("LINE_SECRET", "")
LINE_TOKEN = os.environ.get("LINE_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")

# 餐廳資料（之後補上）
RESTAURANT_INFO = """
你是一位餐廳客服助理，請用繁體中文回覆顧客。
以下是餐廳基本資料：

【待補充】
- 餐廳名稱：
- 地址：
- 營業時間：
- 電話：
- 菜單與價格：
- 訂位方式：
- 常見問題：

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

# ── Gemini 回覆 ────────────────────────────────────────────────
def ask_gemini(user_message: str) -> str:
    client = genai.Client(api_key=GEMINI_KEY)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=f"{RESTAURANT_INFO}\n\n顧客說：{user_message}"
    )
    return response.text.strip()

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
            reply = ask_gemini(user_msg)
            reply_message(reply_token, reply)

    return {"status": "ok"}

@app.get("/")
def root():
    return {"status": "LINE Bot is running"}
