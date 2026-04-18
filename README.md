# blt-multi-connector

Espelhar o áudio do sistema em **vários dispositivos Bluetooth simultaneamente**
com offset de latência calibrado **por dispositivo**, em Linux nativo
(Ubuntu 24.04) sobre PipeWire + BlueZ.

Sincronia alvo realista: **40–80 ms entre dispositivos heterogêneos** (SBC/AAC)
depois de calibrados. Esse projeto **não consegue** e não promete sincronia
<20 ms com fones BT genéricos — isso seria físico-impossível sem LE Audio.
Leia o [contexto técnico](#contexto-tecnico) antes de decidir se é o que você
quer.

---

## Por que não Windows / WSL / Docker

- **WSL:** o kernel do WSL2 não expõe o stack Bluetooth do host; não funciona.
- **Docker:** Bluetooth exige acesso ao HCI do host; container não ajuda aqui.
- **Windows:** não suporta multi-output A2DP nem offset por sink BT.

Portanto este projeto pressupõe **Ubuntu 24.04 LTS rodando direto no hardware**
(notebook antigo, mini-PC, etc.).

---

## Instalação

```bash
# 1. No Ubuntu 24.04 dedicado:
git clone <seu-fork>/blt-multi-connector.git
cd blt-multi-connector

# 2. Bootstrap (instala bluez, pipewire, uv, etc.)
bash setup/install.sh

# 3. Config de baixa latência + tweaks BT
bash setup/configure-pipewire.sh

# 4. Valida o stack
bash setup/test-bluetooth.sh

# 5. Deps Python do projeto
uv sync

# 6. CLI disponível via:
uv run blt-multi --help
```

---

## Uso básico

```bash
# Escaneia e pareia devices (interativo).
uv run blt-multi pair

# Conecta tudo o que está no store.
uv run blt-multi connect --all

# Calibração manual (slider): inicie sua música antes, depois rode:
uv run blt-multi calibrate manual AA:BB:CC:DD:EE:01

# OU calibração por microfone (chirp + cross-correlation):
uv run blt-multi calibrate mic

# Aplica offsets e monta o combine-sink como default.
uv run blt-multi sync

# Status completo.
uv run blt-multi status
```

A partir de agora, **qualquer app** (Spotify, YouTube, VLC, navegador) que
escreva no sink padrão será espelhado nos N dispositivos com offset
individual. Para voltar ao normal:

```bash
uv run blt-multi unsync
```

### Painel web (opcional)

```bash
uv run blt-multi web --host 127.0.0.1 --port 8765
# abra http://localhost:8765
```

---

## Daemon em background

O daemon escuta D-Bus do BlueZ e **reaplica offsets automaticamente** quando
um dispositivo reconecta (ex: você ligou os fones de novo depois de um tempo
desligados). Instale como serviço do usuário:

```bash
bash systemd/install-service.sh
journalctl --user -u blt-multi -f   # logs ao vivo
```

---

## Calibração: como pensar

O offset por dispositivo é positivo e equivalente a "atrasar esta saída".
Portanto o device **mais lento** (o que chega depois) vira a referência com
offset=0; todos os outros recebem offset positivo para alinhar.

A calibração por microfone mede o **round-trip de ida** (PC → dispositivo →
ar → microfone) e assume que a geometria é parecida para todos os devices.
Se um fone está no seu ouvido direito e outro a 3 m do mic, o erro
geométrico fica na casa de 10 ms — tolerável dentro da meta.

### Recomendações práticas

- Coloque os dispositivos próximos ao microfone do notebook durante a
  calibração por mic.
- Faça 3+ repetições (o default) — a mediana elimina outliers de ruído.
- Para sessões longas (>30 min), revalide a calibração: BT classic tem
  drift de clock; o driver renegocia ocasionalmente e pode variar ~5 ms.

---

## Arquitetura resumida

```
Apps (Spotify, YT, VLC)
        ↓
[ combine-sink blt_multi_combined ]  ← default sink
        ↓        ↓        ↓
   BT sink A  BT sink B  BT sink C
   offset+X   offset+Y   offset=0   ← aplicado via pactl set-port-latency-offset
        ↓        ↓        ↓
    Fone A   Caixa B    Fone C
```

Persistência em `~/.config/blt_multi/devices.toml`. O daemon observa
`InterfacesAdded/Removed` e `PropertiesChanged` do BlueZ (via dbus-next) e
reconstrói o combine-sink + reaplica offsets.

---

## Contexto técnico

Bluetooth A2DP (SBC/AAC) tem **150–300 ms** de latência total (encode →
transmissão → buffer → decode → DAC) que **varia por dispositivo, por
codec e até por sessão de pareamento**. Você não zera isso; você só
compensa atrasando os rápidos até o mais lento.

Para sincronia real abaixo de 10 ms, **LE Audio / Auracast** é a única
tecnologia que foi desenhada para broadcast sincronizado — e exige
hardware compatível dos dois lados (adaptador USB BT 5.2+ com LC3 e fones
suportando Auracast).

Se você precisar dessa precisão, a alternativa prática é **Snapcast sobre
WiFi** com receptores Raspberry Pi que fazem a ponte BT local. Isso está
fora do escopo deste repositório.

---

## Desenvolvimento

```bash
uv sync --extra dev
uv run pytest               # roda os testes (mockam bluetoothctl/pactl)
uv run ruff check .
uv run mypy blt_multi
```

Estrutura:

- `blt_multi/` — pacote principal
  - `discovery.py`, `pairing.py` — BlueZ via `bluetoothctl`
  - `sinks.py` — PipeWire via `pactl`
  - `store.py`, `models.py` — persistência TOML + tipos
  - `calibration.py` — manual (prompt_toolkit) + mic (chirp + scipy)
  - `daemon.py` — watcher D-Bus assíncrono
  - `cli.py` — Typer + Rich
  - `web/` — FastAPI + HTMX (opcional)
- `setup/` — scripts de bootstrap
- `systemd/` — user service + installer
- `tests/` — pytest com subprocess mockado

---

## Licença

MIT.
# blt-multi-connector
