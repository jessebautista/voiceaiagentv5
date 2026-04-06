from temporalio import workflow
from datetime import timedelta
import logging

from app.activities.dev_activities import (
    setup_repository,
    preflight_repository_check,
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

            logging.info("[DevFix] Step 1.5/5: Preflight baseline check")
            preflight = await workflow.execute_activity(
                preflight_repository_check,
                {
                    "repo_path": repo_path,
                    "bug_id": input_data.bug_id,
                    "workflow_id": workflow_id,
                },
                start_to_close_timeout=timedelta(minutes=4),
            )
            baseline_error_paths = preflight.get("baseline_error_paths", []) if isinstance(preflight, dict) else []
            baseline_ok = bool(preflight.get("baseline_ok", True)) if isinstance(preflight, dict) else True
            baseline_summary = str(preflight.get("baseline_summary", "") or "") if isinstance(preflight, dict) else ""
            preflight_status = str(preflight.get("preflight_status", "passed")) if isinstance(preflight, dict) else "passed"
            preflight_reason = str(preflight.get("preflight_reason", "") or "") if isinstance(preflight, dict) else ""
            if preflight_status == "skipped":
                logging.info(
                    "[DevFix] Preflight skipped (%s). Continuing. %s",
                    preflight_reason,
                    baseline_summary[:300],
                )
            elif not baseline_ok:
                logging.warning(
                    "[DevFix] Preflight baseline check found pre-existing check errors; continuing in baseline-aware mode. %s",
                    baseline_summary[:300],
                )

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
                        "baseline_error_paths": baseline_error_paths,
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
                            "baseline_error_paths": baseline_error_paths,
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

            logging.info("[DevFix] Step 4/5: Prepare handoff (commit, push, staging/PR)")
            handoff = await workflow.execute_activity(
                create_pull_request,
                {
                    "repo_path": repo_path,
                    "branch_name": branch_name,
                    "bug_data": input_data.dict(),
                    "workflow_id": workflow_id,
                },
                start_to_close_timeout=timedelta(minutes=8),
                retry_policy=RetryPolicy(non_retryable_error_types=["ValueError"])
            )
            # Backward-compatible handoff parsing:
            # - new activity returns dict {pr_url, staging_url, ...}
            # - older worker code may still return a raw PR URL string
            if isinstance(handoff, dict):
                pr_url = handoff.get("pr_url")
                staging_url = handoff.get("staging_url")
            elif isinstance(handoff, str) and handoff.strip().startswith("http"):
                pr_url = handoff.strip()
                staging_url = None
            else:
                pr_url = None
                staging_url = None
            logging.info("[DevFix] Step 4/5 done. staging=%s pr=%s", bool(staging_url), bool(pr_url))

            logging.info("[DevFix] Step 5/5: Update bug ticket (comment + status)")
            await workflow.execute_activity(
                update_bug_ticket,
                {
                    "bug_id": input_data.bug_id,
                    "pr_url": pr_url,
                    "staging_url": staging_url,
                    "workflow_id": workflow_id,
                },
                start_to_close_timeout=timedelta(minutes=1),
            )
            logging.info(f"[DevFix] Step 5/5 done. Workflow complete for Bug #{input_data.bug_id}.")
            return {"status": "success", "pr_url": pr_url, "staging_url": staging_url}

        except Exception as e:
            logging.error(f"[DevFix] Workflow failed for Bug #{input_data.bug_id}: {e}")
            
            # Post error to the bug ticket if we fail drastically
            await workflow.execute_activity(
                update_bug_ticket,
                {"bug_id": input_data.bug_id, "error_msg": str(e), "workflow_id": workflow_id},
                start_to_close_timeout=timedelta(minutes=1),
            )
            return {"status": "failed", "error": str(e)}
