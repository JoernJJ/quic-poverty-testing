#!/bin/bash
# One-time root setup for the TCP/TLS (nginx) baseline. Ubuntu adaptation of
# quic-implementations/tcp-tls/setup-env.sh:
#   - No apt (nginx + gettext already installed).
#   - Non-destructive: creates a `nginx-server` symlink instead of RENAMING
#     /usr/sbin/nginx (upstream moves it, breaking system nginx), and backs up
#     /etc/nginx/nginx.conf before overwriting.
# Run once with sudo:  sudo ./setup-tcp-tls.sh
set -euo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
TCP="$HERE/../quic-implementations/tcp-tls"

systemctl stop nginx 2>/dev/null || true

# nginx-server alias the interop run-server.sh expects
if [ ! -e /usr/local/bin/nginx-server ]; then
    ln -s /usr/sbin/nginx /usr/local/bin/nginx-server
    echo "linked /usr/local/bin/nginx-server -> /usr/sbin/nginx"
fi

# certs
mkdir -p /etc/nginx/ssl
cp -f "$TCP/cert.pem" /etc/nginx/ssl/cert.pem
cp -f "$TCP/key.pem"  /etc/nginx/ssl/key.pem

# main config (back up the distro one first)
[ -f /etc/nginx/nginx.conf.pre-quic ] || cp -f /etc/nginx/nginx.conf /etc/nginx/nginx.conf.pre-quic
cp -f "$TCP/nginx.conf" /etc/nginx/nginx.conf
rm -f /etc/nginx/sites-enabled/default

echo "tcp-tls baseline ready. run-server.sh will render the per-run site and exec nginx-server."
echo "restore distro nginx later with: sudo cp /etc/nginx/nginx.conf.pre-quic /etc/nginx/nginx.conf"
