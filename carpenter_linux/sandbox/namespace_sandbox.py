"""Namespace sandbox — uses Linux user+mount namespaces via unshare."""

import os
import shlex


def build_command(inner_cmd: list[str], write_dirs: list[str]) -> list[str]:
    """Wrap a command with unshare-based filesystem sandboxing.

    Args:
        inner_cmd: The command to run (e.g. ["python3", "script.py"]).
        write_dirs: Absolute paths that should remain writable.

    Returns:
        Command list with unshare prefix and mount setup.

    Raises:
        ValueError: If any write_dir is not an absolute path.
    """
    mount_script = _build_mount_script(write_dirs)
    escaped_cmd = " ".join(shlex.quote(c) for c in inner_cmd)
    bash_script = f"{mount_script}exec {escaped_cmd}"

    return [
        "unshare", "--user", "--map-root-user", "--mount",
        "bash", "-c", bash_script,
    ]


def build_shell_command(shell_cmd: str, cwd: str, write_dirs: list[str]) -> list[str]:
    """Wrap a shell command for sandboxed execution.

    Args:
        shell_cmd: Shell command string to execute.
        cwd: Working directory for the command.
        write_dirs: Absolute paths that should remain writable.

    Returns:
        Command list with unshare prefix and mount setup.

    Raises:
        ValueError: If cwd or any write_dir is not an absolute path.
    """
    if not os.path.isabs(cwd):
        raise ValueError(f"cwd must be absolute: {cwd}")

    mount_script = _build_mount_script(write_dirs)
    escaped_cwd = shlex.quote(cwd)
    # Escape the shell command for embedding inside bash -c
    escaped_shell_cmd = shell_cmd.replace("'", "'\\''")
    bash_script = f"{mount_script}cd {escaped_cwd} && exec bash -c '{escaped_shell_cmd}'"

    return [
        "unshare", "--user", "--map-root-user", "--mount",
        "bash", "-c", bash_script,
    ]


def _build_mount_script(write_dirs: list[str]) -> str:
    """Build the mount commands for the sandbox setup.

    Makes the entire filesystem read-only, then bind-mounts specified
    directories as writable.
    """
    for d in write_dirs:
        if not os.path.isabs(d):
            raise ValueError(f"write_dir must be absolute: {d}")

    # Filter to existing directories
    existing_dirs = [d for d in write_dirs if os.path.isdir(d)]

    root_dev = os.stat("/").st_dev

    lines = ["mount --make-rprivate / && mount -o remount,ro,bind / /"]
    for d in existing_dirs:
        escaped = shlex.quote(d)
        if os.stat(d).st_dev == root_dev:
            # Same filesystem as root — bind mount inherits ro, so remount rw is needed
            lines.append(f"mount --bind {escaped} {escaped} && mount -o remount,rw,bind {escaped} {escaped}")
        else:
            # Separate filesystem (e.g. tmpfs) — bind mount already inherits rw;
            # remounting flags on a foreign-namespace mount is rejected by the kernel
            lines.append(f"mount --bind {escaped} {escaped}")

    return " && ".join(lines) + " && "
