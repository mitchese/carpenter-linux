"""Bubblewrap sandbox — uses bwrap for lightweight containerization."""

import os
import shlex


def build_command(inner_cmd: list[str], write_dirs: list[str]) -> list[str]:
    """Wrap a command with bwrap-based filesystem sandboxing.

    Args:
        inner_cmd: The command to run (e.g. ["python3", "script.py"]).
        write_dirs: Absolute paths that should remain writable.

    Returns:
        Command list with bwrap prefix.

    Raises:
        ValueError: If any write_dir is not an absolute path.
    """
    return _build_bwrap(write_dirs) + ["--"] + inner_cmd


def build_shell_command(shell_cmd: str, cwd: str, write_dirs: list[str]) -> list[str]:
    """Wrap a shell command for sandboxed execution via bwrap.

    Args:
        shell_cmd: Shell command string to execute.
        cwd: Working directory for the command.
        write_dirs: Absolute paths that should remain writable.

    Returns:
        Command list with bwrap prefix.

    Raises:
        ValueError: If cwd or any write_dir is not an absolute path.
    """
    if not os.path.isabs(cwd):
        raise ValueError(f"cwd must be absolute: {cwd}")

    return _build_bwrap(write_dirs, chdir=cwd) + ["--", "bash", "-c", shell_cmd]


def _build_bwrap(write_dirs: list[str], chdir: str | None = None) -> list[str]:
    """Build the bwrap argument list.

    Binds the entire filesystem read-only, then overlays writable
    bind-mounts for specified directories.
    """
    for d in write_dirs:
        if not os.path.isabs(d):
            raise ValueError(f"write_dir must be absolute: {d}")

    # Filter to existing directories
    existing_dirs = [d for d in write_dirs if os.path.isdir(d)]

    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]

    for d in existing_dirs:
        cmd.extend(["--bind", d, d])

    if chdir:
        cmd.extend(["--chdir", chdir])

    return cmd
