#!/usr/bin/env python3
"""
extend_OS_fs.py
---------------
Step two of the EBS-extend automation. extend_AWS_vol.py has already grown the
EBS volume in AWS; this script grows the partition + filesystem *inside* the
instance so the extra space becomes usable. It runs over SSM, so no SSH/WinRM
access is needed.

  Linux    findmnt -> detect layout -> growpart -> (pvresize + lvextend for LVM)
           -> resize2fs (ext2/3/4) | xfs_growfs (xfs) | btrfs resize (btrfs)
  Windows  Update-Disk -> Get-PartitionSupportedSize -> Resize-Partition

Inputs come from the plan.json produced by `extend_AWS_vol.py plan`.

Safety checks
-------------
  * Refuses to run unless the EBS volume is already at the planned size and its
    modification state is optimizing/completed.
  * Linux: verifies the NVMe serial of the disk matches the planned volume ID
    (so we never grow the wrong disk).
  * Verifies the filesystem/partition actually grew afterwards (non-LVM).
  * Idempotent: re-running when everything is already extended is a no-op.
  * `drive` is strictly validated before being embedded in any script.

Usage
-----
  python extend_OS_fs.py --plan plan.json [--result-file extend_fs_result.json]
                         [--timeout 600] [--skip-size-verify]
"""
import argparse
import json
import os
import re
import shlex
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

DEFAULT_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 5
MIN_GROWTH_RATIO = 0.95   # post-resize FS/partition must be >= 95% of new volume size

_TERMINAL_OK = {"Success"}
_TERMINAL_BAD = {"Failed", "Cancelled", "TimedOut", "Cancelling", "DeliveryTimedOut",
                 "ExecutionTimedOut", "Undeliverable", "Terminated"}


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def normalize_drive(platform, drive):
    """Validate + normalise the drive/mount so it is safe to embed in a script."""
    drive = drive.strip()
    if platform == "windows":
        m = re.fullmatch(r"([A-Za-z]):?\\?", drive)
        if not m:
            raise ValueError(f"Invalid Windows drive {drive!r}; expected a letter like 'D' or 'D:'.")
        return m.group(1).upper()
    if not re.fullmatch(r"/[A-Za-z0-9_.@:+/-]*", drive) or ".." in drive.split("/"):
        raise ValueError(f"Invalid Linux mount path {drive!r}.")
    return drive


def detect_platform(plan):
    return "windows" if "windows" in str(plan.get("platform", "")).lower() else "linux"


# ---------------------------------------------------------------------------
# Scripts executed on the instance
# ---------------------------------------------------------------------------

LINUX_SCRIPT = r"""
set -uo pipefail
MOUNT=__MOUNT__
VOL_ID=__VOLID__
VOL_SERIAL="${VOL_ID//-/}"

fail() { echo "ERROR: $*" >&2; exit 1; }

command -v findmnt >/dev/null || fail "findmnt not found"
command -v lsblk   >/dev/null || fail "lsblk not found"

TARGET=$(findmnt -n -o TARGET --target "$MOUNT") || fail "Cannot find a mount for $MOUNT"
SRC=$(findmnt -n -o SOURCE --target "$MOUNT")
FSTYPE=$(findmnt -n -o FSTYPE --target "$MOUNT")
SRC="${SRC%%\[*}"                                   # strip btrfs [/subvol]
if [ ! -b "$SRC" ]; then SRC=$(df --output=source "$MOUNT" | tail -n1); fi
[ -b "$SRC" ] || fail "Cannot resolve a block device for $MOUNT (got '$SRC')"
SRC=$(readlink -f "$SRC")
echo "Mount: $TARGET  Device: $SRC  FS: $FSTYPE"

size_kb() { df -Pk "$MOUNT" | awk 'NR==2{print $2}'; }
BEFORE_KB=$(size_kb)

# 0 = serial matches (or unknown), 1 = serial belongs to a different volume
serial_ok() {
  local s; s=$(lsblk -dno SERIAL "$1" 2>/dev/null | head -n1 | tr -d '[:space:]')
  if [ -z "$s" ] || [[ "$s" != vol* ]]; then return 0; fi
  [ "$s" = "$VOL_SERIAL" ]
}

# Grow the partition (if any) that holds block device $1 to fill its disk.
# Returns 2 if the underlying disk is not the target EBS volume.
grow_block() {
  local dev="$1" type pk disk partnum out rc
  type=$(lsblk -no TYPE "$dev" | head -n1)
  case "$type" in
    part)
      pk=$(lsblk -no PKNAME "$dev" | head -n1)
      disk="/dev/$pk"
      serial_ok "$disk" || { echo "Skipping $dev: disk $disk is not $VOL_ID"; return 2; }
      partnum=$(cat "/sys/class/block/$(basename "$dev")/partition")
      command -v growpart >/dev/null || fail "growpart not installed (cloud-guest-utils / cloud-utils-growpart)"
      echo "growpart $disk $partnum"
      out=$(growpart "$disk" "$partnum" 2>&1); rc=$?
      echo "$out"
      if [ $rc -ne 0 ] && ! echo "$out" | grep -q "^NOCHANGE"; then fail "growpart failed (rc=$rc)"; fi
      ;;
    disk)
      serial_ok "$dev" || { echo "Skipping $dev: not $VOL_ID"; return 2; }
      echo "$dev has no partition table; nothing to grow at partition level"
      ;;
    *) fail "Unsupported block device type '$type' for $dev" ;;
  esac
  return 0
}

grow_fs() {   # $1 = device, uses $FSTYPE / $MOUNT
  case "$FSTYPE" in
    ext2|ext3|ext4) echo "resize2fs $1";  resize2fs "$1" || fail "resize2fs failed" ;;
    xfs)            echo "xfs_growfs $MOUNT"; xfs_growfs "$MOUNT" || fail "xfs_growfs failed" ;;
    btrfs)          echo "btrfs resize max"; btrfs filesystem resize max "$MOUNT" || fail "btrfs resize failed" ;;
    *) fail "Unsupported filesystem '$FSTYPE'" ;;
  esac
}

TYPE=$(lsblk -no TYPE "$SRC" | head -n1)
LAYOUT="partition"
case "$TYPE" in
  part|disk)
    grow_block "$SRC"; rc=$?
    [ $rc -eq 0 ] || fail "Device backing $MOUNT is not volume $VOL_ID"
    [ "$TYPE" = disk ] && LAYOUT="disk"
    grow_fs "$SRC"
    ;;
  lvm)
    LAYOUT="lvm"
    command -v pvresize >/dev/null || fail "LVM tools not installed"
    VG=$(lvs --noheadings -o vg_name "$SRC" | xargs)
    [ -n "$VG" ] || fail "Cannot determine volume group for $SRC"
    GREW=0
    for PV in $(pvs --noheadings -o pv_name --select "vg_name=$VG" | xargs -n1); do
      PV=$(readlink -f "$PV")
      grow_block "$PV"; rc=$?
      if [ $rc -eq 0 ]; then echo "pvresize $PV"; pvresize "$PV" || fail "pvresize failed"; GREW=1; fi
    done
    [ $GREW -eq 1 ] || fail "No physical volume in VG $VG lives on $VOL_ID"
    echo "lvextend -r -l +100%FREE $SRC"
    OUT=$(lvextend -r -l +100%FREE "$SRC" 2>&1); rc=$?
    echo "$OUT"
    if [ $rc -ne 0 ] && ! echo "$OUT" | grep -qi "matches existing size"; then fail "lvextend failed (rc=$rc)"; fi
    ;;
  *) fail "Unsupported layout '$TYPE' (crypt/raid/etc. need manual handling)" ;;
esac

AFTER_KB=$(size_kb)
df -h "$MOUNT"
echo "RESULT_LAYOUT=$LAYOUT"
echo "RESULT_BEFORE_BYTES=$((BEFORE_KB * 1024))"
echo "RESULT_AFTER_BYTES=$((AFTER_KB * 1024))"
"""

WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$letter  = '__LETTER__'
$volHint = '__VOLHINT__'

$part = Get-Partition -DriveLetter $letter
$disk = Get-Disk -Number $part.DiskNumber
Write-Output ("Drive {0}: disk {1}, serial '{2}', partition {3}" -f $letter, $disk.Number, $disk.SerialNumber, $part.PartitionNumber)
if ($disk.SerialNumber -and ($disk.SerialNumber -notlike "*$volHint*")) {
    Write-Output "WARNING: disk serial does not contain $volHint (serial format varies by driver; verify manually if unexpected)."
}
$before = $part.Size

Update-HostStorageCache
Update-Disk -Number $disk.Number
$disk = Get-Disk -Number $disk.Number
$part = Get-Partition -DriveLetter $letter
$sup  = Get-PartitionSupportedSize -DriveLetter $letter

if ($sup.SizeMax -gt $part.Size) {
    Write-Output ("Resize-Partition {0}: {1} -> {2} bytes" -f $letter, $part.Size, $sup.SizeMax)
    Resize-Partition -DriveLetter $letter -Size $sup.SizeMax
} else {
    $end = $part.Offset + $part.Size
    if ($end -ge ($disk.Size - 64MB)) {
        Write-Output "Partition already uses all available space; nothing to do."
    } else {
        throw "No contiguous unallocated space after partition $($part.PartitionNumber) (another partition may sit behind it). Manual intervention required."
    }
}

$after = (Get-Partition -DriveLetter $letter).Size
Write-Output "RESULT_LAYOUT=partition"
Write-Output "RESULT_BEFORE_BYTES=$before"
Write-Output "RESULT_AFTER_BYTES=$after"
"""


def build_linux_script(mount, volume_id):
    return (LINUX_SCRIPT
            .replace("__MOUNT__", shlex.quote(mount))
            .replace("__VOLID__", shlex.quote(volume_id)))


def build_windows_script(letter, volume_id):
    return (WINDOWS_SCRIPT
            .replace("__LETTER__", letter)
            .replace("__VOLHINT__", volume_id.replace("-", "")))


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------

def verify_volume_ready(ec2, volume_id, expected_size_gb):
    """Make sure step one really finished before touching the OS."""
    vol = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    if vol["Size"] != expected_size_gb:
        raise ValueError(
            f"Volume {volume_id} is {vol['Size']} GB but the plan expects {expected_size_gb} GB. "
            f"Has the AWS-side resize completed?"
        )
    mods = ec2.describe_volumes_modifications(VolumeIds=[volume_id]).get("VolumesModifications", [])
    if mods and mods[0]["ModificationState"] not in ("optimizing", "completed"):
        raise ValueError(
            f"Volume {volume_id} modification state is {mods[0]['ModificationState']!r}; "
            f"must be optimizing/completed before extending the filesystem."
        )


def run_ssm_script(ssm, instance_id, platform, script, timeout):
    """Send a script via SSM Run Command, wait for it, return (status, stdout, stderr)."""
    document = "AWS-RunPowerShellScript" if platform == "windows" else "AWS-RunShellScript"
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=document,
        Comment="extend-ebs-volume: extend OS filesystem",
        Parameters={"commands": script.strip("\n").splitlines(),
                    "executionTimeout": [str(timeout)]},
        TimeoutSeconds=max(30, min(timeout, 600)),
    )
    command_id = resp["Command"]["CommandId"]
    print(f"[INFO] SSM command {command_id} sent to {instance_id} ({document})", flush=True)

    deadline = time.time() + timeout + 60
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvocationDoesNotExist":
                continue   # not registered yet
            raise
        status = inv["Status"]
        if status in _TERMINAL_OK or status in _TERMINAL_BAD:
            return status, inv.get("StandardOutputContent", ""), inv.get("StandardErrorContent", "")
    raise TimeoutError(f"SSM command {command_id} did not finish within {timeout}s.")


def parse_results(stdout):
    return dict(re.findall(r"^RESULT_(\w+)=(\S+)\s*$", stdout, flags=re.MULTILINE))


# ---------------------------------------------------------------------------
# Main stage
# ---------------------------------------------------------------------------

def run_extend(plan_file, timeout, verify_size=True):
    with open(plan_file) as f:
        plan = json.load(f)

    if plan.get("action") != "resize":
        raise ValueError(f"Plan action is {plan.get('action')!r}, not 'resize' — nothing to extend.")

    region = plan["region"]
    instance_id = plan["instance_id"]
    volume_id = plan["volume_id"]
    new_size_gb = plan["proposed_new_size_gb"]
    platform = detect_platform(plan)
    drive = normalize_drive(platform, plan["drive"])

    ec2 = boto3.client("ec2", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    verify_volume_ready(ec2, volume_id, new_size_gb)

    script = build_windows_script(drive, volume_id) if platform == "windows" \
        else build_linux_script(drive, volume_id)

    status, stdout, stderr = run_ssm_script(ssm, instance_id, platform, script, timeout)
    print(stdout)
    if stderr.strip():
        print(stderr, file=sys.stderr)
    if status != "Success":
        raise RuntimeError(f"SSM command ended with status {status}. {stderr.strip()[-500:]}")

    res = parse_results(stdout)
    if "BEFORE_BYTES" not in res or "AFTER_BYTES" not in res:
        raise RuntimeError("Could not parse before/after sizes from the instance output.")
    before, after = int(res["BEFORE_BYTES"]), int(res["AFTER_BYTES"])
    layout = res.get("LAYOUT", "unknown")

    if verify_size and layout != "lvm":
        needed = new_size_gb * (1024 ** 3) * MIN_GROWTH_RATIO
        if after < needed:
            raise RuntimeError(
                f"Filesystem/partition is {after / 1024**3:.1f} GiB after the resize, expected at "
                f"least {needed / 1024**3:.1f} GiB for a {new_size_gb} GB volume. It did not grow."
            )

    return {
        "instance_id": instance_id,
        "volume_id": volume_id,
        "platform": platform,
        "drive": drive,
        "layout": layout,
        "fs_size_before_gb": round(before / 1024**3, 2),
        "fs_size_after_gb": round(after / 1024**3, 2),
        "grew": after > before,
        "success": True,
        "error": None,
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def format_summary(r):
    lines = [
        "# EBS Volume Extend — OS Filesystem",
        "",
        f"- **Instance ID:** {r['instance_id']}",
        f"- **Volume ID:** {r['volume_id']}",
        f"- **Platform:** {r['platform']}",
        f"- **Drive/mount:** {r['drive']}",
        f"- **Layout:** {r['layout']}",
        f"- **Size before:** {r['fs_size_before_gb']} GB",
        f"- **Size after:** {r['fs_size_after_gb']} GB",
        f"- **Grew this run:** {r['grew']}",
        f"- **Success:** {r['success']}",
    ]
    if not r["grew"]:
        lines.append("\n_Nothing changed this run — the filesystem was already at full size._")
    return "\n".join(lines)


def _append_step_summary(markdown):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(markdown + "\n")


def _fail(result_file, message):
    print(f"Error: {message}", file=sys.stderr)
    with open(result_file, "w") as f:
        json.dump({"success": False, "error": message}, f, indent=2)
    _append_step_summary(f"# EBS Volume Extend — OS Filesystem FAILED\n\n**Error:** {message}")
    sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="Extend the partition/filesystem after an EBS resize.")
    p.add_argument("--plan", required=True, help="Path to plan.json from extend_AWS_vol.py plan.")
    p.add_argument("--result-file", default="extend_fs_result.json")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                   help=f"Seconds to wait for the on-instance script (default {DEFAULT_TIMEOUT_SECONDS}).")
    p.add_argument("--skip-size-verify", action="store_true",
                   help="Don't fail if the partition ends up smaller than the new volume size "
                        "(e.g. drive shares a disk with other partitions).")
    args = p.parse_args()

    try:
        result = run_extend(args.plan, args.timeout, verify_size=not args.skip_size_verify)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        _fail(args.result_file, str(exc))
    except ClientError as exc:
        _fail(args.result_file, f"AWS error: {exc}")

    with open(args.result_file, "w") as f:
        json.dump(result, f, indent=2)
    summary = format_summary(result)
    print(summary)
    _append_step_summary(summary)


if __name__ == "__main__":
    main()
