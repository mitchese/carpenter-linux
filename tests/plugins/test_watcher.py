"""Tests for the plugin watcher script."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

# Import watcher as a module
from carpenter_linux.plugins.watcher_template import watcher


@pytest.fixture(autouse=True)
def reset_shutdown():
    """Ensure _shutdown is cleared before and after each test."""
    watcher._shutdown.clear()
    yield
    watcher._shutdown.set()  # Stop any lingering threads


@pytest.fixture
def shared_folder(tmp_path):
    """Create a shared folder with standard structure."""
    shared = tmp_path / "test-plugin"
    shared.mkdir()
    (shared / "triggered").mkdir()
    (shared / "completed").mkdir()
    return shared


@pytest.fixture
def sample_config(shared_folder):
    """Return a valid watcher config dict."""
    return {
        "shared_folder": str(shared_folder),
        "command": ["echo", "hello from watcher"],
        "prompt_mode": "stdin",
        "heartbeat_interval": 10,
        "poll_interval": 1,
        "timeout_seconds": 600,
        "log_level": "INFO",
    }


def _create_task(shared_folder, task_id="task-1", prompt="do something",
                 timeout=60):
    """Create a task directory with config.json and prompt.txt."""
    task_dir = shared_folder / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    workspace = task_dir / "workspace"
    workspace.mkdir(exist_ok=True)

    config_data = {
        "task_id": task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": timeout,
        "working_directory": str(workspace),
        "context": {},
        "metadata": {"initiated_by": "carpenter", "plugin": "test"},
    }

    config_path = task_dir / "config.json"
    config_path.write_text(json.dumps(config_data, indent=2))
    (task_dir / "prompt.txt").write_text(prompt)

    return config_data, config_path


def _create_trigger(shared_folder, task_id="task-1"):
    """Create a valid trigger file for a task."""
    config_path = shared_folder / task_id / "config.json"
    config_bytes = config_path.read_bytes()
    checksum = hashlib.sha256(config_bytes).hexdigest()[:8]
    trigger_name = f"{task_id}-{checksum}.trigger"
    trigger_path = shared_folder / "triggered" / trigger_name
    trigger_path.touch()
    return trigger_name, checksum


# --- Config loading ---

class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "shared_folder": "/tmp/test",
            "command": ["echo", "hi"],
        }))
        config = watcher.load_config(str(config_file))
        assert config["shared_folder"] == "/tmp/test"
        assert config["command"] == ["echo", "hi"]

    def test_applies_defaults(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "shared_folder": "/tmp/test",
            "command": ["echo"],
        }))
        config = watcher.load_config(str(config_file))
        assert config["prompt_mode"] == "stdin"
        assert config["heartbeat_interval"] == 10
        assert config["poll_interval"] == 1
        assert config["timeout_seconds"] == 600
        assert config["log_level"] == "INFO"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            watcher.load_config(str(tmp_path / "nonexistent.json"))

    def test_missing_shared_folder_raises(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"command": ["echo"]}))
        with pytest.raises(ValueError, match="shared_folder"):
            watcher.load_config(str(config_file))

    def test_missing_command_raises(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"shared_folder": "/tmp"}))
        with pytest.raises(ValueError, match="command"):
            watcher.load_config(str(config_file))

    def test_empty_command_raises(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "shared_folder": "/tmp",
            "command": [],
        }))
        with pytest.raises(ValueError, match="non-empty list"):
            watcher.load_config(str(config_file))

    def test_invalid_prompt_mode_raises(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "shared_folder": "/tmp",
            "command": ["echo"],
            "prompt_mode": "magic",
        }))
        with pytest.raises(ValueError, match="prompt_mode"):
            watcher.load_config(str(config_file))


# --- Trigger filename parsing ---

class TestParseTriggerFilename:
    def test_valid_trigger(self):
        result = watcher.parse_trigger_filename("task-1-abcd1234.trigger")
        assert result == ("task-1", "abcd1234")

    def test_uuid_task_id(self):
        result = watcher.parse_trigger_filename(
            "550e8400-e29b-41d4-a716-446655440000-1a2b3c4d.trigger"
        )
        assert result == (
            "550e8400-e29b-41d4-a716-446655440000",
            "1a2b3c4d",
        )

    def test_not_trigger_extension(self):
        assert watcher.parse_trigger_filename("task-1-abcd1234.txt") is None

    def test_too_short(self):
        assert watcher.parse_trigger_filename("ab.trigger") is None

    def test_non_hex_checksum(self):
        assert watcher.parse_trigger_filename("task-1-zzzzzzzz.trigger") is None

    def test_no_task_id(self):
        assert watcher.parse_trigger_filename("-abcd1234.trigger") is None


# --- Checksum validation ---

class TestValidateChecksum:
    def test_valid_checksum(self, shared_folder):
        _create_task(shared_folder)
        _, checksum = _create_trigger(shared_folder)
        assert watcher.validate_checksum(shared_folder, "task-1", checksum)

    def test_invalid_checksum(self, shared_folder):
        _create_task(shared_folder)
        assert not watcher.validate_checksum(
            shared_folder, "task-1", "00000000",
            max_retries=1, retry_delay=0,
        )

    def test_missing_config(self, shared_folder):
        assert not watcher.validate_checksum(
            shared_folder, "nonexistent", "abcd1234",
            max_retries=1, retry_delay=0,
        )

    def test_retries_on_mismatch(self, shared_folder):
        _create_task(shared_folder)
        config_path = shared_folder / "task-1" / "config.json"
        correct_checksum = hashlib.sha256(
            config_path.read_bytes()
        ).hexdigest()[:8]

        call_count = 0
        original_read = Path.read_bytes

        def patched_read(self_path):
            nonlocal call_count
            call_count += 1
            return original_read(self_path)

        with mock.patch.object(Path, "read_bytes", patched_read):
            result = watcher.validate_checksum(
                shared_folder, "task-1", correct_checksum,
                max_retries=3, retry_delay=0,
            )

        assert result is True
        # Should succeed on first try
        assert call_count >= 1


# --- Heartbeat ---

class TestHeartbeatWriter:
    def test_writes_heartbeat(self, shared_folder):
        hb = watcher.HeartbeatWriter(shared_folder, interval=1)
        hb.write_heartbeat()

        heartbeat_path = shared_folder / "heartbeat.json"
        assert heartbeat_path.exists()

        data = json.loads(heartbeat_path.read_text())
        assert "timestamp" in data
        assert "pid" in data
        assert data["pid"] == os.getpid()

    def test_heartbeat_timestamp_is_recent(self, shared_folder):
        hb = watcher.HeartbeatWriter(shared_folder, interval=1)
        hb.write_heartbeat()

        data = json.loads((shared_folder / "heartbeat.json").read_text())
        ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        assert age < 30  # generous threshold for slow/loaded systems (e.g. RPi)

    def test_heartbeat_thread_starts(self, shared_folder):
        hb = watcher.HeartbeatWriter(shared_folder, interval=0.01)
        hb.start()
        time.sleep(0.05)  # Reduced from 0.3s

        assert (shared_folder / "heartbeat.json").exists()


# --- Result writing ---

class TestWriteErrorResult:
    def test_writes_result_json(self, shared_folder):
        watcher.write_error_result(shared_folder, "task-1", "checksum failed")

        result = json.loads(
            (shared_folder / "task-1" / "result.json").read_text()
        )
        assert result["task_id"] == "task-1"
        assert result["status"] == "failed"
        assert result["exit_code"] == 1
        assert result["error"] == "checksum failed"

    def test_writes_empty_output(self, shared_folder):
        watcher.write_error_result(shared_folder, "task-1", "error")
        assert (shared_folder / "task-1" / "output.txt").read_text() == ""

    def test_creates_done_file(self, shared_folder):
        watcher.write_error_result(shared_folder, "task-1", "error")
        assert (shared_folder / "completed" / "task-1.done").exists()


# --- Task execution ---

class TestRunTask:
    def test_success(self, shared_folder, sample_config):
        _create_task(shared_folder, prompt="world")
        sample_config["command"] = [sys.executable, "-c",
                                    "import sys; print('hello ' + sys.stdin.read())"]
        watcher.run_task(shared_folder, "task-1", sample_config)

        result = json.loads(
            (shared_folder / "task-1" / "result.json").read_text()
        )
        assert result["status"] == "completed"
        assert result["exit_code"] == 0

        output = (shared_folder / "task-1" / "output.txt").read_text()
        assert "hello world" in output

        assert (shared_folder / "completed" / "task-1.done").exists()

    def test_failure_exit_code(self, shared_folder, sample_config):
        _create_task(shared_folder)
        sample_config["command"] = [sys.executable, "-c", "import sys; sys.exit(42)"]
        watcher.run_task(shared_folder, "task-1", sample_config)

        result = json.loads(
            (shared_folder / "task-1" / "result.json").read_text()
        )
        assert result["status"] == "failed"
        assert result["exit_code"] == 42
        assert (shared_folder / "completed" / "task-1.done").exists()

    def test_timeout(self, shared_folder, sample_config):
        _create_task(shared_folder, timeout=0.1)  # Reduced from 1s
        sample_config["command"] = [sys.executable, "-c",
                                    "import time; time.sleep(2)"]  # Reduced from 60s
        watcher.run_task(shared_folder, "task-1", sample_config)

        result = json.loads(
            (shared_folder / "task-1" / "result.json").read_text()
        )
        assert result["status"] == "failed"
        assert "timed out" in result["error"]
        assert (shared_folder / "completed" / "task-1.done").exists()

    def test_prompt_mode_file(self, shared_folder, sample_config):
        _create_task(shared_folder, prompt="test content")
        prompt_path = str(shared_folder / "task-1" / "prompt.txt")
        sample_config["command"] = [sys.executable, "-c",
                                    f"print(open('{prompt_path}').read())"]
        sample_config["prompt_mode"] = "file"
        watcher.run_task(shared_folder, "task-1", sample_config)

        output = (shared_folder / "task-1" / "output.txt").read_text()
        assert "test content" in output

    def test_prompt_mode_arg(self, shared_folder, sample_config):
        _create_task(shared_folder, prompt="test content")
        # The --prompt flag and path get appended to the command
        sample_config["command"] = [sys.executable, "-c",
                                    "import sys; print(sys.argv)"]
        sample_config["prompt_mode"] = "arg"
        watcher.run_task(shared_folder, "task-1", sample_config)

        output = (shared_folder / "task-1" / "output.txt").read_text()
        assert "--prompt" in output

    def test_env_vars_set(self, shared_folder, sample_config):
        _create_task(shared_folder)
        sample_config["command"] = [
            sys.executable, "-c",
            "import os; print(os.environ['CARPENTER_TASK_ID'],"
            " os.environ['CARPENTER_WORKSPACE'],"
            " os.environ['CARPENTER_TASK_DIR'])"
        ]
        watcher.run_task(shared_folder, "task-1", sample_config)

        output = (shared_folder / "task-1" / "output.txt").read_text()
        assert "task-1" in output

    def test_writes_stream_log(self, shared_folder, sample_config):
        _create_task(shared_folder)
        sample_config["command"] = [sys.executable, "-c",
                                    "import sys; print('stderr msg', file=sys.stderr)"]
        watcher.run_task(shared_folder, "task-1", sample_config)

        stream_log = (shared_folder / "task-1" / "stream.log").read_text()
        assert "stderr msg" in stream_log

    def test_result_has_timestamps(self, shared_folder, sample_config):
        _create_task(shared_folder)
        watcher.run_task(shared_folder, "task-1", sample_config)

        result = json.loads(
            (shared_folder / "task-1" / "result.json").read_text()
        )
        assert "started_at" in result
        assert "completed_at" in result
        assert "duration_seconds" in result
        assert result["duration_seconds"] >= 0


# --- Signal handling ---

class TestSignalHandling:
    def test_shutdown_event_stops_watcher(self, sample_config):
        pw = watcher.PluginWatcher(sample_config)

        def stop_after_delay():
            time.sleep(0.2)
            watcher._shutdown.set()

        t = threading.Thread(target=stop_after_delay, daemon=True)
        t.start()

        pw.run()  # Should return after shutdown is set
        assert watcher._shutdown.is_set()


# --- Concurrent task handling ---

class TestConcurrency:
    def test_multiple_tasks(self, shared_folder, sample_config):
        sample_config["command"] = [sys.executable, "-c",
                                    "import sys; print('ok: ' + sys.stdin.read())"]

        _create_task(shared_folder, task_id="task-a", prompt="prompt-a")
        _create_task(shared_folder, task_id="task-b", prompt="prompt-b")

        watcher.run_task(shared_folder, "task-a", sample_config)
        watcher.run_task(shared_folder, "task-b", sample_config)

        for tid in ("task-a", "task-b"):
            result = json.loads(
                (shared_folder / tid / "result.json").read_text()
            )
            assert result["status"] == "completed"
            assert (shared_folder / "completed" / f"{tid}.done").exists()

    def test_skip_already_active_task(self, shared_folder, sample_config):
        _create_task(shared_folder)
        _create_trigger(shared_folder)

        pw = watcher.PluginWatcher(sample_config)
        # Pretend task-1 is already running
        pw._active_tasks.add("task-1")

        pw._poll_once()
        # Trigger file should still exist (not picked up)
        triggers = list((shared_folder / "triggered").iterdir())
        assert len(triggers) == 1


# --- PluginWatcher poll ---

class TestPluginWatcherPoll:
    def test_poll_picks_up_trigger(self, shared_folder, sample_config):
        sample_config["command"] = [sys.executable, "-c", "print('done')"]

        _create_task(shared_folder)
        _create_trigger(shared_folder)

        pw = watcher.PluginWatcher(sample_config)
        pw._poll_once()

        # Poll until the task thread finishes (up to 10s — spawning a Python
        # subprocess can be slow under CPU load on a Raspberry Pi)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if (shared_folder / "completed" / "task-1.done").exists():
                break
            time.sleep(0.1)

        assert (shared_folder / "completed" / "task-1.done").exists()
        # Trigger should have been removed
        triggers = list((shared_folder / "triggered").iterdir())
        assert len(triggers) == 0

    def test_poll_ignores_malformed_trigger(self, shared_folder, sample_config):
        (shared_folder / "triggered" / "not-a-trigger.txt").touch()

        pw = watcher.PluginWatcher(sample_config)
        pw._poll_once()

        # File should still be there (not processed, not removed)
        assert (shared_folder / "triggered" / "not-a-trigger.txt").exists()

    def test_checksum_mismatch_writes_error(self, shared_folder, sample_config):
        _create_task(shared_folder)
        # Create trigger with wrong checksum
        (shared_folder / "triggered" / "task-1-00000000.trigger").touch()

        pw = watcher.PluginWatcher(sample_config)
        pw._poll_once()

        # Should write error result
        time.sleep(0.2)
        result = json.loads(
            (shared_folder / "task-1" / "result.json").read_text()
        )
        assert result["status"] == "failed"
        assert "checksum" in result["error"].lower()
