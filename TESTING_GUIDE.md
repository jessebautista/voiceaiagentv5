# Testing the Temporal Email Workflows

This guide explains how to start the required services and manually test the AI-driven, time-delayed email workflows on your local machine. Because this handles asynchronous multi-day workflows, you will be running 3 different terminal windows.

## Step 1: Start the Core Services
You need three terminal tabs open.

**Terminal 1: Start the Temporal Development Server (via Docker)**
```bash
docker run --rm -p 7233:7233 -p 8233:8233 temporalio/auto-setup:latest
```
*(This acts as the master queue that stores the state of workflows even if your python servers crash).*

**Terminal 2: Start the FastAPI App Server**
```bash
uvicorn app.main:app
```
*(This runs your backend API and hosts the Voice AI Agent).*

**Terminal 3: Start the Temporal Python Worker**
```bash
python app/temporal_worker.py
```
*(This is the background processor. It listens to the Temporal Server, executes the Python workflow code, and actually fires the emails).*

---

## Step 2: Test the "Invitation" Flow

1. Open your browser to the Chat UI: `http://127.0.0.1:8000/`
2. Speak or type to the AI:
   > *"Can you send an onboarding invitation to testuser@example.com?"*
3. The AI will immediately trigger the workflow. 
4. Check **Terminal 3** (the python worker). You will immediately see a console log saying:
   `📧 MOCK EMAIL SENT: You're Invited!`

**Simulate the user accepting the invite:**
The workflow is now sleeping for 3 days waiting for a response. To simulate the user clicking "I Accept" in their email, run this command in a 4th terminal:
```bash
python -c "import requests, json; print(requests.post('http://127.0.0.1:8000/api/invitations/accept', json={'email': 'testuser@example.com'}).json())"
```
If you check **Terminal 3** again, the worker will wake up, log `User testuser@example.com responded early. Exiting workflow`, and cancel all future follow-up emails!

---

## Step 3: Test the "Agreement/Adjudication" Flow

1. Go back to the Chat UI.
2. Speak or type to the AI:
   > *"The user testuser@example.com is ready to start the agreement process."*
3. The AI will confirm it started the flow.
4. Check **Terminal 3**. You will immediately see:
   `📧 MOCK EMAIL SENT: Reminder`
   
**Simulate the User submitting their adjudication form:**
The workflow is now waiting. To simulate your website notifying the backend that the user finished their paperwork, run this command:
```bash
python -c "import requests, json; print(requests.post('http://127.0.0.1:8000/api/agreements/adjudicate', json={'email': 'testuser@example.com'}).json())"
```
If you check **Terminal 3** again, you will see it log the next steps of the workflow and finalize the pipeline.

*(Note: Actual Resend email delivery is currently mocked via console logs in `app/activities/email_activities.py` to bypass your local network firewall. In production, simply uncomment the Resend SDK lines in that file to send real emails).*
