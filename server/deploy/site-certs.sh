#!/bin/sh
# Certificates for published sites on their own hosts (<site>.<user>.<domain>); run as root every minute (site-certs.timer).
# The app drops a file per wanted host into $DATA/sites/.want; a host is tried once its name resolves like the main domain
# (failed validations are rate-limited by Let's Encrypt, so a failure waits an hour before the next try).
DATA=${ME_DATA:-/var/lib/me-agent}
WANT="$DATA/sites/.want" CERTS="$DATA/sites/.certs"
mkdir -p "$CERTS" /var/www/acme
for f in "$WANT"/*; do
    [ -f "$f" ] || continue
    h=$(basename "$f")
    case "$h" in *.fail) continue ;; *[!a-z0-9.-]*) rm -f "$f"; continue ;; esac
    [ -n "$(find "$f.fail" -mmin -60 2>/dev/null)" ] && continue
    ip=$(dig +short "$h" @8.8.8.8 | tail -n1)
    [ -n "$ip" ] && [ "$ip" = "$(dig +short "${h#*.*.}" @8.8.8.8 | tail -n1)" ] || continue
    if [ -f "/etc/letsencrypt/live/$h/fullchain.pem" ] || certbot certonly --webroot -w /var/www/acme -d "$h" -n --agree-tos \
            --register-unsafely-without-email -q --keep-until-expiring; then
        touch "$CERTS/$h"
        rm -f "$f" "$f.fail"
    else
        touch "$f.fail"
    fi
done
chown -R meagent "$DATA/sites/.want" "$CERTS" 2>/dev/null
