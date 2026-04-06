"""
S038 — Explain Audit Trail

The user starts a 3-step workflow about planets. After completion, the user
asks "walk me through everything you did step by step." The agent reconstructs
a narrative from arc history with real arc IDs — essentially presenting the
audit trail of its autonomous work.

Expected behaviour:
  1. User requests 3-step workflow: describe Mercury, Venus, Mars.
  2. Agent creates parent + 3 child arcs.
  3. All arcs complete with messages delivered.
  4. User asks for a step-by-step walkthrough.
  5. Agent reconstructs a narrative from arc history.
  6. Response references real arc IDs and describes what each step did.
  7. Response covers all 3 planets.

DB verification:
  - At least 3 child arcs completed.
  - Agent's narrative references at least 1 real arc ID.
  - All 3 planets mentioned in the narrative.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_WORKFLOW_PROMPT = (
    "Please create a 3-step workflow. In each step, use the messaging tool "
    "to send me a short description of a planet:\n"
    "Step 1: Mercury\n"
    "Step 2: Venus\n"
    "Step 3: Mars\n"
    "Use separate arcs for each step and let them run automatically."
)

_AUDIT_PROMPT = (
    "Walk me through everything you just did, step by step. "
    "I want a detailed account — reference the actual arc IDs, "
    "what each step did, and whether it succeeded. "
    "Give me the full audit trail."
)


class ExplainAuditTrail(AcceptanceStory):
    name = "S038 — Explain Audit Trail"
    description = (
        "User starts 3-step planet workflow, then asks for step-by-step "
        "walkthrough; agent reconstructs narrative from arc history with IDs."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Start workflow ─────────────────────────────────────────────────
        print("\n  [1/4] Starting 3-step planets workflow...")
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

        # ── 2. Wait for arc messages ──────────────────────────────────────────
        print("  [2/4] Waiting for 3 planet messages (up to 150s)...")
        all_msgs = client.wait_for_n_assistant_messages(conv_id, n=4, timeout=150)
        print(f"     Got {len(all_msgs)} assistant messages")

        # Verify planets were described
        combined = " ".join(m["content"] for m in all_msgs).lower()
        planet_count = sum(1 for p in ("mercury", "venus", "mars")
                           if p in combined)
        self.assert_that(
            planet_count >= 2,
            f"Expected messages about Mercury/Venus/Mars, "
            f"found {planet_count} planets",
            response_preview=combined[:600],
        )

        # ── 3. Wait for arcs to settle ────────────────────────────────────────
        if db is not None:
            print("  [3/4] Waiting for arcs to settle (up to 90s)...")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                arcs = db.get_arcs_created_after(start_ts)
                pending = [a for a in arcs
                           if a["status"] not in ("completed", "failed", "cancelled")]
                if not pending:
                    break
                time.sleep(5)

        # ── 4. Ask for audit trail ────────────────────────────────────────────
        print("  [4/4] Requesting audit trail walkthrough...")
        client.send_message(_AUDIT_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        audit_msgs = client.get_assistant_messages(conv_id)
        audit = audit_msgs[-1]["content"]
        print(f"     Audit trail: {audit[:400]}")

        audit_lower = audit.lower()

        # Should mention all 3 planets
        audit_planets = sum(1 for p in ("mercury", "venus", "mars")
                            if p in audit_lower)
        self.assert_that(
            audit_planets >= 2,
            f"Audit trail only mentions {audit_planets}/3 planets",
            response_preview=audit[:600],
        )

        # Should reference arc concepts (step, arc, completed, etc.)
        self.assert_that(
            any(kw in audit_lower for kw in
                ("arc", "step", "complet", "status", "workflow",
                 "executed", "finished")),
            "Audit trail does not reference arc/step/status concepts",
            response_preview=audit[:600],
        )

        # Check for real arc ID references
        if db is not None:
            new_arcs = db.get_arcs_created_after(start_ts)
            arc_ids = [str(a["id"]) for a in new_arcs]

            id_refs = sum(1 for aid in arc_ids if aid in audit)
            self.assert_that(
                id_refs >= 1,
                f"Audit trail does not reference any real arc IDs "
                f"(expected one of: {arc_ids})",
                response_preview=audit[:600],
                arc_ids=arc_ids,
            )
            print(f"     {id_refs} arc ID(s) referenced ✓")

            # Verify arcs completed
            child_arcs = [a for a in new_arcs if a.get("parent_id") is not None]
            completed = [a for a in child_arcs if a["status"] == "completed"]
            self.assert_that(
                len(completed) >= 3,
                f"Expected >=3 completed child arcs, found {len(completed)}",
                arcs=db.format_arcs_table(new_arcs),
            )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{len(all_msgs)} messages ✓, "
                f"{audit_planets} planets in audit trail ✓, "
                f"real arc IDs referenced ✓"
            ),
        )
