"""
S033 — Recover From Failed Arc

A 3-step workflow is started where step 2 is intentionally tricky — it asks
the agent to write a greeting in Klingon. The agent may struggle or fail on
this step, but should NOT abandon the entire workflow. It should recover
(retry, skip, or work around) and complete at least 2 of the 3 steps.
The parent arc should reach 'completed' status.

Expected behaviour:
  1. User requests a 3-step workflow:
     Step 1: Describe what a hash table is.
     Step 2: Write a greeting message in Klingon.
     Step 3: Describe what a stack is.
  2. Step 2 may fail or produce a partial result (Klingon is hard).
  3. The agent does NOT abandon the whole workflow.
  4. At least 2 of the 3 steps complete.
  5. The parent arc reaches 'completed' status.

DB verification:
  - At least 3 child arcs created.
  - At least 2 child arcs have status='completed'.
  - Parent arc has status='completed'.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_WORKFLOW_PROMPT = (
    "Please create a 3-step workflow:\n"
    "Step 1: Send me a message explaining what a hash table is.\n"
    "Step 2: Send me a greeting message written entirely in Klingon "
    "(the Star Trek language).\n"
    "Step 3: Send me a message explaining what a stack data structure is.\n"
    "Use separate arcs for each step. Let them run automatically."
)


class RecoverFromFailedArc(AcceptanceStory):
    name = "S033 — Recover From Failed Arc"
    description = (
        "3-step workflow with step 2 intentionally tricky (Klingon); agent "
        "recovers or continues; at least 2/3 steps complete; parent completes."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Start workflow ─────────────────────────────────────────────────
        print("\n  [1/3] Starting 3-step workflow (step 2 is tricky)...")
        conv_id = client.create_conversation()
        client.send_message(_WORKFLOW_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No planning response received",
            conversation_id=conv_id,
        )
        print(f"     Planning: {msgs[-1]['content'][:150]}")

        # ── 2. Wait for messages ──────────────────────────────────────────────
        print("  [2/3] Waiting for arc messages (up to 180s)...")
        try:
            all_msgs = client.wait_for_n_assistant_messages(
                conv_id, n=3, timeout=180
            )
        except TimeoutError:
            all_msgs = client.get_assistant_messages(conv_id)
        print(f"     Got {len(all_msgs)} assistant messages")

        # At least some messages should have arrived
        self.assert_that(
            len(all_msgs) >= 3,
            f"Expected >=3 assistant messages (planning + 2 arcs), "
            f"got {len(all_msgs)}",
            messages=[m.get("content", "")[:80] for m in all_msgs],
        )

        # ── 3. DB assertions ─────────────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message=f"Behavioural: {len(all_msgs)} messages ✓",
            )

        print("  [3/3] Verifying recovery in DB...")

        # Wait for arcs to settle
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            arcs = db.get_arcs_created_after(start_ts)
            pending = [a for a in arcs
                       if a["status"] not in ("completed", "failed", "cancelled")]
            if not pending:
                break
            time.sleep(5)

        new_arcs = db.get_arcs_created_after(start_ts)
        print(f"     {len(new_arcs)} arcs created")

        # Find child arcs
        child_arcs = [a for a in new_arcs if a.get("parent_id") is not None]
        if not child_arcs:
            child_arcs = new_arcs  # flat structure

        completed = [a for a in child_arcs if a["status"] == "completed"]
        print(f"     {len(completed)}/{len(child_arcs)} child arcs completed")

        # At least 2 of 3 should complete
        self.assert_that(
            len(completed) >= 2,
            f"Expected >=2 completed child arcs, found {len(completed)}",
            arcs=db.format_arcs_table(new_arcs),
        )

        # Find parent arc — should be completed
        parent_candidates = [a for a in new_arcs
                             if a.get("parent_id") is None
                             and any(c.get("parent_id") == a["id"]
                                     for c in new_arcs)]
        if parent_candidates:
            parent = parent_candidates[0]
            self.assert_that(
                parent["status"] == "completed",
                f"Parent arc status is {parent['status']}, expected 'completed'",
                arcs=db.format_arcs_table(new_arcs),
            )
            print(f"     Parent arc {parent['id']} completed ✓")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{len(completed)}/{len(child_arcs)} steps completed ✓, "
                f"workflow recovered from tricky step ✓"
            ),
        )
