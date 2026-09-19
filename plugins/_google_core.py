"""
Shared Google sign-in for the Gmail and Calendar plugins (leading underscore:
not a plugin itself).

One-time setup (⚙ → PLUGIN SETTINGS → Google):
  1. In Google Cloud Console create an OAuth client of type "Desktop app"
     (APIs: Gmail API + Google Calendar API) and download its JSON.
  2. Paste the path to that client_secret JSON and press SIGN IN.
     A browser window opens for your consent; the token is stored in
     config/token_google.json (git-ignored) and refreshed automatically.
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TOKEN = BASE / "config" / "token_google.json"
NS = "google"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
]


def _secret_path() -> str:
    try:
        from memory.config_manager import get_plugin_config
        return str(get_plugin_config(NS).get("client_secret", "")).strip().strip('"')
    except Exception:
        return ""


def sign_in(values: dict) -> tuple[bool, str]:
    """Settings button: run the OAuth consent flow in the browser."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    path = str(values.get("client_secret") or _secret_path()).strip().strip('"')
    if not path or not Path(path).is_file():
        return False, "Point to your OAuth 'Desktop app' client_secret JSON file first."
    try:
        flow = InstalledAppFlow.from_client_secrets_file(path, SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True,
                                      success_message="Jarvis is connected to Google. You can close this tab.")
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    except Exception as e:
        return False, f"Google sign-in failed: {e}"
    return True, "Signed in to Google — Gmail and Calendar are ready."


def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN.exists():
        raise RuntimeError("Google isn't connected yet — open Plugin Settings → Google and press SIGN IN.")
    creds = Credentials.from_authorized_user_info(json.loads(TOKEN.read_text(encoding="utf-8")), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("Google sign-in expired — press SIGN IN again in Plugin Settings.")
    return creds


def service(api: str, version: str):
    from googleapiclient.discovery import build
    return build(api, version, credentials=credentials(), cache_discovery=False)


SETTINGS = {
    "namespace": NS,
    "title": "Google (Gmail + Calendar)",
    "fields": [
        {"key": "client_secret", "label": "OAuth client_secret JSON path (Desktop app)", "type": "text",
         "placeholder": r"C:\Users\you\Downloads\client_secret_xxx.json"},
    ],
    "action": {"label": "SIGN IN WITH GOOGLE", "run": sign_in},
}
