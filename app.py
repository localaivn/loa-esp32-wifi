import os
import io
import json
import struct
import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly
from flask import Flask, request, render_template, jsonify, session
import requests

app = Flask(__name__)
app.secret_key = "loa-esp32-secret-key"

UPLOAD_FOLDER = "uploads"
CONFIG_FILE   = "config.json"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ============================================================
# CONFIG - lưu IP vào file json
# ============================================================
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"esp32_ip": "192.168.1.100"}

def save_config(data: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ============================================================
# XỬ LÝ WAV - optimize → raw PCM 16kHz mono 16-bit
# ============================================================
def to_float32(data: np.ndarray) -> np.ndarray:
    """Chuyển bất kỳ dtype → float32 trong khoảng [-1.0, 1.0]"""
    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        return data.astype(np.float32) / 2147483648.0
    elif data.dtype == np.uint8:
        return (data.astype(np.float32) - 128.0) / 128.0
    elif data.dtype == np.float32:
        return data
    elif data.dtype == np.float64:
        return data.astype(np.float32)
    else:
        return data.astype(np.float32)


def optimize_wav_to_pcm(input_bytes: bytes,
                         target_sr: int = 16000) -> bytes:
    """
    Chuyển WAV bất kỳ → raw PCM 16-bit mono 16kHz (không có header).
    Đây là format nhỏ nhất, ESP32 I2S phát trực tiếp.
    """
    buf = io.BytesIO(input_bytes)
    sr, data = wavfile.read(buf)

    audio = to_float32(data)

    # Stereo → Mono (trung bình 2 kênh)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    # Resample về target_sr nếu khác
    if sr != target_sr:
        from math import gcd
        g    = gcd(target_sr, sr)
        up   = target_sr // g
        down = sr // g
        audio = resample_poly(audio, up, down).astype(np.float32)

    # Normalize tránh clipping
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 0.95

    # Chuyển về int16 → bytes
    pcm = (audio * 32767).astype(np.int16)
    return pcm.tobytes()


def build_wav_header(pcm_data: bytes,
                     sample_rate: int,
                     channels: int = 1,
                     bits: int = 16) -> bytes:
    """Tạo WAV header 44 bytes chuẩn"""
    data_size   = len(pcm_data)
    byte_rate   = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + data_size,
        b'WAVE',
        b'fmt ',
        16,
        1,             # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b'data',
        data_size
    )


def optimize_wav_with_header(input_bytes: bytes,
                              target_sr: int = 16000) -> bytes:
    """Optimize + thêm WAV header → dùng khi upload lên LittleFS"""
    pcm = optimize_wav_to_pcm(input_bytes, target_sr)
    return build_wav_header(pcm, target_sr) + pcm


def get_wav_info(input_bytes: bytes) -> dict:
    buf = io.BytesIO(input_bytes)
    sr, data = wavfile.read(buf)
    channels = 1 if data.ndim == 1 else data.shape[1]
    duration = len(data) / sr
    return {
        "sample_rate" : sr,
        "channels"    : channels,
        "duration_sec": round(duration, 2),
        "size_kb"     : round(len(input_bytes) / 1024, 1),
        "dtype"       : str(data.dtype),
    }

# ============================================================
# ROUTES - PAGES
# ============================================================
@app.route("/")
def index():
    cfg = load_config()
    return render_template("index.html", esp32_ip=cfg["esp32_ip"])


# ============================================================
# ROUTES - API
# ============================================================
@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/set-ip", methods=["POST"])
def api_set_ip():
    cfg = load_config()
    ip  = request.json.get("ip", "").strip()
    if not ip:
        return jsonify({"error": "IP không hợp lệ"}), 400
    cfg["esp32_ip"] = ip
    save_config(cfg)
    return jsonify({"status": "ok", "ip": ip})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Phân tích file WAV, trả về thông tin gốc + sau optimize"""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Không có file"}), 400
    raw = f.read()
    try:
        info = get_wav_info(raw)
        pcm  = optimize_wav_to_pcm(raw)
        info["optimized_size_kb"] = round(len(pcm) / 1024, 1)
        info["optimized_sr"]      = 16000
        info["reduction_pct"]     = round((1 - len(pcm) / len(raw)) * 100, 1)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Optimize WAV rồi upload lên LittleFS của ESP32"""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Không có file"}), 400

    cfg      = load_config()
    esp_ip   = cfg["esp32_ip"]
    filename = request.form.get("filename", f.filename).strip()
    if not filename.endswith(".wav"):
        filename += ".wav"

    raw = f.read()
    try:
        optimized = optimize_wav_with_header(raw)
    except Exception as e:
        return jsonify({"error": f"Optimize thất bại: {e}"}), 500

    try:
        resp = requests.post(
            f"http://{esp_ip}/audio/upload",
            files={"file": (filename, optimized, "audio/wav")},
            timeout=30
        )
        return jsonify({
            "status"       : "ok",
            "filename"     : filename,
            "original_kb"  : round(len(raw) / 1024, 1),
            "uploaded_kb"  : round(len(optimized) / 1024, 1),
            "esp32_response": resp.json()
        })
    except Exception as e:
        return jsonify({"error": f"Upload lên ESP32 thất bại: {e}"}), 500


@app.route("/api/play", methods=["POST"])
def api_play():
    """Gọi ESP32 phát file đã có trên LittleFS"""
    cfg      = load_config()
    filename = request.json.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "Thiếu filename"}), 400
    try:
        resp = requests.get(
            f"http://{cfg['esp32_ip']}/play",
            params={"file": filename},
            timeout=5
        )
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/play-stream", methods=["POST"])
def api_play_stream():
    """Optimize WAV → raw PCM → stream thẳng tới ESP32 để phát ngay"""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Không có file"}), 400

    cfg = load_config()
    raw = f.read()

    try:
        pcm = optimize_wav_to_pcm(raw)   # raw PCM không header
    except Exception as e:
        return jsonify({"error": f"Optimize thất bại: {e}"}), 500

    try:
        resp = requests.post(
            f"http://{cfg['esp32_ip']}/play-stream",
            data=pcm,
            headers={"Content-Type": "application/octet-stream"},
            timeout=60
        )
        return jsonify({
            "status"       : "ok",
            "streamed_kb"  : round(len(pcm) / 1024, 1),
            "esp32_response": resp.json()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/list", methods=["GET"])
def api_list():
    """Lấy danh sách file trên ESP32"""
    cfg = load_config()
    try:
        resp = requests.get(
            f"http://{cfg['esp32_ip']}/audio/list",
            timeout=5
        )
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete", methods=["DELETE"])
def api_delete():
    """Xóa file trên ESP32"""
    cfg      = load_config()
    filename = request.json.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "Thiếu filename"}), 400
    try:
        resp = requests.delete(
            f"http://{cfg['esp32_ip']}/audio/delete",
            params={"file": filename},
            timeout=5
        )
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
