#!/usr/bin/env python3
"""
resolve_instance_id.py
----------------------
Resolves any host identifier supplied in a Jira ticket to a running,
SSM-managed EC2 instance ID.

Supported identifier types (tried in priority order):
  1. EC2 instance ID  — i-0abc123def456789a
  2. Private IP       — 10.0.1.42
  3. Public IP        — 54.12.34.56
  4. tag:Name         — prod-web-01
  5. tag:Hostname     — prod-web-01.internal
  6. tag:Hostname (contains) — prod-web-01 matches prod-web-01.internal
"""

import re
import logging
from typing import Optional

from botocore.exceptions import ClientError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_instance_id(ec2_client, target: str, ssm_client=None) -> str:
    """
    Resolve a host identifier to a running EC2 instance ID.

    Accepts any of: instance ID, private IP, public IP, tag:Name, tag:Hostname.
    Resolution order is fixed (see module docstring) and stops at the first
    unambiguous match.

    Args:
        ec2_client: Boto3 EC2 client.
        target:     The raw identifier — instance ID, IP, hostname, or tag value.
        ssm_client: Optional Boto3 SSM client. When provided, the resolved instance
                    is confirmed to be SSM-managed before returning. Omit to skip
                    the SSM check (preserves backward compatibility).

    Returns:
        EC2 instance ID string (e.g. 'i-0abc123def456789a').

    Raises:
        ValueError:   Target is empty, not found, ambiguous, or (if ssm_client
                      provided) not registered in SSM.
        ClientError:  Unexpected AWS error (invalid credentials, throttling, etc.).
    """
    target = target.strip()
    if not target:
        raise ValueError("Target identifier is empty.")

    instance_id = _resolve_to_instance_id(ec2_client, target)

    if ssm_client is not None and not _is_ssm_managed(ssm_client, instance_id):
        raise ValueError(
            f"Instance {instance_id} (resolved from {target!r}) is not "
            f"registered in SSM. Verify the SSM agent is running and the "
            f"instance IAM role has the required SSM permissions."
        )

    log.info("Resolved %r → %s%s", target, instance_id,
             " (SSM-managed ✓)" if ssm_client is not None else "")
    return instance_id


# ---------------------------------------------------------------------------
# Internal — SSM check
# ---------------------------------------------------------------------------

def _is_ssm_managed(ssm_client, instance_id: str) -> bool:
    """Return True if the instance appears in SSM's managed-instance list."""
    resp = ssm_client.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )
    return bool(resp.get("InstanceInformationList"))


# ---------------------------------------------------------------------------
# Internal — EC2 resolution
# ---------------------------------------------------------------------------

def _resolve_to_instance_id(ec2_client, target: str) -> str:
    """
    Find the single running EC2 instance that matches *target*.
    Does not check SSM — that is the caller's responsibility.

    Resolution is attempted in this order:
      1. Instance ID (direct lookup, no filter overhead)
      2. Private IP, Public IP, tag:Name, tag:Hostname (exact), tag:Hostname
         (contains — matches a short name against an FQDN Hostname tag)

    Raises ValueError if zero or more than one instance matches.
    Re-raises unexpected ClientErrors; swallows only "not found" / invalid-filter
    errors so the next strategy can be attempted.
    """

    # ── Strategy 1: target looks like an instance ID ─────────────────────────
    if re.fullmatch(r"i-[0-9a-f]{8,17}", target, re.IGNORECASE):
        try:
            resp = ec2_client.describe_instances(InstanceIds=[target])
            if _flatten_instances(resp):
                log.debug("Matched %r as a direct instance ID.", target)
                return target
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise  # unexpected error — surface it
            log.debug(
                "Target %r looks like an instance ID but was not found (%s); "
                "falling through to attribute search.",
                target, code,
            )

    # ── Strategy 2: attribute filters (tried in priority order) ──────────────
    # The final tag:Hostname entry uses a wildcard so a bare short name (e.g.
    # "PRODRXSGTWYA01") matches a fully-qualified Hostname tag value (e.g.
    # "PRODRXSGTWYA01.corp.data-rx.com").
    filters_to_try: list[tuple[str, str]] = [
        ("private-ip-address", target),
        ("ip-address",         target),   # public / elastic IP
        ("tag:Name",           target),
        ("tag:Hostname",       target),
        ("tag:Hostname",       f"*{target}*"),
    ]

    for filter_name, filter_value in filters_to_try:
        try:
            resp = ec2_client.describe_instances(
                Filters=[
                    {"Name": filter_name,           "Values": [filter_value]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            # Invalid filter value (e.g. a hostname passed as an IP) is expected —
            # skip to the next filter. Anything else is a real AWS problem.
            if "Invalid" in code:
                log.debug("Filter %r skipped for %r (%s).", filter_name, target, code)
                continue
            raise

        instances = _flatten_instances(resp)

        if len(instances) == 1:
            instance_id = instances[0]["InstanceId"]
            log.debug(
                "Matched %r via filter %r → %s.", target, filter_name, instance_id
            )
            return instance_id

        if len(instances) > 1:
            ids = [i["InstanceId"] for i in instances]
            raise ValueError(
                f"{target!r} is ambiguous: matched {len(ids)} running instances "
                f"via {filter_name!r} ({', '.join(ids)}). "
                f"Use an instance ID or a more specific tag value."
            )

        # len == 0 → this filter returned nothing; try the next one
        log.debug("Filter %r returned no matches for %r.", filter_name, target)

    raise ValueError(
        f"No running EC2 instance found for {target!r}. "
        f"Tried: instance ID, private IP, public IP, tag:Name, tag:Hostname "
        f"(exact and contains)."
    )


def _flatten_instances(describe_response: dict) -> list[dict]:
    """Flatten reservations → instances from a describe_instances response."""
    return [
        instance
        for reservation in describe_response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
