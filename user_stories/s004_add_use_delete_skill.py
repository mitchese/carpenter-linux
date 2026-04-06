"""
S004 — Agent Creates, Uses, and Then Deletes KB Skill Entry

Extends S003: after creating and using the fibonacci-sequence KB entry the user
asks the agent to delete it permanently. A follow-on message then asks the
agent to verify deletion via kb_describe('skills').

Expected behaviour:
  1. Agent creates fibonacci-sequence KB entry (via submit_code).
  2. Agent navigates to the entry and answers fib(8) = 21.
  3. Agent deletes the KB entry (via submit_code).
  4. Agent reports success; kb_describe('skills') confirms the entry is gone.

DB verification checks:
  - After step 3: no row in kb_entries table with path='skills/fibonacci-sequence'.
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
    "'skills/fibonacci-sequence'. The entry should document the recurrence "
    "fib(n) = fib(n-1) + fib(n-2) with fib(0)=0 and fib(1)=1, and include "
    "the first ten values: 0, 1, 1, 2, 3, 5, 8, 13, 21, 34. "
    "Save it permanently."
)

_USE_PROMPT = (
    "Navigate to your fibonacci-sequence knowledge base entry and tell me: "
    "what is fib(8)?"
)

_DELETE_PROMPT = (
    "The fibonacci-sequence knowledge base entry is no longer needed. "
    "Please delete it permanently."
)

_CONFIRM_PROMPT = (
    "Please use kb_describe('skills') and confirm whether fibonacci-sequence "
    "is still available."
)


class AddUseAndDeleteSkill(AcceptanceStory):
    name = "S004 — Agent Creates, Uses, and Then Deletes KB Skill Entry"
    description = (
        "Agent creates fibonacci-sequence, uses it, deletes it, "
        "then confirms deletion with kb_describe and DB inspection."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Create the KB entry ────────────────────────────────────────────
        print(f"\n  [1/4] Creating '{_KB_PATH}'...")
        conv_id = client.create_conversation()
        client.send_message(_CREATE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(len(msgs) >= 1, "No response after KB creation", conversation_id=conv_id)
        create_resp = msgs[-1]["content"]
        print(f"     {create_resp[:120]}")

        self.assert_that(
            any(kw in create_resp.lower() for kw in
                ("fibonacci", "knowledge", "kb", "created", "saved", "added", "add")),
            "Create response does not acknowledge KB creation",
            response_preview=create_resp[:400],
        )
        self.assert_that(
            not any(kw in create_resp.lower() for kw in
                    ("failed", "error", "not saved", "could not save", "unable")),
            "Create response indicates KB entry creation failed",
            response_preview=create_resp[:400],
        )

        # ── 2. Use the KB entry ───────────────────────────────────────────────
        print(f"  [2/4] Using KB entry — asking fib(8)...")
        client.send_message(_USE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        use_resp = msgs[-1]["content"]
        self.assert_that(
            "21" in use_resp,
            "Use response does not contain fib(8) = 21",
            response_preview=use_resp[:400],
        )
        print(f"     fib(8)=21 ✓")

        # ── 3. Delete the KB entry via kb_delete tool ─────────────────────────
        print(f"  [3/4] Asking agent to delete KB entry...")
        client.send_message(_DELETE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        del_resp = msgs[-1]["content"]
        print(f"     {del_resp[:150]}")

        self.assert_that(
            any(kw in del_resp.lower() for kw in
                ("delet", "remov", "gone", "done", "success", "executed", "complet")),
            "Delete response does not indicate deletion was attempted",
            response_preview=del_resp[:400],
        )

        # ── 4. Confirm via kb_describe ─────────────────────────────────────────
        print(f"  [4/4] Confirming KB entry is gone via kb_describe...")
        client.send_message(_CONFIRM_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        confirm_resp = msgs[-1]["content"]
        print(f"     {confirm_resp[:150]}")

        # Either "fibonacci" is absent entirely, or it is mentioned alongside
        # a negation ("not available", "no longer", "deleted", etc.)
        self.assert_that(
            "fibonacci" not in confirm_resp.lower()
            or any(kw in confirm_resp.lower() for kw in
                   ("not", "no longer", "gone", "delet", "remov",
                    "unavailable", "don't see", "doesn't appear", "does not appear")),
            "Confirmation response still shows fibonacci-sequence as available",
            response_preview=confirm_resp[:400],
        )

        # ── DB assertions ─────────────────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name, passed=True,
                message="Behavioural checks passed (no DB path configured)",
            )

        remaining = db.get_kb_entries(path_prefix=_KB_PATH)
        self.assert_that(
            len(remaining) == 0,
            f"KB entry '{_KB_PATH}' still exists in DB after deletion",
            entry=remaining[0] if remaining else None,
            all_kb_entries=[e["path"] for e in db.get_kb_entries(path_prefix="skills/")],
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"KB entry created ✓, fib(8)=21 confirmed ✓, "
                f"deleted ✓, kb_describe confirms gone ✓, "
                f"DB row absent ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: DBInspector) -> None:
        """Remove any residual KB entry if the test failed before the deletion step."""
        if db is None:
            return
        try:
            conn = sqlite3.connect(db.db_path)
            try:
                conn.execute("DELETE FROM kb_entries WHERE path = ?", (_KB_PATH,))
                conn.execute("DELETE FROM kb_links WHERE source_path = ?", (_KB_PATH,))
                conn.commit()
                print(f"  [cleanup] Removed '{_KB_PATH}' from DB")
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
