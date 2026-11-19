from __future__ import annotations

from pathlib import Path

import pytest

from sitemon import SigningKey
from sitemon._cli import main
from sitemon.config import ConfigError, load_agent_config, load_hub_config

ROOT = Path(__file__).resolve().parents[1]


def write(tmp: Path, text: str, name: str = "c.toml") -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_example_configs_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITEMON_AMI_SECRET", "x")
    monkeypatch.setenv("SITEMON_DISCORD_WEBHOOK", "https://discord.example/api/webhooks/1/x")
    ag = load_agent_config(str(ROOT / "deploy" / "agent.example.toml"))
    assert ag.ami is not None and ag.ami.host == "127.0.0.1"
    hub = load_hub_config(str(ROOT / "deploy" / "hub.example.toml"))
    assert hub.rules and hub.channels and hub.sites


def test_ami_must_be_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S", "x")
    p = write(tmp_path, '[site]\nid="s"\nkey="k"\n[ami]\nhost="10.0.0.5"\nnode=1\nsecret_env="S"\n')
    with pytest.raises(ConfigError, match="never be exposed"):
        load_agent_config(p)


def test_inline_secret_is_refused(tmp_path: Path) -> None:
    p = write(tmp_path, '[site]\nid="s"\nkey="k"\n[ami]\nnode=1\nsecret="hunter2"\n')
    with pytest.raises(ConfigError, match="not in the config"):
        load_agent_config(p)


def test_secret_from_file(tmp_path: Path) -> None:
    (tmp_path / "pw").write_text("from-file\n")
    p = write(
        tmp_path,
        f'[site]\nid="s"\nkey="k"\n[ami]\nnode=1\nsecret_file="{(tmp_path / "pw").as_posix()}"\n',
    )
    cfg = load_agent_config(p)
    assert cfg.ami is not None and cfg.ami.secret == "from-file"


@pytest.mark.parametrize(
    "bad, msg",
    [
        ('[[rules]]\nname="x"\nmetric="m"\n', "needs below/above"),
        ('[[rules]]\nname="x"\nmetric="m"\ntype="rate"\n', "needs drop/rise"),
        ('[[rules]]\nname="x"\nmetric="m"\ntype="state"\n', "needs bad"),
        (
            '[[rules]]\nname="x"\nmetric="m"\ntype="rate"\ndrop=1\nwindow="1h"\nsmooth="2h"\n',
            "smooth",
        ),
        ('[[sites]]\nid="a"\n', "key"),
        ('[[sites]]\nid="a"\nkey="k"\ngroup="nope"\n', "not listed"),
        ('[heartbeat]\nwarn_after="10m"\ncritical_after="5m"\n', "critical_after"),
        ('[[channels]]\ntype="sms"\n', "type must be"),
    ],
)
def test_hub_config_errors(tmp_path: Path, bad: str, msg: str) -> None:
    with pytest.raises(ConfigError, match=msg):
        load_hub_config(write(tmp_path, bad))


def test_keygen_and_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    keyfile = tmp_path / "site.key"
    assert main(["keygen", "--out", str(keyfile)]) == 0
    out = capsys.readouterr().out
    pub = SigningKey.load(str(keyfile)).public_key().to_b64()
    assert pub in out
    with pytest.raises(SystemExit, match="refusing"):
        main(["keygen", "--out", str(keyfile)])
    assert main(["pubkey", "--key", str(keyfile)]) == 0
    assert capsys.readouterr().out.strip() == pub

    agent = write(
        tmp_path,
        f'[site]\nid="s"\nkey="{keyfile.as_posix()}"\n'
        '[[sensors]]\ntype="sim-battery"\nname="batt"\n',
        "agent.toml",
    )
    assert main(["check", agent]) == 0
    assert "agent config OK" in capsys.readouterr().out
    bad = write(tmp_path, '[site]\nid="s"\n', "bad.toml")
    assert main(["check", bad]) == 1


def test_registration_command_must_be_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("S", "x")
    base = '[site]\nid="s"\nkey="k"\n[ami]\nnode=1\nsecret_env="S"\n'
    off = load_agent_config(write(tmp_path, base))
    assert off.ami is not None and off.ami.registration_command == ""
    ok = load_agent_config(write(tmp_path, base + 'registration_command="iax2 show registry"\n'))
    assert ok.ami is not None and ok.ami.registration_command == "iax2 show registry"
    with pytest.raises(ConfigError, match="use one of"):
        load_agent_config(write(tmp_path, base + 'registration_command="core stop now"\n'))
