"""Lồng Tiếng Việt - bản app: mở tool trong cửa sổ riêng (không cần trình duyệt, không có cửa sổ đen).

Chạy:  pythonw app.pyw    (hoặc bấm lối tắt "Lồng Tiếng Việt" trên Desktop - tạo bằng: python tao-loi-tat.py)
- Tự bật server ngầm nếu chưa chạy; nếu server đã chạy sẵn thì chỉ mở cửa sổ.
- Đóng cửa sổ thì tắt server do app bật (hỏi lại nếu đang làm video hoặc có bài hẹn giờ).
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import webview

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", 5577))
URL = f"http://127.0.0.1:{PORT}"
TITLE = "Lồng Tiếng Việt"
ICON = ROOT / "brand" / "app.ico"
LOG = ROOT / "app.log"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def get(path, timeout=2):
    with urllib.request.urlopen(URL + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def server_up():
    try:
        get("/api/jobs", 1.5)
        return True
    except Exception:
        return False


def start_server():
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":  # server cần python.exe (in log ra file), vẫn ẩn cửa sổ
        py = py.with_name("python.exe")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    log = open(LOG, "a", encoding="utf-8")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} mở app =====\n")
    log.flush()
    return subprocess.Popen([str(py), "server.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def stop_server(proc):
    if proc and proc.poll() is None:  # tắt cả các bước con (ffmpeg, lồng tiếng...) đang chạy
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)


def msgbox(text, kind=0x30, buttons=0x4):
    """kind 0x30 = cảnh báo, 0x10 = lỗi; buttons 0x4 = Có/Không, 0x0 = OK. Trả về True khi bấm Có/OK."""
    r = ctypes.windll.user32.MessageBoxW(None, text, TITLE, buttons | kind | 0x40000)  # 0x40000 = luôn nổi trên cùng
    return r in (1, 6)


def busy_note():
    """Nhắc việc đang dở trước khi đóng app (đóng thì server tắt)."""
    try:
        jobs = get("/api/jobs")
        sched = get("/api/schedule")
    except Exception:
        return ""
    run = [j for j in jobs if j.get("status") in ("running", "queued")]
    posting = [p for j in jobs for p in (j.get("posts") or []) if p.get("status") in ("running", "queued")]
    notes = []
    if run:
        notes.append(f"- {len(run)} video đang làm / đang chờ sẽ bị dừng (sau mở lại bấm \"Chạy tiếp từ bước lỗi\").")
    if posting:
        notes.append(f"- {len(posting)} bài đang đăng sẽ bị dừng.")
    if sched:
        notes.append(f"- {len(sched)} bài hẹn giờ sẽ không tự đăng khi app đang tắt (mở lại app là đăng tiếp).")
    return "\n".join(notes)


def set_icon():
    """Gắn logo kênh làm biểu tượng cửa sổ + thanh taskbar."""
    if not ICON.is_file():
        return
    u = ctypes.windll.user32
    for _ in range(50):
        hwnd = u.FindWindowW(None, TITLE)
        if hwnd:
            break
        time.sleep(0.1)
    else:
        return
    for size, which in ((16, 0), (32, 1)):  # ICON_SMALL, ICON_BIG
        h = u.LoadImageW(None, str(ICON), 1, size * 2 if which else size, size * 2 if which else size, 0x10)
        if h:
            u.SendMessageW(hwnd, 0x80, which, h)  # WM_SETICON


def main():
    try:  # tách khỏi nhóm "Python" trên taskbar để hiện đúng biểu tượng app
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LongTiengViet.App")
    except Exception:
        pass
    proc = None
    if not server_up():
        proc = start_server()
        for _ in range(240):  # tối đa 60 giây (lần đầu nạp thư viện có thể lâu)
            if server_up():
                break
            if proc.poll() is not None:
                msgbox(f"Không bật được server. Xem lỗi trong file:\n{LOG}", kind=0x10, buttons=0)
                return
            time.sleep(0.25)
        else:
            msgbox(f"Server khởi động quá lâu. Xem file:\n{LOG}", kind=0x10, buttons=0)
            stop_server(proc)
            return

    win = webview.create_window(TITLE, URL, width=1380, height=900, min_size=(900, 600), text_select=True)

    def on_closing():
        if proc is None:  # server do nơi khác bật (cửa sổ .bat) -> để nguyên
            return True
        note = busy_note()
        if note and not msgbox("Đóng app sẽ tắt luôn tool:\n\n" + note + "\n\nVẫn đóng?"):
            return False
        return True

    win.events.closing += on_closing
    win.events.shown += set_icon
    webview.settings["ALLOW_DOWNLOADS"] = True  # nút tải video / ảnh bìa -> lưu vào thư mục Downloads
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # liên kết tài khoản, YouTube... mở trình duyệt
    try:
        webview.start(private_mode=False, storage_path=str(ROOT / ".app-profile"))  # giữ lựa chọn đã lưu
    finally:
        stop_server(proc)


if __name__ == "__main__":
    main()
