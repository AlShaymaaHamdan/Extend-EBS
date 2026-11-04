#!/usr/bin/env python3
"""
verify_extension.py
--------------------
Standalone post-extension check: given a host/drive that was already
resized at the AWS volume level (via extend_AWS_vol.py, run on some
earlier date/run), re-checks current disk usage and compares it against
the before-extension baseline supplied by the caller.

This is intentionally decoupled from extend_AWS_vol.py's plan/apply
artifacts: resizing and verifying often happen in separate runs (the
in-OS filesystem extend -- growpart/resize2fs/Resize-Partition -- is a
manual step not automated anywhere in this repo yet), so the "before"
baseline is passed in directly rather than read back from a prior run's
artifact.

"Improved" is decided purely by used_percent dropping:

  - If used_percent dropped: post a before/after comment (size + used%)
    on the Jira ticket and resolve it.
  - If it did not: the AWS volume may have been resized without the
    in-OS filesystem being grown to use the extra space. Post a
    manual-review comment instead and leave the ticket open -- this
    script never resolves a ticket where usage didn't actually improve.

Usage:
  python verify_extension.py \
      --issue-key NOC-1234 \
      --host i-0123456789abcdef0 \
      --drive D \
      --region us-east-1 \
      --before-total-gb 100 \
      --before-used-percent 92 \
      [--resolution-status Resolved] \
      [--result-file verify_result.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from resolve_instance_id import resolve_instance_id
from detect_os import detect_os
from check_disk import get_disk_usage

sys.path.append("Jira")
from jira_client import JiraError, post_adf_comment, change_ticket_status


def _utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_verification(host, drive, region, before_total_gb, before_used_percent):
    """
    Resolve the host and re-check current disk usage against the supplied
    before-extension baseline. Never modifies AWS or Jira.

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
    after = get_disk_usage(ssm, instance_id, platform, drive)

    improved = after["used_percent"] < before_used_percent

    return {
        "schema_version": 1,
        "checked_at": _utcnow_iso(),
        "host_identifier": host,
        "instance_id": instance_id,
        "region": region,
        "platform": platform,
        "drive": drive,
        "before_total_gb": before_total_gb,
        "before_used_percent": before_used_percent,
        "after_total_gb": after["total_gb"],
        "after_used_gb": after["used_gb"],
        "after_free_gb": after["free_gb"],
        "after_used_percent": after["used_percent"],
        "improved": improved,
        "action": "resolve" if improved else "manual_review_required",
    }


def format_before_after_report(result) -> str:
    """Report body for the improved case -- before/after size + used%."""
    return "\n".join([
        f"Host:           {result['host_identifier']} ({result['instance_id']}, {result['region']})",
        f"Drive/mount:    {result['drive']}",
        "",
        f"Before:  {result['before_total_gb']} GB total | {result['before_used_percent']}% used",
        f"After:   {result['after_total_gb']} GB total | {result['after_used_percent']}% used "
        f"({result['after_used_gb']} GB used / {result['after_free_gb']} GB free)",
        "",
        f"Used % dropped from {result['before_used_percent']}% to "
        f"{result['after_used_percent']}% -- extension confirmed effective. Ticket resolved.",
    ])


def format_manual_review_report(result) -> str:
    """Report body for the not-improved case -- explains why, asks for manual review."""
    return "\n".join([
        f"Host:           {result['host_identifier']} ({result['instance_id']}, {result['region']})",
        f"Drive/mount:    {result['drive']}",
        "",
        f"Before:  {result['before_total_gb']} GB total | {result['before_used_percent']}% used",
        f"After:   {result['after_total_gb']} GB total | {result['after_used_percent']}% used "
        f"({result['after_used_gb']} GB used / {result['after_free_gb']} GB free)",
        "",
        f"Used % did not drop (before: {result['before_used_percent']}%, "
        f"after: {result['after_used_percent']}%). The AWS volume may have been resized "
        f"without the in-OS filesystem being extended (growpart/resize2fs on Linux, "
        f"Resize-Partition on Windows). Needs manual review -- ticket left open.",
    ])


def build_adf_comment(title: str, body_text: str) -> dict:
    """
    Heading + codeBlock ADF comment -- same pattern as
    disk_review/run_disk_review.py's build_adf_comment(), since a plain
    paragraph (post_comment()) does not preserve line breaks.
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": title}],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "text"},
                "content": [{"type": "text", "text": body_text}],
            },
        ],
    }


def _append_step_summary(markdown):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(markdown + "\n")


def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Verify a disk extension took effect; comment on and resolve the Jira ticket if it did."
    )
    parser.add_argument("--issue-key", required=True, help="Jira issue key or URL, e.g. NOC-1234.")
    parser.add_argument("--host", required=True, help="Instance ID, private/public IP, or tag value.")
    parser.add_argument("--drive", required=True, help="Drive letter (Windows) or mount path (Linux).")
    parser.add_argument("--region", required=True, help="AWS region.")
    parser.add_argument("--before-total-gb", required=True, type=float, help="Volume size (GB) before the extension.")
    parser.add_argument("--before-used-percent", required=True, type=float, help="Used space (%) before the extension.")
    parser.add_argument("--resolution-status", default="Resolved",
                         help="Jira transition name to apply when usage improved (default: Resolved).")
    parser.add_argument("--result-file", default="verify_result.json", help="Where to write the verification result JSON.")
    return parser


def _post_comment(issue_key, title, body_text):
    try:
        post_adf_comment(issue_key, build_adf_comment(title, body_text))
    except JiraError as exc:
        print(f"::error::Failed to post Jira comment for {issue_key}: {exc}")
        sys.exit(1)


def main():
    args = _build_arg_parser().parse_args()

    try:
        result = build_verification(
            args.host, args.drive, args.region, args.before_total_gb, args.before_used_percent,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        _post_comment(
            args.issue_key,
            f"Disk Extension Verification — {args.host} — FAILED",
            f"Could not verify disk usage for {args.host} ({args.drive}, {args.region}): {exc}",
        )
        sys.exit(1)
    except ClientError as exc:
        print(f"AWS error: {exc}", file=sys.stderr)
        _post_comment(
            args.issue_key,
            f"Disk Extension Verification — {args.host} — FAILED",
            f"AWS error while verifying {args.host} ({args.drive}, {args.region}): {exc}",
        )
        sys.exit(1)

    with open(args.result_file, "w") as f:
        json.dump(result, f, indent=2)

    title = f"Disk Extension Verification — {result['host_identifier']} ({result['instance_id']}, {result['region']})"

    if result["improved"]:
        report = format_before_after_report(result)
        print(report)
        _append_step_summary(f"# Disk Extension Verification — Resolved\n\n```\n{report}\n```")
        _post_comment(args.issue_key, title, report)

        try:
            change_ticket_status(args.issue_key, args.resolution_status)
        except JiraError as exc:
            print(f"::error::Failed to resolve ticket {args.issue_key}: {exc}")
            sys.exit(1)
    else:
        report = format_manual_review_report(result)
        print(report)
        _append_step_summary(f"# Disk Extension Verification — Manual Review Required\n\n```\n{report}\n```")
        _post_comment(args.issue_key, f"{title} — Manual Review Required", report)


if __name__ == "__main__":
    main()
