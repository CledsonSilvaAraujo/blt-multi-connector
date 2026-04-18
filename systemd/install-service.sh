#!/usr/bin/env bash
# Instala a unit do blt-multi como user service.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$SCRIPT_DIR/blt-multi.service"
USER_UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$USER_UNIT_DIR"
cp "$UNIT_SRC" "$USER_UNIT_DIR/blt-multi.service"
echo "[install] copiado para $USER_UNIT_DIR/blt-multi.service"

systemctl --user daemon-reload
systemctl --user enable blt-multi.service
systemctl --user restart blt-multi.service

# Garante que o serviço sobreviva a logout (linger).
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
    echo "[install] habilitando linger (precisa de sudo)..."
    sudo loginctl enable-linger "$USER"
fi

echo
echo "Status:"
systemctl --user --no-pager status blt-multi.service || true
echo
echo "Logs ao vivo: journalctl --user -u blt-multi -f"
