#!/usr/bin/env python3
"""
server.py - Giao diện web cho douyin_dub.py

Chạy:  python server.py   rồi mở http://localhost:5577
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Request, abort, jsonify, request, send_from_directory

import notify
import vieneu_tts
import postfx
import publish_kit
import social
import storage

storage.apply_env()

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
JOBS_DIR = storage.jobs_dir()  # đổi được trên web (Cài đặt -> Lưu trữ)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PORT", 5577))

class BigUploadRequest(Request):
    """File tải lên ghi tạm vào thư mục dữ liệu (ổ D...) thay vì thư mục TEMP của Windows trên ổ C (hay đầy)."""

    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        import tempfile

        return tempfile.TemporaryFile("wb+", dir=str(JOBS_DIR))


app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.request_class = BigUploadRequest
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 ** 3  # file tải lên tối đa 20 GB (phim dài)
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v", ".wmv", ".mpg", ".mpeg")
app.register_blueprint(social.bp)

jobs = {}  # id -> dict
lock = threading.Lock()
work_q = queue.Queue()
procs = {}  # id -> Popen

STEPS = [
    ("download", ("Đang tải", "Dùng file video có sẵn")),
    ("transcribe", ("Nhận dạng giọng nói",)),
    ("translate", ("Đang dịch", "Dịch bằng AI", "Khởi động model dịch", "Tải model dịch", "Tải bộ chạy llama")),
    ("dub", ("Lồng tiếng", "Lồng lại")),
    ("separate", ("Tách giọng Trung", "Dùng lại nhạc nền")),
    ("render", ("Đang xuất video",)),
    ("finish", ("Tạo bản video dọc", "Gắn logo")),
]
MAX_BATCH = 50
GEMINI_PICK = ("Aoede", "Kore", "Zephyr", "Leda", "Despina", "Charon", "Puck", "Orus", "Fenrir", "Algenib")
PUBLIC = ("id", "source", "title", "status", "step", "created", "finished", "options", "files", "error", "progress",
          "queue_pos", "kit", "posts", "watch", "redub", "rerender", "edited")


def save(job):
    d = JOBS_DIR / job["id"]
    d.mkdir(exist_ok=True)
    data = {k: job.get(k) for k in PUBLIC if k != "queue_pos"}
    data["log"] = job.get("log", [])[-400:]
    data["auto_post"] = job.get("auto_post")
    (d / "job.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_jobs():
    for d in JOBS_DIR.iterdir():
        f = d / "job.json"
        if f.is_file():
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if j.get("status") in ("queued", "running"):
                j["status"], j["error"] = "error", "Server đã tắt khi đang xử lý. Hãy chạy lại."
            if j.get("kit") == "running":
                j["kit"] = ""
            if (j.get("redub") or j.get("rerender")) and j.get("status") == "error":  # đang làm tiếp thì tắt -> video cũ vẫn còn
                j["redub"] = j["rerender"] = False
                if find_files(d).get("video"):
                    j["status"], j["error"] = "done", ""
            for post in j.get("posts") or []:  # bài hẹn giờ ("scheduled") giữ nguyên, tới giờ sẽ tự đăng
                if post.get("status") in ("queued", "running"):
                    post.update(status="error",
                                error="Server đã tắt khi đang đăng. Hãy kiểm tra trên nền tảng trước khi đăng lại.")
            jobs[j["id"]] = j


def find_files(job_dir):
    out = {}
    for p in job_dir.iterdir():
        n = p.name
        if n.endswith(".vi.mp4"):
            out["video"] = n
        elif n.endswith(".doc.mp4"):
            out["vertical"] = n
        elif n.endswith(".goc.mp4"):
            out["original"] = n
        elif n.endswith(".vi.srt"):
            out["srt_vi"] = n
        elif n.endswith(".zh.srt"):
            out["srt_zh"] = n
        elif n == publish_kit.COVER_FILE:
            out["cover"] = n
        elif n == publish_kit.TIKTOK_COVER:
            out["cover_tiktok"] = n
        elif n == "segs.json":
            out["segs"] = n
    return out


def job_title(j):
    return re.sub(r"#\S+", "", j.get("title") or "").strip() or j.get("source") or j["id"]


def parse_srt(path):
    if not path.is_file():
        return []
    blocks = re.split(r"\r?\n\r?\n", path.read_text(encoding="utf-8").strip())
    res = []
    for b in blocks:
        lines = b.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            res.append({"time": lines[1].split(" --> ")[0][:8], "text": " ".join(lines[2:])})
    return res


def run_job(job):
    jid = job["id"]
    jdir = JOBS_DIR / jid
    opt = job["options"]
    cmd = [sys.executable, "-u", str(ROOT / "douyin_dub.py"), job.get("source_arg") or job["source"], "-o", str(jdir),
           "--voice", opt["voice"], "--model", opt["model"], "--bg-volume", str(opt["bg_volume"]),
           "--font-size", str(opt["font_size"]), "--tts", opt.get("tts", "edge")]
    if opt.get("tts") == "gemini" and opt.get("gemini_voice"):
        cmd += ["--gemini-voice", opt["gemini_voice"]]
    track = pick_music(opt.get("music")) if opt.get("dub") else None
    if track:
        cmd += ["--music", str(track)]
    if opt.get("tts") == "vieneu" and opt.get("vieneu_voice"):
        cmd += ["--vieneu-voice", opt["vieneu_voice"]]
    if opt.get("tts") == "eleven":
        cmd += ["--eleven-model", opt.get("eleven_model", "v3")]
        if opt.get("eleven_voice"):
            cmd += ["--eleven-voice", opt["eleven_voice"]]
    if not opt["dub"]:
        cmd.append("--no-dub")
    if opt["burn"]:
        cmd.append("--subs")
    if not opt.get("cover", True):
        cmd += ["--cover-subs", "off"]
    if opt.get("separate"):
        cmd.append("--separate")
    if opt.get("translator", "auto") != "auto":
        cmd += ["--translator", opt["translator"]]
    rerender = bool(job.get("rerender"))
    redub = bool(job.get("redub")) or rerender  # cả hai đều là "làm tiếp" từ lời thoại đã lưu
    started = time.time()
    if rerender:
        cmd.append("--rerender")
    elif redub:
        cmd.append("--redub")
    elif job.get("resume"):
        cmd.append("--resume")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    if opt.get("music") and opt.get("dub"):
        job["log"].append(f"Nhạc nền: {track.parent.name}/{track.name}" if track else
                          "Chưa có nhạc nào trong thư viện (Cài đặt -> Thư viện nhạc nền) -> giữ nền gốc")
    with lock:
        job.update(status="running", step="render" if rerender else "dub" if redub else "download",
                   error="", progress="")
    save(job)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", env=env, cwd=str(ROOT),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    procs[jid] = p
    last_err = ""
    for raw in p.stdout:
        line = raw.rstrip()
        if not line.strip() or "huggingface" in line.lower() or "warnings.warn" in line or "HF_TOKEN" in line \
                or "symlink" in line.lower() or "developer mode" in line.lower():
            continue
        with lock:
            m = re.match(r"\s*(\d+)/(\d+)\s*$", line)
            if m:  # tiến độ lồng tiếng
                job["progress"] = f"{m.group(1)}/{m.group(2)}"
                continue
            job["log"].append(line)
            if len(job["log"]) > 2000:
                job["log"] = job["log"][-1500:]
            for key, marks in STEPS:
                if any(mk in line for mk in marks):
                    job["step"], job["progress"] = key, ""
            if "Tiêu đề:" in line:
                job["title"] = line.split("Tiêu đề:", 1)[1].strip()
            # lỗi Python ("RuntimeError: ...") hoặc thông báo sys.exit() tiếng Việt
            if re.match(r"^\w+(Error|Exception):", line) or (
                    not line.startswith((" ", "[douyin-dub]", "Traceback")) and line.startswith(("Không", "Chưa"))):
                last_err = line
    code = p.wait()
    procs.pop(jid, None)
    for t in jdir.glob("_tmp_*"):  # dọn thư mục tạm nếu bị huỷ / lỗi giữa chừng
        shutil.rmtree(t, ignore_errors=True)
    video = find_files(jdir).get("video")
    brand = postfx.clean_brand(read_config().get("brand")) if opt.get("brand") else None
    wrap = postfx.wrap_cfg(brand) if brand else None  # mở đầu có logo / lời nhắc cuối (theo cài đặt logo)
    split = int(opt.get("split") or 0)
    if code == 0 and video and job.get("status") != "cancelled":
        def flog(msg):
            with lock:
                job["log"].append(msg)
                job["step"], job["progress"] = "finish", ""

        try:
            if opt.get("vertical", "off") != "off" or postfx.has_brand(brand) or (wrap and not split):
                # cắt nhiều phần thì mở đầu / kết thúc gắn vào từng phần, không gắn vào video chính
                postfx.finish(jdir, video, opt.get("vertical", "off"), brand, flog, vsubs=opt.get("vsubs", False),
                              burned=bool(opt.get("burn")), wrap=None if split else wrap)
            postfx.split_parts(jdir, video, split, flog, wrap=wrap)
        except Exception as e:  # lỗi hoàn thiện không làm hỏng video đã lồng tiếng
            flog(f"Không hoàn thiện được video: {str(e)[:300]}")
        kit = publish_kit.load_kit(jdir) if redub else None
        if kit and kit.get("frames"):  # làm tiếp: vẽ lại bìa (logo mới, bìa từng phần...)
            try:
                publish_kit.render_cover(jdir, kit)
                kit["rev"] = int(kit.get("rev", 0)) + 1
                publish_kit.save_kit(jdir, kit)
            except Exception as e:
                flog(f"Không vẽ lại được ảnh bìa: {str(e)[:200]}")
    with lock:
        job["finished"] = time.time()
        job["files"] = find_files(jdir)
        job["redub"] = job["rerender"] = job["resume"] = False
        if job.get("status") == "cancelled":
            if redub and job["files"].get("video"):  # huỷ giữa lúc làm tiếp -> video cũ vẫn còn, dùng tiếp được
                job.update(status="done", step="done", error="")
                job["log"].append("Đã huỷ làm tiếp, giữ video trước đó.")
                code = -1  # không báo "xong" qua Telegram
        elif code == 0 and job["files"].get("video"):
            job["status"], job["step"] = "done", "done"
            job["edited"] = False
            if not job.get("title"):
                job["title"] = job["files"]["video"].rsplit(".vi.mp4", 1)[0]
        else:
            job["status"] = "error"
            msg = last_err or next((l for l in reversed(job["log"]) if l.strip()), "Lỗi không rõ")
            job["error"] = re.sub(r"^\w+Error:\s*", "", msg)[:400]
    save(job)
    mins = (job["finished"] - started) / 60
    if job["status"] == "done" and code != -1:
        notify.send("job_done", f"✅ {'Đã xuất lại' if rerender else 'Đã lồng lại' if redub else 'Xong video'}: {job_title(job)}\n"
                                f"(mất {mins:.0f} phút) - xem ở http://localhost:{PORT}")
    elif job["status"] == "error":
        notify.send("job_error", f"❌ Lỗi video: {job_title(job)}\n{job.get('error', '')[:300]}")
    if job["status"] == "done" and opt.get("kit", True) and not redub:
        start_kit(job)
    if job["status"] == "done" and job.get("auto_post") and not redub:
        threading.Thread(target=auto_post, args=(job,), daemon=True).start()


def start_kit(job, keep_frames=False):
    """Tạo ảnh bìa + tiêu đề + hashtag ở luồng riêng để không chặn hàng đợi video."""
    with lock:
        if job.get("kit") == "running":
            return False
        job["kit"] = "running"
    threading.Thread(target=run_kit, args=(job, keep_frames), daemon=True).start()
    return True


def run_kit(job, keep_frames=False):
    jdir = JOBS_DIR / job["id"]
    try:
        import douyin_dub

        f = find_files(jdir)
        video = f.get("original") or f.get("video")
        if not video:
            raise RuntimeError("Không thấy file video của job này.")
        lines = [l["text"] for l in parse_srt(jdir / f.get("srt_vi", "_"))]
        publish_kit.build_kit(jdir, jdir / video, job.get("title") or "", lines, douyin_dub.load_ai_config(),
                              douyin_dub.ai_chat, douyin_dub._parse_json, keep_frames=keep_frames)
        state = "done"
    except Exception as e:
        state = "error"
        publish_kit.save_kit(jdir, {**(publish_kit.load_kit(jdir) or {}), "error": str(e)[:300]})
    with lock:
        job["kit"] = state
        job["files"] = find_files(jdir)
    save(job)


def worker():
    while True:
        jid = work_q.get()
        job = jobs.get(jid)
        if job and job["status"] == "queued":
            try:
                run_job(job)
            except Exception as e:  # không để worker chết
                with lock:
                    job.update(status="error", error=str(e), finished=time.time())
                save(job)
        work_q.task_done()


def public(job, full=False):
    d = {k: job.get(k) for k in PUBLIC}
    if job["status"] == "queued":
        waiting = [j for j in jobs.values() if j["status"] == "queued" and j["created"] < job["created"]]
        d["queue_pos"] = len(waiting) + 1
    if full:
        d["log"] = job.get("log", [])[-300:]
        jdir = JOBS_DIR / job["id"]
        f = job.get("files") or {}
        if f.get("srt_vi"):
            vi, zh = parse_srt(jdir / f["srt_vi"]), parse_srt(jdir / f.get("srt_zh", "_"))
            d["lines"] = [{"time": v["time"], "vi": v["text"], "zh": zh[i]["text"] if i < len(zh) else ""}
                          for i, v in enumerate(vi)]
        d["kit_data"] = publish_kit.load_kit(jdir)
        d["parts"] = postfx.load_parts(jdir)
    return d


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/jobs")
def list_jobs():
    with lock:
        items = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
        return jsonify([public(j) for j in items])


@app.get("/api/jobs/<jid>")
def get_job(jid):
    with lock:
        job = jobs.get(jid) or abort(404)
        return jsonify(public(job, full=True))


@app.post("/api/jobs")
def create_job():
    f = request.form
    link = (f.get("link") or "").strip()
    upload = request.files.get("file")
    if not link and not (upload and upload.filename):
        return jsonify(error="Hãy dán link Douyin hoặc chọn một file video."), 400
    # dán đường dẫn file trên máy (vd D:\phim\abc.mp4) -> dùng thẳng file đó, khỏi tải lên / sao chép (phim dài)
    paths = []
    for ln in link.splitlines():
        ln = ln.strip().strip('"').strip("'")
        if ln and not re.match(r"https?://", ln) and Path(ln).suffix.lower() in VIDEO_EXT:
            if not Path(ln).is_file():
                return jsonify(error=f"Không thấy file: {ln}"), 400
            paths.append(str(Path(ln).resolve()))
    if link and not paths and not re.search(r"https?://", link):
        return jsonify(error="Không thấy link hay đường dẫn file video trong nội dung bạn dán."), 400
    links = list(paths)
    if not (upload and upload.filename):
        # dán nhiều link (mỗi dòng một link / nhiều đoạn chia sẻ) -> mỗi link một video trong hàng đợi
        for u in re.findall(r"https?://[^\s，。“”\"'<>]+", link):
            u = u.rstrip(".,;)")
            if u not in links:
                links.append(u)
        if len(links) > MAX_BATCH:
            return jsonify(error=f"Tối đa {MAX_BATCH} link mỗi lần (bạn dán {len(links)})."), 400

    options = parse_options(f)

    def new_job(n=0):
        if upload and upload.filename:
            jid = new_id()
            (JOBS_DIR / jid).mkdir(parents=True)
            ext = Path(upload.filename).suffix.lower() or ".mp4"
            src_path = JOBS_DIR / jid / f"upload{ext}"
            upload.save(src_path)
            return make_job(str(src_path), upload.filename, Path(upload.filename).stem, options, jid=jid)
        if links[n] in paths:
            return make_job(links[n], Path(links[n]).name, Path(links[n]).stem, options, n)
        return make_job(links[n], links[n], "", options, n)  # chỉ giữ phần link trong đoạn chia sẻ để hiển thị gọn

    made = [new_job(n) for n in range(len(links) or 1)]
    res = public(made[0])
    res["batch"] = [j["id"] for j in made]
    return jsonify(res), 201


def new_id():
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def make_job(source_arg, source, title, options, n=0, jid=None, **extra):
    jid = jid or new_id()
    (JOBS_DIR / jid).mkdir(parents=True, exist_ok=True)
    job = {"id": jid, "source": source, "source_arg": source_arg, "title": title, "status": "queued",
           "step": "", "progress": "", "created": time.time() + n * 1e-3, "finished": None,
           "options": dict(options), "files": {}, "error": "", "log": [], "kit": "", "posts": [], **extra}
    with lock:
        jobs[jid] = job
    save(job)
    work_q.put(jid)
    return job


def parse_options(f):
    """Tuỳ chọn xử lý video từ form (hoặc dict đã lưu của kênh theo dõi)."""
    def num(name, default, lo, hi, cast=float):
        try:
            return min(max(cast(f.get(name, default)), lo), hi)
        except (TypeError, ValueError):
            return default

    def on(name, default):
        v = f.get(name, default)
        return v in ("1", 1, True, "true")

    model = f.get("model", "medium")
    return {
        "voice": f.get("voice") if f.get("voice") in ("auto", "nam", "nu") else "auto",
        "tts": f.get("tts") if f.get("tts") in ("gemini", "eleven", "piper", "mix", "edgeplus", "vieneu", "edge") else "vieneu",
        "vieneu_voice": f.get("vieneu_voice") if f.get("vieneu_voice") in vieneu_tts.ALL else "",
        "translator": f.get("translator") if f.get("translator") in ("google", "hunyuan") else "auto",
        "eleven_voice": f.get("eleven_voice", "") if re.fullmatch(r"[A-Za-z0-9]{8,40}", f.get("eleven_voice", "")) else "",
        "eleven_model": "flash" if f.get("eleven_model") == "flash" else "v3",
        "gemini_voice": f.get("gemini_voice") if f.get("gemini_voice") in GEMINI_PICK else "",
        "model": model if model in ("tiny", "base", "small", "medium", "large-v3") else "medium",
        "dub": on("dub", "1"),
        "burn": on("burn", "0"),
        "cover": on("cover", "1"),
        "bg_volume": num("bg_volume", 0.15, 0, 1),
        "font_size": num("font_size", 13, 8, 30, int),
        "kit": on("kit", "1"),
        "vertical": f.get("vertical") if f.get("vertical") in postfx.VERTICAL else "off",
        "brand": on("brand", "0"),
        "separate": on("separate", "0"),
        "vsubs": on("vsubs", "0"),
        "split": int(num("split", 0, 0, 10, int)) if int(num("split", 0, 0, 10, int)) in postfx.SPLIT_MIN else 0,
        "music": f.get("music") if f.get("music") in ("random", *MUSIC_MOODS) else "",
    }


@app.post("/api/jobs/<jid>/cancel")
def cancel_job(jid):
    with lock:
        job = jobs.get(jid) or abort(404)
        if job["status"] not in ("queued", "running"):
            return jsonify(public(job))
        was_queued = job["status"] == "queued"
        job.update(status="cancelled", error="Đã huỷ", finished=time.time())
        if was_queued and (job.get("redub") or job.get("rerender")) and find_files(JOBS_DIR / jid).get("video"):
            # huỷ việc làm tiếp chưa kịp chạy -> video cũ vẫn nguyên
            job.update(status="done", step="done", error="", redub=False, rerender=False)
    p = procs.get(jid)
    if p:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True) if os.name == "nt" else p.kill()
    save(job)
    return jsonify(public(job))


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    with lock:
        job = jobs.get(jid) or abort(404)
        if job["status"] == "running":
            return jsonify(error="Đang xử lý, hãy huỷ trước."), 409
        jobs.pop(jid)
    shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/open-folder")
def open_folder(jid):
    if jid not in jobs:
        abort(404)
    if os.name == "nt":
        os.startfile(str(JOBS_DIR / jid))
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/kit")
def make_kit(jid):
    """Tạo (lại) bộ đăng bài. body: {keep_frames: bool}"""
    job = jobs.get(jid) or abort(404)
    if job["status"] != "done":
        return jsonify(error="Video chưa xử lý xong."), 409
    body = request.get_json(silent=True) or {}
    start_kit(job, keep_frames=bool(body.get("keep_frames")))
    return jsonify(public(job)), 202


@app.put("/api/jobs/<jid>/kit")
def edit_kit(jid):
    """Lưu chỉnh sửa tiêu đề / mô tả / hashtag; đổi khung hình, chữ hoặc kiểu ảnh bìa thì vẽ lại cover.jpg."""
    job = jobs.get(jid) or abort(404)
    jdir = JOBS_DIR / jid
    kit = publish_kit.load_kit(jdir)
    if not kit or not kit.get("frames"):
        return jsonify(error="Chưa có bộ đăng bài. Hãy bấm Tạo."), 409
    body = request.get_json(force=True) or {}
    before = (kit.get("frame"), kit.get("cover_text"), kit.get("style"), kit.get("ratio"), kit.get("badge"), kit.get("logo", True))
    if "title" in body:
        kit["title"] = str(body["title"]).strip()[:200]
    if "description" in body:
        kit["description"] = str(body["description"]).strip()[:2000]
    if "hashtags" in body:
        kit["hashtags"] = publish_kit._tags(re.split(r"[\s,#]+", str(body["hashtags"])))[:30]
    if "cover_text" in body:
        kit["cover_text"] = str(body["cover_text"]).strip()[:120]
    if body.get("style") in publish_kit.STYLES:
        kit["style"] = body["style"]
    if body.get("ratio") in publish_kit.COVER_RATIOS:
        kit["ratio"] = body["ratio"]
    if "logo" in body:
        kit["logo"] = bool(body["logo"])
    if "badge" in body:
        kit["badge"] = re.sub(r"\s+", " ", str(body["badge"] or "")).strip()[:30]
    if "frame" in body:
        try:
            kit["frame"] = min(max(int(body["frame"]), 0), len(kit["frames"]) - 1)
        except (TypeError, ValueError):
            pass
    if before != (kit.get("frame"), kit.get("cover_text"), kit.get("style"), kit.get("ratio"), kit.get("badge"), kit.get("logo", True)):
        publish_kit.render_cover(jdir, kit)
        kit["rev"] = int(kit.get("rev", 0)) + 1
    publish_kit.save_kit(jdir, kit)
    with lock:
        job["files"] = find_files(jdir)
    return jsonify(kit)


@app.post("/api/jobs/<jid>/publish")
def publish_job(jid):
    """Đăng video của job lên các tài khoản đã liên kết.
    body: {account_ids: [...], privacy: private|public, variant: video|vertical, at: unix giây (hẹn giờ, tuỳ chọn)}"""
    job = jobs.get(jid) or abort(404)
    files = find_files(JOBS_DIR / jid)
    if job["status"] != "done" or not files.get("video"):
        return jsonify(error="Video chưa xử lý xong."), 409
    body = request.get_json(force=True) or {}
    privacy = body.get("privacy") if body.get("privacy") in social.PRIVACY else "private"
    variant = "vertical" if body.get("variant") == "vertical" and files.get("vertical") else "video"
    parts = postfx.load_parts(JOBS_DIR / jid) if body.get("parts") else []
    try:
        gap_h = min(max(float(body.get("gap_h") or 0), 0), 72)
    except (TypeError, ValueError):
        gap_h = 0
    accounts = [a for a in (social.account_brief(str(i)) for i in (body.get("account_ids") or [])[:20]) if a]
    if not accounts:
        return jsonify(error="Hãy chọn ít nhất một tài khoản."), 400
    at = None
    if body.get("at"):
        try:
            at = float(body["at"])
        except (TypeError, ValueError):
            return jsonify(error="Giờ hẹn không hợp lệ."), 400
        if at < time.time() + 30:
            return jsonify(error="Giờ hẹn phải ở tương lai (ít nhất 1 phút nữa)."), 400
        if at > time.time() + 60 * 86400:
            return jsonify(error="Chỉ hẹn được trong vòng 60 ngày."), 400
    new = []
    with lock:
        posts = job.setdefault("posts", [])
        busy = {p["account_id"] for p in posts if p["status"] in ("queued", "running")}
        for a in accounts:
            if a["id"] in busy and not at and not parts:
                continue
            for part in parts or [None]:
                k = part["k"] if part else 0
                # phần sau đăng cách phần trước gap_h giờ (phần 1: ngay / giờ hẹn)
                when = (at or time.time()) + (k - 1) * gap_h * 3600 if part and k > 1 else at
                if part and k > 1 and not gap_h:
                    when = at  # không giãn cách -> đăng nối tiếp ngay sau phần trước
                post = {"id": uuid.uuid4().hex[:8], "account_id": a["id"], "account": a["name"],
                        "platform": a["platform"], "privacy": privacy, "variant": variant,
                        "status": "scheduled" if when else "queued", "at": when, "part": k or None,
                        "parts": len(parts) or None, "progress": 0, "url": "", "note": "", "error": "",
                        "created": time.time()}
                posts.append(post)
                new.append(post)
        keep = [p for p in posts if p["status"] == "scheduled"]  # không xoá bài đang hẹn khi danh sách dài
        rest = [p for p in posts if p["status"] != "scheduled"][-60:]
        posts[:] = [p for p in posts if p in keep or p in rest]
    save(job)
    now = [p for p in new if p["status"] == "queued"]
    if now:
        threading.Thread(target=lambda: [run_post(job, p) for p in now], daemon=True).start()
    return jsonify(public(job)), 202


def run_post(job, post):
    jdir = JOBS_DIR / job["id"]
    files = find_files(jdir)
    kit = publish_kit.load_kit(jdir) or {}
    title = (kit.get("title") or re.sub(r"#\S+", "", job.get("title") or "").strip() or "Video")[:200]
    frames, fi = kit.get("frames") or [], kit.get("frame", 0)
    cover_ms = int(frames[fi]["t"] * 1000) if isinstance(fi, int) and 0 <= fi < len(frames) else None
    video = files.get(post.get("variant") or "video") or files.get("video")
    cover = jdir / files["cover"] if files.get("cover") else None
    if post.get("part"):
        part = next((x for x in postfx.load_parts(jdir) if x["k"] == post["part"]), None)
        video = (part or {}).get("vertical" if post.get("variant") == "vertical" else "video") or (part or {}).get("video")
        pc = jdir / publish_kit.part_cover_name(post["part"], post.get("variant") == "vertical")
        cover = pc if pc.is_file() else cover
        title = f"{title} | Phần {post['part']}/{post.get('parts') or (part or {}).get('n', '')}"[:200]
        cover_ms = None
    post["status"] = "running"
    save(job)

    def tick(pct):
        post["progress"] = pct

    try:
        if not video:
            raise RuntimeError("Không còn file video để đăng.")
        res = social.publish(post["account_id"], jdir / video, title, kit.get("description", ""),
                             kit.get("hashtags") or [], cover, cover_ms, post.get("privacy", "private"), tick)
        late = post.get("at") and time.time() - post["at"] > 600
        note = res.get("note", "")
        if late:
            note = (note + " " if note else "") + "Đăng trễ so với giờ hẹn vì lúc đó tool không chạy."
        post.update(status="done", progress=100, url=res.get("url", ""), note=note, ext_id=res.get("id", ""),
                    title=title)
        notify.send("post_done", f"📤 Đã đăng lên {post.get('platform')} ({post.get('account')}): {title}\n"
                                 f"{post.get('url') or ''}")
    except Exception as e:
        post.update(status="error", error=str(e)[:400])
        notify.send("post_error", f"⚠️ Đăng lên {post.get('platform')} ({post.get('account')}) lỗi: {title}\n"
                                  f"{post['error'][:300]}")
    post["finished"] = time.time()
    save(job)


def scheduler():
    """Mỗi 20 giây xem có bài hẹn giờ nào tới lượt thì đăng (lần lượt, không đăng song song)."""
    while True:
        time.sleep(20)
        now = time.time()
        with lock:
            due = [(j, p) for j in jobs.values() for p in (j.get("posts") or [])
                   if p.get("status") == "scheduled" and (p.get("at") or 0) <= now]
            for _, p in due:
                p["status"] = "queued"
        for j, p in sorted(due, key=lambda x: x[1]["at"]):
            try:
                run_post(j, p)
            except Exception as e:
                p.update(status="error", error=str(e)[:400])
                save(j)


@app.delete("/api/jobs/<jid>/posts/<pid>")
def cancel_post(jid, pid):
    """Huỷ một bài đang hẹn giờ."""
    job = jobs.get(jid) or abort(404)
    with lock:
        post = next((p for p in job.get("posts") or [] if p["id"] == pid), None) or abort(404)
        if post["status"] != "scheduled":
            return jsonify(error="Bài này không còn ở trạng thái hẹn giờ."), 409
        job["posts"].remove(post)
    save(job)
    return jsonify(public(job))


@app.get("/api/schedule")
def list_schedule():
    """Tất cả bài đang hẹn giờ (mọi video), sớm nhất trước."""
    with lock:
        items = [{**p, "job_id": j["id"], "job_title": re.sub(r"#\S+", "", j.get("title") or "").strip() or j["source"]}
                 for j in jobs.values() for p in (j.get("posts") or []) if p.get("status") == "scheduled"]
    return jsonify(sorted(items, key=lambda p: p["at"]))


CONFIG_PATH = ROOT / "config.json"
AI_PROVIDERS = ("gemini", "claude", "openai", "deepseek")


def read_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def public_config(cfg):
    key = cfg.get("ai_key", "")
    ek = cfg.get("eleven_key", "")
    return {"provider": cfg.get("ai_provider", "gemini"), "model": cfg.get("ai_model", ""),
            "has_key": len(key) > 10, "key_hint": (key[:4] + "••••" + key[-4:]) if len(key) > 10 else "",
            "eleven_has_key": len(ek) > 10, "eleven_hint": (ek[:4] + "••••" + ek[-4:]) if len(ek) > 10 else ""}


@app.get("/api/config")
def get_config():
    return jsonify(public_config(read_config()))


@app.post("/api/config")
def set_config():
    body = request.get_json(force=True) or {}
    cfg = read_config()
    if body.get("provider") in AI_PROVIDERS:
        cfg["ai_provider"] = body["provider"]
    if "model" in body:
        cfg["ai_model"] = str(body["model"]).strip()
    if body.get("clear_key"):
        cfg.pop("ai_key", None)
    elif str(body.get("key", "")).strip():
        cfg["ai_key"] = str(body["key"]).strip()
    if body.get("clear_eleven_key"):
        cfg.pop("eleven_key", None)
    elif str(body.get("eleven_key", "")).strip():
        cfg["eleven_key"] = str(body["eleven_key"]).strip()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify(public_config(cfg))


@app.get("/api/eleven/voices")
def eleven_voices():
    """Danh sách giọng + số credit còn lại của tài khoản ElevenLabs (cũng là cách kiểm tra key)."""
    sys.path.insert(0, str(ROOT))
    import eleven_tts

    kc = eleven_tts.load_config()
    if not kc:
        return jsonify(ok=False, error="Chưa có API key ElevenLabs."), 400
    try:
        vs = eleven_tts.voices(kc["key"])
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400
    c = eleven_tts.credits(kc["key"])
    return jsonify(ok=True, voices=vs, used=c[0] if c else None, limit=c[1] if c else None)


@app.get("/api/voices/<engine>")
def voice_catalog(engine):
    """Danh sách giọng của một bộ đọc để nghe thử, kèm giọng nào đã có mẫu sẵn."""
    import voice_preview

    if engine not in voice_preview.ENGINES:
        abort(404)
    try:
        vs = voice_preview.catalog(engine)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400
    ready = set(voice_preview.has(engine, [v["id"] for v in vs], request.args.get("text", "")))
    for v in vs:
        v["ready"] = v["id"] in ready
    return jsonify(ok=True, voices=vs, sample=voice_preview.SAMPLE, cost=voice_preview.COST.get(engine, ""))


@app.get("/api/voices/<engine>/sample")
def voice_sample(engine):
    import voice_preview

    if engine not in voice_preview.ENGINES or not request.args.get("voice"):
        abort(404)
    try:
        f = voice_preview.sample(engine, request.args["voice"], request.args.get("text", ""))
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400
    return send_from_directory(f.parent, f.name, max_age=0)


@app.post("/api/config/test")
def test_config():
    sys.path.insert(0, str(ROOT))
    import douyin_dub

    cfg = douyin_dub.load_ai_config()
    if not cfg:
        return jsonify(ok=False, error="Chưa có API key."), 400
    try:
        out = douyin_dub.ai_chat(cfg, 'Chỉ trả về JSON {"lines":[{"i":0,"vi":"..."}]}',
                                 '[{"i":0,"zh":"你好，明哥","max":6}]', timeout=30)
        if cfg["model"] != (read_config().get("ai_model") or ""):  # nhớ model chạy được
            c = read_config()
            c["ai_model"] = cfg["model"]
            CONFIG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
        vi = douyin_dub._parse_json(out)["lines"][0]["vi"]
        return jsonify(ok=True, sample=f"你好，明哥 → {vi}", model=cfg["model"])
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            msg = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        return jsonify(ok=False, error=msg[:400]), 400


def brand_state():
    return {**postfx.clean_brand(read_config().get("brand")), "has_logo": postfx.LOGO.is_file()}


@app.get("/api/brand")
def get_brand():
    return jsonify(brand_state())


@app.post("/api/brand")
def set_brand():
    """Lưu logo / chữ ký. JSON {enabled,text,pos,opacity,size} hoặc form có file 'logo' / remove_logo=1."""
    if request.files.get("logo"):
        try:
            postfx.save_logo(request.files["logo"])
        except Exception:
            return jsonify(error="Không đọc được ảnh logo. Hãy dùng file PNG hoặc JPG."), 400
        return jsonify(brand_state())
    if request.form.get("remove_logo") == "1":
        postfx.LOGO.unlink(missing_ok=True)
        return jsonify(brand_state())
    body = request.get_json(force=True) or {}
    cfg = read_config()
    cfg["brand"] = postfx.clean_brand({**(cfg.get("brand") or {}), **body})
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify(brand_state())


@app.get("/api/brand/preview.jpg")
def brand_preview():
    import io

    frame = None
    with lock:
        done = sorted((j for j in jobs.values() if j["status"] == "done"), key=lambda j: j["created"], reverse=True)
    for j in done[:5]:
        kit = publish_kit.load_kit(JOBS_DIR / j["id"]) or {}
        fr = kit.get("frames") or []
        if fr:
            fi = kit.get("frame", 0) if isinstance(kit.get("frame"), int) and kit["frame"] < len(fr) else 0
            p = JOBS_DIR / j["id"] / fr[fi]["file"]
            if p.is_file():
                frame = p
                break
    buf = io.BytesIO()
    postfx.preview(postfx.clean_brand(read_config().get("brand")), frame).save(buf, "JPEG", quality=85)
    buf.seek(0)
    from flask import send_file

    return send_file(buf, mimetype="image/jpeg", max_age=0)


def write_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ sửa lời thoại + lồng lại
def read_segs(jid):
    p = JOBS_DIR / jid / "segs.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/jobs/<jid>/segs")
def get_segs(jid):
    job = jobs.get(jid) or abort(404)
    data = read_segs(jid)
    if not data:
        return jsonify(error="Video này làm từ bản cũ, chưa lưu lời thoại để sửa. Hãy chạy lại video."), 404
    has_video = (JOBS_DIR / jid / data["video"]).is_file()
    return jsonify(chars=data.get("chars") or [], can_redub=has_video, edited=bool(job.get("edited")),
                   segs=[{"i": i, "start": s["start"], "end": s["end"], "zh": s.get("zh", ""), "vi": s.get("vi", ""),
                          "who": s.get("who", "")} for i, s in enumerate(data["segs"])])


@app.put("/api/jobs/<jid>/segs")
def put_segs(jid):
    """body: {lines: [{i, vi, who}]} - chỉ cần gửi các câu đã sửa."""
    job = jobs.get(jid) or abort(404)
    if job["status"] in ("queued", "running"):
        return jsonify(error="Video đang xử lý, hãy đợi xong."), 409
    data = read_segs(jid) or abort(404)
    chars = {c["id"] for c in data.get("chars") or []}
    cv = data.get("char_voice") or {}
    changed = 0
    for ln in (request.get_json(force=True) or {}).get("lines") or []:
        try:
            s = data["segs"][int(ln["i"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if "vi" in ln:
            vi = re.sub(r"\s+", " ", str(ln["vi"])).strip()[:400]
            if vi != s.get("vi"):
                s["vi"], changed = vi, changed + 1
        who = str(ln.get("who") or "")
        if who and who in chars and who != s.get("who"):
            s["who"], changed = who, changed + 1
            if who in cv:  # đổi người nói -> đổi luôn giọng sang giọng của nhân vật đó
                s["voice"], s["vkey"] = cv[who]["voice"], cv[who]["vkey"]
    if changed:
        (JOBS_DIR / jid / "segs.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        with lock:
            job["edited"] = True
        save(job)
    return jsonify(ok=True, changed=changed)


@app.post("/api/jobs/<jid>/redub")
def redub_job(jid):
    """Lồng tiếng lại từ lời thoại đã sửa (không tải / nghe / dịch lại). body: {tts?, separate?}"""
    job = jobs.get(jid) or abort(404)
    data = read_segs(jid)
    if not data:
        return jsonify(error="Chưa có lời thoại đã lưu cho video này."), 409
    if not (JOBS_DIR / jid / data["video"]).is_file():
        return jsonify(error="Video gốc đã bị xoá nên không lồng lại được."), 409
    body = request.get_json(silent=True) or {}
    return queue_redo(job, "dub", {k: body[k] for k in ("tts", "separate") if k in body})


# các tuỳ chọn mà đổi thì phải đọc giọng lại; còn lại (phụ đề, cỡ chữ, bản dọc, logo) chỉ cần xuất lại video
DUB_KEYS = ("dub", "voice", "tts", "gemini_voice", "eleven_voice", "eleven_model", "vieneu_voice")
REDO_KEYS = DUB_KEYS + ("burn", "cover", "font_size", "vertical", "vsubs", "brand", "kit", "split", "music")
MIX_KEYS = ("separate", "bg_volume", "music")  # chỉ cần trộn lại tiếng (đã lưu giọng Việt riêng), không phải đọc lại


def queue_redo(job, mode, changes):
    """Làm tiếp một video đã xong / lỗi với tuỳ chọn mới, không tải / nghe / dịch lại.
    mode: "render" (giữ tiếng đã lồng, chỉ xuất lại hình) hoặc "dub" (đọc giọng lại rồi xuất)."""
    with lock:
        if job["status"] in ("queued", "running"):
            return jsonify(error="Video đang xử lý."), 409
        old = job["options"]
        new = parse_options({**old, **{k: v for k, v in changes.items() if k in REDO_KEYS}})
        new = {**old, **new}
        if mode != "dub" and any(new.get(k) != old.get(k) for k in DUB_KEYS if k != "dub") and new["dub"]:
            mode = "dub"  # đổi giọng / tách giọng -> bắt buộc đọc lại
        if mode != "dub" and new["dub"] and not old.get("dub"):
            mode = "dub"  # trước chỉ làm phụ đề, giờ bật lồng tiếng
        if mode != "dub" and not find_files(JOBS_DIR / job["id"]).get("video"):
            mode = "dub"  # chưa từng xuất xong -> không có tiếng đã lồng để dùng lại
        if mode != "dub" and any(new.get(k) != old.get(k) for k in MIX_KEYS) and new["dub"] \
                and not (JOBS_DIR / job["id"] / "giong_viet.m4a").is_file():
            mode = "dub"  # video làm từ bản cũ chưa lưu giọng Việt riêng -> đổi nền / nhạc phải đọc lại
        job["options"] = new
        job.update(status="queued", redub=mode == "dub", rerender=mode != "dub", error="")
        job["log"].append("—— Lồng lại giọng đọc ——" if mode == "dub" else "—— Xuất lại video với tuỳ chọn mới ——")
    save(job)
    work_q.put(job["id"])
    return jsonify(public(job)), 202


@app.post("/api/jobs/<jid>/resume")
def resume_job(jid):
    """Chạy tiếp job lỗi / bị tắt giữa chừng: dùng lại video đã tải, kết quả nghe, bản dịch đã lưu."""
    job = jobs.get(jid) or abort(404)
    with lock:
        if job["status"] in ("queued", "running"):
            return jsonify(error="Video đang xử lý."), 409
        job.update(status="queued", resume=True, redub=False, rerender=False, error="")
        job["log"].append("—— Chạy tiếp từ bước bị lỗi ——")
    save(job)
    work_q.put(jid)
    return jsonify(public(job)), 202


@app.post("/api/jobs/<jid>/redo")
def redo_job(jid):
    """body: {mode: render|dub, options: {burn, font_size, cover, vertical, vsubs, brand, dub, tts, voice, separate, ...}}"""
    job = jobs.get(jid) or abort(404)
    data = read_segs(jid)
    if not data:
        return jsonify(error="Video này làm từ bản cũ, chưa lưu lời thoại nên không làm tiếp được. Hãy chạy lại từ đầu."), 409
    if not (JOBS_DIR / jid / data["video"]).is_file():
        return jsonify(error="Video gốc đã bị xoá nên không làm tiếp được."), 409
    body = request.get_json(silent=True) or {}
    return queue_redo(job, "dub" if body.get("mode") == "dub" else "render", body.get("options") or {})


# ------------------------------------------------------------------ tự đăng (kênh theo dõi)
def auto_post(job):
    """Đăng video của kênh theo dõi lên các tài khoản đã chọn; có giãn cách thì xếp lịch nối tiếp nhau."""
    ap = job.get("auto_post") or {}
    for _ in range(120):  # đợi bộ đăng bài (tiêu đề, ảnh bìa) làm xong, tối đa 10 phút
        if job.get("kit") != "running":
            break
        time.sleep(5)
    accounts = [a for a in (social.account_brief(str(i)) for i in ap.get("account_ids") or []) if a]
    if not accounts:
        return
    files = find_files(JOBS_DIR / job["id"])
    variant = "vertical" if ap.get("variant") == "vertical" and files.get("vertical") else "video"
    gap = float(ap.get("gap_h") or 0) * 3600
    at = None
    with lock:
        if gap:
            last = max([p["at"] for j in jobs.values() if j.get("watch") == job.get("watch")
                        for p in j.get("posts") or [] if p.get("status") == "scheduled" and p.get("at")] or [0])
            at = max(time.time() + 120, last + gap)
        new = []
        for a in accounts:
            post = {"id": uuid.uuid4().hex[:8], "account_id": a["id"], "account": a["name"], "platform": a["platform"],
                    "privacy": ap.get("privacy") if ap.get("privacy") in social.PRIVACY else "private",
                    "variant": variant, "status": "scheduled" if at else "queued", "at": at, "progress": 0,
                    "url": "", "note": "Tự đăng từ kênh theo dõi", "error": "", "created": time.time()}
            job.setdefault("posts", []).append(post)
            new.append(post)
    save(job)
    if not at:
        for p in new:
            run_post(job, p)


# ------------------------------------------------------------------ lưu trữ
move_state = {"running": False, "log": [], "error": ""}


def storage_state():
    with lock:
        n = len(jobs)
    cfg = read_config()
    return {"dir": str(storage.data_dir() or JOBS_DIR.parent), "custom": bool(storage.data_dir()),
            "jobs_dir": str(JOBS_DIR), "free": storage.disk_free(JOBS_DIR), "used": storage.folder_size(JOBS_DIR),
            "c_free": storage.disk_free(Path.home()), "jobs": n, "keep_days": int(cfg.get("keep_days") or 0),
            "moving": move_state}


@app.get("/api/storage")
def get_storage():
    return jsonify(storage_state())


@app.post("/api/storage")
def set_storage():
    """body: {keep_days} và/hoặc {dir} (chuyển toàn bộ dữ liệu sang thư mục khác, chạy nền)."""
    global JOBS_DIR
    body = request.get_json(force=True) or {}
    if "keep_days" in body:
        try:
            days = int(body["keep_days"])
        except (TypeError, ValueError):
            days = 0
        cfg = read_config()
        cfg["keep_days"] = days if days in (0, 3, 7, 14, 30, 60, 90) else 0
        write_config(cfg)
    if body.get("dir"):
        new = Path(str(body["dir"]).strip().strip('"')).expanduser()
        if not new.is_absolute():
            return jsonify(error="Hãy nhập đường dẫn đầy đủ, ví dụ D:\\LongTiengViet-data"), 400
        with lock:
            if move_state["running"]:
                return jsonify(error="Đang chuyển dữ liệu."), 409
            if any(j["status"] in ("queued", "running") for j in jobs.values()):
                return jsonify(error="Đang có video xử lý / chờ xử lý. Hãy đợi xong rồi chuyển."), 409
            move_state.update(running=True, log=[], error="")

        def go():
            global JOBS_DIR
            try:
                storage.relocate(new, lambda m: move_state["log"].append(m))
                with lock:
                    JOBS_DIR = storage.jobs_dir()
                storage.apply_env()
                move_state["log"].append("Xong.")
            except Exception as e:
                move_state["error"] = str(e)[:300]
            move_state["running"] = False

        threading.Thread(target=go, daemon=True).start()
    return jsonify(storage_state())


def clean_old(days=None):
    days = int(read_config().get("keep_days") or 0) if days is None else days
    with lock:
        olds = storage.old_jobs(list(jobs.values()), days)
    freed = 0
    for j in olds:
        d = JOBS_DIR / j["id"]
        freed += storage.folder_size(d)
        shutil.rmtree(d, ignore_errors=True)
        with lock:
            jobs.pop(j["id"], None)
    return len(olds), freed


@app.post("/api/storage/clean")
def clean_now():
    body = request.get_json(silent=True) or {}
    try:
        days = int(body.get("days", read_config().get("keep_days") or 0))
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return jsonify(error="Hãy chọn số ngày cần giữ lại."), 400
    n, freed = clean_old(days)
    return jsonify(removed=n, freed=freed, **storage_state())


def janitor():
    while True:
        time.sleep(3600)
        try:
            clean_old()
        except Exception:
            pass


# ------------------------------------------------------------------ Telegram
def telegram_state():
    t = read_config().get("telegram") or {}
    return {"connected": bool(t.get("token") and t.get("chat_id")), "chat_name": t.get("chat_name", ""),
            "bot": t.get("bot", ""), "events": {e: (t.get("events") or {}).get(e, True) for e in notify.EVENTS}}


# ------------------------------------------------------------------ thư viện nhạc nền thay thế
MUSIC_MOODS = {"vui": "Vui nhộn", "nhenhang": "Nhẹ nhàng", "hoihop": "Hồi hộp", "camdong": "Cảm động"}
MUSIC_EXT = (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac")


def music_dir():
    d = (storage.data_dir() or ROOT) / "music"
    for m in MUSIC_MOODS:
        (d / m).mkdir(parents=True, exist_ok=True)
    return d


def music_list():
    d = music_dir()
    return {m: sorted(f.name for f in (d / m).iterdir() if f.suffix.lower() in MUSIC_EXT) for m in MUSIC_MOODS}


def pick_music(choice):
    """choice: random (mọi tâm trạng) / tên tâm trạng -> chọn ngẫu nhiên một bài trong thư viện."""
    import random

    if not choice:
        return None
    lib = music_list()
    pool = [(m, n) for m in (MUSIC_MOODS if choice == "random" else [choice]) for n in lib.get(m, [])]
    if not pool:
        return None
    m, n = random.choice(pool)
    return music_dir() / m / n


@app.get("/api/music")
def get_music():
    return jsonify(moods=MUSIC_MOODS, tracks=music_list(), dir=str(music_dir()))


@app.post("/api/music")
def add_music():
    mood = request.form.get("mood")
    files = request.files.getlist("file")
    if mood not in MUSIC_MOODS or not files:
        return jsonify(error="Hãy chọn tâm trạng và file nhạc."), 400
    saved = 0
    for f in files[:30]:
        name = re.sub(r'[\\/:*?"<>|]+', " ", Path(f.filename or "").name).strip()
        if Path(name).suffix.lower() not in MUSIC_EXT:
            continue
        f.save(music_dir() / mood / name)
        saved += 1
    if not saved:
        return jsonify(error="Chỉ nhận file nhạc mp3, m4a, wav, ogg, aac, flac."), 400
    return jsonify(moods=MUSIC_MOODS, tracks=music_list(), saved=saved)


@app.delete("/api/music/<mood>/<name>")
def del_music(mood, name):
    if mood not in MUSIC_MOODS:
        abort(404)
    f = music_dir() / mood / Path(name).name
    f.unlink(missing_ok=True)
    return jsonify(moods=MUSIC_MOODS, tracks=music_list())


@app.get("/api/music/file/<mood>/<name>")
def music_file(mood, name):
    if mood not in MUSIC_MOODS:
        abort(404)
    return send_from_directory(music_dir() / mood, Path(name).name)


# ------------------------------------------------------------------ thống kê video đã đăng
STATS_TTL = 15 * 60  # mỗi video lấy số liệu lại tối đa 15 phút / lần
stats_state = {"running": False, "at": 0}


def stats_rows():
    rows = []
    with lock:
        for j in jobs.values():
            for p in j.get("posts") or []:
                if p.get("status") != "done":
                    continue
                rows.append({"job": j["id"], "post": p["id"], "title": p.get("title") or job_title(j),
                             "platform": p.get("platform"), "account": p.get("account"), "url": p.get("url", ""),
                             "posted": p.get("finished") or p.get("created"), "part": p.get("part"),
                             "privacy": p.get("privacy"), "has_id": bool(p.get("ext_id")),
                             "stats": p.get("stats") or {}})
    return sorted(rows, key=lambda r: -(r["posted"] or 0))


def refresh_stats(force=False):
    """Lấy số liệu mới cho các video đã đăng (lần lượt, ở luồng riêng)."""
    stats_state.update(running=True)
    try:
        with lock:
            todo = [(j, p) for j in jobs.values() for p in (j.get("posts") or [])
                    if p.get("status") == "done" and p.get("ext_id")
                    and (force or time.time() - (p.get("stats") or {}).get("at", 0) > STATS_TTL)]
        touched = set()
        for j, p in todo:
            try:
                st = social.stats(p["account_id"], p["ext_id"])
                st["at"] = time.time()
                p["stats"] = st
            except Exception as e:
                p["stats"] = {**(p.get("stats") or {}), "error": str(e)[:200], "at": time.time()}
            touched.add(j["id"])
        for jid in touched:
            save(jobs[jid])
    finally:
        stats_state.update(running=False, at=time.time())


@app.get("/api/stats")
def get_stats():
    return jsonify(rows=stats_rows(), running=stats_state["running"], at=stats_state["at"])


@app.post("/api/stats/refresh")
def post_stats_refresh():
    if not stats_state["running"]:
        force = bool((request.get_json(silent=True) or {}).get("force"))
        threading.Thread(target=refresh_stats, args=(force,), daemon=True).start()
        stats_state["running"] = True
    return jsonify(running=True)


@app.get("/api/glossary")
def get_glossary():
    return jsonify(text=read_config().get("glossary") or "")


@app.post("/api/glossary")
def set_glossary():
    """Bảng tên riêng: mỗi dòng "tên gốc = tên Việt"."""
    text = str((request.get_json(force=True) or {}).get("text") or "")
    lines = [re.sub(r"\s*=\s*", " = ", ln.strip(), count=1) for ln in text.splitlines() if "=" in ln]
    cfg = read_config()
    cfg["glossary"] = "\n".join(ln for ln in lines if ln.split("=", 1)[0].strip() and ln.split("=", 1)[1].strip())[:20000]
    write_config(cfg)
    return jsonify(text=cfg["glossary"], count=len([x for x in cfg["glossary"].splitlines() if x]))


@app.get("/api/telegram")
def get_telegram():
    return jsonify(telegram_state())


@app.post("/api/telegram")
def set_telegram():
    """body: {token} kết nối (tự tìm cuộc trò chuyện) | {events: {...}} | {disconnect: true} | {test: true}"""
    body = request.get_json(force=True) or {}
    cfg = read_config()
    t = cfg.get("telegram") or {}
    if body.get("disconnect"):
        cfg.pop("telegram", None)
        write_config(cfg)
        return jsonify(telegram_state())
    token = str(body.get("token") or "").strip()
    if token:
        if not re.fullmatch(r"\d{5,15}:[\w-]{30,60}", token):
            return jsonify(error="Token không đúng dạng (ví dụ 123456789:AAH...). Lấy ở @BotFather."), 400
        try:
            chat_id, name, bot = notify.find_chat(token)
            t.update(token=token, chat_id=chat_id, chat_name=name, bot=bot)
            notify.send_now("🔔 Đã kết nối Douyin Dub. Tool sẽ báo cho bạn ở đây.", t)
        except Exception as e:
            return jsonify(error=str(e)[:300]), 400
    if isinstance(body.get("events"), dict):
        t["events"] = {e: bool(body["events"].get(e, True)) for e in notify.EVENTS}
    cfg["telegram"] = t
    write_config(cfg)
    if body.get("test"):
        try:
            notify.send_now("🔔 Tin nhắn thử từ Douyin Dub.", t)
        except Exception as e:
            return jsonify(error=str(e)[:300]), 400
    return jsonify(telegram_state())


# ------------------------------------------------------------------ theo dõi kênh
WATCH_PATH = ROOT / "watches.json"
watch_lock = threading.Lock()


def load_watches():
    try:
        return json.loads(WATCH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_watches(ws):
    WATCH_PATH.write_text(json.dumps(ws, ensure_ascii=False, indent=1), encoding="utf-8")


def watch_public(w):
    return {k: w.get(k) for k in ("id", "url", "name", "every_h", "enabled", "last_check", "last_error", "last_new",
                                  "found", "options", "auto_post", "checking", "created", "kind", "query", "platform",
                                  "filters", "per_check")} | {"seen": len(w.get("seen") or [])}


def watch_list(w):
    """Danh sách video của một mục theo dõi: kênh (watcher) hoặc từ khoá (tìm kiếm + lọc)."""
    import douyin_search
    import watcher

    if w.get("kind") == "search":
        f = w.get("filters") or {}
        items = douyin_search.search(w.get("platform", "douyin"), w["query"], f.get("sort", 0), f.get("days", 0),
                                     f.get("dur", ""), f.get("min_likes", 0))
        return w.get("name") or w["query"], items
    if "douyin.com" in w["url"]:
        with douyin_search.profile_lock:
            return watcher.list_videos(w["url"])
    return watcher.list_videos(w["url"])


def check_watch(wid, take=None):
    """Đọc danh sách video của kênh / từ khoá; video chưa thấy bao giờ -> tạo job. take: lần đầu lấy N video."""
    with watch_lock:
        ws = load_watches()
        w = next((x for x in ws if x["id"] == wid), None)
        if not w:
            return
        w["checking"] = True
        save_watches(ws)
    name, items, err = "", [], ""
    try:
        name, items = watch_list(w)
    except Exception as e:
        err = str(e)[:300]
    with watch_lock:
        ws = load_watches()
        w = next((x for x in ws if x["id"] == wid), None)
        if not w:
            return
        w.update(checking=False, last_check=time.time(), last_error=err)
        if name:
            w["name"] = name
        seen = w.setdefault("seen", [])
        if items:
            if take is not None:  # lần đầu: các video cũ coi như đã thấy, chỉ lấy `take` video mới nhất
                new = items[:take]
                seen.extend(i["id"] for i in items[take:])
            else:
                new = [i for i in items if i["id"] not in seen][:int(w.get("per_check") or 5)]  # mặc định 5 video / lần
            for it in reversed(new):  # cũ trước, mới sau
                make_job(it["url"], it["url"], it.get("title") or "", parse_options(w.get("options") or {}),
                         watch=wid, auto_post=w.get("auto_post"))
                seen.append(it["id"])
            w["seen"] = seen[-500:]
            if new:
                w["last_new"] = time.time()
                w["found"] = int(w.get("found") or 0) + len(new)
                notify.send("watch_new", f"📥 Kênh {w.get('name') or w['url']}: {len(new)} video mới, đang dịch.")
        save_watches(ws)


@app.get("/api/watches")
def list_watches():
    with watch_lock:
        return jsonify([watch_public(w) for w in load_watches()])


@app.post("/api/watches")
def add_watch():
    body = request.get_json(force=True) or {}
    extra = {}
    if body.get("kind") == "search":  # tự động theo từ khoá
        q = str(body.get("query") or "").strip()[:100]
        if not q:
            return jsonify(error="Hãy nhập từ khoá."), 400
        f = body.get("filters") or {}
        platform = body.get("platform") if body.get("platform") in ("youtube", "bilibili") else "douyin"
        try:
            filters = {"sort": int(f.get("sort", 0)) if int(f.get("sort", 0)) in (0, 1, 2) else 0,
                       "days": int(f.get("days", 0)) if int(f.get("days", 0)) in (0, 1, 7, 180) else 0,
                       "dur": f.get("dur") if f.get("dur") in ("short", "mid", "long") else "",
                       "min_likes": max(0, int(f.get("min_likes") or 0))}
            per = min(max(int(body.get("per_check", 3)), 1), 10)
        except (TypeError, ValueError):
            return jsonify(error="Bộ lọc không hợp lệ."), 400
        url = f"{platform}:search:{q}"
        extra = {"kind": "search", "query": q, "platform": platform, "filters": filters, "per_check": per,
                 "name": f"Từ khoá “{q}” ({ {'youtube': 'YouTube', 'bilibili': 'Bilibili'}.get(platform, 'Douyin') })"}
    else:
        url = str(body.get("url") or "").strip()
        m = re.search(r"https?://[^\s，。“”\"'<>]+", url)
        if not m:
            return jsonify(error="Hãy dán link trang kênh / trang cá nhân."), 400
        url = m.group(0)
    try:
        every = min(max(float(body.get("every_h", 6)), 1), 72)
        take = min(max(int(body.get("take", 1)), 0), 10)
    except (TypeError, ValueError):
        every, take = 6, 1
    ap = body.get("auto_post") or None
    if ap:
        ap = {"account_ids": [str(i) for i in (ap.get("account_ids") or [])][:20],
              "privacy": ap.get("privacy") if ap.get("privacy") in social.PRIVACY else "private",
              "variant": "vertical" if ap.get("variant") == "vertical" else "video",
              "gap_h": min(max(float(ap.get("gap_h") or 0), 0), 48)}
        if not ap["account_ids"]:
            ap = None
    w = {"id": uuid.uuid4().hex[:8], "url": url, "name": "", "every_h": every, "enabled": True,
         "options": parse_options(body.get("options") or {}), "auto_post": ap, "seen": [], "created": time.time(),
         "last_check": 0, "last_error": "", "found": 0, **extra}
    if extra and body.get("seen"):  # video đang hiện trong kết quả tìm kiếm mà người dùng không chọn -> bỏ qua
        w["seen"] = [str(i) for i in body["seen"]][:500]
    with watch_lock:
        ws = load_watches()
        if any(x["url"] == url for x in ws):
            return jsonify(error="Kênh này đã có trong danh sách theo dõi."), 409
        ws.append(w)
        save_watches(ws)
    threading.Thread(target=check_watch, args=(w["id"], take), daemon=True).start()
    return jsonify(watch_public(w)), 201


@app.post("/api/watches/<wid>")
def update_watch(wid):
    body = request.get_json(force=True) or {}
    with watch_lock:
        ws = load_watches()
        w = next((x for x in ws if x["id"] == wid), None) or abort(404)
        if "enabled" in body:
            w["enabled"] = bool(body["enabled"])
        if "every_h" in body:
            try:
                w["every_h"] = min(max(float(body["every_h"]), 1), 72)
            except (TypeError, ValueError):
                pass
        save_watches(ws)
    if body.get("check"):
        threading.Thread(target=check_watch, args=(wid,), daemon=True).start()
    return jsonify(watch_public(w))


@app.delete("/api/watches/<wid>")
def delete_watch(wid):
    with watch_lock:
        ws = load_watches()
        save_watches([x for x in ws if x["id"] != wid])
    return jsonify(ok=True)


# ------------------------------------------------------------------ tìm video theo từ khoá
def _vi_titles(items):
    """Dịch tiêu đề sang tiếng Việt (Google, một lượt) để dễ chọn; lỗi thì bỏ qua."""
    import douyin_dub

    zh = [re.sub(r"\s+", " ", i["title"]).strip() or "-" for i in items]
    try:
        tr = douyin_dub.GoogleGtx("auto", "vi")
        out = (tr.translate("\n".join(zh)) or "").split("\n")
        if len(out) != len(zh):
            return
        for i, v in zip(items, out):
            i["title_vi"] = v.strip()
    except Exception:
        pass


@app.get("/api/search")
def search_videos():
    import douyin_search

    a = request.args
    q = (a.get("q") or "").strip()[:100]
    if not q:
        return jsonify(error="Hãy nhập từ khoá."), 400
    try:
        items = douyin_search.search(a.get("platform") if a.get("platform") in ("youtube", "bilibili") else "douyin", q,
                                     int(a.get("sort") or 0), int(a.get("days") or 0), a.get("dur") or "",
                                     int(a.get("min_likes") or 0))
    except douyin_search.NeedLogin as e:
        return jsonify(error=str(e), need_login=True), 409
    except douyin_search.Busy as e:
        return jsonify(error=str(e)), 409
    except Exception as e:
        return jsonify(error=str(e)[:300]), 400
    _vi_titles(items)
    with lock:
        srcs = " ".join(j.get("source") or "" for j in jobs.values())
    for i in items:
        i["done"] = i["id"] in srcs
    return jsonify(items=items)


@app.get("/api/douyin/hot")
def douyin_hot():
    import douyin_search

    try:
        words = douyin_search.hot_list()[:30]
    except Exception as e:
        return jsonify(error=str(e)[:300]), 400
    for w in words:
        w["title"] = w["word"]
    _vi_titles(words)
    return jsonify(words=words)


@app.get("/api/douyin/login")
def douyin_login_state():
    import douyin_search

    st = dict(douyin_search.login_state)
    if st["logged_in"] is None and not st["open"] and request.args.get("check") == "1":
        try:
            st["logged_in"] = douyin_search.check_login()
        except Exception as e:
            st["msg"] = str(e)[:200]
    return jsonify(st)


@app.post("/api/douyin/login")
def douyin_login():
    """Mở cửa sổ trình duyệt của tool tại douyin.com để người dùng tự đăng nhập (quét QR) / xác minh."""
    import douyin_search

    if douyin_search.login_state["open"]:
        return jsonify(douyin_search.login_state)
    threading.Thread(target=douyin_search.login_window, daemon=True).start()
    time.sleep(0.3)
    return jsonify(douyin_search.login_state)


def watch_loop():
    time.sleep(30)
    while True:
        try:
            with watch_lock:
                due = [w["id"] for w in load_watches() if w.get("enabled") and not w.get("checking")
                       and time.time() - (w.get("last_check") or 0) >= w.get("every_h", 6) * 3600]
            for wid in due:
                check_watch(wid)
        except Exception:
            pass
        time.sleep(60)


@app.get("/files/<jid>/<path:name>")
def files(jid, name):
    if jid not in jobs:
        abort(404)
    return send_from_directory(JOBS_DIR / jid, name, as_attachment=request.args.get("dl") == "1")


if __name__ == "__main__":
    load_jobs()
    _ws = load_watches()
    for _w in _ws:
        _w["checking"] = False
    if _ws:
        save_watches(_ws)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    print(f"Douyin Dub web đang chạy: http://localhost:{PORT}")
    if "--open" in sys.argv:
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
