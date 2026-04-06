"""
S048 — Skill KB Review Workflow Triggers on Agent Write

When an agent writes to a KB entry under the `skills/` path, the platform
automatically triggers a `skill-kb-review` template workflow.  This story
verifies the end-to-end review pipeline for a **clean** (untainted) source:

1. Agent creates a KB entry at `skills/test-review-workflow` (via submit_code).
2. Platform triggers the skill-kb-review template (4 child arcs).
3. classify-source auto-completes (clean source).
4. text-review auto-passes (clean source).
5. intent-review runs as an AI REVIEWER arc.
6. human-escalation is auto-skipped (clean + intent passed).
7. Parent review arc reaches 'completed'.

For a clean conversation the entire pipeline should complete without human
intervention.

DB verification:
  - A skill-kb-review parent arc exists.
  - The parent has 4 child arcs (classify-source, text-review,
    intent-review, human-escalation).
  - All children reach 'completed' status.
  - Parent arc_state contains `_source_tainted: false`.
"""

import sqlite3
import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_KB_PATH = "skills/test-review-workflow"

_CREATE_PROMPT = (
    "Please create a new knowledge base entry at "
    "'skills/test-review-workflow'. The entry should contain a short note "
    "about testing: 'This is a test entry to verify the skill-KB review "
    "workflow triggers correctly when writing to the skills/ path.'\n"
    "Save it permanently."
)


class SkillKbReviewWorkflow(AcceptanceStory):
    name = "S048 — Skill KB Review Workflow Triggers on Agent Write"
    description = (
        "Agent creates a skills/ KB entry; platform auto-triggers "
        "skill-kb-review template; clean source completes without "
        "human escalation."
    )
    timeout = 300  # 5 minutes — review arcs need time to process

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── Step 1: Ask agent to create the KB entry ────────────────────
        print(f"\n  [1/3] Sending KB-creation request for '{_KB_PATH}'...")
        conv_id = client.create_conversation()
        client.send_message(_CREATE_PROMPT, conv_id)

        print("  Waiting for KB creation to complete (up to 90s)...")
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        create_msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(create_msgs) >= 1,
            "No assistant response after KB-creation request",
            conversation_id=conv_id,
        )

        create_response = create_msgs[-1]["content"]
        print(f"  Response preview: {create_response[:200]}")

        # Agent should acknowledge the KB entry was created.
        self.assert_that(
            any(
                kw in create_response.lower()
                for kw in ("created", "saved", "added", "add", "knowledge", "kb")
            ),
            "Create response does not acknowledge KB creation",
            response_preview=create_response[:400],
        )

        # ── Step 2: Verify the review arc was created ───────────────────
        print("  [2/3] Waiting for skill-kb-review arc (up to 120s)...")
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message="Behavioural checks passed (no DB; skipping arc verification)",
            )

        review_parent = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            arcs = db.get_arcs_created_after(start_ts)
            for arc in arcs:
                if arc.get("name") == "skill-kb-review":
                    review_parent = arc
                    break
            if review_parent is not None:
                break
            time.sleep(3)

        self.assert_that(
            review_parent is not None,
            "No skill-kb-review arc was created after KB write",
            arcs_since_start=[
                {"id": a["id"], "name": a.get("name"), "status": a.get("status")}
                for a in db.get_arcs_created_after(start_ts)
            ],
        )

        parent_id = review_parent["id"]
        print(f"  Found skill-kb-review parent arc #{parent_id}")

        # Verify parent arc_state has the KB path
        parent_state = db.get_arc_state(parent_id)
        self.assert_that(
            parent_state.get("kb_path") == _KB_PATH,
            f"Expected kb_path='{_KB_PATH}' in parent state, got {parent_state.get('kb_path')}",
            parent_state=parent_state,
        )

        # ── Step 3: Wait for the review pipeline to complete ────────────
        print("  [3/3] Waiting for review pipeline to complete (up to 180s)...")
        deadline = time.monotonic() + 180
        review_completed = False
        while time.monotonic() < deadline:
            parent_arc = db.get_arc(parent_id)
            if parent_arc and parent_arc.get("status") in ("completed", "failed"):
                review_completed = True
                break
            time.sleep(5)

        # Diagnostics: show child arc statuses regardless of outcome
        children = db.get_arc_children(parent_id)
        child_summary = [
            {"name": c.get("name"), "status": c.get("status"), "id": c["id"]}
            for c in children
        ]
        print(f"  Child arcs: {child_summary}")

        self.assert_that(
            review_completed,
            "skill-kb-review parent did not reach terminal status within 180s",
            parent_status=db.get_arc(parent_id).get("status") if db.get_arc(parent_id) else "unknown",
            child_summary=child_summary,
        )

        # Parent should be completed (not failed)
        final_parent = db.get_arc(parent_id)
        self.assert_that(
            final_parent.get("status") == "completed",
            f"Expected parent status='completed', got '{final_parent.get('status')}'",
            child_summary=child_summary,
        )

        # Verify child arc structure
        self.assert_that(
            len(children) == 4,
            f"Expected 4 child arcs, got {len(children)}",
            child_summary=child_summary,
        )

        expected_names = {"classify-source", "text-review", "intent-review", "human-escalation"}
        actual_names = {c.get("name") for c in children}
        self.assert_that(
            expected_names == actual_names,
            f"Expected child names {expected_names}, got {actual_names}",
            child_summary=child_summary,
        )

        # All children should be completed
        for child in children:
            self.assert_that(
                child.get("status") == "completed",
                f"Child '{child.get('name')}' has status '{child.get('status')}', expected 'completed'",
                child_summary=child_summary,
            )

        # Verify the source was classified as clean (untainted)
        parent_state = db.get_arc_state(parent_id)
        self.assert_that(
            parent_state.get("_source_tainted") is False,
            f"Expected _source_tainted=False, got {parent_state.get('_source_tainted')}",
            parent_state=parent_state,
        )

        print(f"  Review pipeline completed successfully (clean source, auto-approved)")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"KB entry '{_KB_PATH}' created ✓, "
                f"skill-kb-review arc #{parent_id} triggered ✓, "
                f"4 child arcs all completed ✓, "
                f"source classified clean ✓, "
                f"human-escalation auto-skipped ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: DBInspector) -> None:
        """Remove the test KB entry and any review arcs."""
        if db is None:
            return

        try:
            conn = sqlite3.connect(db.db_path)
            try:
                conn.execute("DELETE FROM kb_entries WHERE path = ?", (_KB_PATH,))
                conn.execute("DELETE FROM kb_links WHERE source_path = ?", (_KB_PATH,))
                conn.commit()
                print(f"  [cleanup] Removed '{_KB_PATH}' from kb_entries table")
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [cleanup] DB cleanup failed: {exc}")

        # Also remove the KB file on disk so autogen doesn't re-create the
        # DB entry on next server restart.
        import os
        base_dir = os.path.dirname(os.path.dirname(db.db_path))  # data/ -> config/kb
        kb_file = os.path.join(base_dir, "config", "kb", _KB_PATH + ".md")
        try:
            if os.path.exists(kb_file):
                os.remove(kb_file)
                print(f"  [cleanup] Removed KB file: {kb_file}")
        except Exception as exc:
            print(f"  [cleanup] KB file cleanup failed: {exc}")
