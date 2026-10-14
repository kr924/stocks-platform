#!/usr/bin/env bash
#
# Deploy whatever is on origin/main, with a health check and a rollback.
#
# Run by hand on the VM — nothing runs it automatically:
#
#   ssh -i ~/.ssh/oci_key ubuntu@129.159.23.190
#   bash ~/stocks/ops/deploy.sh
#
# Each run:
#
#   1. Fetches origin/main. Already deployed → says so and stops.
#   2. Refuses Mon–Fri 09:00–15:35 IST unless --force: a restart mid-session
#      drops the 2-second announcement cycle and armed-target matching for a
#      minute or more.
#   3. Refuses if tracked files on the VM were edited by hand — fast-forwarding
#      over them would either fail or silently ship them.
#   4. Fast-forwards the checkout and builds the image under a candidate tag.
#      The running container is untouched until the build has succeeded, so a
#      failed build costs nothing.
#   5. Swaps the container (same docker run line as ops/retunnel.sh), then
#      health-checks it. If it does not answer within ~2 minutes the previous
#      image and commit are restored.
#   6. Reports the outcome on screen and to Telegram.
#
# Usage:
#   bash ops/deploy.sh            # deploy main if it is new
#   bash ops/deploy.sh --force    # also during market hours, or retry a commit
#                                 # that was rolled back
#   bash ops/deploy.sh --status   # what is deployed, what is pending
#
set -uo pipefail

STOCKS="${STOCKS:-$HOME/stocks}"
ENV_FILE="$STOCKS/backend/.env"
DB_FILE="$STOCKS/backend/market_tracker.db"
STATE_DIR="$HOME/.stocks-deploy"
LOG="$STOCKS/deploy.log"          # *.log is gitignored
LOCK="/tmp/stocks-deploy.lock"
IMAGE="stocks-app"
CONTAINER="stocks-app"
BRANCH="main"
HEALTH_TRIES=40                   # x 3s = 2 minutes

mkdir -p "$STATE_DIR"
FORCE=0
case "${1:-}" in
    --force) FORCE=1 ;;
    --status) STATUS=1 ;;
esac

log() { echo "$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S IST') $*" | tee -a "$LOG"; }

# Keep the log from growing without bound.
if [[ -f "$LOG" && $(stat -c %s "$LOG") -gt 2000000 ]]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

env_value() {
    # Read one key from .env without sourcing it.
    grep -aE "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//; s/["'\'']$//'
}

notify() {
    # Telegram. Best effort: no token or chat id in .env means no message.
    local msg="$1" token chat
    token=$(env_value TELEGRAM_BOT_TOKEN)
    chat=$(env_value TELEGRAM_CHAT_ID)
    [[ -n "$token" && -n "$chat" ]] || return 0
    curl -s --max-time 15 "https://api.telegram.org/bot$token/sendMessage" \
        --data-urlencode "chat_id=$chat" \
        --data-urlencode "text=$msg" > /dev/null || true
}

healthy() {
    local root api
    root=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://localhost:8000/ || true)
    api=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://localhost:8000/api/trading/poller/status || true)
    [[ "$root" == "200" && "$api" == "200" ]]
}

run_container() {
    sudo docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
    sudo docker run -d --name "$CONTAINER" --restart always --network host \
        --env-file "$ENV_FILE" \
        -v "$DB_FILE:/app/backend/market_tracker.db" \
        -v "$ENV_FILE:/app/backend/.env:ro" \
        "$1" > /dev/null
}

cd "$STOCKS" || { echo "no repo at $STOCKS"; exit 1; }

exec 9> "$LOCK"
flock -n 9 || { echo "Another deploy is already running."; exit 1; }

deployed=$(cat "$STATE_DIR/deployed" 2>/dev/null || echo "")
failed=$(cat "$STATE_DIR/failed" 2>/dev/null || echo "")

if ! git fetch -q origin "$BRANCH" 2>> "$LOG"; then
    log "git fetch failed"
    exit 1
fi
target=$(git rev-parse "origin/$BRANCH")
short=$(git rev-parse --short "$target")

if [[ "${STATUS:-0}" == "1" ]]; then
    echo "checkout : $(git log --oneline -1 HEAD)"
    echo "deployed : ${deployed:-<none recorded>}"
    echo "main     : $(git log --oneline -1 "$target")"
    echo "failed   : ${failed:-<none>}"
    echo "container: $(sudo docker inspect -f '{{.State.Status}} since {{.State.StartedAt}}' "$CONTAINER" 2>/dev/null || echo missing)"
    exit 0
fi

if [[ "$target" == "$deployed" ]]; then
    echo "Already deployed: $(git log --oneline -1 "$target"). Nothing to do."
    exit 0
fi
if [[ "$target" == "$failed" && "$FORCE" == "0" ]]; then
    echo "$short was deployed before and rolled back (see $LOG)."
    echo "Push a fix, or retry it anyway with: bash ops/deploy.sh --force"
    exit 1
fi

# ── 1. Market hours ──────────────────────────────────────────────────────────
dow=$(TZ=Asia/Kolkata date +%u)            # 1=Mon … 7=Sun
hhmm=$((10#$(TZ=Asia/Kolkata date +%H%M)))
if [[ "$FORCE" == "0" && "$dow" -le 5 && "$hhmm" -ge 900 && "$hhmm" -lt 1535 ]]; then
    echo "Market hours (Mon-Fri 09:00-15:35 IST) — not deploying $short now."
    echo "Run again after 15:35, or deploy anyway with: bash ops/deploy.sh --force"
    exit 1
fi

# ── 2. Refuse to deploy over hand edits ──────────────────────────────────────
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    log "REFUSED $short: tracked files edited on the VM:"
    git status --porcelain --untracked-files=no | tee -a "$LOG"
    notify "⚠️ Deploy of $short refused: files in ~/stocks were edited on the VM. Run 'git status' there, then 'git stash' or 'git checkout -- .'."
    exit 1
fi

previous=$(git rev-parse HEAD)
echo "Building the new image — this takes a few minutes; the app keeps running meanwhile."
log "Deploying $short ($(git log --format=%s -1 "$target")) over $(git rev-parse --short "$previous")"

if ! git merge -q --ff-only "$target" 2>> "$LOG"; then
    log "REFUSED $short: main is not a fast-forward of the VM checkout"
    notify "⚠️ Deploy of $short refused: main is not a fast-forward of the VM checkout. Someone rewrote history or committed on the VM."
    exit 1
fi

# ── 3. Build beside the running container ────────────────────────────────────
if ! sudo docker build -q -t "$IMAGE:candidate" . >> "$LOG" 2>&1; then
    log "BUILD FAILED for $short — still running $(git rev-parse --short "$previous")"
    git reset -q --hard "$previous"
    echo "$target" > "$STATE_DIR/failed"
    notify "❌ Deploy of $short failed to build. The previous version is still running. Last lines: $(tail -n 5 "$LOG" | tr '\n' ' ' | cut -c1-500)"
    exit 1
fi

# ── 4. Swap, then prove it is up ─────────────────────────────────────────────
had_previous=0
if sudo docker image inspect "$IMAGE:latest" > /dev/null 2>&1; then
    sudo docker tag "$IMAGE:latest" "$IMAGE:previous"
    had_previous=1
fi
sudo docker tag "$IMAGE:candidate" "$IMAGE:latest"
run_container "$IMAGE:latest"

ok=0
for _ in $(seq 1 "$HEALTH_TRIES"); do
    sleep 3
    if healthy; then ok=1; break; fi
done

if [[ "$ok" == "1" ]]; then
    echo "$target" > "$STATE_DIR/deployed"
    rm -f "$STATE_DIR/failed"
    sudo docker image prune -f > /dev/null 2>&1 || true
    log "DEPLOYED $short — healthy"
    notify "✅ Deployed $short: $(git log --format=%s -1 "$target")"
    exit 0
fi

# ── 5. Roll back ─────────────────────────────────────────────────────────────
log "UNHEALTHY after deploy of $short — rolling back. Container log tail:"
sudo docker logs --tail 40 "$CONTAINER" >> "$LOG" 2>&1 || true
if [[ "$had_previous" == "1" ]]; then
    sudo docker tag "$IMAGE:previous" "$IMAGE:latest"
    run_container "$IMAGE:latest"
fi
git reset -q --hard "$previous"
echo "$target" > "$STATE_DIR/failed"
sleep 15
if healthy; then
    log "ROLLED BACK to $(git rev-parse --short "$previous") — healthy"
    notify "↩️ Deploy of $short was unhealthy and was rolled back to $(git rev-parse --short "$previous"). See ~/stocks/deploy.log."
else
    log "ROLLBACK ALSO UNHEALTHY — needs a person"
    notify "🚨 Deploy of $short was unhealthy AND the rollback is not answering. The app may be down. Check: sudo docker logs --tail 100 $CONTAINER"
fi
exit 1
