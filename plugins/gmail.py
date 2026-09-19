"""Gmail: list / search / read messages, draft replies, and send (after CONFIRM on the HUD)."""
from __future__ import annotations

import base64
from email.mime.text import MIMEText

from plugins import _google_core as g

PLUGIN = {
    "name": "gmail",
    "description": (
        "The user's Gmail. action=list (recent inbox), search (Gmail query like 'from:bob is:unread'), "
        "read (message_id), draft (to, subject, body — optionally reply to message_id), "
        "send (same fields; the user must confirm on the HUD before it is sent)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | search | read | draft | send"},
            "query": {"type": "STRING", "description": "Gmail search query (for search)"},
            "message_id": {"type": "STRING", "description": "Message id (read, or reply target)"},
            "to": {"type": "STRING"}, "subject": {"type": "STRING"}, "body": {"type": "STRING"},
            "max_results": {"type": "INTEGER"},
        },
        "required": ["action"],
    },
}
PLUGIN_SETTINGS = g.SETTINGS


def _hdr(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _body(payload) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        t = _body(part)
        if t:
            return t
    return ""


def _list(svc, q, n):
    res = svc.users().messages().list(userId="me", q=q, maxResults=max(1, min(n, 20))).execute()
    rows = []
    for it in res.get("messages", []):
        m = svc.users().messages().get(userId="me", id=it["id"], format="metadata",
                                       metadataHeaders=["From", "Subject", "Date"]).execute()
        rows.append(f"[{it['id']}] {_hdr(m, 'Date')[:22]} | {_hdr(m, 'From')[:40]} | {_hdr(m, 'Subject')[:80]}"
                    f"\n    {m.get('snippet', '')[:160]}")
    return "\n".join(rows) or "No messages."


def _mime(p, svc):
    msg = MIMEText(str(p.get("body") or ""))
    msg["to"] = str(p.get("to") or "")
    msg["subject"] = str(p.get("subject") or "")
    thread = None
    if p.get("message_id"):
        orig = svc.users().messages().get(userId="me", id=p["message_id"], format="metadata",
                                          metadataHeaders=["Message-ID", "Subject", "From"]).execute()
        thread = orig.get("threadId")
        mid = _hdr(orig, "Message-ID")
        if mid:
            msg["In-Reply-To"] = mid
            msg["References"] = mid
        if not p.get("to"):
            msg.replace_header("to", _hdr(orig, "From"))
        if not p.get("subject"):
            msg.replace_header("subject", "Re: " + _hdr(orig, "Subject"))
    body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread:
        body["threadId"] = thread
    return body, msg["to"], msg["subject"]


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "list").lower()
    try:
        svc = g.service("gmail", "v1")
        n = int(parameters.get("max_results") or 8)
        if action == "list":
            return _list(svc, "in:inbox", n)
        if action == "search":
            return _list(svc, str(parameters.get("query") or ""), n)
        if action == "read":
            m = svc.users().messages().get(userId="me", id=str(parameters.get("message_id")), format="full").execute()
            return (f"From: {_hdr(m, 'From')}\nSubject: {_hdr(m, 'Subject')}\nDate: {_hdr(m, 'Date')}\n\n"
                    + (_body(m.get("payload", {})) or m.get("snippet", ""))[:6000])
        if action == "draft":
            body, to, subj = _mime(parameters, svc)
            d = svc.users().drafts().create(userId="me", body={"message": body}).execute()
            return f"Draft saved in Gmail (to {to}, '{subj}'). Id {d.get('id')}."
        if action == "send":
            body, to, subj = _mime(parameters, svc)
            from core import confirm
            return confirm.request(
                "gmail_send", f"Send email to {to}",
                f"Subject: {subj}\n\n{str(parameters.get('body') or '')[:400]}",
                lambda: f"Email sent (id {svc.users().messages().send(userId='me', body=body).execute().get('id')}).")
        return "Unknown Gmail action."
    except Exception as e:
        return f"Gmail: {e}"
