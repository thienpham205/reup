#!/usr/bin/env python3
"""
publish_kit.py - "Bộ đăng bài" cho video đã lồng tiếng:
  * tiêu đề + mô tả + hashtag (bằng AI nếu có key, không thì tạo theo mẫu đơn giản)
  * ảnh bìa: chọn khung hình đẹp trong video rồi chèn chữ tiêu đề

Dùng riêng:  python publish_kit.py <thư mục job>
"""
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

KIT_FILE = "kit.json"
COVER_FILE = "cover.jpg"
TIKTOK_COVER = "cover_tiktok.jpg"
FRAME_DIR = "frames"
STYLES = ("noi", "poster", "hop", "dam", "tron")  # chữ nổi / poster / hộp màu / chữ trên nền tối / không chữ
COVER_RATIOS = ("auto", "ngang", "doc", "vuong")
DEFAULT_BADGE = ""  # nhãn góc trái ảnh bìa (vd "Thuyết minh"); mặc định không có
DEFAULT_TAGS = ["phimhay", "reviewphim", "longtieng", "thuyetminh", "phimtrungquoc", "tomtatphim", "xuhuong", "fyp"]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\tahomabd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


# ---------------------------------------------------------------- khung hình
def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _duration(video):
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)])
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _score(path):
    """Điểm khung hình: nét + tương phản, trừ điểm khi quá tối / quá sáng (cảnh chuyển, màn hình đen)."""
    im = Image.open(path).convert("L")
    im.thumbnail((320, 320))
    sharp = ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).var[0]
    st = ImageStat.Stat(im)
    mean, contrast = st.mean[0], st.stddev[0]
    penalty = 0.25 if mean < 45 or mean > 215 else 1.0
    return (sharp ** 0.5) * (contrast + 1) * penalty


def _face_bonus(path):
    """Khung có mặt người rõ thì bìa hút mắt hơn."""
    try:
        import cv2

        g = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        g = cv2.resize(g, (480, int(480 * g.shape[0] / g.shape[1])))
        cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cas.detectMultiScale(g, 1.15, 5, minSize=(32, 32))
        if len(faces):
            big = max(f[2] * f[3] for f in faces) / (g.shape[0] * g.shape[1])
            return 1.3 + min(big * 8, 0.7)
    except Exception:
        pass
    return 1.0


def _colorful(path):
    im = Image.open(path).convert("HSV")
    im.thumbnail((160, 160))
    return 0.75 + ImageStat.Stat(im).mean[1] / 255  # độ bão hoà màu trung bình


def extract_frames(video, job_dir, n=8, pool=16):
    """Lấy pool khung hình rải đều (bỏ 6% đầu và cuối), chấm điểm (nét, tương phản, màu, mặt người),
    giữ n khung đẹp nhất theo thứ tự thời gian. Trả về [{file, t, score}]."""
    out_dir = Path(job_dir) / FRAME_DIR
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("f*.jpg"):
        old.unlink(missing_ok=True)
    dur = _duration(video)
    frames = []
    for i in range(pool):
        t = dur * (0.06 + 0.88 * i / max(pool - 1, 1)) if dur else float(i)
        f = out_dir / f"f{i}.jpg"
        _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
              "-vf", "scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))'", "-q:v", "2", str(f)])
        if f.is_file() and f.stat().st_size > 0:
            try:
                s = _score(f) * _face_bonus(f) * _colorful(f)
                frames.append({"file": f"{FRAME_DIR}/{f.name}", "t": round(t, 2), "score": round(s, 1)})
            except Exception:
                pass
    keep = sorted(sorted(frames, key=lambda x: -x["score"])[:n], key=lambda x: x["t"])
    for fr in frames:
        if fr not in keep:
            (Path(job_dir) / fr["file"]).unlink(missing_ok=True)
    return keep


# ---------------------------------------------------------------- ảnh bìa
def _font(size):
    for p in FONT_CANDIDATES:
        if Path(p).is_file():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


HEAVY_FONTS = [r"C:\Windows\Fonts\seguibl.ttf", r"C:\Windows\Fonts\ariblk.ttf"] + FONT_CANDIDATES
YELLOW, RED, WHITE, INK = (255, 214, 10), (235, 36, 54), (255, 255, 255), (18, 18, 18)
RATIOS = {"ngang": (1280, 720), "doc": (1080, 1920), "vuong": (1080, 1080)}


def _heavy(size):
    for p in HEAVY_FONTS:
        if Path(p).is_file():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return _font(size)


def _enhance(im):
    """Ảnh bìa nổi hơn: kéo tương phản, tăng màu, làm nét."""
    from PIL import ImageEnhance, ImageOps

    im = ImageOps.autocontrast(im, cutoff=1)
    im = ImageEnhance.Color(im).enhance(1.25)
    im = ImageEnhance.Contrast(im).enhance(1.08)
    return ImageEnhance.Sharpness(im).enhance(1.4)


def _vignette(im, strength=0.45):
    W, H = im.size
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    d.ellipse([-W * 0.25, -H * 0.25, W * 1.25, H * 1.25], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(min(W, H) * 0.18))
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    return Image.composite(im, Image.blend(im, dark, strength), m)


def _focus(im):
    """Tâm khuôn mặt lớn nhất (tỉ lệ 0-1) để cắt khung không mất mặt nhân vật; không thấy -> giữa hơi cao."""
    try:
        import cv2
        import numpy as np

        g = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
        cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cas.detectMultiScale(g, 1.15, 5, minSize=(max(24, g.shape[1] // 20),) * 2)
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            return (x + w / 2) / im.width, (y + h / 2) / im.height
    except Exception:
        pass
    return 0.5, 0.42


def _canvas(im, W, H, y_center=0.5):
    """Đặt khung hình vào khổ W x H: cùng hướng -> cắt vừa (giữ mặt); khác hướng -> nền mờ + ảnh giữa.
    Trả về (ảnh, hộp ảnh chính (x0, y0, x1, y1))."""
    from PIL import ImageOps

    r_in, r_out = im.width / im.height, W / H
    if abs(r_in - r_out) / r_out < 0.45:
        return ImageOps.fit(im, (W, H), Image.LANCZOS, centering=_focus(im)), (0, 0, W, H)
    bg = ImageOps.fit(im, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.35)
    fg = im.copy()
    fg.thumbnail((W, H), Image.LANCZOS)
    if fg.width < W * 0.98:  # video dọc trên khổ ngang -> ảnh cao bằng khung
        fg = fg.resize((int(fg.width * H / fg.height), H), Image.LANCZOS)
    else:  # video ngang trên khổ dọc -> phóng to hơn chút cho đỡ trống
        s = 1.0  # giữ trọn bề ngang (không cắt mất logo / nhân vật sát mép)
        fg = fg.resize((int(W * s), int(fg.height * W * s / fg.width)), Image.LANCZOS)
        fg = fg.crop(((fg.width - W) // 2, 0, (fg.width - W) // 2 + W, fg.height))
    x = (W - fg.width) // 2
    y = min(max(0, int(H * y_center - fg.height / 2)), H - fg.height)
    bg.paste(fg, (x, y))
    return bg, (x, y, x + fg.width, y + fg.height)


def _words(text):
    """'Cái *kết* bất ngờ' -> [(CÁI, False), (KẾT, True), ...]: từ trong *...* được tô màu nhấn."""
    out, hl = [], False
    for tok in re.split(r"(\*)", re.sub(r"\s+", " ", text or "").strip()):
        if tok == "*":
            hl = not hl
            continue
        if hl and tok.strip():  # cụm nhấn giữ liền một khối, không bị ngắt dòng giữa chừng
            out.append((" ".join(tok.split()).upper(), True))
        else:
            out += [(w.upper(), hl) for w in tok.split()]
    return out


def _lines(draw, words, font, max_w):
    lines, cur = [], []
    for w in words:
        trial = " ".join(x for x, _ in cur + [w])
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    return lines + ([cur] if cur else [])


def _fit_text(words, max_w, max_lines, size, min_size=24):
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    while True:
        font = _heavy(size)
        lines = _lines(d, words, font, max_w)
        widest = max(d.textlength(" ".join(x for x, _ in l), font=font) for l in lines)
        if (len(lines) <= max_lines and widest <= max_w) or size <= min_size:
            return font, size, lines
        size = int(size * 0.93)


def _text_block(lines, font, size, base=WHITE, hl=YELLOW, stroke_col=INK, tilt=0.0, banners=None):
    """Khối chữ (RGBA, nền trong) có viền + bóng đổ; banners: màu nền từng dòng (kiểu hộp màu)."""
    d0 = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    space = d0.textlength(" ", font=font)
    stroke = 0 if banners else max(3, size // 9)
    lh = int(size * (1.32 if banners else 1.14))
    widths = [sum(d0.textlength(w, font=font) for w, _ in l) + space * (len(l) - 1) for l in lines]
    pad = int(size * 0.5) + stroke
    bw, bh = int(max(widths)) + pad * 2, lh * len(lines) + pad * 2
    layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i, l in enumerate(lines):
        x, y = (bw - widths[i]) / 2, pad + i * lh
        if banners:
            px, py = size * 0.32, size * 0.12
            d.rounded_rectangle([x - px, y - py, x + widths[i] + px, y + size * 1.18 + py],
                                radius=int(size * 0.16), fill=banners[i % len(banners)])
        for w, h in l:
            if banners:
                col = (RED if banners[i % len(banners)] != RED else WHITE) if h else (
                    INK if banners[i % len(banners)] in (YELLOW, WHITE) else WHITE)
            else:
                col = hl if h else base
            d.text((x, y), w, font=font, fill=col, stroke_width=stroke, stroke_fill=stroke_col)
            x += d0.textlength(w, font=font) + space
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(layer.getchannel("A").point(lambda a: int(a * 0.75)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(3, size // 10)))
    out = Image.new("RGBA", (bw + size // 6, bh + size // 6), (0, 0, 0, 0))
    out.alpha_composite(shadow, (size // 6, size // 6))
    out.alpha_composite(layer, (0, 0))
    if tilt:
        out = out.rotate(tilt, resample=Image.BICUBIC, expand=True)
    return out


def _badge(text, size, color=RED):
    font = _heavy(size)
    d0 = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    w = d0.textlength(text, font=font)
    px, py = int(size * 0.55), int(size * 0.32)
    im = Image.new("RGBA", (int(w + px * 2), int(size * 1.3 + py * 2)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=int(size * 0.35), fill=color)
    d.text((px, py - size * 0.08), text, font=font, fill=WHITE)
    return im.rotate(4, resample=Image.BICUBIC, expand=True)


def _blur_band(im, band):
    """Làm mờ dải phụ đề Trung in cứng trên khung hình."""
    if not band:
        return im
    W, H = im.size
    y0, y1 = int(H * band[0]), int(H * band[1])
    if y1 - y0 < 4:
        return im
    strip = im.crop((0, y0, W, y1)).filter(ImageFilter.GaussianBlur(18))
    strip = Image.blend(strip, Image.new("RGB", strip.size, (0, 0, 0)), 0.35)
    im.paste(strip, (0, y0))
    return im


def _auto_hl(lines):
    """Không đánh dấu *từ nhấn* -> tô màu cả dòng đầu (kiểu bìa TikTok quen thuộc)."""
    if any(h for l in lines for _, h in l):
        return lines
    return [[(w, i == 0 and len(lines) > 1) for w, _ in l] for i, l in enumerate(lines)] if len(lines) > 1 else \
        [[(w, j >= len(lines[0]) - 2) for j, (w, _) in enumerate(lines[0])]]


# TikTok cắt ảnh bìa dọc 9:16 thành 3:4 ở trang cá nhân (mất ~12,5% trên và dưới) -> chữ, logo, nhãn phải nằm trong
# vùng an toàn này
SAFE = 0.14


def _safe_top(im, frac):
    return int(im.height * (SAFE if im.height > im.width * 1.4 else frac))


def _paste_badge(out, badge):
    if badge:
        b = _badge(badge.upper(), int(min(out.size) * 0.045))
        out.alpha_composite(b, (int(out.width * 0.03), _safe_top(out, 0.03)))
    return out


def _overlay_text(out, box, words, style, badge, top=SAFE):
    out = out.convert("RGBA")
    W, H = out.size
    tall = H > W
    if words:
        size = int((W * 0.135) if tall else (H * 0.155))
        top_gap = tall and box[1] > H * 0.2  # khổ dọc có khoảng nền mờ phía trên ảnh
        if top_gap:  # chữ phải vừa khoảng trống đó: tối đa 3 dòng, cỡ theo chiều cao còn lại
            # khối chữ cao ~ (số dòng x 1,14 + 1,3) x cỡ chữ (kể cả đệm + bóng) -> phải lọt khoảng trống trên ảnh
            size = min(size, int((box[1] - H * (top + 0.02)) / (3 * 1.14 + 1.3)))
        font, size, lines = _fit_text(words, int(W * (0.88 if tall else 0.86)), 3 if top_gap else 4 if tall else 3, size)
        lines = _auto_hl(lines)
        if style == "hop":
            blk = _text_block(lines, font, size, banners=[YELLOW, WHITE], tilt=-2)
        else:
            blk = _text_block(lines, font, size, tilt=-3)
        x = (W - blk.width) // 2
        if not tall:
            y = H - blk.height - int(H * 0.04)
        elif top_gap:  # chữ nằm ở khoảng nền mờ phía trên ảnh
            y = max(int(H * top), int(H * top + (box[1] - H * top - blk.height) / 2))
        else:  # ảnh phủ kín khung dọc -> chữ hơi dưới giữa, trên vùng nút bấm của app
            y = min(int(H * 0.64) - blk.height // 2, int(H * (1 - SAFE)) - blk.height)
        out.alpha_composite(blk, (max(0, x), max(0, y)))
    return _paste_badge(out, badge)


def _poster(im, W, H, words, badge, top=SAFE):
    from PIL import ImageOps

    tall = H > W
    bg = ImageOps.fit(im, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(34))
    out = Image.blend(bg, Image.new("RGB", (W, H), (8, 8, 14)), 0.5).convert("RGBA")
    # ảnh chính: thẻ bo góc viền trắng, đổ bóng, nghiêng nhẹ
    a = im.width / im.height  # khung ảnh theo đúng tỉ lệ video (cắt bớt nếu quá dẹt / quá cao)
    if tall:
        cw = int(W * 0.86)
        ch = min(int(H * 0.42), int(cw / min(a, 1.6)))
    else:
        ch = int(H * 0.78)
        cw = min(int(W * 0.58), int(ch * max(a, 0.62)))
    card = ImageOps.fit(im, (cw, ch), Image.LANCZOS, centering=_focus(im))
    bd = max(6, int(min(W, H) * 0.012))
    framed = Image.new("RGBA", (cw + bd * 2, ch + bd * 2), (0, 0, 0, 0))
    r = int(min(W, H) * 0.03)
    m = Image.new("L", framed.size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, framed.width - 1, framed.height - 1], radius=r, fill=255)
    framed.paste(Image.new("RGBA", framed.size, WHITE + (255,)), (0, 0), m)
    m2 = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(m2).rounded_rectangle([0, 0, cw - 1, ch - 1], radius=max(2, r - bd), fill=255)
    framed.paste(card, (bd, bd), m2)
    framed = framed.rotate(-2, resample=Image.BICUBIC, expand=True)
    sh = Image.new("RGBA", framed.size, (0, 0, 0, 0))
    sh.putalpha(framed.getchannel("A").point(lambda a: int(a * 0.7)))
    sh = sh.filter(ImageFilter.GaussianBlur(int(min(W, H) * 0.02)))
    if tall:
        cx, cy = (W - framed.width) // 2, min(int(H * 0.63 - framed.height / 2), int(H * (1 - SAFE)) - framed.height)
    else:
        cx, cy = int(W * 0.04), (H - framed.height) // 2
    off = int(min(W, H) * 0.015)
    out.alpha_composite(sh, (cx + off, cy + off))
    out.alpha_composite(framed, (cx, cy))
    if words:
        if tall:
            room = cy - H * top - H * 0.02  # chữ phải nằm gọn giữa mép an toàn trên và khung ảnh
            font, size, lines = _fit_text(words, int(W * 0.88), 3, min(int(W * 0.13), int(room / (3 * 1.3))))
            blk = _text_block(_auto_hl(lines), font, size, tilt=-2)
            out.alpha_composite(blk, ((W - blk.width) // 2, max(int(H * top), int(H * top + (cy - H * top - blk.height) / 2))))
        else:
            area = W - (cx + framed.width) - int(W * 0.04)
            font, size, lines = _fit_text(words, int(area * 0.92), 5, int(H * 0.13))
            blk = _text_block(_auto_hl(lines), font, size, tilt=-2)
            out.alpha_composite(blk, (cx + framed.width + max(0, (area - blk.width) // 2) + int(W * 0.02),
                                      max(0, (H - blk.height) // 2)))
    return _paste_badge(out, badge)


def make_cover(frame_path, out_path, text="", style="noi", accent=YELLOW, ratio="auto", badge="", band=None,
               logo=None, logo_spot=None, rm_spot=None):
    """Vẽ ảnh bìa. style: noi (chữ nổi viral) / poster (khung ảnh trên nền mờ) / hop (chữ trong hộp màu) /
    dam (chữ dưới nền tối) / tron (không chữ). ratio: auto / ngang / doc / vuong. text: *từ* để tô màu nhấn.
    logo: file PNG logo kênh; logo_spot: chỗ logo cũ trong khung hình (đặt đè lên), không có thì đặt góc phải trên."""
    im = _blur_band(Image.open(frame_path).convert("RGB"), band)
    if rm_spot:  # xoá logo kênh cũ bằng cách vá ảnh
        import postfx

        im = postfx.remove_old_logo(im, rm_spot)
    if logo and logo_spot:  # đặt lên khung hình gốc trước khi dựng bìa -> logo đi theo ảnh, che đúng logo cũ
        import postfx

        im = im.convert("RGBA")
        im.alpha_composite(postfx.spot_layer(im.width, im.height, logo_spot))
        im = im.convert("RGB")
    # bìa dọc: logo góc / nhãn nằm ở mép trên vùng an toàn -> đẩy chữ xuống dưới chúng
    top = SAFE + (0.1 if (logo and not logo_spot) or badge else 0)
    _cover_body(im, out_path, text, style, accent, ratio, badge, top)
    if logo and not logo_spot:
        out = Image.open(out_path).convert("RGBA")
        lg = Image.open(logo).convert("RGBA")
        lw = int(min(out.size) * 0.15)
        lg = lg.resize((lw, int(lg.height * lw / lg.width)), Image.LANCZOS)
        m = int(min(out.size) * 0.035)
        out.alpha_composite(lg, (out.width - lw - m, _safe_top(out, 0.035)))
        out.convert("RGB").save(out_path, quality=93)
    return out_path


def _cover_body(im, out_path, text, style, accent, ratio, badge, top=SAFE):
    if style in ("noi", "poster", "hop"):
        W, H = RATIOS.get(ratio) or (RATIOS["doc"] if im.height > im.width else RATIOS["ngang"])
        im = _enhance(im)
        if style == "poster":
            out = _poster(im, W, H, _words(text), badge, top)
        else:
            # khổ dọc từ video ngang: hạ ảnh xuống dưới giữa để chừa chỗ cho chữ phía trên
            out, box = _canvas(im, W, H, 0.6 if text and H > W else 0.5)
            out = _vignette(out, 0.4 if style == "noi" else 0.25)
            out = _overlay_text(out, box, _words(text), style, badge, top)
        out.convert("RGB").save(out_path, quality=93)
        return out_path
    if ratio in RATIOS:
        im, _ = _canvas(im, *RATIOS[ratio])
    W, H = im.size
    text = re.sub(r"[\s*]+", " ", text or "").strip().upper()
    if not text or style == "tron":
        im.save(out_path, quality=92)
        return out_path

    draw = ImageDraw.Draw(im)
    max_w = int(W * 0.86)
    size = int(min(W, H) * (0.115 if H >= W else 0.13))
    while True:  # thu chữ tới khi vừa tối đa 4 dòng
        font = _font(size)
        lines = _wrap(draw, text, font, max_w)
        widest = max(draw.textlength(l, font=font) for l in lines)
        if (len(lines) <= 4 and widest <= max_w) or size <= 22:
            break
        size = int(size * 0.92)
    line_h = int(size * 1.22)
    block_h = line_h * len(lines)
    y0 = int(H * (0.90 if H >= W else 0.93)) - block_h  # chừa mép dưới cho nút bấm của app

    if style == "hop":
        pad_x, pad_y = int(size * 0.32), int(size * 0.10)
        for i, line in enumerate(lines):
            w = draw.textlength(line, font=font)
            x, y = (W - w) / 2, y0 + i * line_h
            fill = accent if i == 0 else (255, 255, 255)
            draw.rounded_rectangle([x - pad_x, y - pad_y, x + w + pad_x, y + size * 1.12 + pad_y],
                                   radius=int(size * 0.18), fill=fill)
            draw.text((x, y), line, font=font, fill=(18, 18, 18))
    else:  # "dam": nền tối dần phía dưới + chữ trắng viền đen, dòng đầu màu nhấn
        top = max(0, y0 - int(H * 0.12))
        grad = Image.new("L", (1, H), 0)
        for y in range(top, H):
            grad.putpixel((0, y), int(215 * min(1.0, (y - top) / max(1, (H - top) * 0.55))))
        im.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), grad.resize((W, H)))
        draw = ImageDraw.Draw(im)
        stroke = max(2, size // 12)
        for i, line in enumerate(lines):
            w = draw.textlength(line, font=font)
            draw.text(((W - w) / 2, y0 + i * line_h), line, font=font, fill=accent if i == 0 else (255, 255, 255),
                      stroke_width=stroke, stroke_fill=(0, 0, 0))
    im.save(out_path, quality=92)
    return out_path


# ---------------------------------------------------------------- tiêu đề + hashtag
META_SYSTEM = """Bạn là biên tập viên mạng xã hội chuyên đăng video ngắn (TikTok, YouTube Shorts, Facebook Reels) cho khán giả Việt Nam.
Dựa vào tiêu đề gốc và lời thoại tiếng Việt của video, hãy viết bộ nội dung đăng bài. Chỉ trả về JSON đúng dạng:
{"titles": ["...", "...", "..."], "cover_text": "...", "description": "...", "hashtags": ["...", "..."]}
Quy tắc:
- titles: 3 tiêu đề khác kiểu nhau (gây tò mò / tóm tắt nội dung / cảm xúc), tiếng Việt tự nhiên, mỗi tiêu đề tối đa 90 ký tự,
  không viết hoa toàn bộ, không bịa chi tiết không có trong lời thoại, không chứa hashtag.
- cover_text: 3 đến 7 từ, thật ngắn và gây tò mò, để in lên ảnh bìa. Bọc 1-2 từ đắt nhất trong dấu sao để tô màu,
  ví dụ: "Cái kết *không ai ngờ*".
- description: 1 đến 3 câu tóm tắt video, không tiết lộ kết thúc.
- hashtags: 10 đến 14 hashtag, viết liền không dấu, chữ thường, không có ký tự '#'. Trộn hashtag sát nội dung
  (thể loại, chủ đề, nhân vật) với vài hashtag phổ biến (xuhuong, fyp, reviewphim...)."""


def _clean_tag(t):
    t = unicodedata.normalize("NFD", str(t).replace("đ", "d").replace("Đ", "D"))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9_]", "", t.lower())


def _tags(items):
    seen, out = set(), []
    for t in items:
        t = _clean_tag(t)
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _has_cjk(s):
    return bool(re.search(r"[一-鿿]", s or ""))


def _short_cover(title, n=12):
    """Chữ bìa từ tiêu đề: lấy vế đầu (trước dấu phẩy / chấm), tối đa n từ, giữ dấu hỏi / chấm than."""
    t = re.sub(r"\s+", " ", title or "").strip()
    head = re.split(r"(?<=[?!])\s|[,.;:|–-]\s", t)[0].strip() or t
    words = head.split()
    out = " ".join(words[:n])
    return out + ("…" if len(words) > n else "")


def fallback_meta(src_title, lines_vi):
    """Không có AI: dịch tiêu đề gốc bằng Google Dịch (nếu được), hashtag theo mẫu chung."""
    raw_tags = re.findall(r"#([^\s#]+)", src_title or "")
    title = re.sub(r"#\S+", "", src_title or "").strip()
    if _has_cjk(title):
        try:  # bộ dịch có thử lại / đổi cổng của tool (thư viện deep_translator hay bị Google chặn)
            from douyin_dub import GoogleGtx

            title = GoogleGtx("zh-CN", "vi").translate(title) or title
        except Exception:
            try:
                from deep_translator import GoogleTranslator

                title = GoogleTranslator(source="zh-CN", target="vi").translate(title) or title
            except Exception:
                title = ""
    if not title or _has_cjk(title):
        title = " ".join(" ".join(lines_vi[:2]).split()[:14])
    title = title.strip(" .,") or "Video lồng tiếng Việt"
    title = title[:1].upper() + title[1:]
    return {
        "titles": [title[:90]],
        "cover_text": _short_cover(title),
        "description": " ".join(lines_vi[:3])[:300],
        "hashtags": _tags([t for t in raw_tags if not _has_cjk(t)] + DEFAULT_TAGS),
        "ai": False,
    }


def generate_meta(src_title, lines_vi, ai_cfg=None, ai_chat=None, parse_json=None):
    """Trả về {titles, cover_text, description, hashtags, ai}. AI lỗi -> dùng bản theo mẫu, kèm 'note'."""
    if not (ai_cfg and ai_chat):
        return fallback_meta(src_title, lines_vi)
    script = "\n".join(lines_vi)
    if len(script) > 7000:  # video dài: giữ đầu + cuối
        script = script[:4500] + "\n[...]\n" + script[-2300:]
    user = f"Tiêu đề gốc: {src_title or '(không có)'}\n\nLời thoại tiếng Việt:\n{script or '(video không có lời thoại)'}"
    try:
        data = (parse_json or json.loads)(ai_chat(ai_cfg, META_SYSTEM, user, timeout=60))
        titles = [re.sub(r"\s+", " ", str(t)).strip()[:110] for t in data.get("titles", []) if str(t).strip()][:3]
        if not titles:
            raise ValueError("AI không trả về tiêu đề")
        return {
            "titles": titles,
            "cover_text": re.sub(r"\s+", " ", str(data.get("cover_text") or "")).strip() or " ".join(titles[0].split()[:6]),
            "description": str(data.get("description") or "").strip()[:600],
            "hashtags": _tags(data.get("hashtags") or [])[:16] or _tags(DEFAULT_TAGS),
            "ai": True,
        }
    except Exception as e:
        meta = fallback_meta(src_title, lines_vi)
        meta["note"] = f"AI lỗi nên dùng bản tạo theo mẫu: {str(e)[:160]}"
        return meta


# ---------------------------------------------------------------- gộp lại
def load_kit(job_dir):
    try:
        return json.loads((Path(job_dir) / KIT_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None


def save_kit(job_dir, kit):
    (Path(job_dir) / KIT_FILE).write_text(json.dumps(kit, ensure_ascii=False, indent=1), encoding="utf-8")
    return kit


def _brand_cfg():
    try:
        return json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8")).get("brand")
    except Exception:
        return None


def part_cover_name(k, tiktok=False):
    return f"parts/cover{k:02}{'_tiktok' if tiktok else ''}.jpg"


def _part_covers(job_dir, kit, frames, idx, band, logo, spot, rm):
    """Video đã cắt nhiều phần: mỗi phần một bộ bìa (thường + TikTok) có nhãn "TẬP k", khung hình lấy trong phần đó."""
    try:
        import postfx

        parts = postfx.load_parts(job_dir)
    except Exception:
        parts = []
    if not parts:
        return
    style = kit.get("style", "noi")
    for part in parts:
        inside = [i for i, f in enumerate(frames) if part["start"] <= f["t"] < part["end"]]
        if inside:
            frame = job_dir / frames[max(inside, key=lambda i: frames[i]["score"])]["file"]
        else:  # không có khung đã chấm điểm trong phần này -> lấy khung giữa phần
            frame = job_dir / "parts" / f"_f{part['k']:02}.jpg"
            goc = next(iter(job_dir.glob("*.goc.mp4")), None)
            if not frame.is_file() and goc:
                _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{(part['start'] + part['end']) / 2:.2f}",
                      "-i", str(goc), "-frames:v", "1", "-q:v", "2", str(frame)])
            if not frame.is_file():
                frame = job_dir / frames[idx]["file"]
        badge = f"Tập {part['k']}"
        for tik in (False, True):
            make_cover(frame, job_dir / part_cover_name(part["k"], tik), kit.get("cover_text", ""),
                       (style if style in ("noi", "poster", "hop") else "noi") if tik else style,
                       ratio="doc" if tik else kit.get("ratio", "auto"), badge=badge, band=band,
                       logo=logo, logo_spot=spot, rm_spot=rm)


def render_cover(job_dir, kit):
    """Vẽ lại cover.jpg theo kit['frame'], kit['cover_text'], kit['style']."""
    job_dir = Path(job_dir)
    frames = kit.get("frames") or []
    if not frames:
        return None
    idx = min(max(int(kit.get("frame", 0)), 0), len(frames) - 1)
    band = None
    try:  # khung hình lấy từ video gốc -> làm mờ dải phụ đề Trung (nếu đã dò được lúc xuất video)
        band = json.loads((job_dir / "segs.json").read_text(encoding="utf-8")).get("band")
    except Exception:
        pass
    logo, spot, rm = None, None, None
    if kit.get("logo", True):
        try:
            import postfx

            b = postfx.clean_brand(_brand_cfg())
            logo = postfx.LOGO if postfx.LOGO.is_file() else None
            mode = b["old"] if (logo or b["old"] == "remove") else "keep"
            if mode != "keep":
                found = postfx.find_spot(job_dir, corner=b["pos"])
                spot, rm = (found, None) if mode == "cover" else (None, found)
        except Exception:
            pass
    make_cover(job_dir / frames[idx]["file"], job_dir / COVER_FILE, kit.get("cover_text", ""),
               kit.get("style", "noi"), ratio=kit.get("ratio", "auto"), badge=kit.get("badge", DEFAULT_BADGE), band=band,
               logo=logo, logo_spot=spot, rm_spot=rm)
    # bản riêng cho TikTok: dọc 9:16, chữ / logo nằm trong vùng 3:4 mà trang cá nhân TikTok còn giữ lại
    style = kit.get("style", "noi")
    make_cover(job_dir / frames[idx]["file"], job_dir / TIKTOK_COVER, kit.get("cover_text", ""),
               style if style in ("noi", "poster", "hop") else "noi", ratio="doc",
               badge=kit.get("badge", DEFAULT_BADGE), band=band, logo=logo, logo_spot=spot, rm_spot=rm)
    kit["frame"], kit["cover"], kit["cover_tiktok"] = idx, COVER_FILE, TIKTOK_COVER
    _part_covers(job_dir, kit, frames, idx, band, logo, spot, rm)
    return kit["cover"]


def build_kit(job_dir, video, src_title, lines_vi, ai_cfg=None, ai_chat=None, parse_json=None, keep_frames=False):
    """Tạo đầy đủ bộ đăng bài cho một job và lưu kit.json + cover.jpg."""
    job_dir = Path(job_dir)
    old = load_kit(job_dir) or {}
    frames = old.get("frames") if keep_frames and old.get("frames") else extract_frames(video, job_dir)
    meta = generate_meta(src_title, lines_vi, ai_cfg, ai_chat, parse_json)
    best = max(range(len(frames)), key=lambda i: frames[i]["score"]) if frames else 0
    kit = {
        **meta,
        "title": meta["titles"][0],
        "frames": frames,
        "frame": old.get("frame", best) if keep_frames else best,
        "style": old.get("style") if old.get("style") in STYLES else "noi",
        "ratio": old.get("ratio") if old.get("ratio") in COVER_RATIOS else "auto",
        "badge": old.get("badge", DEFAULT_BADGE),
    }
    render_cover(job_dir, kit)
    return save_kit(job_dir, kit)


if __name__ == "__main__":
    d = Path(sys.argv[1])
    vid = next(iter(list(d.glob("*.goc.mp4")) + list(d.glob("*.vi.mp4")) + list(d.glob("*.mp4"))), None)
    if not vid:
        sys.exit("Không thấy file video trong thư mục.")
    print(json.dumps(build_kit(d, vid, vid.stem, []), ensure_ascii=False, indent=1))
