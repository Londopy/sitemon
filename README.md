# sitemon

[![CI](https://github.com/Londopy/sitemon/actions/workflows/ci.yml/badge.svg)](https://github.com/Londopy/sitemon/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Londopy/sitemon)](https://github.com/Londopy/sitemon/releases)
[![PyPI](https://img.shields.io/pypi/v/sitemon)](https://pypi.org/project/sitemon/)
[![Python](https://img.shields.io/pypi/pyversions/sitemon)](https://pypi.org/project/sitemon/)
[![License](https://img.shields.io/github/license/Londopy/sitemon)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi%20%7C%20Linux-informational)

**Find out a repeater site is in trouble before someone keys up and nothing answers.**

sitemon is a small agent for the Raspberry Pi at each repeater or AllStar site, plus a hub that
watches all of them. The agent watches supply voltage and current, temperature and humidity, the
door, AllStar node state and the local AREDN node. The hub alerts people (Discord, ntfy, email) and
shows every site on one page. It keeps working over flaky links: readings queue at the site and
catch up when any path returns.

```console
$ sitemon demo
Dashboard: http://127.0.0.1:8470  (data in /tmp/sitemon-demo-1a2b; Ctrl+C to stop)
```

## Features

- **Power:** battery, solar and PSU rails via INA226 (or INA219 for lower voltage).
- **Environment:** temperature, humidity and pressure via BME280.
- **Door and water:** any reed switch or float switch on a GPIO pin.
- **AllStar:** keyed state, linked nodes and uptime over AMI, on localhost only, with an AMI user
  that can query status and nothing else. Registration status is an opt-in extra.
- **AREDN:** node reachability, firmware, links and RF links from the local node.
- **RF:** forward and reflected power and SWR while transmitting, from a directional coupler and
  two AD8307 detectors, so a failing antenna or feedline shows up as a trend.
- **Relays:** power-cycle a hung radio or node, or switch shack lights, with commands signed by
  your own key. Every relay has a time limit, and a hardware cutoff drops them if the Pi hangs.
- **Rules:** thresholds with hysteresis, slow trends ("battery fell 0.3 V in a week"), state
  changes, sensor faults, and silent sites.
- **Alerts:** one message when something breaks, one when it escalates, one when it's fixed,
  by Discord, ntfy, email or SMS. Queued and retried when the hub's internet path is down.
  Optionally spoken on the repeater itself.
- **Dashboard:** a tile per site with sparklines, a plain-language status, a page per site with
  7-day charts and alert history, and CSV export for every sensor. Weekly summary to Discord.
- **Built for transmitter sites:** I2C error recovery, chips re-initialised after RF-induced
  resets, systemd watchdog, batched writes to spare the SD card.

## Install

On each site's Pi (ASL3 / Raspberry Pi OS bookworm ship Python 3.11):

```bash
python3 -m venv /opt/sitemon
/opt/sitemon/bin/pip install "sitemon[pi]"
```

On the hub (any Linux box on the mesh, or a small VPS):

```bash
python3 -m venv /opt/sitemon
/opt/sitemon/bin/pip install sitemon
```

[deploy/README.md](deploy/README.md) has the full walk-through: wiring, the AMI user,
systemd units and a read-only root. Public hub deployments sit behind
[Anubis](https://github.com/TecharoHQ/anubis) to keep AI scrapers off; see `deploy/hub/`.

## Usage

1. On the site, make its signing key and note the public key it prints:

   ```bash
   sitemon keygen --out /etc/sitemon/site.key
   ```

2. Add the site and its public key to the hub config, and point the agent at the hub
   (examples in [deploy/](deploy/)).

3. Check both configs, then start the services:

   ```bash
   sitemon check /etc/sitemon/agent.toml
   systemctl enable --now sitemon-agent
   ```

Each site also serves a small status page on `127.0.0.1:8471` for whoever is standing in the
shack.

## Configuration

| File | Where |
|---|---|
| Agent | `/etc/sitemon/agent.toml` ([example](deploy/agent.example.toml)) |
| Hub | `/etc/sitemon/hub.toml` ([example](deploy/hub.example.toml)) |
| Site key | `/etc/sitemon/site.key` (0600, made by `sitemon keygen`) |

Secrets never go in the TOML: the AMI password, webhook URLs and SMTP password are read from an
environment variable (`*_env`) or a file (`*_file`), so a config can be pasted into a bug report
as-is.

```toml
# hub.toml (excerpt)
[[rules]]
name = "Battery low"
metric = "batt.v"          # <sensor name>.<value>
below = 12.2               # warn
critical_below = 11.8
hysteresis = 0.1           # stays open until 12.3 V, so it doesn't flap
hold = "2m"                # must stay low 2 minutes; a key-up sag never alerts
unit = "V"

[[rules]]
name = "Battery falling"
type = "rate"
metric = "batt.v"
window = "7d"              # compare with a week ago...
smooth = "1d"              # ...using daily averages, so the solar cycle cancels out
drop = 0.3
unit = "V"
```

## How is this different from Allmon3 or a PacketFlux SiteMonitor?

| | Allmon3 / Supermon | PacketFlux / NetGuardian | sitemon |
|---|---|---|---|
| AllStar link and keyed state | ✅ | ❌ | ✅ |
| Site power, temperature, door | ❌ | ✅ | ✅ |
| Every site on one page | ❌ | via your NMS | ✅ |
| Alerts when a site goes silent | ❌ | via your NMS | ✅ |
| Keeps readings through a link outage | ❌ | ❌ | ✅ |
| Cost per site, excluding the Pi | free | $130+ | about $40-80 in parts |

sitemon doesn't replace Allmon3; run both. It reads AMI the same way, read-only.

## What it will not do

- **Replace the repeater controller.** Relays power-cycle equipment and switch accessories; they
  don't key the transmitter or change its settings. Commands only travel over wireline paths
  (internet, cellular), never the mesh, unless you run a proper auxiliary station (47 CFR 97.213).
- **Encrypt data over the mesh.** Part 97 doesn't allow it. Everything is signed instead, so the
  hub can tell a real reading from a forged one.
- **Measure RF power precisely.** Forward and reflected power are tracked for trends; it isn't a
  calibrated wattmeter.

## How it works

```
[site]  sensors --I2C/GPIO--> Pi: sitemon agent <--AMI 127.0.0.1-- Asterisk (app_rpt)
                                  |  <--HTTP-- AREDN node /a/sysinfo
                                  |  signed events, SQLite queue
[path]  AREDN mesh / cellular / internet: every configured hub gets every event
[hub]   verify signatures -> rules -> alerts (Discord, ntfy, email) -> dashboard
```

- **Every event is signed** (Ed25519) and numbered with a per-site counter that never repeats,
  even after a power cut. The hub stores each number once, so a retried upload is harmless.
- **Time without a trusted clock:** each event carries the agent's monotonic clock as well as its
  wall clock. The hub learns the offset from each upload and places events by that, so a Pi that
  boots without NTP after an outage still files its readings at the right time.
- **Store-and-forward:** the agent keeps each event until every configured hub (say, one on the
  mesh and one on the internet) confirms it has it, backing off separately for each while its
  path is down.
- **Silence is an alarm:** the hub raises it from its own clock, because a dead site can't report
  itself.

## Building from source

```bash
git clone https://github.com/Londopy/sitemon
cd sitemon
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check . && mypy sitemon
```

No hardware needed: the tests use simulated sensors and a fake AMI server.

## Layout

```
sitemon/
  _events.py        # events, Ed25519 signing, upload envelope
  _commands.py      # signed commands: relays and spoken alerts
  config.py         # agent and hub TOML
  agent/
    runtime.py      # sampling loops, heartbeat, sync, local status page
    store.py        # SQLite event queue, never-repeating sequence numbers
    ami.py          # AMI client (loopback only, allow-listed requests)
    relays.py       # relay outputs and the keep-alive pin
    announce.py     # spoken alerts: text to speech, rpt localplay
    aredn.py        # AREDN sysinfo
    sensors/        # INA226/219, BME280, RF (ADS1115), GPIO, I2C recovery, simulated sensors
  hub/
    ingest.py       # signature checks, de-duplication, time placement
    rules.py        # thresholds, trends, states, faults, silence
    alerts.py       # de-duplication, escalation, outbox
    channels.py     # Discord, ntfy, email, SMS
    commands.py     # queue of signed commands waiting for their site
    server.py       # HTTP API, dashboard, CSV, background loops
    static/         # dashboard (plain HTML/JS/CSS)
deploy/             # example configs, systemd units, hub Docker + Anubis
tests/
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: it's a monitor, not a controller.

## License

MIT — see [LICENSE](LICENSE).
