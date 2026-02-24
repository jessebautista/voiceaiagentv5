# email_client.py — IMAP client for reading inbox (e.g. Gmail).
# Used by email_check tools to find and parse forwarded emails and attachments.
# Set GMAIL_EMAIL + GMAIL_APP_PASSWORD (or IMAP_HOST, IMAP_USER, IMAP_PASSWORD) in .env.

import imaplib
import os
import re
from email import policy
from email.parser import BytesParser
from typing import Any, Dict, List, Optional, Tuple

# Defaults for Gmail
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993


def _get_imap_config() -> Optional[Tuple[str, str, str, int]]:
    """Return (host, user, password, port) or None if not configured."""
    # Prefer Gmail app password
    user = os.getenv("GMAIL_EMAIL") or os.getenv("IMAP_USER")
    password = os.getenv("GMAIL_APP_PASSWORD") or os.getenv("IMAP_PASSWORD")
    host = os.getenv("IMAP_HOST") or DEFAULT_IMAP_HOST
    port = int(os.getenv("IMAP_PORT", str(DEFAULT_IMAP_PORT)))
    if user and password:
        return (host, user, password, port)
    return None


def _connect() -> Optional[imaplib.IMAP4_SSL]:
    """Open IMAP connection. Caller must call .logout() when done."""
    config = _get_imap_config()
    if not config:
        return None
    host, user, password, port = config
    try:
        conn = imaplib.IMAP4_SSL(host, port=port)
        conn.login(user, password)
        conn.select("INBOX")
        return conn
    except Exception:
        return None


def is_configured() -> bool:
    """Return True if email (IMAP) is configured."""
    return _get_imap_config() is not None


def search_emails(
    subject_query: str = "",
    from_email: str = "",
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """
    Search inbox by subject and/or sender. Returns list of dicts with id, subject, from, date.
    id is the IMAP UID (string) for use with get_email_content.
    """
    config = _get_imap_config()
    if not config:
        return []
    conn = _connect()
    if not conn:
        return []
    result = []
    try:
        criteria = []
        if subject_query:
            # Escape double quotes in query; use SUBJECT for Gmail/IMAP
            safe = subject_query.replace("\\", "\\\\").replace('"', '\\"')
            criteria.append(f'SUBJECT "{safe}"')
        if from_email:
            safe = from_email.replace("\\", "\\\\").replace('"', '\\"')
            criteria.append(f'FROM "{safe}"')
        search_crit = " ".join(criteria) if criteria else "ALL"
        typ, data = conn.search(None, search_crit)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        uids = uids[-max_results:] if len(uids) > max_results else uids  # most recent last
        uids = list(reversed(uids))  # newest first
        for uid in uids:
            typ, msg_data = conn.fetch(uid, "(RFC822.HEADER)")
            if typ != "OK" or not msg_data:
                continue
            raw = msg_data[0]
            if isinstance(raw, tuple):
                header_bytes = raw[1]
            else:
                header_bytes = raw
            msg = BytesParser(policy=policy.default).parsebytes(header_bytes)
            result.append({
                "id": uid.decode() if isinstance(uid, bytes) else str(uid),
                "subject": (msg.get("Subject") or "").strip(),
                "from": (msg.get("From") or "").strip(),
                "date": (msg.get("Date") or "").strip(),
            })
        return result
    except Exception:
        return []
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def get_email_content(email_id: str) -> Dict[str, Any]:
    """
    Fetch full email by IMAP UID. Returns body_plain, body_html, attachments, links.
    attachments: list of {filename, content_type, size}; content not included (use fetch_attachment).
    links: list of URLs found in the body (including Google Doc links).
    """
    out = {"body_plain": "", "body_html": "", "attachments": [], "links": [], "subject": "", "from": "", "error": None}
    conn = _connect()
    if not conn:
        out["error"] = "Email (IMAP) is not configured. Set GMAIL_EMAIL and GMAIL_APP_PASSWORD in .env."
        return out
    try:
        typ, data = conn.fetch(email_id.encode() if isinstance(email_id, str) and email_id.isdigit() else email_id, "(RFC822)")
        if typ != "OK" or not data:
            out["error"] = "Email not found or could not be fetched."
            return out
        raw = data[0]
        if isinstance(raw, tuple):
            msg_bytes = raw[1]
        else:
            msg_bytes = raw
        msg = BytesParser(policy=policy.default).parsebytes(msg_bytes)
        out["subject"] = (msg.get("Subject") or "").strip()
        out["from"] = (msg.get("From") or "").strip()

        body_plain_parts = []
        body_html_parts = []
        url_pattern = re.compile(
            r"https?://[^\s<>\"']+",
            re.IGNORECASE,
        )

        def walk(part):
            content_type = (part.get_content_type() or "").lower()
            disp = part.get("Content-Disposition") or ""
            if "attachment" in disp or (part.get_filename() and "attachment" in disp):
                fn = part.get_filename() or "attachment"
                out["attachments"].append({
                    "filename": fn,
                    "content_type": content_type,
                    "size": len(part.get_payload(decode=True) or b""),
                })
                return
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        body_plain_parts.append(payload.decode("utf-8", errors="replace"))
                    except Exception:
                        body_plain_parts.append(payload.decode("latin-1", errors="replace"))
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        html = payload.decode("utf-8", errors="replace")
                    except Exception:
                        html = payload.decode("latin-1", errors="replace")
                    body_html_parts.append(html)
                    for m in url_pattern.finditer(html):
                        out["links"].append(m.group(0))
            else:
                if part.is_multipart():
                    for sub in part.iter_parts():
                        walk(sub)
                elif part.get_filename():
                    out["attachments"].append({
                        "filename": part.get_filename(),
                        "content_type": content_type,
                        "size": len(part.get_payload(decode=True) or b""),
                    })

        if msg.is_multipart():
            for part in msg.iter_parts():
                walk(part)
        else:
            walk(msg)

        out["body_plain"] = "\n".join(body_plain_parts) if body_plain_parts else ""
        out["body_html"] = "\n".join(body_html_parts) if body_html_parts else ""
        text_for_links = out["body_plain"] + " " + out["body_html"]
        for m in url_pattern.finditer(text_for_links):
            out["links"].append(m.group(0))
        # Deduplicate and keep Google Doc links prominent
        seen = set()
        unique_links = []
        for u in out["links"]:
            u = u.rstrip(".,;:)")
            if u not in seen:
                seen.add(u)
                unique_links.append(u)
        out["links"] = unique_links
        return out
    except Exception as e:
        out["error"] = str(e)
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def fetch_attachment(email_id: str, filename: str) -> Optional[bytes]:
    """Fetch raw bytes of an attachment by email UID and filename."""
    conn = _connect()
    if not conn:
        return None
    try:
        typ, data = conn.fetch(email_id.encode() if isinstance(email_id, str) and email_id.isdigit() else email_id, "(RFC822)")
        if typ != "OK" or not data:
            return None
        raw = data[0]
        msg_bytes = raw[1] if isinstance(raw, tuple) else raw
        msg = BytesParser(policy=policy.default).parsebytes(msg_bytes)

        def find_attachment(part):
            fn = part.get_filename()
            if fn and fn.strip():
                if fn.strip() == filename.strip():
                    payload = part.get_payload(decode=True)
                    return payload
            if part.is_multipart():
                for sub in part.iter_parts():
                    r = find_attachment(sub)
                    if r is not None:
                        return r
            return None

        return find_attachment(msg)
    except Exception:
        return None
    finally:
        try:
            conn.logout()
        except Exception:
            pass
