#!/usr/bin/env python3
"""
extend_AWS_vol.py
------------------
Two-stage orchestrator for extending an EBS volume in response to a
disk-utilization alert (e.g. raised via Jira automation and dispatched
through the extend-ebs-volume.yml GitHub Actions workflow).

Stages
------
  plan   Resolve the host, check current disk usage, locate the EBS volume
         backing the target drive, and compute a proposed new size. Writes
         the result to a plan JSON file and prints a human-readable summary.
         Never modifies anything in AWS — safe to run repeatedly.

  apply  Loads an approved plan, re-validates that the volume's current
         size still matches what the plan was computed against (aborts if
         it drifted — the plan went stale between approval and execution),
         calls ec2_client.modify_volume(), and polls
         describe_volumes_modifications() until the resize reaches
         "optimizing" or "completed". Does NOT run any in-OS filesystem
         extend commands (growpart/resize2fs/Resize-Partition) — that is a
         separate follow-up step.

Usage
-----
  python extend_AWS_vol.py plan  --host <id-or-ip-or-tag> --drive <drive-or-mount> --region <region> [--plan-file plan.json]
  python extend_AWS_vol.py apply --plan plan.json [--timeout 900] [--result-file apply_result.json]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from resolve_instance_id import resolve_instance_id
from detect_os import detect_os
from find_EBS_vol import find_volume_for_drive
from check_disk import get_disk_usage

MAX_VOLUME_SIZE_GB = 16384          # AWS hard limit (gp2/gp3/io1/io2)
USED_PERCENT_THRESHOLD = 70         # Only propose a resize at/above this
DEFAULT_APPLY_TIMEOUT_SECONDS = 900 # 15 minutes to reach optimizing/completed
POLL_INTERVAL_SECONDS = 15


# ---------------------------------------------------------------------------
# Pure sizing logic — no AWS calls, easy to unit test.
# ---------------------------------------------------------------------------

def calculate_new_size(current_size_gb):
    """
    Compute the proposed new size for an EBS volume based on its current size.

    Tiers (increment added to current_size_gb):
        current < 1000 GB              -> +50 GB
        1000 GB <= current < 5000 GB   -> +100 GB
        5000 GB <= current < 10000 GB  -> +500 GB
        current >= 10000 GB            -> +1000 GB

    The result is capped at MAX_VOLUME_SIZE_GB. If current_size_gb is
    already at or above MAX_VOLUME_SIZE_GB, no resize is possible at all —
    capped_at_max is set True and new_size_gb is returned unchanged
    (equal to current_size_gb) so callers can surface a "manual
    intervention required" message instead of proposing a resize.

    Returns:
        dict: {"new_size_gb": int, "tier": str | None, "capped_at_max": bool}
    """
    if current_size_gb >= MAX_VOLUME_SIZE_GB:
        return {
            "new_size_gb": current_size_gb,
            "tier": None,
            "capped_at_max": True,
        }

    if current_size_gb < 1000:
        tier, increment = "under_1000gb", 50
    elif current_size_gb < 5000:
        tier, increment = "1000_to_5000gb", 100
    elif current_size_gb < 10000:
        tier, increment = "5000_to_10000gb", 500
    else:
        tier, increment = "10000gb_and_above", 1000

    new_size_gb = min(current_size_gb + increment, MAX_VOLUME_SIZE_GB)

    return {
        "new_size_gb": new_size_gb,
        "tier": tier,
        "capped_at_max": False,
    }


# ---------------------------------------------------------------------------
# plan stage
# ---------------------------------------------------------------------------

def build_plan(host, drive, region):
    """
    Resolve the host, check disk usage, and compute a sizing proposal.
    Never modifies AWS state.

    Returns a JSON-serializable plan dict. The "action" field drives what
    happens next:
        "no_action_needed"           — used % below threshold.
        "resize"                     — a resize is proposed.
        "manual_intervention_required" — volume already at/above the AWS max.

    Raises:
        ValueError: host could not be resolved to a running, SSM-managed
                    instance.
        RuntimeError: disk usage output from SSM could not be parsed.
        ClientError: unexpected AWS API error.
    """
    ec2 = boto3.client("ec2", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    instance_id = resolve_instance_id(ec2, host, ssm)
    platform = detect_os(instance_id, region)
    usage = get_disk_usage(ssm, instance_id, platform, drive)

    plan = {
        "schema_version": 1,
        "generated_at": _utcnow_iso(),
        "host_identifier": host,
        "instance_id": instance_id,
        "region": region,
        "platform": platform,
        "drive": drive,
        "disk_usage": usage,
    }

    if usage["used_percent"] < USED_PERCENT_THRESHOLD:
        plan["action"] = "no_action_needed"
        plan["reason"] = (
            f"Used space is {usage['used_percent']}%, below the "
            f"{USED_PERCENT_THRESHOLD}% threshold. No resize proposed."
        )
        return plan

    volume_id = find_volume_for_drive(ec2, instance_id, platform, drive)
    volume = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    current_size_gb = volume["Size"]

    sizing = calculate_new_size(current_size_gb)
    plan["volume_id"] = volume_id
    plan["current_size_gb"] = current_size_gb
    plan["sizing_tier"] = sizing["tier"]
    plan["capped_at_max"] = sizing["capped_at_max"]

    if sizing["capped_at_max"]:
        plan["proposed_new_size_gb"] = None
        plan["action"] = "manual_intervention_required"
        plan["reason"] = (
            f"Volume {volume_id} is already at or above the AWS maximum of "
            f"{MAX_VOLUME_SIZE_GB} GB (current: {current_size_gb} GB). "
            f"No further automatic resize is possible — manual intervention "
            f"required (e.g. add a new volume / LVM extend)."
        )
    else:
        plan["proposed_new_size_gb"] = sizing["new_size_gb"]
        plan["action"] = "resize"
        plan["reason"] = (
            f"Used space is {usage['used_percent']}%, at/above the "
            f"{USED_PERCENT_THRESHOLD}% threshold. Proposing resize of "
            f"{volume_id} from {current_size_gb} GB to "
            f"{sizing['new_size_gb']} GB (tier: {sizing['tier']})."
        )

    return plan


def format_plan_summary(plan):
    """Human-readable Markdown summary — printed to stdout and, in CI, to $GITHUB_STEP_SUMMARY."""
    usage = plan["disk_usage"]
    lines = [
        "# EBS Volume Extend — Plan",
        "",
        f"- **Host identifier:** {plan['host_identifier']}",
        f"- **Instance ID:** {plan['instance_id']}",
        f"- **Platform:** {plan['platform']}",
        f"- **Region:** {plan['region']}",
        f"- **Drive/mount:** {plan['drive']}",
        f"- **Current used %:** {usage['used_percent']}%",
        f"- **Current disk usage:** {usage['used_gb']} GB used / {usage['total_gb']} GB total "
        f"({usage['free_gb']} GB free)",
    ]

    if "volume_id" in plan:
        lines.append(f"- **Volume ID:** {plan['volume_id']}")
    if "current_size_gb" in plan:
        lines.append(f"- **Current volume size:** {plan['current_size_gb']} GB")
    if plan.get("sizing_tier"):
        lines.append(f"- **Sizing tier applied:** {plan['sizing_tier']}")
    lines.append(f"- **Proposed new size:** {plan.get('proposed_new_size_gb') or 'n/a'} GB")
    lines.append(f"- **capped_at_max:** {plan.get('capped_at_max', False)}")
    lines.append(f"- **Action:** `{plan['action']}`")
    lines.append(f"- **Reason:** {plan['reason']}")

    return "\n".join(lines)


def run_plan(host, drive, region, plan_file):
    plan = build_plan(host, drive, region)

    with open(plan_file, "w") as f:
        json.dump(plan, f, indent=2)

    summary = format_plan_summary(plan)
    print(summary)
    _append_step_summary(summary)
    _write_gha_output({"action": plan["action"], "plan_file": plan_file})

    return plan


# ---------------------------------------------------------------------------
# apply stage
# ---------------------------------------------------------------------------

def run_apply(plan_file, timeout=DEFAULT_APPLY_TIMEOUT_SECONDS):
    """
    Load an approved plan and apply it.

    Raises:
        ValueError: plan action isn't "resize", or the volume's size drifted
                    since the plan was generated (stale plan).
        ClientError: unexpected AWS API error.
        TimeoutError: modification didn't reach optimizing/completed in time.
        RuntimeError: the modification reached a "failed" state.
    """
    with open(plan_file) as f:
        plan = json.load(f)

    if plan.get("action") != "resize":
        raise ValueError(
            f"Plan action is {plan.get('action')!r}, not 'resize' — nothing to apply. "
            f"Reason recorded in the plan: {plan.get('reason')}"
        )

    region = plan["region"]
    volume_id = plan["volume_id"]
    expected_size = plan["current_size_gb"]
    new_size = plan["proposed_new_size_gb"]

    ec2 = boto3.client("ec2", region_name=region)

    volume = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    actual_size = volume["Size"]

    if actual_size != expected_size:
        raise ValueError(
            f"Plan is stale: volume {volume_id} is now {actual_size} GB but "
            f"the plan was generated against {expected_size} GB. The volume "
            f"changed between approval and execution — re-run 'plan' and "
            f"get it re-approved."
        )

    ec2.modify_volume(VolumeId=volume_id, Size=new_size)
    modification_state = _poll_modification(ec2, volume_id, timeout)

    return {
        "instance_id": plan["instance_id"],
        "volume_id": volume_id,
        "drive": plan["drive"],
        "old_size_gb": actual_size,
        "new_size_gb": new_size,
        "used_percent_at_resize_time": plan["disk_usage"]["used_percent"],
        "capped_at_max": plan.get("capped_at_max", False),
        "modification_state": modification_state,
        "success": True,
        "error": None,
        "applied_at": _utcnow_iso(),
    }


def _poll_modification(ec2_client, volume_id, timeout):
    """Poll describe_volumes_modifications() until optimizing/completed, or raise."""
    deadline = time.time() + timeout
    last_state = None

    while time.time() < deadline:
        resp = ec2_client.describe_volumes_modifications(VolumeIds=[volume_id])
        mods = resp.get("VolumesModifications", [])
        if mods:
            last_state = mods[0]["ModificationState"]
            print(f"[INFO] Volume {volume_id} modification state: {last_state}", flush=True)
            if last_state in ("optimizing", "completed"):
                return last_state
            if last_state == "failed":
                raise RuntimeError(f"Volume modification failed for {volume_id}.")
        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"Timed out after {timeout}s waiting for volume {volume_id} to reach "
        f"'optimizing' or 'completed' (last observed state: {last_state})."
    )


def format_apply_summary(result):
    """Human-readable Markdown summary for $GITHUB_STEP_SUMMARY / Jira comment."""
    lines = [
        "# EBS Volume Extend — Applied",
        "",
        f"- **Instance ID:** {result['instance_id']}",
        f"- **Volume ID:** {result['volume_id']}",
        f"- **Drive/mount:** {result['drive']}",
        f"- **Old size:** {result['old_size_gb']} GB",
        f"- **New size:** {result['new_size_gb']} GB",
        f"- **Used % at resize time:** {result['used_percent_at_resize_time']}%",
        f"- **Modification state:** {result['modification_state']}",
        f"- **Success:** {result['success']}",
    ]
    if result.get("error"):
        lines.append(f"- **Error:** {result['error']}")
    lines.append(
        "\n_Note: the in-OS filesystem (growpart/resize2fs/Resize-Partition) "
        "has NOT been extended yet — that is a separate follow-up step._"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub Actions integration helpers
# ---------------------------------------------------------------------------

def _utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_step_summary(markdown):
    """Append Markdown to $GITHUB_STEP_SUMMARY, if running in GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(markdown + "\n")


def _write_gha_output(values):
    """Write output variables to $GITHUB_OUTPUT, if running in GitHub Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser():
    parser = argparse.ArgumentParser(description="Plan and apply EBS volume extensions.")
    sub = parser.add_subparsers(dest="stage", required=True)

    plan_p = sub.add_parser("plan", help="Check disk usage and produce a resize plan. Makes no AWS changes.")
    plan_p.add_argument("--host", required=True, help="Instance ID, private/public IP, or tag value.")
    plan_p.add_argument("--drive", required=True, help="Drive letter (Windows) or mount path (Linux).")
    plan_p.add_argument("--region", required=True, help="AWS region.")
    plan_p.add_argument("--plan-file", default="plan.json", help="Where to write the plan JSON (default: plan.json).")

    apply_p = sub.add_parser("apply", help="Apply an approved plan (calls modify_volume).")
    apply_p.add_argument("--plan", required=True, dest="plan_file", help="Path to the plan JSON produced by 'plan'.")
    apply_p.add_argument("--timeout", type=int, default=DEFAULT_APPLY_TIMEOUT_SECONDS,
                          help=f"Seconds to wait for the modification to reach optimizing/completed (default: {DEFAULT_APPLY_TIMEOUT_SECONDS}).")
    apply_p.add_argument("--result-file", default="apply_result.json", help="Where to write the apply result JSON.")

    return parser


def main():
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.stage == "plan":
        try:
            plan = run_plan(args.host, args.drive, args.region, args.plan_file)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except ClientError as exc:
            print(f"AWS error: {exc}", file=sys.stderr)
            sys.exit(1)

        # A successful plan run always exits 0, whatever the action —
        # "no_action_needed" and "manual_intervention_required" are valid,
        # non-error outcomes. The caller (the GitHub Actions workflow)
        # branches on the "action" job output to decide whether to proceed
        # to the approval gate.
        if plan["action"] == "manual_intervention_required":
            print("\nManual intervention required — see reason above.", file=sys.stderr)
        return

    # apply
    try:
        result = run_apply(args.plan_file, args.timeout)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        with open(args.result_file, "w") as f:
            json.dump({"success": False, "error": str(exc)}, f, indent=2)
        _append_step_summary(f"# EBS Volume Extend — FAILED\n\n**Error:** {exc}")
        sys.exit(1)
    except ClientError as exc:
        print(f"AWS error: {exc}", file=sys.stderr)
        with open(args.result_file, "w") as f:
            json.dump({"success": False, "error": str(exc)}, f, indent=2)
        _append_step_summary(f"# EBS Volume Extend — FAILED\n\n**AWS error:** {exc}")
        sys.exit(1)

    with open(args.result_file, "w") as f:
        json.dump(result, f, indent=2)

    summary = format_apply_summary(result)
    print(summary)
    _append_step_summary(summary)


if __name__ == "__main__":
    main()
