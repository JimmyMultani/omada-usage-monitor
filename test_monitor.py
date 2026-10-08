import json
import os
import tempfile
import unittest
from unittest import mock

import monitor

INTERVAL = 300
MB = 1_000_000

THRESHOLDS = {
    "vlans": {
        "0": {"label": "Default"},
        "20": {
            "label": "IoT",
            "report_top_uploaders": True,
            "sustained_mbps": {"up": 10, "down": 100},
            "daily_gb": {"up": 5, "down": 500},
        },
    },
    "clients": {"AA-AA-AA-AA-AA-AA": {"sustained_mbps": {"up": None}, "daily_gb": {"up": None}}},
}


def client(mac="11-11-11-11-11-11", up=0, down=0, uptime=1000, vid=20, name="Cam"):
    return {"mac": mac, "name": name, "ip": "10.0.20.5", "vid": vid, "trafficUp": up, "trafficDown": down, "uptime": uptime}


def upload_mbps(rate):
    """Bytes uploaded in one poll interval at `rate` Mbps."""
    return int(rate * MB / 8 * INTERVAL)


class ComputeDeltaTest(unittest.TestCase):
    def test_first_sighting_has_no_delta(self):
        self.assertIsNone(monitor.compute_delta(None, 10, 10, 100, 1000, 600))

    def test_normal_delta(self):
        self.assertEqual(monitor.compute_delta((700, 100, 200, 50), 150, 260, 350, 1000, 600), (50, 60, 300))

    def test_counter_reset_is_skipped(self):
        self.assertIsNone(monitor.compute_delta((700, 100, 200, 500), 5, 5, 20, 1000, 600))

    def test_uptime_reset_is_skipped_even_if_counters_grew(self):
        self.assertIsNone(monitor.compute_delta((700, 100, 200, 500), 150, 260, 20, 1000, 600))

    def test_long_gap_is_skipped(self):
        self.assertIsNone(monitor.compute_delta((0, 100, 200, 50), 150, 260, 5000, 1000, 600))


class LimitsTest(unittest.TestCase):
    def test_client_override_replaces_only_named_directions(self):
        limits = monitor.limits_for(THRESHOLDS, "AA-AA-AA-AA-AA-AA", 20)
        self.assertEqual(limits["sustained_mbps"], {"up": None, "down": 100})

    def test_report_only_vlan_has_no_limits(self):
        self.assertEqual(monitor.limits_for(THRESHOLDS, "11-11-11-11-11-11", 0), {"sustained_mbps": {}, "daily_gb": {}})


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.db = monitor.open_db(":memory:")
        self.now = 1_000_000

    def poll(self, *clients):
        self.now += INTERVAL
        monitor.record_poll(self.db, list(clients), self.now, INTERVAL * 2)
        return monitor.check_alerts(self.db, list(clients), THRESHOLDS, self.now, INTERVAL)

    def test_sustained_upload_alerts_once_after_three_polls(self):
        up = 0
        self.poll(client(up=up))
        messages = []
        for _ in range(4):
            up += upload_mbps(12)
            messages.append(self.poll(client(up=up)))
        self.assertEqual([len(m) for m in messages], [0, 0, 1, 0])
        self.assertIn("uploading at ≥ 12.0 Mbps", messages[2][0])

    def test_single_spike_does_not_alert(self):
        up = 0
        self.poll(client(up=up))
        for rate in (1, 50, 1, 1):
            up += upload_mbps(rate)
            self.assertEqual(self.poll(client(up=up)), [])

    def test_override_suppresses_alerts(self):
        up = 0
        self.poll(client(mac="AA-AA-AA-AA-AA-AA", up=up))
        for _ in range(4):
            up += upload_mbps(50)
            self.assertEqual(self.poll(client(mac="AA-AA-AA-AA-AA-AA", up=up)), [])

    def test_daily_volume_alert(self):
        self.poll(client(up=0))
        messages = self.poll(client(up=6 * 10**9))
        self.assertEqual(len(messages), 1)
        self.assertIn("uploaded 6.0 GB in the last 24h", messages[0])

    def test_cooldown_expires(self):
        self.assertTrue(monitor.claim_alert(self.db, "m", "daily_up", 0))
        self.assertFalse(monitor.claim_alert(self.db, "m", "daily_up", monitor.ALERT_COOLDOWN_SECONDS - 1))
        self.assertTrue(monitor.claim_alert(self.db, "m", "daily_up", monitor.ALERT_COOLDOWN_SECONDS))


class ReportTest(unittest.TestCase):
    def test_report_lists_top_talkers_and_new_devices(self):
        db = monitor.open_db(":memory:")
        now = 1_000_000
        monitor.record_poll(db, [client(name="Cam")], now, INTERVAL * 2)
        now += INTERVAL
        monitor.record_poll(db, [client(name="Cam", up=3 * 10**9)], now, INTERVAL * 2)
        now += 3600
        monitor.record_poll(db, [client(name="Cam", up=3 * 10**9), client(mac="22-22-22-22-22-22", name="Mystery Plug")], now, INTERVAL * 2)

        report = monitor.build_report(db, THRESHOLDS, now)
        self.assertIn("Top IoT uploaders", report)
        self.assertIn("Cam", report)
        self.assertIn("**New devices seen**\n• Mystery Plug", report)

    def test_empty_report(self):
        self.assertIn("no usage samples", monitor.build_report(monitor.open_db(":memory:"), THRESHOLDS, 1000))


class FakeOmada:
    def __init__(self):
        self.clients = []

    def list_clients(self):
        return self.clients


class RunOnceTest(unittest.TestCase):
    def test_daily_report_skipped_until_there_is_data(self):
        with tempfile.TemporaryDirectory() as data_dir:
            thresholds_path = os.path.join(data_dir, "thresholds.json")
            with open(thresholds_path, "w") as f:
                json.dump(THRESHOLDS, f)
            config = monitor.Config(
                base_url="", client_id="", client_secret="", omadac_id="", site_id="", strict_ssl=False,
                webhook_url=None, db_path=":memory:", heartbeat_path=os.path.join(data_dir, "heartbeat"),
                thresholds_path=thresholds_path, poll_interval=INTERVAL, report_hour=0,
            )
            db = monitor.open_db(":memory:")
            omada = FakeOmada()
            omada.clients = [client()]
            with mock.patch.object(monitor, "post_discord") as post:
                monitor.run_once(config, omada, db, 1_000_000)
                post.assert_not_called()
                self.assertIsNotNone(monitor.get_meta(db, "last_report_date"))

                db.execute("DELETE FROM meta")
                omada.clients = [client(up=10**6)]
                monitor.run_once(config, omada, db, 1_000_000 + INTERVAL)
                post.assert_called_once()
                self.assertIn("Daily usage report", post.call_args[0][1])


class SplitMessageTest(unittest.TestCase):
    def test_long_code_block_is_split_and_reopened(self):
        content = "header\n```\n" + "\n".join("x" * 50 for _ in range(100)) + "\n```"
        chunks = monitor.split_message(content)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), monitor.DISCORD_LIMIT)
            self.assertEqual(chunk.count("```") % 2, 0)


if __name__ == "__main__":
    unittest.main()
