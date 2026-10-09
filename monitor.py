"""Per-client bandwidth usage monitor for the Omada controller.

Polls per-client traffic counters from the Omada Open API, turns consecutive
snapshots into rates and rolling 24h totals, and posts to a Discord webhook
when a client crosses a threshold. Also posts a daily top-talkers report.

Standard library only, so the image is just a stock python base plus this
file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

__version__ = "1.0.0"  # x-release-please-version

DAY = 24 * 60 * 60
RETENTION_SECONDS = 35 * DAY
ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
SUSTAINED_POLLS = 3
# Consecutive failed polls before posting a "monitor can't reach the
# controller" alert, so a silent monitor isn't mistaken for a quiet network.
FAILURE_ALERT_POLLS = 6
DISCORD_LIMIT = 2000

LIMIT_KINDS = ("sustained_mbps", "daily_gb")
VLAN_KEYS = {"label", "report_top_uploaders", *LIMIT_KINDS}
CLIENT_KEYS = {"note", *LIMIT_KINDS}
MAC_PATTERN = re.compile(r"[0-9A-F]{2}(-[0-9A-F]{2}){5}")


@dataclass
class Config:
    base_url: str
    client_id: str
    client_secret: str
    omadac_id: str
    site_id: str
    strict_ssl: bool
    webhook_url: Optional[str]
    db_path: str
    heartbeat_path: str
    thresholds_path: str
    poll_interval: int
    report_hour: int

    @classmethod
    def from_env(cls) -> "Config":
        def required(key: str) -> str:
            value = os.environ.get(key)
            if not value:
                sys.exit(f"{key} is required")
            return value

        data_dir = os.environ.get("DATA_DIR", "/data")
        here = os.path.dirname(os.path.abspath(__file__))
        return cls(
            base_url=required("OMADA_BASE_URL").rstrip("/"),
            client_id=required("OMADA_CLIENT_ID"),
            client_secret=required("OMADA_CLIENT_SECRET"),
            omadac_id=required("OMADA_OMADAC_ID"),
            site_id=required("OMADA_SITE_ID"),
            strict_ssl=os.environ.get("OMADA_STRICT_SSL", "false").lower() == "true",
            webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or None,
            db_path=os.path.join(data_dir, "usage.sqlite3"),
            heartbeat_path=os.path.join(data_dir, "heartbeat"),
            thresholds_path=os.environ.get("THRESHOLDS_PATH", os.path.join(here, "thresholds.json")),
            poll_interval=int(os.environ.get("POLL_INTERVAL_SECONDS", "300")),
            report_hour=int(os.environ.get("DAILY_REPORT_HOUR", "8")),
        )


# --- Omada Open API ---------------------------------------------------------


class OmadaClient:
    def __init__(self, config: Config):
        self.config = config
        self.ssl_context = ssl.create_default_context()
        if not config.strict_ssl:
            # The controller uses a self-signed certificate on the LAN.
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE

    def _request(self, method: str, path: str, params: dict, body: Optional[dict] = None, token: Optional[str] = None) -> dict:
        url = f"{self.config.base_url}{path}?{urllib.parse.urlencode(params)}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"AccessToken={token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, context=self.ssl_context, timeout=30) as response:
            payload = json.load(response)
        if payload.get("errorCode") != 0:
            raise RuntimeError(f"Omada API error {payload.get('errorCode')} on {path}: {payload.get('msg')}")
        return payload.get("result") or {}

    def _token(self) -> str:
        # A fresh client_credentials token per poll; simpler than tracking
        # expiry/refresh, and cheap at one request every few minutes.
        result = self._request(
            "POST",
            "/openapi/authorize/token",
            {"grant_type": "client_credentials"},
            {"omadacId": self.config.omadac_id, "client_id": self.config.client_id, "client_secret": self.config.client_secret},
        )
        return result["accessToken"]

    def list_clients(self) -> list[dict]:
        token = self._token()
        path = f"/openapi/v1/{self.config.omadac_id}/sites/{self.config.site_id}/clients"
        clients: list[dict] = []
        page = 1
        while True:
            result = self._request("GET", path, {"page": page, "pageSize": 1000}, token=token)
            clients.extend(result.get("data") or [])
            if len(clients) >= result.get("totalRows", 0) or not result.get("data"):
                return clients
            page += 1


# --- Storage ----------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    mac TEXT PRIMARY KEY, ts INTEGER, up INTEGER, down INTEGER, uptime INTEGER
);
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER, mac TEXT, name TEXT, ip TEXT, vid INTEGER,
    up_bytes INTEGER, down_bytes INTEGER, seconds INTEGER
);
CREATE INDEX IF NOT EXISTS samples_mac_ts ON samples (mac, ts);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS first_seen (
    mac TEXT PRIMARY KEY, ts INTEGER, name TEXT, ip TEXT, vid INTEGER
);
CREATE TABLE IF NOT EXISTS alerts (
    mac TEXT, kind TEXT, ts INTEGER, PRIMARY KEY (mac, kind)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def get_meta(db: sqlite3.Connection, key: str) -> Optional[str]:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# --- Usage math -------------------------------------------------------------


def compute_delta(prev: Optional[tuple], up: int, down: int, uptime: int, now: int, max_gap: int) -> Optional[tuple]:
    """Bytes moved since the previous snapshot, as (up, down, seconds).

    Returns None when there's no usable baseline: first sighting, a gap too
    long to attribute to one interval, or a counter reset (the client
    reconnected, so its counters or uptime went backwards).
    """
    if prev is None:
        return None
    prev_ts, prev_up, prev_down, prev_uptime = prev
    seconds = now - prev_ts
    if seconds <= 0 or seconds > max_gap:
        return None
    if up < prev_up or down < prev_down or uptime < prev_uptime:
        return None
    return up - prev_up, down - prev_down, seconds


def mbps(byte_count: int, seconds: int) -> float:
    return byte_count * 8 / seconds / 1_000_000


def gb(byte_count: int) -> float:
    return byte_count / 1_000_000_000


def load_thresholds(path: str) -> tuple[dict, list[str]]:
    """The thresholds file, cleaned up, plus a list of problems found in it.

    Problems never stop a poll. A missing or unreadable file means no limits
    (usage is still recorded and reported), and an unusable entry or limit is
    dropped rather than left to crash the alert checks. Client MACs are
    normalized to Omada's AA-BB-CC-DD-EE-FF format.
    """
    report_only = "running report-only (no alerts)"
    try:
        with open(path) as f:
            thresholds = json.load(f)
    except FileNotFoundError:
        return {}, [f"file not found; {report_only}"]
    except ValueError as error:
        return {}, [f"not valid JSON ({error}); {report_only}"]
    if not isinstance(thresholds, dict):
        return {}, [f"must be a JSON object; {report_only}"]

    problems = [f'unknown top-level key "{key}"; ignored' for key in sorted(thresholds.keys() - {"vlans", "clients"})]
    vlans, clients = {}, {}
    for section, entries in (("vlans", vlans), ("clients", clients)):
        raw = thresholds.get(section, {})
        if not isinstance(raw, dict):
            problems.append(f"{section}: must be an object; ignored")
            continue
        for key, entry in raw.items():
            where = f'{section}["{key}"]'
            if section == "vlans":
                if not key.isdigit():
                    problems.append(f'{where}: key must be a VLAN ID like "20"')
                entries[key] = check_entry(where, entry, VLAN_KEYS, problems)
            else:
                mac = key.strip().upper().replace(":", "-")
                if not MAC_PATTERN.fullmatch(mac):
                    problems.append(f'{where}: not a MAC address like "AA-BB-CC-DD-EE-FF"')
                entries[mac] = check_entry(where, entry, CLIENT_KEYS, problems)
    return {"vlans": vlans, "clients": clients}, problems


def check_entry(where: str, entry: object, allowed: set, problems: list[str]) -> dict:
    """One VLAN or client entry with unusable values dropped, appending what
    was wrong to `problems`."""
    if not isinstance(entry, dict):
        problems.append(f"{where}: must be an object; ignored")
        return {}
    problems += [f'{where}: unknown key "{key}"; ignored' for key in sorted(entry.keys() - allowed)]
    clean = {key: value for key, value in entry.items() if key in allowed}
    if "label" in clean and not isinstance(clean["label"], str):
        problems.append(f"{where}.label: must be a string; ignored")
        del clean["label"]
    for kind in LIMIT_KINDS:
        if kind not in clean:
            continue
        limits = clean[kind]
        if not isinstance(limits, dict):
            problems.append(f'{where}.{kind}: must be an object like {{"up": 10, "down": 100}}; ignored')
            del clean[kind]
            continue
        clean[kind] = {}
        for direction, limit in limits.items():
            if direction not in ("up", "down"):
                problems.append(f'{where}.{kind}: unknown key "{direction}"; ignored')
            elif limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit < 0):
                problems.append(f"{where}.{kind}.{direction}: must be a non-negative number or null; ignored")
            else:
                clean[kind][direction] = limit
    return clean


def report_threshold_problems(db: sqlite3.Connection, webhook_url: Optional[str], path: str, problems: list[str]) -> None:
    """Log and post thresholds file problems when they change, rather than on
    every poll, and post again once they're fixed."""
    current = "\n".join(problems)
    if current == (get_meta(db, "threshold_problems") or ""):
        return
    if problems:
        message = f"⚠️ Problems in the thresholds file `{path}`:\n" + "\n".join(f"• {problem}" for problem in problems)
    else:
        message = f"✅ The thresholds file `{path}` has no more problems."
    if webhook_url:  # In dry-run mode post_discord already prints it.
        print(message, file=sys.stderr, flush=True)
    post_discord(webhook_url, message)
    set_meta(db, "threshold_problems", current)


def vlan_label(thresholds: dict, vid: int) -> str:
    return thresholds.get("vlans", {}).get(str(vid), {}).get("label", f"VLAN {vid}")


def limits_for(thresholds: dict, mac: str, vid: int) -> dict:
    """Effective limits for a client: its VLAN's, with per-MAC overrides on top.

    Shape: {"sustained_mbps": {"up": x, "down": y}, "daily_gb": {...}}, where
    a missing or null value means "don't alert on this".
    """
    vlan = thresholds.get("vlans", {}).get(str(vid), {})
    client = thresholds.get("clients", {}).get(mac, {})
    limits = {}
    for kind in ("sustained_mbps", "daily_gb"):
        limits[kind] = {**vlan.get(kind, {}), **client.get(kind, {})}
    return limits


def sustained_rate(db: sqlite3.Connection, mac: str, direction: str, polls: int, max_gap: int) -> Optional[float]:
    """Lowest per-poll rate (Mbps) across the last `polls` samples, or None if
    there aren't that many contiguous samples. "Lowest" means every one of the
    polls was at least this fast — a sustained rate, not a single spike."""
    column = "up_bytes" if direction == "up" else "down_bytes"
    rows = db.execute(
        f"SELECT ts, {column}, seconds FROM samples WHERE mac = ? ORDER BY ts DESC LIMIT ?",
        (mac, polls),
    ).fetchall()
    if len(rows) < polls:
        return None
    for newer, older in zip(rows, rows[1:]):
        if newer[0] - older[0] > max_gap:
            return None
    return min(mbps(byte_count, seconds) for _, byte_count, seconds in rows)


def daily_totals(db: sqlite3.Connection, mac: str, now: int) -> tuple[int, int]:
    row = db.execute(
        "SELECT COALESCE(SUM(up_bytes), 0), COALESCE(SUM(down_bytes), 0) FROM samples WHERE mac = ? AND ts > ?",
        (mac, now - DAY),
    ).fetchone()
    return row[0], row[1]


# --- Discord ----------------------------------------------------------------


def post_discord(webhook_url: Optional[str], content: str) -> None:
    if not webhook_url:
        print(f"[discord dry-run]\n{content}", flush=True)
        return
    for chunk in split_message(content):
        request = urllib.request.Request(
            webhook_url,
            data=json.dumps({"content": chunk}).encode(),
            # Discord's edge rejects urllib's default User-Agent.
            headers={"Content-Type": "application/json", "User-Agent": f"omada-usage-monitor/{__version__}"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=30).close()


def split_message(content: str) -> list[str]:
    """Split on line boundaries to stay under Discord's per-message limit,
    re-opening a code block if a split lands inside one."""
    chunks, current, in_code = [], "", False
    for line in content.split("\n"):
        if len(current) + len(line) + 5 > DISCORD_LIMIT:
            chunks.append(current + ("```" if in_code else ""))
            current = "```\n" if in_code else ""
        current += line + "\n"
        if line.startswith("```"):
            in_code = not in_code
    if current.strip():
        chunks.append(current)
    return chunks


def describe(client: dict, thresholds: dict) -> str:
    vid = client.get("vid") or 0
    return f"**{client.get('name') or client['mac']}** ({vlan_label(thresholds, vid)}, `{client.get('ip')}`, `{client['mac']}`)"


# --- Poll -------------------------------------------------------------------


def record_poll(db: sqlite3.Connection, clients: list[dict], now: int, max_gap: int) -> None:
    for client in clients:
        mac = client["mac"]
        up, down, uptime = client.get("trafficUp") or 0, client.get("trafficDown") or 0, client.get("uptime") or 0
        vid = client.get("vid") or 0
        prev = db.execute("SELECT ts, up, down, uptime FROM counters WHERE mac = ?", (mac,)).fetchone()
        delta = compute_delta(prev, up, down, uptime, now, max_gap)
        if delta is not None:
            db.execute(
                "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, mac, client.get("name"), client.get("ip"), vid, *delta),
            )
        db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?, ?, ?, ?)", (mac, now, up, down, uptime))
        db.execute(
            "INSERT OR IGNORE INTO first_seen VALUES (?, ?, ?, ?, ?)",
            (mac, now, client.get("name"), client.get("ip"), vid),
        )
    db.execute("DELETE FROM samples WHERE ts < ?", (now - RETENTION_SECONDS,))


def check_alerts(db: sqlite3.Connection, clients: list[dict], thresholds: dict, now: int, poll_interval: int) -> list[str]:
    max_gap = poll_interval * 2
    messages = []
    arrows = {"up": "uploading", "down": "downloading"}
    for client in clients:
        mac, vid = client["mac"], client.get("vid") or 0
        limits = limits_for(thresholds, mac, vid)
        up_24h, down_24h = daily_totals(db, mac, now)
        totals = {"up": up_24h, "down": down_24h}
        footer = f"24h so far: ↑ {gb(up_24h):.1f} GB  ↓ {gb(down_24h):.1f} GB"

        for direction in ("up", "down"):
            limit = limits["sustained_mbps"].get(direction)
            if limit is not None:
                rate = sustained_rate(db, mac, direction, SUSTAINED_POLLS, max_gap)
                if rate is not None and rate > limit and claim_alert(db, mac, f"sustained_{direction}", now):
                    minutes = SUSTAINED_POLLS * poll_interval // 60
                    messages.append(
                        f"⚠️ {describe(client, thresholds)} has been {arrows[direction]} at ≥ {rate:.1f} Mbps "
                        f"for ~{minutes} min (limit {limit} Mbps).\n{footer}"
                    )

            limit = limits["daily_gb"].get(direction)
            if limit is not None and gb(totals[direction]) > limit and claim_alert(db, mac, f"daily_{direction}", now):
                messages.append(
                    f"📈 {describe(client, thresholds)} has {'uploaded' if direction == 'up' else 'downloaded'} "
                    f"{gb(totals[direction]):.1f} GB in the last 24h (limit {limit} GB).\n{footer}"
                )
    return messages


def claim_alert(db: sqlite3.Connection, mac: str, kind: str, now: int) -> bool:
    """True (and records the alert) unless the same alert fired within the cooldown."""
    row = db.execute("SELECT ts FROM alerts WHERE mac = ? AND kind = ?", (mac, kind)).fetchone()
    if row and now - row[0] < ALERT_COOLDOWN_SECONDS:
        return False
    db.execute("INSERT OR REPLACE INTO alerts VALUES (?, ?, ?)", (mac, kind, now))
    return True


# --- Daily report -----------------------------------------------------------


def build_report(db: sqlite3.Connection, thresholds: dict, now: int) -> str:
    since = now - DAY
    oldest = db.execute("SELECT MIN(ts - seconds) FROM samples WHERE ts > ?", (since,)).fetchone()[0]
    if oldest is None:
        return "📊 **Daily usage report** — no usage samples in the last 24h yet."
    hours = (now - max(oldest, since)) / 3600

    rows = db.execute(
        """
        SELECT mac, name, vid, SUM(up_bytes) AS up, SUM(down_bytes) AS down
        FROM samples WHERE ts > ?
        GROUP BY mac ORDER BY up + down DESC
        """,
        (since,),
    ).fetchall()

    def table(selected: list) -> str:
        lines = [f"{'Device':<26} {'VLAN':<8} {'Up GB':>7} {'Down GB':>8}"]
        for _, name, vid, up, down in selected:
            lines.append(f"{(name or '?')[:26]:<26} {vlan_label(thresholds, vid)[:8]:<8} {gb(up):>7.1f} {gb(down):>8.1f}")
        return "```\n" + "\n".join(lines) + "\n```"

    watched_vids = {int(vid) for vid, vlan in thresholds.get("vlans", {}).items() if vlan.get("report_top_uploaders")}
    top_uploaders = sorted((r for r in rows if r[2] in watched_vids), key=lambda r: r[3], reverse=True)[:5]
    total_up = sum(r[3] for r in rows)
    total_down = sum(r[4] for r in rows)

    parts = [
        f"📊 **Daily usage report** — last {hours:.0f}h, all clients: ↑ {gb(total_up):.1f} GB  ↓ {gb(total_down):.1f} GB",
        "_Per-client counters include LAN traffic (e.g. streaming from a local media server), not just internet._",
        "**Top 10 by total traffic**",
        table(rows[:10]),
    ]
    if top_uploaders:
        labels = "/".join(vlan_label(thresholds, vid) for vid in sorted(watched_vids))
        parts += [f"**Top {labels} uploaders**", table(top_uploaders)]

    new_devices = db.execute(
        "SELECT name, ip, mac, vid FROM first_seen WHERE ts > ? AND ts > (SELECT MIN(ts) FROM first_seen) + 600 ORDER BY ts",
        (since,),
    ).fetchall()
    if new_devices:
        parts.append("**New devices seen**")
        parts += [f"• {name or '?'} — `{ip}` `{mac}` ({vlan_label(thresholds, vid)})" for name, ip, mac, vid in new_devices]
    return "\n".join(parts)


def report_due(db: sqlite3.Connection, report_hour: int, now: int) -> bool:
    local = time.localtime(now)
    return local.tm_hour >= report_hour and get_meta(db, "last_report_date") != time.strftime("%Y-%m-%d", local)


# --- Main loop --------------------------------------------------------------


def run_once(config: Config, client: OmadaClient, db: sqlite3.Connection, now: int) -> None:
    max_gap = config.poll_interval * 2
    thresholds, problems = load_thresholds(config.thresholds_path)
    clients = client.list_clients()
    record_poll(db, clients, now, max_gap)
    report_threshold_problems(db, config.webhook_url, config.thresholds_path, problems)
    for message in check_alerts(db, clients, thresholds, now, config.poll_interval):
        post_discord(config.webhook_url, message)
    if report_due(db, config.report_hour, now):
        # Skip (but still mark as sent) when there's nothing to report yet, e.g.
        # the first poll after a fresh start past the report hour.
        if db.execute("SELECT 1 FROM samples WHERE ts > ? LIMIT 1", (now - DAY,)).fetchone():
            post_discord(config.webhook_url, build_report(db, thresholds, now))
        set_meta(db, "last_report_date", time.strftime("%Y-%m-%d", time.localtime(now)))
    db.commit()
    with open(config.heartbeat_path, "w") as f:
        f.write(str(now))
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} polled {len(clients)} clients", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--report-now", action="store_true", help="print/post the daily report from stored data and exit")
    args = parser.parse_args()

    config = Config.from_env()
    db = open_db(config.db_path)

    if args.report_now:
        thresholds, problems = load_thresholds(config.thresholds_path)
        for problem in problems:
            print(f"thresholds file {config.thresholds_path}: {problem}", file=sys.stderr, flush=True)
        post_discord(config.webhook_url, build_report(db, thresholds, int(time.time())))
        return

    client = OmadaClient(config)
    failures = 0
    while True:
        try:
            run_once(config, client, db, int(time.time()))
            if failures >= FAILURE_ALERT_POLLS:
                post_discord(config.webhook_url, "✅ Usage monitor is reaching the Omada controller again.")
            failures = 0
        except Exception as error:  # Keep polling through transient controller/network failures.
            if args.once:
                raise
            db.rollback()
            failures += 1
            print(f"poll failed ({failures} in a row): {error}", file=sys.stderr, flush=True)
            if failures == FAILURE_ALERT_POLLS:
                try:
                    post_discord(
                        config.webhook_url,
                        f"🛑 Usage monitor has failed {failures} polls in a row and isn't seeing traffic. Last error: `{error}`",
                    )
                except Exception as post_error:
                    print(f"failed to post failure alert: {post_error}", file=sys.stderr, flush=True)
        if args.once:
            return
        time.sleep(config.poll_interval)


if __name__ == "__main__":
    main()
