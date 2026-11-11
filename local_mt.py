"""
local_mt.py - Dịch Trung -> Việt ngay trên máy (không cần mạng, không cần API key) bằng model mã nguồn mở
Hunyuan-MT-7B của Tencent (https://github.com/Tencent-Hunyuan/Hunyuan-MT), đứng đầu 30/31 cặp ngôn ngữ ở cuộc
thi dịch máy WMT25. Chạy bằng llama.cpp (https://github.com/ggml-org/llama.cpp) trên CPU.

Lần đầu tự tải (vào thư mục models, xem Cài đặt -> Lưu trữ):
  - llama.cpp bản Windows CPU (~25 MB)
  - Hunyuan-MT-7B đã nén Q4_K_M (~4,6 GB, bản lượng tử hoá của mradermacher trên Hugging Face)

    python local_mt.py "你好，明哥" "我吃饭以前要先洗澡"
"""
import atexit
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests

import storage

MODEL_URL = "https://huggingface.co/mradermacher/Hunyuan-MT-7B-GGUF/resolve/main/Hunyuan-MT-7B.Q4_K_M.gguf"
MODEL_SIZE = 4624950272
LLAMA_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"
PORT = 5588
PROMPT = "把下面的文本翻译成越南语，不要额外解释。\n\n{}"  # mẫu câu lệnh chính thức của Hunyuan-MT cho chiều zh -> xx


def base():
    return storage.models_dir() / "mt"


def model_path():
    return base() / "Hunyuan-MT-7B.Q4_K_M.gguf"


def server_exe():
    hits = list((base() / "llama").rglob("llama-server.exe" if os.name == "nt" else "llama-server"))
    return hits[0] if hits else None


def installed():
    return model_path().is_file() and server_exe() is not None


def _download(url, dst, log, label, size=None):
    """Tải có tiếp tục (file .part) và báo tiến độ mỗi 5%."""
    part = Path(str(dst) + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with requests.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True) as r:
        if r.status_code == 416:  # đã tải đủ
            part.replace(dst)
            return
        r.raise_for_status()
        if have and r.status_code != 206:  # máy chủ không cho tải tiếp -> tải lại từ đầu
            have = 0
        total = size or (have + int(r.headers.get("content-length", 0)))
        step = -1
        with open(part, "ab" if have else "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                have += len(chunk)
                if total:
                    pct = int(have * 20 / total)
                    if pct != step:
                        step = pct
                        log(f"  tải {label}: {have / 1e9:.2f}/{total / 1e9:.2f} GB ({pct * 5}%)")
    if size and part.stat().st_size != size:
        raise RuntimeError(f"Tải {label} chưa trọn vẹn, hãy thử lại (sẽ tải tiếp phần còn thiếu)")
    part.replace(dst)


def ensure(log=print):
    """Tải bộ chạy + model nếu chưa có. Trả về True nếu dùng được."""
    base().mkdir(parents=True, exist_ok=True)
    if server_exe() is None:
        if os.name != "nt":
            raise RuntimeError("Hãy cài llama.cpp (llama-server) rồi đặt vào " + str(base() / "llama"))
        log("Tải bộ chạy llama.cpp (~25 MB, chỉ lần đầu)...")
        rels = requests.get(LLAMA_RELEASES, timeout=30).json()
        # bản mới nhất có kèm file chạy Windows CPU (một số bản phát hành không có)
        asset = next((a for rel in rels for a in rel.get("assets") or []
                      if re.search(r"^llama-b\d+-bin-win-cpu-x64\.zip$", a["name"])), None)
        if not asset:
            raise RuntimeError("Không tìm thấy bản llama.cpp cho Windows trên GitHub")
        z = base() / asset["name"]
        _download(asset["browser_download_url"], z, log, "llama.cpp", asset["size"])
        with zipfile.ZipFile(z) as zf:
            zf.extractall(base() / "llama")
        z.unlink(missing_ok=True)
    if not model_path().is_file():
        log("Tải model dịch Hunyuan-MT-7B (~4,6 GB, chỉ lần đầu; tải dở thì lần sau tải tiếp)...")
        _download(MODEL_URL, model_path(), log, "Hunyuan-MT", MODEL_SIZE)
    return True


def _perf_cores():
    """Số nhân thật (không tính siêu phân luồng). Chip Intel đời 12+ có nhân P (mạnh) và nhân E (yếu): chạy model trên
    quá nhiều luồng thì nhân E làm chậm cả nhóm, nên giới hạn ở khoảng số nhân P."""
    try:
        import psutil

        phys, logical = psutil.cpu_count(logical=False) or 4, psutil.cpu_count() or 8
        p_cores = logical - phys  # nhân P có 2 luồng, nhân E có 1 -> số luồng dư = số nhân P
        return max(4, min(p_cores, 8)) if p_cores > 0 else max(2, phys)
    except Exception:
        return max(2, (os.cpu_count() or 4) // 2)


class Server:
    """llama-server chạy nền trong suốt lúc dịch, tự tắt khi xong."""

    def __init__(self, log=print):
        self.log = log
        self.p = None

    def __enter__(self):
        ensure(self.log)
        threads = int(os.environ.get("LMT_THREADS") or 0) or _perf_cores()
        cmd = [str(server_exe()), "-m", str(model_path()), "--host", "127.0.0.1", "--port", str(PORT),
               "-c", "4096", "-t", str(threads), "--no-webui", "-np", "1"]
        self.log("Khởi động model dịch Hunyuan-MT trên máy...")
        self.p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        atexit.register(self.close)
        t0 = time.time()
        while time.time() - t0 < 300:
            if self.p.poll() is not None:
                err = (self.p.stderr.read() or b"").decode("utf-8", "replace")[-400:]
                raise RuntimeError(f"Không chạy được model dịch: {err}")
            try:
                if requests.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
                    return self
            except requests.RequestException:
                pass
            time.sleep(1)
        raise RuntimeError("Model dịch khởi động quá lâu")

    def close(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except Exception:
                self.p.kill()

    def __exit__(self, *a):
        self.close()

    def translate(self, text, max_tokens=512):
        r = requests.post(f"http://127.0.0.1:{PORT}/v1/chat/completions", timeout=300, json={
            "messages": [{"role": "user", "content": PROMPT.format(text)}],
            # thông số khuyến nghị của Hunyuan-MT (hạ temperature cho bản dịch ổn định)
            "temperature": 0.3, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05, "max_tokens": max_tokens})
        r.raise_for_status()
        out = r.json()["choices"][0]["message"]["content"]
        return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


def translate_lines(lines, log=print, chunk=6):
    """Dịch danh sách câu tiếng Trung. Dịch từng cụm vài câu (giữ ngữ cảnh: xưng hô, đại từ), lệch số dòng thì
    dịch lại từng câu của cụm đó."""
    out = [""] * len(lines)
    with Server(log) as srv:
        t0 = time.time()
        for a in range(0, len(lines), chunk):
            idx = [i for i in range(a, min(a + chunk, len(lines))) if lines[i].strip()]
            if not idx:
                continue
            res = srv.translate("\n".join(lines[i].strip() for i in idx)).split("\n")
            res = [x.strip() for x in res if x.strip()]
            if len(res) != len(idx):
                res = [srv.translate(lines[i].strip(), 200).replace("\n", " ") for i in idx]
            for i, v in zip(idx, res):
                out[i] = v
            log(f"  dịch {min(a + chunk, len(lines))}/{len(lines)} câu ({time.time() - t0:.0f}s)")
    return out


if __name__ == "__main__":
    for zh, vi in zip(sys.argv[1:], translate_lines(sys.argv[1:])):
        print(zh, "->", vi)
