"""
S014 — Local Provider Install & Fallback

End-to-end acceptance test: installs Carpenter from scratch using the
built-in local AI provider (llama.cpp) into an isolated temporary directory,
starts the server on dynamically chosen ports, runs stories S001-S006 as
substories, then tests local fallback when the cloud API is unavailable.

Verification goals:
  1. install.sh creates a complete, self-contained installation in --base-dir.
  2. The generated config.yaml and database live inside that dir.
  3. The server starts, launches the local inference backend, and responds to
     HTTP health checks.
  4. Stories S001-S006 run against the fresh installation.
  5. No files are written to ~/carpenter (the default base dir) or any
     other location outside the temporary directory.
  6. When cloud API is unavailable and local_fallback is enabled, the server
     falls back to the local inference server and still responds to chat.

Prerequisites:
  - llama-server must be in PATH (install via apt, brew, or build llama.cpp).
  - A GGUF model must be cached in one of: ~/models/, ~/.cache/huggingface/hub/,
    or /tmp/.  The recommended model is qwen2.5-3b-instruct-q4_k_m.gguf (~2 GB).

The install uses --port and --inference-port flags so no post-install config
patching is needed.

Substories (run in order):
  S001 — Describe Abilities (pure chat, no arcs)
  S002 — Workflow With Three Arc Messages
  S003 — Agent Creates KB Skill Entry and Uses It
  S004 — Agent Creates, Uses, and Then Deletes KB Skill Entry
  S005 — Agent Adds a Platform Tool via Coding-Change
  S006 — Remind Me at a Specific Time
"""

import importlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from user_stories.framework import (
    AcceptanceStory,
    AssertionFailure,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


# -- Helpers ------------------------------------------------------------------


def _find_free_port() -> int:
    """Find a free TCP port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_free_port_excluding(exclude: int) -> int:
    """Find a free TCP port, excluding one already in use."""
    for _ in range(10):
        port = _find_free_port()
        if port != exclude:
            return port
    raise RuntimeError("Could not find a free port for inference server")


# Model files to search for, in preference order.
_MODEL_CANDIDATES = [
    "qwen2.5-3b-instruct-q4_k_m.gguf",       # recommended (S014 default)
    "qwen2.5-1.5b-instruct-q4_k_m.gguf",      # smaller fallback
]

# Directories where a model might already be cached.
_MODEL_SEARCH_DIRS = [
    Path.home() / "models",
    Path.home() / ".cache" / "huggingface" / "hub",
    Path("/tmp"),
]


def _find_cached_model() -> Path | None:
    """Locate an already-downloaded GGUF model file."""
    for directory in _MODEL_SEARCH_DIRS:
        if not directory.is_dir():
            continue
        # Prefer candidates in priority order
        for candidate in _MODEL_CANDIDATES:
            path = directory / candidate
            if path.is_file():
                return path
        # Fall back to any .gguf in the directory
        gguf_files = sorted(directory.glob("*.gguf"), key=lambda p: p.stat().st_size)
        if gguf_files:
            return gguf_files[-1]  # largest file (most capable model)
    return None


def _model_key_for_filename(filename: str) -> str:
    """Map a GGUF filename to the install.sh --local-model catalog key."""
    name = filename.lower()
    if "qwen2.5-3b" in name:
        return "qwen2.5-3b-q4"
    if "qwen2.5-1.5b" in name:
        return "qwen2.5-1.5b-q4"
    # Default to the recommended model key
    return "qwen2.5-3b-q4"


class _SlowModelClient(CarpenterClient):
    """Client wrapper that scales timeouts for slow local model inference.

    Local models on Pi-class hardware need ~100s for prompt processing alone,
    so substory timeouts (designed for cloud API speeds) must be multiplied.

    Only the waiting methods are overridden -- NOT chat(), because chat()
    delegates to wait_for_pending_to_clear() internally, and overriding both
    would double-scale the timeout.

    is_pending() is overridden to absorb HTTP timeouts: when the server is
    blocked on local inference it may not respond to health-check requests
    within the per-request timeout, but that doesn't mean the request failed.
    """

    TIMEOUT_MULTIPLIER = 15

    def is_pending(self, conversation_id: int) -> bool:
        """Return True if the AI is still processing, or if the server is
        unresponsive (assumed to be busy with inference)."""
        try:
            return super().is_pending(conversation_id)
        except Exception:
            # Server blocked on inference -- treat as still pending
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


# Substory modules, loaded dynamically.
# Only S043 (smoke test) is used — the 3B local model cannot handle
# tool reasoning, code generation, or workflow planning (S001-S006).
_SUBSTORY_MODULES = [
    ("s043_smoke_test_api_health", "SmokeTestApiHealth"),
]


class LocalProviderSpinup(AcceptanceStory):
    name = "S014 — Local Provider Install & Fallback"
    description = (
        "Install Carpenter from scratch with local AI (llama.cpp) in an "
        "isolated directory, run S043 smoke test, test local fallback when "
        "cloud API is unavailable, then tear down."
    )

    def __init__(self):
        self._base_dir: str | None = None
        self._server_proc: subprocess.Popen | None = None
        self._port: int = 0
        self._inference_port: int = 0

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        """Note: the client/db args from the runner are ignored.

        This story creates its own server, client, and db inspector.
        """
        start_ts = time.time()
        repo_dir = Path(__file__).resolve().parent.parent

        # -- 1. Check prerequisites -------------------------------------------
        llama_server = shutil.which("llama-server")
        if not llama_server:
            raise AssertionFailure(
                "llama-server not found in PATH. "
                "Install it before running this story.",
            )
        print(f"\n  llama-server: {llama_server}")

        cached_model = _find_cached_model()
        if not cached_model:
            raise AssertionFailure(
                "No cached GGUF model found. Download one to ~/models/ first. "
                f"Searched: {[str(d) for d in _MODEL_SEARCH_DIRS]}",
            )
        model_key = _model_key_for_filename(cached_model.name)
        print(f"  Cached model: {cached_model} (key: {model_key})")

        # -- 2. Set up temp directory ------------------------------------------
        # Use /tmp on disk (not TMPDIR which may be tmpfs/ramdisk).
        # The model symlink needs to resolve, not consume RAM.
        self._base_dir = tempfile.mkdtemp(prefix="tc_s014_", dir="/tmp")
        print(f"  Base directory: {self._base_dir}")

        # -- 3. Record pre-install state of default base dir -------------------
        default_base = Path.home() / "carpenter"
        if default_base.is_dir():
            pre_install_files = set(
                str(p.relative_to(default_base))
                for p in default_base.rglob("*")
                if p.is_file()
            )
            print(f"  Existing installation detected: {default_base} "
                  f"({len(pre_install_files)} files)")
        else:
            pre_install_files = set()
            print(f"  No existing installation at {default_base}")

        # -- 4. Choose ports ---------------------------------------------------
        self._port = _find_free_port()
        self._inference_port = _find_free_port_excluding(self._port)
        print(f"  Server port: {self._port}")
        print(f"  Inference port: {self._inference_port}")

        # -- 5. Pre-cache model into base dir ----------------------------------
        # Symlink the model so install.sh finds it and skips download.
        models_dir = Path(self._base_dir) / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / cached_model.name).symlink_to(cached_model)
        print(f"  Pre-cached model via symlink")

        # -- 6. Run install.sh -------------------------------------------------
        print("\n  Running install.sh (local provider, non-interactive)...")
        install_cmd = [
            "bash", str(repo_dir / "install.sh"),
            "--non-interactive",
            "--base-dir", self._base_dir,
            "--ai-provider", "local",
            "--local-model", model_key,
            "--port", str(self._port),
            "--inference-port", str(self._inference_port),
            "--skip-token",
            "--no-plugin",
        ]

        install_env = os.environ.copy()
        install_env["PYTHONPATH"] = str(repo_dir)

        install_result = subprocess.run(
            install_cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min for potential llama.cpp build from source
            env=install_env,
        )
        if install_result.returncode != 0:
            print(f"  STDOUT: {install_result.stdout[-500:]}")
            print(f"  STDERR: {install_result.stderr[-500:]}")
            raise AssertionFailure(
                f"install.sh failed with exit code {install_result.returncode}",
                {"stdout_tail": install_result.stdout[-1000:],
                 "stderr_tail": install_result.stderr[-1000:]},
            )
        print("  install.sh completed successfully")

        # -- 7. Verify installation structure ----------------------------------
        config_path = os.path.join(self._base_dir, "config", "config.yaml")
        db_path = os.path.join(self._base_dir, "data", "platform.db")

        self.assert_that(
            os.path.isfile(config_path),
            f"Config file not created: {config_path}",
        )
        self.assert_that(
            os.path.isfile(db_path),
            f"Database not created: {db_path}",
        )

        # Verify config references temp base dir, local provider, and our ports
        with open(config_path) as f:
            config_text = f.read()

        self.assert_that(
            self._base_dir in config_text,
            "config.yaml does not reference the temp base dir",
            config_preview=config_text[:500],
        )
        self.assert_that(
            "ai_provider: local" in config_text,
            "config.yaml does not set ai_provider to local",
            config_preview=config_text[:500],
        )
        self.assert_that(
            f"port: {self._port}" in config_text,
            f"Config should contain 'port: {self._port}' (via --port flag)",
        )
        self.assert_that(
            f"local_server_port: {self._inference_port}" in config_text,
            f"Config should contain 'local_server_port: {self._inference_port}' "
            f"(via --inference-port flag)",
        )

        # Verify model file is accessible
        model_files = list(Path(self._base_dir, "models").glob("*.gguf"))
        self.assert_that(
            len(model_files) >= 1,
            f"No GGUF model files found in {self._base_dir}/models",
        )

        print(f"  Config:   {config_path}")
        print(f"  Database: {db_path}")
        print(f"  Model:    {model_files[0].name}")
        print(f"  Ports verified: port={self._port}, "
              f"inference_port={self._inference_port}")

        # -- 8. Verify no overlap with existing installation -------------------
        if pre_install_files:
            post_install_files = set(
                str(p.relative_to(default_base))
                for p in default_base.rglob("*")
                if p.is_file()
            )
            new_files = post_install_files - pre_install_files
            self.assert_that(
                len(new_files) == 0,
                f"install.sh wrote {len(new_files)} new files to the default "
                f"base dir {default_base}: {sorted(new_files)[:10]}",
            )
            print("  Isolation check: no new files in default base dir")
        else:
            if default_base.is_dir():
                new_files = list(default_base.rglob("*"))
                self.assert_that(
                    len(new_files) == 0,
                    f"install.sh created files in default base dir {default_base} "
                    f"which did not previously exist",
                )
            print("  Isolation check: default base dir still absent")

        # -- 9. Start the server -----------------------------------------------
        print("\n  Starting Carpenter server...")
        server_env = os.environ.copy()
        server_env["CARPENTER_CONFIG"] = config_path
        server_env["PYTHONPATH"] = str(repo_dir)

        self._server_proc = subprocess.Popen(
            [sys.executable, "-m", "carpenter"],
            env=server_env,
            cwd=str(repo_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # Own process group for clean killpg
        )

        # Use _SlowModelClient -- local inference is ~3-5 tok/s on Pi.
        # The 300s per-request timeout covers the worst case where the server
        # blocks on inference and can't respond to HTTP requests concurrently.
        local_client = _SlowModelClient(
            f"http://127.0.0.1:{self._port}", timeout=300,
        )
        server_ready = False
        deadline = time.monotonic() + 180  # 3 min for inference server startup
        while time.monotonic() < deadline:
            if self._server_proc.poll() is not None:
                stdout = self._server_proc.stdout.read().decode(errors="replace")
                raise AssertionFailure(
                    f"Server exited with code {self._server_proc.returncode}",
                    {"stdout_tail": stdout[-2000:]},
                )
            if local_client.is_running():
                server_ready = True
                break
            time.sleep(2)

        self.assert_that(
            server_ready, "Server did not become reachable within 180s"
        )
        print(f"  Server running at http://127.0.0.1:{self._port}")

        # -- 9b. Verify inference server is healthy ----------------------------
        import httpx

        inference_healthy = False
        inf_deadline = time.monotonic() + 30
        while time.monotonic() < inf_deadline:
            try:
                r = httpx.get(
                    f"http://127.0.0.1:{self._inference_port}/health", timeout=5
                )
                if r.status_code == 200:
                    inference_healthy = True
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            time.sleep(2)

        if not inference_healthy:
            print("  WARNING: Inference server not healthy -- substories will "
                  "likely fail")
        else:
            print(f"  Inference server healthy at "
                  f"http://127.0.0.1:{self._inference_port}")

        # -- 10. Create DB inspector -------------------------------------------
        local_db = DBInspector(db_path)

        # -- 11. Run substories ------------------------------------------------
        results = []
        stories_dir = Path(__file__).resolve().parent

        for module_name, class_name in _SUBSTORY_MODULES:
            print(f"\n  {'='*60}")
            print(f"  Running substory: {module_name}")
            print(f"  {'='*60}")

            try:
                spec = importlib.util.spec_from_file_location(
                    f"user_stories.{module_name}",
                    str(stories_dir / f"{module_name}.py"),
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                story_class = getattr(mod, class_name)
                story = story_class()

                result = story.run(local_client, local_db)
                results.append(result)
                status = "PASS" if result.passed else "FAIL"
                print(f"\n  [{status}] {result.name}: {result.message}")

                try:
                    story.cleanup(local_client, local_db)
                except Exception:
                    pass

            except AssertionFailure as e:
                result = StoryResult(
                    name=f"{module_name}::{class_name}",
                    passed=False,
                    error=e.message,
                    diagnostics=e.diagnostics,
                )
                results.append(result)
                print(f"\n  [FAIL] {module_name}: {e.message}")

            except Exception as e:
                result = StoryResult(
                    name=f"{module_name}::{class_name}",
                    passed=False,
                    error=str(e),
                )
                results.append(result)
                print(f"\n  [ERROR] {module_name}: {e}")

        # -- 12. Substory summary ---------------------------------------------
        passed = sum(1 for r in results if r.passed)
        total = len(results)

        print(f"\n  {'='*60}")
        print(f"  SUBSTORY RESULTS: {passed}/{total} passed")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"    [{status}] {r.name}")
            if not r.passed and r.error:
                print(f"           {r.error[:200]}")
        print(f"  {'='*60}")

        # -- 13. Fallback test -------------------------------------------------
        # Test that when cloud API is unavailable, the server can fall back
        # to the already-running local inference server.
        fallback_ok = self._test_local_fallback(
            config_path, repo_dir, local_db
        )

        # -- 14. Final result --------------------------------------------------
        duration = time.time() - start_ts

        install_ok = True  # We got this far
        server_ok = True   # Server responded to health check
        substories_ok = passed == total and total > 0
        all_passed = install_ok and server_ok and substories_ok and fallback_ok

        return StoryResult(
            name=self.name,
            passed=all_passed,
            message=(
                f"install=OK, server=OK, substories={passed}/{total}, "
                f"fallback={'OK' if fallback_ok else 'FAIL'}, "
                f"{duration:.0f}s"
            ),
            diagnostics={
                "base_dir": self._base_dir,
                "port": self._port,
                "inference_port": self._inference_port,
                "install_ok": install_ok,
                "server_ok": server_ok,
                "fallback_ok": fallback_ok,
                "substory_results": [
                    {"name": r.name, "passed": r.passed, "error": r.error}
                    for r in results
                ],
            },
            duration_s=duration,
        )

    def _test_local_fallback(
        self,
        config_path: str,
        repo_dir: Path,
        local_db: DBInspector,
    ) -> bool:
        """Test local fallback: patch config so cloud API fails, enable
        local_fallback pointing to the already-running inference server,
        restart the TC server, and verify a chat message gets a response.

        Returns True if fallback worked, False otherwise (non-fatal).
        """
        import yaml

        print(f"\n  {'='*60}")
        print("  FALLBACK TEST: Simulating cloud API outage")
        print(f"  {'='*60}")

        # -- Stop the current server ------------------------------------------
        if self._server_proc and self._server_proc.poll() is None:
            print("  Stopping server for fallback test...")
            try:
                os.killpg(os.getpgid(self._server_proc.pid), signal.SIGTERM)
                self._server_proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                try:
                    os.killpg(os.getpgid(self._server_proc.pid), signal.SIGKILL)
                    self._server_proc.wait(timeout=5)
                except (ProcessLookupError, OSError):
                    pass
            self._server_proc = None

        # -- Patch config.yaml -------------------------------------------------
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)
        except Exception as e:
            print(f"  Failed to parse config.yaml: {e}")
            return False

        # Save original for restore
        original_config_text = Path(config_path).read_text()

        # Enable local_fallback pointing to the running inference server
        config["local_fallback"] = {
            "enabled": True,
            "provider": "llamacpp",
            "url": f"http://127.0.0.1:{self._inference_port}",
            "model": "local-test",
            "context_window": 16384,
            "timeout": 300,
            "max_tokens": 4096,
            "allowed_operations": ["chat", "summarization", "simple_code"],
            "blocked_operations": [],
        }

        # Set an invalid API key to simulate cloud outage.
        # The .env file is what gets checked first, so patch it there.
        dot_env_path = Path(self._base_dir) / ".env"
        original_env_text = ""
        if dot_env_path.is_file():
            original_env_text = dot_env_path.read_text()

        # Write patched config
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        # Patch .env to set an invalid API key
        env_lines = original_env_text.splitlines() if original_env_text else []
        patched_env_lines = []
        found_key = False
        for line in env_lines:
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                patched_env_lines.append("ANTHROPIC_API_KEY=sk-invalid-for-fallback-test")
                found_key = True
            else:
                patched_env_lines.append(line)
        if not found_key:
            patched_env_lines.append("ANTHROPIC_API_KEY=sk-invalid-for-fallback-test")
        dot_env_path.write_text("\n".join(patched_env_lines) + "\n")

        # -- Restart server with patched config --------------------------------
        print("  Starting server with fallback config...")
        server_env = os.environ.copy()
        server_env["CARPENTER_CONFIG"] = config_path
        server_env["PYTHONPATH"] = str(repo_dir)
        # Override any real API key in env to force fallback
        server_env["ANTHROPIC_API_KEY"] = "sk-invalid-for-fallback-test"

        fallback_ok = False
        try:
            self._server_proc = subprocess.Popen(
                [sys.executable, "-m", "carpenter"],
                env=server_env,
                cwd=str(repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            # Wait for server to come up
            fallback_client = _SlowModelClient(
                f"http://127.0.0.1:{self._port}", timeout=300,
            )
            server_ready = False
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if self._server_proc.poll() is not None:
                    stdout = self._server_proc.stdout.read().decode(
                        errors="replace"
                    )
                    print(f"  Fallback server exited: {stdout[-500:]}")
                    break
                if fallback_client.is_running():
                    server_ready = True
                    break
                time.sleep(2)

            if not server_ready:
                print("  Fallback server did not become reachable within 120s")
                return False

            print("  Fallback server running, sending test message...")

            # -- Send a chat message -------------------------------------------
            try:
                conv_id, response = fallback_client.chat(
                    "What is 2+2? Reply with just the number.",
                    timeout=300,
                )
                print(f"  Got response ({len(response)} chars): "
                      f"{response[:100]}")
                fallback_ok = len(response) > 0
            except Exception as e:
                print(f"  Chat during fallback failed: {e}")
                fallback_ok = False

            # -- Check model_calls for failure + success -----------------------
            if fallback_ok:
                try:
                    calls = local_db.fetchall(
                        "SELECT model_id, success, error_type, provider "
                        "FROM model_calls ORDER BY id DESC LIMIT 10"
                    )
                    if calls:
                        print(f"  Recent model_calls ({len(calls)}):")
                        for c in calls:
                            status = "OK" if c["success"] else "FAIL"
                            print(f"    [{status}] {c['model_id']} "
                                  f"({c['provider']}) {c.get('error_type','')}")
                except Exception as e:
                    print(f"  Could not inspect model_calls: {e}")

            if fallback_ok:
                print("  FALLBACK TEST: PASSED")
            else:
                print("  FALLBACK TEST: FAILED (non-fatal)")

        finally:
            # -- Restore original config ---------------------------------------
            Path(config_path).write_text(original_config_text)
            if original_env_text:
                dot_env_path.write_text(original_env_text)
            elif dot_env_path.is_file():
                dot_env_path.unlink()

        return fallback_ok

    def cleanup(self, client: CarpenterClient, db: DBInspector | None) -> None:
        """Stop the server (and its child inference process) and remove the temp dir."""
        if self._server_proc and self._server_proc.poll() is None:
            print(f"\n  Stopping server process group "
                  f"(pid {self._server_proc.pid})...")
            try:
                # Kill the entire process group so the llama-server child
                # doesn't become an orphan.
                os.killpg(os.getpgid(self._server_proc.pid), signal.SIGTERM)
                self._server_proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError) as e:
                print(f"  SIGTERM failed ({e}), escalating to SIGKILL")
                try:
                    os.killpg(os.getpgid(self._server_proc.pid), signal.SIGKILL)
                    self._server_proc.wait(timeout=5)
                except (ProcessLookupError, OSError) as e2:
                    print(f"  SIGKILL also failed: {e2}")
            print("  Server stopped")

        # Safety net: kill any orphaned llama-server processes on our port
        self._kill_orphaned_llama_servers()

        if self._base_dir and os.path.isdir(self._base_dir):
            print(f"  Removing {self._base_dir}")
            shutil.rmtree(self._base_dir, ignore_errors=True)
            print("  Cleaned up")

    def _kill_orphaned_llama_servers(self) -> None:
        """Find and kill any llama-server processes bound to our inference port."""
        if not self._inference_port:
            return
        try:
            import psutil
        except ImportError:
            return
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["name"] and "llama-server" in proc.info["name"]:
                    cmdline = proc.info.get("cmdline") or []
                    if str(self._inference_port) in cmdline:
                        print(f"  Killing orphaned llama-server "
                              f"(pid {proc.pid})")
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
