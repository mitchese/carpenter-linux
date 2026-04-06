#!/usr/bin/env python3
"""Carpenter plugin watcher — standalone host-side process.

Polls a shared folder for trigger files, validates checksums, runs commands,
and writes results back. No Carpenter imports — stdlib only.

Usage:
    python3 watcher.py /path/to/watcher_config.json
"""

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("carpenter-watcher")

# Sentinel for shutdown
_shutdown = threading.Event()


def load_config(config_path: str) -> dict:
    """Load and validate watcher configuration from JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(path) as f:
        config = json.load(f)

    required = ["shared_folder", "command"]
    for key in required:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")

    if not isinstance(config["command"], list) or not config["command"]:
        raise ValueError("'command' must be a non-empty list")

    if config.get("prompt_mode", "stdin") not in ("stdin", "file", "arg"):
        raise ValueError("'prompt_mode' must be 'stdin', 'file', or 'arg'")

    # Apply defaults
    config.setdefault("prompt_mode", "stdin")
    config.setdefault("heartbeat_interval", 10)
    config.setdefault("poll_interval", 1)
    config.setdefault("timeout_seconds", 600)
    config.setdefault("log_level", "INFO")

    return config


def parse_trigger_filename(filename: str) -> tuple[str, str] | None:
    """Parse a trigger filename into (task_id, checksum).

    Format: {task_id}-{checksum}.trigger
    The checksum is always the last 8 hex chars before .trigger.
    Returns None if the filename doesn't match the expected format.
    """
    if not filename.endswith(".trigger"):
        return None

    stem = filename[:-len(".trigger")]
    # Checksum is always 8 hex chars at the end, separated by '-'
    if len(stem) < 10 or stem[-9] != "-":
        return None

    checksum = stem[-8:]
    task_id = stem[:-9]

    # Validate checksum is hex
    try:
        int(checksum, 16)
    except ValueError:
        return None

    if not task_id:
        return None

    return task_id, checksum


def validate_checksum(shared_folder: Path, task_id: str,
                      expected_checksum: str,
                      max_retries: int = 5,
                      retry_delay: float = 0.2) -> bool:
    """Validate trigger checksum against config.json.

    Retries up to max_retries times with retry_delay between attempts
    to handle filesystem sync delays.
    """
    config_path = shared_folder / task_id / "config.json"

    for attempt in range(max_retries):
        if _shutdown.is_set():
            return False

        if not config_path.exists():
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return False

        try:
            config_bytes = config_path.read_bytes()
            actual_checksum = hashlib.sha256(config_bytes).hexdigest()[:8]
            if actual_checksum == expected_checksum:
                return True
        except OSError:
            pass

        if attempt < max_retries - 1:
            time.sleep(retry_delay)

    return False


def _write_and_sync(path: Path, content: str) -> None:
    """Write a file and fsync for durability."""
    with open(path, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


def _sync_directory(dir_path: Path) -> None:
    """fsync a directory to ensure new entries are durable."""
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass


def write_error_result(shared_folder: Path, task_id: str,
                       error: str) -> None:
    """Write an error result for a task that failed validation."""
    task_dir = shared_folder / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "task_id": task_id,
        "status": "failed",
        "exit_code": 1,
        "duration_seconds": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
    }

    _write_and_sync(task_dir / "result.json", json.dumps(result, indent=2))
    _write_and_sync(task_dir / "output.txt", "")

    # Touch .done last, after fsync
    completed_dir = shared_folder / "completed"
    completed_dir.mkdir(exist_ok=True)
    done_path = completed_dir / f"{task_id}.done"
    done_path.touch()
    _sync_directory(completed_dir)

    logger.info("Wrote error result for task %s: %s", task_id, error)


def run_task(shared_folder: Path, task_id: str, config: dict) -> None:
    """Execute the command for a task and write results.

    Runs in its own thread. Reads prompt.txt from the task directory
    and executes the configured command with the appropriate prompt_mode.
    """
    task_dir = shared_folder / task_id
    prompt_path = task_dir / "prompt.txt"
    config_path = task_dir / "config.json"

    started_at = datetime.now(timezone.utc)

    try:
        # Read task config for timeout and workspace
        task_config = {}
        if config_path.exists():
            with open(config_path) as f:
                task_config = json.load(f)

        timeout = task_config.get("timeout_seconds", config["timeout_seconds"])
        workspace = task_config.get("working_directory", str(task_dir / "workspace"))

        # Read prompt
        prompt_text = ""
        if prompt_path.exists():
            prompt_text = prompt_path.read_text()

        # Build command based on prompt_mode
        cmd = list(config["command"])
        stdin_data = None

        prompt_mode = config["prompt_mode"]
        if prompt_mode == "stdin":
            stdin_data = prompt_text
        elif prompt_mode == "file":
            cmd.append(str(prompt_path))
        elif prompt_mode == "arg":
            cmd.extend(["--prompt", str(prompt_path)])

        # Set up environment
        env = os.environ.copy()
        env["CARPENTER_TASK_ID"] = task_id
        env["CARPENTER_WORKSPACE"] = workspace
        env["CARPENTER_TASK_DIR"] = str(task_dir)

        # Open stream.log for real-time stderr capture
        stream_log_path = task_dir / "stream.log"

        logger.info("Running task %s: %s", task_id, " ".join(cmd))

        with open(stream_log_path, "w") as stream_log:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stream_log,
                env=env,
                cwd=workspace if Path(workspace).is_dir() else None,
            )

            stdout_data, _ = proc.communicate(
                input=stdin_data.encode() if stdin_data is not None else None,
                timeout=timeout,
            )

        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - started_at).total_seconds()

        # Write output.txt
        _write_and_sync(task_dir / "output.txt", stdout_data.decode(errors="replace"))

        # Write result.json
        status = "completed" if proc.returncode == 0 else "failed"
        result = {
            "task_id": task_id,
            "status": status,
            "exit_code": proc.returncode,
            "duration_seconds": round(duration, 2),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "error": None if proc.returncode == 0 else f"Process exited with code {proc.returncode}",
        }
        _write_and_sync(task_dir / "result.json", json.dumps(result, indent=2))

        logger.info("Task %s %s (exit_code=%d, %.1fs)",
                     task_id, status, proc.returncode, duration)

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - started_at).total_seconds()

        _write_and_sync(task_dir / "output.txt", "")
        result = {
            "task_id": task_id,
            "status": "failed",
            "exit_code": -1,
            "duration_seconds": round(duration, 2),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "error": f"Command timed out after {timeout} seconds",
        }
        _write_and_sync(task_dir / "result.json", json.dumps(result, indent=2))
        logger.warning("Task %s timed out after %ds", task_id, timeout)

    except Exception as e:
        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - started_at).total_seconds()

        _write_and_sync(task_dir / "output.txt", "")
        result = {
            "task_id": task_id,
            "status": "failed",
            "exit_code": -1,
            "duration_seconds": round(duration, 2),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "error": str(e),
        }
        _write_and_sync(task_dir / "result.json", json.dumps(result, indent=2))
        logger.exception("Task %s failed with exception", task_id)

    # Touch .done last, after all files are fsynced
    completed_dir = shared_folder / "completed"
    completed_dir.mkdir(exist_ok=True)
    done_path = completed_dir / f"{task_id}.done"
    done_path.touch()
    _sync_directory(completed_dir)


class HeartbeatWriter:
    """Daemon thread that writes heartbeat.json at regular intervals."""

    def __init__(self, shared_folder: Path, interval: float = 10):
        self.shared_folder = shared_folder
        self.interval = interval
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not _shutdown.is_set():
            self.write_heartbeat()
            _shutdown.wait(self.interval)

    def write_heartbeat(self) -> None:
        """Write a single heartbeat."""
        heartbeat = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        try:
            _write_and_sync(
                self.shared_folder / "heartbeat.json",
                json.dumps(heartbeat, indent=2),
            )
        except OSError:
            logger.exception("Failed to write heartbeat")


class PluginWatcher:
    """Main watcher loop — polls triggered/ and spawns TaskRunners."""

    def __init__(self, config: dict):
        self.config = config
        self.shared_folder = Path(config["shared_folder"])
        self.poll_interval = config["poll_interval"]
        self.heartbeat = HeartbeatWriter(
            self.shared_folder,
            config["heartbeat_interval"],
        )
        self._active_tasks: set[str] = set()
        self._lock = threading.Lock()

    def run(self) -> None:
        """Main loop — ensure structure, start heartbeat, poll for triggers."""
        self._ensure_structure()
        self.heartbeat.start()

        logger.info("Watcher started — polling %s every %ds",
                     self.shared_folder / "triggered", self.poll_interval)

        while not _shutdown.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("Error during poll cycle")
            _shutdown.wait(self.poll_interval)

        logger.info("Watcher shutting down")

    def _ensure_structure(self) -> None:
        """Create shared folder structure if it doesn't exist."""
        self.shared_folder.mkdir(parents=True, exist_ok=True)
        (self.shared_folder / "triggered").mkdir(exist_ok=True)
        (self.shared_folder / "completed").mkdir(exist_ok=True)

    def _poll_once(self) -> None:
        """Check triggered/ for new trigger files and process them."""
        triggered_dir = self.shared_folder / "triggered"
        if not triggered_dir.exists():
            return

        try:
            entries = os.listdir(triggered_dir)
        except OSError:
            return

        for filename in entries:
            if _shutdown.is_set():
                break

            parsed = parse_trigger_filename(filename)
            if parsed is None:
                logger.warning("Ignoring malformed trigger file: %s", filename)
                continue

            task_id, checksum = parsed

            # Skip if already processing
            with self._lock:
                if task_id in self._active_tasks:
                    continue
                self._active_tasks.add(task_id)

            # Remove trigger file immediately to prevent re-processing
            trigger_path = triggered_dir / filename
            try:
                trigger_path.unlink()
            except FileNotFoundError:
                # Another instance might have taken it
                with self._lock:
                    self._active_tasks.discard(task_id)
                continue

            # Validate checksum
            if not validate_checksum(self.shared_folder, task_id, checksum):
                logger.error("Checksum mismatch for task %s (expected %s)",
                             task_id, checksum)
                write_error_result(
                    self.shared_folder, task_id,
                    f"Checksum mismatch: expected {checksum}",
                )
                with self._lock:
                    self._active_tasks.discard(task_id)
                continue

            # Spawn task thread
            thread = threading.Thread(
                target=self._run_task,
                args=(task_id,),
                daemon=True,
            )
            thread.start()

    def _run_task(self, task_id: str) -> None:
        """Wrapper that runs a task and cleans up active_tasks."""
        try:
            run_task(self.shared_folder, task_id, self.config)
        finally:
            with self._lock:
                self._active_tasks.discard(task_id)


def setup_signal_handlers() -> None:
    """Install SIGTERM and SIGINT handlers for graceful shutdown."""
    def handler(signum, frame):
        signame = signal.Signals(signum).name
        logger.info("Received %s, initiating shutdown", signame)
        _shutdown.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main(argv: list[str] | None = None) -> int:
    """Entry point — load config and run watcher."""
    args = argv if argv is not None else sys.argv[1:]

    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} <config_path>", file=sys.stderr)
        return 1

    config_path = args[0]

    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, config["log_level"].upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    setup_signal_handlers()

    watcher = PluginWatcher(config)
    watcher.run()

    return 0


if __name__ == "__main__":
    sys.exit(main())
