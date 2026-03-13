# loa-esp32-wifi

Web app Flask để quản lý âm thanh cho ESP32 qua Wi‑Fi: upload WAV, phát ngay (stream), quản lý file trên LittleFS và tạo giọng nói tiếng Việt bằng Piper TTS.

## Tính năng chính

- Cấu hình IP ESP32 trên giao diện web (lưu vào `config.json`).
- Phân tích file WAV trước khi gửi (sample rate, channels, duration, dung lượng...).
- Tự động tối ưu audio về chuẩn ESP32 đang dùng:
  - `22050 Hz`
  - `mono`
  - `16-bit PCM`
- Upload file WAV đã tối ưu lên ESP32 (`/audio/upload`).
- Stream phát ngay:
  - ưu tiên gọi endpoint `/play-stream`
  - fallback sang `upload -> play -> delete` nếu stream trực tiếp lỗi.
- Quản lý file trên ESP32:
  - xem danh sách (`/audio/list`)
  - phát file (`/play?file=...`)
  - xóa file (`/audio/delete`).
- Text-to-Speech bằng Piper:
  - tạo file WAV
  - tạo rồi upload
  - tạo rồi stream phát ngay.

---

## Yêu cầu

- Python 3.10+ (khuyến nghị).
- ESP32 đã chạy firmware có các API audio tương thích (xem mục **API ESP32 cần có**).
- (Tuỳ chọn) Piper TTS đã cài trên máy chạy server.

Cài dependencies:

```bash
pip install -r requirements.txt
```

---

## Chạy ứng dụng

```bash
python3 app.py
```

Mặc định server chạy tại:

- `http://0.0.0.0:5000`
- mở trình duyệt tại `http://<IP-máy-chạy-server>:5000`

Lần đầu chạy, app tự tạo thư mục `uploads/` và đọc IP ESP32 từ `config.json`.

---

## Cấu hình ESP32 IP

Có 2 cách:

1. Trên giao diện web: nhập IP và bấm **Lưu IP**.
2. Sửa trực tiếp file `config.json`:

```json
{
  "esp32_ip": "192.168.31.207"
}
```

---

## Piper TTS (tuỳ chọn)

App gọi Piper qua script:

- `~/piper-tts-cpu/main.py`

Nếu file này không tồn tại, tính năng TTS sẽ báo lỗi và các chức năng còn lại vẫn dùng bình thường.

Lưu ý:

- Text TTS giới hạn tối đa `1000` ký tự/lần.
- Tên file đầu ra nếu không nhập sẽ tự sinh theo timestamp.

---

## Luồng xử lý audio

Mọi audio trước khi upload/stream đều được chuẩn hoá:

1. Đọc WAV bất kỳ (nhiều dtype khác nhau).
2. Chuyển về `float32` trong khoảng `[-1, 1]`.
3. Nếu stereo thì trộn về mono.
4. Resample về `22050 Hz` (nếu cần).
5. Normalize mức đỉnh ~95% để tránh clipping.
6. Chuyển sang `int16 PCM`.
7. Khi upload/stream lên ESP32, app thêm WAV header chuẩn 44 bytes.

---

## API backend của app Flask

### Cấu hình

- `GET /api/config`: lấy cấu hình hiện tại + thông tin target audio + trạng thái Piper.
- `POST /api/set-ip`: lưu IP ESP32.

Body mẫu:

```json
{ "ip": "192.168.1.100" }
```

### WAV upload/stream

- `POST /api/analyze` (multipart): phân tích WAV đầu vào.
- `POST /api/upload` (multipart): optimize + upload lên ESP32.
- `POST /api/play-stream` (multipart): optimize + phát ngay.

### Quản lý file ESP32

- `GET /api/list`
- `POST /api/play` với JSON `{ "filename": "xxx.wav" }`
- `DELETE /api/delete` với JSON `{ "filename": "xxx.wav" }`

### Text-to-Speech

- `POST /api/tts/generate`
- `POST /api/tts/upload`
- `POST /api/tts/stream`

Body mẫu:

```json
{
  "text": "Xin chào, đây là thông báo thử nghiệm.",
  "filename": "thongbao.wav"
}
```

---

## API ESP32 cần có

Để app hoạt động đầy đủ, firmware ESP32 nên hỗ trợ các endpoint:

- `POST /audio/upload` (multipart file WAV)
- `GET /audio/list`
- `DELETE /audio/delete?file=<name>`
- `GET /play?file=<name>`
- `POST /play-stream` (multipart file WAV)

Nếu không có `/play-stream`, app sẽ tự fallback sang upload + play tạm thời.

---

## Cấu trúc thư mục

```text
.
├── app.py
├── config.json
├── requirements.txt
├── templates/
│   └── index.html
└── uploads/
```

---

## Gợi ý xử lý lỗi nhanh

- **Không kết nối được ESP32**
  - Kiểm tra IP trong UI hoặc `config.json`.
  - Đảm bảo ESP32 và máy chạy Flask cùng mạng LAN.
- **TTS báo không tìm thấy Piper**
  - Kiểm tra đường dẫn `~/piper-tts-cpu/main.py`.
- **Upload/stream lỗi timeout**
  - Kiểm tra endpoint firmware ESP32 có đúng như mục trên.
  - Thử file WAV ngắn trước để kiểm tra đường truyền.

---

## License

Dự án nội bộ/demo. Bạn có thể bổ sung license cụ thể nếu cần (MIT/Apache-2.0...).
