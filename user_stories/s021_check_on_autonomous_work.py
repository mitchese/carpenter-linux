"""
S021 — Check on Autonomous Work

The user starts a 3-step workflow about programming languages. After the arcs
complete, the user asks "what have you been up to?" The agent reports from
platform introspection (listing arcs, reading their state/history) rather
than from memory alone.

Expected behaviour:
  1. User requests a 3-step workflow: "Describe Python, Rust, and Go."
  2. Agent creates arcs and they execute autonomously.
  3. After completion, user asks "what have you been up to?"
  4. Agent introspects the platform (list_arcs, get_arc_detail) and
     reports what was accomplished, referencing the actual work done.
  5. Response mentions the 3 languages and the arc outcomes.

DB verification:
  - At least 3 child arcs created and completed.
  - The introspection response references actual arc content.
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
    "to send me a short paragraph about a different programming language: "
    "Python, Rust, and Go. Let them run automatically."
)

_CHECKIN_PROMPT = (
    "What have you been up to? Give me a summary of what you've accomplished "
    "recently — check the arc history to see what work was done."
)


class CheckOnAutonomousWork(AcceptanceStory):
    name = "S021 — Check on Autonomous Work"
    description = (
        "User starts 3-step workflow about programming languages, then asks "
        "'what have you been up to?'; agent introspects platform and reports."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Start workflow ─────────────────────────────────────────────────
        print("\n  [1/4] Starting 3-step programming languages workflow...")
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
        print("  [2/4] Waiting for 3 arc messages (up to 150s)...")
        all_msgs = client.wait_for_n_assistant_messages(conv_id, n=4, timeout=150)
        print(f"     Got {len(all_msgs)} assistant messages")

        # Verify the arc messages mention programming languages
        combined = " ".join(m["content"] for m in all_msgs).lower()
        lang_count = sum(1 for lang in ("python", "rust", "go ")
                         if lang in combined)
        self.assert_that(
            lang_count >= 2,
            f"Expected messages about Python/Rust/Go, found {lang_count} languages",
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

        # ── 4. Ask "what have you been up to?" ────────────────────────────────
        print("  [4/4] Asking agent to report on recent work...")
        client.send_message(_CHECKIN_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        check_msgs = client.get_assistant_messages(conv_id)
        report = check_msgs[-1]["content"]
        print(f"     Report: {report[:300]}")

        # The report should reference the work that was done
        report_lower = report.lower()
        self.assert_that(
            any(kw in report_lower for kw in
                ("python", "rust", "go", "programming", "language",
                 "workflow", "step", "arc", "complet", "message")),
            "Report does not reference the programming languages workflow",
            response_preview=report[:600],
        )

        # Should mention at least 2 of the 3 languages
        report_langs = sum(1 for lang in ("python", "rust", "go ")
                           if lang in report_lower)
        self.assert_that(
            report_langs >= 2,
            f"Report only mentions {report_langs}/3 languages",
            response_preview=report[:600],
        )

        # DB assertions
        if db is not None:
            new_arcs = db.get_arcs_created_after(start_ts)
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
                f"{len(all_msgs)} messages received ✓, "
                f"introspection report references work done ✓, "
                f"{report_langs} languages mentioned in report ✓"
            ),
        )
