import csv
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import CsvTailer, MonitorServer, ProcessState, RuntimeMonitor  # noqa: E402


HEADERS = [
    "frame", "t_ms", "loop_ms", "inference_ms", "deadline_misses", "consecutive_errors",
    "vx", "vy", "wz", "vx_raw", "vy_raw", "wz_raw",
    "theory_vx", "theory_vy", "theory_wz", "cmd_source",
    "uwb_beta", "uwb_pitch", "uwb_distance", "uwb_yaw",
    "uwb_valid", "uwb_age_s", "uwb_error", "uwb_enabled", "uwb_channel",
    "uwb_closing", "sport_valid", "sport_age_s", "sport_vx", "sport_vy", "sport_vz", "sport_wz",
    "feedback_source", "feedback_valid", "feedback_age_s", "feedback_vx", "feedback_vy",
    "feedback_vz", "feedback_wz", "feedback_err_vx", "feedback_err_vy", "feedback_err_wz",
    "dep_inval", "dep_meanv", "front_inval", "front_min", "front_mean",
]


def row(frame=1, uwb_valid=1, dep_inval=0.1):
    values = {key: 0 for key in HEADERS}
    values.update({
        "frame": frame, "t_ms": frame * 20, "loop_ms": 7.5, "inference_ms": 4.2,
        "vx": 0.2, "theory_vx": 0.25, "cmd_source": 1,
        "uwb_beta": 0.1, "uwb_distance": 2.3, "uwb_valid": uwb_valid,
        "uwb_age_s": 0.02, "uwb_error": 0, "uwb_enabled": 1, "uwb_channel": 1,
        "sport_valid": 1, "sport_age_s": 0.01, "sport_vx": 0.18,
        "feedback_source": 1, "feedback_valid": 1, "feedback_age_s": 0.01,
        "feedback_vx": 0.18, "feedback_err_vx": -0.02,
        "dep_inval": dep_inval, "dep_meanv": 0.4, "front_inval": 0.05,
        "front_min": 0.2, "front_mean": 0.45,
    })
    return [values[key] for key in HEADERS]


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.csv_path = self.root / "visloco_diag_1.csv"
        with self.csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(HEADERS)
            writer.writerow(row(1))

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        return {
            "csv_globs": [str(self.root / "visloco_diag_*.csv")],
            "text_log_globs": [str(self.root / "*.log")],
            "process_patterns": ["definitely-not-running"],
            "poll_interval_s": 0.05, "process_interval_s": 10,
            "csv_stale_s": 3, "uwb_stale_s": 0.5, "sport_stale_s": 0.5,
            "depth_invalid_warn": 0.85, "depth_invalid_bad": 0.98,
            "depth_stagnant_frames": 150, "speed_error_good": 0.08, "speed_error_warn": 0.15,
            "inference_warn_ms": 15, "loop_warn_ms": 20,
            "history_max_samples": 100, "log_tail_lines": 20,
        }

    def test_incremental_tail(self):
        tailer = CsvTailer([str(self.root / "visloco_diag_*.csv")])
        path, rows, switched = tailer.read()
        self.assertTrue(switched)
        self.assertEqual(path, self.csv_path)
        self.assertEqual(rows[-1]["frame"], "1")
        with self.csv_path.open("a", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerow(row(2))
        _, rows, switched = tailer.read()
        self.assertFalse(switched)
        self.assertEqual([item["frame"] for item in rows], ["2"])

    def test_rotation(self):
        tailer = CsvTailer([str(self.root / "visloco_diag_*.csv")])
        tailer.read()
        second = self.root / "visloco_diag_2.csv"
        with second.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream); writer.writerow(HEADERS); writer.writerow(row(99))
        future = time.time() + 2
        second.touch()
        import os
        os.utime(second, (future, future))
        path, rows, switched = tailer.read()
        self.assertTrue(switched)
        self.assertEqual(path, second)
        self.assertEqual(rows[-1]["frame"], "99")

    def test_status_mapping(self):
        monitor = RuntimeMonitor(self.config())
        monitor.csv_path = self.csv_path
        monitor.csv_mtime = time.time()
        monitor.process = ProcessState(time.time(), {"go2_loco_ctrl": ["123 go2_loco_ctrl"]})
        raw = dict(zip(HEADERS, map(str, row(1))))
        monitor.latest = monitor._normalize(raw, self.csv_path)
        health = monitor._health(time.time())
        self.assertEqual(health["controller"]["state"], "good")
        self.assertEqual(health["uwb"]["state"], "good")
        self.assertEqual(health["camera"]["state"], "good")
        self.assertEqual(health["feedback"]["label"], "SportState 实测")

    def test_http_status_and_static_page(self):
        monitor = RuntimeMonitor(self.config())
        server = MonitorServer(("127.0.0.1", 0), monitor)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertIn("status", payload)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                page = response.read().decode("utf-8")
                self.assertIn("Go2 实时运行监控", page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_config_is_valid_json(self):
        path = Path(__file__).resolve().parents[1] / "config.json"
        self.assertIn("csv_globs", json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
