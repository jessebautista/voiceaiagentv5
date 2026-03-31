from temporalio import workflow
from datetime import timedelta
import logging

from app.activities.dev_activities import (
    setup_repository,
    analyze_and_code,
    apply_trivial_test_change,
    verify_fix,
    create_pull_request,
    update_bug_ticket,
    DevActionInput
)

VERIFY_FIX_TIMEOUT_MINUTES = 15

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
        bug_id = bug_data.get("id")
        logging.info(f"[DevFix] Starting workflow for Bug #{bug_id}")
        workflow_id = workflow.info().workflow_id

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
            logging.info("[DevFix] Step 1/5: Setup repository (clone + branch)")
            repo_path, branch_name = await workflow.execute_activity(
                setup_repository,
                {"input_data": input_data.dict(), "workflow_id": workflow_id},
                start_to_close_timeout=timedelta(minutes=5),
            )
            logging.info(f"[DevFix] Step 1/5 done. Branch: {branch_name}")

            test_mode = bug_data.get("test_mode") is True
            if test_mode:
                logging.info("[DevFix] Test mode: applying trivial change, then verify → PR")
                await workflow.execute_activity(
                    apply_trivial_test_change,
                    {"repo_path": repo_path, "bug_id": input_data.bug_id, "workflow_id": workflow_id},
                    start_to_close_timeout=timedelta(minutes=1),
                )
                success, err_out = await workflow.execute_activity(
                    verify_fix,
                    {
                        "repo_path": repo_path,
                        "bug_id": input_data.bug_id,
                        "bug_data": input_data.dict(),
                        "workflow_id": workflow_id,
                    },
                    start_to_close_timeout=timedelta(minutes=VERIFY_FIX_TIMEOUT_MINUTES),
                )
                if not success:
                    logging.warning("[DevFix] Test mode: verify failed (e.g. pre-existing errors). Proceeding to PR anyway.")
            else:
                # Normal: Code & Verify loop
                max_attempts = 3
                fix_successful = False
                last_error = None

                for attempt in range(max_attempts):
                    logging.info(f"[DevFix] Step 2/5: Analyze & code (attempt {attempt + 1}/{max_attempts})")
                    await workflow.execute_activity(
                        analyze_and_code,
                        {
                            "repo_path": repo_path,
                            "bug_data": input_data.dict(),
                            "last_error": last_error,
                            "workflow_id": workflow_id,
                        },
                        start_to_close_timeout=timedelta(minutes=30),
                        heartbeat_timeout=timedelta(minutes=5),
                    )
                    logging.info(f"[DevFix] Step 3/5: Verify fix (attempt {attempt + 1}/{max_attempts})")
                    success, error_output = await workflow.execute_activity(
                        verify_fix,
                        {
                            "repo_path": repo_path,
                            "bug_id": input_data.bug_id,
                            "bug_data": input_data.dict(),
                            "workflow_id": workflow_id,
                        },
                        start_to_close_timeout=timedelta(minutes=VERIFY_FIX_TIMEOUT_MINUTES),
                    )
                    if success:
                        fix_successful = True
                        logging.info("[DevFix] Step 3/5 done. Verify passed.")
                        break
                    logging.warning(f"[DevFix] Verify failed attempt {attempt + 1}: {error_output[:300]}")
                    last_error = error_output

                if not fix_successful:
                    raise Exception(f"Failed to verify fix after {max_attempts} attempts. Last error: {last_error}")

            from temporalio.common import RetryPolicy

            logging.info("[DevFix] Step 4/5: Create pull request (commit, push, open PR)")
            pr_url = await workflow.execute_activity(
                create_pull_request,
                {
                    "repo_path": repo_path,
                    "branch_name": branch_name,
                    "bug_data": input_data.dict(),
                    "workflow_id": workflow_id,
                },
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(non_retryable_error_types=["ValueError"])
            )
            logging.info(f"[DevFix] Step 4/5 done. PR: {pr_url}")

            logging.info("[DevFix] Step 5/5: Update bug ticket (comment + status)")
            await workflow.execute_activity(
                update_bug_ticket,
                {"bug_id": input_data.bug_id, "pr_url": pr_url, "workflow_id": workflow_id},
                start_to_close_timeout=timedelta(minutes=1),
            )
            logging.info(f"[DevFix] Step 5/5 done. Workflow complete for Bug #{input_data.bug_id}.")
            return {"status": "success", "pr_url": pr_url}

        except Exception as e:
            logging.error(f"[DevFix] Workflow failed for Bug #{input_data.bug_id}: {e}")
            
            # Post error to the bug ticket if we fail drastically
            await workflow.execute_activity(
                update_bug_ticket,
                {"bug_id": input_data.bug_id, "error_msg": str(e), "workflow_id": workflow_id},
                start_to_close_timeout=timedelta(minutes=1),
            )
            return {"status": "failed", "error": str(e)}
