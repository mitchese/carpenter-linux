"""
S026 — Recall Preference From Prior Conversation

Conversation A: the user states "I always prefer tabs over spaces for
indentation." The platform stores this preference in memory via FTS5.

Conversation B (new): the user asks for a factorial function and says
"format it correctly." The agent should recall the tabs preference from
the prior conversation and use tabs for indentation.

Expected behaviour:
  1. Conv A: user states tabs preference; agent acknowledges.
  2. Conv B: user requests factorial function with "format it correctly."
  3. Agent recalls from memory that the user prefers tabs.
  4. Response contains a factorial function indented with tabs (or at
     least references tabs).

DB verification:
  - Conv A and Conv B are distinct conversations.
  - Memory search (FTS5) was used to recall the preference (optional,
    hard to verify directly — rely on behavioural check).
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_PREFERENCE_PROMPT = (
    "Important: I always prefer tabs over spaces for indentation in code. "
    "Please remember this for all future conversations. Tabs, not spaces — "
    "that's my style preference."
)

_FACTORIAL_PROMPT = (
    "Write me a Python factorial function. Format it correctly based on "
    "my indentation preferences that I mentioned before."
)


class RecallPreferenceFromPriorConversation(AcceptanceStory):
    name = "S026 — Recall Preference From Prior Conversation"
    description = (
        "Conv A: user states tabs preference. Conv B: user asks for factorial "
        "with 'format correctly'; agent recalls tabs preference via FTS5 memory."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Conversation A: state preference ──────────────────────────────
        print("\n  [1/3] Conv A: Stating tabs preference...")
        conv_a, pref_resp = client.chat(_PREFERENCE_PROMPT, timeout=90)
        print(f"     Conv A (id={conv_a}): {pref_resp[:200]}")

        self.assert_that(
            any(kw in pref_resp.lower() for kw in
                ("tab", "remember", "noted", "preference", "understood",
                 "got it", "will remember", "saved")),
            "Agent did not acknowledge the tabs preference",
            response_preview=pref_resp[:400],
        )

        # Brief pause to let memory indexing complete
        time.sleep(3)

        # ── 2. Conversation B: request factorial ─────────────────────────────
        print("  [2/3] Conv B: Requesting factorial function...")
        conv_b, factorial_resp = client.chat(_FACTORIAL_PROMPT, timeout=150)
        print(f"     Conv B (id={conv_b}): {factorial_resp[:300]}")

        # Conversations should be distinct
        self.assert_that(
            conv_a != conv_b,
            f"Expected different conversation IDs, got same: {conv_a}",
        )

        # ── 3. Verify tabs preference was recalled ───────────────────────────
        print("  [3/3] Verifying tabs preference was recalled...")

        factorial_lower = factorial_resp.lower()

        # The response should contain a factorial function
        self.assert_that(
            any(kw in factorial_lower for kw in
                ("factorial", "def ", "return", "function")),
            "Response does not contain a factorial function",
            response_preview=factorial_resp[:400],
        )

        # Check for tabs preference recall — either tabs in the code,
        # or mention of tabs in the explanation
        has_tabs = "\t" in factorial_resp
        mentions_tabs = any(kw in factorial_lower for kw in
                            ("tab", "tabs", "your preference", "as you prefer"))
        self.assert_that(
            has_tabs or mentions_tabs,
            "Response does not use tabs or mention the tabs preference — "
            "agent may not have recalled from prior conversation",
            has_actual_tabs=has_tabs,
            mentions_tabs=mentions_tabs,
            response_preview=factorial_resp[:600],
        )

        mechanism = "tabs in code" if has_tabs else "mentions tabs preference"
        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Preference stored in conv {conv_a} ✓, "
                f"recalled in conv {conv_b} ✓ ({mechanism}) ✓"
            ),
        )
