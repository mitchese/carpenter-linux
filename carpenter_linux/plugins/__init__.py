"""Linux-specific plugin deployment infrastructure.

Provides watcher setup for systemd-based plugin watchers —
generates watcher scripts, config files, and systemd service units.
"""

from .watcher_setup import generate_watcher_setup

__all__ = [
    "generate_watcher_setup",
]
