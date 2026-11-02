"""One-time Google Drive sign-in for the My Drive publishing folder (run again when the sign-in expires).

Usage: python scripts/drive_auth.py
Opens the browser for consent with the OAuth Desktop client kept outside the repository, at the path in GDRIVE_CLIENT_FILE, then saves
GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET and GDRIVE_REFRESH_TOKEN to .env. Never prints any of them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import set_key
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parents[1]
CLIENT_FILE = Path(os.environ.get("GDRIVE_CLIENT_FILE", "google_oauth_client.json"))
SCOPES = ["https://www.googleapis.com/auth/drive"]
SIGN_IN_TIMEOUT_SECONDS = 600


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent", open_browser=True,
                                  authorization_prompt_message="Waiting for sign-in in the browser...",
                                  success_message="Sign-in complete. You can close this tab.",
                                  timeout_seconds=SIGN_IN_TIMEOUT_SECONDS)
    if not creds.refresh_token:
        raise SystemExit("Google returned no refresh token; nothing was saved.")
    client = json.loads(CLIENT_FILE.read_text())["installed"]
    env = str(ROOT / ".env")
    set_key(env, "GDRIVE_CLIENT_ID", client["client_id"])
    set_key(env, "GDRIVE_CLIENT_SECRET", client["client_secret"])
    set_key(env, "GDRIVE_REFRESH_TOKEN", creds.refresh_token)
    print("Saved the Drive sign-in to .env.")


if __name__ == "__main__":
    main()
