#!/usr/bin/env bash
# Configura PipeWire/WirePlumber para baixa latência e sinks BT mais previsíveis.
# Não mexe em nada do sistema; tudo fica em ~/.config.

set -euo pipefail

log() { printf '\033[1;36m[pipewire]\033[0m %s\n' "$*"; }

PW_CONF_DIR="$HOME/.config/pipewire/pipewire.conf.d"
WP_CONF_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"

mkdir -p "$PW_CONF_DIR" "$WP_CONF_DIR"

# Drop-in de baixa latência. Quantum 256 @ 48k = ~5.3ms de granularidade para
# o grafo do PipeWire. Reduzir mais (128) é possível mas causa xruns em BT.
cat > "$PW_CONF_DIR/10-blt-multi-lowlatency.conf" <<'EOF'
# Gerado por blt-multi configure-pipewire.sh
context.properties = {
    default.clock.rate          = 48000
    default.clock.allowed-rates = [ 44100 48000 ]
    default.clock.quantum       = 256
    default.clock.min-quantum   = 128
    default.clock.max-quantum   = 1024
}
EOF
log "Escrevi $PW_CONF_DIR/10-blt-multi-lowlatency.conf"

# Drop-in do WirePlumber: força SBC nos BT sinks (latência mais previsível
# que AAC, que varia muito entre dispositivos). Também liga msbc e desliga
# codecs caros por padrão. Usuário pode editar o arquivo depois.
cat > "$WP_CONF_DIR/51-blt-multi-bluez.conf" <<'EOF'
# Gerado por blt-multi configure-pipewire.sh
monitor.bluez.properties = {
    bluez5.enable-sbc-xq   = true
    bluez5.enable-msbc     = true
    bluez5.enable-hw-volume= true
    bluez5.roles           = [ a2dp_sink a2dp_source ]
    # Ordem de preferência: SBC-XQ primeiro (previsível), depois os demais.
    bluez5.codecs          = [ sbc_xq sbc aac ]
    bluez5.hfphsp-backend  = "native"
}
EOF
log "Escrevi $WP_CONF_DIR/51-blt-multi-bluez.conf"

log "Reiniciando serviços do usuário..."
systemctl --user restart wireplumber pipewire pipewire-pulse || {
    log "AVISO: não foi possível reiniciar via systemctl (talvez sessão sem user bus)."
    log "Faça logout/login ou reboot para aplicar."
}

log "Pronto. Verifique com: pw-metadata -n settings 0"
