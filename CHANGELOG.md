# Changelog

## [1.0.0](https://github.com/JimmyMultani/omada-usage-monitor/releases/tag/v1.0.0) (2026-10-08)

### Features

* Per-client bandwidth monitor for TP-Link Omada controllers: polls the Open API client list, stores rates and rolling 24h totals in SQLite, and posts to a Discord webhook (dry run to stdout when no webhook is set).
* Alerts on sustained rates (3 consecutive polls over a limit) and rolling 24h totals, per VLAN with per-client overrides, with a 6h cooldown per client and alert type.
* Daily top-talkers and new-devices report, and an alert after 6 failed polls in a row.
* Multi-arch (`linux/amd64`, `linux/arm64`) image at `ghcr.io/jimmymultani/omada-usage-monitor`, running as non-root UID/GID 10001, with a heartbeat-based healthcheck.
