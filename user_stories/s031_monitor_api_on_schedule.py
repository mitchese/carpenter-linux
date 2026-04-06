"""
S031 — Monitor API Endpoint on Schedule

The user asks Carpenter to check httpbin.org/status/200 every 30 seconds
and notify if the status changes. The agent sets up a cron schedule with
tainted fetch and notification. After a 2-minute wait, the story verifies
that monitoring messages arrived.

Pattern: follows s013's cron setup/cancel pattern with tainted fetch added.

Expected behaviour:
  1. User: "check httpbin.org/status/200 every 30 seconds, tell me if status
     changes."
  2. Agent creates a cron entry that fires every 30 seconds.
  3. Tainted arc fetches the URL on each fire.
  4. At least 2 monitoring reports arrive within 2 minutes.
  5. User cancels the monitoring.
  6. Agent removes the cron entry.

Cleanup: removes any monitoring cron entries.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_CRON_NAME_PREFIX = "s031-monitor"

_SETUP_PROMPT = (
    "Please monitor the API endpoint https://httpbin.org/status/200 "
    "every 30 seconds. Check its status and send me a message in this "
    "conversation each time you check. Let me know if anything changes."
)

_CANCEL_PROMPT = (
    "Please stop monitoring that API endpoint. Cancel the recurring check."
)

_WAIT_SECONDS = 2 * 60  # 2 minutes for at least 2 checks at 30-sec intervals


class MonitorAPIOnSchedule(AcceptanceStory):
    name = "S031 — Monitor API Endpoint on Schedule"
    description = (
        "User asks agent to check httpbin every 30 seconds; cron + tainted "
        "fetch + notification; 2min wait; cleanup removes cron."
    )
    timeout = 240  # Needs 90s setup + 120s monitoring wait + 30s cancel

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Set up monitoring ──────────────────────────────────────────────
        print("\n  [1/4] Setting up API monitoring...")
        conv_id = client.create_conversation()
        client.send_message(_SETUP_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        setup_msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(setup_msgs) >= 1,
            "No acknowledgement after monitoring setup request",
            conversation_id=conv_id,
        )
        ack = setup_msgs[-1]["content"]
        print(f"     Ack: {ack[:200]}")

        self.assert_that(
            any(kw in ack.lower() for kw in
                ("monitor", "check", "every", "30 second", "schedule",
                 "httpbin", "recurring", "set up", "will check")),
            "Acknowledgement does not indicate monitoring was set up",
            response_preview=ack[:500],
        )

        # ── 2. Wait for monitoring reports ────────────────────────────────────
        baseline_count = len(client.get_history(conv_id))
        print(f"  [2/4] Waiting up to {_WAIT_SECONDS}s for monitoring reports...")

        deadline = time.monotonic() + _WAIT_SECONDS
        monitor_msgs = []
        while time.monotonic() < deadline:
            all_msgs = client.get_history(conv_id)
            new_msgs = all_msgs[baseline_count:]
            monitor_msgs = [
                m for m in new_msgs
                if (m.get("role") in ("assistant", "system")
                    and any(kw in m.get("content", "").lower() for kw in
                            ("status", "200", "httpbin", "check", "monitor",
                             "ok", "available", "up", "running")))
            ]
            if len(monitor_msgs) >= 2:
                break
            time.sleep(20)

        elapsed = time.time() - start_ts
        print(f"     Elapsed: {elapsed:.0f}s, reports: {len(monitor_msgs)}")

        self.assert_that(
            len(monitor_msgs) >= 2,
            f"Expected >=2 monitoring reports within {_WAIT_SECONDS}s, "
            f"got {len(monitor_msgs)}",
            conversation_id=conv_id,
            monitor_msgs=[m.get("content", "")[:100] for m in monitor_msgs],
        )

        for i, m in enumerate(monitor_msgs[:3], 1):
            print(f"     Report {i}: {m['content'][:120]}")

        # ── 3. Cancel monitoring ──────────────────────────────────────────────
        print("  [3/4] Cancelling monitoring...")
        client.send_message(_CANCEL_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        cancel_msgs = client.get_assistant_messages(conv_id)
        cancel_reply = cancel_msgs[-1]["content"] if cancel_msgs else ""
        print(f"     Cancel: {cancel_reply[:200]}")

        self.assert_that(
            any(kw in cancel_reply.lower() for kw in
                ("stop", "cancel", "remov", "no longer", "monitor",
                 "done", "disabled")),
            "Cancel reply does not confirm monitoring stopped",
            response_preview=cancel_reply[:500],
        )

        # ── 4. DB assertions ─────────────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message=f"Behavioural: {len(monitor_msgs)} reports ✓, cancelled ✓",
            )

        print("  [4/4] Verifying cron cleanup in DB...")
        leftover = db._query(
            "SELECT * FROM cron_entries WHERE name LIKE ? AND enabled = 1",
            (f"{_CRON_NAME_PREFIX}%",),
        )
        # Also check for any httpbin-related crons
        httpbin_crons = db._query(
            "SELECT * FROM cron_entries WHERE name LIKE '%httpbin%' "
            "AND enabled = 1"
        )
        leftover.extend(httpbin_crons)

        self.assert_that(
            len(leftover) == 0,
            f"Monitoring cron still active ({len(leftover)} entries)",
            crons=leftover,
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{len(monitor_msgs)} monitoring reports ✓, "
                f"cancelled ✓, "
                f"no leftover cron ✓ "
                f"({elapsed:.0f}s total)"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove any monitoring cron entries."""
        if db is None:
            return
        import sqlite3
        try:
            conn = sqlite3.connect(db.db_path)
            try:
                cur = conn.execute(
                    "DELETE FROM cron_entries WHERE name LIKE ? "
                    "OR name LIKE '%httpbin%' OR name LIKE '%monitor%'",
                    (f"{_CRON_NAME_PREFIX}%",),
                )
                if cur.rowcount:
                    print(f"  [cleanup] Removed {cur.rowcount} monitoring cron(s)")
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [cleanup] Cron cleanup failed: {exc}")
