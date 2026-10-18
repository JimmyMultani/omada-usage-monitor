import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
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
        self.assertEqual(monitor.limits_for(THRESHOLDS, "11-11-11-11-11-11", 0), {kind: {} for kind in monitor.LIMIT_KINDS})


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

    def test_directory_means_report_only_with_a_docker_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            thresholds, problems = monitor.load_thresholds(directory)
        self.assertEqual(thresholds, {})
        self.assertIn("is a directory", problems[0])
        self.assertIn("Docker created a folder", problems[0])
        self.assertTrue(problems[0].endswith("running report-only (no alerts)"))

    def test_unreadable_file_means_report_only(self):
        if os.geteuid() == 0:
            self.skipTest("root can read files regardless of permissions")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "thresholds.json")
            with open(path, "w") as f:
                json.dump(THRESHOLDS, f)
            os.chmod(path, 0)
            thresholds, problems = monitor.load_thresholds(path)
        self.assertEqual(thresholds, {})
        self.assertEqual(problems, ["can't be read (Permission denied); running report-only (no alerts)"])

    def test_invalid_json_means_report_only(self):
        thresholds, problems = self.load('{"vlans": {')
        self.assertEqual(thresholds, {})
        self.assertTrue(problems[0].startswith("not valid JSON"))

    def test_vlan_flags_must_be_booleans(self):
        thresholds, problems = self.load({"vlans": {"20": {"alert_new_devices": "yes", "report_top_uploaders": True}}})
        self.assertEqual(thresholds["vlans"], {"20": {"report_top_uploaders": True}})
        self.assertEqual(problems, ['vlans["20"].alert_new_devices: must be true or false; ignored'])

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


class BaselineAlertTest(unittest.TestCase):
    """Days are written as one sample each; day 0 is the current 24h window."""

    GB = 10**9
    THRESHOLDS = {
        "vlans": {"20": {"label": "IoT", "baseline_multiplier": {"up": 5, "down": 5}}},
        "clients": {"AA-AA-AA-AA-AA-AA": {"baseline_multiplier": {"up": None}}},
    }

    def setUp(self):
        self.db = monitor.open_db(":memory:")
        self.now = 10_000_000

    def day(self, ago, up_gb=0.0, down_gb=0.0, mac="11-11-11-11-11-11", seconds=monitor.DAY):
        self.db.execute(
            "INSERT INTO samples VALUES (?, ?, 'Cam', '10.0.20.5', 20, ?, ?, ?)",
            (self.now - ago * monitor.DAY - 60, mac, int(up_gb * self.GB), int(down_gb * self.GB), seconds),
        )

    def history(self, up_gb, days=3, mac="11-11-11-11-11-11"):
        for ago in range(1, days + 1):
            self.day(ago, up_gb=up_gb, down_gb=1, mac=mac)

    def check(self, mac="11-11-11-11-11-11", at=None):
        return monitor.check_alerts(self.db, [client(mac=mac)], self.THRESHOLDS, at or self.now, INTERVAL)

    def test_alerts_when_last_24h_is_far_above_the_usual_day(self):
        self.history(up_gb=0.5)
        self.day(0, up_gb=15, down_gb=1)
        messages = self.check()
        self.assertEqual(len(messages), 1)
        self.assertIn("has uploaded 15.0 GB in the last 24h, 30× its usual 0.5 GB/day (limit 5×)", messages[0])

    def test_shows_a_decimal_near_the_limit(self):
        self.history(up_gb=2)
        self.day(0, up_gb=10.6, down_gb=1)
        self.assertIn("5.3× its usual 2.0 GB/day", self.check()[0])

    def test_within_multiplier_does_not_alert(self):
        self.history(up_gb=2)
        self.day(0, up_gb=9, down_gb=1)
        self.assertEqual(self.check(), [])

    def test_needs_minimum_full_days_of_history(self):
        self.history(up_gb=0.5, days=2)
        self.day(0, up_gb=15)
        self.assertEqual(self.check(), [])

    def test_small_days_never_alert(self):
        self.history(up_gb=0.01)
        self.day(0, up_gb=0.9)
        self.assertEqual(self.check(), [])

    def test_partially_measured_days_are_left_out_of_the_average(self):
        self.history(up_gb=2)
        self.day(4, up_gb=0.01, seconds=monitor.DAY // 4)
        self.day(5, up_gb=0.01, seconds=monitor.DAY // 4)
        self.day(0, up_gb=9, down_gb=1)
        self.assertEqual(self.check(), [])  # 4.5× the full days; partial days would make it 7.5×

    def test_days_beyond_the_window_are_left_out(self):
        self.history(up_gb=0.5)
        for ago in range(8, 12):
            self.day(ago, up_gb=10)
        self.day(0, up_gb=15)
        self.assertEqual(len(self.check()), 1)

    def test_repeats_at_most_daily(self):
        self.history(up_gb=0.5)
        self.day(0, up_gb=15)
        self.assertEqual(len(self.check()), 1)
        self.assertEqual(self.check(at=self.now + 7 * 3600), [])  # same 15 GB day, still in the last 24h
        # A day later the 15 GB day is part of the baseline (now 4.1 GB/day), so
        # it takes a bigger day to alert again.
        self.now += monitor.DAY + 60
        self.day(0, up_gb=40)
        self.assertEqual(len(self.check()), 1)

    def test_client_override_turns_it_off_per_direction(self):
        mac = "AA-AA-AA-AA-AA-AA"
        self.history(up_gb=0.5, mac=mac)
        self.day(0, up_gb=15, down_gb=20, mac=mac)
        messages = self.check(mac=mac)
        self.assertEqual(len(messages), 1)
        self.assertIn("has downloaded 20.0 GB", messages[0])

    def test_idle_device_that_wakes_up(self):
        self.history(up_gb=0)
        self.day(0, up_gb=3)
        self.assertIn("far above its usual 0.0 GB/day", self.check()[0])


class NewDeviceAlertTest(unittest.TestCase):
    WATCHING = {**THRESHOLDS, "vlans": {**THRESHOLDS["vlans"], "20": {**THRESHOLDS["vlans"]["20"], "alert_new_devices": True}}}

    def setUp(self):
        self.db = monitor.open_db(":memory:")
        self.start = 1_000_000

    def poll(self, at, *clients):
        monitor.record_poll(self.db, list(clients), at, INTERVAL * 2)
        return monitor.new_device_alerts(self.db, list(clients), self.WATCHING, at)

    def test_new_device_on_watched_vlan_alerts_once(self):
        known, new = client(), client(mac="00-11-22-33-44-55", name="Mystery Plug")
        self.assertEqual(self.poll(self.start, known), [])
        later = self.start + 3600
        messages = self.poll(later, known, new)
        self.assertEqual(len(messages), 1)
        self.assertIn("🆕 New device joined: **Mystery Plug** (IoT", messages[0])
        self.assertNotIn("randomized", messages[0])
        self.assertEqual(self.poll(later + INTERVAL, known, new), [])

    def test_devices_seen_during_startup_grace_period_are_not_new(self):
        self.poll(self.start, client())
        self.assertEqual(self.poll(self.start + INTERVAL, client(), client(mac="00-11-22-33-44-55")), [])

    def test_unwatched_vlan_does_not_alert(self):
        self.poll(self.start, client())
        self.assertEqual(self.poll(self.start + 3600, client(mac="00-11-22-33-44-55", vid=0)), [])

    def test_returning_device_is_not_new(self):
        self.poll(self.start, client())
        self.poll(self.start + INTERVAL)
        self.assertEqual(self.poll(self.start + 3600, client()), [])

    def test_randomized_mac_is_flagged(self):
        self.poll(self.start, client())
        messages = self.poll(self.start + 3600, client(mac="DA-11-22-33-44-55", name="iPhone"))
        self.assertIn("MAC is randomized", messages[0])

    def test_is_randomized_mac(self):
        self.assertEqual(
            [monitor.is_randomized_mac(mac) for mac in ("02-00-00-00-00-01", "DA-11-22-33-44-55", "00-11-22-33-44-55", "B8-27-EB-00-00-00")],
            [True, True, False, False],
        )


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

    def test_new_device_alert_is_posted(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config = make_config(data_dir, report_hour=24)  # never due
            with open(config.thresholds_path, "w") as f:
                json.dump(NewDeviceAlertTest.WATCHING, f)
            db = monitor.open_db(":memory:")
            omada = FakeOmada()
            with mock.patch.object(monitor, "post_discord") as post:
                omada.clients = [client()]
                monitor.run_once(config, omada, db, 1_000_000)
                omada.clients = [client(), client(mac="00-11-22-33-44-55", name="Mystery Plug")]
                monitor.run_once(config, omada, db, 1_000_000 + 3600)
                post.assert_called_once()
                self.assertIn("New device joined: **Mystery Plug**", post.call_args[0][1])

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


class MetricsTest(unittest.TestCase):
    def test_render_includes_clients_and_poll_health(self):
        metrics = monitor.Metrics()
        metrics.record_failure()
        metrics.record_success([client(name='Bob\'s "TV"\\', up=5, down=7)], 1_000_000)
        lines = metrics.render().splitlines()
        for expected in [
            f'omada_usage_monitor_info{{version="{monitor.__version__}"}} 1',
            'omada_usage_monitor_polls_total{result="success"} 1',
            'omada_usage_monitor_polls_total{result="failure"} 1',
            "omada_usage_monitor_consecutive_poll_failures 0",
            "omada_usage_monitor_last_success_timestamp_seconds 1000000",
            "omada_usage_monitor_clients 1",
            'omada_client_upload_bytes_total{mac="11-11-11-11-11-11",name="Bob\'s \\"TV\\"\\\\",vlan="20"} 5',
            'omada_client_download_bytes_total{mac="11-11-11-11-11-11",name="Bob\'s \\"TV\\"\\\\",vlan="20"} 7',
            "# TYPE omada_client_upload_bytes_total counter",
        ]:
            self.assertIn(expected, lines)

    def test_queued_messages(self):
        metrics = monitor.Metrics()
        self.assertIn("omada_usage_monitor_queued_messages 0", metrics.render().splitlines())
        metrics.record_queued(4)
        self.assertIn("omada_usage_monitor_queued_messages 4", metrics.render().splitlines())

    def test_failed_poll_drops_client_series_but_keeps_poll_health(self):
        metrics = monitor.Metrics()
        metrics.record_success([client(up=5)], 1_000_000)
        metrics.record_failure()
        lines = metrics.render().splitlines()
        self.assertFalse([line for line in lines if line.startswith("omada_client_")])
        self.assertIn("# TYPE omada_client_upload_bytes_total counter", lines)
        for expected in [
            "omada_usage_monitor_consecutive_poll_failures 1",
            "omada_usage_monitor_last_success_timestamp_seconds 1000000",
            "omada_usage_monitor_clients 1",
        ]:
            self.assertIn(expected, lines)

    def test_no_last_success_before_first_poll(self):
        self.assertNotIn("last_success", monitor.Metrics().render())

    def test_server_serves_metrics_and_404s_elsewhere(self):
        server = monitor.serve_metrics(monitor.Metrics(), 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"], "text/plain; version=0.0.4; charset=utf-8")
            self.assertIn("omada_usage_monitor_info", response.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base}/", timeout=5)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.db = monitor.open_db(":memory:")

    def queue(self, *messages):
        for message in messages:
            monitor.queue_message(self.db, message)
        self.db.commit()

    def posted(self, post):
        return [call.args[1].strip() for call in post.call_args_list]

    def test_failure_partway_does_not_resend_what_already_went_out(self):
        self.queue("one", "two", "three")
        with mock.patch.object(monitor, "post_discord", side_effect=[None, OSError("429"), None, None]) as post:
            monitor.deliver_queued(self.db, "https://discord.example/webhook")
            self.assertEqual(monitor.queued_count(self.db), 2)
            monitor.deliver_queued(self.db, "https://discord.example/webhook")
        self.assertEqual(self.posted(post), ["one", "two", "two", "three"])
        self.assertEqual(monitor.queued_count(self.db), 0)

    def test_poll_succeeds_and_keeps_its_messages_when_discord_is_down(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config = make_config(data_dir, report_hour=24)  # never due; thresholds file missing → one notice
            omada = FakeOmada()
            omada.clients = [client()]
            with mock.patch.object(monitor, "post_discord", side_effect=OSError("discord down")):
                monitor.run_once(config, omada, self.db, 1_000_000)
            self.assertTrue(os.path.exists(config.heartbeat_path))
            self.assertEqual(monitor.queued_count(self.db), 1)

            with mock.patch.object(monitor, "post_discord") as post:
                monitor.run_once(config, omada, self.db, 1_000_000 + INTERVAL)
            post.assert_called_once()
            self.assertIn("file not found", post.call_args.args[1])
            self.assertEqual(monitor.queued_count(self.db), 0)

    def test_messages_from_a_rolled_back_poll_are_never_sent(self):
        monitor.queue_message(self.db, "from a failed poll")
        self.db.rollback()
        self.assertEqual(monitor.queued_count(self.db), 0)

    def test_queue_is_capped_dropping_the_oldest(self):
        with mock.patch.object(monitor, "MAX_QUEUED_MESSAGES", 3):
            self.queue("1", "2", "3", "4", "5")
        contents = [row[0].strip() for row in self.db.execute("SELECT content FROM outbox ORDER BY id")]
        self.assertEqual(contents, ["3", "4", "5"])

    def test_dropped_messages_are_announced_once_before_the_rest(self):
        with mock.patch.object(monitor, "MAX_QUEUED_MESSAGES", 3):
            self.queue("1", "2", "3", "4", "5")
        with mock.patch.object(monitor, "post_discord", side_effect=[OSError("down"), None, None, None, None]) as post:
            monitor.deliver_queued(self.db, "https://discord.example/webhook")  # notice fails, nothing else tried
            monitor.deliver_queued(self.db, "https://discord.example/webhook")
        self.assertEqual(self.posted(post), [
            "⚠️ 2 older messages were dropped while Discord was unreachable.",
            "⚠️ 2 older messages were dropped while Discord was unreachable.",
            "3", "4", "5",
        ])
        self.queue("6")
        with mock.patch.object(monitor, "post_discord") as post:
            monitor.deliver_queued(self.db, "https://discord.example/webhook")
        self.assertEqual(self.posted(post), ["6"])

    def test_long_messages_are_queued_as_chunks(self):
        self.queue("header\n" + "\n".join("x" * 50 for _ in range(100)))
        self.assertGreater(monitor.queued_count(self.db), 1)


class PostChunkTest(unittest.TestCase):
    URL = "https://discord.example/webhook"

    def rate_limited(self, body=b'{"retry_after": 0.5}', headers=None):
        return urllib.error.HTTPError(self.URL, 429, "Too Many Requests", headers or {}, io.BytesIO(body))

    def test_waits_out_a_rate_limit_and_retries(self):
        with mock.patch("urllib.request.urlopen", side_effect=[self.rate_limited(), mock.MagicMock()]) as urlopen, \
                mock.patch.object(monitor.time, "sleep") as sleep:
            monitor.post_chunk(self.URL, "hello")
        sleep.assert_called_once_with(0.5)
        self.assertEqual(urlopen.call_count, 2)

    def test_uses_the_retry_after_header_without_a_json_body(self):
        error = self.rate_limited(body=b"slow down", headers={"Retry-After": "2"})
        with mock.patch("urllib.request.urlopen", side_effect=[error, mock.MagicMock()]), \
                mock.patch.object(monitor.time, "sleep") as sleep:
            monitor.post_chunk(self.URL, "hello")
        sleep.assert_called_once_with(2.0)

    def test_gives_up_on_a_long_wait(self):
        error = self.rate_limited(body=b'{"retry_after": 600}')
        with mock.patch("urllib.request.urlopen", side_effect=[error]), mock.patch.object(monitor.time, "sleep") as sleep, \
                self.assertRaises(urllib.error.HTTPError):
            monitor.post_chunk(self.URL, "hello")
        sleep.assert_not_called()

    def test_gives_up_after_repeated_rate_limits(self):
        errors = [self.rate_limited() for _ in range(monitor.DISCORD_RATE_LIMIT_RETRIES + 1)]
        with mock.patch("urllib.request.urlopen", side_effect=errors), mock.patch.object(monitor.time, "sleep"), \
                self.assertRaises(urllib.error.HTTPError):
            monitor.post_chunk(self.URL, "hello")

    def test_other_errors_are_not_retried(self):
        error = urllib.error.HTTPError(self.URL, 404, "Not Found", {}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=[error]) as urlopen, self.assertRaises(urllib.error.HTTPError):
            monitor.post_chunk(self.URL, "hello")
        self.assertEqual(urlopen.call_count, 1)


class SplitMessageTest(unittest.TestCase):
    def test_long_code_block_is_split_and_reopened(self):
        content = "header\n```\n" + "\n".join("x" * 50 for _ in range(100)) + "\n```"
        chunks = monitor.split_message(content)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), monitor.DISCORD_LIMIT)
            self.assertEqual(chunk.count("```") % 2, 0)


class CliTest(unittest.TestCase):
    """Runs main() with no Omada settings in the environment, as a deployment
    repo's CI would."""

    def run_main(self, *args, env=None):
        output = io.StringIO()
        with mock.patch("sys.argv", ["monitor.py", *args]), mock.patch.dict(os.environ, env or {}, clear=True), \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            monitor.main()
        return exit.exception.code, output.getvalue()

    def write(self, content):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "thresholds.json")
        with open(path, "w") as f:
            json.dump(content, f)
        return path

    def test_check_thresholds_passes_a_good_file(self):
        code, output = self.run_main("--check-thresholds", self.write(THRESHOLDS))
        self.assertEqual(code, 0)
        self.assertIn(": OK (2 VLANs, 1 client overrides)", output)

    def test_check_thresholds_fails_on_any_problem(self):
        code, output = self.run_main("--check-thresholds", self.write({"vlans": {"20": {"sustained_mpbs": {}}}}))
        self.assertEqual(code, 1)
        self.assertIn('vlans["20"]: unknown key "sustained_mpbs"; ignored', output)

    def test_check_thresholds_fails_on_a_missing_file(self):
        code, output = self.run_main("--check-thresholds", "/nonexistent/thresholds.json")
        self.assertEqual(code, 1)
        self.assertIn("file not found", output)

    def test_check_thresholds_defaults_to_thresholds_path(self):
        code, output = self.run_main("--check-thresholds", env={"THRESHOLDS_PATH": self.write(THRESHOLDS)})
        self.assertEqual(code, 0)
        self.assertIn(": OK", output)

    def test_version(self):
        code, output = self.run_main("--version")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), f"omada-usage-monitor {monitor.__version__}")


if __name__ == "__main__":
    unittest.main()
