"""
S002 — Workflow With Three Arc Messages

The user asks Carpenter to create a 3-step workflow. Each step should
send a chat message describing one of Carpenter's tools.

Expected behaviour:
  1. Carpenter creates arcs via submit_code — either a parent arc with
     3 child arcs, or 3 independent flat arcs.
  2. Arc auto-dispatch triggers each arc to run.
  3. Each arc calls messaging.send to post a message into the conversation.
  4. The user sees >=4 total assistant messages:
       - the initial planning acknowledgement
       - 3 tool description messages (one per arc)
  5. All 3 workflow arcs reach status='completed'.

DB verification checks:
  - At least 3 new arcs exist after the test started.
  - The arcs are either children of a parent or independent workflow arcs.
  - All workflow arcs have status='completed'.
  - The conversation contains >=3 messages with arc_id set (from arc executors).
  - Those messages mention tool names or descriptions.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

# Known Carpenter tool names used to verify message content
_KNOWN_TOOLS = [
    "submit_code",
    "list_arcs",
    "get_arc_detail",
    "get_state",
    "read_file",
    "list_files",
    "kb_describe",
    "kb_search",
    "kb.add",
    "kb.edit",
    "messaging",
    "state",
    "arc",
    "tool",
]

_PROMPT = (
    "Please create exactly 3 separate arcs for me. "
    "Each arc should use the messaging tool to send me a chat message "
    "that describes one of your available tools in a sentence or two — "
    "each arc should describe a different tool. "
    "You MUST create 3 individual arcs (not 1 or 2). "
    "Use submit_code to create them and let them run automatically."
)


class WorkflowThreeMessages(AcceptanceStory):
    name = "S002 — Workflow With Three Arc Messages"
    description = (
        "User requests a 3-step workflow where each arc sends a tool description "
        "as a chat message; verifies arc creation, auto-dispatch, and message delivery."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── Act: send the request ─────────────────────────────────────────────
        print(f"\n  Sending workflow request...")
        conv_id = client.create_conversation()
        client.send_message(_PROMPT, conv_id)

        # Wait for the planning phase to complete (arcs created, initial response)
        print("  Waiting for planning response (up to 60s)...")
        client.wait_for_pending_to_clear(conv_id, timeout=60)
        init_msgs = client.get_assistant_messages(conv_id)
        print(f"  Planning response received ({len(init_msgs)} assistant msg(s) so far)")

        self.assert_that(
            len(init_msgs) >= 1,
            "No assistant response received after planning phase",
            conversation_id=conv_id,
        )

        # ── Wait for arc messages ─────────────────────────────────────────────
        # Use the planning message count as baseline — Haiku may produce
        # several messages during planning.  We need 3 MORE (one per arc).
        baseline = len(init_msgs)
        target_n = baseline + 3
        print(f"  Waiting for 3 arc messages (baseline={baseline}, target={target_n}, up to 120s)...")
        all_msgs = client.wait_for_n_assistant_messages(conv_id, n=target_n, timeout=120)
        print(f"  Got {len(all_msgs)} assistant messages total")

        # ── Behavioral assertions ─────────────────────────────────────────────
        # The arc messages are those after the planning messages
        arc_messages = all_msgs[baseline:]

        self.assert_that(
            len(arc_messages) >= 3,
            f"Expected ≥3 arc messages, got {len(arc_messages)} (baseline={baseline})",
            conversation_id=conv_id,
            messages=[(m.get("content", "")[:100]) for m in all_msgs],
        )

        # At least 2 of the first 3 arc messages should mention a tool name.
        # We use arc_messages from DB (messages with arc_id set) for this check
        # because the HTTP history mixes main-agent and arc messages.
        arc_db_msgs = db.get_arc_messages(conv_id) if db else []
        check_msgs = arc_db_msgs if arc_db_msgs else arc_messages[:3]
        tool_refs = sum(
            1
            for m in check_msgs[:3]
            if any(t in m.get("content", "").lower() for t in _KNOWN_TOOLS)
        )
        self.assert_that(
            tool_refs >= 2,
            f"Only {tool_refs}/3 arc-executor messages mention a known tool; "
            f"expected ≥2",
            arc_messages=[m.get("content", "")[:120] for m in check_msgs[:3]],
            all_assistant_messages=[
                m.get("content", "")[:80] for m in all_msgs
            ],
        )

        # ── DB assertions ─────────────────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message="Behavioral checks passed (no DB path configured; skipping DB checks)",
            )

        # Wait for arcs created after start_ts to settle (all reach a terminal state).
        # This handles the case where the test races ahead of arc execution.
        print("\n  Waiting for arcs to complete (up to 90s)...")
        arc_settle_deadline = time.monotonic() + 90
        poll_interval = 0.5  # Faster polling for arc completion (arcs dispatch quickly)
        last_print = 0  # Throttle debug output

        while time.monotonic() < arc_settle_deadline:
            new_arcs = db.get_arcs_created_after(start_ts)
            pending = [
                a for a in new_arcs
                if a.get("status") not in ("completed", "failed", "cancelled")
            ]
            if not pending:
                break
            # Print status at most every 2 seconds to avoid spam
            now = time.monotonic()
            if now - last_print >= 2:
                print(f"    {len(pending)} arc(s) still running: "
                      + ", ".join(f"{a['id']}({a['status']})" for a in pending[:5]))
                last_print = now
            time.sleep(poll_interval)
        else:
            # Timeout settling — we'll report the current state as a failure below
            pass

        new_arcs = db.get_arcs_created_after(start_ts)
        print(f"  DB: {len(new_arcs)} new arc(s) found after test start")

        self.assert_that(
            len(new_arcs) >= 3,
            f"Expected ≥3 new arcs, found {len(new_arcs)}",
            arcs=db.format_arcs_table(new_arcs),
        )

        # Identify workflow arcs: use arc messages in our conversation to
        # find which arcs belong to this test (filters out reflection arcs
        # or other background arcs that may run concurrently).
        arc_msgs_for_filter = db.get_arc_messages(conv_id)
        conv_arc_ids = {m["arc_id"] for m in arc_msgs_for_filter if m.get("arc_id")}

        if len(conv_arc_ids) >= 3:
            # Use conversation-linked arcs as the primary filter
            child_arcs = [a for a in new_arcs if a["id"] in conv_arc_ids]
        else:
            # Fall back: find arcs structurally (parent-child or flat)
            new_arc_ids = {a["id"] for a in new_arcs}
            child_arcs = [
                a for a in new_arcs if a.get("parent_id") in new_arc_ids
            ]
            if not child_arcs:
                child_arcs = [a for a in new_arcs if a.get("parent_id") is not None]
            if not child_arcs:
                workflow_arcs = [
                    a for a in new_arcs
                    if a.get("agent_type") == "EXECUTOR"
                ]
                if len(workflow_arcs) < 3:
                    workflow_arcs = new_arcs
                child_arcs = workflow_arcs

        self.assert_that(
            len(child_arcs) >= 3,
            f"Expected >=3 workflow arcs, found {len(child_arcs)} "
            f"(conv arc ids: {sorted(conv_arc_ids)})",
            arcs=db.format_arcs_table(new_arcs),
        )

        # All workflow arcs must be completed
        incomplete = [
            a for a in child_arcs[:3] if a.get("status") != "completed"
        ]
        self.assert_that(
            len(incomplete) == 0,
            f"{len(incomplete)} workflow arc(s) are not completed: "
            + ", ".join(
                f"arc {a['id']} status={a['status']}" for a in incomplete
            ),
            arcs=db.format_arcs_table(child_arcs),
        )

        # Messages with arc_id set in our conversation
        arc_msgs = db.get_arc_messages(conv_id)
        print(f"  DB: {len(arc_msgs)} message(s) with arc_id in conversation {conv_id}")

        self.assert_that(
            len(arc_msgs) >= 3,
            f"Expected ≥3 arc-originated messages in conversation, got {len(arc_msgs)}",
            messages=db.format_messages_table(
                db.get_messages(conv_id)
            ),
        )

        # Verify the arc messages came from distinct arcs
        arc_ids_in_messages = {m["arc_id"] for m in arc_msgs}
        self.assert_that(
            len(arc_ids_in_messages) >= 3,
            f"Arc messages came from only {len(arc_ids_in_messages)} distinct arc(s); "
            f"expected ≥3",
            arc_ids=sorted(arc_ids_in_messages),
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{len(all_msgs)} assistant messages received ✓, "
                f"{len(child_arcs)} workflow arcs completed ✓, "
                f"{len(arc_msgs)} arc-originated messages in DB ✓"
            ),
        )
