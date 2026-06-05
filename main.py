import os
import re
import hmac
import hashlib
import base64
import json
import asyncio
import time
import requests
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from collections import deque

app = FastAPI()

# ── 環境設定 ────────────────────────────────────────────────────
LINE_SECRET    = os.environ.get("LINE_SECRET", "")
LINE_TOKEN     = os.environ.get("LINE_TOKEN", "")
MINIMAX_KEY    = os.environ.get("MINIMAX_KEY", "")
ADMIN_USER_ID  = os.environ.get("ADMIN_USER_ID", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = "kuangame/line-bot"
GITHUB_BRANCH  = "main"

BUFFER_SECONDS    = 5
HUMAN_TIMEOUT_HR  = 2
DONE_KEYWORD      = "/done"
RATE_LIMIT_COUNT  = 10
RATE_LIMIT_WINDOW = 60

# ── 狀態儲存（in-memory） ────────────────────────────────────────
_pending_messages: dict[str, list[str]] = {}
_pending_tokens:   dict[str, str]       = {}
_pending_tasks:    dict[str, asyncio.Task] = {}
_human_mode:       dict[str, float]     = {}
_rate_timestamps:  dict[str, deque]     = {}
_rate_warned:      set[str]             = set()
_recent_users:     dict[str, dict]      = {}   # {user_id: {last_msg, timestamp}}

# ── Config ───────────────────────────────────────────────────────
_config: dict = {"restaurant_info": "", "keyword_replies": []}
_config_sha: str = ""

def _gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

def load_config():
    global _config, _config_sha
    if not GITHUB_TOKEN:
        print("[config] 未設定 GITHUB_TOKEN，跳過 GitHub 載入")
        return
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/config.json",
            headers=_gh_headers(), timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            _config_sha = data["sha"]
            _config = json.loads(base64.b64decode(data["content"]).decode())
            print(f"[config] 載入成功，{len(_config.get('keyword_replies', []))} 個關鍵字規則")
        elif r.status_code == 404:
            _push_config("init: create config.json")
        else:
            print(f"[config] 載入失敗：{r.status_code}")
    except Exception as e:
        print(f"[config] 載入失敗：{e}")

def _push_config(message: str) -> bool:
    global _config_sha
    try:
        content = base64.b64encode(
            json.dumps(_config, ensure_ascii=False, indent=2).encode()
        ).decode()
        body: dict = {
            "message": message,
            "content": content,
            "branch": GITHUB_BRANCH,
        }
        if _config_sha:
            body["sha"] = _config_sha
        r = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/config.json",
            headers=_gh_headers(), json=body, timeout=15,
        )
        if r.status_code in (200, 201):
            _config_sha = r.json()["content"]["sha"]
            print("[config] 已推送到 GitHub")
            return True
        print(f"[config] 推送失敗：{r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[config] 推送失敗：{e}")
    return False

def upload_image_to_github(filename: str, content: bytes) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    path = f"images/{safe}"
    sha = None
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
            headers=_gh_headers(), timeout=10,
        )
        if r.status_code == 200:
            sha = r.json()["sha"]
    except Exception:
        pass
    body: dict = {
        "message": f"upload image: {safe}",
        "content": base64.b64encode(content).decode(),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
        headers=_gh_headers(), json=body, timeout=30,
    )
    if r.status_code in (200, 201):
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{path}"
    raise Exception(f"圖片上傳失敗：{r.status_code}")

# ── Startup ───────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    load_config()

# ── Admin auth ────────────────────────────────────────────────────
def require_auth(request: Request):
    pw = request.headers.get("X-Admin-Password", "")
    if not ADMIN_PASSWORD or pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ── 人工模式 ──────────────────────────────────────────────────────
def is_human_mode(user_id: str) -> bool:
    if user_id not in _human_mode:
        return False
    if (time.time() - _human_mode[user_id]) / 3600 >= HUMAN_TIMEOUT_HR:
        _human_mode.pop(user_id)
        return False
    return True

def enable_human_mode(user_id: str):
    _human_mode[user_id] = time.time()
    print(f"[human] {user_id} 進入人工模式")

def disable_human_mode(user_id: str):
    _human_mode.pop(user_id, None)
    print(f"[human] {user_id} 解除人工模式")

# ── Rate limit ────────────────────────────────────────────────────
def is_rate_limited(user_id: str) -> bool:
    now = time.time()
    q = _rate_timestamps.setdefault(user_id, deque())
    while q and now - q[0] > RATE_LIMIT_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT_COUNT:
        return True
    q.append(now)
    _rate_warned.discard(user_id)
    return False

# ── LINE 工具 ─────────────────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)

def reply_messages(reply_token: str, messages: list):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": messages[:5]},
        timeout=10,
    )

def reply_text(reply_token: str, text: str):
    reply_messages(reply_token, [{"type": "text", "text": text}])

def push_text(user_id: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )

# ── 關鍵字比對 ────────────────────────────────────────────────────
def find_text_rule(user_msg: str) -> dict | None:
    """找第一個有文字回覆的關鍵字規則。"""
    msg_lower = user_msg.lower()
    for rule in _config.get("keyword_replies", []):
        if rule.get("message_type") not in ("text", "both"):
            continue
        if not rule.get("text"):
            continue
        for kw in rule.get("keywords", []):
            if kw.lower() in msg_lower:
                return rule
    return None

def find_images_in_text(text: str) -> list[str]:
    """掃描任意文字，找出所有命中關鍵字的圖片 URL（去重、最多 4 張）。"""
    text_lower = text.lower()
    found: list[str] = []
    seen: set[str] = set()
    for rule in _config.get("keyword_replies", []):
        url = rule.get("image_url")
        if not url or url in seen:
            continue
        for kw in rule.get("keywords", []):
            if kw.lower() in text_lower:
                found.append(url)
                seen.add(url)
                break
    return found[:4]

# ── 清理 Markdown 符號 ───────────────────────────────────────────
def strip_markdown(text: str) -> str:
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)   # ## 標題
    text = re.sub(r'\*{1,3}([^*]+?)\*{1,3}', r'\1', text)        # **粗體** *斜體*
    text = re.sub(r'^\s*[*\-]\s+', '', text, flags=re.MULTILINE) # * - 列表
    return text.strip()

# ── MiniMax ───────────────────────────────────────────────────────
def ask_minimax(user_message: str) -> str:
    try:
        r = requests.post(
            "https://api.minimax.io/v1/chat/completions",
            headers={"Authorization": f"Bearer {MINIMAX_KEY}", "Content-Type": "application/json"},
            json={
                "model": "MiniMax-M2.7",
                "messages": [
                    {"role": "system", "content": _config.get("restaurant_info", "")},
                    {"role": "user", "content": user_message},
                ],
            },
            timeout=30,
        )
        data = r.json()
        if data.get("choices"):
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            return strip_markdown(content)
        if data.get("reply"):
            return strip_markdown(data["reply"].strip())
        print("[minimax] error:", data)
    except Exception as e:
        print("[minimax] exception:", e)
    return "抱歉，系統暫時無法回應，請稍後再試，或直接來電 05-2398979。"

# ── Buffer ────────────────────────────────────────────────────────
async def process_buffered(user_id: str):
    try:
        await asyncio.sleep(BUFFER_SECONDS)
    except asyncio.CancelledError:
        return

    messages    = _pending_messages.pop(user_id, [])
    reply_token = _pending_tokens.pop(user_id, None)
    _pending_tasks.pop(user_id, None)

    if not messages or not reply_token:
        return
    if is_human_mode(user_id):
        return

    combined = "\n".join(messages)

    # 文字：關鍵字優先，否則走 AI
    text_rule = find_text_rule(combined)
    if text_rule:
        reply_text_content = text_rule["text"]
    else:
        try:
            reply_text_content = await asyncio.to_thread(ask_minimax, combined)
        except Exception as e:
            print(f"[error] {e}")
            await asyncio.to_thread(
                reply_text, reply_token,
                "抱歉，系統暫時無法回應，請稍後再試，或直接來電 05-2398979。"
            )
            return

        HANDOFF_PHRASES = ["幫您轉交給專人", "轉交給專人處理"]
        if any(p in reply_text_content for p in HANDOFF_PHRASES):
            enable_human_mode(user_id)
            if ADMIN_USER_ID:
                asyncio.create_task(asyncio.to_thread(
                    push_text, ADMIN_USER_ID,
                    f"⚠️ 顧客需要真人處理\nUser ID: {user_id}\n\n對顧客傳 /done 可解除接管（2小時自動解除）"
                ))

    # 圖片：掃描 AI/關鍵字的回覆文字，自動附上提到的包廂/圖片
    image_urls = find_images_in_text(reply_text_content)

    # 組合並發送（1 則文字 + 最多 4 張圖）
    line_msgs: list = [{"type": "text", "text": reply_text_content}]
    for url in image_urls:
        line_msgs.append({"type": "image", "originalContentUrl": url, "previewImageUrl": url})

    await asyncio.to_thread(reply_messages, reply_token, line_msgs[:5])

# ── Webhook ───────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    events = json.loads(body).get("events", [])
    for event in events:
        if event["type"] != "message" or event["message"]["type"] != "text":
            continue

        user_id     = event["source"]["userId"]
        user_msg    = event["message"]["text"].strip()
        reply_token = event["replyToken"]

        _recent_users[user_id] = {"last_msg": user_msg[:60], "timestamp": time.time()}

        if is_rate_limited(user_id):
            if user_id not in _rate_warned:
                _rate_warned.add(user_id)
                await asyncio.to_thread(reply_text, reply_token, "您傳送訊息的速度太快，請稍後再試。")
            continue

        if user_msg == DONE_KEYWORD:
            disable_human_mode(user_id)
            await asyncio.to_thread(reply_text, reply_token, "已恢復 AI 自動回覆。")
            continue

        if is_human_mode(user_id):
            continue

        _pending_messages.setdefault(user_id, []).append(user_msg)
        _pending_tokens[user_id] = reply_token

        if user_id in _pending_tasks:
            _pending_tasks[user_id].cancel()
        _pending_tasks[user_id] = asyncio.create_task(process_buffered(user_id))

    return {"status": "ok"}

# ── Admin API ─────────────────────────────────────────────────────
@app.get("/admin/config")
def api_get_config(request: Request):
    require_auth(request)
    return _config

@app.post("/admin/config")
async def api_update_config(request: Request):
    require_auth(request)
    global _config
    _config = await request.json()
    ok = _push_config("update config via admin panel")
    return {"ok": ok}

@app.post("/admin/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    require_auth(request)
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "圖片不能超過 5MB")
    url = await asyncio.to_thread(upload_image_to_github, file.filename, content)
    return {"url": url}

@app.post("/admin/reload")
def api_reload(request: Request):
    require_auth(request)
    load_config()
    return {"ok": True}

@app.get("/admin/humans")
def api_list_humans(request: Request):
    require_auth(request)
    now = time.time()
    result = []
    for uid, info in sorted(_recent_users.items(), key=lambda x: -x[1]["timestamp"]):
        if now - info["timestamp"] > 86400:
            continue
        result.append({
            "user_id": uid,
            "last_msg": info["last_msg"],
            "timestamp": info["timestamp"],
            "human_mode": is_human_mode(uid),
        })
    return result

@app.post("/admin/humans/{user_id}")
def api_enable_human(user_id: str, request: Request):
    require_auth(request)
    enable_human_mode(user_id)
    return {"ok": True}

@app.delete("/admin/humans/{user_id}")
def api_disable_human(user_id: str, request: Request):
    require_auth(request)
    disable_human_mode(user_id)
    _recent_users.pop(user_id, None)
    return {"ok": True}

@app.get("/admin/versions")
def api_list_versions(request: Request):
    require_auth(request)
    if not GITHUB_TOKEN:
        return []
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/commits",
            headers=_gh_headers(),
            params={"path": "config.json", "per_page": 20},
            timeout=10,
        )
        if r.status_code == 200:
            return [{
                "sha":     c["sha"],
                "message": c["commit"]["message"],
                "date":    c["commit"]["committer"]["date"],
            } for c in r.json()]
    except Exception as e:
        print(f"[versions] {e}")
    return []

@app.post("/admin/versions/{commit_sha}/restore")
def api_restore_version(commit_sha: str, request: Request):
    require_auth(request)
    global _config
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/config.json",
            headers=_gh_headers(),
            params={"ref": commit_sha},
            timeout=10,
        )
        if r.status_code == 200:
            _config = json.loads(base64.b64decode(r.json()["content"]).decode())
            ok = _push_config(f"restore: revert to {commit_sha[:7]}")
            return {"ok": ok, "config": _config}
    except Exception as e:
        print(f"[restore] {e}")
    return {"ok": False}

# ── Admin 頁面 ────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return ADMIN_HTML

@app.get("/")
def root():
    return {"status": "LINE Bot is running"}


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>新象園後台</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #111; color: #e0e0e0; min-height: 100vh; }
.login-wrap { display: flex; justify-content: center; align-items: center; min-height: 100vh; }
.login-box { background: #1e1e1e; border-radius: 16px; padding: 36px; width: 320px; border: 1px solid #2a2a2a; }
.login-box h1 { font-size: 20px; margin-bottom: 24px; color: #fff; text-align: center; }
input, textarea, select { width: 100%; padding: 10px 14px; border: 1px solid #333; border-radius: 8px; background: #2a2a2a; color: #e0e0e0; font-size: 14px; margin-bottom: 12px; outline: none; }
input:focus, textarea:focus, select:focus { border-color: #4ade80; }
textarea { resize: vertical; font-family: 'Courier New', monospace; line-height: 1.5; }
button { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; transition: opacity .15s; }
button:hover { opacity: .85; }
.btn-green { background: #4ade80; color: #000; }
.btn-red { background: #ef4444; color: #fff; }
.btn-sm { padding: 5px 12px; font-size: 12px; }
.btn-outline { background: transparent; border: 1px solid #444; color: #ccc; }
.btn-full { width: 100%; }
header { background: #1a1a1a; padding: 14px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #222; position: sticky; top: 0; z-index: 10; }
header h1 { font-size: 16px; color: #fff; }
.tabs { display: flex; background: #161616; border-bottom: 1px solid #222; }
.tab { padding: 13px 20px; cursor: pointer; font-size: 14px; color: #777; border-bottom: 2px solid transparent; }
.tab.active { color: #4ade80; border-bottom-color: #4ade80; }
.container { max-width: 720px; margin: 0 auto; padding: 20px; }
.section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.section-title { font-size: 15px; font-weight: 600; color: #fff; }
.card { background: #1e1e1e; border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; border: 1px solid #2a2a2a; }
.card-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; flex: 1; }
.tag { background: #2a2a2a; border-radius: 20px; padding: 2px 10px; font-size: 12px; color: #aaa; border: 1px solid #333; }
.card-preview { font-size: 12px; color: #666; margin-top: 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.type-badge { font-size: 11px; color: #4ade80; background: #1a3a22; border-radius: 4px; padding: 1px 6px; margin-right: 6px; }
.row { display: flex; gap: 8px; }
.empty { text-align: center; padding: 60px 0; color: #444; }
.overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75); z-index: 50; justify-content: center; align-items: flex-start; padding: 20px; overflow-y: auto; }
.overlay.open { display: flex; }
.modal { background: #1e1e1e; border-radius: 14px; padding: 24px; width: 100%; max-width: 480px; margin: auto; border: 1px solid #2a2a2a; }
.modal h2 { font-size: 16px; color: #fff; margin-bottom: 18px; }
label { display: block; font-size: 12px; color: #888; margin-bottom: 4px; }
.img-preview { max-width: 100%; border-radius: 8px; margin-top: 8px; display: none; border: 1px solid #333; }
.upload-status { font-size: 12px; color: #888; margin-top: 4px; height: 16px; }
.toast { position: fixed; bottom: 24px; right: 20px; padding: 10px 18px; border-radius: 8px; font-size: 13px; font-weight: 600; z-index: 200; opacity: 0; transition: opacity .2s; pointer-events: none; }
.toast.show { opacity: 1; }
.hidden { display: none !important; }
.acc-card { background: #1e1e1e; border: 1px solid #2a2a2a; border-radius: 10px; margin-bottom: 8px; overflow: hidden; }
.acc-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; cursor: pointer; user-select: none; }
.acc-title-input { background: transparent; border: none; color: #fff; font-size: 14px; font-weight: 600; flex: 1; outline: none; cursor: pointer; }
.acc-title-input:focus { color: #4ade80; cursor: text; }
.acc-arrow { color: #666; font-size: 11px; margin-left: 8px; }
.acc-body { display: none; padding: 0 16px 14px; }
.acc-body.open { display: block; }
@media (max-width: 600px) {
  .login-box { width: calc(100% - 32px); }
  header { padding: 12px 14px; }
  header h1 { font-size: 13px; }
  .tabs { overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { padding: 12px 14px; font-size: 13px; white-space: nowrap; flex-shrink: 0; }
  .container { padding: 12px; }
  .section-header { flex-wrap: wrap; gap: 8px; }
  .section-header .row { flex-wrap: wrap; }
  .card-header { flex-wrap: wrap; }
  .btn-sm { padding: 8px 14px; font-size: 13px; }
  .overlay { padding: 0; align-items: flex-end; }
  .modal { border-radius: 16px 16px 0 0; max-height: 88vh; overflow-y: auto; margin: 0; }
  input, textarea, select { font-size: 16px; }
}
</style>
</head>
<body>

<div class="login-wrap" id="loginWrap">
  <div class="login-box">
    <h1>🏮 新象園後台</h1>
    <input type="password" id="pwInput" placeholder="管理密碼" />
    <p id="loginErr" style="color:#ef4444;font-size:12px;margin-bottom:10px;min-height:16px;"></p>
    <button class="btn-green btn-full" onclick="doLogin()">登入</button>
  </div>
</div>

<div id="appWrap" class="hidden">
  <header>
    <h1>🏮 新象園 LINE Bot 後台</h1>
    <button class="btn-outline btn-sm" onclick="doLogout()">登出</button>
  </header>
  <div class="tabs">
    <div class="tab active" onclick="switchTab('keywords')">關鍵字回覆</div>
    <div class="tab" onclick="switchTab('info')">餐廳資料</div>
    <div class="tab" onclick="switchTab('humans')">接管管理</div>
    <div class="tab" onclick="switchTab('versions')">版本記錄</div>
  </div>

  <div id="tab-keywords" class="container">
    <div class="section-header">
      <span class="section-title">關鍵字回覆規則</span>
      <div class="row" style="gap:8px">
        <button class="btn-outline btn-sm" onclick="reloadKeywords()">重新整理</button>
        <button class="btn-green btn-sm" onclick="openModal()">+ 新增規則</button>
      </div>
    </div>
    <div id="ruleList"></div>
    <div id="emptyMsg" class="empty hidden">尚無規則，點「新增規則」開始設定</div>
  </div>

  <div id="tab-info" class="container hidden">
    <div class="section-header">
      <span class="section-title">餐廳資料（AI 回覆依據）</span>
      <div class="row" style="gap:8px">
        <button class="btn-outline btn-sm" onclick="reloadInfo()">重新整理</button>
        <button class="btn-outline btn-sm" onclick="addInfoSection()">+ 新增段落</button>
      </div>
    </div>
    <p style="font-size:12px;color:#666;margin-bottom:12px">點段落標題展開編輯，每次儲存保留版本記錄</p>
    <div id="infoSections"></div>
    <button class="btn-green btn-full" style="margin-top:8px" onclick="saveInfo()">儲存餐廳資料</button>
  </div>

  <div id="tab-humans" class="container hidden">
    <div class="section-header">
      <span class="section-title">接管管理</span>
      <button class="btn-outline btn-sm" onclick="loadHumans()">重新整理</button>
    </div>
    <p style="font-size:12px;color:#666;margin-bottom:16px">點「接管」後 AI 停止回覆，由你透過 LINE OA Manager 直接服務顧客。完成後點「解除接管」讓 AI 繼續。</p>
    <div id="humanList"></div>
  </div>

  <div id="tab-versions" class="container hidden">
    <div class="section-header">
      <span class="section-title">版本記錄</span>
      <button class="btn-outline btn-sm" onclick="loadVersions()">重新整理</button>
    </div>
    <p style="font-size:12px;color:#666;margin-bottom:16px">每次儲存都會產生一筆記錄，點「還原」可以回復到該版本。</p>
    <div id="versionList"></div>
  </div>
</div>

<div class="overlay" id="modalOverlay" onclick="closeOverlay(event)">
  <div class="modal">
    <h2 id="modalTitle">新增關鍵字規則</h2>
    <input type="hidden" id="editIdx" value="" />

    <label>觸發關鍵字（逗號分隔）</label>
    <input type="text" id="fKeywords" placeholder="菜單,menu,想看菜單" />

    <label>回覆類型</label>
    <select id="fType" onchange="updateTypeUI()">
      <option value="text">純文字</option>
      <option value="image">純圖片</option>
      <option value="both">文字＋圖片</option>
    </select>

    <div id="textSection">
      <label>回覆文字</label>
      <textarea id="fText" rows="3" placeholder="輸入回覆內容..."></textarea>
    </div>

    <div id="imageSection" class="hidden">
      <label>圖片（上傳或貼入 URL）</label>
      <input type="text" id="fImageUrl" placeholder="https://..." oninput="previewUrl()" />
      <input type="file" id="fImageFile" accept="image/*" onchange="handleUpload(event)" />
      <div class="upload-status" id="uploadStatus"></div>
      <img id="imgPreview" class="img-preview" />
    </div>

    <div class="row" style="margin-top:16px">
      <button class="btn-green" style="flex:1" onclick="saveRule()">儲存規則</button>
      <button class="btn-outline" onclick="closeModal()">取消</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let pw = '';
let config = { restaurant_info: '', keyword_replies: [] };

// ── Auth ──────────────────────────────────────────────────────
function doLogin() {
  const input = document.getElementById('pwInput').value;
  fetch('/admin/config', { headers: { 'X-Admin-Password': input } })
    .then(r => { if (!r.ok) throw new Error('密碼錯誤'); return r.json(); })
    .then(data => {
      pw = input;
      config = data;
      document.getElementById('loginWrap').classList.add('hidden');
      document.getElementById('appWrap').classList.remove('hidden');
      renderInfoSections(config.restaurant_info || '');
      renderRules();
    })
    .catch(e => document.getElementById('loginErr').textContent = e.message);
}
document.getElementById('pwInput').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });

function doLogout() {
  pw = '';
  document.getElementById('appWrap').classList.add('hidden');
  document.getElementById('loginWrap').classList.remove('hidden');
  document.getElementById('pwInput').value = '';
}

// ── Tabs ──────────────────────────────────────────────────────
function switchTab(name) {
  const names = ['keywords','info','humans','versions'];
  document.querySelectorAll('.tab').forEach((el, i) => el.classList.toggle('active', names[i] === name));
  ['keywords','info','humans','versions'].forEach(n =>
    document.getElementById('tab-' + n).classList.toggle('hidden', n !== name)
  );
  if (name === 'humans') loadHumans();
  if (name === 'versions') loadVersions();
}

// ── Humans ────────────────────────────────────────────────────
function loadHumans() {
  fetch('/admin/humans', { headers: { 'X-Admin-Password': pw } })
    .then(r => r.json()).then(renderHumans);
}

function renderHumans(list) {
  const el = document.getElementById('humanList');
  if (!list.length) {
    el.innerHTML = '<div class="empty">過去 24 小時內沒有對話記錄</div>';
    return;
  }
  el.innerHTML = list.map(u => `
    <div class="card">
      <div class="card-header">
        <div style="flex:1">
          <div style="font-size:12px;color:#888;margin-bottom:4px">${fmtAgo(u.timestamp)}</div>
          <div style="font-size:13px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:240px">${u.last_msg}</div>
        </div>
        <div class="row" style="align-items:center">
          ${u.human_mode
            ? `<span style="color:#f59e0b;font-size:12px;margin-right:8px">人工中</span>
               <button class="btn-green btn-sm" onclick="setHuman('${u.user_id}',false)">解除接管</button>`
            : `<button class="btn-red btn-sm" onclick="setHuman('${u.user_id}',true)">接管</button>`
          }
        </div>
      </div>
    </div>
  `).join('');
}

function setHuman(userId, enable) {
  fetch('/admin/humans/' + userId, {
    method: enable ? 'POST' : 'DELETE',
    headers: { 'X-Admin-Password': pw }
  }).then(r => r.json()).then(() => {
    showToast(enable ? '已接管，AI 暫停回覆' : '已解除，AI 恢復回覆');
    loadHumans();
  });
}

function fmtAgo(ts) {
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + ' 秒前';
  if (s < 3600) return Math.floor(s / 60) + ' 分鐘前';
  return Math.floor(s / 3600) + ' 小時前';
}

// ── API ───────────────────────────────────────────────────────
function api(path, method = 'GET', body = null) {
  return fetch('/admin' + path, {
    method,
    headers: { 'X-Admin-Password': pw, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : null,
  }).then(r => r.json());
}

// ── Rules render ──────────────────────────────────────────────
function renderRules() {
  const rules = config.keyword_replies || [];
  document.getElementById('emptyMsg').classList.toggle('hidden', rules.length > 0);
  document.getElementById('ruleList').innerHTML = rules.map((r, i) => `
    <div class="card">
      <div class="card-header">
        <div class="tags">${r.keywords.map(k => `<span class="tag">${k}</span>`).join('')}</div>
        <div class="row">
          <button class="btn-outline btn-sm" onclick="editRule(${i})">編輯</button>
          <button class="btn-red btn-sm" onclick="deleteRule(${i})">刪除</button>
        </div>
      </div>
      <div class="card-preview">
        <span class="type-badge">${{text:'文字',image:'圖片',both:'文字+圖片'}[r.message_type]}</span>
        ${r.text || (r.image_url ? '📷 ' + r.image_url.split('/').pop() : '')}
      </div>
    </div>
  `).join('');
}

// ── Modal ─────────────────────────────────────────────────────
function openModal(rule = null, idx = null) {
  document.getElementById('modalTitle').textContent = rule ? '編輯規則' : '新增規則';
  document.getElementById('editIdx').value = idx !== null ? idx : '';
  document.getElementById('fKeywords').value = rule ? rule.keywords.join(',') : '';
  document.getElementById('fType').value = rule ? rule.message_type : 'text';
  document.getElementById('fText').value = rule?.text || '';
  document.getElementById('fImageUrl').value = rule?.image_url || '';
  const preview = document.getElementById('imgPreview');
  preview.style.display = rule?.image_url ? 'block' : 'none';
  if (rule?.image_url) preview.src = rule.image_url;
  document.getElementById('uploadStatus').textContent = '';
  document.getElementById('fImageFile').value = '';
  updateTypeUI();
  document.getElementById('modalOverlay').classList.add('open');
}

function editRule(i) { openModal(config.keyword_replies[i], i); }

function closeModal() { document.getElementById('modalOverlay').classList.remove('open'); }

function closeOverlay(e) { if (e.target === document.getElementById('modalOverlay')) closeModal(); }

function updateTypeUI() {
  const t = document.getElementById('fType').value;
  document.getElementById('textSection').classList.toggle('hidden', t === 'image');
  document.getElementById('imageSection').classList.toggle('hidden', t === 'text');
}

function previewUrl() {
  const url = document.getElementById('fImageUrl').value.trim();
  const preview = document.getElementById('imgPreview');
  preview.src = url;
  preview.style.display = url ? 'block' : 'none';
}

function handleUpload(e) {
  const file = e.target.files[0];
  if (!file) return;
  const status = document.getElementById('uploadStatus');
  status.textContent = '上傳中...';
  const form = new FormData();
  form.append('file', file);
  fetch('/admin/upload', { method: 'POST', headers: { 'X-Admin-Password': pw }, body: form })
    .then(r => r.json())
    .then(data => {
      if (data.url) {
        document.getElementById('fImageUrl').value = data.url;
        const preview = document.getElementById('imgPreview');
        preview.src = data.url;
        preview.style.display = 'block';
        status.textContent = '✅ 上傳成功';
      } else {
        status.textContent = '❌ 上傳失敗';
      }
    })
    .catch(() => { status.textContent = '❌ 上傳失敗'; });
}

function saveRule() {
  const keywords = document.getElementById('fKeywords').value.split(',').map(k => k.trim()).filter(Boolean);
  if (!keywords.length) { alert('請輸入至少一個關鍵字'); return; }
  const rule = {
    id: Date.now().toString(),
    keywords,
    message_type: document.getElementById('fType').value,
    text: document.getElementById('fText').value.trim(),
    image_url: document.getElementById('fImageUrl').value.trim(),
  };
  const idx = document.getElementById('editIdx').value;
  if (idx !== '') {
    config.keyword_replies[parseInt(idx)] = rule;
  } else {
    config.keyword_replies.push(rule);
  }
  api('/config', 'POST', config).then(r => {
    if (r.ok) { showToast('已儲存'); renderRules(); closeModal(); }
    else showToast('儲存失敗', true);
  });
}

function deleteRule(i) {
  if (!confirm('確定刪除這個規則？')) return;
  config.keyword_replies.splice(i, 1);
  api('/config', 'POST', config).then(r => {
    if (r.ok) { showToast('已刪除'); renderRules(); }
    else showToast('刪除失敗', true);
  });
}

function saveInfo() {
  config.restaurant_info = collectInfoText();
  api('/config', 'POST', config).then(r => showToast(r.ok ? '已儲存' : '儲存失敗', !r.ok));
}

// ── Reload helpers ────────────────────────────────────────────
function reloadKeywords() {
  api('/config').then(data => { config = data; renderRules(); showToast('已重新整理'); });
}
function reloadInfo() {
  api('/config').then(data => { config = data; renderInfoSections(config.restaurant_info || ''); showToast('已重新整理'); });
}

// ── Restaurant Info Accordion ─────────────────────────────────
function renderInfoSections(text) {
  const container = document.getElementById('infoSections');
  const sections = [];
  let preLines = [], current = null;
  for (const line of text.split('\n')) {
    if (line.startsWith('## ')) {
      if (current === null && preLines.length)
        sections.push({ title: '人設與指示', content: preLines.join('\n').trimEnd(), persona: true });
      if (current !== null) sections.push(current);
      current = { title: line.substring(3).trim(), content: '', persona: false };
      preLines = [];
    } else if (current === null) {
      preLines.push(line);
    } else {
      current.content += line + '\n';
    }
  }
  if (current !== null) sections.push(current);
  else if (preLines.length) sections.push({ title: '人設與指示', content: preLines.join('\n').trimEnd(), persona: true });
  sections.forEach(s => { s.content = (s.content || '').trimEnd(); });

  container.innerHTML = sections.map(s => `
    <div class="acc-card" ${s.persona ? 'data-persona="true"' : ''}>
      <div class="acc-header" onclick="toggleSectionCard(this.parentElement)">
        <input class="acc-title-input" value="${escHtml(s.title)}"
               onclick="event.stopPropagation()"
               ${s.persona ? 'readonly style="cursor:default;color:#888"' : ''} />
        <div style="display:flex;gap:8px;align-items:center">
          <span class="acc-arrow">▶</span>
        </div>
      </div>
      <div class="acc-body">
        <textarea rows="6" style="margin-bottom:0">${escHtml(s.content)}</textarea>
      </div>
    </div>
  `).join('');
}

function toggleSectionCard(card) {
  const body = card.querySelector('.acc-body');
  const arr  = card.querySelector('.acc-arrow');
  const open = body.classList.toggle('open');
  arr.textContent = open ? '▼' : '▶';
}

function addInfoSection() {
  const card = document.createElement('div');
  card.className = 'acc-card';
  card.innerHTML = `
    <div class="acc-header" onclick="toggleSectionCard(this.parentElement)">
      <input class="acc-title-input" value="新段落" onclick="event.stopPropagation()" />
      <div style="display:flex;gap:8px;align-items:center">
        <span class="acc-arrow">▼</span>
      </div>
    </div>
    <div class="acc-body open">
      <textarea rows="6" style="margin-bottom:0" placeholder="輸入內容..."></textarea>
    </div>
  `;
  document.getElementById('infoSections').appendChild(card);
  card.querySelector('textarea').focus();
}

function collectInfoText() {
  return Array.from(document.querySelectorAll('#infoSections .acc-card')).map(card => {
    const title   = card.querySelector('.acc-title-input').value.trim();
    const content = card.querySelector('textarea').value.trim();
    return card.dataset.persona === 'true' ? content : `## ${title}\n${content}`;
  }).join('\n\n');
}

function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Versions ──────────────────────────────────────────────────
function loadVersions() {
  document.getElementById('versionList').innerHTML = '<div class="empty">載入中...</div>';
  fetch('/admin/versions', { headers: { 'X-Admin-Password': pw } })
    .then(r => r.json()).then(renderVersions);
}

function renderVersions(list) {
  const el = document.getElementById('versionList');
  if (!list.length) { el.innerHTML = '<div class="empty">沒有版本記錄</div>'; return; }
  el.innerHTML = list.map((v, i) => `
    <div class="card">
      <div class="card-header">
        <div style="flex:1">
          <div style="font-size:13px;color:#ccc">${escHtml(v.message)}</div>
          <div style="font-size:11px;color:#555;margin-top:4px">${fmtDate(v.date)} &nbsp;·&nbsp; ${v.sha.substring(0,7)}</div>
        </div>
        ${i === 0
          ? '<span style="font-size:11px;color:#4ade80;padding:0 8px">目前版本</span>'
          : `<button class="btn-outline btn-sm" onclick="restoreVersion('${v.sha}','${escHtml(v.message)}')">還原</button>`
        }
      </div>
    </div>
  `).join('');
}

function restoreVersion(sha, msg) {
  if (!confirm('確定還原到「' + msg + '」？目前的設定會被覆蓋。')) return;
  fetch('/admin/versions/' + sha + '/restore', {
    method: 'POST', headers: { 'X-Admin-Password': pw }
  }).then(r => r.json()).then(r => {
    if (r.ok) {
      config = r.config;
      renderInfoSections(config.restaurant_info || '');
      renderRules();
      showToast('已還原');
      loadVersions();
    } else {
      showToast('還原失敗', true);
    }
  });
}

function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' });
}

// ── Toast ─────────────────────────────────────────────────────
function showToast(msg, err = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = err ? '#ef4444' : '#4ade80';
  t.style.color = err ? '#fff' : '#000';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}
</script>
</body>
</html>"""
