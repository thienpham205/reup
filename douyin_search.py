"""
douyin_search.py - Tìm video theo từ khoá (Douyin, YouTube) và lấy bảng xu hướng Douyin.

Douyin chỉ cho tìm kiếm khi đã đăng nhập: bấm "Đăng nhập Douyin" trên web, một cửa sổ trình duyệt của tool mở ra,
bạn quét mã QR bằng app Douyin rồi đóng cửa sổ. Phiên đăng nhập lưu trong hồ sơ trình duyệt của tool
(.browser-profile), lần sau tìm bằng trình duyệt ẩn. Tool không thấy và không lưu mật khẩu.

    python douyin_search.py 蜡笔小新
"""
import json
import subprocess
import sys
import threading
import time
import urllib.parse

import douyin_dub

profile_lock = threading.Lock()  # hồ sơ trình duyệt chỉ mở được ở một nơi tại một thời điểm
HOME = "https://www.douyin.com/video/7704296556186569162"  # trang video mở được không cần xác minh (để gọi API)
FETCH_JS = """async (u) => new Promise((res) => {
  const x = new XMLHttpRequest(); x.open('GET', u); x.timeout = 20000;
  x.onload = () => res(x.responseText); x.onerror = () => res(''); x.ontimeout = () => res(''); x.send();
})"""
SEARCH_API = ("/aweme/v1/web/search/item/?device_platform=webapp&aid=6383&channel=channel_pc_web"
              "&search_channel=aweme_video_web&keyword={q}&sort_type={sort}&publish_time={days}&filter_duration={dur}"
              "&search_source=normal_search&query_correct_type=1&is_filter_search={filt}&offset={off}&count=20")
HOT_API = "/aweme/v1/web/hot/search/list/?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1"
DURATIONS = {"": "", "short": "0-1", "mid": "1-5", "long": "5-10000"}  # phút


class NeedLogin(Exception):
    pass


class Busy(Exception):
    pass


def _open(p, headless=True):
    opts = dict(user_agent=douyin_dub.DESKTOP_UA, locale="zh-CN", viewport={"width": 1280, "height": 800})
    for _ in range(12):  # đang tải video (tiến trình khác giữ hồ sơ) -> đợi
        try:
            return p.chromium.launch_persistent_context(str(douyin_dub.BROWSER_PROFILE), headless=headless, **opts)
        except Exception as e:
            last = e
            time.sleep(5)
    raise Busy("Trình duyệt của tool đang bận tải video, hãy thử lại sau ít phút.") from last


def _with_page(fn):
    from playwright.sync_api import sync_playwright

    with profile_lock, sync_playwright() as p:
        ctx = _open(p)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if "验证" in (page.title() or ""):
                raise NeedLogin("Douyin đang yêu cầu xác minh. Bấm \"Đăng nhập Douyin\" để mở trình duyệt và xác minh.")
            return fn(page)
        finally:
            ctx.close()


def _call(page, path):
    text = page.evaluate(FETCH_JS, path) or "{}"
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _item(a):
    v = a.get("video") or {}
    st = a.get("statistics") or {}
    cover = ((v.get("cover") or v.get("origin_cover") or {}).get("url_list") or [""])[0]
    dur = v.get("duration") or a.get("duration") or 0
    return {"id": a["aweme_id"], "url": f"https://www.douyin.com/video/{a['aweme_id']}", "title": (a.get("desc") or "")[:200],
            "likes": st.get("digg_count") or 0, "comments": st.get("comment_count") or 0, "shares": st.get("share_count") or 0,
            "duration": round(dur / 1000 if dur > 1000 else dur), "cover": cover,
            "author": (a.get("author") or {}).get("nickname") or "", "time": a.get("create_time") or 0}


def search_douyin(q, sort=0, days=0, dur="", pages=2):
    """sort: 0 tổng hợp, 1 nhiều thích nhất, 2 mới nhất; days: 0 / 1 / 7 / 180; dur: ''/short/mid/long."""
    def go(page):
        out, seen = [], set()
        for k in range(pages):
            path = SEARCH_API.format(q=urllib.parse.quote(q), sort=int(sort), days=int(days), dur=DURATIONS.get(dur, ""),
                                     filt=1 if (sort or days or dur) else 0, off=k * 20)
            j = _call(page, path)
            if j.get("status_code") == 2483 or "登录" in (j.get("status_msg") or ""):
                raise NeedLogin("Douyin chỉ cho tìm kiếm khi đã đăng nhập. Bấm \"Đăng nhập Douyin\" để đăng nhập một lần.")
            data = j.get("data") or []
            for d in data:
                a = d.get("aweme_info") or {}
                if a.get("aweme_id") and not a.get("images") and a["aweme_id"] not in seen:
                    seen.add(a["aweme_id"])
                    out.append(_item(a))
            if not j.get("has_more") or not data:
                break
            page.wait_for_timeout(1200)
        if not out and not seen:
            raise RuntimeError("Không tìm thấy video nào (hoặc Douyin chặn tạm). Thử từ khoá khác hoặc thử lại sau.")
        return out

    return _with_page(go)


def hot_list():
    """Bảng xu hướng Douyin (không cần đăng nhập): [{word, hot}]"""
    def go(page):
        d = (_call(page, HOT_API).get("data") or {})
        return [{"word": w.get("word"), "hot": w.get("hot_value") or 0} for w in d.get("word_list") or [] if w.get("word")]

    return _with_page(go)


def search_youtube(q, n=20):
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "-J", f"ytsearch{n}:{q}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode:
        raise RuntimeError((r.stderr.strip().splitlines() or ["yt-dlp lỗi"])[-1][:300])
    out = []
    for e in json.loads(r.stdout).get("entries") or []:
        if not e.get("id"):
            continue
        thumbs = e.get("thumbnails") or []
        out.append({"id": e["id"], "url": f"https://www.youtube.com/watch?v={e['id']}", "title": (e.get("title") or "")[:200],
                    "likes": e.get("view_count") or 0, "views": True, "duration": int(e.get("duration") or 0),
                    "cover": thumbs[-1]["url"] if thumbs else "", "author": e.get("channel") or e.get("uploader") or "",
                    "time": e.get("timestamp") or 0})
    return out


def search_bilibili(q, sort=0, dur="", pages=2):
    """API tìm kiếm công khai của Bilibili (không cần đăng nhập, chỉ cần cookie khách từ trang chủ).
    Bilibili đếm lượt xem (play) làm số chính, như YouTube."""
    import html
    import re

    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": douyin_dub.DESKTOP_UA, "Referer": "https://www.bilibili.com/"})
    s.get("https://www.bilibili.com/", timeout=20)
    order = {0: "totalrank", 1: "click", 2: "pubdate"}.get(int(sort), "totalrank")
    out = []
    for page in range(1, pages + 1):
        j = s.get("https://api.bilibili.com/x/web-interface/search/type", timeout=20, params={
            "search_type": "video", "keyword": q, "page": page, "order": order,
            "duration": 1 if dur in ("short", "mid") else 0}).json()
        if j.get("code"):
            raise RuntimeError(f"Bilibili từ chối tìm kiếm ({j.get('message')}). Thử lại sau ít phút.")
        res = (j.get("data") or {}).get("result") or []
        for v in res:
            if not v.get("bvid"):
                continue
            m, _, sec = str(v.get("duration") or "0:0").rpartition(":")
            h, _, m = m.rpartition(":")
            dsec = int(h or 0) * 3600 + int(m or 0) * 60 + int(sec or 0)
            pic = v.get("pic") or ""
            out.append({"id": v["bvid"], "url": f"https://www.bilibili.com/video/{v['bvid']}",
                        "title": html.unescape(re.sub(r"<.*?>", "", v.get("title") or ""))[:200],
                        "likes": int(v.get("play") or 0), "views": True, "duration": dsec,
                        "cover": ("https:" + pic) if pic.startswith("//") else pic,
                        "author": v.get("author") or "", "time": v.get("pubdate") or 0})
        if len(res) < 20:
            break
    return out


def search(platform, q, sort=0, days=0, dur="", min_likes=0, max_items=40):
    if platform == "youtube":
        items = search_youtube(q)
    elif platform == "bilibili":
        items = search_bilibili(q, sort, dur)
    else:
        items = search_douyin(q, sort, days, dur)
    if platform != "douyin":
        if dur:
            lo, hi = {"short": (0, 60), "mid": (60, 300), "long": (300, 10 ** 6)}[dur]
            items = [i for i in items if lo <= i["duration"] <= hi]
        if days:
            items = [i for i in items if not i["time"] or i["time"] >= time.time() - days * 86400]
    if min_likes:
        items = [i for i in items if i["likes"] >= min_likes]
    return items[:max_items]


login_state = {"open": False, "logged_in": None, "msg": ""}


def login_window(timeout=600):
    """Mở cửa sổ trình duyệt (hiện ra) của tool ở douyin.com để NGƯỜI DÙNG tự đăng nhập / xác minh; đóng cửa sổ là xong."""
    from playwright.sync_api import sync_playwright

    login_state.update(open=True, msg="Đang mở cửa sổ Douyin…")
    try:
        with profile_lock, sync_playwright() as p:
            ctx = _open(p, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            login_state["msg"] = "Hãy đăng nhập (quét mã QR bằng app Douyin) trong cửa sổ vừa mở, xong thì đóng cửa sổ."
            t0 = time.time()
            logged = False
            while time.time() - t0 < timeout:
                try:
                    names = {c["name"] for c in ctx.cookies("https://www.douyin.com")}
                except Exception:
                    break  # người dùng đã đóng cửa sổ
                logged = bool(names & {"sessionid", "sessionid_ss", "sid_guard"})
                if not ctx.pages:
                    break
                time.sleep(1)
            try:
                ctx.close()
            except Exception:
                pass
        login_state.update(logged_in=logged, msg="Đã đăng nhập Douyin." if logged else "Chưa thấy đăng nhập.")
    except Exception as e:
        login_state.update(msg=str(e)[:200])
    finally:
        login_state["open"] = False


def check_login():
    """Đọc cookie trong hồ sơ trình duyệt để biết đã đăng nhập Douyin chưa (không mở trang)."""
    from playwright.sync_api import sync_playwright

    with profile_lock, sync_playwright() as p:
        ctx = _open(p)
        try:
            names = {c["name"] for c in ctx.cookies("https://www.douyin.com")}
        finally:
            ctx.close()
    login_state["logged_in"] = bool(names & {"sessionid", "sessionid_ss", "sid_guard"})
    return login_state["logged_in"]


if __name__ == "__main__":
    try:
        for it in search("douyin", sys.argv[1]):
            print(it["likes"], it["duration"], it["url"], it["title"][:50])
    except NeedLogin as e:
        print("Cần đăng nhập:", e)
