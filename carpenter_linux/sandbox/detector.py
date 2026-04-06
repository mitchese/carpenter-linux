"""System capability probing for sandbox methods."""

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


def detect() -> dict:
    """Probe the system for available sandbox capabilities.

    Returns:
        Dict with keys: namespace (bool), bubblewrap (bool), bubblewrap_path (str|None),
        docker (bool), landlock (bool), apparmor (bool), recommended (str).
    """
    result = {
        "namespace": False,
        "bubblewrap": False,
        "bubblewrap_path": None,
        "docker": False,
        "landlock": False,
        "apparmor": False,
        "recommended": "none",
    }

    # All sandbox methods are Linux-specific kernel features
    if sys.platform != "linux":
        logger.info("Sandbox detection: non-Linux platform (%s), no sandbox available", sys.platform)
        return result

    # Probe user+mount namespaces
    result["namespace"] = _probe_namespace()

    # Probe bubblewrap
    bwrap_path = shutil.which("bwrap")
    if bwrap_path:
        result["bubblewrap"] = True
        result["bubblewrap_path"] = bwrap_path

    # Probe docker
    result["docker"] = shutil.which("docker") is not None

    # Probe landlock (syscall-based)
    result["landlock"] = _probe_landlock()

    # Probe apparmor
    result["apparmor"] = _probe_apparmor()

    # Determine recommendation (best available)
    # Priority: landlock > namespace > bubblewrap > apparmor > docker > none
    if result["landlock"]:
        result["recommended"] = "landlock"
    elif result["namespace"]:
        result["recommended"] = "namespace"
    elif result["bubblewrap"]:
        result["recommended"] = "bubblewrap"
    elif result["apparmor"]:
        result["recommended"] = "apparmor"
    elif result["docker"]:
        result["recommended"] = "docker"
    else:
        result["recommended"] = "none"

    return result


def _probe_namespace() -> bool:
    """Test whether unprivileged user+mount namespaces work."""
    try:
        proc = subprocess.run(
            [
                "unshare", "--user", "--map-root-user", "--mount",
                "bash", "-c", "mount --make-rprivate / && echo ok",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode == 0 and "ok" in proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("Namespace probe failed: %s", e)
        return False


def _probe_landlock() -> bool:
    """Check if Landlock LSM is available via actual syscall probe.

    Calls landlock_create_ruleset with LANDLOCK_CREATE_RULESET_VERSION flag
    to query the ABI version without creating a ruleset. More reliable than
    checking /sys/kernel/security/lsm.
    """
    try:
        from carpenter_linux.sandbox._landlock_helper import probe_landlock_version
        version = probe_landlock_version()
        return version > 0
    except (OSError, ImportError) as e:
        logger.debug("Landlock probe failed: %s", e)
        return False


def _probe_apparmor() -> bool:
    """Check if AppArmor is available (aa-exec binary + AppArmor filesystem)."""
    has_binary = shutil.which("aa-exec") is not None
    has_fs = os.path.isdir("/sys/kernel/security/apparmor")
    return has_binary and has_fs
