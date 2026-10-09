# Changelog

## [1.1.0](https://github.com/JimmyMultani/omada-usage-monitor/compare/v1.0.0...v1.1.0) (2026-10-09)


### Features

* Add optional Prometheus metrics endpoint ([#11](https://github.com/JimmyMultani/omada-usage-monitor/issues/11)) ([527ffef](https://github.com/JimmyMultani/omada-usage-monitor/commit/527ffef79e263fcacd9e4bf68d03668e73b9941e))
* Validate thresholds file and run report-only without one ([#3](https://github.com/JimmyMultani/omada-usage-monitor/issues/3)) ([4377f73](https://github.com/JimmyMultani/omada-usage-monitor/commit/4377f7377acb2d952196f1979441e7ea51f38a0a))


### Bug Fixes

* bump python from 3.13-alpine to 3.14-alpine ([#13](https://github.com/JimmyMultani/omada-usage-monitor/issues/13)) ([8a62a33](https://github.com/JimmyMultani/omada-usage-monitor/commit/8a62a33f5203f5a91215b313f9470e54d45165e3))

## [1.0.0](https://github.com/JimmyMultani/omada-usage-monitor/releases/tag/v1.0.0) (2026-10-08)

### Features

* Per-client bandwidth monitor for TP-Link Omada controllers: polls the Open API client list, stores rates and rolling 24h totals in SQLite, and posts to a Discord webhook (dry run to stdout when no webhook is set).
* Alerts on sustained rates (3 consecutive polls over a limit) and rolling 24h totals, per VLAN with per-client overrides, with a 6h cooldown per client and alert type.
* Daily top-talkers and new-devices report, and an alert after 6 failed polls in a row.
* Multi-arch (`linux/amd64`, `linux/arm64`) image at `ghcr.io/jimmymultani/omada-usage-monitor`, running as non-root UID/GID 10001, with a heartbeat-based healthcheck.
