import asyncio
from datetime import timedelta
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.activities.email_activities import (
        send_invitation_email,
        send_follow_up_email,
        send_final_follow_up,
    )

@workflow.defn
class InvitationWorkflow:
    def __init__(self) -> None:
        self.has_responded = False

    @workflow.run
    async def run(self, email: str) -> str:
        workflow.logger.info(f"Starting Invitation Workflow for {email}")

        # 1. Send the initial invitation
        await workflow.execute_activity(
            send_invitation_email,
            email,
            start_to_close_timeout=timedelta(seconds=15),
        )

        # 2. Wait up to 3 days for a response
        try:
            await workflow.wait_condition(
                lambda: self.has_responded,
                timeout=timedelta(days=3), 
            )
            responded = True
        except asyncio.TimeoutError:
            responded = False

        if responded:
            workflow.logger.info(f"User {email} responded early. Exiting workflow.")
            return f"Success! User {email} accepted the invitation."

        # 3. If no response after waiting, send the first follow-up
        await workflow.execute_activity(
            send_follow_up_email,
            email,
            start_to_close_timeout=timedelta(seconds=15),
        )

        # 4. Wait another 3 days for a response
        try:
            await workflow.wait_condition(
                lambda: self.has_responded,
                timeout=timedelta(days=3),
            )
            responded = True
        except asyncio.TimeoutError:
            responded = False

        if responded:
            return f"Success! User {email} accepted the invitation after the first follow-up."

        # 5. Send the final follow-up
        await workflow.execute_activity(
            send_final_follow_up,
            email,
            start_to_close_timeout=timedelta(seconds=15),
        )
        
        return f"Process complete. User {email} did not respond."

    @workflow.signal
    def user_responded(self) -> None:
        """This signal is sent from your backend when the user accepts"""
        workflow.logger.info("Received user response signal!")
        self.has_responded = True
