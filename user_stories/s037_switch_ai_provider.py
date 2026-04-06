"""
S037 — Switch AI Provider Mid-Conversation

Baseline question (expect France->Paris) on current model. Switch to haiku.
Question (expect Germany->Berlin). Switch back to original model. Question
(expect Japan->Tokyo). Verify the api_calls table shows different models
were used. Cleanup restores the original model config.

Pattern: follows s018's change-model-via-chat pattern.

Expected behaviour:
  1. Ask "What is the capital of France?" — baseline model answers "Paris".
  2. Switch model to haiku via config.
  3. Ask "What is the capital of Germany?" — haiku answers "Berlin".
  4. Switch model back to original.
  5. Ask "What is the capital of Japan?" — original answers "Tokyo".
  6. DB shows different models in api_calls.
  7. Cleanup restores original model.

DB verification:
  - api_calls table shows at least 2 different model identifiers.
  - All three capital city answers are correct.
"""

import time
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_CONFIG_PATH = Path.home() / "carpenter" / "config" / "config.yaml"
_MODEL_HAIKU = "claude-haiku-4-5-20251001"
_MODEL_SONNET = "claude-sonnet-4-20250514"

_Q_FRANCE = "What is the capital of France? Answer in one word."
_Q_GERMANY = "What is the capital of Germany? Answer in one word."
_Q_JAPAN = "What is the capital of Japan? Answer in one word."

_SWITCH_PROMPT = (
    "Please change the chat model to `{target}` using the config tool "
    "(set model_roles.chat). Confirm when done."
)


class SwitchAIProvider(AcceptanceStory):
    name = "S037 — Switch AI Provider Mid-Conversation"
    description = (
        "Baseline (France->Paris), switch to haiku (Germany->Berlin), switch "
        "back (Japan->Tokyo); verify api_calls shows different models."
    )

    def __init__(self):
        super().__init__()
        self._original_model = None

    def _read_current_chat_model(self) -> str:
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return _MODEL_SONNET
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        roles = raw.get("model_roles", {})
        return roles.get("chat", "") or _MODEL_SONNET

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        self._original_model = self._read_current_chat_model()
        # Pick target that's different from current
        if "haiku" in self._original_model.lower():
            switch_model = _MODEL_SONNET
        else:
            switch_model = _MODEL_HAIKU
        print(f"\n  [0/7] Current model: {self._original_model}")
        print(f"        Switch target: {switch_model}")

        # ── 1. Baseline: France → Paris ──────────────────────────────────────
        print("\n  [1/7] Baseline question: capital of France...")
        conv_id, resp1 = client.chat(_Q_FRANCE, timeout=120)
        print(f"     Response: {resp1[:100]}")
        self.assert_contains(resp1, "paris", "France capital")

        # Record watermark
        time.sleep(2)
        if db is not None:
            baseline_calls = db.fetchall(
                "SELECT id, model FROM api_calls WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (conv_id,),
            )
            baseline_model = baseline_calls[0]["model"] if baseline_calls else None
            watermark1 = baseline_calls[0]["id"] if baseline_calls else 0
            print(f"     Baseline model in DB: {baseline_model}")

        # ── 2. Switch to different model ─────────────────────────────────────
        print(f"\n  [2/7] Switching to {switch_model}...")
        _, switch_resp = client.chat(
            _SWITCH_PROMPT.format(target=switch_model),
            conversation_id=conv_id,
            timeout=180,
        )
        print(f"     {switch_resp[:150]}")

        # ── 3. Germany → Berlin ──────────────────────────────────────────────
        print("\n  [3/7] Question with new model: capital of Germany...")
        time.sleep(2)
        if db is not None:
            watermark2 = db.fetchall(
                "SELECT MAX(id) as max_id FROM api_calls"
            )[0]["max_id"] or 0

        _, resp2 = client.chat(
            _Q_GERMANY, conversation_id=conv_id, timeout=120
        )
        print(f"     Response: {resp2[:100]}")
        self.assert_contains(resp2, "berlin", "Germany capital")

        # Check model in api_calls
        time.sleep(2)
        if db is not None:
            new_calls = db.fetchall(
                "SELECT id, model FROM api_calls WHERE id > ? "
                "AND conversation_id = ? ORDER BY id",
                (watermark2, conv_id),
            )
            new_models = [c["model"] for c in new_calls if c["model"]]
            switch_key = switch_model.split("-")[1]  # "haiku" or "sonnet"
            print(f"     Post-switch models: {new_models}")

        # ── 4. Switch back to original ───────────────────────────────────────
        print(f"\n  [4/7] Switching back to {self._original_model}...")
        _, switch_back_resp = client.chat(
            _SWITCH_PROMPT.format(target=self._original_model),
            conversation_id=conv_id,
            timeout=180,
        )
        print(f"     {switch_back_resp[:150]}")

        # ── 5. Japan → Tokyo ────────────────────────────────────────────────
        print("\n  [5/7] Question with original model: capital of Japan...")
        time.sleep(2)
        if db is not None:
            watermark3 = db.fetchall(
                "SELECT MAX(id) as max_id FROM api_calls"
            )[0]["max_id"] or 0

        _, resp3 = client.chat(
            _Q_JAPAN, conversation_id=conv_id, timeout=120
        )
        print(f"     Response: {resp3[:100]}")
        self.assert_contains(resp3, "tokyo", "Japan capital")

        # ── 6. Verify different models in api_calls ──────────────────────────
        if db is not None:
            print("\n  [6/7] Verifying different models in api_calls...")
            all_calls = db.fetchall(
                "SELECT id, model FROM api_calls WHERE conversation_id = ? "
                "ORDER BY id",
                (conv_id,),
            )
            models_used = set(c["model"] for c in all_calls if c.get("model"))
            print(f"     Models used: {models_used}")

            self.assert_that(
                len(models_used) >= 2,
                f"Expected >=2 different models in api_calls, "
                f"found {len(models_used)}: {models_used}",
                api_calls=all_calls[-6:],
            )
            print(f"     {len(models_used)} distinct models confirmed ✓")

        # ── 7. Verify config restored ─────────────────────────────────────────
        print("\n  [7/7] Verifying config restored...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            roles = raw.get("model_roles", {})
            current = roles.get("chat", "")
            # Normalize: strip provider prefix (e.g. "anthropic:") for comparison
            # The agent may write "anthropic:claude-haiku-4-5-..." or just the model name
            norm_current = current.split(":")[-1] if ":" in current else current
            norm_original = self._original_model.split(":")[-1] if ":" in self._original_model else self._original_model
            self.assert_that(
                norm_current == norm_original,
                f"Config not restored: expected {self._original_model}, "
                f"found {current}",
            )
            print(f"     Config restored ✓ ({current})")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Paris ✓, switch to {switch_model} ✓, Berlin ✓, "
                f"switch back ✓, Tokyo ✓, "
                f"multiple models in api_calls ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore original model config."""
        if not self._original_model:
            return
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            if not raw:
                return
            roles = raw.get("model_roles", {})
            current = roles.get("chat", "")
            if current and current != self._original_model:
                roles["chat"] = self._original_model
                raw["model_roles"] = roles
                _CONFIG_PATH.write_text(
                    yaml.dump(raw, default_flow_style=False, allow_unicode=True,
                              sort_keys=False)
                )
                print(f"  [cleanup] Restored model_roles.chat to {self._original_model}")
                try:
                    from carpenter.config import reload_config
                    reload_config()
                except Exception:
                    pass
        except Exception as exc:
            print(f"  [cleanup] Config restore failed: {exc}")
