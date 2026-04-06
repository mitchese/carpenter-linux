"""Tests for the watcher setup file generator."""

import json
from pathlib import Path

import pytest

from carpenter_linux.plugins.watcher_setup import generate_watcher_setup


class TestGenerateWatcherSetup:
    def test_returns_generated_paths(self, tmp_path):
        result = generate_watcher_setup(
            "test-plugin", "/tmp/shared", str(tmp_path),
        )
        assert "watcher.py" in result
        assert "watcher_config.json" in result
        assert "carpenter-plugin-watcher@.service" in result

    def test_copies_watcher_script(self, tmp_path):
        generate_watcher_setup("test-plugin", "/tmp/shared", str(tmp_path))
        watcher_path = tmp_path / "watcher.py"
        assert watcher_path.exists()
        content = watcher_path.read_text()
        assert "PluginWatcher" in content
        assert "HeartbeatWriter" in content

    def test_generates_valid_config(self, tmp_path):
        generate_watcher_setup(
            "test-plugin", "/tmp/shared", str(tmp_path),
            command=["echo", "test"],
            prompt_mode="file",
        )
        config_path = tmp_path / "watcher_config.json"
        assert config_path.exists()

        config = json.loads(config_path.read_text())
        assert config["shared_folder"] == "/tmp/shared"
        assert config["command"] == ["echo", "test"]
        assert config["prompt_mode"] == "file"
        assert config["heartbeat_interval"] == 10
        assert config["poll_interval"] == 1
        assert config["timeout_seconds"] == 600

    def test_default_command(self, tmp_path):
        generate_watcher_setup("test-plugin", "/tmp/shared", str(tmp_path))
        config = json.loads((tmp_path / "watcher_config.json").read_text())
        assert isinstance(config["command"], list)
        assert len(config["command"]) > 0

    def test_copies_service_file(self, tmp_path):
        generate_watcher_setup("test-plugin", "/tmp/shared", str(tmp_path))
        service_path = tmp_path / "carpenter-plugin-watcher@.service"
        assert service_path.exists()
        content = service_path.read_text()
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content
        assert "%i" in content

    def test_creates_target_dir(self, tmp_path):
        target = tmp_path / "nested" / "dir"
        generate_watcher_setup("test-plugin", "/tmp/shared", str(target))
        assert target.exists()
        assert (target / "watcher.py").exists()

    def test_invalid_prompt_mode_raises(self, tmp_path):
        with pytest.raises(ValueError, match="prompt_mode"):
            generate_watcher_setup(
                "test-plugin", "/tmp/shared", str(tmp_path),
                prompt_mode="invalid",
            )

    def test_all_prompt_modes_accepted(self, tmp_path):
        for mode in ("stdin", "file", "arg"):
            target = tmp_path / mode
            generate_watcher_setup(
                "test-plugin", "/tmp/shared", str(target),
                prompt_mode=mode,
            )
            config = json.loads((target / "watcher_config.json").read_text())
            assert config["prompt_mode"] == mode
