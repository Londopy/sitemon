# Deploying sitemon

One agent per site, and one hub for all of them (sites can be split into groups). Budget an afternoon for the
first site: most of it is wiring.

Get the site owner's OK before putting hardware on their property.

## 1. Wire the sensors

All sensors share the Pi's I2C bus (pins 3 SDA and 5 SCL, 3.3 V on pin 1, ground on pin 6).

| Part | Connect | Notes |
|---|---|---|
| INA226 / INA219 | I2C; shunt in the **negative** lead of the rail you measure; VBUS to the rail's positive | Keep the shunt-sense leads short and twisted. Above ~10 A use an external shunt and set `shunt_ohms` (a 75 mV / 50 A shunt is 0.0015 Ω). |
| BME280 | I2C | Mount it away from the PA and PSU, or you'll be monitoring the heatsink. |
| Door reed switch | GPIO 17 and ground | Uses the Pi's internal pull-up: `active_when = "high"`, `key = "open"`. |
| Water float switch | GPIO 27 and ground | `key = "present"`, `active_when` depending on the switch. |

At a transmitter site:

- Use **shielded cable** for any I2C run longer than a few cm, shield grounded at the Pi end only,
  and keep runs under ~1 m. Clip-on ferrites on the leads help.
- Put the Pi and sensors in a metal enclosure if the RF field at the rack is strong.
- sitemon re-initialises a chip that an RF burst has reset, retries failed reads and reopens the
  bus; three failures in a row become a sensor-fault alert. That handles glitches, not a bus
  that's swamped full-time: if faults keep coming back, fix the wiring.

Enable I2C with `sudo raspi-config` (Interface Options > I2C) and check the chips answer:

```bash
sudo apt install i2c-tools
i2cdetect -y 1        # expect 40 (INA) and 76 or 77 (BME280)
```

## 2. Install the agent

```bash
sudo useradd --system --home /var/lib/sitemon --groups i2c,gpio sitemon
# gpiozero needs a pin backend; on Raspberry Pi OS bookworm and ASL3 that's lgpio from apt.
# --system-site-packages lets the venv see it.
sudo apt install python3-lgpio
sudo python3 -m venv --system-site-packages /opt/sitemon
sudo /opt/sitemon/bin/pip install "sitemon[pi]"
sudo mkdir -p /etc/sitemon
sudo /opt/sitemon/bin/sitemon keygen --out /etc/sitemon/site.key
sudo chown sitemon: /etc/sitemon/site.key
```

`keygen` prints the site's public key. It goes in the hub config, in the site's `[[sites]]`
entry. The private key never leaves the site.

Copy [agent.example.toml](agent.example.toml) to `/etc/sitemon/agent.toml` and edit it.

## 3. An AMI user for sitemon

sitemon only reads from Asterisk, and only over loopback. Give it its own AMI user with nothing
more than it needs. In `/etc/asterisk/manager.conf`:

```ini
[general]
enabled = yes
port = 5038
bindaddr = 127.0.0.1        ; AMI must never be reachable from the mesh

[sitemon]
secret = <a long random password>
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
read = system,call
write = reporting          ; status queries (CoreStatus, for uptime)
```

Asterisk checks every AMI *action* against the user's `write` classes; `read` only filters
events. Node status (`RptStatus`) needs no class at all, and `reporting` allows only status
queries (plus rotating Asterisk's logs), so this user can't change the node. Without
`write = reporting` sitemon still works and just leaves uptime out.

Then `sudo asterisk -rx "manager reload"`. Put the password where only the service can read it:

```bash
echo 'SITEMON_AMI_SECRET=<the password>' | sudo tee /etc/sitemon/agent.env
sudo chmod 600 /etc/sitemon/agent.env
```

**Optional: the registration check.** Whether the node is registered with AllStarLink is only
available as a CLI command, and AMI only runs CLI commands for users with `write = command`.
That permission is not read-only: it allows *any* CLI command, including ones that stop
Asterisk or key the transmitter. For this check sitemon only ever sends `rpt show registrations`
or `iax2 show registry` (and `rpt localplay` if you turn on spoken alerts, section 9; anything
else is refused in code), but the password then unlocks more than monitoring. If you want the check anyway, make it `write = reporting,command` above and set
`registration_command` in agent.toml to whichever of the two your node answers (ASL3 registers
over HTTP: `sudo asterisk -rx "rpt show registrations"`; older builds use `iax2 show registry`).

## 4. Start it

```bash
sudo cp deploy/systemd/sitemon-agent.service /etc/systemd/system/
sudo /opt/sitemon/bin/sitemon check /etc/sitemon/agent.toml
sudo systemctl daemon-reload
sudo systemctl enable --now sitemon-agent
journalctl -u sitemon-agent -f
curl -s http://127.0.0.1:8471/status.json
```

## 5. Protect the SD card

The agent already batches writes (one small commit every `flush_interval`). For a site you
can't easily visit, also:

- Use an industrial / high-endurance card.
- Turn on the overlay root (`raspi-config` > Performance > Overlay file system), with
  `/var/lib/sitemon` on a separate writable partition or a USB stick, so the event queue
  survives reboots while the rest of the card stays read-only.
- Turn on the hardware watchdog so a hung kernel reboots the Pi: add `RuntimeWatchdogSec=15` to
  `/etc/systemd/system.conf`. The agent's own `WatchdogSec` restarts a hung agent.

## 6. The hub

Any Linux box works: a Pi on the mesh, a server in the shack, a small VPS. Run it in two places
if you like (mesh and internet): every hub listed in an agent's `hubs` gets every event, each
at its own pace, so the internet mirror has the full history even while the mesh hub is the one
answering. A site only deletes an event once every hub has it.

```bash
sudo useradd --system --home /var/lib/sitemon sitemon
sudo python3 -m venv /opt/sitemon
sudo /opt/sitemon/bin/pip install sitemon
sudo cp deploy/hub.example.toml /etc/sitemon/hub.toml        # add each site and its key
echo 'SITEMON_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...' | sudo tee /etc/sitemon/hub.env
sudo chmod 600 /etc/sitemon/hub.env
sudo /opt/sitemon/bin/sitemon check /etc/sitemon/hub.toml
sudo cp deploy/systemd/sitemon-hub.service /etc/systemd/system/
sudo systemctl enable --now sitemon-hub
```

The dashboard is at `http://<hub>:8470/`. `sitemon summary --config /etc/sitemon/hub.toml` prints
the weekly summary on demand.

### Internet mirror (Docker, behind Anubis)

A hub that faces the internet sits behind [Anubis](https://github.com/TecharoHQ/anubis), which
keeps AI scrapers off the dashboard. Site uploads to `/api/ingest` are let straight through, since agents can't solve the challenge. That's safe
because the hub checks every upload's signature: a forged one is refused and changes nothing.

```bash
cd deploy/hub
cp ../hub.example.toml hub.toml          # edit; set listen = "0.0.0.0:8470"
cp hub.env.example hub.env               # fill in the webhook
echo "ED25519_PRIVATE_KEY_HEX=$(openssl rand -hex 32)" > .env
docker compose up -d --build
```

`hub.toml`, `hub.env` and `.env` are git-ignored. Point your reverse proxy (Caddy, nginx) at
`127.0.0.1:8923` and set `COOKIE_DOMAIN` in `docker-compose.yml` to your domain. The proxy must
pass the client address in `X-Real-IP` (see the Anubis installation docs for Caddy and nginx);
without it Anubis refuses requests. Set `TZ` in `docker-compose.yml` so the weekly summary goes
out at your local hour.

## 7. Relays and remote commands

A relay board on the Pi's GPIO lets you power-cycle a radio or node, or switch shack extras, from
your own computer. Each relay has a mode and a time limit, and starts off:

- `pulse`: on for `pulse` seconds, then off. Wire a radio's or node's supply through the relay's
  normally-closed contact, and a pulse cuts the power for that long. A doorbell is a short pulse.
- `switch`: on and off on command, but never on longer than `max_on` (shack lights).

Every relay is held to 170 s unless you mark it `transmitter = false`, so one you forgot to think
about can't keep a station on the air; see Part 97 below. Shack lights and other things that
can't make the station transmit get `transmitter = false`, and then up to a day.

**Hardware cutoff.** Software limits aren't enough on their own: if the Pi hangs with a relay
energised, it stays energised. There are two ways to wire a cutoff, and you can use both:

- `[control] watchdog_pin`: one pin for the whole relay board. The agent toggles it every second
  while it's healthy, and the circuit below switches the board's coil supply.
- `watchdog_pin` on a `[[relays]]` entry: a pin of that relay's own. The agent toggles it only
  while that relay is meant to be on and the agent is healthy, so the relay also drops out if its
  GPIO pin is somehow left on. Build the same circuit per relay, switching only that relay's coil
  (or gating its input). The relay energises on the first toggle, up to a second after the
  command.

The circuit, for the board-wide pin:

- Watchdog pin into the rising-edge trigger input of a retriggerable monostable: half of a
  74HC123, run from 3.3 V, with the unused half's inputs and the reset tied to fixed levels. The
  pin flips every second, so a rising edge comes every 2 s; set the period to about 3 s with the
  timing resistor and capacitor (from the datasheet formula; a film or other low-leakage
  capacitor, since the charge current is tiny).
- The monostable's output drives an N-channel MOSFET rated for 3.3 V gate drive, in the ground
  lead of the relay board's coil supply. On the usual opto-isolated boards that is JD-VCC, with
  the VCC/JD-VCC jumper removed so the coils don't also run from the Pi's 5 V.
- If the agent hangs, the Pi crashes or the pin stops toggling for any other reason, the output
  drops within about 3 s and every relay releases, whatever its GPIO pin is doing.

Before trusting it, check it on the bench: with a relay on, freeze the agent with
`sudo kill -STOP <its PID>` (from `systemctl show -p MainPID sitemon-agent`) and the relay should
release within about 3 s; `kill -CONT` lets it carry on. Stopping the service isn't a test,
because the agent switches its relays off itself on the way out. This circuit hasn't been built
and measured as part of sitemon yet.

**Part 97.** Switching a radio or node is remote control of the station (47 CFR 97.213). The
control link must be wireline (internet, cellular) unless you run a proper auxiliary station on
2 m or up (97.201). So:

- List only hubs reached over the internet or cellular under `[control] hubs`, by https and by
  their full name. sitemon refuses plain http unless the hub is on the Pi itself, and refuses
  anything that may be the mesh: `*.mesh` names, bare names (a mesh node completes them to
  `.local.mesh`) and 10.x addresses, AREDN's range. The agent also never connects to a control
  hub at a 10.x address, whatever its name resolves to, and command polls don't follow
  redirects. A VPN that uses 10.x addresses can't carry commands for that reason. What sitemon
  can't check is a hub on some other address that the site reaches over the mesh anyway,
  through a tunnel or a gateway, so never list one.
- 97.213 also asks that a station under remote control stop transmitting within 3 minutes if the
  control link fails. sitemon can't see transmissions; what it does is switch every relay
  (except those marked `transmitter = false`) off about 170 s at most after the command that
  switched it on (the relay loop checks once a second), whether the hub, the link or the rest of
  the agent is still working or not. So nothing a command started through a relay outlasts that,
  with the cutoff circuit as the backstop if the Pi hangs. The repeater's own time-out timer still limits each transmission, as it always has.

**Operator keys.** Each person who may send commands makes their own key, on their own computer:

```bash
sitemon keygen --out ~/.config/sitemon/operator.key
```

Put the public key it prints in the site's agent config (`[control] operators`) and in the hub
config (`[[operators]]`, with the sites that person may command). The private key never leaves
their computer: the hub only passes commands on, and can't make one.

**Sending a command:**

```bash
sitemon command hilltop radio pulse --hub https://sitemon.example.org --key ~/.config/sitemon/operator.key
```

The site collects it the next time it syncs (every 30 s by default), checks the signature, the
expiry (2 minutes) and that it hasn't seen the command before, and reports back.
`sitemon command` waits up to 90 s and prints the result. The site's dashboard page lists recent
commands, and the relays' state under its latest values.

## 8. RF power trend

A directional coupler on the feedline, with an AD8307 log detector on each of its forward and
reflected ports, feeding an ADS1115 ADC on the Pi's I2C bus. sitemon records forward and reflected
power, return loss and SWR while the transmitter is keyed, so a feedline or antenna that is
slowly getting worse shows up as a trend weeks before it fails.

- Put enough attenuation in front of each AD8307 to keep it under +15 dBm at full power, and add
  the coupler's coupling factor and the attenuators together as `coupling_db`.
- Calibrate against a wattmeter: transmit at two known powers, read `rf` on the site's status page,
  and adjust `slope_mv_per_db` and `intercept_dbm` until they agree. It's a trend monitor, so
  close is good enough. How close it gets, and how SWR readings compare with a real meter, hasn't
  been measured yet; treat early numbers with care.
- Rules like `SWR above 2` (with a `hold`) and `SWR rose 0.3 in 7 days` are in
  [hub.example.toml](hub.example.toml).

## 9. Spoken alerts on the air

Off unless you turn it on. When a rule marked `announce = true` goes critical, the hub asks the
site to say so on the repeater ("Hilltop repeater. Battery low"), at most once every 10 minutes.

1. On the site: `sudo apt install espeak-ng sox`.
2. Make the hub's own key on the hub: `sitemon keygen --out /etc/sitemon/hub.key`, set
   `[hub] key` to it, and put the public key it prints in the site's `[control] hub_keys`. The
   hub's key can only ask for announcements, never relays.
3. Give the AMI user `write = reporting,command`. As section 3 says, `command` allows any CLI
   command, so treat that password with care. sitemon itself only ever sends `rpt localplay` with
   a file it wrote.
4. Set `[announce] enabled = true` in the agent config and `announce = true` on the rules that
   should speak.

## Changing a site's key

1. Add the new public key next to the old one (`keys = ["<old>", "<new>"]`) and restart the hub.
2. Swap the key file on the site and restart the agent.
3. Wait until the site's queue is empty (`queue` on its status page or the hub dashboard is 0),
   so every event signed with the old key has been delivered.
4. Remove the old key and restart the hub.

Skipping step 3 doesn't wedge the site: events the hub can't verify are set aside in its
`rejected` table and the site carries on. But those readings are missing from the charts, and
the hub warns that events were set aside.

If the hub refuses a site's uploads altogether (wrong key in the hub config), the site keeps
everything queued, and the hub's "no contact" alert says the uploads are being refused.
