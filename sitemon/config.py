"""Agent and hub configuration from TOML. Secrets come from ``*_env`` or
``*_file`` keys, never the file itself, so a config can be shared as-is."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any, Literal

from ._duration import parse_duration
from ._events import PublicKey

Severity = Literal["warn", "critical"]


class ConfigError(ValueError):
    pass


def _secret(section: dict[str, Any], name: str, where: str, *, required: bool = True) -> str:
    """Read ``<name>_env`` or ``<name>_file`` from a config section."""
    env = section.get(f"{name}_env")
    path = section.get(f"{name}_file")
    if env:
        val = os.environ.get(env)
        if val is None:
            raise ConfigError(f"{where}: environment variable {env} is not set")
        if required and not val.strip():
            raise ConfigError(f"{where}: environment variable {env} is empty")
        return val.strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            raise ConfigError(f"{where}: cannot read {name}_file {path}: {e}") from e
    if name in section:
        raise ConfigError(
            f"{where}: put {name} in an environment variable ({name}_env) or a file "
            f"({name}_file), not in the config"
        )
    if required:
        raise ConfigError(f"{where}: set {name}_env or {name}_file")
    return ""


def _dur(d: dict[str, Any], key: str, default: str, where: str) -> float:
    try:
        return parse_duration(d.get(key, default))
    except ValueError as e:
        raise ConfigError(f"{where}.{key}: {e}") from e


def _load(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


@dataclass
class SensorConfig:
    type: str
    name: str
    options: dict[str, Any]


@dataclass
class AmiConfig:
    host: str
    port: int
    user: str
    secret: str
    node: int
    interval: float
    registration_command: str


@dataclass
class ArednConfig:
    url: str
    interval: float


@dataclass
class RelayConfig:
    name: str
    pin: int
    mode: Literal["pulse", "switch"]
    active_low: bool
    pulse: float  # pulse mode: how long it stays on
    max_on: float  # switch mode: the longest it may stay on


@dataclass
class ControlConfig:
    hubs: list[str]  # hubs commands may come from: wireline paths only (Part 97)
    operators: list[str]  # public keys allowed to command relays
    hub_keys: list[str]  # public keys allowed to request announcements
    watchdog_pin: int | None


@dataclass
class AnnounceConfig:
    say_command: list[str]
    convert_command: list[str]
    min_gap: float


@dataclass
class AgentConfig:
    site: str
    key_path: str
    data_dir: str
    hubs: list[str]
    sample_interval: float = 30.0
    switch_interval: float = 1.0
    heartbeat_interval: float = 60.0
    flush_interval: float = 30.0
    sync_interval: float = 30.0
    switch_debounce: float = 2.0
    max_events: int = 500_000
    local_ui: str = "127.0.0.1:8471"
    sensors: list[SensorConfig] = field(default_factory=list)
    ami: AmiConfig | None = None
    aredn: ArednConfig | None = None
    control: ControlConfig | None = None
    relays: list[RelayConfig] = field(default_factory=list)
    announce: AnnounceConfig | None = None


def _keys(v: Any, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(k, str) for k in v):
        raise ConfigError(f"{where} must be a list of public keys")
    for k in v:
        try:
            PublicKey.from_b64(k)
        except ValueError as e:
            raise ConfigError(f"{where}: {k!r} is not a public key from `sitemon keygen`") from e
    return v


SHELLS = {
    "sh",
    "bash",
    "dash",
    "zsh",
    "ash",
    "busybox",
    "env",
    "cmd",
    "cmd.exe",
    "powershell",
    "pwsh",
}


def _command(v: Any, needs: tuple[str, ...], where: str) -> list[str]:
    if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{where} must be a list: the program and its arguments")
    # The spoken text is untrusted; through a shell it could run commands.
    if os.path.basename(v[0]).lower() in SHELLS:
        raise ConfigError(f"{where}: run the program directly, not through a shell")
    for n in needs:
        if not any(n in x for x in v):
            raise ConfigError(f"{where} must use {n}")
    try:
        for x in v:
            x.format(text="t", wav="w", out="o")
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError(f"{where}: only {{text}}, {{wav}} and {{out}} can be used ({e})") from e
    return v


def _pin(v: Any, where: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 27:
        raise ConfigError(f"{where}: a BCM GPIO number, 0-27")
    return v


# A station under remote control must stop transmitting within 3 minutes if
# the control link fails (47 CFR 97.213). The relay loop ticks once a second
# and can stall, so leave some margin.
TX_LIMIT = 170.0
RELAY_KEYS = {"name", "pin", "mode", "active_low", "pulse", "max_on", "transmitter"}


def _limit(d: dict[str, Any], key: str, default: str, where: str, transmitter: bool) -> float:
    v = _dur(d, key, default, where)
    if not 0 < v <= 86400:
        raise ConfigError(f"{where}.{key}: more than 0 and at most a day")
    if transmitter and v > TX_LIMIT:
        raise ConfigError(
            f"{where}.{key}: a relay that can make the station transmit is limited to "
            f"{TX_LIMIT:g} s, under the 3 minutes of 47 CFR 97.213"
        )
    return v


def _control_hub(url: str) -> None:
    # Commands are signed either way, but over plain HTTP anyone on the path
    # sees them and can hold them back; loopback is fine for a hub on the Pi.
    from urllib.parse import urlsplit

    u = urlsplit(url)
    if u.hostname is not None and u.hostname.rstrip(".").endswith(".mesh"):
        raise ConfigError(
            f"[control] hubs: {url!r} is on the AREDN mesh; list a hub the site reaches over "
            "the internet or cellular (47 CFR 97.213)"
        )
    if u.scheme == "https":
        return
    if u.scheme == "http" and u.hostname is not None:
        try:
            if u.hostname == "localhost" or ipaddress.ip_address(u.hostname).is_loopback:
                return
        except ValueError:
            pass
    raise ConfigError(f"[control] hubs: {url!r} must be https (or a hub on this machine)")


def _control(c: dict[str, Any], hubs: list[str]) -> ControlConfig:
    chubs = c.get("hubs", [])
    if not isinstance(chubs, list) or not chubs:
        raise ConfigError("[control] hubs: list the hub URLs commands may come from")
    for h in chubs:
        if h not in hubs:
            raise ConfigError(f"[control] hubs: {h!r} is not in [agent] hubs")
        _control_hub(h)
    pin = c.get("watchdog_pin")
    return ControlConfig(
        hubs=chubs,
        operators=_keys(c.get("operators", []), "[control] operators"),
        hub_keys=_keys(c.get("hub_keys", []), "[control] hub_keys"),
        watchdog_pin=None if pin is None else _pin(pin, "[control] watchdog_pin"),
    )


def _relays(raw: list[dict[str, Any]], taken: set[int]) -> list[RelayConfig]:
    out = []
    names: set[str] = set()
    for i, r in enumerate(raw):
        where = f"relays[{i}]"
        name, pin = r.get("name"), r.get("pin")
        if not isinstance(name, str) or not name or "." in name:
            raise ConfigError(f"{where}: a name without '.' is required")
        if name in names:
            raise ConfigError(f"{where}: duplicate relay name {name!r}")
        pin = _pin(pin, f"{where}.pin")
        if pin in taken:
            raise ConfigError(f"{where}: GPIO{pin} is already used")
        mode = r.get("mode", "pulse")
        if mode not in ("pulse", "switch"):
            raise ConfigError(f"{where}: mode must be pulse or switch")
        # A typo in "transmitter" must not quietly drop its limit.
        unknown = sorted(set(r) - RELAY_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown setting {unknown[0]!r}")
        tx = r.get("transmitter", False)
        if not isinstance(tx, bool):
            raise ConfigError(f"{where}.transmitter: true or false")
        names.add(name)
        taken.add(pin)
        out.append(
            RelayConfig(
                name=name,
                pin=pin,
                mode=mode,
                active_low=bool(r.get("active_low", True)),
                pulse=_limit(r, "pulse", "10s", where, tx and mode == "pulse"),
                max_on=_limit(r, "max_on", "2m" if tx else "1h", where, tx and mode == "switch"),
            )
        )
    return out


def _announce(a: dict[str, Any]) -> AnnounceConfig:
    return AnnounceConfig(
        say_command=_command(
            a.get("say_command", ["espeak-ng", "-s", "140", "-w", "{wav}", "{text}"]),
            ("{text}", "{wav}"),
            "[announce] say_command",
        ),
        convert_command=_command(
            a.get(
                "convert_command", ["sox", "{wav}", "-r", "8000", "-c", "1", "-b", "16", "{out}"]
            ),
            ("{wav}", "{out}"),
            "[announce] convert_command",
        ),
        min_gap=_dur(a, "min_gap", "60s", "announce"),
    )


def _check_loopback(host: str) -> None:
    # AMI can key the transmitter; it must never be reachable from the mesh.
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ConfigError(
        f"ami.host = {host!r}: sitemon only talks to AMI on this machine "
        "(127.0.0.1 or ::1). AMI must never be exposed to the mesh."
    )


def _registration_command(v: Any) -> str:
    from .agent.ami import ALLOWED_COMMANDS

    cmd = str(v).strip()
    if cmd and cmd not in ALLOWED_COMMANDS:
        raise ConfigError(
            f"ami.registration_command = {cmd!r}: use one of "
            + ", ".join(repr(c) for c in sorted(ALLOWED_COMMANDS))
        )
    return cmd


def load_agent_config(path: str) -> AgentConfig:
    raw = _load(path)
    site = raw.get("site", {})
    agent = raw.get("agent", {})
    if not site.get("id"):
        raise ConfigError("[site] id is required")
    if not site.get("key"):
        raise ConfigError("[site] key (path to the signing key) is required")
    hubs = agent.get("hubs", [])
    if not isinstance(hubs, list) or not all(isinstance(h, str) for h in hubs):
        raise ConfigError("[agent] hubs must be a list of URLs")

    sensors = []
    names: set[str] = set()
    for i, s in enumerate(raw.get("sensors", [])):
        where = f"sensors[{i}]"
        if "type" not in s or "name" not in s:
            raise ConfigError(f"{where}: type and name are required")
        if s["name"] in names:
            raise ConfigError(f"{where}: duplicate sensor name {s['name']!r}")
        if "." in s["name"]:
            raise ConfigError(f"{where}: sensor names cannot contain '.'")
        names.add(s["name"])
        opts = {k: v for k, v in s.items() if k not in ("type", "name")}
        sensors.append(SensorConfig(type=s["type"], name=s["name"], options=opts))

    ami = None
    if (a := raw.get("ami")) and a.get("enabled", True):
        host = a.get("host", "127.0.0.1")
        _check_loopback(host)
        if "node" not in a:
            raise ConfigError("[ami] node is required")
        ami = AmiConfig(
            host=host,
            port=int(a.get("port", 5038)),
            user=a.get("user", "sitemon"),
            secret=_secret(a, "secret", "[ami]"),
            node=int(a["node"]),
            interval=_dur(a, "interval", "15s", "ami"),
            # Off by default: it needs write=command, which allows any CLI command.
            registration_command=_registration_command(a.get("registration_command", "")),
        )

    aredn = None
    if (r := raw.get("aredn")) and r.get("enabled", True):
        aredn = ArednConfig(
            url=r.get("url", "http://localnode.local.mesh/a/sysinfo?link_info=1"),
            interval=_dur(r, "interval", "60s", "aredn"),
        )

    control = _control(raw["control"], hubs) if raw.get("control") else None
    taken = {int(sc.options["pin"]) for sc in sensors if "pin" in sc.options}
    if control is not None and control.watchdog_pin is not None:
        if control.watchdog_pin in taken:
            raise ConfigError(f"[control] watchdog_pin: GPIO{control.watchdog_pin} is already used")
        taken.add(control.watchdog_pin)
    relays = _relays(raw.get("relays", []), taken)
    if relays and (control is None or not control.operators):
        raise ConfigError("relays need [control] with at least one operator key")

    announce = None
    if (an := raw.get("announce")) and an.get("enabled", False):
        if ami is None:
            raise ConfigError("[announce] needs [ami]: announcements play through app_rpt")
        if control is None or not control.hub_keys:
            raise ConfigError("[announce] needs [control] hub_keys: the hub signs announcements")
        announce = _announce(an)

    return AgentConfig(
        site=site["id"],
        key_path=site["key"],
        data_dir=agent.get("data_dir", "/var/lib/sitemon"),
        hubs=hubs,
        sample_interval=_dur(agent, "sample_interval", "30s", "agent"),
        switch_interval=_dur(agent, "switch_interval", "1s", "agent"),
        heartbeat_interval=_dur(agent, "heartbeat_interval", "60s", "agent"),
        flush_interval=_dur(agent, "flush_interval", "30s", "agent"),
        sync_interval=_dur(agent, "sync_interval", "30s", "agent"),
        switch_debounce=_dur(agent, "switch_debounce", "2s", "agent"),
        max_events=int(agent.get("max_events", 500_000)),
        local_ui=agent.get("local_ui", "127.0.0.1:8471"),
        sensors=sensors,
        ami=ami,
        aredn=aredn,
        control=control,
        relays=relays,
        announce=announce,
    )


@dataclass
class SiteConfig:
    id: str
    name: str
    group: str
    keys: list[str]  # base64 Ed25519 public keys; more than one during rotation


@dataclass
class RuleConfig:
    name: str
    type: Literal["threshold", "rate", "state"]
    metric: str  # "batt.v", "door.open", "ami.registered"
    sites: list[str]
    # threshold
    below: float | None = None
    critical_below: float | None = None
    above: float | None = None
    critical_above: float | None = None
    hysteresis: float = 0.0
    # rate
    window: float = 7 * 86400.0
    smooth: float = 86400.0
    drop: float | None = None
    critical_drop: float | None = None
    rise: float | None = None
    critical_rise: float | None = None
    # state
    bad: Any = None
    severity: Severity = "warn"
    # all
    escalate_after: float | None = None
    hold: float = 0.0
    announce: bool = False
    unit: str = ""


@dataclass
class ChannelConfig:
    type: Literal["discord", "ntfy", "email", "sms"]
    name: str
    min_severity: Literal["info", "warn", "critical"]
    url: str = ""
    topic: str = ""
    token: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    sender: str = ""
    to: list[str] = field(default_factory=list)
    starttls: bool = True
    account_sid: str = ""
    groups: list[str] = field(default_factory=lambda: ["*"])


@dataclass
class OperatorConfig:
    name: str
    key: str
    sites: list[str]


@dataclass
class HubConfig:
    listen: str
    db_path: str
    public_url: str
    sites: list[SiteConfig]
    rules: list[RuleConfig]
    channels: list[ChannelConfig]
    groups: dict[str, str]  # slug -> display name
    silence_warn: float = 300.0
    silence_critical: float = 900.0
    remind_critical: float = 12 * 3600.0
    summary_weekday: int = 0  # Monday
    summary_hour: int = 8
    summary_enabled: bool = True
    retention: float = 400 * 86400.0
    key_path: str = ""  # the hub's own key, which signs announcements
    operators: list[OperatorConfig] = field(default_factory=list)
    announce_gap: float = 600.0


def _patterns(v: Any, where: str) -> list[str]:
    # A bare string would be iterated character by character and match nearly anything.
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f'{where} must be a list of names or patterns, e.g. ["mtn-*"]')
    return v


_THRESHOLD_KEYS = ("below", "critical_below", "above", "critical_above")
_RATE_KEYS = ("drop", "critical_drop", "rise", "critical_rise")


def _rule(i: int, r: dict[str, Any]) -> RuleConfig:
    where = f"rules[{i}]"
    for k in ("name", "metric"):
        if k not in r:
            raise ConfigError(f"{where}: {k} is required")
    rtype = r.get("type", "threshold")
    if rtype not in ("threshold", "rate", "state"):
        raise ConfigError(f"{where}: type must be threshold, rate or state")
    sites = _patterns(r.get("sites", ["*"]), f"{where}.sites")
    rule = RuleConfig(name=r["name"], type=rtype, metric=r["metric"], sites=sites)
    if rtype == "threshold":
        if not any(k in r for k in _THRESHOLD_KEYS):
            raise ConfigError(f"{where}: a threshold rule needs below/above (or critical_*)")
        for k in _THRESHOLD_KEYS:
            if k in r:
                setattr(rule, k, float(r[k]))
        rule.hysteresis = float(r.get("hysteresis", 0.0))
    elif rtype == "rate":
        if not any(k in r for k in _RATE_KEYS):
            raise ConfigError(f"{where}: a rate rule needs drop/rise (or critical_*)")
        for k in _RATE_KEYS:
            if k in r:
                setattr(rule, k, abs(float(r[k])))
        rule.window = _dur(r, "window", "7d", where)
        rule.smooth = _dur(r, "smooth", "1d", where)
        if rule.smooth > rule.window:
            raise ConfigError(f"{where}: smooth cannot be longer than window")
    else:
        if "bad" not in r:
            raise ConfigError(f"{where}: a state rule needs bad = <value>")
        rule.bad = r["bad"]
    sev = r.get("severity", "warn")
    if sev not in ("warn", "critical"):
        raise ConfigError(f"{where}: severity must be warn or critical")
    rule.severity = sev
    if "escalate_after" in r:
        rule.escalate_after = _dur(r, "escalate_after", "0s", where)
    rule.hold = _dur(r, "hold", "0s", where)
    if rule.hold and (rtype == "rate" or (rtype == "state" and isinstance(rule.bad, str))):
        raise ConfigError(
            f"{where}: hold works on threshold rules and true/false or numeric states"
        )
    rule.announce = bool(r.get("announce", False))
    rule.unit = str(r.get("unit", ""))
    return rule


def _channel(i: int, c: dict[str, Any]) -> ChannelConfig:
    where = f"channels[{i}]"
    ctype = c.get("type")
    if ctype not in ("discord", "ntfy", "email", "sms"):
        raise ConfigError(f"{where}: type must be discord, ntfy, email or sms")
    minsev = c.get("min_severity", "warn")
    if minsev not in ("info", "warn", "critical"):
        raise ConfigError(f"{where}: min_severity must be info, warn or critical")
    ch = ChannelConfig(
        type=ctype,
        name=c.get("name", f"{ctype}-{i}"),
        min_severity=minsev,
        groups=_patterns(c.get("groups", ["*"]), f"{where}.groups"),
    )
    if ctype == "discord":
        ch.url = _secret(c, "url", where)
    elif ctype == "ntfy":
        ch.url = c.get("server", "https://ntfy.sh").rstrip("/")
        if not c.get("topic"):
            raise ConfigError(f"{where}: topic is required")
        ch.topic = c["topic"]
        ch.token = _secret(c, "token", where, required=False)
    elif ctype == "sms":
        for k in ("account_sid", "from", "to"):
            if not c.get(k):
                raise ConfigError(f"{where}: {k} is required")
        ch.url = c.get("api_url", "https://api.twilio.com").rstrip("/")
        ch.account_sid = str(c["account_sid"])
        ch.token = _secret(c, "auth_token", where)
        ch.sender = str(c["from"])
        ch.to = list(c["to"]) if isinstance(c["to"], list) else [c["to"]]
    else:
        for k in ("smtp_host", "sender", "to"):
            if not c.get(k):
                raise ConfigError(f"{where}: {k} is required")
        ch.smtp_host = c["smtp_host"]
        ch.smtp_port = int(c.get("smtp_port", 587))
        ch.smtp_user = c.get("smtp_user", "")
        ch.smtp_password = _secret(c, "smtp_password", where, required=bool(ch.smtp_user))
        ch.sender = c["sender"]
        ch.to = list(c["to"]) if isinstance(c["to"], list) else [c["to"]]
        ch.starttls = bool(c.get("starttls", True))
    return ch


def _rules(raw_rules: list[dict[str, Any]]) -> list[RuleConfig]:
    out = [_rule(i, r) for i, r in enumerate(raw_rules)]
    seen: set[str] = set()
    for i, r in enumerate(out):
        # The name keys the alert, so two rules sharing one would fight over it.
        if r.name in seen:
            raise ConfigError(f"rules[{i}]: duplicate rule name {r.name!r}")
        seen.add(r.name)
    return out


def load_hub_config(path: str) -> HubConfig:
    raw = _load(path)
    hub = raw.get("hub", {})
    groups: dict[str, str] = dict(raw.get("groups", {}))
    default_group = hub.get("group", "group")
    if not groups:
        groups = {default_group: hub.get("group_name", default_group)}

    sites = []
    seen: set[str] = set()
    for i, s in enumerate(raw.get("sites", [])):
        where = f"sites[{i}]"
        if not s.get("id"):
            raise ConfigError(f"{where}: id is required")
        if s["id"] in seen:
            raise ConfigError(f"{where}: duplicate site id {s['id']!r}")
        seen.add(s["id"])
        keys = s.get("keys") or ([s["key"]] if s.get("key") else [])
        if not keys:
            raise ConfigError(f"{where}: key (the site's public key from `sitemon keygen`)")
        group = s.get("group", default_group)
        if group not in groups:
            raise ConfigError(f"{where}: group {group!r} is not listed under [groups]")
        sites.append(SiteConfig(id=s["id"], name=s.get("name", s["id"]), group=group, keys=keys))

    hb = raw.get("heartbeat", {})
    summ = raw.get("summary", {})
    weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    day = str(summ.get("day", "mon")).lower()[:3]
    if day not in weekdays:
        raise ConfigError("[summary] day must be a weekday name")
    cfg = HubConfig(
        listen=hub.get("listen", "127.0.0.1:8470"),
        db_path=hub.get("db", "/var/lib/sitemon/hub.db"),
        public_url=hub.get("public_url", "").rstrip("/"),
        sites=sites,
        rules=_rules(raw.get("rules", [])),
        channels=[_channel(i, c) for i, c in enumerate(raw.get("channels", []))],
        groups=groups,
        silence_warn=_dur(hb, "warn_after", "5m", "heartbeat"),
        silence_critical=_dur(hb, "critical_after", "15m", "heartbeat"),
        remind_critical=_dur(hub, "remind_critical", "12h", "hub"),
        summary_weekday=weekdays.index(day),
        summary_hour=int(summ.get("hour", 8)),
        summary_enabled=bool(summ.get("enabled", True)),
        retention=_dur(hub, "retention", "400d", "hub"),
        key_path=str(hub.get("key", "")),
        operators=_operators(raw.get("operators", [])),
        announce_gap=_dur(hub, "announce_gap", "10m", "hub"),
    )
    if cfg.silence_critical < cfg.silence_warn:
        raise ConfigError("[heartbeat] critical_after must be >= warn_after")
    if any(r.announce for r in cfg.rules) and not cfg.key_path:
        raise ConfigError("rules with announce = true need [hub] key: the hub signs announcements")
    return cfg


def _operators(raw: list[dict[str, Any]]) -> list[OperatorConfig]:
    out = []
    for i, o in enumerate(raw):
        where = f"operators[{i}]"
        if not o.get("name") or not o.get("key"):
            raise ConfigError(f"{where}: name and key are required")
        _keys([o["key"]], where)
        out.append(
            OperatorConfig(
                name=str(o["name"]),
                key=o["key"],
                sites=_patterns(o.get("sites", ["*"]), f"{where}.sites"),
            )
        )
    return out
