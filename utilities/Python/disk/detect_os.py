import boto3
import sys

def detect_os(instance_id, region=None):
    """Detect OS type based on PlatformDetails"""
    ec2 = boto3.client("ec2", region_name=region) if region else boto3.client("ec2")

    # Fetch instance details
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]

    platform_details = inst.get("PlatformDetails", "").lower()
    if "windows" in platform_details:
        return "Windows"
    else:
        return "Linux"  # All others are Linux/Unix
