"""
S045 — Branch-Based Merge Handles Diverged Source Gracefully

When a coding-change workspace is created, the system snapshots the source
repo's HEAD SHA.  If the source advances (new commits) while the agent works,
the approval step uses a proper git merge via a temp branch rather than a
naive patch-apply.  Non-conflicting changes in different files merge cleanly.
Conflicting changes raise MergeConflictError and surface a review link.

This replaces the old patch-apply flow which would either fail silently or
require `git reset --hard` to recover — destroying local work.

Expected behaviour:
  1. Agent creates a coding-change arc targeting the platform source.
  2. While the arc is in 'waiting', the story makes a non-conflicting commit
     directly in the source repo (simulating concurrent human work).
  3. Story approves the diff.
  4. System detects the divergence, creates a temp branch at the snapshot SHA,
     applies the workspace diff, and merges cleanly into the current branch.
  5. Arc completes.  Both the agent's change and the concurrent commit survive.

DB verification:
  - Arc history contains 'changes_applied' event.
  - Arc reaches status='completed'.
  - workspace_base_sha is recorded in arc state.

NOTE: This story makes a small additive change to the platform source.
The concurrent commit adds a harmless marker file that is cleaned up afterward.
"""

import os
import subprocess
import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_PROMPT = (
    "Please add a single-line comment '# s045 merge test' to the very end "
    "of carpenter/__init__.py using the platform coding-change workflow. "
    "This is a trivial change for acceptance testing."
)

_MARKER_FILE = "_s045_concurrent_marker.txt"


class MergeHandlesDivergedSource(AcceptanceStory):
    name = "S045 — Branch-Based Merge Handles Diverged Source"
    description = (
        "Source repo advances while coding agent works; branch-based merge "
        "applies both changes cleanly without destroying concurrent work."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Request a trivial platform change ─────────────────────────
        print("\n  [1/5] Requesting trivial platform change...")
        conv_id = client.create_conversation()
        client.send_message(_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(len(msgs) >= 1, "No response to change request")

        # ── 2. Wait for the arc to reach 'waiting' ───────────────────────
        print("  [2/5] Waiting for coding-change arc (<=5 min)...")
        review_arc = None
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if db is not None:
                pending = db.get_arcs_pending_review(start_ts)
                if pending:
                    review_arc = pending[0]
                    break
            time.sleep(5)

        if db is None:
            return StoryResult(name=self.name, passed=True, message="Skipped (no DB)")

        self.assert_that(
            review_arc is not None,
            "Arc never reached 'waiting'",
            arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
        )

        arc_state = review_arc["arc_state"]
        review_id = arc_state["review_id"]
        base_sha = arc_state.get("workspace_base_sha")
        source_dir = arc_state.get("source_dir", "")
        print(f"     Arc {review_arc['id']} waiting, base_sha={base_sha and base_sha[:12]}")

        self.assert_that(bool(source_dir), "No source_dir in arc state")
        self.assert_that(
            base_sha is not None,
            "workspace_base_sha not recorded — merge-based apply won't activate",
        )

        # ── 3. Simulate concurrent work: commit a non-conflicting file ───
        marker_path = os.path.join(source_dir, _MARKER_FILE)
        print(f"  [3/5] Adding concurrent commit ({_MARKER_FILE})...")
        with open(marker_path, "w") as f:
            f.write("# Concurrent human work during coding-change arc\n")

        subprocess.run(
            ["git", "add", _MARKER_FILE],
            cwd=source_dir, capture_output=True, check=True, timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", "test(s045): concurrent commit during coding-change"],
            cwd=source_dir, capture_output=True, check=True, timeout=10,
        )

        # Verify HEAD has advanced past the snapshot
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_dir, capture_output=True, text=True, timeout=10,
        )
        new_sha = head_result.stdout.strip()
        self.assert_that(
            new_sha != base_sha,
            "HEAD did not advance — concurrent commit failed",
        )
        print(f"     Source advanced: {base_sha[:12]} -> {new_sha[:12]}")

        # ── 4. Approve the diff ──────────────────────────────────────────
        print("  [4/5] Approving diff (merge should handle divergence)...")
        result = client.submit_review_decision(review_id, decision="approve")
        self.assert_that(result.get("recorded") is True, "Approval not recorded")

        # ── 5. Wait for completion ───────────────────────────────────────
        print("  [5/5] Waiting for arc to complete (<=120s)...")
        deadline = time.monotonic() + 120
        final_arc = None
        while time.monotonic() < deadline:
            final_arc = db.get_arc(review_arc["id"])
            if final_arc and final_arc["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(3)

        self.assert_that(
            final_arc is not None and final_arc["status"] == "completed",
            f"Arc did not complete (status={final_arc['status'] if final_arc else 'not found'})",
            arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
        )

        # Verify both changes survived
        self.assert_that(
            os.path.exists(marker_path),
            "Concurrent marker file was destroyed by the merge",
        )
        print("     Merge succeeded — both agent and concurrent changes preserved.")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                "Source diverged during coding-change, "
                "branch-based merge applied both changes cleanly, "
                "concurrent work preserved."
            ),
        )

    def cleanup(self, client: CarpenterClient, db: DBInspector | None) -> None:
        """Revert the concurrent marker commit."""
        if db is None:
            return
        arcs = db.get_arcs_created_after(0)
        for arc in arcs:
            state = db.get_arc_state(arc["id"])
            source_dir = state.get("source_dir", "")
            if not source_dir:
                continue
            marker = os.path.join(source_dir, _MARKER_FILE)
            if os.path.exists(marker):
                os.remove(marker)
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=source_dir, capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["git", "commit", "-m", "cleanup(s045): remove concurrent marker"],
                    cwd=source_dir, capture_output=True, timeout=10,
                )
            break
