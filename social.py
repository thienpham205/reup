#!/usr/bin/env python3
"""
social.py - Liên kết và quản lý tài khoản Facebook / YouTube / TikTok bằng OAuth chính thức.

Mỗi nền tảng cần một "ứng dụng" do chính bạn tạo trên trang dành cho nhà phát triển (miễn phí):
bạn dán Client ID + Client Secret vào web, bấm "Liên kết", đăng nhập trên trang của nền tảng và cho phép.
Tool KHÔNG bao giờ thấy mật khẩu của bạn; chỉ lưu mã truy cập (token) trong accounts.json cạnh tool.
"""
import base64
import hashlib
import html
import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Blueprint, abort, jsonify, redirect, request

ROOT = Path(__file__).resolve().parent
STORE_PATH = ROOT / "accounts.json"
DEFAULT_BASE = "http://localhost:5577"
TIMEOUT = 25

bp = Blueprint("social", __name__)
_lock = threading.RLock()
_pending = {}  # state -> {platform, verifier, created}

PLATFORMS = {
    "youtube": {
        "name": "YouTube",
        "id_label": "Client ID", "secret_label": "Client Secret",
        "console": "https://console.cloud.google.com/apis/credentials",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"],
    },
    "facebook": {
        "name": "Facebook",
        "id_label": "App ID", "secret_label": "App Secret",
        "console": "https://developers.facebook.com/apps/",
        "auth_url": "https://www.facebook.com/dialog/oauth",
        "token_url": "https://graph.facebook.com/oauth/access_token",
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    },
    "tiktok": {
        "name": "TikTok",
        "id_label": "Client Key", "secret_label": "Client Secret",
        "console": "https://developers.tiktok.com/apps/",
        "auth_url": "https://www.tiktok.com/v2/auth/authorize/",
        "token_url": "https://open.tiktokapis.com/v2/oauth/token/",
        "scopes": ["user.info.basic", "video.upload", "video.publish"],
    },
}


class SocialError(Exception):
    pass


# ---------------------------------------------------------------- lưu trữ
def _load():
    try:
        d = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("apps", {})
    d.setdefault("accounts", [])
    return d


def _save(d):
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _hint(s):
    return (s[:4] + "••••" + s[-4:]) if len(s) > 10 else ("••••" if s else "")


def _base(d):
    return (d.get("redirect_base") or DEFAULT_BASE).rstrip("/")


def redirect_uri(d, platform):
    return f"{_base(d)}/oauth/{platform}/callback"


def _public_account(a):
    keys = ("id", "platform", "kind", "name", "handle", "avatar", "url", "stats", "linked_at", "checked_at", "status", "error")
    out = {k: a.get(k) for k in keys}
    exp = a.get("expires_at")
    out["expires_at"] = exp
    out["can_refresh"] = bool(a.get("refresh_token"))
    return out


def public_state():
    d = _load()
    plats = {}
    for key, p in PLATFORMS.items():
        app = d["apps"].get(key) or {}
        plats[key] = {
            "name": p["name"], "id_label": p["id_label"], "secret_label": p["secret_label"], "console": p["console"],
            "configured": bool(app.get("client_id") and app.get("client_secret")),
            "id_hint": _hint(app.get("client_id", "")),
            "redirect_uri": redirect_uri(d, key),
            "scopes": p["scopes"],
        }
    return {"redirect_base": _base(d), "platforms": plats, "accounts": [_public_account(a) for a in d["accounts"]]}


# ---------------------------------------------------------------- gọi API nền tảng
def _json(r):
    try:
        data = r.json()
    except Exception:
        raise SocialError(f"HTTP {r.status_code}: {r.text[:200]}")
    err = data.get("error")
    if r.status_code >= 400 or (err and not (isinstance(err, dict) and err.get("code") in ("ok", 0))):
        if isinstance(err, dict):
            msg = err.get("message") or err.get("error_description") or json.dumps(err, ensure_ascii=False)
        else:
            msg = data.get("error_description") or err or r.text[:200]
        raise SocialError(f"{msg} (HTTP {r.status_code})")
    return data


def _exchange(platform, app, d, code, verifier):
    p = PLATFORMS[platform]
    uri = redirect_uri(d, platform)
    if platform == "youtube":
        return _json(requests.post(p["token_url"], timeout=TIMEOUT, data={
            "client_id": app["client_id"], "client_secret": app["client_secret"], "code": code,
            "grant_type": "authorization_code", "redirect_uri": uri, "code_verifier": verifier}))
    if platform == "facebook":
        tok = _json(requests.get(p["token_url"], timeout=TIMEOUT, params={
            "client_id": app["client_id"], "client_secret": app["client_secret"], "code": code, "redirect_uri": uri}))
        try:  # đổi sang token dài hạn (~60 ngày); token Trang lấy từ token này không hết hạn
            tok = _json(requests.get(p["token_url"], timeout=TIMEOUT, params={
                "grant_type": "fb_exchange_token", "client_id": app["client_id"],
                "client_secret": app["client_secret"], "fb_exchange_token": tok["access_token"]}))
        except SocialError:
            pass
        return tok
    return _json(requests.post(p["token_url"], timeout=TIMEOUT, data={
        "client_key": app["client_id"], "client_secret": app["client_secret"], "code": code,
        "grant_type": "authorization_code", "redirect_uri": uri, "code_verifier": verifier}))


def _refresh(acc, app):
    """Làm mới access token (YouTube, TikTok). Facebook: token Trang không cần làm mới."""
    plat = acc["platform"]
    if plat == "facebook" or not acc.get("refresh_token"):
        return
    if plat == "youtube":
        tok = _json(requests.post(PLATFORMS[plat]["token_url"], timeout=TIMEOUT, data={
            "client_id": app["client_id"], "client_secret": app["client_secret"],
            "refresh_token": acc["refresh_token"], "grant_type": "refresh_token"}))
    else:
        tok = _json(requests.post(PLATFORMS[plat]["token_url"], timeout=TIMEOUT, data={
            "client_key": app["client_id"], "client_secret": app["client_secret"],
            "refresh_token": acc["refresh_token"], "grant_type": "refresh_token"}))
        if tok.get("refresh_token"):
            acc["refresh_token"] = tok["refresh_token"]
    acc["access_token"] = tok["access_token"]
    acc["expires_at"] = time.time() + int(tok.get("expires_in") or 3600)


def _profiles(platform, tok):
    """Từ token vừa nhận -> danh sách tài khoản để lưu (Facebook: mỗi Trang là một tài khoản)."""
    now = time.time()
    access = tok["access_token"]
    exp = now + int(tok["expires_in"]) if tok.get("expires_in") else None
    if platform == "youtube":
        data = _json(requests.get("https://www.googleapis.com/youtube/v3/channels", timeout=TIMEOUT,
                                  params={"part": "snippet,statistics", "mine": "true"},
                                  headers={"Authorization": f"Bearer {access}"}))
        items = data.get("items") or []
        if not items:
            raise SocialError("Tài khoản Google này chưa có kênh YouTube nào.")
        out = []
        for ch in items:
            sn, st = ch.get("snippet", {}), ch.get("statistics", {})
            out.append({"platform": "youtube", "kind": "Kênh", "external_id": ch["id"], "name": sn.get("title", "Kênh YouTube"),
                        "handle": sn.get("customUrl", ""), "avatar": (sn.get("thumbnails", {}).get("default") or {}).get("url", ""),
                        "url": f"https://www.youtube.com/channel/{ch['id']}",
                        "stats": {"Người đăng ký": st.get("subscriberCount"), "Video": st.get("videoCount")},
                        "access_token": access, "refresh_token": tok.get("refresh_token", ""), "expires_at": exp})
        return out
    if platform == "facebook":
        data = _json(requests.get("https://graph.facebook.com/me/accounts", timeout=TIMEOUT, params={
            "fields": "id,name,username,access_token,fan_count,picture{url}", "limit": 100, "access_token": access}))
        pages = data.get("data") or []
        if not pages:
            raise SocialError("Không thấy Trang (Page) nào. Facebook chỉ cho ứng dụng đăng lên Trang, không đăng lên trang cá nhân. "
                              "Hãy tạo một Trang và tick chọn Trang đó ở bước cho phép.")
        return [{"platform": "facebook", "kind": "Trang", "external_id": pg["id"], "name": pg.get("name", "Trang Facebook"),
                 "handle": pg.get("username", ""), "avatar": ((pg.get("picture") or {}).get("data") or {}).get("url", ""),
                 "url": f"https://www.facebook.com/{pg['id']}", "stats": {"Người thích": pg.get("fan_count")},
                 "access_token": pg["access_token"], "refresh_token": "", "expires_at": None} for pg in pages]
    data = _json(requests.get("https://open.tiktokapis.com/v2/user/info/", timeout=TIMEOUT,
                              params={"fields": "open_id,display_name,avatar_url"},
                              headers={"Authorization": f"Bearer {access}"}))
    u = (data.get("data") or {}).get("user") or {}
    return [{"platform": "tiktok", "kind": "Tài khoản", "external_id": u.get("open_id") or tok.get("open_id", ""),
             "name": u.get("display_name") or "Tài khoản TikTok", "handle": "", "avatar": u.get("avatar_url", ""),
             "url": "", "stats": {}, "access_token": access, "refresh_token": tok.get("refresh_token", ""), "expires_at": exp}]


def _check(acc, app):
    """Kiểm tra token còn dùng được không và cập nhật tên / ảnh / số liệu."""
    if acc.get("expires_at") and acc["expires_at"] < time.time() + 120:
        _refresh(acc, app)
    plat, access = acc["platform"], acc["access_token"]
    if plat == "youtube":
        data = _json(requests.get("https://www.googleapis.com/youtube/v3/channels", timeout=TIMEOUT,
                                  params={"part": "snippet,statistics", "id": acc["external_id"]},
                                  headers={"Authorization": f"Bearer {access}"}))
        items = data.get("items") or []
        if not items:
            raise SocialError("Không còn thấy kênh này.")
        sn, st = items[0].get("snippet", {}), items[0].get("statistics", {})
        acc.update(name=sn.get("title", acc["name"]), handle=sn.get("customUrl", ""),
                   avatar=(sn.get("thumbnails", {}).get("default") or {}).get("url", acc.get("avatar", "")),
                   stats={"Người đăng ký": st.get("subscriberCount"), "Video": st.get("videoCount")})
    elif plat == "facebook":
        pg = _json(requests.get(f"https://graph.facebook.com/{acc['external_id']}", timeout=TIMEOUT, params={
            "fields": "id,name,username,fan_count,picture{url}", "access_token": access}))
        acc.update(name=pg.get("name", acc["name"]), handle=pg.get("username", ""),
                   avatar=((pg.get("picture") or {}).get("data") or {}).get("url", acc.get("avatar", "")),
                   stats={"Người thích": pg.get("fan_count")})
    else:
        data = _json(requests.get("https://open.tiktokapis.com/v2/user/info/", timeout=TIMEOUT,
                                  params={"fields": "open_id,display_name,avatar_url"},
                                  headers={"Authorization": f"Bearer {access}"}))
        u = (data.get("data") or {}).get("user") or {}
        acc.update(name=u.get("display_name") or acc["name"], avatar=u.get("avatar_url") or acc.get("avatar", ""))


# ---------------------------------------------------------------- API cho giao diện
@bp.get("/api/social")
def api_state():
    return jsonify(public_state())


@bp.post("/api/social/app/<platform>")
def api_set_app(platform):
    if platform not in PLATFORMS:
        abort(404)
    body = request.get_json(force=True) or {}
    with _lock:
        d = _load()
        if body.get("clear"):
            d["apps"].pop(platform, None)
        else:
            cid, sec = str(body.get("client_id", "")).strip(), str(body.get("client_secret", "")).strip()
            old = d["apps"].get(platform) or {}
            cid, sec = cid or old.get("client_id", ""), sec or old.get("client_secret", "")
            if not cid or not sec:
                return jsonify(error=f"Cần nhập đủ {PLATFORMS[platform]['id_label']} và {PLATFORMS[platform]['secret_label']}."), 400
            d["apps"][platform] = {"client_id": cid, "client_secret": sec}
        _save(d)
    return jsonify(public_state())


@bp.post("/api/social/redirect-base")
def api_set_base():
    base = str((request.get_json(force=True) or {}).get("redirect_base", "")).strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        return jsonify(error="Địa chỉ phải bắt đầu bằng http:// hoặc https://"), 400
    with _lock:
        d = _load()
        if base and base != DEFAULT_BASE:
            d["redirect_base"] = base
        else:
            d.pop("redirect_base", None)
        _save(d)
    return jsonify(public_state())


@bp.post("/api/social/accounts/<aid>/check")
def api_check(aid):
    with _lock:
        d = _load()
        acc = next((a for a in d["accounts"] if a["id"] == aid), None) or abort(404)
        app = d["apps"].get(acc["platform"]) or {}
        try:
            _check(acc, app)
            acc.update(status="ok", error="")
        except (SocialError, requests.RequestException, KeyError) as e:
            acc.update(status="error", error=str(e)[:300])
        acc["checked_at"] = time.time()
        _save(d)
        return jsonify(_public_account(acc))


@bp.delete("/api/social/accounts/<aid>")
def api_unlink(aid):
    with _lock:
        d = _load()
        acc = next((a for a in d["accounts"] if a["id"] == aid), None) or abort(404)
        d["accounts"] = [a for a in d["accounts"] if a["id"] != aid]
        _save(d)
    if acc["platform"] == "youtube":  # thu hồi quyền phía Google (không được cũng không sao)
        try:
            requests.post("https://oauth2.googleapis.com/revoke", timeout=10,
                          data={"token": acc.get("refresh_token") or acc.get("access_token", "")})
        except requests.RequestException:
            pass
    return jsonify(ok=True)


# ---------------------------------------------------------------- luồng OAuth
def _page(title, body, ok):
    color = "#1f8a4c" if ok else "#c0322a"
    return f"""<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<body style="font:16px/1.6 'Segoe UI',system-ui,sans-serif;background:#0f1115;color:#e8eaed;display:grid;place-items:center;min-height:100vh;margin:0">
<div style="max-width:520px;padding:32px;text-align:center">
<div style="font-size:44px;color:{color}">{"✓" if ok else "✕"}</div>
<h1 style="font-size:22px;margin:8px 0">{html.escape(title)}</h1>
<p style="color:#a5acb8;word-break:break-word">{body}</p>
<p><a href="/#accounts" style="color:#ff6a3d;font-weight:600">← Quay lại trang Tài khoản</a></p>
</div>
<script>try{{ if (window.opener) {{ window.opener.postMessage({{social:"{'ok' if ok else 'error'}"}}, location.origin); {"setTimeout(()=>window.close(),1200);" if ok else ""} }} }}catch(e){{}}</script>
</body></html>"""


@bp.get("/oauth/<platform>/start")
def oauth_start(platform):
    if platform not in PLATFORMS:
        abort(404)
    d = _load()
    app = d["apps"].get(platform) or {}
    if not (app.get("client_id") and app.get("client_secret")):
        return _page("Chưa cấu hình ứng dụng", f"Hãy nhập {PLATFORMS[platform]['id_label']} và "
                     f"{PLATFORMS[platform]['secret_label']} của {PLATFORMS[platform]['name']} trước.", False), 400
    p = PLATFORMS[platform]
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    with _lock:
        for k in [k for k, v in _pending.items() if time.time() - v["created"] > 900]:
            _pending.pop(k, None)
        _pending[state] = {"platform": platform, "verifier": verifier, "created": time.time()}
    digest = hashlib.sha256(verifier.encode()).digest()
    params = {"response_type": "code", "redirect_uri": redirect_uri(d, platform), "state": state}
    if platform == "youtube":
        params.update(client_id=app["client_id"], scope=" ".join(p["scopes"]), access_type="offline", prompt="consent",
                      include_granted_scopes="true", code_challenge_method="S256",
                      code_challenge=base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
    elif platform == "facebook":
        params.update(client_id=app["client_id"], scope=",".join(p["scopes"]))
    else:  # TikTok dùng client_key và code_challenge dạng hex
        params.update(client_key=app["client_id"], scope=",".join(p["scopes"]),
                      code_challenge_method="S256", code_challenge=digest.hex())
    return redirect(f"{p['auth_url']}?{urlencode(params)}")


@bp.get("/oauth/<platform>/callback")
def oauth_callback(platform):
    if platform not in PLATFORMS:
        abort(404)
    name = PLATFORMS[platform]["name"]
    q = request.args
    with _lock:
        pend = _pending.pop(q.get("state", ""), None)
    if q.get("error"):
        why = q.get("error_description") or q.get("error_reason") or q.get("error")
        return _page(f"Chưa liên kết được {name}", html.escape(str(why)[:300]), False), 400
    if not pend or pend["platform"] != platform or not q.get("code"):
        return _page(f"Chưa liên kết được {name}", "Phiên liên kết đã hết hạn hoặc không hợp lệ. Hãy bấm Liên kết lại.", False), 400
    try:
        with _lock:
            d = _load()
            app = d["apps"].get(platform) or {}
            tok = _exchange(platform, app, d, q["code"], pend["verifier"])
            found = _profiles(platform, tok)
            now = time.time()
            for acc in found:
                old = next((a for a in d["accounts"] if a["platform"] == platform and a.get("external_id") == acc["external_id"]), None)
                if old:  # liên kết lại: giữ id cũ, giữ refresh token cũ nếu lần này không được cấp
                    acc["refresh_token"] = acc["refresh_token"] or old.get("refresh_token", "")
                    old.update(acc, status="ok", error="", checked_at=now)
                else:
                    d["accounts"].append({"id": secrets.token_hex(6), **acc, "linked_at": now, "checked_at": now,
                                          "status": "ok", "error": ""})
            _save(d)
    except (SocialError, requests.RequestException, KeyError) as e:
        return _page(f"Chưa liên kết được {name}", html.escape(str(e)[:400]), False), 400
    names = ", ".join(html.escape(a["name"]) for a in found)
    return _page(f"Đã liên kết {name}", f"{names}<br>Bạn có thể đóng tab này.", True)


# ---------------------------------------------------------------- đăng video
PRIVACY = ("private", "public")


class _Progress:
    """Bọc file đang tải lên để báo tiến độ (requests đọc dần qua read())."""

    def __init__(self, path, cb, start=0, length=None):
        self.f = open(path, "rb")
        self.f.seek(start)
        self.total = Path(path).stat().st_size
        self.left = self.total - start if length is None else length
        self.len = self.left  # requests dùng thuộc tính này làm Content-Length
        self.sent, self.cb = start, cb

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        chunk = self.f.read(self.left if n is None or n < 0 else min(n, self.left))
        self.left -= len(chunk)
        self.len = self.left
        self.sent += len(chunk)
        if self.cb:
            self.cb(int(self.sent * 100 / max(self.total, 1)))
        return chunk

    def close(self):
        self.f.close()


def _fresh_account(aid):
    """Lấy tài khoản kèm token còn hạn (tự làm mới nếu sắp hết hạn)."""
    with _lock:
        d = _load()
        acc = next((a for a in d["accounts"] if a["id"] == aid), None)
        if not acc:
            raise SocialError("Tài khoản này đã bị gỡ liên kết.")
        if acc.get("expires_at") and acc["expires_at"] < time.time() + 300:
            _refresh(acc, d["apps"].get(acc["platform"]) or {})
            _save(d)
        return dict(acc)


def account_brief(aid):
    acc = next((a for a in _load()["accounts"] if a["id"] == aid), None)
    return {"id": acc["id"], "platform": acc["platform"], "name": acc["name"]} if acc else None


def _hashline(tags):
    return " ".join("#" + t for t in tags)


def _yt_upload(acc, video, title, desc, tags, cover, privacy, cb):
    auth = {"Authorization": f"Bearer {acc['access_token']}"}
    size = Path(video).stat().st_size
    body = {"snippet": {"title": (title.replace("<", "").replace(">", "") or "Video")[:100],
                        "description": "\n\n".join(x for x in (desc, _hashline(tags)) if x)[:4900],
                        "tags": tags[:30], "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos", timeout=TIMEOUT,
                      params={"uploadType": "resumable", "part": "snippet,status"}, json=body,
                      headers={**auth, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    if r.status_code >= 400:
        _json(r)
    up = _Progress(video, cb)
    try:
        r = requests.put(r.headers["Location"], data=up, timeout=3600, headers={**auth, "Content-Type": "video/mp4"})
    finally:
        up.close()
    vid = _json(r)["id"]
    out = {"url": f"https://youtu.be/{vid}", "note": ""}
    if cover and Path(cover).is_file():  # kênh chưa xác minh số điện thoại sẽ bị từ chối ảnh bìa, không coi là lỗi
        try:
            _json(requests.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set", timeout=120,
                                params={"videoId": vid, "uploadType": "media"}, data=Path(cover).read_bytes(),
                                headers={**auth, "Content-Type": "image/jpeg"}))
        except (SocialError, requests.RequestException) as e:
            out["note"] = f"Video đã lên nhưng chưa đặt được ảnh bìa: {str(e)[:150]}"
    return out


def _fb_upload(acc, video, title, desc, tags, cover, privacy, cb):
    text = "\n\n".join(x for x in (title, desc, _hashline(tags)) if x)
    files = {"source": (Path(video).name, open(video, "rb"), "video/mp4")}
    if cover and Path(cover).is_file():
        files["thumb"] = ("cover.jpg", open(cover, "rb"), "image/jpeg")
    cb(1)  # tải một lần cả file nên không có tiến độ chi tiết
    try:
        data = _json(requests.post(f"https://graph-video.facebook.com/{acc['external_id']}/videos", timeout=3600,
                                   files=files, data={"access_token": acc["access_token"], "title": title[:250],
                                                      "description": text[:5000],
                                                      "published": "true" if privacy == "public" else "false"}))
    finally:
        for f in files.values():
            f[1].close()
    return {"url": f"https://www.facebook.com/{acc['external_id']}/videos/{data['id']}",
            "note": "" if privacy == "public" else "Video ở dạng chưa công bố, vào Trang để công bố."}


def _tt_upload(acc, video, title, desc, tags, cover_ms, privacy, cb):
    api = "https://open.tiktokapis.com/v2/post/publish"
    auth = {"Authorization": f"Bearer {acc['access_token']}", "Content-Type": "application/json; charset=UTF-8"}
    info = (_json(requests.post(f"{api}/creator_info/query/", timeout=TIMEOUT, headers=auth)).get("data")) or {}
    options = info.get("privacy_level_options") or ["SELF_ONLY"]
    level, note = "SELF_ONLY", ""
    if privacy == "public":
        if "PUBLIC_TO_EVERYONE" in options:
            level = "PUBLIC_TO_EVERYONE"
        else:
            note = "TikTok chỉ cho ứng dụng này đăng ở chế độ riêng tư (ứng dụng chưa được duyệt)."
    size = Path(video).stat().st_size
    mb = 1024 * 1024
    chunk = size if size <= 64 * mb else 10 * mb
    count = max(1, size // chunk)  # phần dư gộp vào khúc cuối
    post_info = {"title": " ".join(x for x in (title, _hashline(tags)) if x)[:2200], "privacy_level": level,
                 "disable_duet": False, "disable_comment": False, "disable_stitch": False}
    if cover_ms:
        post_info["video_cover_timestamp_ms"] = int(cover_ms)
    init = _json(requests.post(f"{api}/video/init/", timeout=TIMEOUT, headers=auth, json={
        "post_info": post_info,
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                        "total_chunk_count": count}}))["data"]
    for i in range(count):
        start = i * chunk
        length = (size - start) if i == count - 1 else chunk
        up = _Progress(video, cb, start, length)
        try:
            r = requests.put(init["upload_url"], data=up, timeout=3600, headers={
                "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{start + length - 1}/{size}"})
        finally:
            up.close()
        if r.status_code >= 400:
            raise SocialError(f"TikTok từ chối file tải lên (HTTP {r.status_code}): {r.text[:200]}")
    for _ in range(24):  # chờ TikTok xử lý, tối đa khoảng 2 phút
        time.sleep(5)
        st = (_json(requests.post(f"{api}/status/fetch/", timeout=TIMEOUT, headers=auth,
                                  json={"publish_id": init["publish_id"]})).get("data")) or {}
        if st.get("status") == "FAILED":
            raise SocialError(f"TikTok báo lỗi: {st.get('fail_reason') or 'không rõ'}")
        if st.get("status") == "PUBLISH_COMPLETE":
            return {"url": "", "note": note or "Đã đăng. Mở ứng dụng TikTok để xem video."}
    return {"url": "", "note": (note + " " if note else "")
            + "Đã tải lên, TikTok vẫn đang xử lý. Kiểm tra lại trong ứng dụng TikTok."}


def publish(aid, video, title, desc, tags, cover=None, cover_ms=None, privacy="private", progress=None):
    """Đăng một video lên một tài khoản đã liên kết. Trả về {url, note}; lỗi -> SocialError."""
    cb = progress or (lambda p: None)
    try:
        acc = _fresh_account(aid)
        if acc["platform"] == "youtube":
            return _yt_upload(acc, video, title, desc, tags, cover, privacy, cb)
        if acc["platform"] == "facebook":
            return _fb_upload(acc, video, title, desc, tags, cover, privacy, cb)
        return _tt_upload(acc, video, title, desc, tags, cover_ms, privacy, cb)
    except (requests.RequestException, KeyError) as e:
        raise SocialError(f"{type(e).__name__}: {str(e)[:250]}")
