"""
watcher.py - Lấy danh sách video mới nhất của một kênh (để tự dịch video mới).

  - Douyin: trang tác giả https://www.douyin.com/user/<sec_uid> (hoặc link chia sẻ trang cá nhân v.douyin.com/...)
    -> mở bằng trình duyệt ẩn như khi tải video, đọc danh sách bài đăng.
  - YouTube / TikTok / Bilibili...: yt-dlp đọc danh sách video của kênh.

    python watcher.py <link kênh>
"""
import json
import re
import subprocess
import sys

import douyin_dub

POST_XHR_JS = """async (uid) => new Promise((res) => {
  const x = new XMLHttpRequest();
  x.open('GET', '/aweme/v1/web/aweme/post/?device_platform=webapp&aid=6383&channel=channel_pc_web&sec_user_id='
         + uid + '&max_cursor=0&count=18&publish_video_strategy_type=2');
  x.timeout = 20000;
  x.onload = () => res(x.responseText); x.onerror = () => res(''); x.ontimeout = () => res('');
  x.send();
})"""


def is_douyin(url):
    return "douyin.com" in url


def _douyin_user(url):
    """sec_uid của tác giả từ link trang cá nhân (link rút gọn thì mở ra để lấy link thật)."""
    m = re.search(r"/user/([\w-]{20,})", url)
    if m:
        return m.group(1)
    import requests

    r = requests.get(url, headers={"User-Agent": douyin_dub.MOBILE_UA}, allow_redirects=True, timeout=20)
    m = re.search(r"/user/([\w-]{20,})", r.url) or re.search(r"sec_uid=([\w-]{20,})", r.url)
    if m:
        return m.group(1)
    raise RuntimeError("Link này không phải trang cá nhân Douyin. Hãy mở trang tác giả rồi sao chép link.")


def list_douyin(url):
    from playwright.sync_api import sync_playwright

    uid = _douyin_user(url)
    found, name = {}, ""
    with sync_playwright() as p:
        opts = dict(user_agent=douyin_dub.DESKTOP_UA, locale="zh-CN", viewport={"width": 1280, "height": 800})
        browser = None
        try:
            ctx = p.chromium.launch_persistent_context(str(douyin_dub.BROWSER_PROFILE), headless=True, **opts)
        except Exception:  # hồ sơ đang được dùng để tải video -> phiên tạm
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(**opts)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def take(j):
            for a in (j or {}).get("aweme_list") or []:
                if a.get("aweme_id") and a["aweme_id"] not in found:
                    found[a["aweme_id"]] = a

        def on_resp(r):
            if "aweme/post" in r.url:
                try:
                    take(r.json())
                except Exception:
                    pass

        page.on("response", on_resp)
        page.goto(f"https://www.douyin.com/user/{uid}", wait_until="domcontentloaded", timeout=60000)
        for _ in range(20):
            if found:
                break
            page.wait_for_timeout(500)
        for attempt in range(3):  # trang không tự gọi API -> gọi từ bên trong trang (Douyin tự ký request)
            if found:
                break
            try:
                take(json.loads(page.evaluate(POST_XHR_JS, uid) or "{}"))
            except Exception:
                pass
            if not found:
                page.wait_for_timeout(2500 * (attempt + 1))
        dom = []
        if not found:
            try:
                dom = page.eval_on_selector_all("a[href*='/video/']", "els => els.map(e => e.href)")
            except Exception:
                dom = []
        try:
            name = re.sub(r"\s*的抖音.*$|\s*-\s*抖音.*$", "", page.title() or "")
        except Exception:
            pass
        ctx.close()
        if browser:
            browser.close()
    items = []
    for a in found.values():
        if a.get("images"):  # bài ảnh, không phải video
            continue
        items.append({"id": a["aweme_id"], "url": f"https://www.douyin.com/video/{a['aweme_id']}",
                      "title": (a.get("desc") or "")[:120], "time": a.get("create_time") or 0})
        name = name or ((a.get("author") or {}).get("nickname") or "")
    for href in dom:
        m = re.search(r"/video/(\d{10,})", href)
        if m and all(i["id"] != m.group(1) for i in items):
            items.append({"id": m.group(1), "url": f"https://www.douyin.com/video/{m.group(1)}", "title": "", "time": 0})
    if not items:
        raise RuntimeError("Không đọc được danh sách video (Douyin chặn tạm hoặc kênh không có video). Sẽ thử lại sau.")
    items.sort(key=lambda i: i["time"], reverse=True)
    return name, items


def list_ytdlp(url, n=15):
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "-J", "--playlist-end", str(n), url],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if r.returncode:
        raise RuntimeError((r.stderr.strip().splitlines() or ["yt-dlp lỗi"])[-1][:300])
    data = json.loads(r.stdout)
    entries = data.get("entries") or []
    if entries and entries[0].get("_type") == "playlist" and entries[0].get("entries") is not None:
        entries = entries[0]["entries"]  # kênh YouTube: tab "Videos"
    items = []
    for e in entries:
        u = e.get("url") or e.get("webpage_url")
        if not e.get("id") or not u:
            continue
        if not u.startswith("http"):
            u = f"https://www.youtube.com/watch?v={e['id']}"
        items.append({"id": e["id"], "url": u, "title": (e.get("title") or "")[:120], "time": e.get("timestamp") or 0})
    if not items:
        raise RuntimeError("Không thấy video nào ở link này.")
    return data.get("uploader") or data.get("channel") or data.get("title") or "", items


def list_videos(url):
    """Trả về (tên kênh, [{id, url, title, time}]) mới nhất trước."""
    url = douyin_dub.extract_url(url)
    return list_douyin(url) if is_douyin(url) else list_ytdlp(url)


if __name__ == "__main__":
    nm, its = list_videos(sys.argv[1])
    print(nm)
    for it in its:
        print(it["id"], it["url"], it["title"][:60])
