"""
S003 — Agent Creates KB Skill Entry and Uses It

The user asks Carpenter to create a new knowledge base entry at
'skills/fibonacci-sequence' that documents how to compute Fibonacci numbers,
and save it permanently. In a follow-up message the user asks the agent to
navigate to that KB entry and use its content.

Expected behaviour:
  1. The agent saves the KB entry at skills/fibonacci-sequence (via submit_code).
  2. The agent acknowledges that the entry has been saved.
  3. In a follow-up message the agent uses kb_describe('skills/fibonacci-sequence').
  4. The agent uses the KB content to answer that fib(8) = 21.

DB verification checks:
  - A row exists in the kb_entries table with path='skills/fibonacci-sequence'.
"""

import sqlite3
import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_KB_PATH = "skills/fibonacci-sequence"

_CREATE_PROMPT = (
    "Please create a new knowledge base entry for yourself at "
    "'skills/fibonacci-sequence'. The entry should document how to compute "
    "Fibonacci numbers using the recurrence fib(n) = fib(n-1) + fib(n-2), "
    "with fib(0)=0 and fib(1)=1. Include the first ten values: "
    "0, 1, 1, 2, 3, 5, 8, 13, 21, 34. "
    "Save it permanently so you can reference it in future conversations."
)

_USE_PROMPT = (
    "Please navigate to your fibonacci-sequence knowledge base entry and "
    "tell me: what is fib(8)?"
)


class AddSkillAndUseIt(AcceptanceStory):
    name = "S003 — Agent Creates KB Skill Entry and Uses It"
    description = (
        "User asks agent to create a fibonacci-sequence KB entry, "
        "then navigates to and uses it in a follow-up message; verifies DB KB record."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── Step 1: Ask agent to create the KB entry ──────────────────────────
        print(f"\n  Sending KB-creation request for '{_KB_PATH}'...")
        conv_id = client.create_conversation()
        client.send_message(_CREATE_PROMPT, conv_id)

        print("  Waiting for KB creation to complete (up to 90s)...")
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        create_msgs = client.get_assistant_messages(conv_id)
        print(f"  Got {len(create_msgs)} assistant message(s) after KB creation")

        self.assert_that(
            len(create_msgs) >= 1,
            "No assistant response after KB-creation request",
            conversation_id=conv_id,
        )

        create_response = create_msgs[-1]["content"]
        print(f"  Response preview: {create_response[:150]}")

        # Agent should acknowledge the KB entry was created or added.
        self.assert_that(
            any(
                kw in create_response.lower()
                for kw in ("fibonacci", "knowledge", "kb", "created", "saved", "added", "add")
            ),
            "Create response does not acknowledge KB creation",
            response_preview=create_response[:400],
        )

        # Agent should NOT be reporting a failure.
        self.assert_that(
            not any(
                kw in create_response.lower()
                for kw in ("failed", "error", "not saved", "could not save", "unable")
            ),
            "Create response indicates KB entry creation failed",
            response_preview=create_response[:400],
        )

        # ── Step 2: Ask agent to navigate to and use the KB entry ─────────────
        print("  Sending KB-use request...")
        client.send_message(_USE_PROMPT, conv_id)

        print("  Waiting for KB-use response (up to 90s)...")
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        all_msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(all_msgs) >= 2,
            f"Expected ≥2 assistant messages after follow-up, got {len(all_msgs)}",
            conversation_id=conv_id,
        )

        use_response = all_msgs[-1]["content"]
        print(f"  Use response preview: {use_response[:150]}")

        # Agent should give the correct answer: fib(8) = 21.
        self.assert_that(
            "21" in use_response,
            "Response does not contain the correct answer fib(8) = 21",
            response_preview=use_response[:400],
        )

        # ── DB assertions ─────────────────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message="Behavioural checks passed (no DB path configured; skipping DB checks)",
            )

        kb_entries = db.get_kb_entries(path_prefix=_KB_PATH)
        self.assert_that(
            len(kb_entries) >= 1,
            f"Expected a KB entry at '{_KB_PATH}' in the kb_entries table, found none",
            all_kb_entries=[e["path"] for e in db.get_kb_entries(path_prefix="skills/")],
        )

        entry = kb_entries[0]
        print(f"  DB: KB entry found at path={entry.get('path')}")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"KB entry '{_KB_PATH}' created and written to DB ✓, "
                f"navigated and used in follow-up ✓, "
                f"fib(8)=21 confirmed ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: DBInspector) -> None:
        """Remove the fibonacci-sequence KB entry from the DB and disk."""
        if db is None:
            return

        # Delete from DB (needs a read-write connection; DBInspector is read-only).
        try:
            conn = sqlite3.connect(db.db_path)
            try:
                conn.execute("DELETE FROM kb_entries WHERE path = ?", (_KB_PATH,))
                conn.execute("DELETE FROM kb_links WHERE source_path = ?", (_KB_PATH,))
                conn.commit()
                print(f"  [cleanup] Removed '{_KB_PATH}' from kb_entries table")
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [cleanup] DB cleanup failed: {exc}")

        # Also remove the KB file on disk so autogen doesn't re-create the
        # DB entry on next server restart.
        import os
        base_dir = os.path.dirname(os.path.dirname(db.db_path))
        kb_file = os.path.join(base_dir, "config", "kb", _KB_PATH + ".md")
        try:
            if os.path.exists(kb_file):
                os.remove(kb_file)
                print(f"  [cleanup] Removed KB file: {kb_file}")
        except Exception as exc:
            print(f"  [cleanup] KB file cleanup failed: {exc}")
