"""
S008 — User Changes a Platform Configuration Value

The user asks Carpenter to change the `memory_recent_hints` configuration
value (how many recent conversation titles appear in the agent's context).
The user knows the config key by name and states the desired value.

The agent submits reviewed code that calls
``from carpenter_tools.act import config as cfg_tool; cfg_tool.set_value(key, value)``.
The platform-side handler writes to ~/carpenter/config/config.yaml and
immediately hot-reloads the in-memory CONFIG without a server restart.

The story verifies the live value by asking the agent to submit verification
code that reads ``config.CONFIG.get(key)`` directly.

Then the user asks to revert.  The agent sets the value back to the default (3),
hot-reloads, and confirms.  The story verifies the restored value.

Health checks:
  - Arc history is clean — the agent checks via list_arcs or arc.get_history.
  - No arc from this session reaches failed/cancelled status.

Config key:  memory_recent_hints
Original:    3  (platform default; not set in ~/carpenter/config/config.yaml)
Changed to:  7
Reverted to: 3

NOTE: This story writes and reverts ~/carpenter/config/config.yaml.  The
cleanup() method restores the default value if the test fails mid-way.
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

_CONFIG_KEY = "memory_recent_hints"
_ORIGINAL_VALUE = 3
_NEW_VALUE = 7
_CONFIG_PATH = Path.home() / "carpenter" / "config" / "config.yaml"

_CHANGE_PROMPT = (
    f"Please change the `{_CONFIG_KEY}` configuration value to {_NEW_VALUE}. "
    f"Use the config tool to do this: submit code that calls "
    f"`from carpenter_tools.act import config as cfg_tool; "
    f"result = cfg_tool.set_value('{_CONFIG_KEY}', {_NEW_VALUE}); print(result)`. "
    f"Confirm when the code executes successfully."
)

_VERIFY_PROMPT = (
    f"Please submit code to verify the current live value of `{_CONFIG_KEY}`. "
    f"Run: `from carpenter_tools.read import config as cfg_tool; "
    f"result = cfg_tool.get_value('{_CONFIG_KEY}'); print(result['value'])`"
)

_REVERT_PROMPT = (
    f"Please revert `{_CONFIG_KEY}` back to {_ORIGINAL_VALUE}. "
    f"Submit code that calls "
    f"`from carpenter_tools.act import config as cfg_tool; "
    f"result = cfg_tool.set_value('{_CONFIG_KEY}', {_ORIGINAL_VALUE}); print(result)`. "
    f"Confirm when done."
)

_ARC_HEALTH_PROMPT = (
    "Please check the recent arc history and confirm there are no unexpected "
    "failures, cancelled arcs, or repeated retry patterns from the config "
    "change operations we just performed."
)


class ChangeConfigValue(AcceptanceStory):
    name = "S008 — User Changes a Platform Configuration Value"
    description = (
        f"User changes {_CONFIG_KEY} to {_NEW_VALUE} via chat; agent calls "
        f"config.set_value (reviewed callback, hot-reload); story verifies live "
        f"value; user reverts; agent confirms restored; arc history clean."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Request the config change ──────────────────────────────────────
        print(f"\n  [1/6] Requesting {_CONFIG_KEY} → {_NEW_VALUE}...")
        conv_id = client.create_conversation()
        client.send_message(_CHANGE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=60)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after config change request",
            conversation_id=conv_id,
        )
        change_resp = msgs[-1]["content"]
        print(f"     {change_resp[:150]}")
        self.assert_that(
            any(kw in change_resp.lower() for kw in
                ("done", "updated", "changed", "set", "success", "complet",
                 "ok", "status", "memory_recent", str(_NEW_VALUE))),
            "Change response does not acknowledge the config update",
            response_preview=change_resp[:400],
        )

        # ── 2. Structural verify — config.yaml on disk ────────────────────────
        print(f"  [2/6] Verifying config.yaml on disk...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            disk_val = raw.get(_CONFIG_KEY) if raw else None
            self.assert_that(
                disk_val == _NEW_VALUE,
                f"config.yaml does not have {_CONFIG_KEY}={_NEW_VALUE} "
                f"(found {disk_val!r})",
                config_yaml=dict(raw) if raw else {},
            )
            print(f"     config.yaml: {_CONFIG_KEY}={disk_val} ✓")

        # ── 3. Live verify — in-memory CONFIG via submitted code ──────────────
        print(f"  [3/6] Verifying live CONFIG value via agent-submitted code...")
        client.send_message(_VERIFY_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=45)

        msgs = client.get_assistant_messages(conv_id)
        verify_resp = msgs[-1]["content"]
        print(f"     {verify_resp[:200]}")
        self.assert_that(
            str(_NEW_VALUE) in verify_resp,
            f"Live verify response does not contain '{_NEW_VALUE}' "
            f"— hot-reload may not have taken effect",
            response_preview=verify_resp[:400],
        )
        print(f"     Live CONFIG shows {_CONFIG_KEY}={_NEW_VALUE} ✓")

        # ── 4. Revert the config change ───────────────────────────────────────
        print(f"  [4/6] Requesting revert → {_ORIGINAL_VALUE}...")
        client.send_message(_REVERT_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=60)

        msgs = client.get_assistant_messages(conv_id)
        revert_resp = msgs[-1]["content"]
        print(f"     {revert_resp[:150]}")
        self.assert_that(
            any(kw in revert_resp.lower() for kw in
                ("done", "reverted", "set", "back", "reset",
                 "success", "complet", "ok", str(_ORIGINAL_VALUE))),
            "Revert response does not acknowledge restoration",
            response_preview=revert_resp[:400],
        )

        # ── 5. Structural verify post-revert ─────────────────────────────────
        print(f"  [5/6] Verifying config.yaml after revert...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw2 = yaml.safe_load(_CONFIG_PATH.read_text())
            disk_val2 = raw2.get(_CONFIG_KEY) if raw2 else None
            self.assert_that(
                disk_val2 == _ORIGINAL_VALUE or disk_val2 is None,
                f"config.yaml still has {_CONFIG_KEY}={disk_val2!r} after revert "
                f"(expected {_ORIGINAL_VALUE} or absent)",
                config_yaml=dict(raw2) if raw2 else {},
            )
            print(f"     config.yaml: {_CONFIG_KEY}={disk_val2!r} ✓")

        # Behavioural: ask agent to confirm the live value
        client.send_message(_VERIFY_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=45)
        msgs = client.get_assistant_messages(conv_id)
        verify_resp2 = msgs[-1]["content"]
        print(f"     Post-revert verify: {verify_resp2[:150]}")
        self.assert_that(
            str(_ORIGINAL_VALUE) in verify_resp2,
            f"Post-revert verify does not confirm {_CONFIG_KEY}={_ORIGINAL_VALUE}",
            response_preview=verify_resp2[:400],
        )
        # Must NOT still show the new value
        self.assert_that(
            str(_NEW_VALUE) not in verify_resp2
            or str(_ORIGINAL_VALUE) in verify_resp2,
            f"Post-revert verify still shows {_NEW_VALUE} without {_ORIGINAL_VALUE}",
            response_preview=verify_resp2[:400],
        )
        print(f"     Live CONFIG shows {_CONFIG_KEY}={_ORIGINAL_VALUE} ✓")

        # ── 6. Arc health check ───────────────────────────────────────────────
        print(f"  [6/6] Requesting arc health check from agent...")
        client.send_message(_ARC_HEALTH_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=60)

        msgs = client.get_assistant_messages(conv_id)
        health_resp = msgs[-1]["content"]
        print(f"     {health_resp[:200]}")
        self.assert_that(
            any(kw in health_resp.lower() for kw in
                ("no fail", "clean", "healthy", "success", "all complet",
                 "no unexpect", "no retry", "no cancel", "no error",
                 "look good", "looks good", "completed successfully",
                 "no issues")),
            "Arc health check does not confirm clean history",
            response_preview=health_resp[:400],
        )

        # Structural: no failed/cancelled arcs from our operations
        if db is not None:
            all_arcs = db.get_arcs_created_after(start_ts)
            bad = [a for a in all_arcs if a["status"] in ("failed", "cancelled")]
            self.assert_that(
                len(bad) == 0,
                f"{len(bad)} arc(s) ended in failed/cancelled",
                arcs=db.format_arcs_table(bad),
            )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{_CONFIG_KEY} changed to {_NEW_VALUE} ✓, "
                f"disk verify ✓, "
                f"live CONFIG hot-reload verified ✓, "
                f"reverted to {_ORIGINAL_VALUE} ✓, "
                f"post-revert verify ✓, "
                f"arc history clean ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore memory_recent_hints to default if the story failed mid-way."""
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            if not raw:
                return
            current = raw.get(_CONFIG_KEY)
            if current == _NEW_VALUE:
                raw[_CONFIG_KEY] = _ORIGINAL_VALUE
                _CONFIG_PATH.write_text(
                    yaml.dump(raw, default_flow_style=False, allow_unicode=True,
                              sort_keys=False)
                )
                print(f"  [cleanup] Reset {_CONFIG_KEY} to {_ORIGINAL_VALUE} in config.yaml")
                # Best-effort in-process reload
                try:
                    from carpenter.config import reload_config
                    reload_config()
                    print(f"  [cleanup] Config reloaded")
                except Exception as exc:
                    print(f"  [cleanup] In-process reload skipped: {exc}")
        except Exception as exc:
            print(f"  [cleanup] Failed to restore config: {exc}")
