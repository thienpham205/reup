"""
postfx.py - Hoàn thiện video sau khi lồng tiếng:
  - bản dọc 9:16 (1080x1920) cho Shorts / Reels / TikTok: nền mờ hoặc cắt giữa
  - gắn logo + chữ ký (ảnh PNG trong suốt vẽ bằng Pillow rồi chồng lên bằng ffmpeg)
"""
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
BRAND_DIR = ROOT / "brand"
LOGO = BRAND_DIR / "logo.png"
POSITIONS = ("tr", "tl", "br", "bl")
VERTICAL = ("off", "blur", "crop")
DEFAULT_BRAND = {"enabled": False, "text": "", "pos": "tr", "opacity": 85, "size": 14}
VW, VH = 1080, 1920
FONTS = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/tahomabd.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def clean_brand(b):
    b = {**DEFAULT_BRAND, **(b or {})}
    b["enabled"] = bool(b.get("enabled"))
    b["text"] = str(b.get("text") or "").strip()[:60]
    b["pos"] = b["pos"] if b.get("pos") in POSITIONS else "tr"
    for k, lo, hi in (("opacity", 10, 100), ("size", 5, 40)):
        try:
            b[k] = int(min(max(int(b[k]), lo), hi))
        except (TypeError, ValueError):
            b[k] = DEFAULT_BRAND[k]
    return b


def has_brand(b):
    return bool(b and (b.get("text") or LOGO.is_file()))  # bật/tắt theo từng video (tuỳ chọn "brand" của job)


def save_logo(file_storage):
    """Lưu logo người dùng tải lên thành PNG trong suốt, cạnh dài tối đa 1024 px."""
    BRAND_DIR.mkdir(exist_ok=True)
    im = Image.open(file_storage.stream)
    im.load()
    im = im.convert("RGBA")
    im.thumbnail((1024, 1024))
    im.save(LOGO)


def _font(px):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, px)
        except Exception:
            continue
    return ImageFont.load_default()


def brand_layer(w, h, b):
    """Ảnh RGBA đúng cỡ video, có logo + chữ ở góc đã chọn."""
    b = clean_brand(b)
    base = min(w, h)
    margin = int(base * 0.035)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    parts = []
    if LOGO.is_file():
        lg = Image.open(LOGO).convert("RGBA")
        lw = max(16, int(base * b["size"] / 100))
        lg = lg.resize((lw, max(1, int(lg.height * lw / lg.width))), Image.LANCZOS)
        parts.append(lg)
    if b["text"]:
        font = _font(max(12, int(base * 0.042)))
        d = ImageDraw.Draw(layer)
        x0, y0, x1, y1 = d.textbbox((0, 0), b["text"], font=font, stroke_width=3)
        tx = Image.new("RGBA", (x1 - x0 + 4, y1 - y0 + 4), (0, 0, 0, 0))
        ImageDraw.Draw(tx).text((2 - x0, 2 - y0), b["text"], font=font, fill=(255, 255, 255, 255),
                                stroke_width=3, stroke_fill=(0, 0, 0, 200))
        parts.append(tx)
    if not parts:
        return layer
    gap = int(base * 0.015)
    bw = sum(p.width for p in parts) + gap * (len(parts) - 1)
    bh = max(p.height for p in parts)
    block = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    x = 0
    for p in parts:
        block.alpha_composite(p, (x, (bh - p.height) // 2))
        x += p.width + gap
    if b["opacity"] < 100:
        a = block.getchannel("A").point(lambda v: v * b["opacity"] // 100)
        block.putalpha(a)
    right, bottom = b["pos"][1] == "r", b["pos"][0] == "b"
    # bản dọc: tránh mép dưới (nút bấm / chú thích của TikTok, Reels) và mép trên (thanh tìm kiếm)
    tall = h > w * 1.5
    my = int(h * 0.09) if tall else margin
    pos = (w - bw - margin if right else margin, h - bh - (int(h * 0.2) if tall else margin) if bottom else my)
    layer.alpha_composite(block, pos)
    return layer


def video_size(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "json", str(video)], capture_output=True, text=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def _ffmpeg(args, log, cwd=None):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=cwd)
    if r.returncode:
        raise RuntimeError(f"ffmpeg lỗi: {r.stderr.strip()[-300:]}")


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy",
       "-movflags", "+faststart"]


def _ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02}:{int(t % 60):02}.{int(t * 100 % 100):02}"


def karaoke_ass(segs, y, path):
    """Phụ đề kiểu TikTok: chữ to giữa khung, từ đang đọc chuyển vàng (karaoke \\k).
    Thời gian từng từ chia theo độ dài chữ trong khoảng giọng Việt đang đọc câu đó."""
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: K,Arial,76,&H0000E1FF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,6,3,8,70,70,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for s in segs:
        text = (s.get("vi") or "").strip()
        if not text:
            continue
        start, end = float(s["start"]), float(s["end"])
        speak = min(max(float(s.get("spoken") or 0), 0.3), end - start) if s.get("spoken") else end - start
        words = text.replace("{", "(").replace("}", ")").split()
        weights = [len(w) + 1 for w in words]
        tot = sum(weights)
        cs = [max(1, round(speak * 100 * k / tot)) for k in weights]
        body = " ".join(f"{{\\k{c}}}{w}" for c, w in zip(cs, words))
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},K,,0,0,0,,{{\\an8\\pos(540,{y})}}{body}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return bool(lines)


def make_vertical(src, dst, mode, brand, tmp_dir, log=print, segs=None):
    w, h = video_size(src)
    if mode == "crop":
        chain = f"[0:v]scale={VW}:{VH}:force_original_aspect_ratio=increase,crop={VW}:{VH},setsar=1[v]"
    else:  # nền là chính video phóng to + làm mờ, video gốc nằm giữa
        chain = (f"[0:v]split[a][b];[a]scale={VW}:{VH}:force_original_aspect_ratio=increase,crop={VW}:{VH},"
                 f"boxblur=24:2,eq=brightness=-0.06[bg];[b]scale={VW}:{VH}:force_original_aspect_ratio=decrease[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
    tall = h >= w * 1.7
    if tall:  # đã là video dọc -> chỉ đưa về đúng cỡ
        chain = f"[0:v]scale={VW}:{VH}:force_original_aspect_ratio=decrease,pad={VW}:{VH}:(ow-iw)/2:(oh-ih)/2,setsar=1[v]"
    if segs:
        # nền mờ: chữ nằm ngay dưới khung video (không đè phụ đề Trung in sẵn); còn lại: khoảng 2/3 chiều cao,
        # trên vùng nút bấm / chú thích của TikTok, Reels
        fh = VW * h / w
        y = int((VH + fh) / 2 + 40) if mode != "crop" and not tall and fh < VH * 0.6 else int(VH * 0.64)
        if karaoke_ass(segs, y, Path(tmp_dir) / "k.ass"):
            chain += ";[v]subtitles=k.ass[v]"
    inputs = ["-i", str(Path(src).resolve())]
    if has_brand(brand):
        png = Path(tmp_dir) / "brand_v.png"
        brand_layer(VW, VH, brand).save(png)
        inputs += ["-i", "brand_v.png"]
        chain += ";[v][1:v]overlay=0:0[v]"
    # chạy trong thư mục tạm để đường dẫn file phụ đề không bị lỗi ký tự trên Windows
    _ffmpeg(inputs + ["-filter_complex", chain, "-map", "[v]", "-map", "0:a?"] + ENC + [str(Path(dst).resolve())],
            log, cwd=str(tmp_dir))


def apply_brand(src, dst, brand, tmp_dir, log=print):
    w, h = video_size(src)
    png = Path(tmp_dir) / "brand_h.png"
    brand_layer(w, h, brand).save(png)
    _ffmpeg(["-i", str(src), "-i", str(png), "-filter_complex", "[0:v][1:v]overlay=0:0[v]", "-map", "[v]",
             "-map", "0:a?"] + ENC + [str(dst)], log)


def finish(job_dir, video_name, vertical, brand, log=print, vsubs=False):
    """Gắn logo vào video chính (thay tại chỗ) và tạo bản dọc <tên>.doc.mp4 (vsubs: kèm phụ đề kiểu TikTok).
    Trả về danh sách việc đã làm."""
    job_dir = Path(job_dir)
    src = job_dir / video_name
    done = []
    tmp = job_dir / "_fx"
    tmp.mkdir(exist_ok=True)
    try:
        if vertical in ("blur", "crop"):
            log("Tạo bản video dọc 9:16 cho Shorts / Reels / TikTok...")
            dst = job_dir / video_name.replace(".vi.mp4", ".doc.mp4")
            part = tmp / "vertical.mp4"
            segs = None
            if vsubs:
                try:
                    segs = json.loads((job_dir / "segs.json").read_text(encoding="utf-8"))["segs"]
                    log("  kèm phụ đề kiểu TikTok (chữ to, sáng dần theo lời đọc)")
                except Exception:
                    log("  không có lời thoại đã lưu -> bản dọc không kèm phụ đề")
            make_vertical(src, part, vertical, brand, tmp, log, segs)
            part.replace(dst)
            done.append("vertical")
        if has_brand(brand):
            log("Gắn logo / chữ ký vào video...")
            part = tmp / "brand.mp4"
            apply_brand(src, part, brand, tmp, log)
            part.replace(src)
            done.append("brand")
    finally:
        for p in tmp.glob("*"):
            p.unlink(missing_ok=True)
        tmp.rmdir()
    return done


def preview(brand, frame=None):
    """Ảnh JPEG xem trước: khung ngang + khung dọc có logo / chữ ký."""
    if frame and Path(frame).is_file():
        bg = Image.open(frame).convert("RGB")
    else:
        bg = Image.new("RGB", (1280, 720), (60, 64, 80))
        d = ImageDraw.Draw(bg)
        for y in range(720):
            d.line([(0, y), (1280, y)], fill=(40 + y // 12, 50 + y // 20, 90 + y // 10))
    bg = bg.resize((960, int(bg.height * 960 / bg.width)))
    h = bg.convert("RGBA")
    h.alpha_composite(brand_layer(h.width, h.height, {**brand, "enabled": True}))
    vh = h.height
    vw = int(vh * 9 / 16)
    big = bg.resize((int(bg.width * vh / bg.height), vh))
    x0 = max(0, (big.width - vw) // 2)
    v = big.crop((x0, 0, x0 + vw, vh)).convert("RGBA")
    from PIL import ImageFilter
    v = v.filter(ImageFilter.GaussianBlur(10))
    fg = bg.resize((vw, int(bg.height * vw / bg.width))).convert("RGBA")
    v.alpha_composite(fg, (0, (vh - fg.height) // 2))
    v.alpha_composite(brand_layer(vw, vh, {**brand, "enabled": True}))
    out = Image.new("RGB", (h.width + vw + 16, vh), (20, 20, 24))
    out.paste(h.convert("RGB"), (0, 0))
    out.paste(v.convert("RGB"), (h.width + 16, 0))
    return out
