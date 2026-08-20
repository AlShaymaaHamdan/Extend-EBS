#!/usr/bin/env python3

import boto3
import sys
import time

def wait_for_ssm_command(command_id, region, timeout=10800):
    ssm = boto3.client("ssm", region_name=region)

    print(f"[INFO] Waiting for SSM command {command_id} to complete...", flush=True)

    start_time = time.time()
    statuses = {}  # Track per-instance statuses

    while True:
        resp = ssm.list_command_invocations(
            CommandId=command_id,
            Details=True
        )

        if not resp["CommandInvocations"]:
            print("[WARN] No command invocations found yet, retrying...", flush=True)
        else:
            for invocation in resp["CommandInvocations"]:
                iid = invocation["InstanceId"]
                status = invocation["Status"]
                statuses[iid] = status
                print(f"[INFO] Instance {iid} status: {status}", flush=True)

        # Exit loop if all instances are done
        if statuses and all(s not in ["InProgress", "Pending"] for s in statuses.values()):
            break

        # Timeout safeguard
        if time.time() - start_time > timeout:
            raise TimeoutError(f"Command {command_id} did not finish within {timeout} seconds")

        time.sleep(15)

    print(f"[INFO] Final statuses: {statuses}", flush=True)

    # Fail the script if any instance failed
    for iid, st in statuses.items():
        if st != "Success":
            print(f"[ERROR] Instance {iid} failed with status: {st}", flush=True)
            sys.exit(1)

    # Collect and print outputs
    outputs = []
    for invocation in resp["CommandInvocations"]:
        for plugin in invocation.get("CommandPlugins", []):
            if "Output" in plugin:
                outputs.append(f"[{invocation['InstanceId']}] {plugin['Output']}")
    if outputs:
        print("\n".join(outputs))
