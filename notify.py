"""
notify.py - Báo qua Telegram khi video xử lý xong / lỗi, khi đăng bài xong / lỗi, khi kênh theo dõi có video mới.

Cài: tạo bot với @BotFather (lệnh /newbot) -> lấy token, nhắn một tin bất kỳ cho bot, rồi trên web
(Cài đặt -> Báo qua Telegram) dán token và bấm "Kết nối": tool tự tìm cuộc trò chuyện của bạn.
"""
import json
import threading
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
API = "https://api.telegram.org/bot{}/{}"
EVENTS = ("job_done", "job_error", "post_done", "post_error", "watch_new")


def _cfg():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("telegram") or {}
    except Exception:
        return {}


def _call(token, method, **params):
    r = requests.post(API.format(token, method), json=params, timeout=20)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"HTTP {r.status_code}")
    return data["result"]


def find_chat(token):
    """Lấy cuộc trò chuyện gần nhất có người nhắn cho bot. Trả về (chat_id, tên)."""
    me = _call(token, "getMe")
    ups = _call(token, "getUpdates", limit=50)
    for u in reversed(ups):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
            return chat["id"], name or str(chat["id"]), me.get("username", "")
    raise RuntimeError(f"Chưa thấy tin nhắn nào. Mở Telegram, nhắn một tin bất kỳ cho @{me.get('username')} "
                       "rồi bấm Kết nối lại.")


def send_now(text, cfg=None):
    cfg = cfg or _cfg()
    if not (cfg.get("token") and cfg.get("chat_id")):
        raise RuntimeError("Chưa kết nối Telegram.")
    _call(cfg["token"], "sendMessage", chat_id=cfg["chat_id"], text=text[:4000], disable_web_page_preview=True)


def send(event, text):
    """Gửi ở luồng riêng, lỗi mạng không làm ảnh hưởng việc khác."""
    cfg = _cfg()
    if not (cfg.get("enabled", True) and cfg.get("token") and cfg.get("chat_id")):
        return
    if event in EVENTS and not (cfg.get("events") or {}).get(event, True):
        return

    def go():
        try:
            send_now(text, cfg)
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()
