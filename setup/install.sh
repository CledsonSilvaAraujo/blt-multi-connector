#!/usr/bin/env bash
# Bootstrap do ambiente no Ubuntu 24.04 para o blt-multi-connector.
# Idempotente: pode ser rodado várias vezes com segurança.

set -euo pipefail

log() { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[erro]\033[0m %s\n' "$*" >&2; }

require_ubuntu() {
    if [[ ! -r /etc/os-release ]]; then
        err "Não é um sistema Linux padrão (sem /etc/os-release)."
        exit 1
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ "${ID:-}" != "ubuntu" ]]; then
        err "Este script foi testado apenas no Ubuntu. Detectado: ${ID:-?}"
        err "Prossiga por sua conta e risco (Ctrl+C para abortar)."
        sleep 3
    fi
    log "Distro: ${PRETTY_NAME:-desconhecida}"
}

check_not_wsl() {
    if grep -qiE '(microsoft|wsl)' /proc/version 2>/dev/null; then
        err "Detectado WSL. WSL não tem acesso estável ao stack Bluetooth."
        err "Este projeto precisa rodar em Linux nativo (instalação dedicada)."
        err "Abortando. Rode em um Ubuntu instalado diretamente no hardware."
        exit 2
    fi
}

apt_install() {
    local packages=(
        bluez
        bluez-tools
        bluez-obexd
        pipewire
        pipewire-pulse
        pipewire-audio-client-libraries
        wireplumber
        libspa-0.2-bluetooth
        pavucontrol
        pulseaudio-utils
        python3
        python3-venv
        python3-pip
        python3-dbus
        python3-gi
        libdbus-1-dev
        libasound2-dev
        libportaudio2
        portaudio19-dev
        git
        curl
        build-essential
    )
    log "Instalando pacotes APT (pode pedir senha)..."
    sudo apt-get update -qq
    sudo apt-get install -y "${packages[@]}"
}

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        log "uv já instalado: $(uv --version)"
        return
    fi
    log "Instalando uv (gerenciador Python)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1090
    [[ -f "$HOME/.local/bin/env" ]] && . "$HOME/.local/bin/env" || true
    export PATH="$HOME/.local/bin:$PATH"
}

enable_services() {
    log "Habilitando serviços do usuário..."
    systemctl --user daemon-reload || true
    systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null || {
        err "Falha ao habilitar serviços do usuário. Se estiver em sessão SSH sem lingering,"
        err "rode: loginctl enable-linger \$USER"
    }

    log "Habilitando bluetooth (system)..."
    sudo systemctl enable --now bluetooth
}

add_user_to_groups() {
    if ! id -nG "$USER" | grep -qw bluetooth; then
        log "Adicionando $USER ao grupo bluetooth..."
        sudo usermod -aG bluetooth "$USER"
        log "Logout/login pode ser necessário para o grupo ter efeito."
    fi
    if ! id -nG "$USER" | grep -qw audio; then
        log "Adicionando $USER ao grupo audio..."
        sudo usermod -aG audio "$USER"
    fi
}

main() {
    require_ubuntu
    check_not_wsl
    apt_install
    ensure_uv
    add_user_to_groups
    enable_services

    log "Concluído. Próximos passos:"
    echo "  1) bash setup/configure-pipewire.sh  # baixa latência + BT tweaks"
    echo "  2) bash setup/test-bluetooth.sh      # valida o stack"
    echo "  3) uv sync                           # instala deps Python do projeto"
    echo "  4) uv run blt-multi --help"
}

main "$@"
