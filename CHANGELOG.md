# Changelog

All notable changes to sitemon are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The section for a version is
what appears on its GitHub release page. Validated in CI by
[patchnotes](https://github.com/Londopy/patchnotes).

## [Unreleased]

## [0.1.0] - 2026-11-19

### Added

- Site agent: INA226/INA219 power, BME280 environment, door and water inputs,
  AllStar state over AMI (localhost only) and AREDN node state, with simulated
  backends for testing without hardware.
- Signed, sequence-numbered event log with batched SQLite writes and
  store-and-forward sync to every configured hub.
- Door and water inputs are debounced (`switch_debounce`, 2 s by default).
- Hub: signature checks, rules for thresholds, rate of change, state changes and
  silent sites, alert de-duplication and warn -> critical escalation.
- `hold` on a rule: the condition must last that long before an alert opens.
- Alerts to Discord, ntfy and email, each channel sent independently; weekly
  summary to Discord.
- Dashboard with a tile and sparklines per site, and CSV export per sensor.

[Unreleased]: https://github.com/Londopy/sitemon/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Londopy/sitemon/releases/tag/v0.1.0
