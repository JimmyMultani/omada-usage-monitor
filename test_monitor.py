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


class LoadThresholdsTest(unittest.TestCase):
    def load(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "thresholds.json")
            with open(path, "w") as f:
                f.write(content if isinstance(content, str) else json.dumps(content))
            return monitor.load_thresholds(path)

    def test_valid_file_has_no_problems(self):
        self.assertEqual(self.load(THRESHOLDS), (THRESHOLDS, []))

    def test_example_file_has_no_problems(self):
        example = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.example.json")
        self.assertEqual(monitor.load_thresholds(example)[1], [])

    def test_missing_file_means_report_only(self):
        thresholds, problems = monitor.load_thresholds("/nonexistent/thresholds.json")
        self.assertEqual(thresholds, {})
        self.assertEqual(problems, ["file not found; running report-only (no alerts)"])

    def test_invalid_json_means_report_only(self):
        thresholds, problems = self.load('{"vlans": {')
        self.assertEqual(thresholds, {})
        self.assertTrue(problems[0].startswith("not valid JSON"))

    def test_client_macs_are_normalized(self):
        thresholds, problems = self.load({"clients": {" aa:bb:cc:dd:ee:0f ": {"daily_gb": {"up": 1}}}})
        self.assertEqual(problems, [])
        self.assertEqual(thresholds["clients"], {"AA-BB-CC-DD-EE-0F": {"daily_gb": {"up": 1}}})

    def test_unusable_values_are_dropped_and_reported(self):
        thresholds, problems = self.load({
            "vlan": {},
            "vlans": {
                "IoT": {"sustained_mpbs": {"up": 1}, "daily_gb": {"up": "5", "down": 500, "total": 1}},
                "30": "report only",
                "40": {"label": 40, "sustained_mbps": 10},
            },
            "clients": {"not-a-mac": {"daily_gb": {"up": True, "down": -1}}},
        })
        self.assertEqual(thresholds, {
            "vlans": {"IoT": {"daily_gb": {"down": 500}}, "30": {}, "40": {}},
            "clients": {"NOT-A-MAC": {"daily_gb": {}}},
        })
        self.assertEqual(problems, [
            'unknown top-level key "vlan"; ignored',
            'vlans["IoT"]: key must be a VLAN ID like "20"',
            'vlans["IoT"]: unknown key "sustained_mpbs"; ignored',
            'vlans["IoT"].daily_gb.up: must be a non-negative number or null; ignored',
            'vlans["IoT"].daily_gb: unknown key "total"; ignored',
            'vlans["30"]: must be an object; ignored',
            'vlans["40"].label: must be a string; ignored',
            'vlans["40"].sustained_mbps: must be an object like {"up": 10, "down": 100}; ignored',
            'clients["not-a-mac"]: not a MAC address like "AA-BB-CC-DD-EE-FF"',
            'clients["not-a-mac"].daily_gb.up: must be a non-negative number or null; ignored',
            'clients["not-a-mac"].daily_gb.down: must be a non-negative number or null; ignored',
        ])


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


def make_config(data_dir, report_hour=0):
    return monitor.Config(
        base_url="", client_id="", client_secret="", omadac_id="", site_id="", strict_ssl=False,
        webhook_url=None, db_path=":memory:", heartbeat_path=os.path.join(data_dir, "heartbeat"),
        thresholds_path=os.path.join(data_dir, "thresholds.json"), poll_interval=INTERVAL, report_hour=report_hour,
    )


class RunOnceTest(unittest.TestCase):
    def test_missing_thresholds_file_is_reported_once_and_polling_continues(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config = make_config(data_dir, report_hour=24)  # never due
            db = monitor.open_db(":memory:")
            omada = FakeOmada()
            with mock.patch.object(monitor, "post_discord") as post:
                for i in range(3):
                    omada.clients = [client(up=i * 10**9)]
                    monitor.run_once(config, omada, db, 1_000_000 + i * INTERVAL)
                post.assert_called_once()
                self.assertIn("file not found", post.call_args[0][1])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)

                with open(config.thresholds_path, "w") as f:
                    json.dump(THRESHOLDS, f)
                monitor.run_once(config, omada, db, 1_000_000 + 3 * INTERVAL)
                self.assertEqual(post.call_count, 2)
                self.assertIn("no more problems", post.call_args[0][1])

    def test_daily_report_skipped_until_there_is_data(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config = make_config(data_dir)
            with open(config.thresholds_path, "w") as f:
                json.dump(THRESHOLDS, f)
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
