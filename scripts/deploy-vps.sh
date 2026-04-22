#!/usr/bin/env bash
# One-shot VPS bootstrap for polymoney on Ubuntu 22.04/24.04.
#
# Usage (from fresh VPS, as root):
#   curl -sSL https://raw.githubusercontent.com/quentinleininger24-create/polymoney/main/scripts/deploy-vps.sh | bash
#
# After it finishes:
#   cd /opt/polymoney
#   nano .env                                                 # fill in your keys
#   docker compose -f docker-compose.prod.yml up -d --build   # start everything

set -euo pipefail

REPO_URL="https://github.com/quentinleininger24-create/polymoney.git"
INSTALL_DIR="/opt/polymoney"

echo "============================================================"
echo " polymoney VPS bootstrap"
echo "============================================================"

if [[ $EUID -ne 0 ]]; then
  echo "!! Must run as root (the script uses apt, sets firewall, etc.)"
  echo "   Try: sudo -i, then re-run."
  exit 1
fi

echo "[1/6] Updating system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq ca-certificates curl gnupg git ufw fail2ban tmux htop nano

echo "[2/6] Installing Docker..."
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
  systemctl enable --now docker
fi
docker --version

echo "[3/6] Configuring firewall (only SSH exposed)..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

echo "[4/6] Enabling fail2ban for SSH brute-force protection..."
systemctl enable --now fail2ban

echo "[5/6] Cloning polymoney repo into ${INSTALL_DIR}..."
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  cd "${INSTALL_DIR}"
  git pull --ff-only
else
  git clone "${REPO_URL}" "${INSTALL_DIR}"
  cd "${INSTALL_DIR}"
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi

echo "[6/6] Done. Next steps:"
echo ""
echo "  cd ${INSTALL_DIR}"
echo "  nano .env              # fill in: GEMINI_API_KEY, TELEGRAM_*,"
echo "                         #          WALLET_PRIVATE_KEY, WALLET_ADDRESS,"
echo "                         #          POLYMARKET_API_KEY/SECRET/PASSPHRASE,"
echo "                         #          NEWSAPI_KEY (optional)"
echo "  docker compose -f docker-compose.prod.yml up -d --build"
echo ""
echo "To check the logs:"
echo "  docker compose -f docker-compose.prod.yml logs -f order_manager"
echo ""
echo "To stop everything:"
echo "  docker compose -f docker-compose.prod.yml down"
echo ""
echo "SAFETY REMINDERS:"
echo "  - Keep MODE=paper in .env until backtest and paper mode look right."
echo "  - Never put funds you can't afford to lose."
echo "  - Use /panic on the Telegram bot to halt all trading."
