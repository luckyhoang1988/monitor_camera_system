#!/usr/bin/env bash
# Tạo CA nội bộ + cert server (SAN theo IP) cho Caddy phục vụ HTTPS trên LAN.
#
# Chạy trên server, trong thư mục dự án:
#   ./scripts/gen_certs.sh
#
# - CA (tls/ca.crt + tls/ca.key) tạo MỘT LẦN và giữ nguyên; client cài ca.crt một lần.
# - Chạy lại script chỉ cấp lại cert server (server.crt/server.key), KHÔNG đụng CA,
#   nên client đã tin CA rồi thì không phải cài lại.
# - Đặt SRV_IP nếu IP server khác mặc định:  SRV_IP=10.0.0.5 ./scripts/gen_certs.sh
set -euo pipefail

SRV_IP="${SRV_IP:-10.0.193.233}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/tls"
mkdir -p "$DIR"
cd "$DIR"

DAYS_CA=3650   # CA: 10 năm
DAYS_SRV=825   # cert server: tối đa ~27 tháng (giới hạn nhiều trình duyệt)

if [[ ! -f ca.key || ! -f ca.crt ]]; then
	echo "[certs] Tạo CA nội bộ mới (ca.crt/ca.key)..."
	openssl genrsa -out ca.key 4096
	openssl req -x509 -new -nodes -key ca.key -sha256 -days "$DAYS_CA" \
		-out ca.crt -subj "/O=Chek_NVR/CN=Chek_NVR Internal CA"
else
	echo "[certs] Đã có CA sẵn — dùng lại (client KHÔNG cần cài lại ca.crt)."
fi

echo "[certs] Cấp cert server cho IP $SRV_IP..."
openssl genrsa -out server.key 2048
cat > server.ext <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$SRV_IP
EOF
openssl req -new -key server.key -out server.csr -subj "/O=Chek_NVR/CN=$SRV_IP"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
	-out server.crt -days "$DAYS_SRV" -sha256 -extfile server.ext
rm -f server.csr server.ext
chmod 600 ca.key server.key

echo "[certs] Hoàn tất."
echo "  - Cert server : $DIR/server.crt (Caddy dùng)"
echo "  - Root CA      : $DIR/ca.crt   (cài lên máy client để hết cảnh báo)"
