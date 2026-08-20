#!/usr/bin/env python3
"""
check_disk.py
-------------
Checks current disk usage for a drive/mount on an EC2 instance via SSM.

Exposes get_disk_usage() as the reusable entry point (used by
extend_AWS_vol.py); main() is kept as a standalone CLI for ad-hoc use.
"""
import boto3
import sys
import re

sys.path.append("utilities/Python")
from send_ssm_command import send_ssm_command


def normalize_drive_windows(drive):
    d = drive.strip() # Trim whitespaces
    if len(d) == 1:
        return d.upper() + ":" # add : after windows drives ex. C:
    if d.endswith("\\") or d.endswith("/"):
        d = d[:-1] # remove trailing slashes
    if not d.endswith(":"):
        d = d.upper() + ":"
    return d


def parse_windows_output(output):
    m = re.search( # output will be like this: Drive:C: TotalGB:100 UsedGB:60 FreeGB:40 UsedPercent:60%
        r"TotalGB:([0-9\.]+)\s+UsedGB:([0-9\.]+)\s+FreeGB:([0-9\.]+)\s+UsedPercent:([0-9]+)%",
        output
    )
    if not m:
        return None

    total, used, free, pct = m.groups() # Extract the four captured values
    return { # Return them as a dictionary
        "total": total,
        "used": used,
        "free": free,
        "pct": pct
    }


def parse_linux_output(output):
    """
    Parse 'df -h <mount>' output and extract:
    size, used, avail, percent, mount.

    Returns dict or None if not matched.
    """
    # Typical df -h line:
    # /dev/xvda1   10G   8G   2G   80%   /
    pattern = (
        r"(?P<filesystem>\S+)\s+"
        r"(?P<size>\S+)\s+"
        r"(?P<used>\S+)\s+"
        r"(?P<avail>\S+)\s+"
        r"(?P<pct>\d+)%\s+"
        r"(?P<mount>.+)"
    )

    m = re.search(pattern, output)
    if not m:
        return None

    return {
        "filesystem": m.group("filesystem"),
        "size": m.group("size"),
        "used": m.group("used"),
        "avail": m.group("avail"),
        "pct": m.group("pct"),
        "mount": m.group("mount").strip()
    }


def _human_size_to_gb(value):
    """
    Convert a df -h style size string (e.g. '10G', '512M', '1.5T', '900K')
    to a float number of GB. Bare numbers (no unit) are treated as bytes.
    """
    m = re.match(r"^([0-9.]+)\s*([KMGT]?)$", value.strip(), re.IGNORECASE)
    if not m:
        raise RuntimeError(f"Could not parse size value: {value!r}")

    num, unit = m.groups()
    factor = {
        "": 1 / 1024 ** 3,
        "K": 1 / 1024 ** 2,
        "M": 1 / 1024,
        "G": 1,
        "T": 1024,
    }[unit.upper()]
    return round(float(num) * factor, 2)


def get_disk_usage(ssm_client, instance_id, platform, drive):
    """
    Query disk usage for a drive/mount via SSM.

    Args:
        ssm_client: Boto3 SSM client.
        instance_id: EC2 instance ID.
        platform: "Windows" or "Linux" (as returned by detect_os()).
        drive: Drive letter (Windows) or mount path (Linux).

    Returns:
        dict with float fields: total_gb, used_gb, free_gb, used_percent.

    Raises:
        RuntimeError: SSM output could not be parsed.
    """
    is_windows = "win" in platform.lower()

    if is_windows:
        drive = normalize_drive_windows(drive)
        command = ( # Powershell Command that calculated data (total, free, used, percentage)
            f"$drv='{drive}'; "
            f"$ld=Get-CimInstance -ClassName Win32_LogicalDisk -Filter \"DeviceID='$drv'\"; "
            f"if(-not $ld){{ Write-Output \"DriveNotFound:$drv\"; exit 0 }}; "
            f"$sizeGB=[math]::Round($ld.Size/1GB,2); "
            f"$freeGB=[math]::Round($ld.FreeSpace/1GB,2); "
            f"$usedGB=[math]::Round($sizeGB - $freeGB,2); "
            f"$usedPct=[math]::Round((($sizeGB - $freeGB)/$sizeGB)*100,0); "
            f"Write-Output \"TotalGB:$sizeGB UsedGB:$usedGB FreeGB:$freeGB UsedPercent:$usedPct%\""
        )
        out = send_ssm_command(ssm_client, instance_id, command, is_windows=True)
        stdout = out.get("StandardOutputContent", "").strip()

        info = parse_windows_output(stdout)
        if not info:
            raise RuntimeError(f"Could not parse Windows disk output for {drive}: {stdout!r}")

        return {
            "total_gb": float(info["total"]),
            "used_gb": float(info["used"]),
            "free_gb": float(info["free"]),
            "used_percent": float(info["pct"]),
        }

    else:
        command = f"df -h {drive} | tail -1" # shell command
        out = send_ssm_command(ssm_client, instance_id, command, is_windows=False)
        stdout = out.get("StandardOutputContent", "").strip()

        info = parse_linux_output(stdout)
        if not info:
            raise RuntimeError(f"Could not parse Linux disk output for {drive}: {stdout!r}")

        return {
            "total_gb": _human_size_to_gb(info["size"]),
            "used_gb": _human_size_to_gb(info["used"]),
            "free_gb": _human_size_to_gb(info["avail"]),
            "used_percent": float(info["pct"]),
        }


def main():
    if len(sys.argv) != 5:
        print("Usage: python check_disk.py <instance-id> <os-type> <drive-or-mount> <region>", file=sys.stderr)
        sys.exit(1)

    instance_id = sys.argv[1].strip()
    os_type = sys.argv[2].strip().lower()
    drive = sys.argv[3].strip()
    region = sys.argv[4].strip()

    ssm = boto3.client("ssm", region_name=region)
    platform = "Windows" if "win" in os_type else "Linux"

    try:
        info = get_disk_usage(ssm, instance_id, platform, drive)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    print(
        f"Drive/Mount {drive} | Total: {info['total_gb']} GB | Used: {info['used_gb']} GB | "
        f"Free: {info['free_gb']} GB | UsedPercent: {info['used_percent']}%"
    )


if __name__ == "__main__":
    main()
