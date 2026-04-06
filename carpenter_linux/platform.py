"""Linux platform implementation.

Provides Linux-specific behaviour: os.execv restart, chmod 0o600 file
protection, systemd service generation, and SIGTERM→SIGKILL kill
escalation.
"""

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


class LinuxPlatform:
    """Platform implementation for Linux (and compatible Unix systems)."""

    name = "linux"

    def restart_process(self) -> None:
        """Replace the current process with a fresh copy via os.execv."""
        logger.info("Restarting platform now (os.execv)")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def protect_file(self, path: str) -> None:
        """Set file permissions to owner-read-write only (0o600)."""
        os.chmod(path, 0o600)

    def generate_service(self, name: str, command: list[str],
                         description: str, *, working_dir: str = "",
                         env_file: str = "") -> str | None:
        """Generate a systemd user service unit file."""
        exec_start = " ".join(command)
        lines = [
            "[Unit]",
            f"Description={description}",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_start}",
            "Restart=on-failure",
            "RestartSec=5",
            "Environment=PYTHONUNBUFFERED=1",
        ]
        if working_dir:
            lines.append(f"WorkingDirectory={working_dir}")
        if env_file:
            lines.append(f"EnvironmentFile={env_file}")
        lines.extend([
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])
        return "\n".join(lines)

    def install_service(self, name: str, service_content: str) -> bool:
        """Install a systemd user service and reload the daemon."""
        from pathlib import Path
        systemd_dir = Path.home() / ".config" / "systemd" / "user"
        systemd_dir.mkdir(parents=True, exist_ok=True)
        service_path = systemd_dir / f"{name}.service"
        service_path.write_text(service_content)

        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True, timeout=10,
            )
            return True
        except (subprocess.SubprocessError, FileNotFoundError):
            logger.warning("systemctl daemon-reload failed (non-fatal)")
            return True  # service file was written even if reload failed

    def graceful_kill(self, proc, grace_seconds: int = 5) -> None:
        """SIGTERM → wait → SIGKILL escalation."""
        try:
            proc.terminate()  # SIGTERM
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()  # SIGKILL
                proc.wait()
        except (OSError, ProcessLookupError):
            pass  # Already exited
