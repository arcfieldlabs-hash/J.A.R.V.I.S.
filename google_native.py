"""Optional native Google Workspace integration for JARVIS.

Requires:
  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

Setup:
  1. Create a Google Cloud project and OAuth Desktop client credentials.
  2. Download credentials.json into ~/.jarvis/google/credentials.json
  3. First run will open a browser for consent; token is saved locally.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
]

DEFAULT_DIR = Path.home() / ".jarvis" / "google"


def _creds_dir() -> Path:
    d = DEFAULT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def available() -> bool:
    try:
        import google.auth  # noqa: F401
        import googleapiclient  # noqa: F401
        return True
    except ImportError:
        return False


def get_credentials():
    """Load or refresh OAuth credentials. Raises RuntimeError with guidance on failure."""
    if not available():
        raise RuntimeError(
            "Native Google libraries not installed. "
            "Run: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = _creds_dir() / "credentials.json"
    token_path = _creds_dir() / "token.json"

    if not creds_path.exists():
        raise RuntimeError(
            f"Missing OAuth client file: {creds_path}\n"
            "1. Go to https://console.cloud.google.com/\n"
            "2. Create a project → Enable Gmail, Calendar, Drive APIs\n"
            "3. Credentials → Create OAuth client ID → Desktop app\n"
            "4. Download JSON and save as ~/.jarvis/google/credentials.json"
        )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def gmail_list_unread(limit: int = 5) -> str:
    from googleapiclient.discovery import build

    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)
    results = (
        service.users()
        .messages()
        .list(userId="me", q="is:unread", maxResults=max(1, min(limit, 20)))
        .execute()
    )
    messages = results.get("messages", [])
    if not messages:
        return "No unread messages."

    lines = []
    for m in messages:
        msg = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["From", "Subject", "Date"]).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        lines.append(
            f"- {headers.get('Subject', '(no subject)')}\n"
            f"  From: {headers.get('From', '?')}  |  {headers.get('Date', '')}"
        )
    return "\n".join(lines)


def calendar_today(limit: int = 10) -> str:
    from datetime import datetime, timedelta, timezone
    from googleapiclient.discovery import build

    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=1)
    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            maxResults=max(1, min(limit, 25)),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = events_result.get("items", [])
    if not events:
        return "No events scheduled for the next 24 hours."

    lines = []
    for ev in events:
        start = ev["start"].get("dateTime", ev["start"].get("date", "?"))
        lines.append(f"- {start}  {ev.get('summary', '(no title)')}")
    return "\n".join(lines)


def drive_search(query: str, limit: int = 8) -> str:
    from googleapiclient.discovery import build

    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)
    q = f"fullText contains '{query.replace(chr(39), '')}' and trashed = false"
    results = (
        service.files()
        .list(q=q, pageSize=max(1, min(limit, 20)), fields="files(id, name, mimeType, webViewLink, modifiedTime)")
        .execute()
    )
    files = results.get("files", [])
    if not files:
        return f"No Drive files matched '{query}'."

    lines = []
    for f in files:
        link = f.get("webViewLink", "")
        lines.append(f"- {f.get('name')}  ({f.get('mimeType', '')})\n  {link}")
    return "\n".join(lines)


def run_action(action: str, args: dict[str, Any]) -> str:
    """Dispatch a high-level Google action."""
    action = action.strip().lower()
    if action in {"gmail_unread", "unread", "mail"}:
        return gmail_list_unread(int(args.get("limit", 5)))
    if action in {"calendar_today", "calendar", "today"}:
        return calendar_today(int(args.get("limit", 10)))
    if action in {"drive_search", "drive", "search"}:
        q = str(args.get("query", "")).strip()
        if not q:
            return "Missing search query for Drive."
        return drive_search(q, int(args.get("limit", 8)))
    return (
        "Unknown native Google action. Supported: gmail_unread, calendar_today, drive_search. "
        "Or install gog CLI for full command coverage."
    )
