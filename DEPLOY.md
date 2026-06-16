# Triển khai Chek_NVR lên server (Docker Compose)

Hướng dẫn đưa Chek_NVR lên server bằng Docker Compose (app FastAPI + PostgreSQL).

## 1. Yêu cầu server
- Docker Engine + plugin Docker Compose (`docker compose version`).
- Mở được mạng tới các NVR cần giám sát (HTTPS 443 — xem CLAUDE.md §4).

## 2. Đưa mã nguồn lên server
Copy thư mục dự án lên server (scp/rsync/git). KHÔNG mang theo file `.env` của máy
dev — file này đã bị `.gitignore`/`.dockerignore` loại. Trên server tạo `.env` riêng.

## 3. Cấu hình `.env`
```bash
cp .env.docker.example .env
```
Sửa `.env` và điền **bắt buộc**:
- `POSTGRES_PASSWORD` — mật khẩu DB mạnh, và cập nhật cùng giá trị trong `DATABASE_URL`.
- `ENCRYPTION_KEY` — khóa Fernet mã hóa mật khẩu NVR. Tạo:
  ```bash
  docker run --rm python:3.14-slim python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `SECRET_KEY` — ký cookie phiên đăng nhập. Tạo:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
- `COOKIE_SECURE=true` nếu chạy sau HTTPS; để `false` nếu truy cập HTTP thuần (LAN).

> ⚠️ Mất `ENCRYPTION_KEY` = không giải mã được mật khẩu NVR đã lưu. Hãy sao lưu khóa
> an toàn (vault/password manager), tách khỏi server.

## 4. Khởi động
```bash
docker compose up -d --build
```
Entrypoint tự: chờ DB sẵn sàng → `alembic upgrade head` (tạo/cập nhật bảng) → chạy app.

Kiểm tra:
```bash
docker compose ps
docker compose logs -f app
curl http://127.0.0.1:8080/health   # {"status":"ok"}
```

## 5. Tạo admin đầu tiên
```bash
docker compose exec app python -m scripts.manage_user add \
    --username admin --password "MatKhauManh" --role admin
```
Sau đó đăng nhập web và quản lý thêm user ở `/users`.

## 6. Truy cập & HTTPS (Caddy + CA nội bộ)
App chỉ lắng nghe `127.0.0.1:8080`; ra LAN đi qua service **Caddy** (đã có trong
`docker-compose.yml`) cấp TLS ở cổng 443. Vì chạy theo IP nội bộ (không domain
public) nên dùng **CA nội bộ tự cấp**, không dùng Let's Encrypt.

Các bước (chạy trên server):
```bash
# 1. Sinh CA nội bộ + cert server cho IP (mặc định 10.0.193.233).
#    SRV_IP=<ip-khac> ./scripts/gen_certs.sh   # nếu IP khác
chmod +x scripts/gen_certs.sh && ./scripts/gen_certs.sh

# 2. Bật cookie Secure (đã có HTTPS) trong .env
#    COOKIE_SECURE=true

# 3. Khởi động (Caddy mở 80->redirect, 443->app)
docker compose up -d

# 4. Kiểm tra (dùng CA vừa tạo)
curl --cacert tls/ca.crt https://10.0.193.233/health   # {"status":"ok"}
```

**Cài root CA lên máy client để hết cảnh báo** (làm 1 lần/máy): lấy file
`tls/ca.crt` từ server, cài vào *Trusted Root Certification Authorities*:
- Windows: nhấp đôi `ca.crt` → *Install Certificate* → *Local Machine* →
  *Place all certificates in the following store* → *Trusted Root Certification
  Authorities*. (Hoặc đẩy qua Group Policy cho cả miền.)
- Sau đó truy cập `https://10.0.193.233` không còn cảnh báo.

> Chạy lại `gen_certs.sh` chỉ cấp lại cert server, **giữ nguyên CA** → client đã
> cài `ca.crt` không phải cài lại. CA/cert nằm trong `tls/` (đã `.gitignore`).

## 7. Vận hành thường ngày
```bash
docker compose logs -f app              # xem log
docker compose restart app              # khởi động lại app
docker compose pull && docker compose up -d --build   # cập nhật code/ảnh
docker compose down                     # dừng (giữ dữ liệu trong volume pgdata)
```
Quét NVR (mỗi 180s) và dọn log cũ (03:00, giữ `LOG_RETENTION_DAYS` ngày) chạy tự động
trong app. Chạy thủ công 1 lượt: `docker compose exec app python -m scripts.manage_nvr scan`.

## 8. Sao lưu / phục hồi DB
```bash
# Backup
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > backup_$(date +%F).sql
# Restore
cat backup_YYYY-MM-DD.sql | docker compose exec -T db psql -U "$POSTGRES_USER" "$POSTGRES_DB"
```
Dữ liệu Postgres nằm ở volume `pgdata` (tồn tại qua `docker compose down`; chỉ mất khi
`docker compose down -v`).

## Lưu ý kiến trúc
- App chạy **1 tiến trình uvicorn** (không nhiều worker): APScheduler chạy in-process,
  nhiều worker sẽ nhân bản job quét. Muốn scale nhiều worker phải tách scheduler ra
  tiến trình/dịch vụ riêng.
- Migration chạy tự động lúc khởi động. Tạo migration mới (khi đổi model) ở môi trường
  dev: `alembic revision --autogenerate -m "..."` rồi commit file migration.
