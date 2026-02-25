import asyncio
import sys
import os
from pathlib import Path
from temporalio.client import Client
from temporalio.worker import Worker

# Ensure the root project directory is in the Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

# Load environment variables if running standalone
from dotenv import load_dotenv
load_dotenv()

# Import our workflows and activities
from app.workflows.invitation import InvitationWorkflow
from app.workflows.agreed import AgreedWorkflow
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

async def main():
    # In production, TEMPORAL_URL would point to your cloud instance.
    # We default to localhost for development.
    temporal_url = os.getenv("TEMPORAL_URL", "localhost:7233")
    
    print(f"Connecting to Temporal Server at {temporal_url}...")
    client = await Client.connect(temporal_url)

    worker = Worker(
        client,
        task_queue="voiceai-email-queue",
        workflows=[InvitationWorkflow, AgreedWorkflow],
        activities=[
            send_invitation_email,
            send_follow_up_email,
            send_final_follow_up,
            send_reminder,
            start_adjudication,
            send_reminder_to_adjudicate,
            send_end_of_adjudication,
            send_thank_you_message,
        ],
    )

    print("Worker started. Listening on task queue: 'voiceai-email-queue'")
    print("Press Ctrl+C to exit.")
    await worker.run()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
