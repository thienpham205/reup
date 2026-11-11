"""
separate.py - Tách giọng nói khỏi nhạc nền / tiếng động (chạy offline trên CPU).

Dùng model mã nguồn mở MDX-Net "Kim_Vocal_2" của dự án Ultimate Vocal Remover
(https://github.com/Anjok07/ultimatevocalremovergui, ~66 MB, tự tải lần đầu), chạy bằng onnxruntime (đã có sẵn
cùng Piper), không cần PyTorch. Kết quả là phần "nền" = âm thanh gốc trừ đi giọng nói.

    python separate.py video.mp4 nen.wav
"""
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

import storage

MODEL_URL = "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/Kim_Vocal_2.onnx"
SR = 44100
N_FFT, HOP, DIM_F, DIM_T = 7680, 1024, 3072, 256
COMPENSATE = 1.009  # hệ số bù của model này (theo UVR)
CHUNK = HOP * (DIM_T - 1)
TRIM = N_FFT // 2
GEN = CHUNK - 2 * TRIM


def model_path():
    return storage.models_dir() / "sep" / "Kim_Vocal_2.onnx"


def ensure(log=print):
    p = model_path()
    if p.is_file():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    log("Tải model tách giọng (~66 MB, chỉ lần đầu)...")
    tmp = p.with_suffix(".part")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    if tmp.stat().st_size < 50e6:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Tải model tách giọng không trọn vẹn")
    tmp.replace(p)
    return p


_WIN = np.hanning(N_FFT + 1)[:-1].astype(np.float32)  # cửa sổ Hann tuần hoàn (như torch.hann_window)


def _stft(x):
    """x: (2, CHUNK) -> (4, DIM_F, DIM_T): thực/ảo của kênh trái, rồi kênh phải."""
    pad = N_FFT // 2
    x = np.pad(x, ((0, 0), (pad, pad)), mode="reflect")
    idx = np.arange(DIM_T)[:, None] * HOP + np.arange(N_FFT)[None, :]
    frames = x[:, idx] * _WIN  # (2, T, N_FFT)
    spec = np.fft.rfft(frames, axis=-1)[:, :, :DIM_F]  # (2, T, F)
    spec = spec.transpose(0, 2, 1)  # (2, F, T)
    return np.stack([spec[0].real, spec[0].imag, spec[1].real, spec[1].imag]).astype(np.float32)


def _istft(s):
    """(4, DIM_F, DIM_T) -> (2, CHUNK)"""
    nb = N_FFT // 2 + 1
    out = []
    for c in range(2):
        z = np.zeros((nb, DIM_T), dtype=np.complex64)
        z[:DIM_F] = s[2 * c] + 1j * s[2 * c + 1]
        frames = np.fft.irfft(z.T, n=N_FFT, axis=-1) * _WIN  # (T, N_FFT)
        length = N_FFT + HOP * (DIM_T - 1)
        y = np.zeros(length, dtype=np.float32)
        wsum = np.zeros(length, dtype=np.float32)
        for t in range(DIM_T):
            y[t * HOP:t * HOP + N_FFT] += frames[t]
            wsum[t * HOP:t * HOP + N_FFT] += _WIN ** 2
        y /= np.maximum(wsum, 1e-8)
        out.append(y[N_FFT // 2:N_FFT // 2 + CHUNK])
    return np.stack(out)


def _read(src):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True)
    if r.returncode or not r.stdout:
        raise RuntimeError("Không đọc được âm thanh để tách giọng")
    return np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, 2).T.copy()


def _write(x, dst):
    pcm = (np.clip(x.T, -1, 1) * 32767).astype(np.int16).tobytes()
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "2", "-i", "-",
                        str(dst)], input=pcm, capture_output=True)
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:])


def separate(src, dst_bg, dst_vocals=None, log=print, threads=0):
    """Ghi phần nền (nhạc + tiếng động, không còn giọng nói) của src ra dst_bg."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    # không giữ vùng nhớ đệm lớn giữa các đoạn (máy ít RAM trống dễ báo "bad allocation")
    so.enable_cpu_mem_arena = False
    so.enable_mem_pattern = False
    if threads:
        so.intra_op_num_threads = threads
    sess = ort.InferenceSession(str(ensure(log)), so, providers=["CPUExecutionProvider"])
    mix = _read(src)
    n = mix.shape[1]
    pad = GEN - n % GEN
    mixp = np.concatenate([np.zeros((2, TRIM), np.float32), mix, np.zeros((2, pad + TRIM), np.float32)], axis=1)
    starts = list(range(0, n + pad, GEN))
    voc = []
    t0 = time.time()
    for k, i in enumerate(starts):
        spec = _stft(mixp[:, i:i + CHUNK])[None]
        # chạy model với cả phổ đảo dấu rồi lấy trung bình -> bớt nhiễu (cách "denoise" của UVR)
        pred = (sess.run(None, {"input": spec})[0] - sess.run(None, {"input": -spec})[0]) * 0.5
        voc.append(_istft(pred[0])[:, TRIM:-TRIM])
        if k == 0 or (k + 1) % 5 == 0 or k + 1 == len(starts):
            el = time.time() - t0
            log(f"  tách giọng {k + 1}/{len(starts)} đoạn ({el:.0f}s)")
    vocals = np.concatenate(voc, axis=1)[:, :n] * COMPENSATE
    bg = mix - vocals
    _write(bg, dst_bg)
    if dst_vocals:
        _write(vocals, dst_vocals)
    return dst_bg


if __name__ == "__main__":
    separate(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
