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
python -m pytest tests/ -q          # PHẢI xanh toàn bộ
python -m alembic heads             # PHẢI 1 head duy nhất (nếu thêm migration)
python -c "import app.main"         # smoke import (bắt lỗi import-time)
```
- Đổi model → tạo migration trong `alembic/versions/` (theo `down_revision` của head hiện tại), tự viết `op.add_column`/`create_table` + index, kèm `downgrade`.
- Lint diagnostic báo "Cannot find module httpx/sqlalchemy..." là **giả** (interpreter linter sai), bỏ qua.

## 3. Commit (tiếng Việt)
- **Bắt buộc dùng Bash tool + heredoc** (PowerShell here-string làm hỏng UTF-8 — memory `commit-vietnamese-use-bash`).
- Trên branch `main` (dự án commit thẳng main theo lệ). Kết thúc message bằng:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- KHÔNG add `plan_print.md` và file rác.

## 4. Deploy production (sau khi push) — repo PRIVATE nên git pull HỎNG trên server
Quy trình đang dùng (chi tiết server/SSH ở memory `prod-deployment`):
```bash
# 1. push
git push origin main

# 2. chép CHÍNH XÁC các file của commit (giữ WIP khác trên server) qua tar+ssh
git show --name-only --pretty=format: <SHA> | grep -v '^$' > /tmp/f.txt
tar -czf - -T /tmp/f.txt | ssh chek-nvr 'cd ~/Chek_NVR && tar -xzf -'

# 3. build + restart (entrypoint TỰ chạy `alembic upgrade head`)
ssh chek-nvr 'cd ~/Chek_NVR && docker compose build app && docker compose up -d app'
```
- ⚠️ Server có **WIP chưa commit** = phần đã commit ở local. Trước khi đè file chồng lấn, **so md5** với bản đã commit; chỉ chép file của đúng commit, KHÔNG đụng file khác.
- `docker compose exec -T ...` luôn kèm `< /dev/null`. Truy cập app: **https://10.0.193.233**.

## 5. Verify sau deploy
- Xem log: migration đã `upgrade`, `Application startup complete`, đủ job scheduler.
- Query DB xác nhận schema/dữ liệu: `docker compose exec -T db sh -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB"` (truyền SQL qua stdin để né quoting).
- Chạy 1 lượt quét tay nếu cần thấy kết quả ngay (vd `scan_storage`) thay vì đợi scheduler.
- Báo lại user: container restart, migration version, dữ liệu thật quan sát được.
