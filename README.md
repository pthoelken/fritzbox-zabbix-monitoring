# Zabbix Monitoring for AVM Fritz!Box Devices

Comprehensive monitoring of AVM Fritz!Box routers using **three interfaces**, sending metrics to **Zabbix** via `zabbix_sender` from a Docker container.

| Interface | Data | Activation |
|-----------|------|------------|
| **TR-064** (SOAP/UPnP) | WAN, DSL, WLAN, LAN, Smart Home, services | Always available (port 49000) |
| **LUA** (data.lua) | CPU temp, CPU load, RAM, traffic stats, per-device RSSI | Requires web login (PBKDF2 auth) |
| **Callmonitor** (TCP 1012) | Incoming/outgoing/missed calls, active calls | Dial `#96*5*` on a DECT phone to activate |

## What changed (v2)

- **Rewritten from PHP to Python** using `fritzconnection` + `requests`
- **Three data sources** instead of just TR-064
- **100+ metrics** collected per cycle
- **LUA interface** adds CPU temperature, CPU load, RAM usage, daily/monthly/total traffic volumes, per-device WLAN signal strength (RSSI), and extended DSL statistics
- **Callmonitor** tracks incoming, outgoing, missed, and active calls in real time
- **Device log events** are sent as Zabbix log values with deduplication (uses `X_AVM-DE_GetDeviceLogPath` when available, fallback to `GetDeviceLog`)
- **PBKDF2 authentication** for the LUA interface (with MD5 fallback for older firmware)
- **Zabbix 7 YAML template** with 80+ items, 20+ triggers, template macros
- **Feature toggles**: Enable/disable LUA and Callmonitor independently
- **Multi-arch Docker image** (amd64 + arm64)

## Collected Metrics

| Category | Source | Examples |
|----------|--------|---------|
| **Device** | TR-064 | Model, firmware, serial, uptime, event log stream |
| **WAN** | TR-064 | Connection status, external IPv4/IPv6, bytes/packets, current transfer rates, DNS |
| **DSL** | TR-064 | Sync rates, noise margin (SNR), attenuation, CRC/FEC/HEC errors |
| **DSL Detail** | LUA | Error seconds, loss of signal/frame, capacity |
| **WLAN** | TR-064 | Per-band status (2.4/5/Guest), channel, SSID, clients, packets |
| **LAN** | TR-064 | Active/total hosts, WLAN vs Ethernet breakdown, per-port stats |
| **Smart Home** | TR-064 | Per-device: temperature, power (mW), energy (Wh), switch, battery |
| **System** | LUA | CPU temperature, CPU usage %, RAM usage %, RAM free |
| **Traffic** | LUA | Today/yesterday/week/month/total bytes sent/received |
| **Network Devices** | LUA | Per-device: speed, type, IP, WLAN RSSI, guest flag |
| **Calls** | Callmon | Incoming/outgoing/missed/answered count, active calls, last caller |
| **Updates** | TR-064 | Firmware update available, latest version, lab mode |
| **Services** | TR-064 | MyFRITZ, VPN, UPnP, FTP, SMB status |

## Triggers

| Trigger | Severity | Source |
|---------|----------|--------|
| Collector cannot reach device | HIGH | TR-064 |
| No data for 5 minutes | HIGH | Collector |
| Device recently rebooted | WARNING | TR-064 |
| Firmware version changed | INFO | TR-064 |
| WAN disconnected | DISASTER | TR-064 |
| WAN physical link down | HIGH | TR-064 |
| External IP changed | INFO | TR-064 |
| WAN reconnected (ISP disconnect) | WARNING | TR-064 |
| DSL link not up | HIGH | TR-064 |
| DSL noise margin low | WARNING | TR-064 |
| High CRC error rate | WARNING | TR-064 |
| Firmware update available | WARNING | TR-064 |
| UPnP enabled | INFO | TR-064 |
| CPU temperature high (>80C) | WARNING | LUA |
| CPU temperature critical (>90C) | HIGH | LUA |
| CPU usage high (>80%) | WARNING | LUA |
| RAM usage high (>85%) | WARNING | LUA |
| Missed call | INFO | Callmon |

All thresholds are configurable via template macros.

## Setup

### 1. Create a Fritz!Box monitoring user

**System > Fritz!Box Users > Add User**

- Username: `zabbixmonitor`
- Permissions: enable **Settings** (Einstellungen)
- No dollar signs (`$`) in the password

> **Important:** UPnP must be enabled on the Fritz!Box — otherwise TR-064 cannot retrieve all metrics and errors will appear in the console.
> Enable it under **Home Network > Network > General** → "Allow access for applications" (Zugriff für Anwendungen erlauben).

### 2. Activate the Callmonitor (optional)

Pick up a DECT phone connected to your Fritz!Box and dial:

```
#96*5*
```

Press the green call button. The Callmonitor on TCP port 1012 is now active. To deactivate later, dial `#96*4*`.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

### 4. Import the Zabbix template

Download `templates/template_fritzbox_tr064.yaml` and import it:

**Configuration > Templates > Import**

### 5. Create a Zabbix host

- **Host name**: must match `FRITZBOX_HOSTNAME` exactly (default: `fritz.box`)
- **Template**: link "AVM FritzBox TR-064"

### 6. Start the container

```bash
docker compose up -d
docker compose logs -f
```

Expected output:

```
FritzBox Zabbix Monitor v2 (interval=60s)
Features: TR-064=yes LUA=True Callmonitor=False
TR-064: Connected to FRITZ!Box 7590 (FritzOS 7.57)
LUA: Authenticated (SID=a3f2e1d0...)
Collected 107 metrics
Zabbix sender: sent: 107; skipped: 0; total: 107
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ZABBIX_SERVER` | - | Zabbix server IP (required) |
| `ZABBIX_SERVER_PORT` | `10051` | Zabbix trapper port |
| `TLS_PSK_IDENTITY` | - | PSK identity for encryption |
| `TLS_PSK_FILE` | - | Path to PSK file inside the container (e.g. `/run/secrets/zabbix_psk`) |
| `FRITZBOX_IP` | `192.168.178.1` | Fritz!Box IP |
| `FRITZBOX_PORT` | `49000` | TR-064 port |
| `FRITZBOX_USE_TLS` | `false` | Use HTTPS for TR-064 |
| `FRITZBOX_USER` | - | Fritz!Box user (required) |
| `FRITZBOX_PASSWD` | - | Fritz!Box password (required) |
| `FRITZBOX_HOSTNAME` | `fritz.box` | Zabbix host name |
| `INTERVAL` | `60s` | Check interval (`s`/`m`/`h`) |
| `ZABBIX_SENDER_DEBUG` | `false` | Verbose logging |
| `ENABLE_LUA` | `true` | Enable LUA web interface |
| `ENABLE_CALLMONITOR` | `false` | Enable TCP 1012 callmonitor |
| `CALLMONITOR_PORT` | `1012` | Callmonitor TCP port |
| `DEVICE_LOG_ITEM_KEY` | `fritzbox.device.log` | Zabbix item key used for streamed device log events |
| `DEVICE_LOG_HISTORY_SIZE` | `5000` | Number of log fingerprints kept in memory for deduplication |

## Template Macros

| Macro | Default | Description |
|-------|---------|-------------|
| `{$FRITZBOX.DSL.NOISE_MARGIN.MIN}` | `6` | Min noise margin (dB) |
| `{$FRITZBOX.DSL.CRC_ERRORS.THRESHOLD}` | `100` | CRC error delta threshold |
| `{$FRITZBOX.UPTIME.MIN}` | `600` | Reboot detection threshold (s) |
| `{$FRITZBOX.CPU_TEMP.WARN}` | `80` | CPU temp warning (C) |
| `{$FRITZBOX.CPU_TEMP.HIGH}` | `90` | CPU temp critical (C) |
| `{$FRITZBOX.CPU_USAGE.WARN}` | `80` | CPU usage warning (%) |
| `{$FRITZBOX.RAM_USAGE.WARN}` | `85` | RAM usage warning (%) |

## Building from Source

```bash
git clone https://github.com/pthoelken/fritzbox-zabbix-monitoring.git
cd fritzbox-zabbix-monitoring
chmod +x build
./build          # :latest
./build v2.0   # custom tag
```

## Troubleshooting

**TR-064 errors / metrics missing**: UPnP may be disabled. Enable "Allow access for applications" under **Home Network > Network > General** (Zugriff für Anwendungen erlauben).

**"Hostname required" error**: Remove quotation marks from environment values.

**LUA data empty**: Check that the Fritz!Box user has "Settings" permission. Some data.lua pages require this.

**Callmonitor not connecting**: Verify you activated it by dialing `#96*5*`. Test with `telnet fritz.box 1012`.

**No DSL items**: Normal for Cable/Fiber connections. DSL items only populate with a DSL modem.

**Per-device RSSI not showing**: RSSI data is only available for WLAN clients, and only via the LUA netDev page. The item keys are dynamic (`fritzbox.netdev.<devicename>.rssi`), so they appear as trapper items in Zabbix once the first data arrives.

## DockerHub

https://hub.docker.com/r/pthoelken/fritzbox-zabbix-monitoring

## Issues

https://github.com/pthoelken/fritzbox-zabbix-monitoring/issues
