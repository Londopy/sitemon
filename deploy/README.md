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
Asterisk or key the transmitter. sitemon itself only ever sends `rpt show registrations` or
`iax2 show registry` (anything else is refused in code), but the password then unlocks more than
monitoring. If you want the check anyway, make it `write = reporting,command` above and set
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
