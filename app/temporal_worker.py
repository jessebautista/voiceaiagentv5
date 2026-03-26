import asyncio
import sys
import os
import warnings
from pathlib import Path

# Suppress known harmless warnings so we can focus on dev-fix flow and real errors.
# - Temporal sandbox: "Module X was imported after initial workflow load" (does not affect workflow or code fix).
warnings.filterwarnings(
    "ignore",
    message=".*was imported after initial workflow load.*",
    module="temporalio.worker.workflow_sandbox._importer",
)

from temporalio.client import Client
from temporalio.worker import Worker

# Ensure the root project directory is in the Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

# Load environment variables if running standalone
from dotenv import load_dotenv
load_dotenv()
from app.logging_config import configure_logging
configure_logging(os.getenv("DEV_AGENT_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO")

from app.workflows.invitation import InvitationWorkflow
from app.workflows.agreed import AgreedWorkflow
from app.workflows.dev_fix import DevFixWorkflow

from app.activities.email_activities import (
    send_invitation_email,
    send_follow_up_email,
    send_final_follow_up,
    send_reminder,
    start_adjudication,
    send_reminder_to_adjudicate,
    send_end_of_adjudication,
    send_thank_you_message,
)
from app.activities.dev_activities import (
    setup_repository,
    analyze_and_code,
    apply_trivial_test_change,
    verify_fix,
    create_pull_request,
    update_bug_ticket,
)

async def main():
    # In production, TEMPORAL_URL would point to your cloud instance.
    # We default to localhost for development.
    temporal_url = os.getenv("TEMPORAL_URL", "localhost:7233")
    
    print(f"Connecting to Temporal Server at {temporal_url}...")
    client = await Client.connect(temporal_url)

    worker = Worker(
        client,
        task_queue="voiceai-email-queue-v3",
        workflows=[InvitationWorkflow, AgreedWorkflow, DevFixWorkflow],
        activities=[
            send_invitation_email,
            send_follow_up_email,
            send_final_follow_up,
            send_reminder,
            start_adjudication,
            send_reminder_to_adjudicate,
            send_end_of_adjudication,
            send_thank_you_message,
            setup_repository,
            analyze_and_code,
            apply_trivial_test_change,
            verify_fix,
            create_pull_request,
            update_bug_ticket,
        ],
    )

    print(f"Worker started. Listening on task queue: 'voiceai-email-queue-v3'")
    print("Press Ctrl+C to exit.")
    await worker.run()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
