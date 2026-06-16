# CLAUDE.md — Chek_NVR

Tài liệu nền tảng cho **Chek_NVR**: hệ thống kiểm tra, giám sát và thống kê các đầu ghi NVR Hikvision (online/offline) và trạng thái từng camera trong NVR.

> File này dành cho Claude Code (và lập trình viên) đọc trước khi làm việc trên dự án. Cập nhật khi kiến trúc/quy ước thay đổi.

---

## 1. Mục tiêu

Cho biết rõ tại mọi thời điểm:
- NVR nào đang **Online / Offline / Warning**.
- Camera nào đang **hoạt động / mất tín hiệu**.
- Tổng quan toàn hệ thống (tổng NVR, tổng camera, tỷ lệ uptime).
- Lịch sử lỗi & thống kê uptime để báo cáo, xử lý sự cố.
- Cảnh báo sớm khi có sự cố.
- Lọc theo khu vực / vị trí / trạng thái.

Quy mô tham chiếu: ~45 NVR, ~720 camera.

---

## 2. Tech stack

| Thành phần | Lựa chọn |
|---|---|
| Backend | **FastAPI** |
| HTTP client | **httpx** (async, `DigestAuth`) |
| ORM | **SQLAlchemy 2.0** (async) + **asyncpg** |
| Database | **PostgreSQL** |
| Migrations | **Alembic** |
| Lập lịch | **APScheduler** |
| Frontend | **Jinja2 + HTMX + Bootstrap** (server-side render, tích hợp trong FastAPI) |
| Config | **pydantic-settings** (đọc `.env`) |
| Test | **pytest** |

Quyết định kiến trúc đã chốt:
- Cảnh báo bản đầu: **chỉ Web dashboard notification** (Telegram/Email/Teams để mở rộng sau).
- MVP: **Collector + DB + Dashboard cơ bản** (tổng quan + danh sách + chi tiết). Báo cáo nâng cao & user auth làm sau.

---

## 3. Cấu trúc dự án

```
Chek_NVR/
├── app/
│   ├── main.py                # Khởi tạo FastAPI, mount routes, startup scheduler
│   ├── config.py              # Pydantic Settings (đọc .env)
│   ├── db/
│   │   ├── base.py            # SQLAlchemy engine/session (async)
│   │   └── models.py          # ORM models
│   ├── collector/
│   │   ├── isapi_client.py    # httpx DigestAuth, gọi ISAPI, parse XML
│   │   ├── checker.py         # Kiểm tra NVR (ping/port/API) + state machine
│   │   ├── camera_checker.py  # Lấy & chuẩn hóa trạng thái camera
│   │   └── scheduler.py       # APScheduler: lịch quét NVR & camera
│   ├── services/
│   │   ├── status_service.py  # Cập nhật trạng thái + ghi status_logs
│   │   └── alert_service.py   # Sinh alert (web notification) theo ngưỡng
│   ├── api/
│   │   └── routes.py          # API JSON cho dashboard/HTMX
│   ├── web/
│   │   ├── views.py           # Routes render Jinja2
│   │   ├── templates/         # dashboard, nvr_list, nvr_detail, alerts
│   │   └── static/            # Bootstrap, HTMX, css/js
│   └── schemas.py             # Pydantic schemas
├── alembic/                   # Migrations
├── tests/
├── .env.example
├── requirements.txt
├── README.md
└── CLAUDE.md
```

---

## 4. Tích hợp Hikvision ISAPI — LƯU Ý QUAN TRỌNG

Đây là phần dễ sai nhất. Ghi nhớ:

1. **Port:** ISAPI là REST HTTP chạy trên **80 (HTTP) / 443 (HTTPS)**.
   - Port **8000** = cổng SDK riêng của Hikvision (KHÔNG phải REST).
   - Port **554** = RTSP (streaming, không phải status).
   - → Chỉ gọi ISAPI trên 80/443. Coi 8000/554 là tín hiệu liveness phụ khi check port.
   - ⚠️ **Thực tế triển khai tại hệ thống này:** các NVR đang dùng **ISAPI qua HTTPS cổng 443**
     (không phải 80, càng không phải 8000=SDK). Khi thêm NVR mới trên web/CLI: đặt
     **Port = 443** và bật **"Dùng HTTPS"** (`use_https=True` / cờ `--https`).
2. **Xác thực:** ISAPI dùng **HTTP Digest auth**, KHÔNG phải Basic.
   - Dùng `httpx.AsyncClient(auth=httpx.DigestAuth(user, pwd))`.
   - HTTP 401 → coi là **Auth Error** (sai tài khoản/mật khẩu), không phải offline.
3. **Phản hồi là XML** (mặc định), namespace `urn:psialliance-org` / `http://www.hikvision.com/ver20/XMLSchema`. Cần parse XML và xử lý namespace cẩn thận.
4. **Endpoint chính sử dụng:**
   | Endpoint | Mục đích |
   |---|---|
   | `GET /ISAPI/System/deviceInfo` | Model, serial, firmware → xác nhận NVR Online |
   | `GET /ISAPI/System/status` | Uptime, tình trạng hệ thống |
   | `GET /ISAPI/ContentMgmt/InputProxy/channels` | Danh sách kênh/camera |
   | `GET /ISAPI/ContentMgmt/InputProxy/channels/status` | **Trạng thái online/offline từng camera** (endpoint cốt lõi) |

---

## 5. Logic kiểm tra (nhiều lớp)

**Không chỉ dựa vào ping** — nhiều thiết bị chặn ICMP nhưng web/API vẫn chạy.

### NVR (theo thứ tự lớp)
1. Ping IP.
2. Check TCP port 80/443.
3. Gọi `GET /ISAPI/System/deviceInfo` với DigestAuth.

Ánh xạ trạng thái:
- API trả về hợp lệ → **Online**.
- HTTP 401 → **Auth Error**.
- Ping/port OK nhưng API lỗi/timeout/chậm → **Warning**.
- Không ping được, không mở port, không gọi được API → **Network Error / Offline** (sau N lần xác nhận).

### Camera
- Gọi `/channels` (danh sách) + `/channels/status` (trạng thái) → parse XML → map về trạng thái chuẩn hóa.

### Chống "flapping" (BẮT BUỘC)
- State machine dùng **bộ đếm xác nhận**: chỉ chuyển sang Offline sau **N lần thất bại liên tiếp** (mặc định N=3). Tránh báo động giả do blip mạng ngắn.

### Concurrency
- Quét bằng `asyncio` + `httpx.AsyncClient`.
- Giới hạn song song bằng `asyncio.Semaphore` (chia batch) để tránh quá tải mạng khi số NVR lớn.

---

## 6. Trạng thái chuẩn hóa (enum — dùng nhất quán toàn hệ thống)

**NVR:** `Online` · `Offline` · `Warning` · `Auth Error` · `Network Error`

**Camera:** `Online` · `Offline` · `Disabled` · `No Signal` · `Auth Failed` · `Unknown`

---

## 7. Database (PostgreSQL)

| Bảng | Nội dung chính |
|---|---|
| `nvr_devices` | name, ip/domain, http_port, use_https, username, **password_enc**, location, area, model, channel_count, note |
| `camera_channels` | nvr_id (FK), channel_no, name, camera_ip, camera_type, location, current_status, last_checked_at, last_error |
| `nvr_status_logs` | nvr_id (FK), status, response_time_ms, error_msg, checked_at — **index (nvr_id, checked_at)** |
| `camera_status_logs` | camera_id (FK), status, error_msg, checked_at — **index (camera_id, checked_at)** |
| `alerts` | type, severity, nvr_id, camera_id, message, status (open/resolved), created_at, resolved_at |
| `users` | (skeleton, để sau) username, password_hash, role |

Lưu ý:
- **Mật khẩu NVR:** KHÔNG lưu plaintext. Mã hóa (Fernet) khi lưu hoặc lấy từ env. Cột là `password_enc`.
- **Tăng trưởng log:** 720 camera × mỗi 5 phút ≈ ~200k dòng/ngày → cần index + **chính sách retention** (xóa/gộp log cũ).
- **Timestamp:** lưu UTC (timezone-aware), hiển thị giờ local ở dashboard.

---

## 8. Tần suất quét

- NVR online/offline: mỗi **1–5 phút**.
- Camera status: mỗi **5–10 phút**.
- Ghi `*_status_logs` mỗi lần quét.
- Chia batch khi số NVR lớn.

---

## 9. Dashboard (Jinja2 + HTMX)

- **Tổng quan:** tổng NVR, online/offline/warning, tổng camera, online/offline, tỷ lệ uptime toàn hệ thống.
  - Tự làm mới mỗi 30s qua HTMX polling (`#dashboard-body` swap `innerHTML`).
  - Khi có NVR/camera offline: hiệu ứng cảnh báo động — card glow (`.alert-glow`) + icon/badge nhấp nháy (`.blink`), CSS định nghĩa trong `base.html`.
  - Nút **"Tắt/Bật cảnh báo động"** (id `toggle-blink`, đặt NGOÀI `#dashboard-body` để không bị swap): toggle class `.alerts-muted` trên `#dashboard-body` (phần tử tồn tại xuyên suốt polling) → CSS tắt animation cho con; lưu lựa chọn vào `localStorage['nvr_alerts_muted']`.
- **Danh sách NVR:** tên, IP, vị trí, trạng thái, số camera online/offline, lần kiểm tra cuối, lỗi. Lọc theo khu vực/trạng thái.
- **Chi tiết NVR:** thông tin thiết bị (model/serial/firmware/uptime), danh sách camera + trạng thái, lịch sử lỗi gần đây.
- **Cảnh báo:** danh sách alert, auto-refresh qua HTMX polling.

---

## 10. Cảnh báo (bản đầu — chỉ web)

`alert_service` sinh alert khi:
- NVR offline > N lần kiểm tra liên tiếp.
- Camera offline > 10 phút.
- NVR phản hồi chậm > 5s.
- Lỗi xác thực (Auth Error).
- NVR online lại sau khi offline (recovery).

Bản đầu chỉ hiển thị trên trang Cảnh báo. Telegram / Email / Teams để mở rộng sau (giữ `alert_service` đủ tổng quát để cắm thêm kênh).

---

## 11. Lệnh thường dùng

```bash
# Cài đặt
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Migrations
alembic upgrade head
alembic revision --autogenerate -m "message"

# Chạy app (dev)
uvicorn app.main:app --reload

# Test
pytest tests/
```

Cấu hình qua `.env` (xem `.env.example`): DB URL, khóa mã hóa (Fernet), tần suất quét, ngưỡng cảnh báo, timeout.

---

## 12. Quy ước code

- **Async-first:** I/O (DB, HTTP) dùng async. Không gọi blocking trong event loop.
- Dùng đúng **enum trạng thái** ở Mục 6 ở mọi nơi (DB, service, template) — không hardcode string rời rạc.
- Logic kiểm tra tách khỏi I/O để **test được bằng mock** (mock ISAPI XML response).
- Không commit secrets; `.env` không vào git, chỉ commit `.env.example`.
- Timestamp luôn UTC timezone-aware trong code/DB.

---

## 13. Lộ trình triển khai

1. ✅ `CLAUDE.md` (file này).
2. Skeleton dự án + `requirements.txt` + `.env.example` + `config.py`.
3. ORM models + Alembic + migration đầu.
4. `isapi_client.py` (DigestAuth, parse XML) — test với 1–2 NVR thật.
5. `checker.py` (NVR multi-layer + state machine) + `camera_checker.py`.
6. `scheduler.py` + `status_service.py` (quét định kỳ, ghi log, cập nhật trạng thái).
7. Dashboard: tổng quan → danh sách → chi tiết.
8. `alert_service` + trang cảnh báo.
9. Chạy thử 3–5 NVR → mở rộng → tối ưu batch/retention.
