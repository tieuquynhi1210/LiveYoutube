# LiveYoutube

Phần mềm phát **file video / playlist** lên **nhiều kênh YouTube Live cùng lúc** (kể cả 4K), thay cho việc live từ camera. Viết bằng Python + PySide6, dùng FFmpeg làm engine.

## Nguyên lý

Kiến trúc **mỗi kênh một tiến trình encode độc lập**:

- Mỗi kênh: đọc playlist → chuẩn hóa kích thước/fps → **encode** bằng GPU (NVENC/QSV/AMF) hoặc CPU → đẩy **thẳng RTMP** lên kênh YouTube tương ứng (`-f flv`).

Nhờ tách tiến trình, có thể **thêm / xoá / tạm dừng / phát tiếp từng kênh ngay khi đang live** mà không ảnh hưởng kênh khác; mỗi kênh có watchdog tự nối lại khi rớt, tự lặp playlist, và (playlist 1 clip) tự phát tiếp từ chỗ dừng. Encode thẳng RTMP nên **YouTube nhận chắc chắn, độ trễ thấp**. Đổi lại mỗi kênh encode riêng nên **tốn GPU theo số kênh** (RTX 5060 kham tốt vài kênh 1080p/1440p; 4K thì ít kênh hơn). Giới hạn còn lại là **băng thông upload** (≈ số kênh × bitrate) và số phiên NVENC (~8 trên GeForce).

Xử lý playlist: **1 clip** dùng concat demuxer (lặp mượt, tua tiếp được); **nhiều clip** dùng concat *filter* (giải mã từng clip đúng codec) để không bị màn hình đen ở điểm chuyển khi các clip khác codec (H.264/HEVC…).

## Yêu cầu

- **Python 3.11+**
- **FFmpeg** (bản có NVENC/QSV/AMF nếu muốn encode bằng GPU). Cài sẵn trên PATH,
  hoặc đặt biến môi trường `LIVEYT_FFMPEG` trỏ tới thư mục/file ffmpeg,
  hoặc bỏ `ffmpeg.exe`/`ffprobe.exe` vào `resources/ffmpeg/`.
- **Phát 4K:** GPU có NVENC (NVIDIA GTX 1660+/RTX) và mạng upload ~25 Mbps mỗi kênh.

## Cài đặt & chạy

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.main
```

## Cách dùng

1. **Thêm video** vào playlist (kéo thứ tự bằng nút ▲▼).
2. **Thêm kênh**: mỗi dòng nhập tên gợi nhớ + **stream key** lấy từ YouTube Studio
   (Tạo → Phát trực tiếp → Khóa luồng). Bỏ tick để tạm tắt một kênh.
3. Chọn **chất lượng** (4K/1440p/1080p) và **encoder** (để *Tự động* là tối ưu).
4. Xem **ước tính băng thông** để chắc mạng đủ tải.
5. Bấm **Bắt đầu phát**. Theo dõi fps/bitrate/thời gian live ở panel Giám sát.

> ⚠️ **Lưu ý chính sách:** phát nội dung y hệt lên nhiều kênh có thể vi phạm quy định
> *reused/duplicate content* của YouTube. Dùng cho các kênh cùng chủ / mục đích hợp lệ.

## Cấu trúc mã

```
src/
  main.py                  điểm khởi động
  core/
    ffmpeg_locator.py      tìm ffmpeg/ffprobe
    encoder_detector.py    dò NVENC/QSV/AMF/CPU
    presets.py             preset chất lượng
    models.py              Channel, StreamConfig
    playlist_manager.py    sinh file concat, đọc media
    ffmpeg_command.py      dựng lệnh FFmpeg + tee đa kênh
    ffmpeg_parser.py       parse -progress
    stream_controller.py   QProcess quản lý FFmpeg + auto-restart
  ui/main_window.py        giao diện chính
  config/store.py          lưu cấu hình JSON
```

## Đóng gói .exe (sau này)

```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --name LiveYoutube ^
  --add-data "resources;resources" src/main.py
```
