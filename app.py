import os
import io
import json
import struct
import uuid
import subprocess
import time
from datetime import datetime
import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly
from flask import Flask, request, render_template, jsonify, session
import requests
from requests.exceptions import RequestException

app = Flask(__name__)
app.secret_key = "loa-esp32-secret-key"

UPLOAD_FOLDER = "uploads"
CONFIG_FILE   = "config.json"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ============================================================
# ESP32 AUDIO CONFIG - Config #3 từ test
# ============================================================
TARGET_SAMPLE_RATE = 22050  # 22.05kHz - config #3 phát tốt nhất
TARGET_CHANNELS    = 1      # Mono
TARGET_BITS        = 16     # 16-bit

# ============================================================
# PIPER TTS CONFIG
# ============================================================
PIPER_SCRIPT_PATH = os.path.expanduser("~/piper-tts-cpu/main.py")
PIPER_OUTPUT_DIR  = os.path.expanduser("~/piper-tts-cpu")

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
# XỬ LÝ WAV - optimize → 22.05kHz mono 16-bit
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
                        target_sr: int = TARGET_SAMPLE_RATE,
                        normalize: bool = True) -> tuple[bytes, int]:
    """
    Chuyển WAV bất kỳ → raw PCM 16-bit mono 22.05kHz (không có header).
    - Luôn resample về 22.05kHz để khớp với ESP32 config #3
    - Normalize để tránh méo tiếng
    """
    buf = io.BytesIO(input_bytes)
    sr, data = wavfile.read(buf)

    audio = to_float32(data)

    # Stereo → Mono (trung bình 2 kênh)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    # Resample về 22.05kHz nếu khác
    if sr != target_sr:
        from math import gcd
        g    = gcd(target_sr, sr)
        up   = target_sr // g
        down = sr // g
        audio = resample_poly(audio, up, down).astype(np.float32)
        sr = target_sr

    # Normalize để tránh clipping
    if normalize:
        peak = np.max(np.abs(audio))
        if peak > 0:
            audio = audio / peak * 0.95

    # Chuyển về int16 → bytes
    pcm = (audio * 32767).astype(np.int16)
    return pcm.tobytes(), sr


def build_wav_header(pcm_data: bytes,
                     sample_rate: int = TARGET_SAMPLE_RATE,
                     channels: int = TARGET_CHANNELS,
                     bits: int = TARGET_BITS) -> bytes:
    """Tạo WAV header 44 bytes chuẩn cho ESP32"""
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
                             target_sr: int = TARGET_SAMPLE_RATE,
                             normalize: bool = True) -> tuple[bytes, int]:
    """Optimize + thêm WAV header → dùng khi upload lên LittleFS"""
    pcm, sr = optimize_wav_to_pcm(input_bytes, target_sr, normalize)
    return build_wav_header(pcm, sr) + pcm, sr


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
# TEXT-TO-SPEECH - PIPER TTS
# ============================================================
def text_to_speech(text: str, output_filename: str = None) -> dict:
    """
    Chuyển text → WAV bằng Piper TTS
    Returns: {
        "status": "ok" | "error",
        "filepath": "/path/to/file.wav",
        "filename": "file.wav",
        "error": "..." (nếu lỗi)
    }
    """
    # Kiểm tra Piper script có tồn tại không
    if not os.path.exists(PIPER_SCRIPT_PATH):
        return {
            "status": "error",
            "error": f"Piper script không tồn tại: {PIPER_SCRIPT_PATH}"
        }
    
    # Tạo tên file nếu không có
    if not output_filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"tts_{timestamp}.wav"
    
    # Đảm bảo có đuôi .wav
    if not output_filename.endswith(".wav"):
        output_filename += ".wav"
    
    output_path = os.path.join(PIPER_OUTPUT_DIR, output_filename)
    
    # Xây dựng command - QUAN TRỌNG: phải dùng shell=True và format đúng
    cmd = f'python3 "{PIPER_SCRIPT_PATH}" "{text}" -o "{output_path}"'
    
    try:
        # Chạy Piper TTS với shell=True
        result = subprocess.run(
            cmd,
            shell=True,  # ← QUAN TRỌNG: bật shell mode
            capture_output=True,
            text=True,
            timeout=30,
            cwd=PIPER_OUTPUT_DIR  # ← Chạy trong thư mục Piper
        )
        
        # Log để debug
        print("=" * 60)
        print("🎤 PIPER TTS DEBUG")
        print("=" * 60)
        print(f"Command: {cmd}")
        print(f"Return code: {result.returncode}")
        print(f"STDOUT:\n{result.stdout}")
        print(f"STDERR:\n{result.stderr}")
        print("=" * 60)
        
        # Kiểm tra có lỗi không
        if result.returncode != 0:
            return {
                "status": "error",
                "error": f"Piper TTS exit code {result.returncode}: {result.stderr}"
            }
        
        # Kiểm tra file đã tạo thành công
        if not os.path.exists(output_path):
            return {
                "status": "error",
                "error": f"File WAV không được tạo. STDOUT: {result.stdout}, STDERR: {result.stderr}"
            }
        
        file_size = os.path.getsize(output_path)
        
        return {
            "status": "ok",
            "filepath": output_path,
            "filename": output_filename,
            "size_kb": round(file_size / 1024, 1),
            "stdout": result.stdout,
            "stderr": result.stderr
        }
        
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "error": "Timeout: Piper TTS mất quá 30 giây"
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Exception: {str(e)}"
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
    cfg = load_config()
    cfg["target_config"] = {
        "sample_rate": TARGET_SAMPLE_RATE,
        "channels": TARGET_CHANNELS,
        "bits": TARGET_BITS
    }
    cfg["piper_available"] = os.path.exists(PIPER_SCRIPT_PATH)
    cfg["piper_path"] = PIPER_SCRIPT_PATH
    return jsonify(cfg)


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
        pcm, optimized_sr = optimize_wav_to_pcm(raw)
        info["optimized_size_kb"] = round(len(pcm) / 1024, 1)
        info["optimized_sr"]      = optimized_sr
        info["optimized_channels"] = TARGET_CHANNELS
        info["optimized_bits"]     = TARGET_BITS
        info["reduction_pct"]     = round((1 - len(pcm) / len(raw)) * 100, 1)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Optimize WAV về 22.05kHz mono 16-bit rồi upload lên LittleFS của ESP32"""
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
        optimized, out_sr = optimize_wav_with_header(raw)
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
            "sample_rate"  : out_sr,
            "channels"     : TARGET_CHANNELS,
            "bits"         : TARGET_BITS,
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
    """Phát ngay: optimize về 22.05kHz mono 16-bit, ưu tiên stream trực tiếp"""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Không có file"}), 400

    cfg = load_config()
    raw = f.read()

    try:
        wav_payload, out_sr = optimize_wav_with_header(raw)
    except Exception as e:
        return jsonify({"error": f"Optimize thất bại: {e}"}), 500

    stream_filename = f"stream_{uuid.uuid4().hex[:8]}.wav"

    try:
        # Cách 1: stream trực tiếp tới endpoint play-stream
        resp = requests.post(
            f"http://{cfg['esp32_ip']}/play-stream",
            files={"file": (stream_filename, wav_payload, "audio/wav")},
            timeout=60
        )
        resp.raise_for_status()

        payload = resp.json() if resp.content else {"status": "ok"}
        return jsonify({
            "status"       : "ok",
            "mode"         : "direct-stream",
            "sample_rate"  : out_sr,
            "channels"     : TARGET_CHANNELS,
            "bits"         : TARGET_BITS,
            "streamed_kb"  : round(len(wav_payload) / 1024, 1),
            "esp32_response": payload
        })
    except Exception:
        # Cách 2 (an toàn): upload file tạm, gọi play ngay, rồi dọn dẹp
        try:
            upload_resp = requests.post(
                f"http://{cfg['esp32_ip']}/audio/upload",
                files={"file": (stream_filename, wav_payload, "audio/wav")},
                timeout=60
            )
            upload_resp.raise_for_status()

            play_resp = requests.get(
                f"http://{cfg['esp32_ip']}/play",
                params={"file": stream_filename},
                timeout=8
            )
            play_resp.raise_for_status()

            # Dọn file tạm (best effort)
            try:
                requests.delete(
                    f"http://{cfg['esp32_ip']}/audio/delete",
                    params={"file": stream_filename},
                    timeout=5
                )
            except RequestException:
                pass

            return jsonify({
                "status"       : "ok",
                "mode"         : "fallback-upload-play",
                "sample_rate"  : out_sr,
                "channels"     : TARGET_CHANNELS,
                "bits"         : TARGET_BITS,
                "streamed_kb"  : round(len(wav_payload) / 1024, 1),
                "esp32_response": {
                    "upload": upload_resp.json() if upload_resp.content else {"status": "ok"},
                    "play": play_resp.json() if play_resp.content else {"status": "ok"}
                }
            })
        except Exception as e:
            return jsonify({"error": f"Stream thất bại (direct + fallback): {e}"}), 500


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
# ROUTES - TEXT-TO-SPEECH
# ============================================================
@app.route("/api/tts/generate", methods=["POST"])
def api_tts_generate():
    """
    Chuyển text → WAV bằng Piper TTS
    Body: {
        "text": "Xin chào",
        "filename": "hello.wav" (optional, mặc định dùng timestamp)
    }
    """
    data = request.json
    text = data.get("text", "").strip()
    filename = data.get("filename", "").strip()
    
    if not text:
        return jsonify({"error": "Thiếu text"}), 400
    
    # Giới hạn độ dài text
    if len(text) > 1000:
        return jsonify({"error": "Text quá dài (max 1000 ký tự)"}), 400
    
    result = text_to_speech(text, filename)
    
    if result["status"] == "error":
        return jsonify(result), 500
    
    return jsonify(result)


@app.route("/api/tts/upload", methods=["POST"])
def api_tts_upload():
    """
    TTS → optimize → upload lên ESP32
    Body: {
        "text": "Xin chào",
        "filename": "hello.wav" (optional)
    }
    """
    data = request.json
    text = data.get("text", "").strip()
    filename = data.get("filename", "").strip()
    
    if not text:
        return jsonify({"error": "Thiếu text"}), 400
    
    # Bước 1: TTS
    tts_result = text_to_speech(text, filename)
    if tts_result["status"] == "error":
        return jsonify(tts_result), 500
    
    tts_filepath = tts_result["filepath"]
    tts_filename = tts_result["filename"]
    
    # Bước 2: Đọc file WAV
    try:
        with open(tts_filepath, "rb") as f:
            raw = f.read()
    except Exception as e:
        return jsonify({"error": f"Không đọc được file TTS: {e}"}), 500
    
    # Bước 3: Optimize
    try:
        optimized, out_sr = optimize_wav_with_header(raw)
    except Exception as e:
        return jsonify({"error": f"Optimize thất bại: {e}"}), 500
    
    # Bước 4: Upload lên ESP32
    cfg = load_config()
    try:
        resp = requests.post(
            f"http://{cfg['esp32_ip']}/audio/upload",
            files={"file": (tts_filename, optimized, "audio/wav")},
            timeout=30
        )
        
        return jsonify({
            "status"       : "ok",
            "text"         : text,
            "filename"     : tts_filename,
            "tts_size_kb"  : tts_result["size_kb"],
            "uploaded_kb"  : round(len(optimized) / 1024, 1),
            "sample_rate"  : out_sr,
            "channels"     : TARGET_CHANNELS,
            "bits"         : TARGET_BITS,
            "esp32_response": resp.json()
        })
    except Exception as e:
        return jsonify({"error": f"Upload lên ESP32 thất bại: {e}"}), 500


@app.route("/api/tts/stream", methods=["POST"])
def api_tts_stream():
    """
    TTS → optimize → stream phát ngay trên ESP32
    Body: {
        "text": "Xin chào",
        "filename": "hello.wav" (optional)
    }
    """
    data = request.json
    text = data.get("text", "").strip()
    filename = data.get("filename", "").strip()
    
    if not text:
        return jsonify({"error": "Thiếu text"}), 400
    
    # Bước 1: TTS
    tts_result = text_to_speech(text, filename)
    if tts_result["status"] == "error":
        return jsonify(tts_result), 500
    
    tts_filepath = tts_result["filepath"]
    tts_filename = tts_result["filename"]
    
    # Bước 2: Đọc file WAV
    try:
        with open(tts_filepath, "rb") as f:
            raw = f.read()
    except Exception as e:
        return jsonify({"error": f"Không đọc được file TTS: {e}"}), 500
    
    # Bước 3: Optimize
    try:
        wav_payload, out_sr = optimize_wav_with_header(raw)
    except Exception as e:
        return jsonify({"error": f"Optimize thất bại: {e}"}), 500
    
    # Bước 4: Stream lên ESP32
    cfg = load_config()
    stream_filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
    
    try:
        # Thử stream trực tiếp
        resp = requests.post(
            f"http://{cfg['esp32_ip']}/play-stream",
            files={"file": (stream_filename, wav_payload, "audio/wav")},
            timeout=60
        )
        resp.raise_for_status()
        
        payload = resp.json() if resp.content else {"status": "ok"}
        return jsonify({
            "status"       : "ok",
            "mode"         : "direct-stream",
            "text"         : text,
            "filename"     : tts_filename,
            "tts_size_kb"  : tts_result["size_kb"],
            "streamed_kb"  : round(len(wav_payload) / 1024, 1),
            "sample_rate"  : out_sr,
            "channels"     : TARGET_CHANNELS,
            "bits"         : TARGET_BITS,
            "esp32_response": payload
        })
    except Exception:
        # Fallback: upload + play + delete
        try:
            upload_resp = requests.post(
                f"http://{cfg['esp32_ip']}/audio/upload",
                files={"file": (stream_filename, wav_payload, "audio/wav")},
                timeout=60
            )
            upload_resp.raise_for_status()
            
            play_resp = requests.get(
                f"http://{cfg['esp32_ip']}/play",
                params={"file": stream_filename},
                timeout=8
            )
            play_resp.raise_for_status()
            
            # Dọn file tạm
            try:
                requests.delete(
                    f"http://{cfg['esp32_ip']}/audio/delete",
                    params={"file": stream_filename},
                    timeout=5
                )
            except RequestException:
                pass
            
            return jsonify({
                "status"       : "ok",
                "mode"         : "fallback-upload-play",
                "text"         : text,
                "filename"     : tts_filename,
                "tts_size_kb"  : tts_result["size_kb"],
                "streamed_kb"  : round(len(wav_payload) / 1024, 1),
                "sample_rate"  : out_sr,
                "channels"     : TARGET_CHANNELS,
                "bits"         : TARGET_BITS,
                "esp32_response": {
                    "upload": upload_resp.json() if upload_resp.content else {"status": "ok"},
                    "play": play_resp.json() if play_resp.content else {"status": "ok"}
                }
            })
        except Exception as e:
            return jsonify({"error": f"Stream thất bại: {e}"}), 500


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🎵 ESP32 Audio Server - Optimized for Config #3")
    print("=" * 60)
    print(f"   Target Sample Rate: {TARGET_SAMPLE_RATE} Hz")
    print(f"   Target Channels:    {TARGET_CHANNELS} (Mono)")
    print(f"   Target Bits:        {TARGET_BITS} bit")
    print("=" * 60)
    print(f"   Piper TTS:          {'✅ Available' if os.path.exists(PIPER_SCRIPT_PATH) else '❌ Not Found'}")
    print(f"   Piper Path:         {PIPER_SCRIPT_PATH}")
    print("=" * 60)
    print("   Server running on http://0.0.0.0:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True)
