"""Tạo biểu tượng app (từ logo kênh) và lối tắt "Lồng Tiếng Việt" trên Desktop + menu Start.

Chạy một lần:  python tao-loi-tat.py
"""
import base64
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "brand" / "app.ico"


def make_icon():
    from PIL import Image, ImageChops, ImageDraw

    src = next((p for p in (ROOT / "brand" / "logo.png", ROOT / "static" / "favicon.png") if p.is_file()), None)
    if not src:
        return None
    im = Image.open(src).convert("RGBA")
    s = min(im.size)
    im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2)).resize((256, 256))
    mask = Image.new("L", (256, 256), 0)  # bo tròn cho đẹp trên taskbar
    ImageDraw.Draw(mask).ellipse((0, 0, 255, 255), fill=255)
    im.putalpha(ImageChops.darker(im.getchannel("A"), mask))
    ICON.parent.mkdir(exist_ok=True)
    im.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON


def shortcut(lnk, target, args, icon):
    tmp = lnk.with_name("LongTiengViet-tmp.lnk")  # WScript.Shell không lưu được tên có dấu -> lưu tạm rồi đổi tên
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{tmp}');"
          f"$s.TargetPath='{target}';$s.Arguments='{args}';$s.WorkingDirectory='{ROOT}';"
          f"$s.Description='Lồng Tiếng Việt - tải, dịch, lồng tiếng và đăng video';"
          + (f"$s.IconLocation='{icon},0';" if icon else "") + "$s.Save()")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()  # giữ đúng chữ có dấu
    subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc], check=True, capture_output=True)
    os.replace(tmp, lnk)


def folder(name):
    out = subprocess.run(["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{name}')"],
                         capture_output=True, text=True).stdout.strip()
    return Path(out) if out else None


def main():
    icon = make_icon()
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.is_file():
        pyw = Path(sys.executable)
    made = []
    for name in ("Desktop", "Programs"):  # Programs = menu Start
        d = folder(name)
        if d and d.is_dir():
            lnk = d / "Lồng Tiếng Việt.lnk"
            shortcut(lnk, pyw, f'"{ROOT / "app.pyw"}"', icon)
            made.append(lnk)
    for m in made:
        print("Đã tạo:", m)
    print("Biểu tượng:", icon or "(không có logo trong brand/, dùng biểu tượng Python)")


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
