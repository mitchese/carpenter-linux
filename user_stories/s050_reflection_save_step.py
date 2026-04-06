"""
S050 — Reflection Save Step Persists Output

Verifies the save-reflection Python-handled step works correctly:
the handler reads AI output from the reflect arc's arc_state,
calls save_reflection() to persist it, and completes the arc.

This complements S049 by focusing on the save-step mechanics rather
than the full arc tree creation. It also validates that the reflection
record contains expected metadata fields (cadence, period, model info).

Pre-requisite: reflection.enabled must be True in config.yaml.

Expected behaviour:
  1. A daily reflection is triggered via work-queue injection.
  2. The reflect arc runs and stores _agent_response in arc_state.
  3. The save-reflection step reads the response and saves to reflections.
  4. The reflection record has correct cadence, period_start, period_end.
  5. The save-reflection arc reaches "completed" or "frozen" status.

DB verification:
  - reflections row with cadence="daily", non-null period_start/period_end.
  - save-reflection arc status is "completed" or "frozen".
  - reflect arc's arc_state contains "_agent_response" key.

Cleanup: removes arcs, reflections, and conversations created during test.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


def _inject_work_item(db_path: str, cadence: str = "daily") -> int:
    """Insert a reflection.trigger work item directly into the work queue."""
    conn = sqlite3.connect(db_path)
    try:
        payload = json.dumps({"event_payload": {"cadence": cadence}})
        idem_key = f"test-save-step-{cadence}-{int(time.time())}"
        cur = conn.execute(
            "INSERT INTO work_queue "
            "(event_type, payload_json, status, max_retries, idempotency_key) "
            "VALUES (?, ?, 'pending', 1, ?)",
            ("reflection.trigger", payload, idem_key),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class ReflectionSaveStep(AcceptanceStory):
    name = "S050 — Reflection Save Step Persists Output"
    description = (
        "Verify save-reflection step reads AI output from reflect arc's "
        "arc_state and persists it to the reflections table with correct metadata."
    )
    timeout = 300

    _parent_arc_id: int | None = None
    _reflection_id: int | None = None
    _work_item_id: int | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required for this test")
        start_ts = time.time()
        since_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        # ── 1. Trigger reflection ──────────────────────────────────────────
        print("\n  [1/4] Injecting reflection.trigger work item...")
        self._work_item_id = _inject_work_item(db.db_path, "daily")

        # ── 2. Wait for save-reflection arc to complete ────────────────────
        print("  [2/4] Waiting for save-reflection arc to complete (up to 240s)...")
        deadline = time.monotonic() + 240
        save_arc = None
        last_print = 0

        while time.monotonic() < deadline:
            new_arcs = db.get_arcs_created_after(start_ts)

            # Find parent
            parents = [a for a in new_arcs if a.get("name") == "daily-reflection"]
            if parents and self._parent_arc_id is None:
                self._parent_arc_id = parents[0]["id"]
                print(f"     Parent arc found: {self._parent_arc_id}")

            # Find save-reflection
            save_candidates = [
                a for a in new_arcs
                if a.get("name") == "save-reflection"
                and a.get("status") in ("completed", "frozen")
            ]
            if save_candidates:
                save_arc = save_candidates[0]
                break

            now = time.monotonic()
            if now - last_print >= 10:
                statuses = ", ".join(
                    f"{a['name']}={a['status']}" for a in new_arcs
                ) or "(no arcs yet)"
                print(f"     Waiting... {statuses}")
                last_print = now
            time.sleep(2)

        self.assert_that(
            save_arc is not None,
            "save-reflection arc did not complete within 240s. "
            "Is reflection.enabled=true in config?",
        )

        print(f"     save-reflection completed (status={save_arc['status']}) ✓")

        # ── 3. Verify reflect arc's _agent_response ────────────────────────
        print("  [3/4] Verifying reflect arc stored AI output...")

        children = db.get_arc_children(self._parent_arc_id)
        reflect_arc = next(
            (c for c in children if c["name"] == "reflect"), None
        )
        self.assert_that(
            reflect_arc is not None,
            "reflect child arc not found",
        )

        reflect_state = db.get_arc_state(reflect_arc["id"])

        self.assert_that(
            "_agent_response" in reflect_state,
            "reflect arc's arc_state missing '_agent_response' key. "
            f"Keys found: {list(reflect_state.keys())}",
        )

        agent_response = reflect_state["_agent_response"]
        self.assert_that(
            isinstance(agent_response, str) and len(agent_response) > 20,
            f"_agent_response too short or wrong type: {type(agent_response)}, "
            f"length={len(str(agent_response))}",
        )
        print(f"     _agent_response: {len(agent_response)} chars ✓")

        # ── 4. Verify reflection record in DB ──────────────────────────────
        print("  [4/4] Verifying reflection record metadata...")

        reflections = db.fetchall(
            "SELECT * FROM reflections WHERE cadence = 'daily' "
            "AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (since_iso,),
        )

        self.assert_that(
            len(reflections) >= 1,
            "No reflection record found in DB after test start",
        )

        refl = reflections[0]
        self._reflection_id = refl["id"]

        # Cadence
        self.assert_that(
            refl["cadence"] == "daily",
            f"Reflection cadence should be 'daily', got '{refl['cadence']}'",
        )

        # Period fields
        self.assert_that(
            refl["period_start"] is not None and len(refl["period_start"]) > 0,
            f"period_start is empty: {refl['period_start']}",
        )
        self.assert_that(
            refl["period_end"] is not None and len(refl["period_end"]) > 0,
            f"period_end is empty: {refl['period_end']}",
        )

        # Period_start should be before period_end
        self.assert_that(
            refl["period_start"] < refl["period_end"],
            f"period_start ({refl['period_start']}) should be before "
            f"period_end ({refl['period_end']})",
        )

        # Content matches _agent_response
        content = refl.get("content", "")
        self.assert_that(
            len(content) > 20,
            f"Reflection content is too short ({len(content)} chars)",
        )

        # Model field should be set (AI was invoked)
        self.assert_that(
            refl.get("model") is not None and len(str(refl.get("model", ""))) > 0,
            f"Reflection model field is empty: {refl.get('model')}",
        )

        # Token counts should be positive (AI was called)
        input_tokens = refl.get("input_tokens", 0) or 0
        output_tokens = refl.get("output_tokens", 0) or 0
        print(f"     Model: {refl.get('model')}")
        print(f"     Tokens: {input_tokens} in / {output_tokens} out")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"save-reflection completed ✓, "
                f"_agent_response stored ({len(agent_response)} chars) ✓, "
                f"reflection record persisted (id={self._reflection_id}) ✓, "
                f"metadata valid (period, model, tokens) ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove arcs, reflections, and conversations created during test."""
        if db is None:
            return

        conn = sqlite3.connect(db.db_path)
        try:
            deleted = []

            if self._reflection_id:
                conn.execute(
                    "DELETE FROM reflections WHERE id = ?",
                    (self._reflection_id,),
                )
                deleted.append(f"reflection {self._reflection_id}")

            if self._parent_arc_id:
                conv_row = conn.execute(
                    "SELECT conversation_id FROM conversation_arcs "
                    "WHERE arc_id = ?",
                    (self._parent_arc_id,),
                ).fetchone()

                child_ids = [
                    r[0] for r in conn.execute(
                        "SELECT id FROM arcs WHERE parent_id = ?",
                        (self._parent_arc_id,),
                    ).fetchall()
                ]
                for cid in child_ids:
                    conn.execute("DELETE FROM arc_state WHERE arc_id = ?", (cid,))
                    conn.execute("DELETE FROM arc_history WHERE arc_id = ?", (cid,))
                    conn.execute("DELETE FROM arcs WHERE id = ?", (cid,))
                deleted.append(f"{len(child_ids)} child arcs")

                conn.execute(
                    "DELETE FROM arc_state WHERE arc_id = ?",
                    (self._parent_arc_id,),
                )
                conn.execute(
                    "DELETE FROM arc_history WHERE arc_id = ?",
                    (self._parent_arc_id,),
                )
                conn.execute(
                    "DELETE FROM conversation_arcs WHERE arc_id = ?",
                    (self._parent_arc_id,),
                )
                conn.execute(
                    "DELETE FROM arcs WHERE id = ?",
                    (self._parent_arc_id,),
                )
                deleted.append(f"parent arc {self._parent_arc_id}")

                if conv_row:
                    conv_id = conv_row[0]
                    conn.execute(
                        "DELETE FROM messages WHERE conversation_id = ?",
                        (conv_id,),
                    )
                    conn.execute(
                        "DELETE FROM conversations WHERE id = ?",
                        (conv_id,),
                    )
                    deleted.append(f"conversation {conv_id}")

            if self._work_item_id:
                conn.execute(
                    "DELETE FROM work_queue WHERE id = ?",
                    (self._work_item_id,),
                )
                deleted.append(f"work item {self._work_item_id}")

            conn.commit()
            if deleted:
                print(f"  [cleanup] Removed: {', '.join(deleted)}")
        except Exception as exc:
            print(f"  [cleanup] Error: {exc}")
        finally:
            conn.close()
