#!/usr/bin/env python3

import sys
import boto3
sys.path.append("utilities/Python")
from send_ssm_command import send_ssm_command


def find_volume_for_drive(ec2_client, instance_id, platform, drive):
    """Return the EBS volume ID linked to a given drive or mount, or exit if it cannot be determined."""
    desc = ec2_client.describe_instances(InstanceIds=[instance_id])
    inst = desc["Reservations"][0]["Instances"][0]
    block_mappings = inst.get("BlockDeviceMappings", [])

    # Case 1: only one volume attached
    if len(block_mappings) == 1:
        return block_mappings[0]["Ebs"]["VolumeId"]

    # Case 2: multiple volumes, try matching by drive size
    ssm = boto3.client("ssm", region_name=ec2_client.meta.region_name)
    try:
        if "win" in platform.lower():
            cmd = f"(Get-Volume -DriveLetter {drive.strip(':')}).Size / 1GB"
            resp = send_ssm_command(ssm, instance_id, cmd, is_windows=True)
        else:
            cmd = f"df -BG {drive} | tail -1 | awk '{{print $2}}' | tr -d 'G'"
            resp = send_ssm_command(ssm, instance_id, cmd, is_windows=False)
        drive_size = float(resp["StandardOutputContent"].strip())
    except Exception as e:
        print(f"❌ Could not determine drive size: {e}", file=sys.stderr)
        sys.exit(1)

    # Compare with attached volumes
    volumes = []
    for m in block_mappings:
        vol_id = m["Ebs"]["VolumeId"]
        vol = ec2_client.describe_volumes(VolumeIds=[vol_id])["Volumes"][0]
        volumes.append({"VolumeId": vol_id, "SizeGB": vol["Size"]})

    # Find the closest volume by size
    match = min(volumes, key=lambda v: abs(v["SizeGB"] - drive_size))
    if abs(match["SizeGB"] - drive_size) <= 1:
        return match["VolumeId"]

    # Case 3: cannot determine -> exit
    print(
        f"❌ Could not confidently map drive '{drive}' (size {drive_size} GB) to any EBS volume. Please specify manually.",
        file=sys.stderr,
    )
    sys.exit(1)

# # -----------------------------
# # CLI entry point
# # -----------------------------
# if __name__ == "__main__":
#     if len(sys.argv) != 4:
#         print(
#             "Usage: python find_volume.py <instance-id> <platform> <drive-or-mount>",
#             file=sys.stderr,
#         )
#         sys.exit(1)

#     instance_id = sys.argv[1]
#     platform = sys.argv[2]
#     drive = sys.argv[3]

#     ec2 = boto3.client("ec2")
#     volume_id = find_volume_for_drive(ec2, instance_id, platform, drive)
#     print(f"✅ Volume ID for {drive} on instance {instance_id}: {volume_id}")
