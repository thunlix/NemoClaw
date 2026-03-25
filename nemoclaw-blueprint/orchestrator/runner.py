#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
NemoClaw Blueprint Runner

Orchestrates OpenClaw sandbox lifecycle inside OpenShell.
Called by the thin TS plugin via subprocess.

Protocol:
  - stdout lines starting with PROGRESS:<0-100>:<label> are parsed as progress updates
  - stdout line RUN_ID:<id> reports the run identifier
  - exit code 0 = success, non-zero = failure
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml


def log(msg: str) -> None:
    print(msg, flush=True)


def progress(pct: int, label: str) -> None:
    print(f"PROGRESS:{pct}:{label}", flush=True)


def emit_run_id() -> str:
    rid = f"nc-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    print(f"RUN_ID:{rid}", flush=True)
    return rid


def load_blueprint() -> dict[str, Any]:
    blueprint_path = Path(os.environ.get("NEMOCLAW_BLUEPRINT_PATH", "."))
    bp_file = blueprint_path / "blueprint.yaml"
    if not bp_file.exists():
        log(f"ERROR: blueprint.yaml not found at {bp_file}")
        sys.exit(1)
    with bp_file.open() as f:
        return yaml.safe_load(f)


def run_cmd(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command as an argv list (never shell=True)."""
    return subprocess.run(
        args,
        check=check,
        capture_output=capture,
        text=True,
    )


def openshell_available() -> bool:
    """Check if openshell CLI is available."""
    return shutil.which("openshell") is not None


# ---------------------------------------------------------------------------
# Tether integration
# ---------------------------------------------------------------------------


def _tether_post(endpoint: str, path: str, body: dict) -> dict | None:
    """POST JSON to a Tether endpoint. Returns parsed response or None on failure."""
    url = f"{endpoint.rstrip('/')}{path}"
    data = json.dumps(body).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (URLError, OSError, json.JSONDecodeError) as exc:
        log(f"WARNING: Tether request to {path} failed: {exc}")
        return None


def setup_tether(blueprint: dict[str, Any]) -> dict[str, str]:
    """Register agent and commit intent with Tether.

    Reads the tether config from the blueprint, registers the agent,
    commits the declared intent, and returns env vars that should be
    set on the sandbox so the Tether bridge can connect.

    Returns:
        Dict of environment variables to pass to the sandbox supervisor:
        TETHER_ENDPOINT, TETHER_TASK_ID, TETHER_MODE.
        Empty dict if Tether is disabled or setup fails.
    """
    tether_cfg = blueprint.get("components", {}).get("tether", {})
    if not tether_cfg.get("enabled", False):
        log("Tether: disabled in blueprint, skipping")
        return {}

    endpoint = tether_cfg.get("endpoint", "")
    if not endpoint:
        log("WARNING: Tether enabled but no endpoint configured, skipping")
        return {}

    agent_id = tether_cfg.get("agent_id", "nemoclaw-agent")
    mode = tether_cfg.get("mode", "monitor")
    intent_cfg = tether_cfg.get("intent", {})

    # Step 1: Register agent (idempotent — returns existing agent if already registered)
    log(f"Tether: registering agent '{agent_id}' at {endpoint}")
    reg_result = _tether_post(endpoint, "/api/agents/register", {
        "agentId": agent_id,
        "metadata": {"source": "nemoclaw", "version": blueprint.get("version", "unknown")},
    })
    if reg_result is None:
        log("WARNING: Tether agent registration failed — continuing without Tether")
        return {}

    if reg_result.get("alreadyRegistered"):
        agent_info = reg_result.get("agent", {})
        log(f"Tether: agent already registered (tokens={agent_info.get('tokens')}, rep={agent_info.get('reputation')})")
    else:
        log("Tether: agent registered successfully")

    # Step 2: Commit intent — generate a unique task ID per run
    task_id = f"nc-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    intent = {
        "goal": intent_cfg.get("goal", "Assist user with development tasks"),
        "constraints": intent_cfg.get("constraints", []),
        "expectedOutputs": intent_cfg.get("expected_outputs", []),
        "driftPolicy": intent_cfg.get("drift_policy", {"mode": "warn", "threshold": 0.7}),
    }

    log(f"Tether: committing intent for task '{task_id}'")
    commit_result = _tether_post(endpoint, "/api/intent/commit", {
        "agentId": agent_id,
        "taskId": task_id,
        "intent": intent,
    })
    if commit_result is None:
        log("WARNING: Tether intent commit failed — continuing without Tether")
        return {}

    task_info = commit_result.get("task", {})
    staked = task_info.get("stakedTokens", "?")
    log(f"Tether: intent committed (task={task_id}, staked={staked} tokens)")
    log(f"Tether: goal = {intent['goal']}")
    log(f"Tether: mode = {mode}")

    return {
        "TETHER_ENDPOINT": endpoint,
        "TETHER_TASK_ID": task_id,
        "TETHER_MODE": mode,
    }


def _update_policy_tether_task_id(blueprint: dict[str, Any], task_id: str) -> None:
    """Write the Tether task_id into the sandbox policy YAML.

    The policy file's tether.task_id field starts blank in the template.
    After intent commit, we fill it so the sandbox supervisor's Tether
    bridge knows which task to report against.
    """
    blueprint_path = Path(os.environ.get("NEMOCLAW_BLUEPRINT_PATH", "."))
    policy_base = blueprint.get("components", {}).get("policy", {}).get("base", "")
    if not policy_base:
        return

    policy_file = blueprint_path / policy_base
    if not policy_file.exists():
        # Try relative to policies dir
        policy_file = blueprint_path / "policies" / "openclaw-sandbox.yaml"
    if not policy_file.exists():
        log(f"WARNING: Cannot find policy file to update tether task_id")
        return

    try:
        with policy_file.open() as f:
            policy = yaml.safe_load(f)

        if "tether" not in policy:
            policy["tether"] = {}
        policy["tether"]["task_id"] = task_id
        policy["tether"]["enabled"] = True

        with policy_file.open("w") as f:
            yaml.dump(policy, f, default_flow_style=False, sort_keys=False)
        log(f"Tether: updated policy with task_id={task_id}")
    except Exception as exc:
        log(f"WARNING: Failed to update policy tether task_id: {exc}")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def action_plan(
    profile: str,
    blueprint: dict[str, Any],
    *,
    dry_run: bool = False,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Plan the deployment: validate inputs, resolve profile, check prerequisites."""
    rid = emit_run_id()
    progress(10, "Validating blueprint")

    inference_profiles: dict[str, Any] = (
        blueprint.get("components", {}).get("inference", {}).get("profiles", {})
    )
    if profile not in inference_profiles:
        available = ", ".join(inference_profiles.keys())
        log(f"ERROR: Profile '{profile}' not found. Available: {available}")
        sys.exit(1)

    progress(20, "Checking prerequisites")
    if not openshell_available():
        log("ERROR: openshell CLI not found. Install OpenShell first.")
        log("  See: https://github.com/NVIDIA/OpenShell")
        sys.exit(1)

    sandbox_cfg: dict[str, Any] = blueprint.get("components", {}).get("sandbox", {})
    inference_cfg: dict[str, Any] = inference_profiles[profile]

    # Override endpoint if provided (e.g., NCP dynamic endpoint)
    if endpoint_url:
        inference_cfg = {**inference_cfg, "endpoint": endpoint_url}

    plan: dict[str, Any] = {
        "run_id": rid,
        "profile": profile,
        "sandbox": {
            "image": sandbox_cfg.get("image", "openclaw"),
            "name": sandbox_cfg.get("name", "openclaw"),
            "forward_ports": sandbox_cfg.get("forward_ports", [18789]),
        },
        "inference": {
            "provider_type": inference_cfg.get("provider_type"),
            "provider_name": inference_cfg.get("provider_name"),
            "endpoint": inference_cfg.get("endpoint"),
            "model": inference_cfg.get("model"),
            "credential_env": inference_cfg.get("credential_env"),
        },
        "policy_additions": (
            blueprint.get("components", {}).get("policy", {}).get("additions", {})
        ),
        "dry_run": dry_run,
    }

    progress(100, "Plan complete")
    log(json.dumps(plan, indent=2))
    return plan


def action_apply(
    profile: str,
    blueprint: dict[str, Any],
    plan_path: str | None = None,
    endpoint_url: str | None = None,
) -> None:
    """Apply the plan: create sandbox, configure provider, set inference route."""
    rid = emit_run_id()

    # Load plan if provided, otherwise generate one
    if plan_path:
        # In a real implementation, load the saved plan
        pass

    inference_profiles: dict[str, Any] = (
        blueprint.get("components", {}).get("inference", {}).get("profiles", {})
    )
    inference_cfg: dict[str, Any] = inference_profiles.get(profile, {})

    # Override endpoint if provided (e.g., NCP dynamic endpoint)
    if endpoint_url:
        inference_cfg = {**inference_cfg, "endpoint": endpoint_url}

    sandbox_cfg: dict[str, Any] = blueprint.get("components", {}).get("sandbox", {})

    sandbox_name: str = sandbox_cfg.get("name", "openclaw")
    sandbox_image: str = sandbox_cfg.get("image", "openclaw")
    forward_ports: list[int] = sandbox_cfg.get("forward_ports", [18789])

    # Step 1: Create sandbox
    progress(20, "Creating OpenClaw sandbox")
    create_args = [
        "openshell",
        "sandbox",
        "create",
        "--from",
        sandbox_image,
        "--name",
        sandbox_name,
    ]
    for port in forward_ports:
        create_args.extend(["--forward", str(port)])

    result = run_cmd(create_args, check=False, capture=True)
    if result.returncode != 0:
        if "already exists" in (result.stderr or ""):
            log(f"Sandbox '{sandbox_name}' already exists, reusing.")
        else:
            log(f"ERROR: Failed to create sandbox: {result.stderr}")
            sys.exit(1)

    # Step 2: Configure inference provider
    progress(50, "Configuring inference provider")
    provider_name: str = inference_cfg.get("provider_name", "default")
    provider_type: str = inference_cfg.get("provider_type", "openai")
    endpoint: str = inference_cfg.get("endpoint", "")
    model: str = inference_cfg.get("model", "")

    # Resolve credential from environment
    credential_env = inference_cfg.get("credential_env")
    credential_default: str = inference_cfg.get("credential_default", "")
    credential = ""
    if credential_env:
        credential = os.environ.get(credential_env, credential_default)

    provider_args = [
        "openshell",
        "provider",
        "create",
        "--name",
        provider_name,
        "--type",
        provider_type,
    ]
    if credential:
        provider_args.extend(["--credential", f"OPENAI_API_KEY={credential}"])
    if endpoint:
        provider_args.extend(["--config", f"OPENAI_BASE_URL={endpoint}"])

    run_cmd(provider_args, check=False, capture=True)

    # Step 3: Set inference route
    progress(70, "Setting inference route")
    run_cmd(
        ["openshell", "inference", "set", "--provider", provider_name, "--model", model],
        check=False,
        capture=True,
    )

    # Step 4: Set up Tether (behavioral drift enforcement)
    progress(80, "Configuring Tether")
    tether_env = setup_tether(blueprint)
    if tether_env:
        # Write Tether task_id back into the policy so the sandbox supervisor's
        # bridge can read it. The policy YAML tether.task_id field was blank in
        # the template — we fill it with the task ID generated during intent commit.
        _update_policy_tether_task_id(blueprint, tether_env.get("TETHER_TASK_ID", ""))

        # Also export as env vars so they're available to any openshell commands
        # run in this process (e.g., policy set).
        for key, value in tether_env.items():
            os.environ[key] = value

    # Step 5: Save run state
    progress(90, "Saving run state")
    state_dir = Path.home() / ".nemoclaw" / "state" / "runs" / rid
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "plan.json").write_text(
        json.dumps(
            {
                "run_id": rid,
                "profile": profile,
                "sandbox_name": sandbox_name,
                "inference": inference_cfg,
                "tether": tether_env or None,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
    )

    progress(100, "Apply complete")
    log(f"Sandbox '{sandbox_name}' is ready.")
    log(f"Inference: {provider_name} -> {model} @ {endpoint}")
    if tether_env:
        log(f"Tether: {tether_env.get('TETHER_MODE', 'monitor')} mode, task={tether_env.get('TETHER_TASK_ID', '?')}")


def action_status(rid: str | None = None) -> None:
    """Report current state of the most recent (or specified) run."""
    emit_run_id()
    state_dir = Path.home() / ".nemoclaw" / "state" / "runs"

    if rid:
        run_dir = state_dir / rid
    else:
        if not state_dir.exists():
            log("No runs found.")
            sys.exit(0)
        runs = sorted(state_dir.iterdir(), reverse=True)
        if not runs:
            log("No runs found.")
            sys.exit(0)
        run_dir = runs[0]

    plan_file = run_dir / "plan.json"
    if plan_file.exists():
        log(plan_file.read_text())
    else:
        log(json.dumps({"run_id": run_dir.name, "status": "unknown"}))


def action_rollback(rid: str) -> None:
    """Rollback a specific run: stop sandbox, remove provider config."""
    emit_run_id()

    state_dir = Path.home() / ".nemoclaw" / "state" / "runs" / rid
    if not state_dir.exists():
        log(f"ERROR: Run {rid} not found.")
        sys.exit(1)

    plan_file = state_dir / "plan.json"
    if plan_file.exists():
        plan = json.loads(plan_file.read_text())
        sandbox_name = plan.get("sandbox_name", "openclaw")

        progress(30, f"Stopping sandbox {sandbox_name}")
        run_cmd(
            ["openshell", "sandbox", "stop", sandbox_name],
            check=False,
            capture=True,
        )

        progress(60, f"Removing sandbox {sandbox_name}")
        run_cmd(
            ["openshell", "sandbox", "remove", sandbox_name],
            check=False,
            capture=True,
        )

    progress(90, "Cleaning up run state")
    (state_dir / "rolled_back").write_text(datetime.now(UTC).isoformat())

    progress(100, "Rollback complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="NemoClaw Blueprint Runner")
    parser.add_argument("action", choices=["plan", "apply", "status", "rollback"])
    parser.add_argument("--profile", default="default")
    parser.add_argument("--plan", dest="plan_path")
    parser.add_argument("--run-id", dest="run_id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--endpoint-url",
        dest="endpoint_url",
        default=None,
        help="Override endpoint URL for the selected profile",
    )

    args = parser.parse_args()
    blueprint = load_blueprint()

    if args.action == "plan":
        action_plan(args.profile, blueprint, dry_run=args.dry_run, endpoint_url=args.endpoint_url)
    elif args.action == "apply":
        action_apply(
            args.profile, blueprint, plan_path=args.plan_path, endpoint_url=args.endpoint_url
        )
    elif args.action == "status":
        action_status(rid=args.run_id)
    elif args.action == "rollback":
        if not args.run_id:
            log("ERROR: --run-id is required for rollback")
            sys.exit(1)
        action_rollback(args.run_id)


if __name__ == "__main__":
    main()
