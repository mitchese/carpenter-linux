"""
Carpenter Acceptance Test Framework

Provides the building blocks for writing acceptance stories:
- CarpenterClient  — HTTP interaction with the running server
- DBInspector          — Direct SQLite read access for verifying internal state
- AcceptanceStory      — Base class for acceptance stories
- StoryResult          — Rich result container
- AssertionFailure     — Exception raised by failed assertions
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AssertionFailure(Exception):
    """Raised by story assertions to signal a test failure."""
    message: str
    diagnostics: dict = field(default_factory=dict)


@dataclass
class StoryResult:
    name: str
    passed: bool
    message: str = ""
    error: str = ""
    diagnostics: dict = field(default_factory=dict)
    duration_s: float = 0.0

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.name} ({self.duration_s:.1f}s)"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class CarpenterClient:
    """HTTP client for interacting with Carpenter's chat API."""

    def __init__(self, base_url: str, token: str | None = None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._default_timeout = timeout
        self._headers: dict[str, str] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, **kw) -> httpx.Response:
        kw.setdefault("timeout", self._default_timeout)
        kw.setdefault("follow_redirects", False)
        # Retry once on ReadTimeout — the Pi server can be slow under load
        try:
            return httpx.get(f"{self.base_url}{path}", headers=self._headers, **kw)
        except httpx.ReadTimeout:
            return httpx.get(f"{self.base_url}{path}", headers=self._headers, **kw)

    def _post(self, path: str, json_body: dict, **kw) -> httpx.Response:
        kw.setdefault("timeout", self._default_timeout)
        return httpx.post(
            f"{self.base_url}{path}", json=json_body, headers=self._headers, **kw
        )

    def is_running(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/", timeout=5, follow_redirects=False)
            return r.status_code in (200, 401, 302)
        except Exception:
            return False

    def create_conversation(self) -> int:
        """Create a fresh conversation and return its integer ID."""
        r = self._get("/new")
        if r.status_code == 302:
            loc = r.headers.get("location", "")
            params = parse_qs(urlparse(loc).query)
            if "c" in params:
                return int(params["c"][0])
        raise RuntimeError(
            f"Failed to create conversation: {r.status_code} {r.text[:200]}"
        )

    def send_message(self, text: str, conversation_id: int) -> dict:
        """Send a chat message. Returns {event_id, conversation_id}."""
        r = self._post(
            "/api/chat", json_body={"text": text, "conversation_id": conversation_id}
        )
        if r.status_code != 202:
            raise RuntimeError(
                f"POST /api/chat failed: {r.status_code} {r.text[:200]}"
            )
        return r.json()

    def is_pending(self, conversation_id: int) -> bool:
        """Return True if the AI is still processing a response."""
        r = self._get(f"/api/chat/pending?c={conversation_id}")
        r.raise_for_status()
        return r.json().get("pending", False)

    def get_history(self, conversation_id: int) -> list[dict]:
        """Return all messages for a conversation as a list of dicts."""
        r = self._get(f"/api/chat/history?conversation_id={conversation_id}")
        r.raise_for_status()
        return r.json().get("messages", [])

    def get_assistant_messages(self, conversation_id: int) -> list[dict]:
        """Return only assistant-role messages with non-empty content.

        Empty assistant messages can appear when system notifications
        (e.g. module-reload, verification-arc creation) trigger an
        invocation that produces no visible text.  Filtering them out
        prevents ``msgs[-1]`` from landing on an empty response.
        """
        return [
            m for m in self.get_history(conversation_id)
            if m["role"] == "assistant" and m.get("content")
        ]

    def wait_for_pending_to_clear(
        self, conversation_id: int, timeout: int = 60, poll_interval: float = 0.5
    ) -> None:
        """Block until the AI is no longer processing. Raises TimeoutError.

        Args:
            conversation_id: Conversation to monitor
            timeout: Maximum seconds to wait
            poll_interval: Seconds between status checks (default 0.5s)
        """
        deadline = time.monotonic() + timeout
        # Check immediately — no initial sleep needed (API is fast)
        while time.monotonic() < deadline:
            if not self.is_pending(conversation_id):
                return
            time.sleep(poll_interval)
        raise TimeoutError(
            f"AI still pending after {timeout}s for conversation {conversation_id}"
        )

    def chat(
        self,
        text: str,
        conversation_id: int | None = None,
        timeout: int = 60,
    ) -> tuple[int, str]:
        """Send a message, wait for the AI to respond.

        Returns (conversation_id, last_assistant_message_content).
        Creates a new conversation if conversation_id is None.
        """
        if conversation_id is None:
            conversation_id = self.create_conversation()
        self.send_message(text, conversation_id)
        self.wait_for_pending_to_clear(conversation_id, timeout=timeout)
        msgs = self.get_assistant_messages(conversation_id)
        if not msgs:
            raise RuntimeError("AI produced no assistant message after pending cleared")
        return conversation_id, msgs[-1]["content"]

    def wait_for_n_assistant_messages(
        self,
        conversation_id: int,
        n: int,
        timeout: int = 120,
        poll_interval: float = 1.0,
    ) -> list[dict]:
        """Poll until there are at least *n* assistant messages. Return them.

        Args:
            conversation_id: Conversation to monitor
            n: Minimum number of assistant messages to wait for
            timeout: Maximum seconds to wait
            poll_interval: Seconds between checks (default 1.0s)
        """
        deadline = time.monotonic() + timeout
        # Check immediately in case messages already exist (fast-path)
        msgs = self.get_assistant_messages(conversation_id)
        if len(msgs) >= n:
            return msgs

        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            msgs = self.get_assistant_messages(conversation_id)
            if len(msgs) >= n:
                return msgs

        raise TimeoutError(
            f"Expected ≥{n} assistant messages, only got {len(msgs)} after {timeout}s "
            f"(conversation {conversation_id})"
        )

    def submit_review_decision(
        self,
        review_id: str,
        decision: str,
        comment: str = "",
    ) -> dict:
        """Submit approve/reject/revise for a pending coding-change diff review.

        Args:
            review_id: UUID from arc_state['review_id'].
            decision:  "approve", "reject", or "revise".
            comment:   Optional feedback (required when decision="revise").

        Returns:
            Server response dict with at least {"recorded": True} on success.
        """
        r = self._post(
            f"/api/review/{review_id}/decide",
            json_body={"decision": decision, "comment": comment},
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Database inspector
# ---------------------------------------------------------------------------


class DBInspector:
    """Direct read-only SQLite access for verifying internal platform state.

    Opens the database in read-only mode for each query to avoid locking
    the live server's connection.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # --- Arc queries ---

    def get_arcs(self, limit: int = 50) -> list[dict]:
        return self._query(
            "SELECT * FROM arcs ORDER BY id DESC LIMIT ?", (limit,)
        )

    def get_arc(self, arc_id: int) -> dict | None:
        rows = self._query("SELECT * FROM arcs WHERE id = ?", (arc_id,))
        return rows[0] if rows else None

    def get_arc_children(self, parent_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM arcs WHERE parent_id = ? ORDER BY step_order",
            (parent_id,),
        )

    def get_arc_state(self, arc_id: int) -> dict[str, Any]:
        rows = self._query(
            "SELECT key, value_json FROM arc_state WHERE arc_id = ?", (arc_id,)
        )
        return {r["key"]: json.loads(r["value_json"]) for r in rows}

    def get_arcs_created_after(self, since_ts: float) -> list[dict]:
        """Return arcs created at or after the given Unix timestamp (UTC)."""
        # SQLite stores CURRENT_TIMESTAMP as 'YYYY-MM-DD HH:MM:SS' in UTC
        since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return self._query(
            "SELECT * FROM arcs WHERE created_at >= ? ORDER BY id", (since_iso,)
        )

    def get_arc_history(self, arc_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM arc_history WHERE arc_id = ? ORDER BY id", (arc_id,)
        )

    # --- Message queries ---

    def get_messages(self, conversation_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )

    def get_arc_messages(self, conversation_id: int) -> list[dict]:
        """Return messages that were sent by arc executors (arc_id IS NOT NULL)."""
        return self._query(
            "SELECT * FROM messages "
            "WHERE conversation_id = ? AND arc_id IS NOT NULL ORDER BY id",
            (conversation_id,),
        )

    # --- Coding-change / review queries ---

    def get_arcs_pending_review(self, since_ts: float) -> list[dict]:
        """Return arcs that are waiting for human review.

        These are arcs in 'waiting' status that have a 'review_id' key in
        their arc_state, created at or after since_ts.  Each returned dict
        includes an extra 'arc_state' key containing the full state dict.
        """
        since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        rows = self._query(
            "SELECT a.* FROM arcs a "
            "JOIN arc_state s ON s.arc_id = a.id "
            "WHERE a.status = 'waiting' AND s.key = 'review_id' "
            "AND a.created_at >= ? "
            "ORDER BY a.id",
            (since_iso,),
        )
        result = []
        for row in rows:
            state = self.get_arc_state(row["id"])
            result.append({**row, "arc_state": state})
        return result

    # --- KB queries ---

    def get_kb_entries(self, path_prefix: str | None = None) -> list[dict]:
        """Return knowledge base entries from the kb_entries table.

        Pass path_prefix= to filter to entries starting with that path.
        """
        if path_prefix is not None:
            return self._query(
                "SELECT * FROM kb_entries WHERE path LIKE ? ORDER BY path",
                (path_prefix + "%",),
            )
        return self._query("SELECT * FROM kb_entries ORDER BY path")

    # --- Generic query ---

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute an arbitrary read-only SQL query and return all rows as dicts."""
        return self._query(sql, params)

    # --- Work queue ---

    def get_work_queue(self, limit: int = 20) -> list[dict]:
        return self._query(
            "SELECT * FROM work_queue ORDER BY id DESC LIMIT ?", (limit,)
        )

    def get_conversations(self, limit: int = 10) -> list[dict]:
        return self._query(
            "SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        )

    # --- Formatting helpers for diagnostic output ---

    def format_arcs_table(self, arcs: list[dict]) -> str:
        if not arcs:
            return "  (none)"
        lines = [
            f"  {'ID':>4} | {'Name':<28} | {'Status':<10} | "
            f"{'Par':>4} | {'Ord':>3} | {'Taint':<8} | {'Agent':<10}"
        ]
        lines.append("  " + "-" * 84)
        for a in arcs:
            lines.append(
                f"  {a['id']:>4} | {str(a.get('name',''))[:28]:<28} | "
                f"{str(a.get('status','')):<10} | "
                f"{str(a.get('parent_id') or ''):>4} | "
                f"{str(a.get('step_order') or '0'):>3} | "
                f"{str(a.get('integrity_level','')):<8} | "
                f"{str(a.get('agent_type','')):<10}"
            )
        return "\n".join(lines)

    def format_messages_table(self, messages: list[dict]) -> str:
        if not messages:
            return "  (none)"
        lines = []
        for m in messages:
            arc_tag = f" [arc={m['arc_id']}]" if m.get("arc_id") else ""
            preview = str(m.get("content", ""))[:100].replace("\n", "↵")
            lines.append(f"  [{m['role']}{arc_tag}] {preview}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Story base class
# ---------------------------------------------------------------------------


class AcceptanceStory:
    """Base class for an acceptance story.

    Subclasses must:
    - Set class attributes `name` and `description`
    - Implement `run(client, db)` which performs the scenario and checks

    Assertion helpers:
    - `self.assert_that(condition, message)` — generic boolean assert
    - `self.assert_contains(text, substring)` — case-insensitive substring check
    """

    name: str = "unnamed"
    description: str = ""
    timeout: int = 300  # Default timeout in seconds for test execution

    def run(
        self, client: CarpenterClient, db: DBInspector
    ) -> StoryResult:
        raise NotImplementedError

    def cleanup(
        self, client: CarpenterClient, db: "DBInspector | None"
    ) -> None:
        """Called after run() completes (pass or fail). Override to remove test state."""

    def assert_that(
        self, condition: bool, message: str, **diagnostics: Any
    ) -> None:
        if not condition:
            raise AssertionFailure(message, diagnostics)

    def assert_contains(
        self, text: str, substring: str, context: str = ""
    ) -> None:
        msg = f"Expected to find {substring!r} in response"
        if context:
            msg += f" ({context})"
        self.assert_that(
            substring.lower() in text.lower(),
            msg,
            text_preview=text[:400],
        )

    def result(self, message: str = "") -> "StoryResult":
        """Return a passing StoryResult for this story. Convenience helper."""
        return StoryResult(name=self.name, passed=True, message=message)
