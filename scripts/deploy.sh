#!/usr/bin/env bash
# Deploy Chek_NVR lên server LAN — THAY màn scp/md5 thủ công (xem skill chek-nvr §4).
#
# Giao ĐÚNG cây HEAD đã commit qua `git archive` (chỉ file tracked -> KHÔNG đụng
# .env/tls/backups vốn không nằm trong git), rồi build + up trên server. Entrypoint
# tự chạy `alembic upgrade head`. Deterministic: luôn deploy đúng commit, hết md5 dance.
#
# ⚠️ CHẠY TỪ MÁY DEV TRONG LAN (có SSH tới server). Cloud CI/CD KHÔNG tới được server
#    vì IP nội bộ 10.x (xem docs/CD_AND_SCALING.md).
#
# Dùng:
#   ./scripts/deploy.sh                 # deploy HEAD qua alias ssh 'chek-nvr'
#   ./scripts/deploy.sh <commit|tag>    # deploy một ref cụ thể (rollback)
#   SSH_ALIAS=chek-nvr ./scripts/deploy.sh
set -euo pipefail

SSH_ALIAS="${SSH_ALIAS:-chek-nvr}"
REMOTE_DIR="${REMOTE_DIR:-~/Chek_NVR}"
REF="${1:-HEAD}"

if ! git rev-parse --verify --quiet "$REF" >/dev/null; then
  echo "✗ Không tìm thấy ref '$REF'." >&2
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "⚠ Có thay đổi CHƯA COMMIT — deploy.sh chỉ giao $REF (không gồm WIP)."
fi

SHA="$(git rev-parse --short "$REF")"
echo "[deploy] giao cây $REF ($SHA) -> $SSH_ALIAS:$REMOTE_DIR"
git archive --format=tar "$REF" | ssh "$SSH_ALIAS" "cd $REMOTE_DIR && tar -x"

echo "[deploy] build + up (entrypoint tự chạy migration)..."
ssh "$SSH_ALIAS" "cd $REMOTE_DIR && docker compose build app && docker compose up -d app"

echo "[deploy] log khởi động (migration + scheduler):"
ssh "$SSH_ALIAS" "cd $REMOTE_DIR && sleep 8 && docker compose logs app --tail 20 \
  | grep -E 'upgrade|Added job|startup complete|Error|Traceback' || true"
echo "[deploy] xong ($SHA). Kiểm tra: https://10.0.193.233"
