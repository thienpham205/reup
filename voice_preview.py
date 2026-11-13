"""Nghe thử giọng đọc trước khi chọn: danh sách giọng của từng bộ đọc + tạo câu mẫu (lưu lại, lần sau phát ngay).

Mẫu lưu ở <thư mục dữ liệu>/voice_samples/<bộ đọc>/<mã>.mp3|wav. VieNeu nạp model mất vài chục giây nên lần
đầu nghe thử sẽ tạo luôn mẫu cho cả 25 giọng trong một lần chạy (giọng bấm nghe được làm trước).
"""
import hashlib
import threading
import time
import wave
from pathlib import Path

import storage
import vieneu_tts

ROOT = Path(__file__).resolve().parent
SAMPLE = "Xin chào các bạn! Hôm nay mình sẽ kể cho các bạn nghe một câu chuyện cực kỳ thú vị. Nhớ xem đến cuối nhé!"
ENGINES = ("vieneu", "edge", "edgeplus", "mix", "piper", "gemini", "eleven")

EDGE_NAMES = {
    "vi-VN-HoaiMyNeural": ("Hoài My", "Nữ · tiếng Việt · rõ, chuẩn"),
    "vi-VN-NamMinhNeural": ("Nam Minh", "Nam · tiếng Việt · rõ, chuẩn"),
}
GEMINI_INFO = {"Aoede": "Nữ trẻ, tươi", "Kore": "Nữ, chắc giọng", "Zephyr": "Nữ, sáng", "Leda": "Nữ rất trẻ",
               "Despina": "Nữ, mềm", "Charon": "Nam, kiểu thuyết minh", "Puck": "Nam trẻ, vui", "Orus": "Nam, chắc giọng",
               "Fenrir": "Nam, sôi nổi", "Algenib": "Nam, khàn"}
COST = {
    "gemini": "Mỗi giọng nghe lần đầu dùng 1 lượt Gemini TTS (gói miễn phí có hạn mức theo ngày).",
    "eleven": "Mỗi giọng nghe lần đầu tốn khoảng 50 credit ElevenLabs (đọc bằng model Flash). Nghe lại không tốn.",
    "vieneu": "Lần đầu nghe thử: tạo mẫu cho cả 25 giọng một lượt, khoảng 1–2 phút. Sau đó bấm là nghe ngay.",
}

_vn_lock = threading.Lock()
_vn_state = {"running": False, "err": "", "queue": []}


def sample_dir(engine):
    d = (storage.data_dir() or ROOT / "output") / "voice_samples" / engine
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(engine, voice, text):
    return hashlib.sha1(f"{engine}|{voice}|{text}".encode("utf-8")).hexdigest()[:16]


def _edge_label(v):
    if v in EDGE_NAMES:
        return EDGE_NAMES[v]
    lang, _, rest = v.partition("-")[2].partition("-")
    name = rest.replace("MultilingualNeural", "")
    nation = {"US": "Mỹ", "AU": "Úc", "DE": "Đức", "FR": "Pháp", "KR": "Hàn", "BR": "Brazil", "IT": "Ý"}.get(lang, lang)
    import douyin_dub

    sex = "Nam" if v in douyin_dub.EDGE_MALE else "Nữ"
    return name, f"{sex} · giọng {nation} đọc tiếng Việt (hơi lơ lớ)"


def catalog(engine):
    """[{id, name, desc, group}] theo thứ tự hiện trên web. pick: lựa chọn giọng này gắn với ô nào trên web."""
    import douyin_dub

    out = []
    if engine == "vieneu":
        for g, label in (("male", "Nam"), ("female", "Nữ")):
            for v in vieneu_tts.VOICES[g]:
                out.append({"id": v, "name": v, "desc": vieneu_tts.INFO.get(v, label).split(" · ", 1)[-1], "group": label})
    elif engine in ("edge", "edgeplus", "mix"):
        for v in (douyin_dub.FEMALE, douyin_dub.MALE):
            n, d = _edge_label(v)
            out.append({"id": v, "name": n, "desc": d, "group": "Giọng chính (Edge)"})
        if engine == "edgeplus":
            for g in ("female", "male"):
                for v in douyin_dub.EDGE_EXTRA[g]:
                    n, d = _edge_label(v)
                    out.append({"id": v, "name": n, "desc": d, "group": "Vai phụ (Edge đa ngôn ngữ)"})
    if engine in ("mix", "piper"):
        import piper_tts

        seen = set()
        for g, label in (("female", "Nữ"), ("male", "Nam"), ("child", "Trẻ con")):
            for k in piper_tts.pool()[g]:
                if k in seen:
                    continue
                seen.add(k)
                out.append({"id": "piper:" + k, "name": piper_tts.label(k), "desc": label + " · Piper (mã nguồn mở)",
                            "group": ("Vai phụ (Piper) · " if engine == "mix" else "") + label})
    elif engine == "gemini":
        for v in douyin_dub.GEMINI_PICK:
            out.append({"id": v, "name": v, "desc": GEMINI_INFO.get(v, ""), "group": "Nữ" if GEMINI_INFO.get(v, "").startswith("Nữ") else "Nam"})
    elif engine == "eleven":
        import eleven_tts

        kc = eleven_tts.load_config()
        if not kc:
            raise RuntimeError("Chưa có API key ElevenLabs.")
        G = {"female": "Nữ", "male": "Nam"}
        for v in eleven_tts.voices(kc["key"]):
            out.append({"id": v["id"], "name": v["name"], "desc": ", ".join(x for x in (G.get(v["gender"], ""), v["age"]) if x),
                        "group": G.get(v["gender"], "Khác")})
    return out


def cached(engine, voice, text):
    for ext in (".mp3", ".wav"):
        f = sample_dir(engine) / (_key(engine, voice, text) + ext)
        if f.is_file() and f.stat().st_size > 1000:
            return f
    return None


def _vieneu_batch(first, text):
    """Một lần nạp model: đọc giọng được bấm trước, rồi các giọng còn thiếu (chỉ với câu mẫu)."""
    voices = [first] + ([v for g in ("female", "male") for v in vieneu_tts.VOICES[g] if v != first] if text == SAMPLE else [])
    items = [(text, sample_dir("vieneu") / (_key("vieneu", v, text) + ".wav"), v) for v in voices
             if not cached("vieneu", v, text)]
    try:
        if items:
            vieneu_tts.speak_all(items, log=lambda *a: None)
    except Exception as e:
        _vn_state["err"] = str(e)[:200]
    finally:
        _vn_state["running"] = False


def _vieneu(voice, text, wait=240):
    if not vieneu_tts.installed():
        raise RuntimeError("VieNeu chưa cài: làm một video bằng VieNeu là tool tự cài (~1 GB), sau đó nghe thử được.")
    with _vn_lock:
        if not _vn_state["running"]:
            _vn_state.update(running=True, err="")
            threading.Thread(target=_vieneu_batch, args=(voice, text), daemon=True).start()
    end = time.time() + wait
    while time.time() < end:
        f = cached("vieneu", voice, text)
        if f:
            return f
        if not _vn_state["running"]:  # lượt đang chạy không có giọng này (câu khác) -> chạy lượt mới
            if _vn_state["err"]:
                raise RuntimeError("VieNeu lỗi: " + _vn_state["err"])
            with _vn_lock:
                if not _vn_state["running"]:
                    _vn_state.update(running=True, err="")
                    threading.Thread(target=_vieneu_batch, args=(voice, text), daemon=True).start()
        time.sleep(0.4)
    raise RuntimeError("VieNeu tạo mẫu quá lâu, thử lại sau ít phút.")


def sample(engine, voice, text=""):
    """Trả về file âm thanh đọc câu mẫu (hoặc câu người dùng gõ) bằng giọng voice của bộ đọc engine."""
    import douyin_dub

    text = " ".join((text or "").split())[:200] or SAMPLE
    f = cached(engine, voice, text)
    if f:
        return f
    out = sample_dir(engine) / (_key(engine, voice, text) + ".mp3")
    if engine == "vieneu":
        return _vieneu(voice, text)
    if voice.startswith("piper:"):
        import piper_tts

        if not piper_tts.installed():
            raise RuntimeError("Giọng Piper chưa tải: làm một video bằng Piper là tool tự tải, sau đó nghe thử được.")
        out = out.with_suffix(".wav")
        piper_tts.speak(text, out, voice[6:])
    elif engine in ("edge", "edgeplus", "mix"):
        if not douyin_dub._edge_try(text, (voice, 0, 0), str(out), 3, 1.5):
            raise RuntimeError("Máy chủ Edge không trả âm thanh, thử lại sau ít giây.")
    elif engine == "gemini":
        kc = douyin_dub.load_ai_config() or {}
        if kc.get("provider") != "gemini" or not kc.get("key"):
            raise RuntimeError("Giọng Gemini cần API key Gemini (Cài đặt → Dịch bằng AI).")
        pcm = douyin_dub._gemini_tts_call(kc["key"], text, voice,
                                          {"models": list(douyin_dub.GEMINI_TTS_MODELS), "calls": 0, "key": kc["key"]})
        out = out.with_suffix(".wav")
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(douyin_dub.SR)
            w.writeframes(pcm.tobytes())
    elif engine == "eleven":
        import eleven_tts

        kc = eleven_tts.load_config()
        if not kc:
            raise RuntimeError("Chưa có API key ElevenLabs.")
        eleven_tts.speak({"key": kc["key"], "model": "flash"}, text, voice, out)
    else:
        raise RuntimeError("Bộ đọc không hỗ trợ nghe thử.")
    return out


def has(engine, ids, text=""):
    """Giọng nào đã có mẫu sẵn (để web đánh dấu "nghe ngay")."""
    text = " ".join((text or "").split())[:200] or SAMPLE
    return [v for v in ids if cached(engine, v, text)]
