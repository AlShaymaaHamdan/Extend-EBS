import sys

sys.path.append("utilities/Python")
from wait_for_status import wait_for_ssm_command

def send_ssm_command(ssm_client, instance_id, command, is_windows=False):
    """Send an SSM command and return the result using wait_for_status."""
    doc_name = "AWS-RunPowerShellScript" if is_windows else "AWS-RunShellScript"
    response = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName=doc_name,
        Parameters={"commands": [command]},
    )
    command_id = response["Command"]["CommandId"]

    # wait_for_ssm_command expects (command_id, region) and builds its own
    # SSM client internally from the region. It blocks until completion and
    # exits(1) internally on failure — it does NOT return the command output,
    # so we fetch that ourselves afterward via get_command_invocation().
    region = ssm_client.meta.region_name
    wait_for_ssm_command(command_id, region)

    # Reaching this line means wait_for_ssm_command didn't sys.exit(1),
    # i.e. the command succeeded. Fetch the actual output now.
    output = ssm_client.get_command_invocation(
        CommandId=command_id,
        InstanceId=instance_id,
    )

    if output["Status"] != "Success":
        raise RuntimeError(
            f"SSM command failed with status {output['Status']} and message: {output.get('StandardErrorContent')}"
        )

    return output
