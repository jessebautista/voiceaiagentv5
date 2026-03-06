"""
Simple test script to submit a DevFixWorkflow to Temporal without importing app.main.
"""
import asyncio
from temporalio.client import Client
from app.workflows.dev_fix import DevFixWorkflow

async def main():
    client = await Client.connect("localhost:7233")
    
    # Build a simple dict that matches what BugFixRequest provides
    # The workflow will receive this via its dataclass
    from dataclasses import dataclass
    
    @dataclass
    class SimpleBugRequest:
        id: int
        title: str
        description: str
        category: str
        status: str
    
    request = SimpleBugRequest(
        id=1, 
        title="Add LLC column in the Payroll table", 
        description="The Payroll table is missing an LLC column to track the LLC entity for each payroll entry.", 
        category="Payroll", 
        status="new"
    )
    
    print("Starting workflow...")
    handle = await client.start_workflow(
        DevFixWorkflow.run,
        request,
        id=f"dev-fix-workflow-1-test-v27",
        task_queue="voiceai-email-queue-v3",
    )
    print(f"Workflow started: {handle.id}")
    print("Waiting for workflow to complete (this may take a few minutes)...")
    result = await handle.result()
    print(f"\nWorkflow finished successfully!")
    print(f"PR Link: {result}")
    
if __name__ == "__main__":
    asyncio.run(main())
