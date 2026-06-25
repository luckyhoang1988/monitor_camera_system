# Deploy & Scaling

## 1. Deploy: dùng `scripts/deploy.sh` (đã thay màn scp/md5)

**Ràng buộc hạ tầng quan trọng:** server production ở **10.0.193.233 — IP nội bộ LAN**.
GitHub-hosted runner (cloud) **KHÔNG SSH/scp tới được** địa chỉ 10.x → **CD qua GitHub
Actions bất khả** với hạ tầng hiện tại. Deploy phải xuất phát từ **trong LAN**.

**Giải pháp đang dùng:** [scripts/deploy.sh](../scripts/deploy.sh) — chạy từ máy dev
(trong LAN, có SSH alias `chek-nvr`):

```bash
./scripts/deploy.sh            # deploy HEAD
./scripts/deploy.sh <sha|tag>  # deploy/rollback về 1 commit
```

Cơ chế: `git archive HEAD | ssh chek-nvr 'tar -x'` giao **đúng cây đã commit** (chỉ file
tracked → KHÔNG đụng `.env`/`tls`/`backups`), rồi `docker compose build app && up -d`
(entrypoint tự `alembic upgrade head`). **Deterministic**: luôn deploy đúng commit, hết
màn so md5 thủ công, hết nguy cơ đè nhầm WIP.

> Điều kiện đúng đắn: server nên đồng bộ với git (các file tracked == committed). Đã xác
> minh điều này; từ nay `deploy.sh` giữ server luôn khớp HEAD.

### Nếu muốn CD tự động qua GitHub Actions
Chỉ khả thi khi có **self-hosted runner đặt TRONG LAN** (reach được 10.0.193.233). Khi đó
thêm workflow `runs-on: [self-hosted]` chạy `scripts/deploy.sh`. Không cần secret SSH
(runner đã ở trong mạng). Đây là việc cần bạn dựng máy/runner nên chưa làm sẵn.

---

## 2. Multi-worker / event bus dùng Redis (khi scale)

Hiện `event_bus` là pub/sub **trong tiến trình** (xem docstring `app/services/event_bus.py`)
→ chỉ broadcast giữa các kết nối SSE của **một** worker uvicorn. Với ~45 NVR/720 camera,
**1 worker là đủ** nên CHƯA cần đổi.

**Khi nào cần:** chạy nhiều worker/replica → SSE của worker này không nhận event do
worker khác phát.

**Cách làm khi đó:**
1. Thêm service `redis` vào `docker-compose.yml`.
2. Thay `EventBus` bằng Redis pub/sub: `publish` → `redis.publish`; mỗi SSE `subscribe`
   kênh và đẩy xuống browser. Giữ nguyên API `publish/subscribe/unsubscribe`.
3. Tăng worker uvicorn (`--workers N`) hoặc thêm replica.

Khối lượng: ~1 file (`event_bus.py`) + 1 service compose + 1 dependency `redis`.
