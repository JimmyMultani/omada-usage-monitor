# omada-usage-monitor

Per-client bandwidth usage alerts for TP-Link Omada controllers, posted to
Discord.

The Omada controller doesn't alert on per-client traffic volume. This small
service polls every client's traffic counters through the Omada Open API,
turns consecutive readings into rates and rolling 24-hour totals (stored in
SQLite), and posts to a Discord channel when a client crosses a limit you set.

It's a single Python script with no dependencies beyond the standard library,
published as a multi-arch (`linux/amd64`, `linux/arm64`) container image:

```
ghcr.io/jimmymultani/omada-usage-monitor
```

## What it posts

- **⚠️ Sustained rate:** a client's upload or download stays above its
  `sustained_mbps` limit for 3 consecutive polls (~15 min at the default
  interval). A single spike never alerts.
- **📈 Daily volume:** a client's rolling 24-hour upload or download passes its
  `daily_gb` limit.
- **📊 Daily report** (once a day, after `DAILY_REPORT_HOUR` local time): the
  top 10 clients by total traffic, the top uploaders on VLANs marked
  `report_top_uploaders`, and any device first seen in the last 24 hours.
- **🛑 / ✅ Monitor health:** a post when 6 polls in a row fail (~30 min of
  not seeing traffic), and another when polling recovers.

Each (client, alert type) pair posts at most once every 6 hours.

With no `DISCORD_WEBHOOK_URL` set, it runs in dry-run mode and prints messages
to stdout instead.

## Caveats: reading the numbers

- **Counters include LAN traffic.** Omada counts every byte a client sends or
  receives, including traffic to other devices on the LAN. A TV streaming
  from a local media server, or cameras recording to a local hub, will show
  high numbers that aren't internet usage. Use per-client overrides to turn
  off limits for these.
- **Reconnects lose one interval.** Counters reset when a client reconnects.
  The monitor skips that interval rather than guessing, so 24-hour totals can
  slightly undercount devices that reconnect often.
- **"Up" means the client is sending.** For a camera, a high upload with no
  matching rise in a local hub's download suggests it's streaming to the
  cloud.

## Setup

### 1. Create an Omada Open API client

In the controller: **Global View → Settings → Platform Integration → Open
API → Add New App**. Choose **Client** mode and the read-only **Viewer**
role, and give it access to the site you want to monitor. Note the
**Client ID**, **Client Secret**, and the **Omada ID** (`omadacId`) shown on
that page.

You also need the site ID. One way to get it is to call
`GET /openapi/v1/{omadacId}/sites` with a token from the client above.

### 2. Create a Discord webhook

In Discord: **Server → channel ⚙️ (Edit Channel) → Integrations → Webhooks →
New Webhook → Copy Webhook URL**.

Treat the webhook URL as a secret: anyone who has it can post to the channel.
Keep it in your deployment's environment or secrets store, not in version
control.

### 3. Write a thresholds file

Copy [`thresholds.example.json`](thresholds.example.json) and adjust it (see
[Thresholds](#thresholds)). Mount it at `/config/thresholds.json`. The file is
re-read on every poll, so edits take effect without a restart. The file is
required: polls fail until it's present.

### 4. Run it

```yaml
# docker-compose.yml
services:
  omada-usage-monitor:
    image: ghcr.io/jimmymultani/omada-usage-monitor:1
    container_name: omada-usage-monitor
    restart: unless-stopped
    environment:
      TZ: America/New_York
      OMADA_BASE_URL: https://omada.example.lan:8043
      OMADA_CLIENT_ID: ${OMADA_CLIENT_ID}
      OMADA_CLIENT_SECRET: ${OMADA_CLIENT_SECRET}
      OMADA_OMADAC_ID: ${OMADA_OMADAC_ID}
      OMADA_SITE_ID: ${OMADA_SITE_ID}
      DISCORD_WEBHOOK_URL: ${DISCORD_WEBHOOK_URL}
    volumes:
      - ./data:/data
      - ./thresholds.json:/config/thresholds.json:ro
```

The container runs as a non-root user, **UID 10001 / GID 10001**. A
bind-mounted data directory must be writable by that user:

```sh
mkdir -p data && sudo chown 10001:10001 data
```

Named volumes need no extra steps.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `OMADA_BASE_URL` | required | Controller URL, e.g. `https://omada.example.lan:8043` |
| `OMADA_CLIENT_ID` | required | Open API client ID (Viewer role) |
| `OMADA_CLIENT_SECRET` | required | Open API client secret |
| `OMADA_OMADAC_ID` | required | Controller ID (`omadacId`) |
| `OMADA_SITE_ID` | required | ID of the site to monitor |
| `OMADA_STRICT_SSL` | `false` | Verify the controller's TLS certificate. Off by default because controller certs are usually self-signed |
| `DISCORD_WEBHOOK_URL` | unset | Discord webhook. Unset = dry run (print messages to stdout) |
| `POLL_INTERVAL_SECONDS` | `300` | Seconds between polls |
| `DAILY_REPORT_HOUR` | `8` | Local hour (per `TZ`) after which the daily report is posted |
| `DATA_DIR` | `/data` | SQLite history (35-day retention) and the healthcheck heartbeat |
| `THRESHOLDS_PATH` | `/config/thresholds.json` | Thresholds file |

The image's healthcheck passes while the last successful poll is more recent
than `max(15 min, 3 × POLL_INTERVAL_SECONDS)`.

## Thresholds

```json
{
  "vlans": {
    "<vlan id>": {
      "label": "IoT",
      "report_top_uploaders": true,
      "sustained_mbps": { "up": 10, "down": 100 },
      "daily_gb": { "up": 100, "down": 500 }
    }
  },
  "clients": {
    "AA-BB-CC-DD-EE-FF": {
      "note": "why this override exists",
      "sustained_mbps": { "down": null },
      "daily_gb": { "down": null }
    }
  }
}
```

- **`vlans`** is keyed by VLAN ID as a string (`"0"` is the default
  network). `label` is used in messages.
- **`report_top_uploaders`** adds a "top uploaders" table for that VLAN to the
  daily report.
- **`sustained_mbps`** is the per-poll rate limit, which must be exceeded for 3
  polls in a row. **`daily_gb`** is the rolling 24-hour total limit. Both take
  separate `up` and `down` values.
- **`clients`** is keyed by MAC address in Omada's format (`AA-BB-CC-DD-EE-FF`,
  uppercase, dashes). A client's keys override its VLAN's limits per
  direction. Anything not overridden is inherited.
- A missing or `null` limit means "don't alert on this". A VLAN with no
  limits only appears in the daily report.

Start with loose limits that only catch obvious runaways, then tighten them
once a week of daily reports shows real baselines.

## CLI

```sh
python3 monitor.py              # poll forever
python3 monitor.py --once       # poll once and exit (errors are raised, not retried)
python3 monitor.py --report-now # post/print the daily report from stored data and exit
```

In the container:

```sh
docker compose run --rm omada-usage-monitor --report-now
```

## Image tags

| Tag | Updated on |
|---|---|
| `1.2.3`, `1.2`, `1`, `latest` | Release tags (`v1.2.3`) |
| `edge`, `sha-<short>` | Every push to `main` |

Pin a major version (e.g. `:1`) to get fixes without breaking changes.

## Development

Python 3.9+ with no dependencies:

```sh
python3 -m unittest test_monitor
```

## License

[MIT](LICENSE)
