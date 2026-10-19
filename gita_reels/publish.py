"""YouTube upload. Credentials come from YOUTUBE_TOKEN_JSON (CI) or youtube_token.json (local)."""
from __future__ import annotations

import json
import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILE = Path("youtube_token.json")
CLIENT_SECRET = Path("client_secret.json")


def authorize() -> str:
    """One-time local OAuth. Store the printed JSON as the YOUTUBE_TOKEN_JSON secret for automation."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES).run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    return creds.to_json()


def _youtube():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    raw = os.environ.get("YOUTUBE_TOKEN_JSON") or (TOKEN_FILE.read_text() if TOKEN_FILE.exists() else None)
    if not raw:
        raise RuntimeError("No YouTube credentials: run `gita-reels youtube-auth` once on your computer")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload(folder: Path, privacy: str, publish_at: str | None = None) -> str:
    from googleapiclient.http import MediaFileUpload
    meta = json.loads((folder / "project.json").read_text(encoding="utf-8"))
    if meta.get("status") != "ready":
        raise RuntimeError(f"{folder} is not ready to publish (status={meta.get('status')})")
    status = {"privacyStatus": "private" if publish_at else privacy, "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": True}
    if publish_at:
        status["publishAt"] = publish_at
    body = {"snippet": {"title": meta["youtube"]["title"][:100], "description": meta["youtube"]["description"],
                        "tags": meta["youtube"]["tags"], "categoryId": "27"}, "status": status}
    req = _youtube().videos().insert(part="snippet,status", body=body,
                                     media_body=MediaFileUpload(str(folder / "final.mp4"), resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    meta["status"] = "published"
    meta["youtube"]["id"] = resp["id"]
    (folder / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"https://youtube.com/shorts/{resp['id']}"
