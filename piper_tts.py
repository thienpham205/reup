"""
piper_tts.py - Giọng đọc mã nguồn mở Piper (https://github.com/OHF-Voice/piper1-gpl), chạy offline trên máy.

Giọng tiếng Việt lấy từ kho mở rhasspy/piper-voices (giấy phép theo từng bộ dữ liệu, xem MODEL_CARD):
  - vais1000 : 1 giọng nữ, chất lượng tốt nhất (22 kHz)
  - 25hours  : 1 giọng nữ
  - vivos    : 65 người đọc (nam + nữ) của bộ dữ liệu VIVOS, chất lượng thấp hơn (16 kHz) nhưng rất đa dạng

Mỗi nhân vật được một giọng riêng; giọng xếp theo độ rõ đã chấm bằng nhận dạng giọng nói (xem voices/pool.json).
"""
import io
import json
import logging
import subprocess
import threading
import urllib.request
import wave
from pathlib import Path

import storage

ROOT = Path(__file__).resolve().parent
VOICE_DIR = storage.voices_dir()  # có thể đã chuyển sang ổ khác (Cài đặt -> Lưu trữ)
BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/"
MODELS = {
    "vais1000": "vais1000/medium/vi_VN-vais1000-medium",
    "25hours": "25hours_single/low/vi_VN-25hours_single-low",
    "vivos": "vivos/x_low/vi_VN-vivos-x_low",
}
# giọng xếp từ rõ nhất; "model:speaker". Ghi đè được bằng voices/pool.json
DEFAULT_POOL = {
    # điểm rõ (0-1) khi máy nghe lại: nam 0,82-0,89; nữ 0,82-0,89; VAIS là giọng thu âm studio nên đứng đầu
    "female": ["vais1000", "vivos:27", "25hours", "vivos:21", "vivos:23", "vivos:26", "vivos:15", "vivos:18", "vivos:40"],
    "male": ["vivos:45", "vivos:43", "vivos:41", "vivos:37", "vivos:9", "vivos:46", "vivos:30", "vivos:25", "vivos:29"],
    "child": ["vivos:0", "vivos:1", "vivos:22", "vivos:10"],  # giọng nữ cao nhất, nâng thêm cao độ
}
NAMES = {"vais1000": "Nữ VAIS", "25hours": "Nữ 25h"}

logging.getLogger("piper").setLevel(logging.ERROR)  # bỏ cảnh báo "Missing phoneme" của các model cũ

_lock = threading.Lock()
_voices = {}
_vlocks = {}


def model_path(name):
    return VOICE_DIR / (MODELS[name].rsplit("/", 1)[1] + ".onnx")


def installed():
    try:
        import piper  # noqa: F401
    except Exception:
        return False
    return all(model_path(n).is_file() for n in MODELS)


def ensure(log=print):
    """Tải các model còn thiếu (~150 MB, chỉ lần đầu). Trả về True nếu dùng được Piper."""
    try:
        import piper  # noqa: F401
    except Exception:
        log("Chưa cài Piper (pip install piper-tts) -> dùng giọng Edge")
        return False
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    for n, rel in MODELS.items():
        dst = model_path(n)
        if dst.is_file():
            continue
        log(f"Tải giọng Piper {n} (chỉ lần đầu)...")
        try:
            for ext in (".onnx.json", ".onnx"):
                tmp = dst.with_name(dst.name.replace(".onnx", ext) + ".part")
                urllib.request.urlretrieve(BASE_URL + rel + ext, tmp)
                tmp.replace(dst.with_name(dst.name.replace(".onnx", ext)))
        except Exception as e:
            log(f"Không tải được giọng Piper ({str(e)[:120]}) -> dùng giọng Edge")
            return False
    return True


def pool():
    for f in (VOICE_DIR / "pool.json", ROOT / "voices" / "pool.json"):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
            if all(p.get(k) for k in DEFAULT_POOL):
                return p
        except Exception:
            pass
    return DEFAULT_POOL


def label(key):
    model, _, spk = key.partition(":")
    return NAMES.get(model) or f"VIVOS #{spk}"


def _voice(model):
    with _lock:
        if model not in _voices:
            from piper import PiperVoice

            _voices[model] = PiperVoice.load(str(model_path(model)))
            _vlocks[model] = threading.Lock()
        return _voices[model], _vlocks[model]


def speak(text, out, key, rate=0, pitch=0):
    """Đọc text bằng giọng key ("vivos:21", "vais1000"...) ra file WAV out.
    rate: % nhanh/chậm như Edge; pitch: Hz như Edge (đổi bằng ffmpeg, ~ +45 Hz = giọng trẻ con)."""
    from piper import SynthesisConfig

    model, _, spk = key.partition(":")
    voice, vlock = _voice(model)
    cfg = SynthesisConfig(speaker_id=int(spk) if spk else None, length_scale=1 / max(0.5, 1 + rate / 100))
    buf = io.BytesIO()
    with vlock:  # một model không chạy song song nhiều câu
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w, syn_config=cfg)
    data = buf.getvalue()
    if len(data) < 2000:
        raise RuntimeError("Piper không đọc được câu này")
    factor = 1 + pitch / 220
    if abs(factor - 1) < 0.01:
        Path(out).write_bytes(data)
        return
    sr = voice.config.sample_rate
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "wav", "-i", "-", "-af",
                        f"asetrate={int(sr * factor)},aresample={sr},atempo={1 / factor:.4f}", "-f", "wav", str(out)],
                       input=data, capture_output=True)
    if r.returncode:
        Path(out).write_bytes(data)


class Picker:
    """Cấp cho mỗi nhân vật (giới tính, tuổi, thứ tự) một giọng Piper riêng, không trùng nhau."""

    def __init__(self):
        self.p = pool()
        self.used = set()
        self.map = {}

    def get(self, g, age, k):
        key = (g, age, k)
        if key not in self.map:
            names = self.p["child"] if age == "child" else self.p["male" if g == "male" else "female"]
            free = [n for n in names if n not in self.used] or names
            v = free[0]
            self.used.add(v)
            pitch, rate = {"old": (-10, -6), "young": (4, 2), "child": (20, 6)}.get(age, (0, 0))
            self.map[key] = (f"piper:{v}", pitch, rate)
        return self.map[key]
