#!/usr/bin/env python3
"""Read-only Go2 runtime monitor.

Run this file on the Go2/Jetson. It tails the controller's diagnostic CSV,
checks local Linux processes, and serves a self-contained browser dashboard.
Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import mimetypes
import os
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DEFAULT_CONFIG = APP_DIR / "config.json"


def number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def flag(value: Any) -> bool:
    return integer(value, 0) != 0


def level(state: str, label: str, detail: str = "") -> dict[str, str]:
    return {"state": state, "label": label, "detail": detail}


def newest_file(patterns: Iterable[str]) -> Path | None:
    candidates: list[Path] = []
    for pattern in patterns:
        for raw in glob.iglob(os.path.expanduser(pattern), recursive=True):
            path = Path(raw)
            try:
                if path.is_file():
                    candidates.append(path)
            except OSError:
                continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def tail_text(path: Path, lines: int) -> list[str]:
    """Return the last lines without loading an unbounded log into memory."""
    block_size = 8192
    data = b""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            while position > 0 and data.count(b"\n") <= lines:
                take = min(block_size, position)
                position -= take
                stream.seek(position)
                data = stream.read(take) + data
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-lines:]


class CsvTailer:
    """Incrementally reads complete rows and follows CSV rotation."""

    def __init__(self, patterns: list[str]) -> None:
        self.patterns = patterns
        self.path: Path | None = None
        self.headers: list[str] = []
        self.offset = 0
        self.pending = b""

    def _switch(self, path: Path) -> list[dict[str, str]]:
        self.path = path
        self.headers = []
        self.offset = 0
        self.pending = b""
        rows: list[dict[str, str]] = []
        try:
            with path.open("rb") as stream:
                header_raw = stream.readline()
                self.headers = next(csv.reader([header_raw.decode("utf-8-sig", "replace")]))
                header_end = stream.tell()
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                start = max(header_end, size - 512 * 1024)
                stream.seek(start)
                if start > header_end:
                    stream.readline()
                raw = stream.read()
                self.offset = stream.tell()
            rows = self._decode_complete(raw, keep_partial=True)
        except (OSError, csv.Error, StopIteration):
            self.headers = []
            self.offset = 0
        return rows

    def _decode_complete(self, raw: bytes, keep_partial: bool = True) -> list[dict[str, str]]:
        if not raw or not self.headers:
            return []
        data = self.pending + raw
        self.pending = b""
        if keep_partial and not data.endswith((b"\n", b"\r")):
            cut = data.rfind(b"\n")
            if cut < 0:
                self.pending = data
                return []
            self.pending = data[cut + 1 :]
            data = data[: cut + 1]
        result: list[dict[str, str]] = []
        for raw_line in data.splitlines():
            if not raw_line.strip():
                continue
            try:
                values = next(csv.reader([raw_line.decode("utf-8", "replace")]))
            except (csv.Error, StopIteration):
                continue
            if len(values) < len(self.headers):
                continue
            result.append(dict(zip(self.headers, values)))
        return result

    def read(self) -> tuple[Path | None, list[dict[str, str]], bool]:
        path = newest_file(self.patterns)
        if path is None:
            self.path = None
            self.headers = []
            self.offset = 0
            self.pending = b""
            return None, [], False
        switched = path != self.path
        if switched:
            return path, self._switch(path), True
        try:
            size = path.stat().st_size
            if size < self.offset:
                return path, self._switch(path), True
            if size == self.offset:
                return path, [], False
            with path.open("rb") as stream:
                stream.seek(self.offset)
                raw = stream.read()
                self.offset = stream.tell()
            return path, self._decode_complete(raw), False
        except OSError:
            return path, [], False


@dataclass
class ProcessState:
    checked_at: float
    matches: dict[str, list[str]]

    @property
    def running(self) -> bool:
        return any(self.matches.values())


def inspect_processes(patterns: list[str]) -> ProcessState:
    matches: dict[str, list[str]] = {}
    for pattern in patterns:
        try:
            result = subprocess.run(
                ["pgrep", "-af", pattern],
                text=True,
                capture_output=True,
                timeout=1.5,
                check=False,
            )
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            matches[pattern] = [line for line in lines if "server.py" not in line]
        except (OSError, subprocess.TimeoutExpired):
            matches[pattern] = []
    return ProcessState(time.time(), matches)


class RuntimeMonitor:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.tailer = CsvTailer(list(config["csv_globs"]))
        self.history: deque[dict[str, Any]] = deque(maxlen=int(config["history_max_samples"]))
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.revision = 0
        self.event: dict[str, Any] = {}
        self.latest: dict[str, Any] | None = None
        self.csv_path: Path | None = None
        self.csv_mtime = 0.0
        self.process = ProcessState(0.0, {})
        self.last_process_check = 0.0
        self.depth_signature: tuple[float | None, ...] | None = None
        self.depth_same_frames = 0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="runtime-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=3)

    def _normalize(self, row: dict[str, str], path: Path) -> dict[str, Any]:
        now = time.time()
        depth_sig = tuple(number(row.get(key)) for key in (
            "dep_inval", "dep_meanv", "front_inval", "front_min", "front_mean"
        ))
        if depth_sig == self.depth_signature:
            self.depth_same_frames += 1
        else:
            self.depth_signature = depth_sig
            self.depth_same_frames = 0
        source_code = integer(row.get("feedback_source"), 0)
        source_name = {0: "none", 1: "sport", 2: "uwb_estimate"}.get(source_code, "unknown")
        return {
            "received_at": now,
            "file": str(path),
            "frame": integer(row.get("frame"), -1),
            "t_ms": integer(row.get("t_ms"), -1),
            "command": {
                "vx": number(row.get("vx"), 0.0), "vy": number(row.get("vy"), 0.0),
                "wz": number(row.get("wz"), 0.0),
                "raw_vx": number(row.get("vx_raw"), 0.0), "raw_vy": number(row.get("vy_raw"), 0.0),
                "raw_wz": number(row.get("wz_raw"), 0.0),
                "theory_vx": number(row.get("theory_vx"), 0.0),
                "theory_vy": number(row.get("theory_vy"), 0.0),
                "theory_wz": number(row.get("theory_wz"), 0.0),
                "source": integer(row.get("cmd_source"), -1),
            },
            "uwb": {
                "valid": flag(row.get("uwb_valid")), "age_s": number(row.get("uwb_age_s"), -1.0),
                "error": integer(row.get("uwb_error"), 255), "enabled": flag(row.get("uwb_enabled")),
                "channel": integer(row.get("uwb_channel"), -1),
                "beta": number(row.get("uwb_beta")), "pitch": number(row.get("uwb_pitch")),
                "distance": number(row.get("uwb_distance")), "yaw": number(row.get("uwb_yaw")),
                "closing": number(row.get("uwb_closing")),
            },
            "sport": {
                "valid": flag(row.get("sport_valid")), "age_s": number(row.get("sport_age_s"), -1.0),
                "vx": number(row.get("sport_vx"), 0.0), "vy": number(row.get("sport_vy"), 0.0),
                "vz": number(row.get("sport_vz"), 0.0), "wz": number(row.get("sport_wz"), 0.0),
            },
            "feedback": {
                "valid": flag(row.get("feedback_valid")),
                "source": source_name, "source_code": source_code,
                "age_s": number(row.get("feedback_age_s"), -1.0),
                "vx": number(row.get("feedback_vx"), 0.0), "vy": number(row.get("feedback_vy"), 0.0),
                "vz": number(row.get("feedback_vz"), 0.0), "wz": number(row.get("feedback_wz"), 0.0),
                "err_vx": number(row.get("feedback_err_vx"), 0.0),
                "err_vy": number(row.get("feedback_err_vy"), 0.0),
                "err_wz": number(row.get("feedback_err_wz"), 0.0),
            },
            "depth": {
                "invalid": number(row.get("dep_inval")), "mean": number(row.get("dep_meanv")),
                "front_invalid": number(row.get("front_inval")),
                "front_min": number(row.get("front_min")), "front_mean": number(row.get("front_mean")),
                "same_frames": self.depth_same_frames,
            },
            "performance": {
                "loop_ms": number(row.get("loop_ms")), "inference_ms": number(row.get("inference_ms")),
                "deadline_misses": integer(row.get("deadline_misses"), 0),
                "consecutive_errors": integer(row.get("consecutive_errors"), 0),
            },
            "imu": {
                "avx": number(row.get("avx")), "avy": number(row.get("avy")), "avz": number(row.get("avz")),
                "pgx": number(row.get("pgx")), "pgy": number(row.get("pgy")), "pgz": number(row.get("pgz")),
            },
        }

    def _health(self, now: float) -> dict[str, Any]:
        csv_age = now - self.csv_mtime if self.csv_mtime else None
        fresh = csv_age is not None and csv_age <= float(self.config["csv_stale_s"])
        sample = self.latest
        controller = level(
            "good" if self.process.running else "bad",
            "运行中" if self.process.running else "未运行",
            "；".join(key for key, values in self.process.matches.items() if values) or "未找到匹配进程",
        )
        if not self.csv_path:
            vision = level("unknown", "等待日志", "尚未找到 visloco_diag CSV")
        elif fresh:
            vision = level("good", "数据更新中", f"CSV 延迟 {csv_age:.2f}s")
        else:
            vision = level("bad", "数据已停止", f"CSV 已 {csv_age:.1f}s 未刷新")

        if not sample or not fresh:
            uwb = level("unknown" if not sample else "bad", "无实时数据", "等待新鲜 CSV 数据")
            camera = level("unknown" if not sample else "bad", "无法确认", "深度数据未刷新")
            feedback = level("unknown" if not sample else "bad", "无反馈", "实际速度数据未刷新")
            inference = level("unknown" if not sample else "bad", "无实时数据", "推理数据未刷新")
        else:
            u = sample["uwb"]
            if u["valid"] and u["age_s"] is not None and u["age_s"] <= float(self.config["uwb_stale_s"]):
                uwb = level("good", "UWB 正常", f"age={u['age_s']:.3f}s, channel={u['channel']}")
            elif u["enabled"]:
                uwb = level("warn", "UWB 无效", f"error={u['error']}, age={u['age_s']}s")
            else:
                uwb = level("bad", "UWB 未使能/未收到", f"error={u['error']}, age={u['age_s']}s")

            d = sample["depth"]
            invalid = d["invalid"]
            if invalid is None:
                camera = level("unknown", "没有深度字段", "CSV 不含 dep_inval")
            elif invalid >= float(self.config["depth_invalid_bad"]):
                camera = level("bad", "深度数据异常", f"无效像素 {invalid * 100:.1f}%")
            elif d["same_frames"] >= int(self.config["depth_stagnant_frames"]):
                camera = level("warn", "疑似卡帧/常量源", f"统计值连续 {d['same_frames']} 帧未变化")
            elif invalid >= float(self.config["depth_invalid_warn"]):
                camera = level("warn", "深度质量较差", f"无效像素 {invalid * 100:.1f}%")
            else:
                camera = level("good", "深度数据正常", f"无效像素 {invalid * 100:.1f}%（基于数据推断）")

            f = sample["feedback"]
            feedback_age = f["age_s"] if f["age_s"] is not None else -1.0
            if (f["valid"] and f["source"] == "sport" and
                    0.0 <= feedback_age <= float(self.config["sport_stale_s"])):
                feedback = level("good", "SportState 实测", f"age={feedback_age:.3f}s")
            elif f["valid"] and f["source"] == "sport":
                feedback = level("warn", "SportState 数据偏旧", f"age={feedback_age:.3f}s")
            elif f["valid"] and f["source"] == "uwb_estimate":
                feedback = level("warn", "UWB 估算速度", "不是 SportState 实测速度")
            else:
                feedback = level("bad", "无实际速度反馈", f"source={f['source']}")

            perf = sample["performance"]
            infer = perf["inference_ms"] or 0.0
            loop = perf["loop_ms"] or 0.0
            if perf["consecutive_errors"] > 0:
                inference = level("bad", "推理异常", f"连续错误 {perf['consecutive_errors']}")
            elif infer > float(self.config["inference_warn_ms"]) or loop > float(self.config["loop_warn_ms"]):
                inference = level("warn", "性能接近上限", f"infer={infer:.2f}ms loop={loop:.2f}ms")
            else:
                inference = level("good", "推理正常", f"infer={infer:.2f}ms loop={loop:.2f}ms")

        speed_warning = False
        if sample and fresh and sample["feedback"]["valid"]:
            f = sample["feedback"]
            speed_warning = max(abs(f["err_vx"] or 0), abs(f["err_vy"] or 0)) > float(self.config["speed_error_warn"])
        return {
            "server_time": now,
            "csv": {
                "path": str(self.csv_path) if self.csv_path else None,
                "mtime": self.csv_mtime or None,
                "age_s": csv_age,
                "fresh": fresh,
            },
            "controller": controller,
            "vision": vision,
            "uwb": uwb,
            "camera": camera,
            "feedback": feedback,
            "inference": inference,
            "speed_warning": speed_warning,
            "process_matches": self.process.matches,
        }

    def _publish(self, samples: list[dict[str, Any]] | None = None) -> None:
        with self.condition:
            self.revision += 1
            self.event = {
                "revision": self.revision,
                "status": self._health(time.time()),
                "latest": self.latest,
                "samples": samples or [],
            }
            self.condition.notify_all()

    def _run(self) -> None:
        poll = max(0.05, float(self.config["poll_interval_s"]))
        process_interval = max(0.2, float(self.config["process_interval_s"]))
        while not self.stop_event.is_set():
            now = time.time()
            path, rows, switched = self.tailer.read()
            if switched:
                self.depth_signature = None
                self.depth_same_frames = 0
                self.latest = None
                self.history.clear()
            self.csv_path = path
            if path:
                try:
                    self.csv_mtime = path.stat().st_mtime
                except OSError:
                    pass
            else:
                self.csv_mtime = 0.0
            samples: list[dict[str, Any]] = []
            for row in rows[-500:]:
                sample = self._normalize(row, path) if path else None
                if sample:
                    self.history.append(sample)
                    self.latest = sample
                    samples.append(sample)
            process_changed = False
            if now - self.last_process_check >= process_interval:
                old_running = self.process.running
                old_matches = self.process.matches
                self.process = inspect_processes(list(self.config["process_patterns"]))
                self.last_process_check = now
                process_changed = old_running != self.process.running or old_matches != self.process.matches
            if samples or switched or process_changed or now - self.event.get("status", {}).get("server_time", 0) >= 1.0:
                self._publish(samples)
            self.stop_event.wait(poll)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "revision": self.revision,
                "status": self._health(time.time()),
                "latest": self.latest,
                "history": list(self.history),
                "config": {
                    "csv_stale_s": self.config["csv_stale_s"],
                    "poll_interval_s": self.config["poll_interval_s"],
                },
            }

    def wait_event(self, after: int, timeout: float = 15.0) -> tuple[int, dict[str, Any] | None]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.revision <= after and not self.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.revision, None
                self.condition.wait(remaining)
            return self.revision, dict(self.event) if self.event else None

    def logs(self, requested_limit: int) -> dict[str, Any]:
        limit = min(max(10, requested_limit), 500)
        path = newest_file(list(self.config["text_log_globs"]))
        if not path:
            return {"path": None, "lines": [], "message": "未找到文本运行日志；CSV 遥测仍可正常监控。"}
        return {"path": str(path), "lines": tail_text(path, limit), "message": ""}


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], monitor: RuntimeMonitor) -> None:
        self.monitor = monitor
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "Go2RuntimeMonitor/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def monitor(self) -> RuntimeMonitor:
        return self.server.monitor  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

    def _json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        raw = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _events(self, query: dict[str, list[str]]) -> None:
        after = integer(query.get("after", ["0"])[-1], 0)
        after = max(after, integer(self.headers.get("Last-Event-ID"), 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while not self.monitor.stop_event.is_set():
                revision, event = self.monitor.wait_event(after, 15.0)
                if event:
                    raw = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"id: {revision}\ndata: {raw}\n\n".encode("utf-8"))
                    after = revision
                else:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/status":
            self._json(self.monitor.snapshot())
        elif parsed.path == "/api/logs":
            self._json(self.monitor.logs(integer(query.get("limit", ["120"])[-1], 120)))
        elif parsed.path == "/api/events":
            self._events(query)
        elif parsed.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
        else:
            self._static(parsed.path)


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {"csv_globs", "text_log_globs", "process_patterns"}
    missing = required - config.keys()
    if missing:
        raise ValueError(f"配置缺少字段: {', '.join(sorted(missing))}")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Go2 Linux runtime web monitor")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    config = load_config(Path(args.config).expanduser().resolve())
    host = args.host or str(config.get("host", "0.0.0.0"))
    port = args.port or int(config.get("port", 8766))
    monitor = RuntimeMonitor(config)
    server = MonitorServer((host, port), monitor)
    monitor.start()
    print(f"Go2 runtime monitor listening on http://{host}:{port}")
    print("Open from Windows: http://<GO2-IP>:%d" % port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.shutdown()
        monitor.stop()
        server.server_close()


if __name__ == "__main__":
    main()
