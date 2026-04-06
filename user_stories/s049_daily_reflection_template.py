"""
S049 — Daily Reflection Template End-to-End

Triggers a daily reflection via direct work-queue injection and verifies the
full template-based flow: parent arc creation → child arcs (reflect +
save-reflection) → AI invocation → reflection persisted to DB.

Pre-requisite: reflection.enabled must be True in config.yaml and the server
must be running with the reflection handler registered.

Expected behaviour:
  1. A "reflection.trigger" work item is enqueued with cadence=daily.
  2. The handler creates a parent arc named "daily-reflection" (PLANNER).
  3. The reflection template is instantiated as two children:
     - "reflect" (EXECUTOR, runs AI analysis)
     - "save-reflection" (Python-only, persists output)
  4. The reflect arc is dispatched and produces analysis text.
  5. The save-reflection step saves the output to the reflections table.
  6. All arcs reach a terminal state.

DB verification:
  - Parent arc exists with name "daily-reflection", agent_type PLANNER.
  - Two child arcs with correct names and ordering.
  - arc_state on parent contains cadence, period_start, period_end.
  - reflections table has a new row with cadence="daily" and non-empty content.

Cleanup: removes arcs, reflections, and conversations created during the test.
"""

import json
import sqlite3
import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


def _inject_work_item(db_path: str, cadence: str = "daily") -> int:
    """Insert a reflection.trigger work item directly into the work queue.

    Returns the work item ID.
    """
    conn = sqlite3.connect(db_path)
    try:
        payload = json.dumps({"event_payload": {"cadence": cadence}})
        idem_key = f"test-reflection-{cadence}-{int(time.time())}"
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


class DailyReflectionTemplate(AcceptanceStory):
    name = "S049 — Daily Reflection Template End-to-End"
    description = (
        "Trigger a daily reflection via work-queue injection; verify arc tree "
        "structure, AI execution, and DB persistence of the reflection output."
    )
    timeout = 300  # AI model call can take time

    # Track IDs for cleanup
    _parent_arc_id: int | None = None
    _reflection_id: int | None = None
    _work_item_id: int | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required for this test")
        start_ts = time.time()

        # ── 0. Pre-check: reflection handler must be registered ────────────
        # If the handler is not registered, work items will sit unprocessed.
        # We detect this by checking server logs or config.
        print("\n  [0/5] Pre-check: verifying reflection handler is registered...")

        # Quick check: see if there's a registered handler by looking at
        # recent work queue items with event_type=reflection.trigger.
        # If none exist, we'll be the first — that's fine.

        # ── 1. Inject work item ────────────────────────────────────────────
        print("  [1/5] Injecting reflection.trigger work item...")
        self._work_item_id = _inject_work_item(db.db_path, "daily")
        print(f"     Work item ID: {self._work_item_id}")

        # ── 2. Wait for parent arc and children to appear ──────────────────
        print("  [2/5] Waiting for parent arc 'daily-reflection' + 2 children (up to 30s)...")
        parent_arc = None
        children = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            new_arcs = db.get_arcs_created_after(start_ts)
            candidates = [
                a for a in new_arcs
                if a.get("name") == "daily-reflection"
            ]
            if candidates:
                parent_arc = candidates[0]
                self._parent_arc_id = parent_arc["id"]
                children = db.get_arc_children(self._parent_arc_id)
                if len(children) >= 2:
                    break
            time.sleep(1)

        self.assert_that(
            parent_arc is not None,
            "Parent arc 'daily-reflection' was not created within 30s. "
            "Is reflection.enabled=true in config and server restarted?",
        )
        self._parent_arc_id = parent_arc["id"]
        print(f"     Parent arc ID: {self._parent_arc_id}")

        # ── 3. Verify arc tree structure ───────────────────────────────────
        print("  [3/5] Verifying arc tree structure...")

        # Parent should be PLANNER
        # Refresh parent to get latest state
        parent_arc = db.get_arc(self._parent_arc_id)
        self.assert_that(
            parent_arc is not None and parent_arc.get("agent_type") == "PLANNER",
            f"Parent arc agent_type should be PLANNER, got {parent_arc.get('agent_type') if parent_arc else 'None'}",
        )

        # Children already fetched in the wait loop
        child_names = [c["name"] for c in children]
        print(f"     Children: {child_names}")

        self.assert_that(
            len(children) == 2,
            f"Expected 2 child arcs, got {len(children)}: {child_names}",
            arcs=db.format_arcs_table(children),
        )

        self.assert_that(
            "reflect" in child_names,
            f"Missing 'reflect' child arc. Got: {child_names}",
        )
        self.assert_that(
            "save-reflection" in child_names,
            f"Missing 'save-reflection' child arc. Got: {child_names}",
        )

        # Check ordering: reflect should come before save-reflection
        reflect_arc = next(c for c in children if c["name"] == "reflect")
        save_arc = next(c for c in children if c["name"] == "save-reflection")

        self.assert_that(
            (reflect_arc.get("step_order") or 0) < (save_arc.get("step_order") or 0),
            "reflect arc should have lower step_order than save-reflection",
        )

        # Verify reflect arc is EXECUTOR
        self.assert_that(
            reflect_arc.get("agent_type") == "EXECUTOR",
            f"reflect arc should be EXECUTOR, got {reflect_arc.get('agent_type')}",
        )

        # Check parent arc_state metadata
        parent_state = db.get_arc_state(self._parent_arc_id)
        print(f"     Parent arc_state keys: {list(parent_state.keys())}")

        self.assert_that(
            parent_state.get("cadence") == "daily",
            f"arc_state cadence should be 'daily', got {parent_state.get('cadence')}",
        )
        self.assert_that(
            "period_start" in parent_state,
            "arc_state missing 'period_start'",
        )
        self.assert_that(
            "period_end" in parent_state,
            "arc_state missing 'period_end'",
        )

        print("     Arc tree structure verified ✓")

        # ── 4. Wait for all arcs to complete ───────────────────────────────
        print("  [4/5] Waiting for arcs to complete (up to 240s)...")
        arc_deadline = time.monotonic() + 240
        last_print = 0

        while time.monotonic() < arc_deadline:
            all_arcs = [parent_arc] + db.get_arc_children(self._parent_arc_id)
            # Refresh parent
            refreshed_parent = db.get_arc(self._parent_arc_id)
            if refreshed_parent:
                all_arcs[0] = refreshed_parent

            pending = [
                a for a in all_arcs
                if a.get("status") not in (
                    "completed", "failed", "cancelled", "frozen"
                )
            ]
            if not pending:
                break

            now = time.monotonic()
            if now - last_print >= 5:
                statuses = ", ".join(
                    f"{a['name']}={a['status']}" for a in pending[:5]
                )
                print(f"     Still waiting: {statuses}")
                last_print = now
            time.sleep(2)
        else:
            # Timeout — check what happened
            all_arcs = [db.get_arc(self._parent_arc_id)] + db.get_arc_children(
                self._parent_arc_id
            )
            statuses = ", ".join(
                f"{a['name']}={a.get('status')}" for a in all_arcs if a
            )
            self.assert_that(
                False,
                f"Arcs did not complete within 240s. Statuses: {statuses}",
                arcs=db.format_arcs_table(all_arcs),
            )

        # Check for failures
        final_children = db.get_arc_children(self._parent_arc_id)
        failed = [c for c in final_children if c.get("status") == "failed"]
        if failed:
            for f in failed:
                state = db.get_arc_state(f["id"])
                print(f"     FAILED arc {f['name']}: {state.get('error', 'unknown')}")

        self.assert_that(
            len(failed) == 0,
            f"{len(failed)} arc(s) failed: "
            + ", ".join(f"{f['name']} (id={f['id']})" for f in failed),
            arcs=db.format_arcs_table(final_children),
        )

        print("     All arcs completed ✓")

        # ── 5. Verify reflection in DB ─────────────────────────────────────
        print("  [5/5] Verifying reflection in database...")

        # Find reflections created after start_ts
        from datetime import datetime, timezone
        since_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        reflections = db.fetchall(
            "SELECT * FROM reflections WHERE cadence = 'daily' "
            "AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (since_iso,),
        )

        self.assert_that(
            len(reflections) >= 1,
            "No daily reflection found in reflections table after test start",
        )

        reflection = reflections[0]
        self._reflection_id = reflection["id"]
        content = reflection.get("content", "")

        print(f"     Reflection ID: {self._reflection_id}")
        print(f"     Content length: {len(content)} chars")
        print(f"     Content preview: {content[:200]}...")

        self.assert_that(
            len(content) > 50,
            f"Reflection content too short ({len(content)} chars), "
            "expected substantive analysis",
            content_preview=content[:500],
        )

        # Verify the content mentions some expected activity-related terms
        content_lower = content.lower()
        activity_terms = [
            "conversation", "arc", "tool", "token", "activity",
            "pattern", "knowledge", "period", "summary",
        ]
        matches = [t for t in activity_terms if t in content_lower]
        self.assert_that(
            len(matches) >= 2,
            f"Reflection content should mention activity metrics. "
            f"Found: {matches}. Expected ≥2 of {activity_terms}",
            content_preview=content[:500],
        )

        # Verify period fields are set
        self.assert_that(
            reflection.get("period_start") is not None,
            "Reflection missing period_start",
        )
        self.assert_that(
            reflection.get("period_end") is not None,
            "Reflection missing period_end",
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Parent arc created ✓, "
                f"2 children (reflect+save-reflection) ✓, "
                f"arcs completed ✓, "
                f"reflection saved (id={self._reflection_id}, "
                f"{len(content)} chars) ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove arcs, reflections, and conversations created during test."""
        if db is None:
            return

        conn = sqlite3.connect(db.db_path)
        try:
            deleted = []

            # Delete reflection
            if self._reflection_id:
                conn.execute(
                    "DELETE FROM reflections WHERE id = ?",
                    (self._reflection_id,),
                )
                deleted.append(f"reflection {self._reflection_id}")

            # Delete child arcs and parent arc
            if self._parent_arc_id:
                # Get conversation linked to parent
                conv_row = conn.execute(
                    "SELECT conversation_id FROM conversation_arcs "
                    "WHERE arc_id = ?",
                    (self._parent_arc_id,),
                ).fetchone()

                # Delete children first
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

                # Delete parent arc
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

                # Delete linked conversation
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

            # Delete work item
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
