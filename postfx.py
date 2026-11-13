"""
postfx.py - Hoàn thiện video sau khi lồng tiếng:
  - bản dọc 9:16 (1080x1920) cho Shorts / Reels / TikTok: nền mờ hoặc cắt giữa
  - gắn logo + chữ ký (ảnh PNG trong suốt vẽ bằng Pillow rồi chồng lên bằng ffmpeg)
"""
import json
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
BRAND_DIR = ROOT / "brand"
LOGO = BRAND_DIR / "logo.png"
POSITIONS = ("tr", "tl", "br", "bl")
VERTICAL = ("off", "blur", "crop")
DEFAULT_BRAND = {"enabled": False, "text": "", "pos": "tr", "opacity": 85, "size": 14, "old": "cover",
                 "intro": False, "outro_on": False, "outro": "Follow kênh để xem thêm video hay"}
# xử lý logo kênh cũ in sẵn trong video: cover = đặt logo của mình đè lên / remove = xoá hẳn (delogo) rồi đặt logo
# của mình ở góc như thường / keep = giữ nguyên
OLD_MODES = ("cover", "remove", "keep")
VW, VH = 1080, 1920
FONTS = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/tahomabd.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def clean_brand(b):
    b = {**DEFAULT_BRAND, **(b or {})}
    b["enabled"] = bool(b.get("enabled"))
    b["intro"], b["outro_on"] = bool(b.get("intro")), bool(b.get("outro_on"))
    b["outro"] = re.sub(r"\s+", " ", str(b.get("outro") or "")).strip()[:60]
    if b.get("old") not in OLD_MODES:
        b["old"] = "cover"
    b.pop("cover_old", None)
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


def detect_watermark(video, corner="tr", n=16):
    """Dò logo kênh cũ in cứng ở một góc video: vùng có đường nét rõ nhưng đứng yên suốt video.
    Trả về {"cx", "cy", "d"} (tâm theo tỉ lệ rộng / cao, đường kính theo tỉ lệ cạnh ngắn) hoặc None."""
    try:
        import cv2
        import numpy as np

        w, h = video_size(video)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(video)], capture_output=True, text=True).stdout.strip() or 0)
        sw = 480
        sh = int(h * sw / w) // 2 * 2
        frames = []
        for i in range(n):
            t = dur * (0.04 + 0.92 * i / (n - 1)) if dur else i
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                                  "-vf", f"scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                 capture_output=True).stdout
            if len(raw) == sw * sh:
                frames.append(np.frombuffer(raw, np.uint8).reshape(sh, sw))
        if len(frames) < 6:
            return None
        # nét có mặt ở >= 70% khung hình rải khắp video = logo in cứng (bắt được cả logo chữ mờ bán trong suốt,
        # vì nền phía sau thay đổi nhưng đường viền chữ vẫn ở yên một chỗ)
        persist = np.mean([cv2.dilate((cv2.Canny(f, 40, 120) > 0).astype(np.uint8), np.ones((3, 3), np.uint8))
                           for f in frames], axis=0)
        edges = persist >= 0.7
        still = np.ones_like(edges)
        # chỉ xét góc đã chọn: 38% chiều rộng, 34% chiều cao
        rx, ry = int(sw * 0.38), int(sh * 0.34)
        x0 = sw - rx if corner[1] == "r" else 0
        y0 = sh - ry if corner[0] == "b" else 0
        m = (edges & still)[y0:y0 + ry, x0:x0 + rx].astype(np.uint8)
        if m.sum() < 15:
            return None
        m = cv2.dilate(m, np.ones((11, 11), np.uint8))  # nối các chữ rời của logo chữ thành một khối
        cnt, lab, stats, _ = cv2.connectedComponentsWithStats(m)
        base = min(sw, sh)
        best = None
        for k in range(1, cnt):
            x, y, bw, bh, area = stats[k]
            if not (base * 0.04 < max(bw, bh) < base * 0.5) or bh > ry * 0.9:
                continue
            ink = int(((lab == k) & (edges & still)[y0:y0 + ry, x0:x0 + rx]).sum())
            if ink > 18 and (best is None or ink > best[0]):
                best = (ink, x, y, bw, bh)
        if not best:
            return None
        _, x, y, bw, bh = best
        # khối đã nở 11 px -> thu lại ~4 px mỗi cạnh để ra khung sát logo
        bw, bh = max(4, bw - 8), max(4, bh - 8)
        return {"cx": round(float((x0 + x + 4 + bw / 2) / sw), 4), "cy": round(float((y0 + y + 4 + bh / 2) / sh), 4),
                "d": round(float(max(bw, bh) * 1.15 / base), 4),
                "w": round(float(bw / sw), 4), "h": round(float(bh / sh), 4)}
    except Exception:
        return None


def spot_rect(w, h, spot, pad=1.25):
    """Khung chữ nhật (x, y, rw, rh) bao logo cũ, nới thêm chút cho chắc, nằm gọn trong khung hình."""
    rw = spot.get("w", spot["d"] * min(w, h) / w) * w * pad
    rh = spot.get("h", spot["d"] * min(w, h) / h) * h * pad
    x = int(min(max(1, spot["cx"] * w - rw / 2), w - 3))
    y = int(min(max(1, spot["cy"] * h - rh / 2), h - 3))
    rw = int(max(2, min(rw, w - 2 - x)))
    rh = int(max(2, min(rh, h - 2 - y)))
    return x, y, rw, rh


def delogo_filter(w, h, spot):
    x, y, rw, rh = spot_rect(w, h, spot)
    return f"delogo=x={x}:y={y}:w={rw}:h={rh}"


def remove_old_logo(im, spot):
    """Ảnh tĩnh (ảnh bìa): xoá logo cũ bằng cách vá ảnh (inpaint) từ vùng xung quanh."""
    try:
        import cv2
        import numpy as np

        arr = np.asarray(im.convert("RGB"))
        x, y, rw, rh = spot_rect(im.width, im.height, spot)
        mask = np.zeros(arr.shape[:2], np.uint8)
        mask[y:y + rh, x:x + rw] = 255
        out = cv2.inpaint(arr, mask, max(3, min(rw, rh) // 6), cv2.INPAINT_TELEA)
        return Image.fromarray(out)
    except Exception:
        return im


def spot_layer(w, h, spot, opacity=100):
    """Logo đặt đè đúng chỗ logo cũ (spot từ detect_watermark)."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if not (spot and LOGO.is_file()):
        return layer
    d = max(16, int(min(w, h) * spot["d"]))
    lg = Image.open(LOGO).convert("RGBA")
    lg = lg.resize((d, max(1, int(lg.height * d / lg.width))), Image.LANCZOS)
    if opacity < 100:
        lg.putalpha(lg.getchannel("A").point(lambda v: v * opacity // 100))
    x = min(max(0, int(w * spot["cx"] - lg.width / 2)), w - lg.width)
    y = min(max(0, int(h * spot["cy"] - lg.height / 2)), h - lg.height)
    layer.alpha_composite(lg, (x, y))
    return layer


def brand_layer(w, h, b, spot=None):
    """Ảnh RGBA đúng cỡ video, có logo + chữ ở góc đã chọn (spot: đặt logo đè lên logo cũ)."""
    b = clean_brand(b)
    spot = spot if LOGO.is_file() else None
    base = min(w, h)
    margin = int(base * 0.035)
    # logo che logo cũ luôn đậm 100% để không lộ logo bên dưới; chữ ký (nếu có) vẫn ở góc như thường
    layer = spot_layer(w, h, spot) if spot else Image.new("RGBA", (w, h), (0, 0, 0, 0))
    parts = []
    if LOGO.is_file() and not spot:
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


def cover_chain(h, band, segs):
    """Đoạn filter làm mờ dải phụ đề Trung in cứng (giống lúc xuất bản ngang), ra nhãn [cv]."""
    y = int(h * band[0]) // 2 * 2
    bh = max(2, (int(h * band[1]) - y) // 2 * 2)
    spans = []
    for s in segs or []:
        a, b = max(0.0, s["start"] - 0.3), s["end"] + 0.3
        if spans and a - spans[-1][1] < 1.0:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    enable = ""
    if spans and len(spans) <= 400:
        enable = ":enable='" + "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in spans) + "'"
    return (f"[0:v]split[base][cut];[cut]crop=iw:{bh}:0:{y},gblur=sigma=30:steps=3,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.72:t=fill[blur];[base][blur]overlay=0:{y}{enable}[cv];")


def make_vertical(src, dst, mode, brand, tmp_dir, log=print, segs=None, audio_src=None, band=None, spot=None, rm=None):
    """src: video làm hình; audio_src: lấy tiếng từ file khác (khi hình là video gốc chưa ghép phụ đề Việt);
    band: dải phụ đề Trung in cứng cần làm mờ; spot: chỗ logo cũ để đặt logo mới đè lên."""
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
    pre, lbl = "", "[0:v]"
    if band:  # làm mờ chữ Trung in cứng trên hình gốc
        pre, lbl = cover_chain(h, band, segs), "[cv]"
    if rm:  # xoá logo cũ trên hình gốc
        pre += f"{lbl}{delogo_filter(w, h, rm)}[dl];"
        lbl = "[dl]"
    if spot and LOGO.is_file():  # logo mới đè lên logo cũ, đặt trên hình gốc trước khi dựng khung dọc
        spot_layer(w, h, spot).save(Path(tmp_dir) / "spot.png")
        inputs += ["-i", "spot.png"]
        pre += f"{lbl}[{len(inputs) // 2 - 1}:v]overlay=0:0[sv];"
        lbl = "[sv]"
    chain = pre + chain.replace("[0:v]", lbl, 1)
    if has_brand(brand) and not spot:
        png = Path(tmp_dir) / "brand_v.png"
        brand_layer(VW, VH, brand).save(png)
        inputs += ["-i", "brand_v.png"]
        chain += f";[v][{len(inputs) // 2 - 1}:v]overlay=0:0[v]"
    amap = "0:a?"
    if audio_src:
        amap = f"{len(inputs) // 2}:a:0"
        inputs += ["-i", str(Path(audio_src).resolve())]
    # chạy trong thư mục tạm để đường dẫn file phụ đề không bị lỗi ký tự trên Windows
    _ffmpeg(inputs + ["-filter_complex", chain, "-map", "[v]", "-map", amap] + ENC + [str(Path(dst).resolve())],
            log, cwd=str(tmp_dir))


def apply_brand(src, dst, brand, tmp_dir, log=print, spot=None, rm=None):
    w, h = video_size(src)
    png = Path(tmp_dir) / "brand_h.png"
    brand_layer(w, h, brand, spot).save(png)
    pre = f"[0:v]{delogo_filter(w, h, rm)}[d];[d]" if rm else "[0:v]"
    _ffmpeg(["-i", str(src), "-i", str(png), "-filter_complex", f"{pre}[1:v]overlay=0:0[v]", "-map", "[v]",
             "-map", "0:a?"] + ENC + [str(dst)], log)


SPOT_FILE = "logo_cu.json"  # chỗ logo cũ đã dò của từng video (dùng lại cho ảnh bìa, lần xuất sau)


def find_spot(job_dir, fallback=None, corner="tr"):
    """Chỗ logo cũ của video trong job (dò trên video gốc, lưu lại để lần sau không phải dò)."""
    job_dir = Path(job_dir)
    f = job_dir / SPOT_FILE
    try:
        saved = json.loads(f.read_text(encoding="utf-8"))
        if saved.get("corner") == corner and (saved.get("spot") is None or "w" in saved["spot"]):
            return saved.get("spot")
    except Exception:
        pass
    video = next(iter(job_dir.glob("*.goc.mp4")), None) or fallback
    if not video:
        return None
    spot = detect_watermark(video, corner)
    f.write_text(json.dumps({"corner": corner, "spot": spot}), encoding="utf-8")
    return spot


def finish(job_dir, video_name, vertical, brand, log=print, vsubs=False, burned=False, wrap=None):
    """Gắn logo vào video chính (thay tại chỗ) và tạo bản dọc <tên>.doc.mp4 (vsubs: kèm phụ đề kiểu TikTok).
    Trả về danh sách việc đã làm."""
    job_dir = Path(job_dir)
    src = job_dir / video_name
    done = []
    tmp = job_dir / "_fx"
    tmp.mkdir(exist_ok=True)
    try:
        spot, rm = None, None  # spot: đặt logo mình đè lên logo cũ; rm: xoá logo cũ
        mode = brand.get("old", "cover") if has_brand(brand) else "keep"
        if mode == "cover" and not LOGO.is_file():
            mode = "keep"  # chỉ có chữ ký, không có ảnh logo để che
        if mode != "keep":
            found = find_spot(job_dir, src, brand.get("pos", "tr"))
            if not found:
                log("Không thấy logo cũ ở góc video -> đặt logo ở góc như thường")
            elif mode == "remove":
                rm = found
                log("Xoá logo cũ ở góc video rồi gắn logo của bạn")
            else:
                spot = found
                log("Đặt logo đè lên logo cũ ở góc video")
        if vertical in ("blur", "crop"):
            log("Tạo bản video dọc 9:16 cho Shorts / Reels / TikTok...")
            dst = job_dir / video_name.replace(".vi.mp4", ".doc.mp4")
            part = tmp / "vertical.mp4"
            segs, vsrc, asrc, band = None, src, None, None
            if vsubs:
                try:
                    data = json.loads((job_dir / "segs.json").read_text(encoding="utf-8"))
                    segs = data["segs"]
                    log("  kèm phụ đề kiểu TikTok (chữ to, sáng dần theo lời đọc)")
                    goc = job_dir / data.get("video", "")
                    if (burned or data.get("burned")) and goc.is_file():
                        # video chính đã ghép phụ đề Việt -> lấy hình từ video gốc để không bị hai lớp chữ
                        vsrc, asrc, band = goc, src, data.get("band")
                except Exception:
                    log("  không có lời thoại đã lưu -> bản dọc không kèm phụ đề")
            make_vertical(vsrc, part, vertical, brand, tmp, log, segs, audio_src=asrc, band=band, spot=spot, rm=rm)
            part.replace(dst)
            done.append("vertical")
        if has_brand(brand) or rm:
            log("Gắn logo / chữ ký vào video...")
            part = tmp / "brand.mp4"
            apply_brand(src, part, brand, tmp, log, spot, rm)
            part.replace(src)
            done.append("brand")
        if wrap:
            log("Thêm đoạn mở đầu có logo / lời nhắc cuối video...")
            apply_wrap(src, wrap, tmp, log)
            vdst = job_dir / video_name.replace(".vi.mp4", ".doc.mp4")
            if "vertical" in done and vdst.is_file():
                apply_wrap(vdst, wrap, tmp, log)
            done.append("wrap")
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


# ------------------------------------------------------------------ mở đầu / kết thúc, cắt nhiều phần
INTRO_S = 2.0  # độ dài đoạn mở đầu có logo
OUTRO_S = 4.0  # lời nhắc cuối video hiện trong mấy giây cuối
PARTS_DIR = "parts"
PARTS_FILE = "parts.json"
SPLIT_MIN = (0, 1, 2, 3, 5, 10)  # phút / phần (0 = không cắt)


def video_fps(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout.strip()
    return out if re.fullmatch(r"\d+(/\d+)?", out or "") and not out.startswith("0") else "30"


def duration(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(video)], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _pill(w, h, text, y_frac, size_frac=0.05, bg=(0, 0, 0, 175), fg=(255, 255, 255, 255), accent=None, x="c"):
    """Lớp RGBA cỡ video có một nhãn chữ bo tròn (x: c giữa / l trái)."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    base = min(w, h)
    size = max(14, int(base * size_frac))
    d = ImageDraw.Draw(layer)
    while True:  # chữ dài thì thu nhỏ cho vừa 90% bề ngang
        font = _font(size)
        x0, y0, x1, y1 = d.textbbox((0, 0), text, font=font)
        if x1 - x0 + size * 1.4 <= w * 0.9 or size <= 14:
            break
        size = int(size * 0.92)
    tw, th = x1 - x0, y1 - y0
    px, py = int(size * 0.7), int(size * 0.45)
    bw, bh = tw + px * 2, th + py * 2
    bx = (w - bw) // 2 if x == "c" else int(base * 0.035)
    by = int(h * y_frac)
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=bg)
    if accent:
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, outline=accent, width=max(2, base // 240))
    d.text((bx + px - x0, by + py - y0), text, font=font, fill=fg)
    return layer


def wrap_cfg(brand):
    """Cấu hình mở đầu / lời nhắc cuối từ cài đặt logo (None nếu tắt cả hai)."""
    b = clean_brand(brand)
    intro = b["intro"] and (LOGO.is_file() or b["text"])
    outro = b["outro_on"] and b["outro"]
    return {"intro": bool(intro), "outro": b["outro"] if outro else "", "text": b["text"]} if (intro or outro) else None


def intro_png(src, w, h, wrap, tmp, name="intro.png"):
    """Ảnh mở đầu: khung hình đầu video làm mờ + logo kênh ở giữa (+ tên kênh)."""
    tmp = Path(tmp)
    frame = tmp / "_first.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(src), "-frames:v", "1", str(frame)],
                   capture_output=True)
    try:
        bg = Image.open(frame).convert("RGB").resize((w, h))
    except Exception:
        bg = Image.new("RGB", (w, h), (20, 22, 34))
    bg = bg.filter(ImageFilter.GaussianBlur(min(w, h) // 25))
    bg = Image.blend(bg, Image.new("RGB", (w, h), (0, 0, 0)), 0.45).convert("RGBA")
    base = min(w, h)
    cy = h // 2
    if LOGO.is_file():
        lw = int(base * 0.42)
        lg = Image.open(LOGO).convert("RGBA")
        lg = lg.resize((lw, int(lg.height * lw / lg.width)), Image.LANCZOS)
        cy = (h - lg.height) // 2 - (int(base * 0.05) if wrap.get("text") else 0)
        bg.alpha_composite(lg, ((w - lg.width) // 2, cy))
        cy += lg.height + int(base * 0.03)
    if wrap.get("text"):
        font = _font(int(base * 0.07))
        d = ImageDraw.Draw(bg)
        x0, y0, x1, y1 = d.textbbox((0, 0), wrap["text"], font=font, stroke_width=4)
        d.text(((w - (x1 - x0)) // 2 - x0, cy - y0), wrap["text"], font=font, fill=(255, 255, 255),
               stroke_width=4, stroke_fill=(0, 0, 0))
    out = tmp / name
    bg.convert("RGB").save(out)
    return out


def render_piece(src, dst, tmp, start=0.0, dur=None, overlays=(), intro=None, log=print):
    """Cắt đoạn [start, start + dur] của src (mã hoá lại nên cắt chính xác), chồng các lớp PNG cỡ khung hình
    (overlays: [(png, t0, t1)] theo thời gian của đoạn), thêm đoạn mở đầu intro (PNG) nếu có."""
    w, h = video_size(src)
    fps = video_fps(src)
    # -ss / -t đặt TRƯỚC -i nên chỉ áp cho video nguồn (đặt sau sẽ bị hiểu là của file ảnh chồng phía sau)
    args = (["-ss", f"{start:.3f}"] if start else []) + (["-t", f"{dur:.3f}"] if dur else [])
    args += ["-i", str(Path(src).resolve())]
    chain = (f"[0:v]setpts=PTS-STARTPTS,fps={fps},format=yuv420p,setsar=1[v0];"
             "[0:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a0]")
    lbl, n = "[v0]", 1
    for png, t0, t1 in overlays:
        args += ["-i", str(Path(png).resolve())]
        chain += f";{lbl}[{n}:v]overlay=0:0:enable='between(t,{t0:.2f},{t1:.2f})'[v{n}]"
        lbl, n = f"[v{n}]", n + 1
    if intro:
        args += ["-loop", "1", "-t", f"{INTRO_S}", "-i", str(Path(intro).resolve()),
                 "-f", "lavfi", "-t", f"{INTRO_S}", "-i", "anullsrc=r=48000:cl=stereo"]
        chain += (f";[{n}:v]scale={w}:{h},fps={fps},format=yuv420p,setsar=1,"
                  f"fade=in:st=0:d=0.35,fade=out:st={INTRO_S - 0.3:.2f}:d=0.3[iv]"
                  f";[iv][{n + 1}:a]{lbl}[a0]concat=n=2:v=1:a=1[vo][ao]")
    else:
        chain += f";{lbl}null[vo];[a0]anull[ao]"
    _ffmpeg(args + ["-filter_complex", chain, "-map", "[vo]", "-map", "[ao]", "-c:v", "libx264", "-preset",
                    "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(Path(dst).resolve())], log)


def apply_wrap(src, wrap, tmp, log=print):
    """Thêm mở đầu có logo + lời nhắc cuối vào cả video src (thay tại chỗ)."""
    if not wrap:
        return
    w, h = video_size(src)
    total = duration(src)
    tall = h > w * 1.4
    ov = []
    if wrap.get("outro") and total > OUTRO_S + 2:
        png = Path(tmp) / f"outro_{w}x{h}.png"
        _pill(w, h, wrap["outro"].upper(), 0.15 if tall else 0.06, 0.045 if tall else 0.05,
              accent=(255, 214, 10)).save(png)
        ov.append((png, total - OUTRO_S, total + 1))
    intro = intro_png(src, w, h, wrap, tmp, f"intro_{w}x{h}.png") if wrap.get("intro") else None
    if not ov and not intro:
        return
    part = Path(tmp) / "wrapped.mp4"
    render_piece(src, part, tmp, overlays=ov, intro=intro, log=log)
    part.replace(src)


def cut_points(total, minutes, segs):
    """Chia [0, total] thành các phần ~minutes phút, cắt ở khoảng lặng giữa hai câu thoại gần mốc nhất."""
    import math

    L = minutes * 60
    if not minutes or total <= L * 1.15:
        return [(0.0, total)]
    n = math.ceil(total / L)
    target = total / n
    segs = sorted(segs or [], key=lambda x: x["start"])
    # end0 = lúc thật sự hết lời (end của phụ đề đã được kéo dài tới sát câu sau)
    end = [min(s.get("end0", s["end"]), s["end"]) for s in segs]
    # ranh giới giữa hai câu: (thời điểm cắt, độ dài khoảng lặng, câu trước có kết thúc hẳn bằng . ? ! không)
    gaps = [((end[i] + segs[i + 1]["start"]) / 2, max(0.0, segs[i + 1]["start"] - end[i]),
             bool(re.search(r"[.?!…]\s*$", segs[i].get("vi") or ""))) for i in range(len(segs) - 1)]
    cuts = []
    for k in range(1, n):
        t = k * target
        cand = [c for c in gaps if abs(c[0] - t) <= target * 0.2]
        # gần mốc nhất; ưu tiên khoảng lặng dài (chuyển cảnh) và chỗ hết hẳn một câu
        best = min(cand, key=lambda c: abs(c[0] - t) - 4 * min(c[1], 3) - (3 if c[2] else 0))[0] if cand else t
        cuts.append(round(best, 2))
    edges = [0.0] + cuts + [total]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def split_parts(job_dir, video_name, minutes, log=print, wrap=None, label="PHẦN"):
    """Cắt video chính (và bản dọc nếu có) thành nhiều phần cho TikTok: mỗi phần có nhãn "PHẦN k/n" vài giây đầu,
    lời nhắc "xem tiếp phần sau" ở cuối, mở đầu có logo (nếu bật). Ghi parts/parts.json."""
    import shutil

    job_dir = Path(job_dir)
    pdir = job_dir / PARTS_DIR
    shutil.rmtree(pdir, ignore_errors=True)
    if not minutes:
        return []
    src = job_dir / video_name
    total = duration(src)
    try:
        segs = json.loads((job_dir / "segs.json").read_text(encoding="utf-8"))["segs"]
    except Exception:
        segs = []
    spans = cut_points(total, minutes, segs)
    if len(spans) < 2:
        log(f"Video {total / 60:.1f} phút, không cần cắt thành nhiều phần ({minutes} phút / phần)")
        return []
    n = len(spans)
    log(f"Cắt thành {n} phần (~{minutes} phút / phần) cho TikTok...")
    pdir.mkdir()
    tmp = pdir / "_tmp"
    tmp.mkdir()
    vert = job_dir / video_name.replace(".vi.mp4", ".doc.mp4")
    sources = [("video", src)] + ([("vertical", vert)] if vert.is_file() else [])
    parts = []
    try:
        intro = {}
        for k, (a, b) in enumerate(spans, 1):
            item = {"k": k, "n": n, "start": round(a, 2), "end": round(b, 2)}
            for kind, video in sources:
                w, h = video_size(video)
                tall = h > w * 1.4
                badge = tmp / f"badge_{k}_{kind}.png"
                _pill(w, h, f"{label} {k}/{n}", 0.12 if tall else 0.035, 0.05 if tall else 0.055,
                      bg=(235, 36, 54, 230), x="l").save(badge)
                ov = [(badge, 0, 4)]
                nxt = f"XEM TIẾP {label} {k + 1} Ở VIDEO SAU" if k < n else ((wrap or {}).get("outro") or "").upper()
                if nxt:
                    end_png = tmp / f"end_{k}_{kind}.png"
                    _pill(w, h, nxt, 0.15 if tall else 0.06, 0.045 if tall else 0.05,
                          accent=(255, 214, 10)).save(end_png)
                    ov.append((end_png, max(0, (b - a) - OUTRO_S), (b - a) + 1))
                if wrap and wrap.get("intro") and kind not in intro:
                    intro[kind] = intro_png(video, w, h, wrap, tmp, f"intro_{kind}.png")
                out = pdir / f"phan{k:02}{'_doc' if kind == 'vertical' else ''}.mp4"
                render_piece(video, out, tmp, a, b - a, ov, intro.get(kind), log)
                item[kind] = f"{PARTS_DIR}/{out.name}"
            parts.append(item)
            log(f"  xong phần {k}/{n}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    (pdir / PARTS_FILE).write_text(json.dumps(parts, ensure_ascii=False, indent=1), encoding="utf-8")
    return parts


def load_parts(job_dir):
    try:
        return json.loads((Path(job_dir) / PARTS_DIR / PARTS_FILE).read_text(encoding="utf-8"))
    except Exception:
        return []
