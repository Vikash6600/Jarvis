"""Google Calendar: upcoming events and new events (created after CONFIRM on the HUD)."""
from __future__ import annotations

from datetime import datetime, timedelta

from plugins import _google_core as g

PLUGIN = {
    "name": "google_calendar",
    "description": (
        "The user's Google Calendar. action=list shows upcoming events for the next `days` days; "
        "action=create adds an event (title, start and end as ISO 8601 local time like "
        "2026-09-21T15:00, optional description/location) after the user confirms on the HUD."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | create"},
            "days": {"type": "INTEGER"},
            "title": {"type": "STRING"}, "start": {"type": "STRING"}, "end": {"type": "STRING"},
            "description": {"type": "STRING"}, "location": {"type": "STRING"},
        },
        "required": ["action"],
    },
}
PLUGIN_SETTINGS = g.SETTINGS


def _iso(s: str) -> str:
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "list").lower()
    try:
        svc = g.service("calendar", "v3")
        if action == "list":
            now = datetime.now().astimezone()
            days = max(1, min(int(parameters.get("days") or 7), 60))
            res = svc.events().list(calendarId="primary", timeMin=now.isoformat(),
                                    timeMax=(now + timedelta(days=days)).isoformat(),
                                    singleEvents=True, orderBy="startTime", maxResults=25).execute()
            rows = []
            for e in res.get("items", []):
                st = e["start"].get("dateTime", e["start"].get("date", ""))
                rows.append(f"{st[:16].replace('T', ' ')}  {e.get('summary', '(no title)')}"
                            + (f" @ {e['location']}" if e.get("location") else ""))
            return "\n".join(rows) or f"Nothing on your calendar in the next {days} days."
        if action == "create":
            title = str(parameters.get("title") or "Event")
            start = _iso(str(parameters["start"]))
            end = _iso(str(parameters.get("end") or "")) if parameters.get("end") else \
                (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
            ev = {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end},
                  "description": str(parameters.get("description") or ""),
                  "location": str(parameters.get("location") or "")}
            from core import confirm
            return confirm.request(
                "calendar_create", f"Add '{title}' to your calendar",
                f"{start[:16].replace('T', ' ')} → {end[11:16]}",
                lambda: f"Added to your calendar: {svc.events().insert(calendarId='primary', body=ev).execute().get('htmlLink', '')}")
        return "Unknown calendar action."
    except KeyError as e:
        return f"Calendar: missing {e}."
    except Exception as e:
        return f"Calendar: {e}"
