# CD & Scaling — lộ trình (cần hạ tầng/secret để bật)

Tài liệu này mô tả 2 cải tiến cần quyết định/hạ tầng của bạn nên CHƯA bật mặc định.

---

## 1. CD bằng image (thay quy trình scp/tar hiện tại)

**Vấn đề hiện tại:** repo private nên `git pull` trên server hỏng → đang deploy bằng
`scp/tar` + so md5 thủ công (mong manh, dễ đè nhầm WIP — xem skill `chek-nvr` §4).

**Đề xuất:** CI build image → đẩy lên registry (GHCR) → server chỉ `pull` & `up -d`.

### Cách bật
1. Sửa `docker-compose.yml` service `app`: thay `build: .` bằng
   `image: ghcr.io/luckyhoang1988/chek_nvr:latest` (giữ `build` lại cho dev nếu muốn).
2. Cho server đăng nhập GHCR (hoặc để package public):
   `echo $GHCR_TOKEN | docker login ghcr.io -u <user> --password-stdin`.
3. Trên GitHub, thêm secrets cho workflow `deploy.yml`:
   - `SSH_PRIVATE_KEY` (khóa deploy tới server `pfvnnvr@10.0.193.233`)
   - `SSH_HOST` = `10.0.193.233`, `SSH_USER` = `pfvnnvr`
4. Chạy workflow **Deploy** (thủ công: tab Actions → Deploy → Run) hoặc đổi trigger sang
   `push` nhánh `main` để tự động.

Workflow mẫu đã có sẵn: [.github/workflows/deploy.yml](../.github/workflows/deploy.yml)
(đang để `workflow_dispatch` = chỉ chạy khi bấm tay, an toàn cho tới khi cấu hình secret).

### Lợi
- Hết màn scp/md5; deploy lặp lại được, rollback = trỏ lại tag image cũ.
- Không còn nguy cơ đè WIP server (image bất biến, không sửa file tại chỗ).

> ⚠️ Khi chuyển sang image-based, nên **commit nốt WIP đang nằm trên server** (hiện
> trùng với code đã commit — xem skill §4) rồi `git reset --hard` cho sạch, để server
> không còn khác git.

---

## 2. Multi-worker / event bus dùng Redis (#scale)

Hiện `event_bus` là pub/sub **trong tiến trình** (xem docstring `app/services/event_bus.py`)
→ chỉ broadcast giữa các kết nối SSE của **một** worker uvicorn. Với ~45 NVR/720 camera,
**1 worker là đủ** nên CHƯA cần đổi.

**Khi nào cần:** chạy nhiều worker/replica (vd sau load balancer) → SSE của worker này
không nhận event do worker khác phát.

**Cách làm khi đó:**
1. Thêm service `redis` vào `docker-compose.yml`.
2. Thay `EventBus` bằng Redis pub/sub: `publish` → `redis.publish(channel, json)`;
   mỗi kết nối SSE `subscribe` kênh và đẩy xuống browser. Giữ nguyên API
   `publish/subscribe/unsubscribe` để không phải sửa nơi gọi.
3. Tăng worker uvicorn (`--workers N`) hoặc thêm replica.

Khối lượng: ~1 file (`event_bus.py`) + 1 service compose + 1 dependency `redis`.
