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
- **⚠️ / ✅ Thresholds file problems:** a post listing problems in the
  thresholds file when they first appear, and another once they're fixed.

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
**Client ID** and **Client Secret**.

Then look up the controller ID (`omadacId`) and site ID. `-k` skips
certificate verification for a self-signed controller cert.

```sh
OMADA=https://omada.example.lan:8043
CLIENT_ID=...
CLIENT_SECRET=...

# Controller ID: no auth needed.
OMADAC_ID=$(curl -sk "$OMADA/api/info" | python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["omadacId"])')
echo "OMADA_OMADAC_ID=$OMADAC_ID"

# Access token for the Open API client.
TOKEN=$(curl -sk -X POST "$OMADA/openapi/authorize/token?grant_type=client_credentials" \
  -H 'Content-Type: application/json' \
  -d "{\"omadacId\": \"$OMADAC_ID\", \"client_id\": \"$CLIENT_ID\", \"client_secret\": \"$CLIENT_SECRET\"}" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["accessToken"])')

# Sites this client can see: use the siteId as OMADA_SITE_ID.
curl -sk "$OMADA/openapi/v1/$OMADAC_ID/sites?page=1&pageSize=100" \
  -H "Authorization: AccessToken=$TOKEN" \
  | python3 -c 'import json, sys; [print(s["siteId"], s["name"]) for s in json.load(sys.stdin)["result"]["data"]]'
```

If a step prints a `KeyError`, run its `curl` alone to see the API's
`errorCode` and `msg`.

### 2. Create a Discord webhook

In Discord: **Server → channel ⚙️ (Edit Channel) → Integrations → Webhooks →
New Webhook → Copy Webhook URL**.

Treat the webhook URL as a secret: anyone who has it can post to the channel.
Keep it in your deployment's environment or secrets store, not in version
control.

### 3. Write a thresholds file

Copy [`thresholds.example.json`](thresholds.example.json) and adjust it (see
[Thresholds](#thresholds)). Mount it at `/config/thresholds.json`. The file is
re-read on every poll, so edits take effect without a restart. Without the
file, the monitor runs report-only: it records usage and posts the daily
report, but sends no alerts.

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
- **`clients`** is keyed by MAC address. Lowercase and colons are fine; they're
  normalized to Omada's `AA-BB-CC-DD-EE-FF`. A client's keys override its
  VLAN's limits per direction. Anything not overridden is inherited.
- A missing or `null` limit means "don't alert on this". A VLAN with no
  limits only appears in the daily report.
- `note` is free text for your own records.

The file is checked on every poll. Problems are logged and posted to Discord
once, when they first appear (and again when they're fixed). These include
unknown keys (e.g. a typo like `sustained_mpbs`), a malformed MAC, or a limit
that isn't a non-negative number. A problem never stops polling: unknown
keys and unusable limits are ignored, and an unreadable file means
report-only until it's fixed.

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

## Troubleshooting

Start with the container logs (`docker logs omada-usage-monitor`). Each
successful poll logs `polled N clients`; each failure logs
`poll failed (N in a row): <error>`.

| Symptom | Likely cause | Fix |
|---|---|---|
| Exits at startup with `sqlite3.OperationalError: unable to open database file` | The bind-mounted data dir isn't writable by the container user (UID 10001) | `sudo chown 10001:10001 <data dir>`. Needed when moving from a setup that ran as root |
| `⚠️ Problems in the thresholds file` | The file is missing, isn't valid JSON, or has entries the monitor can't use. The message lists each one | Fix the listed entries, or mount the file at `/config/thresholds.json` (or set `THRESHOLDS_PATH`). A ✅ follows once it's fixed |
| `poll failed ... Omada API error ... on /openapi/authorize/token` | Wrong client ID, secret, or `OMADA_OMADAC_ID` | Re-check the Open API client and the [ID lookup](#1-create-an-omada-open-api-client) |
| `poll failed ... Omada API error ... on /openapi/v1/.../clients` | Wrong `OMADA_SITE_ID`, or the Open API client has no access to that site | Check the site list from the ID lookup, and the client's site access in the controller |
| `poll failed ... CERTIFICATE_VERIFY_FAILED` | `OMADA_STRICT_SSL=true` with a self-signed controller cert | Unset `OMADA_STRICT_SSL`, or give the controller a trusted cert |
| `poll failed ... HTTP Error 401/403/404` from `discord.com` | Webhook deleted or URL wrong. A failed post fails the whole poll | Create a new webhook and update `DISCORD_WEBHOOK_URL` |
| Container is `unhealthy` | No successful poll in `max(15 min, 3 × POLL_INTERVAL_SECONDS)` | See the `poll failed` lines in the logs |
| Messages appear in the logs as `[discord dry-run]` instead of in Discord | `DISCORD_WEBHOOK_URL` is unset or empty | Set it |
| A client never alerts | No limit applies to it: check its VLAN ID in the controller matches a `vlans` key, and any override's MAC matches the client | Fix `thresholds.json`. It's re-read every poll |

After 6 failed polls in a row (~30 min at the default interval), the monitor
posts a 🛑 alert to Discord (if the webhook works), and a ✅ when polling
recovers.

## Versioning and releases

Releases follow [semantic versioning](https://semver.org/). Pin a major
version (e.g. `:1`) to get fixes and features without breaking changes.

| Tag | Updated on |
|---|---|
| `1.2.3`, `1.2`, `1`, `latest` | Each release |
| `edge`, `sha-<short>` | Every push to `main` |

A **breaking change** (major bump) is anything that can break an existing
deployment on upgrade:

- renaming or removing an env var, or changing its default
- an incompatible change to the thresholds file format
- changing the container user's UID/GID, or the `/data` / `/config` paths
- a SQLite schema change that loses existing history

Releases are automated with
[release-please](https://github.com/googleapis/release-please), driven by
[Conventional Commits](https://www.conventionalcommits.org/) on `main`:

1. Merge PRs to `main` with Conventional Commit titles (squash-merge). `fix:`
   makes a patch release, `feat:` a minor release, and `feat!:` or a
   `BREAKING CHANGE:` footer a major release. `docs:`, `chore:`, `ci:`,
   etc. don't trigger a release.
2. release-please keeps a `chore: Release X.Y.Z` PR open that bumps the
   version and updates [`CHANGELOG.md`](CHANGELOG.md). Review the changelog
   there. The PR is opened by GitHub Actions, so CI doesn't run on it, but
   every commit in it has already passed CI on `main`.
3. Merge the release PR. CI tags `vX.Y.Z`, creates the GitHub Release, and
   publishes the image tags above.

## Development

Python 3.9+ with no dependencies:

```sh
python3 -m unittest test_monitor   # unit tests
ci/smoke_test.sh                   # build the image and run it against a fake Omada API (needs Docker)
```

The smoke test checks that the container polls, raises a dry-run alert,
runs as UID/GID 10001, and reports healthy. CI runs both on every PR and
push, and only releases or publishes once both pass. Unit tests run on
Python 3.9 and on the Dockerfile's base image, so the shipped Python version
is set in one place: the `FROM` line.

Dependabot opens weekly PRs for GitHub Actions updates (`ci:`) and for the
base image, which is pinned by digest (`fix:`). Base image updates include
security rebuilds of the same Python version, and each merged one becomes a
patch release.

## License

[MIT](LICENSE)
