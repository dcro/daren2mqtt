# daren2mqtt

Daren BMS (RS485, YD/T 1363) to MQTT with Home Assistant discovery.

daren2mqtt polls LiFePO4 battery packs with a Daren BMS through an RS485-to-Ethernet
gateway and publishes their state to MQTT. Home Assistant picks every pack up as a device,
much like zigbee2mqtt devices.

- Read-only: it only sends the "read analog values and status" command (42H). It never
  writes to the BMS.
- One container, configured from `docker compose`.
- Images for `linux/amd64` and `linux/arm64` on GHCR.

## Hardware

Each pack's RS485 port (the "PC" or daisy-chain port) goes to a serial gateway set up as a
**raw TCP server**: 9600 baud, 8N1, no protocol conversion. The usual port is 4196. Any
gateway with a transparent TCP mode works, including ones with several RS485 channels on
separate ports or IPs.

`address` is the pack address set on the BMS DIP switches. A pack in master mode also
answers for its slaves, but only with cached values, so query each pack directly when you
can.

Most gateways forward every byte on the bus to every TCP client. Run only one polling client
per channel at a time, or the requests will collide.

## Quick start

```yaml
# compose.yaml
services:
  daren2mqtt:
    image: ghcr.io/dcro/daren2mqtt:latest
    container_name: daren2mqtt
    restart: unless-stopped
    environment:
      MQTT_HOST: 192.168.1.10
      MQTT_USERNAME: daren2mqtt
      MQTT_PASSWORD: change-me
      DAREN2MQTT_CONFIG: |
        packs:
          - id: battery1
            name: Battery 1
            host: 192.168.1.50
            address: 0
          - id: battery2
            name: Battery 2
            host: 192.168.1.51
            address: 1
```

```bash
docker compose up -d
```

The packs show up in Home Assistant under **Settings → Devices & services → MQTT**.

To test one pack without MQTT:

```bash
docker run --rm ghcr.io/dcro/daren2mqtt read 192.168.1.50/0
```

## Configuration

Put the configuration inline in `DAREN2MQTT_CONFIG`, or mount a file at
`/config/config.yaml`. Use `DAREN2MQTT_CONFIG_FILE` to point at another path.

```yaml
interval: 30          # seconds between reads, minimum 5
timeout: 2            # seconds to wait for a reply
log_level: INFO
mqtt:
  host: localhost
  port: 1883
  username:
  password:
  client_id: daren2mqtt
  base_topic: daren2mqtt
  discovery_prefix: homeassistant
packs:
  - id: battery1      # letters, digits, _ and -; used in topics and unique ids
    name: Battery 1   # optional, defaults to id
    host: 192.168.1.50
    port: 4196
    address: 0        # 0..15
```

`MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME` and `MQTT_PASSWORD` override the `mqtt` section.

## MQTT

| Topic | Payload |
| --- | --- |
| `daren2mqtt/bridge/state` | `online` / `offline` (retained, last will) |
| `daren2mqtt/<id>` | JSON state, every `interval` |
| `daren2mqtt/<id>/availability` | `online` / `offline` (retained); offline after 3 failed reads |
| `homeassistant/device/daren2mqtt_<id>/config` | device discovery (retained) |

Discovery is published again when Home Assistant comes online (`homeassistant/status`).

Current and power are **positive while discharging**, as most inverters report them.

Each pack's state contains:

- state of charge, voltage, current, power and status (charging, discharging, idle);
- state of health, remaining and full capacity, and cycles;
- every cell voltage, cell min, max and delta, and the index of the lowest and highest cell;
- ambient, pack and MOS temperatures, plus every temperature sensor;
- charge and discharge MOSFET state;
- balancing and the cells being balanced;
- active alarms, protections and faults ("OK" when none), and a problem flag that turns on
  for protections and faults (alarms are only warnings).

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run daren2mqtt read 192.168.1.50/0
```

Releases are tagged `vX.Y.Z`. CI publishes `X.Y.Z`, `X.Y` and `latest`, and `edge` from `main`.

## Credits

The protocol details come from public research, especially
[cpttinkering/daren-485](https://github.com/cpttinkering/daren-485) and the YD/T 1363
framing it documents.

This project is not affiliated with or endorsed by Daren or any battery manufacturer.

## License

MIT
