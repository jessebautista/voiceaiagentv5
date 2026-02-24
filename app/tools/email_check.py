# email_check.py — Tools for the agent to find and parse forwarded emails (PDF, body, links).
# When a caller says they will forward or have forwarded a PDF/email, the agent can search
# by subject, get email content, parse PDF attachments, and use the result to create a program.

import json
from typing import Any, Optional

from langchain_core.tools import tool

from app.tools.email_client import (
    fetch_attachment,
    get_email_content,
    is_configured,
    search_emails,
)


@tool
def search_emails_tool(
    subject_query: str = "",
    from_email: str = "",
    max_results: int = 10,
) -> str:
    """
    Search the configured inbox for emails by subject and/or sender. Use this when the user
    says they have forwarded an email or PDF and you need to find it (ask for the email subject
    if the user did not provide it). Returns a list of matching emails with id, subject, from, date.
    Use the id with get_email_content_tool to read the full email and attachments.
    """
    if not is_configured():
        return "Email (IMAP) is not configured. Set GMAIL_EMAIL and GMAIL_APP_PASSWORD in .env to enable checking forwarded emails."
    emails = search_emails(subject_query=subject_query, from_email=from_email, max_results=max_results)
    if not emails:
        return f"No emails found for subject like '{subject_query}'" + (f" from '{from_email}'" if from_email else "") + "."
    return json.dumps(emails, indent=2)


@tool
def get_email_content_tool(email_id: str) -> str:
    """
    Get the full content of an email by its id (from search_emails_tool). Returns plain-text body,
    HTML body, list of attachment filenames and types, and links found in the email (including
    Google Doc links). Use the attachment filename with parse_email_attachment_tool to extract
    text from a PDF.
    """
    if not is_configured():
        return "Email (IMAP) is not configured."
    out = get_email_content(email_id)
    if out.get("error"):
        return out["error"]
    # Return a concise summary for the LLM; omit raw HTML if long
    summary = {
        "subject": out["subject"],
        "from": out["from"],
        "body_plain": (out["body_plain"] or "")[:8000],
        "attachments": out["attachments"],
        "links": out["links"],
    }
    return json.dumps(summary, indent=2)


def _extract_text_from_pdf(data: bytes) -> str:
    """Extract text from PDF bytes. Requires pypdf."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
        return "\n".join(parts) if parts else ""
    except ImportError:
        return "[PDF parsing requires pypdf: pip install pypdf]"
    except Exception as e:
        return f"[PDF extraction failed: {e}]"


@tool
def parse_email_attachment_tool(email_id: str, attachment_filename: str) -> str:
    """
    Download an attachment from an email and extract text. Use the email id from search_emails_tool
    and the exact attachment filename from get_email_content_tool. Supports PDF; other types
    return a message that only PDF is supported for text extraction.
    """
    if not is_configured():
        return "Email (IMAP) is not configured."
    data = fetch_attachment(email_id, attachment_filename)
    if not data:
        return f"Could not find or download attachment '{attachment_filename}' from that email."
    fn_lower = attachment_filename.lower()
    if fn_lower.endswith(".pdf"):
        text = _extract_text_from_pdf(data)
        return text if text.strip() else "[PDF has no extractable text]"
    return "Only PDF attachments are supported for text extraction. Use the email body and links from get_email_content_tool for other details."


@tool
def get_google_doc_text_tool(url: str) -> str:
    """
    Try to get plain text from a Google Docs URL (e.g. from an email link). If the doc is shared
    with link and export is allowed, returns extracted text; otherwise returns the URL and a
    note for the user to confirm details. Use when the email contains a Google Doc link and you
    need its content to create a program.
    """
    if not url.strip().startswith("http"):
        return "Please provide a full Google Docs URL (e.g. https://docs.google.com/document/d/...)."
    # Google Docs export as text: requires auth for private docs. We try export?format=txt; if 403, return URL.
    try:
        import re
        import urllib.request
        from urllib import error as urllib_error
        doc_id = None
        if "docs.google.com/document/d/" in url:
            m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
            if m:
                doc_id = m.group(1)
        if not doc_id:
            return f"Could not parse Google Doc ID from: {url}. Use this link and ask the user for key details if needed."
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        req = urllib.request.Request(export_url, headers={"User-Agent": "Mozilla/5.0 (compatible; Agent/1.0)"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = getattr(resp, "status", 200)
            if status == 200:
                text = resp.read().decode("utf-8", errors="replace").strip()
                return text[:15000] if text else f"Doc export returned empty. Link: {url}"
            return f"Could not export doc (status {status}). Link: {url}. Ask the user to confirm program details."
    except urllib_error.HTTPError as e:
        return f"Could not fetch Google Doc (HTTP {e.code}). Link: {url}. Ask the user to confirm program details."
    except Exception as e:
        return f"Could not fetch Google Doc: {e}. Link: {url}. Use the link and ask the user for key program details if needed."
