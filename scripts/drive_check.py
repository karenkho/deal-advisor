"""Verify Drive publishing end to end: create a test subfolder under the configured root, upload a one-line file,
read it back, then delete both. Only ever deletes what it created. Credentials come from .env.

Usage: python scripts/drive_check.py
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROOT_NAME = "dealadvisor"
TEST_FOLDER = "deal-advisor-drive-check"
TEST_LINE = b"Deal Advisor Drive check.\n"
FOLDER_TYPE = "application/vnd.google-apps.folder"


def _gone(drive, file_id: str) -> bool:
    try:
        drive.files().get(fileId=file_id, fields="id").execute()
        return False
    except HttpError as e:
        return e.resp.status == 404


def main() -> None:
    load_dotenv(ROOT / ".env")
    root_id = yaml.safe_load((ROOT / "config" / "storage.yaml").read_text())["gdrive"]["root_folder_id"]
    creds = Credentials(None, refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"], client_id=os.environ["GDRIVE_CLIENT_ID"],
                        client_secret=os.environ["GDRIVE_CLIENT_SECRET"], token_uri="https://oauth2.googleapis.com/token")
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    root = drive.files().get(fileId=root_id, fields="name,mimeType,driveId", supportsAllDrives=True).execute()
    if root["name"] != EXPECTED_ROOT_NAME or root["mimeType"] != FOLDER_TYPE:
        raise SystemExit(f"Configured root is {root['name']!r}, expected the {EXPECTED_ROOT_NAME!r} folder. Nothing created.")
    print(f"PASS root folder: {root['name']} ({'Shared Drive' if root.get('driveId') else 'My Drive'})")

    folder_id = file_id = None
    try:
        folder_id = drive.files().create(body={"name": TEST_FOLDER, "mimeType": FOLDER_TYPE, "parents": [root_id]},
                                         fields="id").execute()["id"]
        print("PASS created test subfolder")
        media = MediaIoBaseUpload(io.BytesIO(TEST_LINE), mimetype="text/plain")
        file_id = drive.files().create(body={"name": "check.txt", "parents": [folder_id]}, media_body=media,
                                       fields="id").execute()["id"]
        print("PASS uploaded 1-line file")
        if drive.files().get_media(fileId=file_id).execute() != TEST_LINE:
            raise SystemExit("FAIL file read back differs")
        print("PASS read the file back")
    finally:
        for label, target in (("file", file_id), ("subfolder", folder_id)):
            if target:
                drive.files().delete(fileId=target).execute()
                print(f"{'PASS' if _gone(drive, target) else 'FAIL'} deleted test {label}")


if __name__ == "__main__":
    main()
