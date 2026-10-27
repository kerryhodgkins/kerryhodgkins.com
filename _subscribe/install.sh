#!/bin/bash
# Installs the kerryhodgkins.com email sign-up on this server (Ubuntu/Debian with nginx and certbot).
#   sudo ./install.sh            first time: asks for the Resend key and segment id, sets up nginx, HTTPS and the service
#   sudo ./install.sh update     after a change: copies the new subscribe.py and restarts
# Before the first run: point subscribe.kerryhodgkins.com at this server (DNS A record), and verify kerryhodgkins.com
# in Resend (Domains) so it can send from hello@kerryhodgkins.com.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
HOST=subscribe.kerryhodgkins.com
ENV=/etc/kh-subscribe.env
WEB=/var/www/kh-subscribe
NGX=/etc/nginx/sites-available/kh-subscribe

install -d -m 755 /usr/local/lib/kh-subscribe
install -m 755 "$HERE/subscribe.py" /usr/local/lib/kh-subscribe/subscribe.py

if [ "${1:-}" = update ]; then systemctl restart kh-subscribe; echo "Updated and restarted."; exit 0; fi

# 1. settings (the key is typed here, never shown, stored readable by root only)
if [ ! -f "$ENV" ]; then
  read -rsp "Resend API key (input hidden): " KEY; echo
  read -rp "Resend segment id (Contacts > Segments > kerryhodgkins.com): " SEG
  read -rp "Send from [Kerry Hodgkins <hello@kerryhodgkins.com>]: " FROM; FROM=${FROM:-Kerry Hodgkins <hello@kerryhodgkins.com>}
  umask 077; printf 'RESEND_API_KEY=%s\nRESEND_SEGMENT=%s\nKH_FROM=%s\nKH_PUBLIC=https://%s\n' "$KEY" "$SEG" "$FROM" "$HOST" > "$ENV"
  chmod 600 "$ENV"; unset KEY; echo "Saved $ENV"
fi

# 2. the service: its own throwaway user, a private state folder, nothing else writable
cat > /etc/systemd/system/kh-subscribe.service <<UNIT
[Unit]
Description=kerryhodgkins.com email sign-up
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$ENV
ExecStart=/usr/bin/python3 /usr/local/lib/kh-subscribe/subscribe.py
DynamicUser=yes
StateDirectory=kh-subscribe
Environment=KH_STATE=/var/lib/kh-subscribe
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now kh-subscribe >/dev/null
systemctl restart kh-subscribe

# 3. nginx on port 80 first (for the certificate), then HTTPS
install -d -m 755 "$WEB"
cat > "$NGX" <<NGINX
server { listen 80; server_name $HOST;
    location /.well-known/acme-challenge/ { root $WEB; }
    location / { return 301 https://$HOST\$request_uri; } }
NGINX
ln -sf "$NGX" /etc/nginx/sites-enabled/kh-subscribe
nginx -t && systemctl reload nginx
if [ ! -f "/etc/letsencrypt/live/$HOST/fullchain.pem" ]; then
  certbot certonly --webroot -w "$WEB" -d "$HOST" --cert-name "$HOST" --non-interactive --agree-tos --register-unsafely-without-email
fi
{ cat <<NGINX80
server { listen 80; server_name $HOST;
    location /.well-known/acme-challenge/ { root $WEB; }
    location / { return 301 https://$HOST\$request_uri; } }
NGINX80
cat <<NGINX
server {
    listen 443 ssl;
    server_name $HOST;
    ssl_certificate     /etc/letsencrypt/live/$HOST/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$HOST/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    access_log off;   # no request log here: the only thing anyone sends this site is their email
    add_header Strict-Transport-Security "max-age=31536000" always;
    limit_req zone=kh_subscribe burst=10 nodelay;
    location / {
        proxy_pass http://127.0.0.1:8097;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        client_max_body_size 8k;
    }
}
NGINX
} > "$NGX"   # rewritten whole each run, so running the installer twice never duplicates anything
grep -q 'zone=kh_subscribe' /etc/nginx/conf.d/kh-subscribe-limit.conf 2>/dev/null || \
  echo 'limit_req_zone $binary_remote_addr zone=kh_subscribe:1m rate=20r/m;' > /etc/nginx/conf.d/kh-subscribe-limit.conf
nginx -t && systemctl reload nginx
sleep 1
curl -fsS "http://127.0.0.1:8097/healthz" >/dev/null && echo "Running: https://$HOST/" || { echo "The service didn't start; see: journalctl -u kh-subscribe -n 30" >&2; exit 1; }
