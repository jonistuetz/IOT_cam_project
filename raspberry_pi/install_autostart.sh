#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${1:-hs-iot-doorbell}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
POWEROFF_HELPER="/usr/local/sbin/hs-iot-safe-poweroff"
SUDOERS_FILE="/etc/sudoers.d/hs-iot-safe-poweroff"
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"

if [[ ! -f "${PROJECT_DIR}/face_verifier.py" ]]; then
  echo "face_verifier.py wurde in ${PROJECT_DIR} nicht gefunden." >&2
  exit 1
fi

if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
elif [[ -x "${PROJECT_DIR}/.iotp/bin/python" ]]; then
  PYTHON_BIN="${PROJECT_DIR}/.iotp/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

TMP_SERVICE="$(mktemp)"
cat > "${TMP_SERVICE}" <<EOF
[Unit]
Description=HS IoT Smart Doorbell
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PYTHON_BIN} ${PROJECT_DIR}/face_verifier.py serve --host 0.0.0.0 --port 8000
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
User=${RUN_USER}
Group=${RUN_GROUP}

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 0644 "${TMP_SERVICE}" "${SERVICE_FILE}"
rm -f "${TMP_SERVICE}"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"

TMP_HELPER="$(mktemp)"
cat > "${TMP_HELPER}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
/usr/bin/systemctl poweroff
EOF
sudo install -m 0755 "${TMP_HELPER}" "${POWEROFF_HELPER}"
rm -f "${TMP_HELPER}"

TMP_SUDOERS="$(mktemp)"
cat > "${TMP_SUDOERS}" <<EOF
${RUN_USER} ALL=(root) NOPASSWD: ${POWEROFF_HELPER}
EOF
sudo visudo -cf "${TMP_SUDOERS}" >/dev/null
sudo install -m 0440 "${TMP_SUDOERS}" "${SUDOERS_FILE}"
rm -f "${TMP_SUDOERS}"

echo "Autostart installiert: ${SERVICE_FILE}"
echo "Shutdown-Helper installiert: ${POWEROFF_HELPER}"
echo "Projektordner: ${PROJECT_DIR}"
echo "Python: ${PYTHON_BIN}"
echo
sudo systemctl --no-pager --lines=20 status "${SERVICE_NAME}.service"
