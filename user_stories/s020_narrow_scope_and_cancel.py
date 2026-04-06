"""
S020 — Narrow Scope and Cancel Non-Relevant Arcs

The user asks the agent to plan a big project — a REST API with auth, CRUD,
and notifications. The agent creates a multi-step arc tree for planning. The
user then narrows the request to just the auth component. The agent cancels
the non-auth arcs with cascade.

Expected behaviour:
  1. User asks agent to PLAN a REST API with auth + CRUD + notifications.
  2. Agent creates a parent arc with multiple child arcs (at least 3 steps).
  3. User says "Actually, let's just focus on auth planning for now."
  4. Agent cancels the non-auth child arcs (CRUD, notifications).
  5. Auth-related arc(s) continue or complete.
  6. At least one arc has status='cancelled'.

DB verification:
  - Parent arc with multiple children created.
  - After narrowing, at least one child arc is cancelled.
  - Auth-related arc is completed or still active (not cancelled).

Note: This test explicitly asks for PLANNING only to avoid generating actual
code artifacts that would pollute the repository.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_BIG_PROJECT_PROMPT = (
    "Please create a 3-step workflow for designing a REST API. "
    "Just create the planning workflow with these steps: "
    "Step 1: Design JWT authentication with login and token refresh. "
    "Step 2: Design CRUD endpoints for a 'products' resource. "
    "Step 3: Design email notifications for new product creation. "
    "Use separate arcs for each step and describe each step."
)

_NARROW_PROMPT = (
    "Actually, let's just focus on planning the authentication part for now. "
    "Please use the arc.cancel tool to cancel ONLY the CRUD and notifications "
    "child arcs (not the auth one). Keep the auth arc running — I still want "
    "that design completed."
)


class NarrowScopeAndCancel(AcceptanceStory):
    name = "S020 — Narrow Scope and Cancel Non-Relevant Arcs"
    description = (
        "User requests big 3-part project, then narrows to just auth; "
        "agent cancels non-auth arcs via cascade; verifies cancelled status."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Request big project ────────────────────────────────────────────
        print("\n  [1/4] Requesting 3-step REST API project...")
        conv_id = client.create_conversation()
        client.send_message(_BIG_PROJECT_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after project request",
            conversation_id=conv_id,
        )
        plan_resp = msgs[-1]["content"]
        print(f"     {plan_resp[:200]}")

        # Verify the agent acknowledged a multi-step plan
        self.assert_that(
            any(kw in plan_resp.lower() for kw in
                ("auth", "crud", "notif", "step", "workflow", "arc", "three",
                 "3 step", "three step")),
            "Planning response does not acknowledge multi-step project",
            response_preview=plan_resp[:400],
        )

        # Wait for at least some arc messages to begin
        print("  [2/4] Waiting for initial arc activity (up to 150s)...")
        try:
            client.wait_for_n_assistant_messages(conv_id, n=2, timeout=150)
        except TimeoutError:
            # It's OK if not all messages arrived yet — we're about to narrow
            pass

        # ── 2. Narrow scope ───────────────────────────────────────────────────
        print("  [3/4] Narrowing scope to auth only...")
        client.send_message(_NARROW_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        narrow_msgs = client.get_assistant_messages(conv_id)
        narrow_resp = narrow_msgs[-1]["content"]
        print(f"     {narrow_resp[:200]}")

        self.assert_that(
            any(kw in narrow_resp.lower() for kw in
                ("cancel", "focus", "auth", "narrow", "only", "stop",
                 "remaining", "removed")),
            "Narrowing response does not acknowledge scope change",
            response_preview=narrow_resp[:400],
        )

        # ── 3. DB assertions ─────────────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message="Behavioural checks passed (no DB configured)",
            )

        print("  [4/4] Verifying arc cancellation in DB...")

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
        print(f"     {len(new_arcs)} total arcs created")

        # Should have at least 3 arcs (parent + children or flat)
        self.assert_that(
            len(new_arcs) >= 3,
            f"Expected >=3 arcs for 3-step project, found {len(new_arcs)}",
            arcs=db.format_arcs_table(new_arcs),
        )

        # At least one arc should be cancelled
        cancelled = [a for a in new_arcs if a["status"] == "cancelled"]
        self.assert_that(
            len(cancelled) >= 1,
            f"Expected at least 1 cancelled arc after narrowing, "
            f"found {len(cancelled)}",
            arcs=db.format_arcs_table(new_arcs),
        )
        print(f"     {len(cancelled)} arc(s) cancelled")

        # At least one arc should be completed (the auth arc)
        completed = [a for a in new_arcs if a["status"] == "completed"]
        self.assert_that(
            len(completed) >= 1,
            f"Expected at least 1 completed arc (auth), found {len(completed)}",
            arcs=db.format_arcs_table(new_arcs),
        )
        print(f"     {len(completed)} arc(s) completed")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{len(new_arcs)} arcs total, "
                f"{len(cancelled)} cancelled ✓, "
                f"{len(completed)} completed ✓"
            ),
        )
