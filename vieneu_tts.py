"""
vieneu_tts.py - Giọng đọc VieNeu-TTS v3 Turbo (https://github.com/pnnbao97/VieNeu-TTS, Apache 2.0).

Giọng tiếng Việt tự nhiên 48 kHz, 25 giọng dựng sẵn (nam / nữ, Bắc / Trung / Nam), chạy offline trên CPU
(~2x thời gian thực). VieNeu cần thư viện riêng nên cài vào môi trường Python tách biệt
(<thư mục dữ liệu>/models/vieneu-venv); tool gọi sang đó một lần cho cả video để chỉ tải model một lần.

Chạy riêng (trong môi trường VieNeu):  python vieneu_tts.py --worker jobs.json
  jobs.json: [{"text": "...", "voice": "Hải Đăng", "out": "D:/.../0001.mp3"}, ...]
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# giọng xếp theo độ hợp làm nhân vật phim / kể chuyện (đã nghe thử + chấm độ rõ bằng nhận dạng giọng nói)
VOICES = {
    "male": ["Hải Đăng", "Thiện Minh", "Adam bựa", "Phạm Tuyên", "Quốc Tuấn", "Xuân Vĩnh", "Thanh Bình", "Minh Đức",
             "Đức Trí", "Thái Sơn", "Minh Triết", "Adam", "Thiền Tâm Đức", "Quang Sơn"],
    "female": ["Trúc Ly", "Ngọc Huyền", "Mai Anh", "Đoan Trang", "Ngọc Linh", "Quỳnh Anh", "Thùy Dung", "Mỹ Duyên",
               "Kim Thanh", "Thục Đoan", "Ngọc Trân"],
}
# ưu tiên theo tuổi (VieNeu không có giọng trẻ con -> dùng giọng nữ trẻ, trong trẻo)
AGE_FIRST = {
    ("male", "old"): ["Thiền Tâm Đức", "Phạm Tuyên", "Thanh Bình"],
    ("male", "young"): ["Adam bựa", "Quốc Tuấn", "Hải Đăng"],
    ("female", "young"): ["Trúc Ly", "Quỳnh Anh", "Ngọc Huyền"],
    ("female", "old"): ["Thục Đoan", "Kim Thanh", "Ngọc Linh"],
    ("male", "child"): ["Quỳnh Anh", "Trúc Ly"],
    ("female", "child"): ["Quỳnh Anh", "Ngọc Huyền"],
}
INFO = {  # giới tính · vùng · phong cách, hiện trên web
    "Adam bựa": "Nam · Bắc · tự nhiên, vui", "Trúc Ly": "Nữ · Bắc · tự nhiên", "Thiện Minh": "Nam · Bắc · kể chuyện",
    "Mai Anh": "Nữ · Bắc · tin tức", "Hải Đăng": "Nam · Bắc · tự nhiên", "Thùy Dung": "Nữ · Nam · tin tức",
    "Thiền Tâm Đức": "Nam · Bắc · kể chuyện, trầm", "Ngọc Huyền": "Nữ · Bắc · tự nhiên",
    "Quang Sơn": "Nam · Trung · tự nhiên", "Ngọc Trân": "Nữ · Trung · tự nhiên", "Minh Đức": "Nam · Bắc · tin tức",
    "Phạm Tuyên": "Nam · Bắc · tự nhiên", "Thái Sơn": "Nam · Nam · kể chuyện", "Xuân Vĩnh": "Nam · Bắc · tự nhiên",
    "Thanh Bình": "Nam · Bắc · kể chuyện", "Ngọc Linh": "Nữ · Bắc · kể chuyện", "Đoan Trang": "Nữ · Bắc · tự nhiên",
    "Thục Đoan": "Nữ · Nam · kể chuyện", "Minh Triết": "Nam · Nam · tin tức", "Mỹ Duyên": "Nữ · Nam · đọc truyện",
    "Quỳnh Anh": "Nữ · Bắc · đọc truyện", "Đức Trí": "Nam · Nam · đọc truyện", "Kim Thanh": "Nữ · Nam · đọc truyện",
    "Adam": "Nam · Nam · tự nhiên", "Quốc Tuấn": "Nam · Bắc · tự nhiên",
}
ALL = list(INFO)
DEFAULT = {"male": "Hải Đăng", "female": "Trúc Ly"}


def venv_dir():
    import storage

    return storage.models_dir() / "vieneu-venv"


def venv_python():
    return venv_dir() / "Scripts" / "python.exe" if os.name == "nt" else venv_dir() / "bin" / "python"


def installed():
    return venv_python().is_file()


def ensure(log=print):
    """Cài VieNeu vào môi trường riêng nếu chưa có (~1 GB, chỉ lần đầu, vài phút). Trả về True nếu dùng được."""
    if installed():
        return True
    py = venv_python()
    try:
        import storage

        log("Cài giọng đọc VieNeu lần đầu (khoảng 1 GB, vài phút)...")
        env = dict(os.environ, PIP_CACHE_DIR=str(storage.models_dir() / "pip-cache"))
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir())], check=True, capture_output=True)
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", "vieneu"], capture_output=True, text=True, env=env)
        if r.returncode:
            raise RuntimeError(r.stderr[-400:])
        return True
    except Exception as e:
        log(f"Không cài được VieNeu ({str(e)[:200]})")
        return False


class Picker:
    """Mỗi nhân vật (giới tính, tuổi, thứ tự) một giọng VieNeu riêng, không trùng nhân vật khác."""

    def __init__(self, main=""):
        self.map, self.main = {}, main if main in INFO else ""

    def get(self, g, age, k):
        key = (g, age, k)
        if key not in self.map:
            g = g if g in VOICES else "male"
            used = set(self.map.values())
            pref = AGE_FIRST.get((g, age), []) + VOICES[g]
            free = [v for v in pref if v not in used]
            self.map[key] = free[0] if free else VOICES[g][k % len(VOICES[g])]
        return self.map[key]

    def set_main(self, key):
        """Giọng người dùng chọn -> dành cho nhân vật nói nhiều nhất."""
        if self.main:
            self.map[key] = self.main


def speak_all(items, log=print, progress=None):
    """items: [(text, out_path, voice)]. Đọc hết trong MỘT tiến trình VieNeu. Trả về tập chỉ số câu đã đọc được."""
    if not items or not ensure(log):
        return set()
    import storage
    import tempfile

    job = [{"text": t, "voice": v, "out": str(o)} for t, o, v in items]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
        jf = f.name
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    hf = storage.hf_dir()
    if hf:
        env["HF_HOME"] = str(hf)
    done = set()
    try:
        p = subprocess.Popen([str(venv_python()), "-I", str(Path(__file__).resolve()), "--worker", jf],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        err = ""
        for line in p.stdout:
            line = line.strip()
            if line.startswith("OK "):
                done.add(int(line[3:]))
                if progress:
                    progress(len(done), len(items))
            elif line.startswith("ERR "):
                err = line[4:]
        p.wait()
        if err and len(done) < len(items):
            log(f"VieNeu lỗi: {err[:200]}")
    finally:
        os.unlink(jf)
    return done


def _worker(job_file):
    """Chạy trong môi trường VieNeu: tải model một lần, đọc lần lượt từng câu."""
    import soundfile as sf
    from vieneu import Vieneu

    jobs = json.loads(Path(job_file).read_text(encoding="utf-8"))
    tts = Vieneu()
    for i, j in enumerate(jobs):
        try:
            audio = tts.infer(j["text"], voice=j["voice"])
            tmp = j["out"] + ".wav"
            sf.write(tmp, audio, 48000)
            os.replace(tmp, j["out"])  # đuôi .mp3 nhưng ffmpeg tự nhận ra là wav
            print(f"OK {i}", flush=True)
        except Exception as e:
            print(f"ERR {type(e).__name__}: {e}".replace("\n", " "), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        _worker(sys.argv[2])
    else:
        sys.exit(__doc__)
