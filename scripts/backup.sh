#!/usr/bin/env bash
# Backup Chek_NVR: .env (CHỨA ENCRYPTION_KEY) + dump PostgreSQL, mã hóa AES-256.
#
# ⚠️ Mất ENCRYPTION_KEY (Fernet) = mất TOÀN BỘ mật khẩu NVR đã lưu (không giải mã lại
#    được). Sao lưu file .enc này ra NGOÀI server (máy khác / cloud).
#
# Chạy trên server, trong thư mục dự án (~/Chek_NVR):
#   BACKUP_PASSPHRASE='mật-khẩu-mạnh' ./scripts/backup.sh [thư_mục_đích]
#
# Tự động hằng ngày (cron, vd 02:30):
#   30 2 * * * cd ~/Chek_NVR && BACKUP_PASSPHRASE='...' ./scripts/backup.sh >> ~/backup.log 2>&1
#
# Khôi phục:
#   openssl enc -d -aes-256-cbc -pbkdf2 -in chek_nvr_YYYYMMDD_HHMMSS.tgz.enc \
#     -out bundle.tgz -pass env:BACKUP_PASSPHRASE
#   tar -xzf bundle.tgz                 # -> db.sql + env.bak
#   # nạp lại DB: docker compose exec -T db psql -U $POSTGRES_USER -d $POSTGRES_DB < db.sql
set -euo pipefail

cd "$(cd "$(dirname "$0")/.." && pwd)"  # về gốc dự án

DEST="${1:-./backups}"
mkdir -p "$DEST"
TS="$(date +%Y%m%d_%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Lấy creds DB từ .env (không in ra).
set -a; . ./.env; set +a
: "${POSTGRES_USER:?thiếu POSTGRES_USER trong .env}"
: "${POSTGRES_DB:?thiếu POSTGRES_DB trong .env}"
: "${BACKUP_PASSPHRASE:?đặt BACKUP_PASSPHRASE (mật khẩu giải mã backup)}"

# 1. Dump toàn bộ DB.
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  > "$WORK/db.sql" < /dev/null

# 2. Gói .env (chứa ENCRYPTION_KEY) + dump DB.
cp ./.env "$WORK/env.bak"
tar -czf "$WORK/bundle.tgz" -C "$WORK" db.sql env.bak

# 3. Mã hóa AES-256 (PBKDF2).
OUT="$DEST/chek_nvr_${TS}.tgz.enc"
openssl enc -aes-256-cbc -pbkdf2 -salt \
  -in "$WORK/bundle.tgz" -out "$OUT" -pass env:BACKUP_PASSPHRASE
echo "[backup] đã tạo $OUT ($(du -h "$OUT" | cut -f1))"

# 4. Giữ 14 bản mới nhất, xóa cũ hơn.
ls -1t "$DEST"/chek_nvr_*.tgz.enc 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "[backup] xong. NHỚ đồng bộ $DEST ra ngoài server (rsync/scp/cloud)."
