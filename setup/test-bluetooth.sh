#!/usr/bin/env bash
# Valida o stack BT + PipeWire. Não modifica nada, apenas diagnostica.

set -uo pipefail

ok()   { printf '\033[1;32m[ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[fail]\033[0m %s\n' "$*"; FAILED=1; }
info() { printf '\033[1;36m[info]\033[0m %s\n' "$*"; }

FAILED=0

# 1) Kernel não-WSL
if grep -qiE '(microsoft|wsl)' /proc/version 2>/dev/null; then
    fail "Kernel WSL detectado. BT não vai funcionar aqui."
else
    ok "Kernel não-WSL."
fi

# 2) Serviço bluetooth
if systemctl is-active --quiet bluetooth; then
    ok "bluetooth.service ativo."
else
    fail "bluetooth.service não está ativo. Rode: sudo systemctl enable --now bluetooth"
fi

# 3) Adaptador HCI
if command -v bluetoothctl >/dev/null 2>&1; then
    ADAPTERS=$(bluetoothctl list 2>/dev/null || true)
    if [[ -n "$ADAPTERS" ]]; then
        ok "Adaptadores BlueZ detectados:"
        echo "$ADAPTERS" | sed 's/^/     /'
    else
        fail "Nenhum adaptador BlueZ listado (bluetoothctl list vazio)."
    fi
else
    fail "bluetoothctl ausente."
fi

# 4) rfkill
if command -v rfkill >/dev/null 2>&1; then
    if rfkill list bluetooth 2>/dev/null | grep -q 'Soft blocked: yes'; then
        fail "Bluetooth soft-blocked. Rode: rfkill unblock bluetooth"
    else
        ok "Bluetooth não está bloqueado (rfkill)."
    fi
fi

# 5) PipeWire rodando
if command -v pw-cli >/dev/null 2>&1; then
    if pw-cli info 0 >/dev/null 2>&1; then
        ok "PipeWire acessível."
    else
        fail "PipeWire não responde (pw-cli info 0 falhou)."
    fi
else
    fail "pw-cli ausente."
fi

# 6) Módulo bluez5 carregado no WirePlumber
if command -v wpctl >/dev/null 2>&1; then
    if wpctl status 2>/dev/null | grep -qi bluez; then
        ok "WirePlumber carregou módulo bluez."
    else
        warn "WirePlumber sem módulo bluez aparente (normal se nenhum device conectado ainda)."
    fi
    info "Resumo do wpctl status (primeiras 30 linhas):"
    wpctl status 2>/dev/null | head -30 | sed 's/^/     /'
fi

# 7) Python + uv
if command -v python3 >/dev/null 2>&1; then
    ok "Python: $(python3 --version)"
else
    fail "python3 ausente."
fi
if command -v uv >/dev/null 2>&1; then
    ok "uv: $(uv --version)"
else
    warn "uv ausente. Rode setup/install.sh ou: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

echo
if [[ $FAILED -eq 0 ]]; then
    ok "Diagnóstico passou. Prossiga para 'uv sync' e 'uv run blt-multi pair'."
    exit 0
else
    fail "Diagnóstico encontrou problemas acima. Resolva antes de seguir."
    exit 1
fi
