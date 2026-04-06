"""
S001 — Describe Abilities

The user starts a fresh conversation and asks Carpenter to describe its
core abilities. Carpenter should respond with a substantive summary covering:
  - Tool usage (read-only tools, submit_code for actions)
  - Knowledge base system (kb_describe, kb_search, kb.add, kb.edit)

No arcs should be created; this is a pure conversational response.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


class DescribeAbilities(AcceptanceStory):
    name = "S001 — Describe Abilities"
    description = (
        "User asks Carpenter to describe its abilities; "
        "expects a response mentioning tools and knowledge base."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── Act ──────────────────────────────────────────────────────────────
        print("\n  Sending: 'Please describe your core abilities to me.'")
        conv_id, response = client.chat(
            "Please describe your core abilities to me.",
            timeout=30,
        )
        print(f"  Got response ({len(response)} chars, conversation {conv_id})")

        # ── Behavioral assertions ─────────────────────────────────────────────
        # The response must be more than a single word — a real description.
        word_count = len(response.split())
        self.assert_that(
            word_count >= 5,
            f"Response too terse ({word_count} words); expected a description",
            response_preview=response[:300],
        )

        # Should mention tools or actions
        self.assert_that(
            any(
                kw in response.lower()
                for kw in ("tool", "submit_code", "action", "callback")
            ),
            "Response does not mention tools or actions",
            response_preview=response[:400],
        )

        # Should mention knowledge base or learning capabilities
        self.assert_that(
            any(
                kw in response.lower()
                for kw in ("knowledge", "kb", "learn", "modify", "update")
            ),
            "Response does not mention knowledge base or learning capabilities",
            response_preview=response[:400],
        )

        # ── DB assertions ─────────────────────────────────────────────────────
        # Describing abilities is pure chat — no arcs should be created
        if db is not None:
            new_arcs = db.get_arcs_created_after(start_ts)
            self.assert_that(
                len(new_arcs) == 0,
                f"Expected 0 arcs for a pure-chat response, got {len(new_arcs)}",
                arcs=db.format_arcs_table(new_arcs),
            )

            # All conversation messages should be from the user or main agent (no arc_id)
            arc_msgs = db.get_arc_messages(conv_id)
            self.assert_that(
                len(arc_msgs) == 0,
                f"Expected no arc-originated messages, got {len(arc_msgs)}",
            )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Response: {len(response)} chars, "
                f"mentions tools ✓, mentions knowledge/learning ✓, "
                f"no arcs created ✓"
            ),
        )
