# Chek_NVR

Hệ thống kiểm tra, giám sát và thống kê đầu ghi NVR Hikvision: trạng thái NVR online/offline, trạng thái từng camera, dashboard tổng quan và lịch sử/báo cáo.

Xem [CLAUDE.md](CLAUDE.md) để biết kiến trúc, quy ước và lưu ý kỹ thuật (đặc biệt phần tích hợp Hikvision ISAPI).

## Cài đặt

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
pip install -r requirements.txt

cp .env.example .env              # rồi điền cấu hình
# Tạo ENCRYPTION_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Chạy

```bash
alembic upgrade head              # tạo bảng
uvicorn app.main:app --reload     # chạy app + scheduler
pytest tests/                     # chạy test
```

Dashboard: http://localhost:8000

## Đăng nhập & phân quyền

Dashboard yêu cầu đăng nhập (`AUTH_ENABLED=true`). Có 2 vai trò:

| Vai trò | Quyền |
|---|---|
| `admin` | Toàn quyền: CRUD NVR, "Kiểm tra ngay", quản lý người dùng (`/users`) |
| `viewer` | Chỉ xem: tổng quan, danh sách, chi tiết, báo cáo, cảnh báo |

Mọi user tự đổi mật khẩu tại `/account`. Phân quyền được chặn cả ở server
(`require_admin`) lẫn ẩn nút trên UI.

Tạo admin đầu tiên bằng CLI (sau đó admin tự quản lý user trên web ở `/users`):

```bash
python -m scripts.manage_user add --username admin --password "matkhau" --role admin
python -m scripts.manage_user list
python -m scripts.manage_user passwd --username admin --password "matkhaumoi"
```

Đặt `SECRET_KEY` trong `.env` để ký cookie phiên (nếu trống sẽ dùng tạm `ENCRYPTION_KEY`).
Khi phát triển có thể tạm tắt bằng `AUTH_ENABLED=false` (coi như admin, không bị chặn).

## Quản lý & bảo trì

```bash
python -m scripts.manage_nvr list                 # liệt kê NVR
python -m scripts.manage_nvr add --name ... --host ... --port 443 --https ...
python -m scripts.manage_nvr purge --days 90      # dọn log cũ thủ công (retention)
```

Báo cáo uptime: **/reports** (chọn 24 giờ / 7 / 30 / 90 ngày). Log cũ được dọn tự
động mỗi ngày lúc 03:00 theo `LOG_RETENTION_DAYS`.

## Bảo mật kết nối NVR (TLS)

NVR Hikvision thường dùng chứng chỉ tự ký nên mặc định `NVR_TLS_VERIFY=false`.
Để **chặn MITM mà không cần phân phối CA**, hãy *pin* SHA-256 fingerprint của từng
NVR (trường **TLS Fingerprint** trong form NVR, hoặc cột `tls_fingerprint`):

```bash
openssl s_client -connect HOST:443 </dev/null 2>/dev/null \
    | openssl x509 -noout -fingerprint -sha256
```

Khi đã pin, fingerprint sai sẽ bị đánh **Warning** kèm cảnh báo nghi ngờ MITM thay
vì cho phép kết nối. Nếu có CA nội bộ, đặt `NVR_CA_CERT_PATH` (ưu tiên hơn cờ verify).

## Triển khai lên server

Dùng Docker Compose (app + PostgreSQL). Xem chi tiết ở [DEPLOY.md](DEPLOY.md):

```bash
cp .env.docker.example .env     # điền ENCRYPTION_KEY, SECRET_KEY, mật khẩu DB
docker compose up -d --build
docker compose exec app python -m scripts.manage_user add \
    --username admin --password "..." --role admin
```

## Tech stack

FastAPI · httpx (async, DigestAuth) · SQLAlchemy 2.0 + asyncpg · PostgreSQL · Alembic · APScheduler · Jinja2 + HTMX + Bootstrap.
