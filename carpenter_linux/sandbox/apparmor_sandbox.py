"""AppArmor sandbox — uses aa-exec to confine commands under an AppArmor profile."""

import logging
import os
import shlex
import subprocess

logger = logging.getLogger(__name__)

PROFILE_NAME = "carpenter_sandbox"


def build_command(inner_cmd: list[str], write_dirs: list[str]) -> list[str]:
    """Wrap a command with AppArmor-based confinement.

    Args:
        inner_cmd: The command to run (e.g. ["python3", "script.py"]).
        write_dirs: Absolute paths that should remain writable (used by profile).

    Returns:
        Command list with aa-exec prefix.

    Raises:
        ValueError: If any write_dir is not an absolute path.
    """
    _validate_dirs(write_dirs)

    return ["aa-exec", f"--profile={PROFILE_NAME}", "--", *inner_cmd]


def build_shell_command(shell_cmd: str, cwd: str, write_dirs: list[str]) -> list[str]:
    """Wrap a shell command for AppArmor-confined execution.

    Args:
        shell_cmd: Shell command string to execute.
        cwd: Working directory for the command.
        write_dirs: Absolute paths that should remain writable (used by profile).

    Returns:
        Command list with aa-exec prefix.

    Raises:
        ValueError: If cwd or any write_dir is not an absolute path.
    """
    if not os.path.isabs(cwd):
        raise ValueError(f"cwd must be absolute: {cwd}")
    _validate_dirs(write_dirs)

    escaped_cwd = shlex.quote(cwd)
    escaped_cmd = shell_cmd.replace("'", "'\\''")

    return [
        "aa-exec", f"--profile={PROFILE_NAME}", "--",
        "bash", "-c", f"cd {escaped_cwd} && exec bash -c '{escaped_cmd}'",
    ]


def generate_profile(write_dirs: list[str]) -> str:
    """Generate an AppArmor profile that restricts filesystem writes.

    Args:
        write_dirs: Absolute paths that should be writable.

    Returns:
        AppArmor profile text as a string.
    """
    # Filter to existing dirs and validate
    existing_dirs = []
    for d in write_dirs:
        if os.path.isabs(d) and os.path.isdir(d):
            existing_dirs.append(d)

    write_rules = ""
    for d in existing_dirs:
        # Trailing slash ensures the directory and its contents
        d_clean = d.rstrip("/")
        write_rules += f"  {d_clean}/ rw,\n"
        write_rules += f"  {d_clean}/** rwk,\n"

    profile = f"""# AppArmor profile for Carpenter sandbox
# Auto-generated — do not edit manually.

profile {PROFILE_NAME} flags=(attach_disconnected) {{
  #include <abstractions/base>

  # Global read access
  /** r,

  # Execute system binaries
  /bin/** rix,
  /usr/** rix,
  /lib/** rm,
  /lib64/** rm,

  # Device access
  /dev/null rw,
  /dev/zero r,
  /dev/urandom r,

  # Proc access
  /proc/** r,

  # Writable directories
{write_rules}}}
"""
    return profile


def install_profile(write_dirs: list[str]) -> bool:
    """Write and load the AppArmor profile.

    Writes the profile to /etc/apparmor.d/{PROFILE_NAME} and loads it
    with apparmor_parser -r. Requires root/sudo.

    Args:
        write_dirs: Absolute paths that should be writable.

    Returns:
        True on success, False on failure.
    """
    profile_text = generate_profile(write_dirs)
    profile_path = f"/etc/apparmor.d/{PROFILE_NAME}"

    try:
        with open(profile_path, "w") as f:
            f.write(profile_text)
        logger.info("Wrote AppArmor profile to %s", profile_path)
    except OSError as e:
        logger.error("Failed to write AppArmor profile: %s", e)
        return False

    try:
        result = subprocess.run(
            ["apparmor_parser", "-r", profile_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error("apparmor_parser failed: %s", result.stderr)
            return False
        logger.info("AppArmor profile loaded successfully")
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error("Failed to load AppArmor profile: %s", e)
        return False


def _validate_dirs(write_dirs: list[str]) -> None:
    """Validate that all write dirs are absolute paths."""
    for d in write_dirs:
        if not os.path.isabs(d):
            raise ValueError(f"write_dir must be absolute: {d}")
