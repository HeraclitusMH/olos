#!/usr/bin/env bash
# One-time bootstrap for a fresh Ubuntu (22.04 / 24.04) VPS.
#
# Run as root (or via sudo) on a freshly-provisioned box:
#   curl -fsSL https://your-host/vps-setup.sh | sudo bash
# or:
#   sudo ./scripts/vps-setup.sh
#
# This script is idempotent — safe to re-run.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (use sudo)." >&2
  exit 1
fi

APP_DIR="/opt/olos/whatsapp-assistant"
BACKUP_DIR="${APP_DIR}/backups"

log() { printf '\033[1;34m[vps-setup]\033[0m %s\n' "$*"; }

export DEBIAN_FRONTEND=noninteractive

# 1. apt update + upgrade
log "Updating apt and upgrading packages"
apt-get update -y
apt-get upgrade -y

log "Installing base packages"
apt-get install -y \
  ca-certificates \
  curl \
  gnupg \
  lsb-release \
  ufw \
  unzip \
  fail2ban

# 2. Install Docker Engine + Compose plugin (official Docker repo)
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -y
  apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

  systemctl enable --now docker
else
  log "Docker already installed — skipping"
fi

# 3. Project directory + backups
log "Creating ${APP_DIR} and ${BACKUP_DIR}"
mkdir -p "$APP_DIR" "$BACKUP_DIR"
chmod 750 "$APP_DIR"

# 4. UFW: SSH + HTTP + HTTPS only
log "Configuring UFW (allow 22, 80, 443)"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose

# 5. rclone (for B2 backups)
if ! command -v rclone >/dev/null 2>&1; then
  log "Installing rclone"
  curl -fsSL https://rclone.org/install.sh | bash
else
  log "rclone already installed — skipping"
fi

# 6. Final notes
cat <<'EOF'

VPS bootstrap complete.

Next steps:
  1. Clone the project:
       git clone <your-repo-url> /opt/olos/whatsapp-assistant
  2. Create /opt/olos/whatsapp-assistant/.env (see .env.example) — DO NOT commit it.
  3. Edit Caddyfile and replace `yourdomain.com` with your real domain.
  4. Point DNS A/AAAA records at this VPS BEFORE first deploy (Caddy needs it
     to issue a TLS cert via ACME).
  5. Configure rclone for Backblaze B2:
       rclone config
     Create a remote called "b2" (type: Backblaze B2).
  6. Run ./deploy.sh
  7. Add the backup cron (as root):
       (crontab -l 2>/dev/null; echo '0 3 * * * /opt/olos/whatsapp-assistant/scripts/backup.sh >> /var/log/whatsapp-assistant-backup.log 2>&1') | crontab -

EOF
