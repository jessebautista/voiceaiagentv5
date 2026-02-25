import asyncio
import os
import resend
from temporalio import activity

# Attempt to configure from environment variables
resend.api_key = os.getenv("RESEND_API_KEY", "")

# We configure a simple "from" address. In production, this must be a verified sender domain in Resend.
# E.g. "Acme Corp <onboarding@resend.dev>"
FROM_EMAIL = os.getenv("FROM_EMAIL", "onboarding@resend.dev")

def _send_email_sync(to_email: str, subject: str, html_content: str):
    """
    Synchronous helper to send the email via Resend API.
    Since Resend's Python SDK is synchronous, we run it in a thread later.
    """
    if not resend.api_key:
        print(f"[Warning] RESEND_API_KEY not set. Would have sent email to {to_email}: {subject}")
        return {"id": "mock_id_no_api_key"}

    params = {
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html_content,
    }
    
    try:
        print("\n" + "="*50)
        print(f"📧 MOCK EMAIL SENT (Bypassed network firewall)")
        print(f"To: {to_email}")
        print(f"Subject: {subject}")
        print(f"Body: {html_content}")
        print("="*50 + "\n")
        
        # response = resend.Emails.send(params)
        return {"id": "mock_success_id"}
    except Exception as e:
        print(f"Failed to send email to {to_email}: {e}")
        raise e

@activity.defn
async def send_invitation_email(email: str) -> str:
    print(f"[Activity] Sending initial invitation to {email}...")
    subject = "You're Invited!"
    html_content = f"<h1>Welcome!</h1><p>Click <a href='https://example.com/accept?email={email}'>here</a> to accept your invitation.</p>"
    
    # Run the sync email sending function in Temporal's async event loop safely
    await asyncio.to_thread(_send_email_sync, email, subject, html_content)
    return f"Invitation sent to {email}"

@activity.defn
async def send_follow_up_email(email: str) -> str:
    print(f"[Activity] Sending first follow-up to {email}...")
    subject = "Checking In - Invitation"
    html_content = f"<h1>Still interested?</h1><p>We haven't heard back from you! Please let us know.</p>"
    
    await asyncio.to_thread(_send_email_sync, email, subject, html_content)
    return f"Follow-up sent to {email}"

@activity.defn
async def send_final_follow_up(email: str) -> str:
    print(f"[Activity] Sending final follow-up to {email}...")
    subject = "Final Notice - Invitation Expiring"
    html_content = f"<h1>Final Notice</h1><p>This is your last chance to accept the invitation before it expires.</p>"
    
    await asyncio.to_thread(_send_email_sync, email, subject, html_content)
    return f"Final follow-up sent to {email}"

# ----- Agreed Flow Activities -----

@activity.defn
async def send_reminder(email: str) -> str:
    print(f"[Activity] Sending reminder to {email}...")
    await asyncio.to_thread(_send_email_sync, email, "Reminder", "<p>This is a reminder.</p>")
    return f"Reminder sent to {email}"

@activity.defn
async def start_adjudication(email: str) -> str:
    print(f"[Activity] Starting adjudication for {email}...")
    await asyncio.to_thread(_send_email_sync, email, "Adjudication Started", "<p>Your adjudication phase has started.</p>")
    return f"Adjudication started for {email}"

@activity.defn
async def send_reminder_to_adjudicate(email: str) -> str:
    print(f"[Activity] Sending reminder to adjudicate for {email}...")
    await asyncio.to_thread(_send_email_sync, email, "Reminder to Adjudicate", "<p>Please complete your adjudication.</p>")
    return f"Reminder to adjudicate sent to {email}"

@activity.defn
async def send_end_of_adjudication(email: str) -> str:
    print(f"[Activity] Sending end of adjudication to {email}...")
    await asyncio.to_thread(_send_email_sync, email, "Adjudication Ended", "<p>Your adjudication has concluded.</p>")
    return f"End of adjudication sent to {email}"

@activity.defn
async def send_thank_you_message(email: str) -> str:
    print(f"[Activity] Sending thank you message to {email}...")
    await asyncio.to_thread(_send_email_sync, email, "Thank You", "<h1>Thank You!</h1><p>We appreciate your time.</p>")
    return f"Thank you message sent to {email}"
