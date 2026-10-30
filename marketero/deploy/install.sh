#!/bin/bash
# Instala/atualiza o Marketero no servidor (rodar como root, a partir da pasta do pacote enviado).
#   bash deploy/install.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/marketero
DATA=/var/lib/marketero
ETC=/etc/marketero
ENVF=$ETC/marketero.env

id marketero >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /sbin/nologin marketero
mkdir -p "$APP" "$DATA" "$ETC"

# Código (sem dados locais nem ambiente virtual de desenvolvimento)
rsync -a --delete --exclude ".venv" --exclude "data" --exclude "__pycache__" "$SRC/" "$APP/"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R root:root "$APP"

if [ ! -f "$ENVF" ]; then
  umask 077
  cat > "$ENVF" <<EOF
MARKETERO_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
MARKETERO_DATA_DIR=$DATA
MARKETERO_ROOT_PATH=/marketero
MARKETERO_PUBLIC_URL=https://institutoalveslima.com.br/marketero
MARKETERO_COOKIE_SECURE=1
PYTHONDONTWRITEBYTECODE=1
EOF
  umask 022
fi
chown root:marketero "$ENVF" && chmod 640 "$ENVF"
chown -R marketero:marketero "$DATA" && chmod 750 "$DATA"

install -m 644 "$APP/deploy/marketero.service" /etc/systemd/system/marketero.service
install -m 644 "$APP/deploy/marketero-backup.cron" /etc/cron.d/marketero-backup
install -m 644 "$APP/deploy/apache-marketero.conf" /etc/httpd/conf/marketero.conf
systemctl daemon-reload
systemctl enable -q marketero
systemctl restart marketero
apachectl configtest && systemctl reload httpd
echo "Marketero instalado. Status: $(systemctl is-active marketero)"
