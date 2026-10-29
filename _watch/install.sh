#!/bin/bash
# Installs kh-watch (green/red checks, alerts, status page) on this server.
#   sudo bash install.sh revenant    Revenant: checks + alerts + the private status page at status.kerryhodgkins.com
#   sudo bash install.sh haunted     Haunted Node: checks + alerts, and it watches Revenant's watcher
#   sudo bash install.sh update      after a change: copies the new watch.py (keeps your config and secrets);
#                                    on Revenant it also adds the public status page if it is not there yet
# Then type: dashboard
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
MODE=${1:-}
LIB=/usr/local/lib/kh-watch
ETC=/etc/kh-watch
HOST=status.kerryhodgkins.com

install -d -m 755 "$LIB" "$ETC"
install -m 755 "$HERE/watch.py" "$LIB/watch.py"
for n in kh-watch dashboard Dashboard; do ln -sf "$LIB/watch.py" "/usr/local/bin/$n"; done
NGX=/etc/nginx/sites-available/kh-watch; WEB=/var/www/kh-watch-acme
getv() { [ -f "$ETC/secrets.env" ] && sed -n "s/^$1=//p" "$ETC/secrets.env" | head -n1; }   # read values without running the file as a script

ensure_public() {   # Revenant: what visitors see at https://$HOST/ (service names only, green/red)
  grep -qi '^\[public\]' "$ETC/watch.conf" 2>/dev/null && return 0
  python3 "$LIB/watch.py" init-public >> "$ETC/watch.conf" && echo "Added the [public] section to $ETC/watch.conf"
}

write_https() {   # $1 = private page token. Public page at /, private page at /<token>/
  install -d -m 755 /etc/nginx/snippets
  cat > /etc/nginx/snippets/kh-watch-headers.conf <<HDR
add_header Strict-Transport-Security "max-age=31536000" always;
add_header Cache-Control "no-store" always;
add_header Content-Security-Policy "default-src 'none'; style-src 'unsafe-inline'" always;
add_header X-Content-Type-Options "nosniff" always;
HDR
  cat > "$NGX" <<NGINX
server { listen 80; server_name $HOST;
    location /.well-known/acme-challenge/ { root $WEB; }
    location / { return 301 https://$HOST\$request_uri; } }
server {
    listen 443 ssl;
    server_name $HOST;
    ssl_certificate     /etc/letsencrypt/live/$HOST/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$HOST/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    access_log off;
    include snippets/kh-watch-headers.conf;
    # the private page: everything, behind an unguessable link
    location /$1/ { alias /var/lib/kh-watch/www/$1/; index index.html; include snippets/kh-watch-headers.conf; add_header X-Robots-Tag "noindex, nofollow" always; }
    # the public page: service names only
    location = / { root /var/lib/kh-watch/www/public; try_files /index.html =404; }
    location = /badge.svg { root /var/lib/kh-watch/www/public; include snippets/kh-watch-headers.conf; add_header Access-Control-Allow-Origin "*" always; }
    location = /status.json { root /var/lib/kh-watch/www/public; include snippets/kh-watch-headers.conf; add_header Access-Control-Allow-Origin "*" always; }
    location / { return 404; }
}
NGINX
  nginx -t && systemctl reload nginx
}

if [ "$MODE" = update ]; then
  if [ -f /etc/systemd/system/kh-watch.timer ] && grep -q OnUnitActiveSec /etc/systemd/system/kh-watch.timer; then
    sed -i -e 's/^OnBootSec=.*$//' -e 's/^OnUnitActiveSec=.*$/OnCalendar=*-*-* *:*:00/' /etc/systemd/system/kh-watch.timer
    systemctl daemon-reload; systemctl restart kh-watch.timer
  fi
  TOK=$(getv KW_PAGE_TOKEN || true)
  if [ -n "$TOK" ] && [ -f "$NGX" ]; then ensure_public; write_https "$TOK"; echo "Public status page: https://$HOST/"; fi
  systemctl start kh-watch.service || true; echo "Updated. Type: dashboard"; exit 0
fi
case "$MODE" in revenant|haunted) ;; *) echo "usage: sudo bash install.sh revenant|haunted|update" >&2; exit 2;; esac

# 1. the checks (no secrets in here, so 'dashboard' works for any user)
if [ ! -f "$ETC/watch.conf" ]; then
  HB=""
  if [ "$MODE" = haunted ]; then
    read -rp "Revenant's status link (from Revenant's install, ends in /status.json; Enter to skip): " HB
  fi
  python3 "$LIB/watch.py" init "$MODE" "$HB" > "$ETC/watch.conf"
  chmod 644 "$ETC/watch.conf"
  echo "Wrote $ETC/watch.conf (edit it to add or remove checks)"
fi

# 2. secrets: typed here, never shown again, root only
if [ ! -f "$ETC/secrets.env" ]; then
  TOPIC="kh-$(head -c 18 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 22)"
  read -rsp "Resend API key for alerts (sending access, kerryhodgkins.com; input hidden; Enter to skip email): " KEY; echo
  TO=""; [ -n "$KEY" ] && read -rp "Send alert emails to: " TO
  {
    printf 'KW_NTFY_TOPIC=%s\n' "$TOPIC"
    printf 'KW_RESEND_KEY=%s\nKW_EMAIL_TO=%s\n' "$KEY" "$TO"
    printf 'KW_EMAIL_FROM=kh-watch <alerts@kerryhodgkins.com>\n'
    if [ "$MODE" = revenant ]; then
      TOKEN=$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)
      printf 'KW_PAGE_TOKEN=%s\nKW_PAGE_URL=https://%s/%s/\n' "$TOKEN" "$HOST" "$TOKEN"
    fi
  } > "$ETC/secrets.env"
  chmod 600 "$ETC/secrets.env"; unset KEY
  echo "Saved $ETC/secrets.env"
fi
KW_NTFY_TOPIC=$(getv KW_NTFY_TOPIC); KW_PAGE_TOKEN=$(getv KW_PAGE_TOKEN); KW_PAGE_URL=$(getv KW_PAGE_URL)

# 3. the watcher: its own user, can only write its own state folder, runs every minute
id khwatch >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin khwatch
usermod -aG systemd-journal khwatch   # read-only access to service logs, for 'journal' checks like the DOVE-Net call
cat > /etc/systemd/system/kh-watch.service <<UNIT
[Unit]
Description=kh-watch: check everything, alert on changes
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=khwatch
EnvironmentFile=$ETC/secrets.env
ExecStart=/usr/bin/python3 $LIB/watch.py run
StateDirectory=kh-watch
StateDirectoryMode=0755
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
TimeoutStartSec=50
UNIT
cat > /etc/systemd/system/kh-watch.timer <<UNIT
[Unit]
Description=Run kh-watch every minute

[Timer]
# a plain clock schedule: fires every minute even if the last run failed
OnCalendar=*-*-* *:*:00
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now kh-watch.timer >/dev/null
systemctl start kh-watch.service || true

# 4. Revenant only: the status pages (needs DNS: status -> this server)
if [ "$MODE" = revenant ]; then
  install -d -m 755 "$WEB"
  if [ ! -f "/etc/letsencrypt/live/$HOST/fullchain.pem" ]; then
    printf 'server { listen 80; server_name %s;\n    location /.well-known/acme-challenge/ { root %s; }\n    location / { return 301 https://%s$request_uri; } }\n' "$HOST" "$WEB" "$HOST" > "$NGX"
    ln -sf "$NGX" /etc/nginx/sites-enabled/kh-watch
    nginx -t && systemctl reload nginx
    certbot certonly --webroot -w "$WEB" -d "$HOST" --cert-name "$HOST" --non-interactive --agree-tos --register-unsafely-without-email
  fi
  ln -sf "$NGX" /etc/nginx/sites-enabled/kh-watch
  ensure_public
  write_https "$KW_PAGE_TOKEN"
fi

sleep 2
echo
echo "== Installed. Your dashboard:"; echo
/usr/local/bin/dashboard || true
echo
echo "== Phone alerts: install the free ntfy app, tap +, and subscribe to this topic (keep it private):"
echo "   $KW_NTFY_TOPIC"
[ "$MODE" = revenant ] && { echo; echo "== Private status page (bookmark it; the link is the password):"; echo "   $KW_PAGE_URL"; echo "   Haunted Node's installer asks for: ${KW_PAGE_URL}status.json"; echo; echo "== Public status page: https://$HOST/"; }
echo
echo "Then run:  sudo kh-watch test-alert   (as root, so it can read the alert settings)"
