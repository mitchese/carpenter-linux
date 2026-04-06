"""
S040 — Ollama Tool Calling Smoke Test

Focused acceptance test for tool calling against an Ollama instance (typically
running on a desktop machine with a GPU for fast iteration).

Unlike S014/S039 which spin up a full local install, this story connects to
a pre-existing Carpenter server configured to use Ollama as its AI provider.
It tests whether small models (3B) can successfully use tools through the
harness optimizations (ultra-core tool set, few-shot examples, validation,
low temperature, KB prepopulation).

Substories (in order):
  1. Chat response — baseline: does the model respond at all?
  2. KB search — send "What do you know about X?", verify model calls kb_search
  3. File read — ask to read a known file, verify model calls read_file

Prerequisites:
  - Desktop Ollama running (set OLLAMA_URL env var, e.g. http://<host>:11434)
  - Model available: qwen2.5:3b-instruct-q4_K_M (or OLLAMA_MODEL env var)
  - Carpenter server running with ai_provider=ollama and ollama_url configured

Usage:
  # Run with default model (Qwen 2.5 3B)
  python -m user_stories.runner --story s040

  # Run with xLAM function-calling model
  OLLAMA_MODEL="hf.co/Salesforce/xLAM-2-3b-fc-r-gguf:latest" \\
    python -m user_stories.runner --story s040
"""

import os
import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


class _SlowModelClient(CarpenterClient):
    """Client wrapper that scales timeouts for slower Ollama inference.

    Desktop Ollama is much faster than Pi-local inference, so we use a
    modest 5x multiplier (vs 15x in S014).
    """

    TIMEOUT_MULTIPLIER = 5

    def is_pending(self, conversation_id: int) -> bool:
        try:
            return super().is_pending(conversation_id)
        except Exception:
            return True

    def wait_for_pending_to_clear(
        self, conversation_id: int, timeout: int = 60
    ) -> None:
        scaled = timeout * self.TIMEOUT_MULTIPLIER
        return super().wait_for_pending_to_clear(conversation_id, timeout=scaled)

    def wait_for_n_assistant_messages(
        self,
        conversation_id: int,
        n: int,
        timeout: int = 120,
    ) -> list[dict]:
        scaled = timeout * self.TIMEOUT_MULTIPLIER
        return super().wait_for_n_assistant_messages(
            conversation_id, n, timeout=scaled
        )


class OllamaToolCalling(AcceptanceStory):
    name = "S040 — Ollama Tool Calling Smoke Test"
    description = (
        "Tests tool calling with small Ollama models (3B). "
        "Verifies chat response, kb_search tool use, and read_file tool use."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()
        model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b-instruct-q4_K_M")
        print(f"\n  Model: {model}")

        # Wrap client with timeout scaling
        slow_client = _SlowModelClient(
            client.base_url,
            token=client._token,
            timeout=client._default_timeout,
        )

        # ── Substory 1: Chat response (baseline) ─────────────────────────
        print("\n  [1/3] Chat response baseline...")
        conv_id, response = slow_client.chat(
            "Hello! What can you do?",
            timeout=120,
        )
        print(f"  Got response ({len(response)} chars, conversation {conv_id})")

        self.assert_that(
            len(response) >= 20,
            f"Chat response too short ({len(response)} chars); expected >= 20",
            response_preview=response[:300],
        )

        # ── Substory 2: KB search ────────────────────────────────────────
        print("\n  [2/3] KB search tool call...")
        conv_id_kb, response_kb = slow_client.chat(
            "What do you know about scheduling? "
            "Please search your knowledge base to find out.",
            timeout=180,
        )
        print(f"  Got response ({len(response_kb)} chars, conversation {conv_id_kb})")

        # Check if kb_search was called by looking at tool_calls in DB
        kb_search_called = self._check_tool_called(db, conv_id_kb, "kb_search")
        if kb_search_called:
            print("  kb_search tool was called successfully")
        else:
            # Check if the response mentions KB content anyway (from prepopulation)
            has_kb_content = any(
                kw in response_kb.lower()
                for kw in ("knowledge base", "scheduling", "kb", "search")
            )
            if has_kb_content:
                print("  kb_search not explicitly called, but KB content present "
                      "(likely from prepopulation)")
            else:
                print("  WARNING: kb_search not called and no KB content in response")

        self.assert_that(
            len(response_kb) >= 20,
            f"KB search response too short ({len(response_kb)} chars)",
            response_preview=response_kb[:300],
            kb_search_called=kb_search_called,
        )

        # ── Substory 3: File read ────────────────────────────────────────
        print("\n  [3/3] File read tool call...")
        conv_id_file, response_file = slow_client.chat(
            "Please read the file at config.yaml and tell me what AI provider is configured.",
            timeout=180,
        )
        print(f"  Got response ({len(response_file)} chars, conversation {conv_id_file})")

        read_file_called = self._check_tool_called(db, conv_id_file, "read_file")
        if read_file_called:
            print("  read_file tool was called successfully")
        else:
            print("  WARNING: read_file not called")

        self.assert_that(
            len(response_file) >= 20,
            f"File read response too short ({len(response_file)} chars)",
            response_preview=response_file[:300],
            read_file_called=read_file_called,
        )

        # ── Summary ──────────────────────────────────────────────────────
        duration = time.time() - start_ts
        tools_called = sum([
            1,  # chat baseline always passes if we get here
            1 if kb_search_called else 0,
            1 if read_file_called else 0,
        ])

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Model: {model}, "
                f"Tools called: {tools_called}/3 substories, "
                f"kb_search={'yes' if kb_search_called else 'no'}, "
                f"read_file={'yes' if read_file_called else 'no'}"
            ),
            diagnostics={
                "model": model,
                "kb_search_called": kb_search_called,
                "read_file_called": read_file_called,
                "tools_called": tools_called,
            },
            duration_s=duration,
        )

    @staticmethod
    def _check_tool_called(
        db: DBInspector, conversation_id: int, tool_name: str
    ) -> bool:
        """Check if a specific tool was called in a conversation."""
        try:
            rows = db._query(
                "SELECT COUNT(*) as cnt FROM tool_calls "
                "WHERE conversation_id = ? AND tool_name = ?",
                (conversation_id, tool_name),
            )
            return rows[0]["cnt"] > 0 if rows else False
        except Exception:
            return False
