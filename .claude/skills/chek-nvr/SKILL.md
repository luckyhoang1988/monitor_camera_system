---
name: chek-nvr
description: Playbook bắt buộc cho mọi việc trên dự án Chek_NVR (giám sát NVR Hikvision) — quy ước code, ISAPI, test, commit và quy trình deploy production. Gọi skill này TRƯỚC khi bắt đầu bất kỳ task nào (sửa code, thêm tính năng, fix bug, deploy).
---

# Chek_NVR — Playbook

Gọi skill này **đầu mỗi task** rồi mới làm. Nền tảng kiến trúc/đầy đủ ở [CLAUDE.md](../../../CLAUDE.md); skill này là checklist hành động + các quirk dễ sai.

## 0. Định hướng trước khi code
- Đọc CLAUDE.md (kiến trúc, §4 ISAPI, §6 enum trạng thái, §7 DB) và các memory liên quan (`prod-deployment`, `storage-monitoring`, `deploy-after-push`).
- Tách **logic thuần** khỏi **I/O** để test bằng mock (mẫu: `camera_checker`, `storage_checker`). Async-first, không blocking trong event loop.
- Dùng đúng **enum** ở CLAUDE.md §6, không hardcode chuỗi trạng thái rời rạc. Timestamp UTC timezone-aware.

## 1. ISAPI Hikvision — quirk dễ sai
- ISAPI chạy HTTPS **443** (KHÔNG phải 8000=SDK), **Digest auth**, phản hồi **XML có namespace** → parse bằng `local_name()`/`_find_text()`/`_child_text()` trong [isapi_client.py](../../../app/collector/isapi_client.py).
- HTTP 401 = Auth Error (KHÔNG retry). Endpoint phụ thiếu (status/SMART) → nuốt lỗi, để None (best-effort), KHÔNG coi là NVR lỗi.
- NVR thường **chặn ICMP** → multi-layer check (ping/port/API), đừng chỉ dựa ping.
- **Storage RAID:** `<hddList>` có cả volume ảo (RW) + đĩa vật lý (RO) trùng id; khay trống = `notexist`. Chi tiết: memory `storage-monitoring`.

## 2. Trước khi commit
```bash
ruff check app/ tests/              # CI chặn nếu lỗi (cấu hình ở pyproject.toml)
python -m pytest tests/ -q          # PHẢI xanh toàn bộ
python -m alembic heads             # PHẢI 1 head duy nhất (nếu thêm migration)
python -c "import app.main"         # smoke import (bắt lỗi import-time)
```
- CI chạy đúng các bước trên + migration up/down trên Postgres (`.github/workflows/ci.yml`).
- Đường ghi DB: thêm test tích hợp dùng `tests/conftest.py::make_session` (SQLite in-memory).
- Đổi model → tạo migration trong `alembic/versions/` (theo `down_revision` của head hiện tại), tự viết `op.add_column`/`create_table` + index, kèm `downgrade`.
- Lint diagnostic báo "Cannot find module httpx/sqlalchemy..." là **giả** (interpreter linter sai), bỏ qua.

## 3. Commit (tiếng Việt)
- **Bắt buộc dùng Bash tool + heredoc** (PowerShell here-string làm hỏng UTF-8 — memory `commit-vietnamese-use-bash`).
- Trên branch `main` (dự án commit thẳng main theo lệ). Kết thúc message bằng:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- KHÔNG add `plan_print.md` và file rác.

## 4. Deploy production — dùng `scripts/deploy.sh` (chạy từ máy dev TRONG LAN)
Server ở IP nội bộ 10.0.193.233 → cloud CI/CD bất khả; deploy từ LAN.
```bash
git push origin main          # 1. push
./scripts/deploy.sh           # 2. giao HEAD qua git archive + build + up (migration tự chạy)
./scripts/deploy.sh <sha>     #    rollback về 1 commit nếu cần
```
- `deploy.sh` giao **đúng cây HEAD đã commit** (chỉ file tracked → KHÔNG đụng .env/tls/backups),
  deterministic — **bỏ hẳn màn scp/md5 thủ công**. Chi tiết: [docs/CD_AND_SCALING.md](../../../docs/CD_AND_SCALING.md).
- `docker compose exec -T ...` luôn kèm `< /dev/null`. Truy cập app: **https://10.0.193.233**.
- Chi tiết server/SSH/quirks ở memory `prod-deployment`.

## 5. Verify sau deploy
- Xem log: migration đã `upgrade`, `Application startup complete`, đủ job scheduler.
- Query DB xác nhận schema/dữ liệu: `docker compose exec -T db sh -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB"` (truyền SQL qua stdin để né quoting).
- Chạy 1 lượt quét tay nếu cần thấy kết quả ngay (vd `scan_storage`) thay vì đợi scheduler.
- Báo lại user: container restart, migration version, dữ liệu thật quan sát được.
