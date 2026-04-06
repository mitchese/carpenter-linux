"""Landlock sandbox — uses kernel Landlock LSM via a Python helper process."""

import os
import sys


def build_command(inner_cmd: list[str], write_dirs: list[str]) -> list[str]:
    """Wrap a command with Landlock-based filesystem sandboxing.

    Args:
        inner_cmd: The command to run (e.g. ["python3", "script.py"]).
        write_dirs: Absolute paths that should remain writable.

    Returns:
        Command list that invokes the Landlock helper then exec's inner_cmd.

    Raises:
        ValueError: If any write_dir is not an absolute path.
    """
    rw_args = _build_rw_args(write_dirs)

    return [
        sys.executable, "-m", "carpenter_linux.sandbox._landlock_helper",
        *rw_args,
        "--", *inner_cmd,
    ]


def build_shell_command(shell_cmd: str, cwd: str, write_dirs: list[str]) -> list[str]:
    """Wrap a shell command for Landlock-sandboxed execution.

    Args:
        shell_cmd: Shell command string to execute.
        cwd: Working directory for the command.
        write_dirs: Absolute paths that should remain writable.

    Returns:
        Command list that invokes the Landlock helper then runs bash.

    Raises:
        ValueError: If cwd or any write_dir is not an absolute path.
    """
    if not os.path.isabs(cwd):
        raise ValueError(f"cwd must be absolute: {cwd}")

    rw_args = _build_rw_args(write_dirs)

    # The helper applies restrictions then execs the command
    escaped_cwd = cwd.replace("'", "'\\''")
    escaped_cmd = shell_cmd.replace("'", "'\\''")
    inner_shell = f"cd '{escaped_cwd}' && exec bash -c '{escaped_cmd}'"

    return [
        sys.executable, "-m", "carpenter_linux.sandbox._landlock_helper",
        *rw_args,
        "--", "bash", "-c", inner_shell,
    ]


def _build_rw_args(write_dirs: list[str]) -> list[str]:
    """Build --rw arguments for the Landlock helper.

    Validates paths are absolute, filters non-existent directories.
    """
    for d in write_dirs:
        if not os.path.isabs(d):
            raise ValueError(f"write_dir must be absolute: {d}")

    existing_dirs = [d for d in write_dirs if os.path.isdir(d)]

    args = []
    for d in existing_dirs:
        args.extend(["--rw", d])
    return args
