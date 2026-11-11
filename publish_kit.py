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
FRAME_DIR = "frames"
STYLES = ("dam", "hop", "tron")  # chữ đậm trên nền mờ / chữ trong hộp màu / không chữ
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


def extract_frames(video, job_dir, n=6):
    """Lấy n khung hình rải đều trong video (bỏ 8% đầu và cuối). Trả về [{file, t, score}], tốt nhất đứng đầu về điểm."""
    out_dir = Path(job_dir) / FRAME_DIR
    out_dir.mkdir(exist_ok=True)
    dur = _duration(video)
    frames = []
    for i in range(n):
        t = dur * (0.08 + 0.84 * i / max(n - 1, 1)) if dur else float(i)
        f = out_dir / f"f{i}.jpg"
        _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
              "-vf", "scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))'", "-q:v", "3", str(f)])
        if f.is_file() and f.stat().st_size > 0:
            try:
                frames.append({"file": f"{FRAME_DIR}/{f.name}", "t": round(t, 2), "score": round(_score(f), 1)})
            except Exception:
                pass
    return frames


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


def make_cover(frame_path, out_path, text="", style="dam", accent=(255, 214, 10)):
    """Vẽ chữ tiêu đề lên khung hình. text rỗng hoặc style 'tron' -> chỉ lưu khung hình."""
    im = Image.open(frame_path).convert("RGB")
    W, H = im.size
    text = re.sub(r"\s+", " ", text or "").strip().upper()
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
- cover_text: 3 đến 7 từ, thật ngắn và gây tò mò, để in lên ảnh bìa.
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


def fallback_meta(src_title, lines_vi):
    """Không có AI: dịch tiêu đề gốc bằng Google Dịch (nếu được), hashtag theo mẫu chung."""
    raw_tags = re.findall(r"#([^\s#]+)", src_title or "")
    title = re.sub(r"#\S+", "", src_title or "").strip()
    if _has_cjk(title):
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
        "cover_text": " ".join(title.split()[:6]),
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


def render_cover(job_dir, kit):
    """Vẽ lại cover.jpg theo kit['frame'], kit['cover_text'], kit['style']."""
    job_dir = Path(job_dir)
    frames = kit.get("frames") or []
    if not frames:
        return None
    idx = min(max(int(kit.get("frame", 0)), 0), len(frames) - 1)
    make_cover(job_dir / frames[idx]["file"], job_dir / COVER_FILE, kit.get("cover_text", ""), kit.get("style", "dam"))
    kit["frame"], kit["cover"] = idx, COVER_FILE
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
        "style": old.get("style", "dam") if old.get("style") in STYLES else "dam",
    }
    render_cover(job_dir, kit)
    return save_kit(job_dir, kit)


if __name__ == "__main__":
    d = Path(sys.argv[1])
    vid = next(iter(list(d.glob("*.goc.mp4")) + list(d.glob("*.vi.mp4")) + list(d.glob("*.mp4"))), None)
    if not vid:
        sys.exit("Không thấy file video trong thư mục.")
    print(json.dumps(build_kit(d, vid, vid.stem, []), ensure_ascii=False, indent=1))
