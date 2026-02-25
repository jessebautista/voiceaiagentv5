import asyncio
from langchain_core.tools import tool
from app.temporal_client import get_temporal_client, init_temporal_client
from app.workflows.invitation import InvitationWorkflow
from app.workflows.agreed import AgreedWorkflow

@tool
async def send_invitation(email: str) -> str:
    """
    Send an invitation to a user via email and start the monitoring workflow.
    Use this when a user needs to be invited or onboarded.
    """
    client = get_temporal_client()
    if not client:
        # If running in a context where the lifespan hasn't fired
        client = await init_temporal_client()

    workflow_id = f"invitation-workflow-{email}"
    try:
        handle = await client.start_workflow(
            InvitationWorkflow.run,
            email,
            id=workflow_id,
            task_queue="voiceai-email-queue",
        )
        return f"Successfully started invitation flow for {email}. Workflow ID: {handle.id}"
    except Exception as e:
        return f"Failed to start invitation flow: {e}"

@tool
async def start_agreement(email: str) -> str:
    """
    Start the agreement and adjudication reminder flow for a user via email.
    Use this when a user is ready to begin the formal agreement or adjudication process.
    """
    client = get_temporal_client()
    if not client:
        # If running in a context where the lifespan hasn't fired
        client = await init_temporal_client()

    workflow_id = f"agreed-workflow-{email}"
    try:
        handle = await client.start_workflow(
            AgreedWorkflow.run,
            email,
            id=workflow_id,
            task_queue="voiceai-email-queue",
        )
        return f"Successfully started agreement flow for {email}. Workflow ID: {handle.id}"
    except Exception as e:
        return f"Failed to start agreement flow: {e}"
