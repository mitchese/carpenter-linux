#!/usr/bin/env python3
"""
Helper script to get the timeout for a story.

Usage:
    python3 user_stories/get_story_timeout.py s031

Outputs the timeout in seconds, or 300 (default) if not specified.
"""

import sys
from pathlib import Path

# Allow running from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from user_stories.runner import discover_stories


def main():
    if len(sys.argv) < 2:
        print("300")  # Default timeout
        return

    story_prefix = sys.argv[1]
    stories = discover_stories([story_prefix])

    if not stories:
        print("300")  # Default if not found
        return

    # Get timeout from first matching story
    timeout = getattr(stories[0], 'timeout', 300)
    print(timeout)


if __name__ == "__main__":
    main()
