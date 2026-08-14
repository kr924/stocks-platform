#!/usr/bin/env bash
#
# Rotate the Cloudflare quick tunnel and rewire everything that hardcodes its
# hostname.
#
# A quick tunnel gets a new random hostname every time it starts, and four
# things point at the old one: UPSTOX_REDIRECT_URI, FRONTEND_URL and
# PUBLIC_BASE_URL in backend/.env, and the Telegram webhook registered with
# Telegram's servers. Missing any one of them leaves the app reachable but
# subtly broken — most painfully the redirect URI, where Upstox auth fails with
# UDAPI100068 and every price on the screen goes blank.
#
# The container is recreated rather than restarted because --env-file is read
# once, when the container is created.
#
# Usage:
#   bash ops/retunnel.sh              # restart the tunnel, take the new URL, rewire
#   bash ops/retunnel.sh --current    # print the current URL, change nothing
#
set -euo pipefail

STOCKS="$HOME/stocks"
ENV_FILE="$STOCKS/backend/.env"
LOG="$STOCKS/cloudflared.log"
SERVICE="cloudflared-quick"
URL_RE='https://[a-z0-9-]*\.trycloudflare\.com'

die() { echo "ERROR: $*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "no .env at $ENV_FILE"

# ── 1. Get a URL ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--current" ]]; then
    url=$(grep -ao "$URL_RE" "$LOG" 2>/dev/null | tail -1 || true)
    [[ -n "$url" ]] || die "no tunnel URL in $LOG"
    echo "Current tunnel URL: $url"
    echo -n "Reachable: "; curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 20 "$url" || true
    echo
    echo ".env currently points at:"
    grep -aE '^(UPSTOX_REDIRECT_URI|FRONTEND_URL|PUBLIC_BASE_URL)=' "$ENV_FILE" | sed 's/^/  /'
    exit 0
fi

# Only lines written after this point can belong to the new tunnel, so the old
# hostname cannot be picked up by mistake.
marker=$(wc -c < "$LOG" 2>/dev/null || echo 0)

echo "Restarting $SERVICE ..."
sudo systemctl restart "$SERVICE"

url=""
for _ in $(seq 1 30); do
    sleep 2
    url=$(tail -c "+$((marker + 1))" "$LOG" 2>/dev/null | grep -ao "$URL_RE" | tail -1 || true)
    [[ -n "$url" ]] && break
done
[[ -n "$url" ]] || die "tunnel did not report a URL within 60s — check: journalctl -u $SERVICE -n 50"

echo "New tunnel URL: $url"

# Rewiring a URL that does not answer would leave the app worse off than before.
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 25 "$url" || echo 000)
[[ "$code" == "200" ]] || die "new URL answered HTTP $code — nothing was changed"
echo "Reachable: HTTP 200"

# ── 2. Rewire .env ──────────────────────────────────────────────────────────
backup="$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
cp "$ENV_FILE" "$backup"

set_var() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

set_var UPSTOX_REDIRECT_URI "$url/api/auth/callback"
set_var FRONTEND_URL "$url"
set_var PUBLIC_BASE_URL "$url"
echo "Updated .env (backup: $backup)"

# ── 3. Recreate the container so it reads the new environment ───────────────
echo "Recreating container ..."
sudo docker rm -f stocks-app > /dev/null 2>&1 || true
sudo docker run -d --name stocks-app --restart always --network host \
    --env-file "$ENV_FILE" \
    -v "$STOCKS/backend/market_tracker.db:/app/backend/market_tracker.db" \
    -v "$ENV_FILE:/app/backend/.env:ro" \
    stocks-app > /dev/null

for _ in $(seq 1 20); do
    sleep 3
    [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost:8000/ || true)" == "200" ]] && break
done

# The app builds its login URL from the environment, so this is the check that
# the redirect URI actually took — not just that the file was edited.
served=$(curl -s --max-time 20 http://localhost:8000/api/auth/login \
         | grep -ao "redirect_uri=[^&\"]*" | head -1 || true)
echo "App is serving: ${served:-<no login url>}"

# ── 4. Re-register the Telegram webhook ─────────────────────────────────────
encoded=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$url")
hook=$(curl -s --max-time 25 -X POST \
       "http://localhost:8000/api/telegram/register-webhook?public_url=$encoded" || true)
echo "Telegram webhook: $hook"

# ── 5. The one step that cannot be automated ────────────────────────────────
cat <<EOF

────────────────────────────────────────────────────────────────────
Done. New URL:

    $url

STILL NEEDS YOU — Upstox developer console, set the redirect URI to:

    $url/api/auth/callback

Until that matches, authorising returns UDAPI100068 and quotes stay empty.
Then open the app and click Authorize Upstox.
────────────────────────────────────────────────────────────────────
EOF
