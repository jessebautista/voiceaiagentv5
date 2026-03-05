from temporalio import workflow
from datetime import timedelta
import logging

from app.activities.dev_activities import (
    setup_repository,
    analyze_and_code,
    verify_fix,
    create_pull_request,
    update_bug_ticket,
    DevActionInput
)

@workflow.defn
class DevFixWorkflow:
    @workflow.run
    async def run(self, bug_data: dict) -> dict:
        """
        Orchestrates the dev fix process:
        1. Setup Repository
        2. Analyze & Code
        3. Verify Fix (Retry cycle)
        4. Create Pull Request
        5. Update Bug Ticket
        """
        logging.info(f"Starting DevFixWorkflow for Bug #{bug_data.get('id')}")
        
        # Prepare input payload
        input_data = DevActionInput(
            bug_id=bug_data.get("id"),
            title=bug_data.get("title"),
            description=bug_data.get("description"),
            category=bug_data.get("category"),
            status=bug_data.get("status")
        )

        try:
            # 1. Setup repository (Clone, Checkout Branch)
            repo_path, branch_name = await workflow.execute_activity(
                setup_repository,
                input_data,
                start_to_close_timeout=timedelta(minutes=5),
            )
            
            # Start loop for Code & Verify
            max_attempts = 3
            fix_successful = False
            last_error = None
            
            for attempt in range(max_attempts):
                # 2. Analyze & Code (LLM writes code)
                await workflow.execute_activity(
                    analyze_and_code,
                    {"repo_path": repo_path, "bug_data": input_data, "last_error": last_error},
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=5),
                )
                
                # 3. Verify Fix (Test commands)
                success, error_output = await workflow.execute_activity(
                    verify_fix,
                    repo_path,
                    start_to_close_timeout=timedelta(minutes=5),
                )
                
                if success:
                    fix_successful = True
                    break
                else:
                    logging.warning(f"Verification failed on attempt {attempt+1}. Error: {error_output}")
                    last_error = error_output
            
            if not fix_successful:
                raise Exception(f"Failed to verify fix after {max_attempts} attempts. Last error: {last_error}")

            from temporalio.common import RetryPolicy
            
            # 4. Create Pull Request
            pr_url = await workflow.execute_activity(
                create_pull_request,
                {"repo_path": repo_path, "branch_name": branch_name, "bug_data": input_data},
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(non_retryable_error_types=["ValueError"])
            )
            
            # 5. Update Bug Ticket in Supabase
            await workflow.execute_activity(
                update_bug_ticket,
                {"bug_id": input_data.bug_id, "pr_url": pr_url},
                start_to_close_timeout=timedelta(minutes=1),
            )
            
            return {"status": "success", "pr_url": pr_url}
            
        except Exception as e:
            logging.error(f"DevFixWorkflow failed for Bug #{input_data.bug_id}: {e}")
            
            # Post error to the bug ticket if we fail drastically
            await workflow.execute_activity(
                update_bug_ticket,
                {"bug_id": input_data.bug_id, "error_msg": str(e)},
                start_to_close_timeout=timedelta(minutes=1),
            )
            return {"status": "failed", "error": str(e)}
