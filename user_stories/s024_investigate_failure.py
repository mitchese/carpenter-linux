"""
S024 — Investigate a Workflow Failure

The user starts a workflow, then asks "something went wrong, can you
investigate?" The agent navigates arc history, reads logs, and presents
a diagnosis with real arc IDs.

Expected behaviour:
  1. User starts a 3-step workflow (deliberately tricky: one step asks
     for something unusual that may fail or produce unexpected results).
  2. After the workflow runs, user says "something went wrong, investigate."
  3. Agent uses list_arcs, get_arc_detail, and arc history to reconstruct
     what happened.
  4. Agent presents a diagnosis referencing real arc IDs and statuses.

DB verification:
  - Arcs were created for the workflow.
  - Agent's investigation response references actual arc IDs from the DB.
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
    "Step 1: Send me a message explaining what a linked list is.\n"
    "Step 2: Send me a message explaining what a binary tree is.\n"
    "Step 3: Send me a message explaining what a quantum B-tree is "
    "(this is a made-up data structure — do your best).\n"
    "Use separate arcs and let them run automatically."
)

_INVESTIGATE_PROMPT = (
    "Something seemed off with that workflow. Can you investigate what happened? "
    "Look at the arc history, check the status of each step, and tell me "
    "exactly what occurred — reference the actual arc IDs."
)


class InvestigateFailure(AcceptanceStory):
    name = "S024 — Investigate a Workflow Failure"
    description = (
        "User starts workflow then asks agent to investigate; agent navigates "
        "arc history, reads logs, presents diagnosis with real arc IDs."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Start workflow ─────────────────────────────────────────────────
        print("\n  [1/4] Starting 3-step workflow...")
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

        # ── 2. Wait for arc messages to arrive ────────────────────────────────
        print("  [2/4] Waiting for workflow to run (up to 180s)...")
        try:
            all_msgs = client.wait_for_n_assistant_messages(
                conv_id, n=4, timeout=180
            )
        except TimeoutError:
            all_msgs = client.get_assistant_messages(conv_id)
        print(f"     Got {len(all_msgs)} assistant messages")

        # Wait for arcs to settle
        if db is not None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                arcs = db.get_arcs_created_after(start_ts)
                pending = [a for a in arcs
                           if a["status"] not in ("completed", "failed", "cancelled")]
                if not pending:
                    break
                time.sleep(5)

        # ── 3. Ask for investigation ──────────────────────────────────────────
        print("  [3/4] Asking agent to investigate...")
        client.send_message(_INVESTIGATE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        invest_msgs = client.get_assistant_messages(conv_id)
        investigation = invest_msgs[-1]["content"]
        print(f"     Investigation: {investigation[:300]}")

        # Should reference arc concepts
        invest_lower = investigation.lower()
        self.assert_that(
            any(kw in invest_lower for kw in
                ("arc", "step", "status", "complet", "fail", "history",
                 "workflow", "linked list", "binary tree", "quantum")),
            "Investigation does not reference arc workflow details",
            response_preview=investigation[:600],
        )

        # ── 4. Verify arc IDs are referenced ─────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message="Behavioural checks passed (no DB configured)",
            )

        print("  [4/4] Verifying arc ID references in investigation...")
        new_arcs = db.get_arcs_created_after(start_ts)
        arc_ids = [str(a["id"]) for a in new_arcs]

        # Check if at least one real arc ID appears in the investigation
        id_refs = sum(1 for aid in arc_ids if aid in investigation)
        self.assert_that(
            id_refs >= 1,
            f"Investigation does not reference any real arc IDs "
            f"(expected one of: {arc_ids})",
            response_preview=investigation[:600],
            arc_ids=arc_ids,
        )
        print(f"     {id_refs} arc ID(s) referenced in investigation ✓")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{len(new_arcs)} arcs created, "
                f"investigation references {id_refs} real arc ID(s) ✓"
            ),
        )
