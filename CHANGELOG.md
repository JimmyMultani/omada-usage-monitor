# Changelog

## [1.3.2](https://github.com/JimmyMultani/omada-usage-monitor/compare/v1.3.1...v1.3.2) (2026-10-23)


### Bug Fixes

* Round baseline alert ratios up so they never read as the limit ([#30](https://github.com/JimmyMultani/omada-usage-monitor/issues/30)) ([d0952ae](https://github.com/JimmyMultani/omada-usage-monitor/commit/d0952ae48d487523ff570ea96fd0c73d6c91edda))

## [1.3.1](https://github.com/JimmyMultani/omada-usage-monitor/compare/v1.3.0...v1.3.1) (2026-10-18)


### Bug Fixes

* Queue Discord messages so a failed post never resends or fails the poll ([#24](https://github.com/JimmyMultani/omada-usage-monitor/issues/24)) ([a91a232](https://github.com/JimmyMultani/omada-usage-monitor/commit/a91a232d0acb9d961eb68e2e01ffb9b402aac1a8))
* Run report-only when the thresholds file can't be read ([#26](https://github.com/JimmyMultani/omada-usage-monitor/issues/26)) ([7578419](https://github.com/JimmyMultani/omada-usage-monitor/commit/7578419e92fe2c40207efb9e7628914fd87bb220))

## [1.3.0](https://github.com/JimmyMultani/omada-usage-monitor/compare/v1.2.0...v1.3.0) (2026-10-15)


### Features

* Add --check-thresholds and --version, and log the version at startup ([#22](https://github.com/JimmyMultani/omada-usage-monitor/issues/22)) ([3ec6342](https://github.com/JimmyMultani/omada-usage-monitor/commit/3ec6342f31de42650d93c8bc502f10278ac89589))
* Alert when a client's daily usage is far above its usual day ([#19](https://github.com/JimmyMultani/omada-usage-monitor/issues/19)) ([308b526](https://github.com/JimmyMultani/omada-usage-monitor/commit/308b526d6e6e9baee3f9428d9ba3815b52da687f))

## [1.2.0](https://github.com/JimmyMultani/omada-usage-monitor/compare/v1.1.0...v1.2.0) (2026-10-12)


### Features

* Alert when a new device joins a watched VLAN ([#17](https://github.com/JimmyMultani/omada-usage-monitor/issues/17)) ([72d1f60](https://github.com/JimmyMultani/omada-usage-monitor/commit/72d1f60ff11eed86ee95e80dc23d973de7204afb))

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
