"""
S034 — Interrupt With Urgent Task

The user starts a 3-step workflow about data structures. After the planning
response, the user interrupts with an urgent question: "What is the time
complexity of binary search?" The agent should answer O(log n) AND reference
the prior workflow. The workflow arcs should NOT be cancelled.

Expected behaviour:
  1. User requests a 3-step workflow about data structures.
  2. Agent plans and begins execution.
  3. User interrupts: "What is the time complexity of binary search?"
  4. Agent answers the urgent question (O(log n)).
  5. Agent references or acknowledges the ongoing workflow.
  6. Workflow arcs are NOT cancelled — they continue or complete.

DB verification:
  - Workflow arcs are not cancelled.
  - The urgent question receives a correct answer.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_WORKFLOW_PROMPT = (
    "Please create a 3-step workflow. In each step, send me a message "
    "describing a data structure: arrays, linked lists, and binary trees. "
    "Use separate arcs and let them run automatically."
)

_INTERRUPT_PROMPT = (
    "Quick urgent question — what is the time complexity of binary search? "
    "I need to know right now."
)


class InterruptWithUrgentTask(AcceptanceStory):
    name = "S034 — Interrupt With Urgent Task"
    description = (
        "User starts 3-step workflow, then interrupts with urgent question; "
        "agent answers O(log n) and references prior workflow; arcs not cancelled."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Start workflow ─────────────────────────────────────────────────
        print("\n  [1/4] Starting 3-step data structures workflow...")
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

        # ── 2. Interrupt with urgent question ─────────────────────────────────
        print("  [2/4] Interrupting with urgent question...")
        client.send_message(_INTERRUPT_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        interrupt_msgs = client.get_assistant_messages(conv_id)
        interrupt_resp = interrupt_msgs[-1]["content"]
        print(f"     Interrupt response: {interrupt_resp[:300]}")

        # Should answer O(log n)
        interrupt_lower = interrupt_resp.lower()
        self.assert_that(
            any(kw in interrupt_lower for kw in
                ("o(log n)", "log n", "logarithmic", "o(log")),
            "Interrupt response does not mention O(log n) or logarithmic",
            response_preview=interrupt_resp[:400],
        )

        # ── 3. Wait for workflow to continue ──────────────────────────────────
        print("  [3/4] Waiting for workflow messages (up to 150s)...")
        try:
            all_msgs = client.wait_for_n_assistant_messages(
                conv_id, n=5, timeout=150
            )
        except TimeoutError:
            all_msgs = client.get_assistant_messages(conv_id)
        print(f"     Total messages: {len(all_msgs)}")

        # ── 4. DB assertions ─────────────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message="Behavioural: O(log n) answered ✓",
            )

        print("  [4/4] Verifying workflow arcs not cancelled...")

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
        child_arcs = [a for a in new_arcs if a.get("parent_id") is not None]

        # Workflow arcs should NOT all be cancelled
        cancelled = [a for a in child_arcs if a["status"] == "cancelled"]
        completed = [a for a in child_arcs if a["status"] == "completed"]

        print(f"     Child arcs: {len(child_arcs)} total, "
              f"{len(completed)} completed, {len(cancelled)} cancelled")

        self.assert_that(
            len(completed) >= 1,
            f"Expected at least 1 completed workflow arc, "
            f"found {len(completed)} (all cancelled?)",
            arcs=db.format_arcs_table(new_arcs),
        )

        # Not all should be cancelled
        if child_arcs:
            self.assert_that(
                len(cancelled) < len(child_arcs),
                "All workflow arcs were cancelled after interrupt — "
                "expected them to continue",
                arcs=db.format_arcs_table(child_arcs),
            )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Urgent question answered (O(log n)) ✓, "
                f"{len(completed)} workflow arcs completed ✓, "
                f"arcs not all cancelled ✓"
            ),
        )
