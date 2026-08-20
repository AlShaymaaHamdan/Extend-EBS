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

    # Use your existing wait_for_status function
    output = wait_for_ssm_command(command_id, region)

    if output["Status"] != "Success":
        raise RuntimeError(
            f"SSM command failed with status {output['Status']} and message: {output.get('StandardErrorContent')}"
        )

    return output
