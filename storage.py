"""
storage.py - Nơi lưu dữ liệu của tool (video, model nhận dạng giọng nói, giọng Piper, trình duyệt ẩn...).

Mặc định mọi thứ nằm cạnh tool. Đặt "data_dir" trong config.json (trên web: Cài đặt -> Lưu trữ) để chuyển
sang ổ khác còn nhiều chỗ trống, ví dụ D:\\LongTiengViet-data:
    <data_dir>/jobs        video đã làm
    <data_dir>/models/hf   model nhận dạng giọng nói (faster-whisper)
    <data_dir>/models/...  giọng Piper, model tách giọng, trình duyệt ẩn
"""
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HF_DEFAULT = Path.home() / ".cache" / "huggingface"


def _cfg():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def data_dir():
    d = _cfg().get("data_dir")
    return Path(d) if d and Path(d).is_dir() else None


def jobs_dir():
    d = data_dir()
    return (d / "jobs") if d else ROOT / "output" / "jobs"


def models_dir():
    d = data_dir()
    return (d / "models") if d else ROOT


def voices_dir():
    d = data_dir()
    return (d / "models" / "voices") if d else ROOT / "voices"


def pw_dir():
    d = data_dir()
    return (d / "models" / "pw-browsers") if d else ROOT / ".pw-browsers"


def hf_dir():
    d = data_dir()
    return (d / "models" / "hf") if d else None


def apply_env():
    """Gọi trước khi import faster-whisper / playwright để chúng tìm model ở đúng chỗ."""
    hf = hf_dir()
    if hf:
        os.environ["HF_HOME"] = str(hf)
    if pw_dir().is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_dir())


def folder_size(p):
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def disk_free(p):
    p = Path(p)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0


def _move(src, dst, log):
    """Chuyển src -> dst (gộp vào nếu dst đã có). Khác ổ đĩa thì chép xong mới xoá bản cũ."""
    src, dst = Path(src), Path(dst)
    if not src.exists() or (dst.exists() and src.resolve() == dst.resolve()):
        return
    log(f"Chuyển {src} -> {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.move(str(src), str(dst))
        return
    for item in src.iterdir():
        target = dst / item.name
        if target.exists():
            if item.is_dir():
                _move(item, target, lambda *_: None)
            continue
        shutil.move(str(item), str(target))
    shutil.rmtree(src, ignore_errors=True)


def relocate(new_dir, log=print):
    """Chuyển toàn bộ dữ liệu sang new_dir rồi ghi vào config.json. Trả về số byte đã chuyển."""
    new = Path(new_dir).expanduser()
    new.mkdir(parents=True, exist_ok=True)
    if not os.access(new, os.W_OK):
        raise RuntimeError("Không ghi được vào thư mục này.")
    old_jobs, old_voices, old_pw = jobs_dir(), voices_dir(), pw_dir()
    old_hf = hf_dir() or HF_DEFAULT
    need = sum(folder_size(p) for p in (old_jobs, old_voices, old_pw, old_hf / "hub") if p.exists())
    same_drive = Path(new).anchor.lower() == Path(old_jobs).anchor.lower()
    if not same_drive and disk_free(new) < need + 500e6:
        raise RuntimeError(f"Ổ đích không đủ chỗ (cần khoảng {need / 1e9:.1f} GB).")
    _move(old_jobs, new / "jobs", log)
    _move(old_voices, new / "models" / "voices", log)
    _move(old_pw, new / "models" / "pw-browsers", log)
    if (old_hf / "hub").is_dir():
        _move(old_hf / "hub", new / "models" / "hf" / "hub", log)
    (new / "jobs").mkdir(parents=True, exist_ok=True)
    cfg = _cfg()
    cfg["data_dir"] = str(new)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return need


def old_jobs(jobs, days):
    """Các job đã xong/lỗi cũ hơn `days` ngày và không còn bài hẹn giờ."""
    if not days:
        return []
    cut = time.time() - days * 86400
    return [j for j in jobs if j.get("status") in ("done", "error", "cancelled")
            and (j.get("finished") or j.get("created") or 0) < cut
            and not any(p.get("status") in ("scheduled", "queued", "running") for p in j.get("posts") or [])]
