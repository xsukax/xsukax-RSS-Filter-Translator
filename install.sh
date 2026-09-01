#!/usr/bin/env bash
# =============================================================================
# xsukax RSS Filter & Translator - install / uninstall script
#
# Usage:
#   sudo bash install.sh            (or: sudo bash install.sh install)
#   sudo bash install.sh uninstall
#   sudo bash install.sh uninstall --purge   # also remove app data + venv
#
# Tested targets: Debian/Ubuntu (apt), RHEL/CentOS/Fedora (dnf/yum),
# Alpine (apk). Requires Python 3.9+ and systemd.
# The web interface listens on port 6985.
# =============================================================================
set -euo pipefail

APP_NAME="xsukax-rss-filter"
APP_USER="xsukax"
INSTALL_DIR="/opt/${APP_NAME}"
VENV_DIR="${INSTALL_DIR}/venv"
DATA_DIR="${INSTALL_DIR}/data"
SERVICE_NAME="${APP_NAME}.service"
PORT=6985

log()  { printf '\033[1;32m[xsukax]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[xsukax]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[xsukax] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "Please run this script as root (sudo bash $0 $1)."
}

pkg_install() {
    # Install system packages with whatever package manager exists.
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q "$@"
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q "$@"
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache "$@"
    else
        die "No supported package manager found (apt/dnf/yum/apk)."
    fi
}

ensure_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        log "Installing Python 3..."
        pkg_install python3 python3-venv 2>/dev/null || pkg_install python3
    fi
    # Ensure venv support (Debian/Ubuntu split it out).
    if ! python3 -c 'import venv' >/dev/null 2>&1 || \
       ! python3 -m venv --without-pip /tmp/.xsukax_venv_test >/dev/null 2>&1; then
        log "Installing python3-venv..."
        pkg_install python3-venv 2>/dev/null || pkg_install python3-virtualenv 2>/dev/null || true
    fi
    rm -rf /tmp/.xsukax_venv_test 2>/dev/null || true
    python3 -c 'import sys; v=sys.version_info; assert v >= (3,9), "Python 3.9+ required"' \
        || die "Python 3.9 or newer is required."
}

# =============================================================================
# INSTALL
# =============================================================================
do_install() {
    require_root install
    log "Installing xsukax RSS Filter & Translator on port ${PORT}..."

    ensure_python

    # The translation stack (ctranslate2, argostranslate, ...) needs roughly
    # 3 GB of free disk for installation; warn early if space is tight.
    AVAIL_MB=$(df -Pm / 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -n "${AVAIL_MB:-}" ] && [ "${AVAIL_MB}" -lt 3072 ]; then
        warn "Less than 3 GB free on the target filesystem (${AVAIL_MB} MB)."
        warn "Installation may fail — free up space first."
    fi

    # Dedicated, unprivileged system user.
    if ! id "${APP_USER}" >/dev/null 2>&1; then
        log "Creating system user '${APP_USER}'..."
        useradd --system --no-create-home --shell /usr/sbin/nologin "${APP_USER}" \
            2>/dev/null || useradd -r -M -s /usr/sbin/nologin "${APP_USER}"
    fi

    # Deploy application files (from the directory containing this script).
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "${SCRIPT_DIR}/app.py" ] || die "app.py not found next to install.sh. Run the script from the application directory."
    log "Copying application files to ${INSTALL_DIR}..."
    mkdir -p "${INSTALL_DIR}" "${DATA_DIR}"
    cp -f "${SCRIPT_DIR}/app.py" "${INSTALL_DIR}/"
    cp -rf "${SCRIPT_DIR}/templates" "${INSTALL_DIR}/"
    cp -rf "${SCRIPT_DIR}/static" "${INSTALL_DIR}/"
    [ -f "${SCRIPT_DIR}/requirements.txt" ] && cp -f "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"

    # Python virtual environment.
    # NOTE: pip unpacks large wheels (ctranslate2 & friends) into $TMPDIR.
    # Many VPSs mount /tmp as a small RAM tmpfs (~1 GB), which makes pip fail
    # with "No space left on device" even when the disk has plenty of room.
    # Force a disk-backed temp dir inside the install dir instead.
    log "Creating Python virtual environment (this can take a few minutes)..."
    export TMPDIR="${INSTALL_DIR}/.tmp"
    mkdir -p "${TMPDIR}"
    python3 -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install --quiet --no-cache-dir --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet --no-cache-dir -r "${INSTALL_DIR}/requirements.txt"
    rm -rf "${TMPDIR}"
    unset TMPDIR

    chown -R "${APP_USER}:${APP_USER}" "${INSTALL_DIR}"

    # systemd service.
    log "Creating systemd service '${SERVICE_NAME}'..."
    cat > "/etc/systemd/system/${SERVICE_NAME}" <<EOF
[Unit]
Description=xsukax RSS Filter & Translator (port ${PORT})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=XSUKAX_HOST=0.0.0.0
Environment=XSUKAX_PORT=${PORT}
Environment=XSUKAX_DB=${DATA_DIR}/xsukax_rss.db
# Local translation models, caches and config live in the data dir
# (the xsukax system user has no home directory, so XDG paths must be set):
Environment=XDG_DATA_HOME=${DATA_DIR}/xdg-data
Environment=XDG_CACHE_HOME=${DATA_DIR}/xdg-cache
Environment=XDG_CONFIG_HOME=${DATA_DIR}/xdg-config
Environment=HOME=${DATA_DIR}
Environment=ARGOS_CHUNK_TYPE=MINISBD
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/app.py
Restart=on-failure
RestartSec=3
# Resource-conscious defaults for small VPS (local translation needs RAM
# while a model is loaded — adjust to your VPS size):
MemoryMax=1G
CPUQuota=150%
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}"

    # Optional firewall opening (only if a firewall is active).
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        log "Opening port ${PORT} in ufw..."
        ufw allow "${PORT}/tcp" >/dev/null
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        log "Opening port ${PORT} in firewalld..."
        firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null
        firewall-cmd --reload >/dev/null
    fi

    sleep 1
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
        log "Installation complete."
        echo
        echo "  Web GUI:  http://${IP:-<server-ip>}:${PORT}/"
        echo "  Login:    default password is 'xsukax' — change it after first login!"
        echo "  Models:   install translation models on the 'Translation models' page"
        echo "  Status:   systemctl status ${SERVICE_NAME}"
        echo "  Logs:     journalctl -u ${SERVICE_NAME} -f"
        echo "  Remove:   sudo bash install.sh uninstall"
        echo
    else
        die "Service failed to start. Check: journalctl -u ${SERVICE_NAME} -e"
    fi
}

# =============================================================================
# UNINSTALL
# =============================================================================
do_uninstall() {
    require_root uninstall
    PURGE="${1:-}"
    log "Uninstalling xsukax RSS Filter & Translator..."

    if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}"; then
        log "Stopping and disabling service..."
        systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
        systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
        rm -f "/etc/systemd/system/${SERVICE_NAME}"
        systemctl daemon-reload
        systemctl reset-failed 2>/dev/null || true
    fi

    if [ "${PURGE}" = "--purge" ]; then
        log "Removing application directory (including database and venv)..."
        rm -rf "${INSTALL_DIR}"
    else
        log "Removing application code and venv (keeping data in ${DATA_DIR})..."
        rm -rf "${VENV_DIR}" "${INSTALL_DIR}/app.py" "${INSTALL_DIR}/templates" \
               "${INSTALL_DIR}/static" "${INSTALL_DIR}/requirements.txt" \
               "${INSTALL_DIR}/.tmp"
        # Remove the directory entirely if only an empty data dir remains.
        rmdir --ignore-fail-on-non-empty "${DATA_DIR}" "${INSTALL_DIR}" 2>/dev/null || true
    fi

    if id "${APP_USER}" >/dev/null 2>&1; then
        log "Removing system user '${APP_USER}'..."
        userdel "${APP_USER}" 2>/dev/null || true
    fi

    log "Uninstall complete."
    [ "${PURGE}" != "--purge" ] && [ -d "${DATA_DIR}" ] && \
        echo "  Note: app data kept in ${DATA_DIR}. Use 'uninstall --purge' to delete it." || true
}

# =============================================================================
case "${1:-install}" in
    install)            do_install ;;
    uninstall|remove)   do_uninstall "${2:-}" ;;
    *)                  echo "Usage: sudo bash $0 [install|uninstall [--purge]]"; exit 1 ;;
esac
