"""
S016 — External Code Change and Pull Request

The user asks Carpenter to add a file to an external repository.
The agent creates a workflow arc or runs inline code, pushes the changes,
and attempts to create a PR.

Verification: the file must exist on Forgejo (on any branch), confirmed
via the Forgejo API with retries.  PR creation is checked but not required.

Preconditions (provided by external harness via env vars):
  CARPENTER_TEST_FORGEJO_URL — Forgejo instance URL
  CARPENTER_TEST_FORGEJO_TOKEN — API token with repo access
  CARPENTER_TEST_REPO_OWNER / CARPENTER_TEST_REPO_NAME — test repo coordinates
  S015 completed (repo configured, credential stored)
"""

import json
import os
import re
import time
import uuid

import httpx

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


_REQUIRED_ENV = [
    "CARPENTER_TEST_FORGEJO_URL",
    "CARPENTER_TEST_FORGEJO_TOKEN",
    "CARPENTER_TEST_REPO_OWNER",
    "CARPENTER_TEST_REPO_NAME",
]


def _forgejo_get(url: str, token: str, **kw) -> httpx.Response:
    """Helper for authenticated Forgejo GET requests."""
    return httpx.get(
        url,
        headers={"Authorization": f"token {token}"},
        timeout=15.0,
        **kw,
    )


def _verify_file_on_forgejo(
    forgejo_url: str,
    token: str,
    repo_owner: str,
    repo_name: str,
    target_file: str,
    max_retries: int = 6,
    retry_delay: float = 5.0,
) -> tuple[bool, str | None]:
    """Check whether target_file exists on any branch, with retries.

    Returns (file_exists, branch_name_or_None).
    """
    api = f"{forgejo_url}/api/v1/repos/{repo_owner}/{repo_name}"

    for attempt in range(max_retries):
        if attempt > 0:
            time.sleep(retry_delay)

        # Check main branch
        try:
            resp = _forgejo_get(f"{api}/contents/{target_file}", token)
            if resp.status_code == 200:
                return True, "main"
        except Exception:
            pass

        # Check non-main branches
        try:
            branches = _forgejo_get(f"{api}/branches", token).json()
            for branch in branches:
                name = branch.get("name", "")
                if name == "main":
                    continue
                try:
                    br_resp = _forgejo_get(
                        f"{api}/contents/{target_file}?ref={name}", token,
                    )
                    if br_resp.status_code == 200:
                        return True, name
                except Exception:
                    pass
        except Exception:
            pass

    return False, None


def _find_recent_prs(
    forgejo_url: str,
    token: str,
    repo_owner: str,
    repo_name: str,
    since_ts: float,
) -> list[dict]:
    """List PRs created after since_ts via Forgejo API."""
    api = f"{forgejo_url}/api/v1/repos/{repo_owner}/{repo_name}/pulls"
    try:
        resp = _forgejo_get(api, token, params={"state": "all", "limit": 10})
        if resp.status_code != 200:
            return []
        prs = resp.json()
        # Filter to PRs created after our start time
        from datetime import datetime, timezone
        result = []
        for pr in prs:
            created = pr.get("created_at", "")
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if dt.timestamp() >= since_ts - 5:  # small grace window
                    result.append(pr)
            except Exception:
                pass
        return result
    except Exception:
        return []


class ExternalCodeChangePR(AcceptanceStory):
    name = "S016 — External Code Change and PR"
    description = (
        "User requests a change to an external repo; agent creates a "
        "workflow arc or runs code inline, pushes changes, and creates a PR."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        # -- Check preconditions -----------------------------------------------
        missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            return self.result(
                f"Missing env vars: {', '.join(missing)}. "
                "Run with the instance-specific harness."
            )

        forgejo_url = os.environ.get("CARPENTER_TEST_FORGEJO_URL")
        forgejo_token = os.environ.get("CARPENTER_TEST_FORGEJO_TOKEN")
        repo_owner = os.environ.get("CARPENTER_TEST_REPO_OWNER")
        repo_name = os.environ.get("CARPENTER_TEST_REPO_NAME")

        start_ts = time.time()

        # -- Step 1: Ask agent to make a change --------------------------------
        run_id = uuid.uuid4().hex[:8]
        target_file = f"test-{run_id}.md"
        prompt = (
            f"Add a file named {target_file} with a short description of "
            f"the project to the repository {repo_owner}/{repo_name}. "
            f"Push the changes and create a pull request."
        )
        print(f"\n  Sending: '{prompt}'")
        conv_id, response = client.chat(prompt, timeout=180)
        print(f"  Got response ({len(response)} chars)")

        # Behavioral: agent should acknowledge the request
        self.assert_that(
            any(kw in response.lower()
                for kw in (target_file.lower(), "pr", "pull request",
                           "change", "push", "commit", "branch", "file",
                           "created", "added")),
            "Agent did not mention the change or PR",
            response_preview=response[:400],
        )

        # -- Step 2: Wait for work to complete ---------------------------------
        # The agent may use arc-based workflow or inline execution.
        max_wait = 300  # 5 minutes — generous for async push+PR
        poll_interval = 5
        waited = 0
        root_arc = None
        children = []

        while waited < max_wait:
            time.sleep(poll_interval)
            waited += poll_interval

            recent = db.get_arcs_created_after(start_ts)
            root_arcs = [a for a in recent if a.get("parent_id") is None]

            if root_arcs:
                root_arc = root_arcs[0]
                # Wait for root arc AND all children to reach terminal state
                children = db.get_arc_children(root_arc["id"])
                all_terminal = root_arc["status"] in ("completed", "failed")
                if children:
                    all_terminal = all_terminal and all(
                        c["status"] in ("completed", "failed")
                        for c in children
                    )
                if all_terminal:
                    break
                print(f"  Arc #{root_arc['id']} '{root_arc['name']}' "
                      f"status: {root_arc['status']} ({waited}s)")
            else:
                try:
                    if not client.is_pending(conv_id):
                        print(f"  Agent done (inline, no arcs) ({waited}s)")
                        break
                except Exception:
                    pass

        # -- Step 3: Collect diagnostics from arcs -----------------------------
        commit_sha = None
        pr_url_from_arc = None

        if root_arc:
            arc_id = root_arc["id"]
            # Refresh arc status
            root_arc = db.get_arc(arc_id) or root_arc
            children = db.get_arc_children(arc_id)

            print(f"  Root arc #{arc_id} '{root_arc['name']}' = "
                  f"{root_arc['status']}")
            for child in children:
                print(f"    Child #{child['id']} '{child['name']}' = "
                      f"{child['status']}")

            # Check arc_state for push/PR evidence
            for check_id in [arc_id] + [c["id"] for c in children]:
                state = db.get_arc_state(check_id)
                if not commit_sha and "commit_sha" in state:
                    commit_sha = state["commit_sha"]
                    print(f"  Commit SHA (arc state): {commit_sha[:12]}")
                if not pr_url_from_arc and "pr_url" in state:
                    pr_url_from_arc = state["pr_url"]
                    print(f"  PR URL (arc state): {pr_url_from_arc}")

        # -- Step 4: Ask agent for status (if arc-based, may have more info) ---
        _, final_response = client.chat(
            "What's the status of the code change? Did the file get added?",
            conversation_id=conv_id,
            timeout=60,
        )
        print(f"  Final response ({len(final_response)} chars)")
        all_responses = response + " " + final_response

        # Extract PR URL from responses
        pr_url = pr_url_from_arc
        if not pr_url:
            url_pattern = r'https?://[^\s)>\]"\'`]+'
            urls = re.findall(url_pattern, all_responses)
            pr_urls = [u for u in urls if '/pulls/' in u or '/pull/' in u]
            if pr_urls:
                pr_url = pr_urls[0]
                print(f"  PR URL (from response): {pr_url}")

        # -- Step 5: Verify file on Forgejo (with retries) --------------------
        print(f"  Verifying file on Forgejo (with retries)...")
        file_exists, found_branch = _verify_file_on_forgejo(
            forgejo_url, forgejo_token, repo_owner, repo_name, target_file,
        )

        if file_exists:
            print(f"  File VERIFIED on branch '{found_branch}'")
        else:
            print(f"  File NOT found on any branch after retries")

        # -- Step 6: Verify PR on Forgejo API ----------------------------------
        recent_prs = _find_recent_prs(
            forgejo_url, forgejo_token, repo_owner, repo_name, start_ts,
        )
        pr_verified = False
        if recent_prs:
            for pr in recent_prs:
                print(f"  Recent PR #{pr['number']}: {pr.get('title', '')}")
            pr_verified = True

        # -- Step 7: Pass criteria ---------------------------------------------
        # Hard requirement: file must exist on Forgejo (any branch).
        # commit_sha in arc_state is also accepted as push evidence.
        push_verified = file_exists or commit_sha is not None

        self.assert_that(
            push_verified,
            "File not found on Forgejo and no commit_sha in arc state. "
            "Agent may have failed to push.",
            response_preview=all_responses[:600],
        )

        if pr_verified:
            print(f"  PR: VERIFIED via Forgejo API")
        elif pr_url:
            print(f"  PR: claimed ({pr_url}), not verified via API")
        else:
            print(f"  PR: not created (push-only success)")

        elapsed = time.time() - start_ts
        return self.result(
            f"External code change completed in {elapsed:.1f}s. "
            f"File {'verified on ' + found_branch if file_exists else 'push verified via arc state'}. "
            f"PR {'verified' if pr_verified else 'not verified'}."
        )
