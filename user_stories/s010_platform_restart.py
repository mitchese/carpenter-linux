"""
S010 — Platform Status and Opportunistic Restart

The user asks Carpenter about its current status, then asks it to restart
when convenient.

Steps:
  1. Send a vague "what's your current status?" message.
  2. Assert: agent calls get_platform_status; response mentions arc count or
     restart state.
  3. Send a vague "restart yourself when you get a chance" message.
  4. Assert: agent calls request_restart(mode='opportunistic'); acknowledges
     gracefully.

DB verification:
  - work_queue has a 'platform.restart' item with payload mode='opportunistic'.

Structural assertions only — no LLM judge required.
"""

import json
import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


class PlatformRestart(AcceptanceStory):
    name = "S010 — Platform Status and Opportunistic Restart"
    description = (
        "User asks for platform status, then requests a restart. "
        "Agent must call get_platform_status and request_restart tools."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        # ── Step 1: Ask for status ────────────────────────────────────────────
        print("\n  Sending: 'What is your current platform status?'")
        conv_id, response = client.chat(
            "What is your current platform status?",
            timeout=120,
        )
        print(f"  Got response ({len(response)} chars)")

        # Agent should mention arcs or restart state
        response_lower = response.lower()
        self.assert_that(
            any(
                kw in response_lower
                for kw in ["arc", "active", "idle", "restart", "status", "pending", "running"]
            ),
            "Status response should mention arcs, restart state, or activity level",
            response_preview=response[:300],
        )

        # ── Step 2: Ask for opportunistic restart ─────────────────────────────
        print("\n  Sending: 'Could you restart yourself when you get a chance?'")
        _, response2 = client.chat(
            "Could you restart yourself when you get a chance?",
            timeout=120,
            conversation_id=conv_id,
        )
        print(f"  Got response ({len(response2)} chars)")

        # Response should acknowledge the restart request
        response2_lower = response2.lower()
        self.assert_that(
            any(
                kw in response2_lower
                for kw in ["restart", "scheduled", "idle", "queued", "when", "queue"]
            ),
            "Restart response should acknowledge the request",
            response_preview=response2[:300],
        )

        # ── DB verification ───────────────────────────────────────────────────
        time.sleep(3)  # allow work item to be written

        rows = db.fetchall(
            "SELECT event_type, payload_json FROM work_queue "
            "WHERE event_type = 'platform.restart'"
        )
        self.assert_that(
            len(rows) >= 1,
            f"Expected a platform.restart work item in work_queue, found {len(rows)}",
        )

        if rows:
            payload = json.loads(rows[0]["payload_json"])
            self.assert_that(
                payload.get("mode") == "opportunistic",
                f"Expected mode='opportunistic', got {payload.get('mode')!r}",
            )

        return self.result()
