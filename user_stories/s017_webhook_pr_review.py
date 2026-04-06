"""
S017 — Webhook-Triggered Automated PR Review

Creates a real PR on the test Forgejo repo, then fires a simulated
webhook event. The platform matches it to a pre-created subscription,
creates a pr-review arc, and the review pipeline runs to completion.

The webhook subscription is created by the external harness
(setup-webhook-subscription.py), not by the agent — chat-based webhook
management is a separate work item.

Preconditions (provided by external harness via env vars):
  CARPENTER_TEST_FORGEJO_URL — Forgejo instance URL
  CARPENTER_TEST_FORGEJO_TOKEN — API token with repo access
  CARPENTER_TEST_REPO_OWNER / CARPENTER_TEST_REPO_NAME — test repo coordinates
  CARPENTER_TEST_WEBHOOK_ID — webhook subscription UUID (created by harness)
"""

import base64
import json
import os
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
    "CARPENTER_TEST_WEBHOOK_ID",
]


def _forgejo_api(method, path, token, data=None):
    """Helper for Forgejo API calls."""
    url = path if path.startswith("http") else path
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }
    resp = httpx.request(method, url, headers=headers, json=data, timeout=30.0)
    return resp


class WebhookPRReview(AcceptanceStory):
    name = "S017 — Webhook-Triggered PR Review"
    description = (
        "Creates a real PR, fires a webhook event; platform creates a "
        "pr-review arc that fetches diff, runs AI review, and posts results."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        # ── Check preconditions ──────────────────────────────────────────
        missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            return self.result(
                f"Missing env vars: {', '.join(missing)}. "
                "Run with the external git harness."
            )

        forgejo_url = os.environ.get("CARPENTER_TEST_FORGEJO_URL")
        forgejo_token = os.environ.get("CARPENTER_TEST_FORGEJO_TOKEN")
        repo_owner = os.environ.get("CARPENTER_TEST_REPO_OWNER")
        repo_name = os.environ.get("CARPENTER_TEST_REPO_NAME")
        webhook_id = os.environ.get("CARPENTER_TEST_WEBHOOK_ID")
        api_base = f"{forgejo_url}/api/v1"

        start_ts = time.time()

        # ── Step 1: Create a real PR on Forgejo ──────────────────────────
        # Create a file on a new branch via the contents API
        # Use unique branch and file names per run to avoid conflicts
        run_id = uuid.uuid4().hex[:8]
        branch_name = f"test-review-{run_id}"
        file_content = base64.b64encode(
            f"# Test File\n\nCreated by S017 acceptance test (run {run_id}).\n"
            .encode()
        ).decode()

        print("  Creating test file on new branch...")
        create_file_resp = _forgejo_api(
            "POST",
            f"{api_base}/repos/{repo_owner}/{repo_name}/contents/test-review-{run_id}.md",
            forgejo_token,
            {
                "content": file_content,
                "message": "Add test file for PR review",
                "new_branch": branch_name,
            },
        )
        self.assert_that(
            create_file_resp.status_code == 201,
            f"Failed to create test file: HTTP {create_file_resp.status_code} "
            f"— {create_file_resp.text[:200]}",
        )

        # Create a PR from the new branch
        print("  Creating pull request...")
        create_pr_resp = _forgejo_api(
            "POST",
            f"{api_base}/repos/{repo_owner}/{repo_name}/pulls",
            forgejo_token,
            {
                "title": "Test PR for automated review",
                "body": "This PR was created by the S017 acceptance test.",
                "head": branch_name,
                "base": "main",
            },
        )
        self.assert_that(
            create_pr_resp.status_code == 201,
            f"Failed to create PR: HTTP {create_pr_resp.status_code} "
            f"— {create_pr_resp.text[:200]}",
        )

        pr_data = create_pr_resp.json()
        pr_number = pr_data["number"]
        pr_html_url = pr_data.get("html_url", "")
        print(f"  Created PR #{pr_number}: {pr_html_url}")

        # ── Step 2: Wait for Forgejo webhook to fire ─────────────────────
        # The harness registered a real Forgejo webhook pointing at the Carpenter
        # server, so creating the PR above should trigger it automatically.
        # No manual simulation needed — this is a true end-to-end test.
        print(f"  Waiting for Forgejo webhook to fire for PR #{pr_number}...")
        max_wait = 180
        poll_interval = 5
        waited = 0
        arc_completed = False
        review_arc_id = None

        while waited < max_wait:
            time.sleep(poll_interval)
            waited += poll_interval

            arcs = db.fetchall(
                "SELECT id, status FROM arcs "
                f"WHERE name LIKE 'pr-review-{pr_number}%' "
                "ORDER BY id DESC LIMIT 1"
            )
            if arcs:
                arc = arcs[0]
                review_arc_id = arc["id"]
                if arc["status"] in ("completed", "failed"):
                    arc_completed = True
                    break
                print(f"  PR review arc #{arc['id']} status: {arc['status']} ({waited}s)")

        self.assert_that(
            arc_completed,
            f"PR review arc did not complete within {max_wait}s",
        )

        # ── Structural assertions ────────────────────────────────────────
        arcs = db.fetchall(
            "SELECT id, status FROM arcs "
            f"WHERE name LIKE 'pr-review-{pr_number}%' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assert_that(len(arcs) > 0, "No pr-review arc found")
        self.assert_that(
            arcs[0]["status"] == "completed",
            f"PR review arc status is '{arcs[0]['status']}', expected 'completed'",
        )

        # Check that review result was stored in arc state
        if review_arc_id:
            review_state = db.fetchall(
                "SELECT value_json FROM arc_state "
                "WHERE arc_id = ? AND key = 'review_result'",
                (review_arc_id,),
            )
            if review_state:
                result = json.loads(review_state[0]["value_json"])
                verdict = result.get("verdict", "unknown")
                print(f"  Review verdict: {verdict}")
                self.assert_that(
                    verdict in ("APPROVED", "REQUEST_CHANGES", "COMMENT"),
                    f"Invalid review verdict: {verdict}",
                )

        # Check webhook event was recorded
        events = db.fetchall(
            "SELECT id FROM work_queue "
            "WHERE event_type = 'webhook.received' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assert_that(
            len(events) > 0,
            "No webhook.received work item found",
        )

        elapsed = time.time() - start_ts
        return self.result(
            f"Webhook PR review completed in {elapsed:.1f}s. "
            f"PR #{pr_number} reviewed, arc #{review_arc_id} completed."
        )
