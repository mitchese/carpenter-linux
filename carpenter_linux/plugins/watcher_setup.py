"""Watcher setup file generator.

Generates watcher files for a plugin — copies the watcher script,
creates a config JSON, and writes a systemd service file to a
target directory.
"""

import importlib.resources
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Files to copy verbatim from the watcher_template package
_TEMPLATE_FILES = ["watcher.py"]

# Systemd service template filename
_SERVICE_TEMPLATE = "carpenter-plugin-watcher@.service"


def generate_watcher_setup(
    plugin_name: str,
    shared_folder: str,
    target_dir: str,
    command: list[str] | None = None,
    prompt_mode: str = "stdin",
) -> dict:
    """Generate watcher files for a plugin.

    Copies watcher.py, generates watcher_config.json and systemd service
    file to target_dir. Returns dict of generated file paths.

    Args:
        plugin_name: Name of the plugin (used in service file).
        shared_folder: Absolute path to the shared plugin folder.
        target_dir: Directory to write generated files into.
        command: Command to run as a list. Defaults to an echo placeholder.
        prompt_mode: How to pass the prompt — "stdin", "file", or "arg".

    Returns:
        Dict mapping file type to absolute path of generated file.

    Raises:
        ValueError: If prompt_mode is invalid.
        FileNotFoundError: If template files cannot be located.
    """
    if prompt_mode not in ("stdin", "file", "arg"):
        raise ValueError(f"Invalid prompt_mode: {prompt_mode!r}")

    if command is None:
        command = ["echo", "Replace this with your tool command"]

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    generated = {}

    # Locate the watcher_template package
    template_pkg = importlib.resources.files(
        "carpenter_linux.plugins.watcher_template"
    )

    # Copy template files
    for filename in _TEMPLATE_FILES:
        src = template_pkg.joinpath(filename)
        dst = target / filename
        with importlib.resources.as_file(src) as src_path:
            shutil.copy2(src_path, dst)
        generated[filename] = str(dst)

    # Generate watcher_config.json
    config = {
        "shared_folder": shared_folder,
        "command": command,
        "prompt_mode": prompt_mode,
        "heartbeat_interval": 10,
        "poll_interval": 1,
        "timeout_seconds": 600,
        "log_level": "INFO",
    }
    config_path = target / "watcher_config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    generated["watcher_config.json"] = str(config_path)

    # Copy systemd service file
    service_src = template_pkg.joinpath(_SERVICE_TEMPLATE)
    service_dst = target / _SERVICE_TEMPLATE
    with importlib.resources.as_file(service_src) as src_path:
        shutil.copy2(src_path, service_dst)
    generated[_SERVICE_TEMPLATE] = str(service_dst)

    logger.info("Generated watcher files for plugin %s in %s",
                plugin_name, target_dir)

    return generated
