"""
S043 — Smoke Test: API Health and Auth

Lightweight smoke test for any running Carpenter instance. Verifies:
  1. HTTP reachability (server responds to a GET)
  2. Token auth middleware (401 without token, 200 with)
  3. Chat round-trip (trivial prompt, any non-empty response)
  4. Conversation appears in history
  5. No arcs created (trivial prompt should be pure chat)

This is not Docker-specific — it fills a gap in the story suite by providing
a fast sanity check that any instance (bare-metal, Docker, or fresh install)
has its basic HTTP + AI pipeline working.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

import httpx


class SmokeTestApiHealth(AcceptanceStory):
    name = "S043 — Smoke Test: API Health and Auth"
    description = (
        "Verify HTTP reachability, token auth, chat round-trip, "
        "and conversation persistence in history."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. HTTP reachability ────────────────────────────────────────
        try:
            r = httpx.get(f"{client.base_url}/", timeout=10, follow_redirects=False)
            self.assert_that(
                r.status_code in (200, 302, 401),
                f"Unexpected status {r.status_code} from GET /",
            )
            print(f"\n  Server reachable")
        except httpx.ConnectError:
            self.assert_that(False, f"Cannot connect to {client.base_url}")

        # ── 2. Auth middleware ──────────────────────────────────────────
        # Without token -> 401
        no_auth = httpx.get(f"{client.base_url}/api/chat/history", timeout=10)
        self.assert_that(
            no_auth.status_code == 401,
            f"Expected 401 without token, got {no_auth.status_code}",
        )
        print(f"  Auth: 401 without token (auth enforced)")

        # With token -> 200
        with_auth = client._get("/api/chat/history")
        self.assert_that(
            with_auth.status_code == 200,
            f"Expected 200 with token, got {with_auth.status_code}",
        )
        print(f"  Auth: 200 with token")

        # ── 3. Chat round-trip ──────────────────────────────────────────
        conv_id, response = client.chat("Say hello.", timeout=60)
        self.assert_that(
            len(response) > 0,
            "Empty response from chat",
        )
        print(f"  Chat: response received ({len(response)} chars), conversation {conv_id}")

        # ── 4. Conversation in history ──────────────────────────────────
        history = client._get(f"/api/chat/history?conversation_id={conv_id}")
        messages = history.json().get("messages", [])
        self.assert_that(
            len(messages) >= 2,
            f"Expected at least 2 messages in history, got {len(messages)}",
        )
        print(f"  History: {len(messages)} messages (user + assistant)")

        # ── 5. DB verification ──────────────────────────────────────────
        if db is not None:
            new_arcs = db.get_arcs_created_after(start_ts)
            self.assert_that(
                len(new_arcs) == 0,
                f"Expected 0 arcs for trivial prompt, got {len(new_arcs)}",
            )
            print(f"  DB: no arcs created (correct for trivial prompt)")

            msgs = db.get_messages(conv_id)
            self.assert_that(
                len(msgs) >= 2,
                f"Expected at least 2 messages in DB, got {len(msgs)}",
            )
            print(f"  DB: {len(msgs)} messages in conversation {conv_id}")

        duration = time.time() - start_ts
        return StoryResult(
            name=self.name,
            passed=True,
            message=f"server=OK, auth=OK, chat=OK, history=OK ({duration:.1f}s)",
            duration_s=duration,
        )
