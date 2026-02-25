import asyncio
from datetime import timedelta
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.activities.email_activities import (
        send_reminder,
        start_adjudication,
        send_reminder_to_adjudicate,
        send_end_of_adjudication,
        send_thank_you_message,
    )

@workflow.defn
class AgreedWorkflow:
    def __init__(self) -> None:
        self.is_adjudicated = False
        self.is_done = False

    @workflow.run
    async def run(self, email: str) -> str:
        # Flow: reminder -> waiting -> start adjudication -> waiting for adjudication (with reminders) -> end -> thank you
        
        # 1. Send reminder
        await workflow.execute_activity(
            send_reminder, email, start_to_close_timeout=timedelta(seconds=15)
        )

        # 2. Wait (placeholder duration, e.g. 1 day)
        await asyncio.sleep(timedelta(days=1).total_seconds())

        # 3. Start Adjudication
        await workflow.execute_activity(
            start_adjudication, email, start_to_close_timeout=timedelta(seconds=15)
        )

        # 4. Wait for it to be adjudicated, with reminders in between
        try:
            await workflow.wait_condition(
                lambda: self.is_adjudicated,
                timeout=timedelta(days=3)
            )
            adjudicated = True
        except asyncio.TimeoutError:
            adjudicated = False

        if not adjudicated:
            # 5. Reminder to adjudicate
            await workflow.execute_activity(
                send_reminder_to_adjudicate, email, start_to_close_timeout=timedelta(seconds=15)
            )
            # Wait again (final wait before forcing end)
            try:
                await workflow.wait_condition(
                    lambda: self.is_adjudicated,
                    timeout=timedelta(days=2)
                )
            except asyncio.TimeoutError:
                pass

        # 6. Send end of adjudication
        await workflow.execute_activity(
            send_end_of_adjudication, email, start_to_close_timeout=timedelta(seconds=15)
        )

        # 7. Wait briefly (e.g. 1 hour) before thank you
        await asyncio.sleep(timedelta(hours=1).total_seconds())

        # 8. Thank you message
        await workflow.execute_activity(
            send_thank_you_message, email, start_to_close_timeout=timedelta(seconds=15)
        )

        self.is_done = True
        return f"Agreed workflow finished for {email}."

    @workflow.signal
    def adjudication_completed(self) -> None:
        """Signal triggered when the adjudication process is marked as complete"""
        workflow.logger.info("Adjudication marked complete via signal!")
        self.is_adjudicated = True
