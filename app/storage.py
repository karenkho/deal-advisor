"""Where generated documents go: Google Drive (My Drive, under the configured root) or a local folder. One-way: the app publishes and never
reads requirements back. Everything stays inside the root; only the private templates/ folder is read. The database stays authoritative, so a
failure here marks a publish as pending and never stops the workflow."""
from __future__ import annotations

import io
import os
import re
import shutil
import socket
import time
from pathlib import Path

from app import faults
from app.config import ROOT, env, load_dotenv_if_present, load_yaml
from app.logging_setup import log_event

TEMPLATES_FOLDER = "templates"
FOLDER_MIME = "application/vnd.google-apps.folder"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
RETRIES = 3
BACKOFF_SECONDS = 1.5
TIMEOUT_SECONDS = 30
AUTH_EXPIRED_MESSAGE = "Drive authorization expired — re-run sign-in."
UNAVAILABLE_MESSAGE = "Drive is not available right now."
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class AuthExpired(Exception):
    pass


class Unavailable(Exception):
    pass


def safe_name(name: str) -> str:
    return _UNSAFE.sub("-", name).strip(" .-")[:120] or "unnamed"


class LocalStore:
    backend = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def _folder(self, parts: list[str]) -> Path:
        return self.root.joinpath(*[safe_name(p) for p in parts])

    def put(self, parts: list[str], name: str, data: bytes, mime: str = DOCX_MIME) -> dict:
        folder = self._folder(parts)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_name(name)
        path.write_bytes(data)
        return {"file_id": None, "url": None, "local_path": str(path)}

    def list_files(self, parts: list[str]) -> list[dict]:
        folder = self._folder(parts)
        return [{"id": p.name, "name": p.name} for p in sorted(folder.iterdir()) if p.is_file()] if folder.exists() else []

    def delete(self, parts: list[str], file_id: str) -> None:
        (self._folder(parts) / safe_name(file_id)).unlink(missing_ok=True)

    def purge_folders(self, code: str) -> dict:
        """Permanently, for the opportunity with this code."""
        prefix = safe_name(code) + " - "
        out = {"folders": 0, "files": 0}
        for folder in (p for p in (self.root.iterdir() if self.root.exists() else []) if p.is_dir() and p.name.startswith(prefix) and p.name != TEMPLATES_FOLDER):
            out["files"] += sum(1 for p in folder.rglob("*") if p.is_file())
            out["folders"] += 1 + sum(1 for p in folder.rglob("*") if p.is_dir())
            shutil.rmtree(folder)
        return out

    def read_template(self) -> tuple[bytes, str] | None:
        folder = self.root / TEMPLATES_FOLDER
        files = sorted(folder.glob("*.docx")) if folder.exists() else []
        return (files[0].read_bytes(), files[0].name) if files else None


class DriveStore:
    backend = "gdrive"

    def __init__(self, root_id: str, service=None, sleep=time.sleep):
        self.root_id = root_id
        self._service = service
        self._sleep = sleep
        self._folders: dict[tuple, str] = {}

    @property
    def service(self):
        if self._service is None:
            self._service = _build_service()
        return self._service

    def _call(self, request_factory):
        """Timeouts and retries with backoff. An expired or revoked sign-in is told apart: retrying cannot help."""
        from google.auth.exceptions import RefreshError, TransportError
        from googleapiclient.errors import HttpError
        last = None
        for attempt in range(RETRIES):
            try:
                return request_factory().execute()
            except RefreshError as e:
                raise AuthExpired(AUTH_EXPIRED_MESSAGE) from e
            except HttpError as e:
                if e.resp.status == 401:
                    raise AuthExpired(AUTH_EXPIRED_MESSAGE) from e
                if e.resp.status not in (403, 429, 500, 502, 503, 504):
                    raise Unavailable(f"Drive refused the request ({e.resp.status}).") from e
                last = e
            except (TransportError, OSError, socket.timeout, TimeoutError) as e:
                last = e
            if attempt < RETRIES - 1:
                self._sleep(BACKOFF_SECONDS * (2 ** attempt))
        log_event("drive unavailable", node="storage", error=type(last).__name__)
        raise Unavailable(UNAVAILABLE_MESSAGE) from last

    def _children(self, parent: str, extra: str = "") -> list[dict]:
        query = f"'{parent}' in parents and trashed=false" + extra
        return self._call(lambda: self.service.files().list(q=query, fields="files(id,name,mimeType,modifiedTime)", pageSize=200))["files"]

    def _folder(self, parts: list[str], create: bool = True) -> str | None:
        """Walks down from the root, so nothing can be created outside it."""
        parent = self.root_id
        walked: tuple = ()
        for raw in parts:
            part = safe_name(raw)
            walked += (part,)
            if walked in self._folders:
                parent = self._folders[walked]
                continue
            escaped = part.replace("\\", "\\\\").replace("'", "\\'")
            found = self._children(parent, f" and name='{escaped}' and mimeType='{FOLDER_MIME}'")
            if found:
                parent = found[0]["id"]
            elif create:
                body = {"name": part, "mimeType": FOLDER_MIME, "parents": [parent]}
                parent = self._call(lambda b=body: self.service.files().create(body=b, fields="id"))["id"]
            else:
                return None
            self._folders[walked] = parent
        return parent

    def put(self, parts: list[str], name: str, data: bytes, mime: str = DOCX_MIME) -> dict:
        from googleapiclient.http import MediaIoBaseUpload
        folder = self._folder(parts)
        name = safe_name(name)
        media = lambda: MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)  # noqa: E731
        same = [f for f in self._children(folder) if f["name"] == name and f["mimeType"] != FOLDER_MIME]
        if same:     # one file per name: publishing again replaces its content and keeps its link
            made = self._call(lambda: self.service.files().update(fileId=same[0]["id"], media_body=media(), fields="id,webViewLink"))
        else:
            body = {"name": name, "parents": [folder]}
            made = self._call(lambda: self.service.files().create(body=body, media_body=media(), fields="id,webViewLink"))
        return {"file_id": made["id"], "url": made.get("webViewLink"), "local_path": None}

    def list_files(self, parts: list[str]) -> list[dict]:
        folder = self._folder(parts, create=False)
        if folder is None:
            return []
        return [{"id": f["id"], "name": f["name"]} for f in self._children(folder) if f["mimeType"] != FOLDER_MIME]

    def delete(self, parts: list[str], file_id: str) -> None:
        """Only a file listed in that folder under the root: an ID alone is never enough."""
        if file_id in {f["id"] for f in self.list_files(parts)}:
            self._call(lambda: self.service.files().delete(fileId=file_id))

    def _all_children(self, parent: str) -> list[dict]:
        """Trashed ones too: a folder or copy already in Trash must not outlive the delete."""
        found, token = [], None
        while True:
            page = self._call(lambda t=token: self.service.files().list(q=f"'{parent}' in parents", fields="nextPageToken,files(id,name,mimeType)", pageSize=200, pageToken=t))
            found += page["files"]
            token = page.get("nextPageToken")
            if not token:
                return found

    def _purge_tree(self, folder_id: str, out: dict) -> None:
        for child in self._all_children(folder_id):
            if child["mimeType"] == FOLDER_MIME:
                self._purge_tree(child["id"], out)
            else:
                self._call(lambda i=child["id"]: self.service.files().delete(fileId=i))
                out["files"] += 1
        self._call(lambda: self.service.files().delete(fileId=folder_id))
        out["folders"] += 1

    def purge_folders(self, code: str) -> dict:
        """Permanently, not to Trash, for the opportunity with this code. Only folders directly under the root, by the code that starts their name, because the client part may have been edited since. Never the templates."""
        prefix = safe_name(code) + " - "
        out = {"folders": 0, "files": 0}
        for folder in self._all_children(self.root_id):
            if folder["mimeType"] == FOLDER_MIME and folder["name"].startswith(prefix) and folder["name"] != TEMPLATES_FOLDER:
                self._purge_tree(folder["id"], out)
        self._folders.clear()
        return out

    def read_template(self) -> tuple[bytes, str] | None:
        folder = self._folder([TEMPLATES_FOLDER], create=False)
        if folder is None:
            return None
        files = [f for f in self._children(folder) if f["mimeType"] == DOCX_MIME or f["name"].lower().endswith(".docx")]
        if not files:
            return None
        chosen = sorted(files, key=lambda f: (not re.search(r"master services|msa", f["name"], re.I), f["name"]))[0]
        data = self._call(lambda: self.service.files().get_media(fileId=chosen["id"]))
        return data, chosen["name"]


def _build_service():
    import google_auth_httplib2
    import httplib2
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    load_dotenv_if_present()
    token, client, secret = env("GDRIVE_REFRESH_TOKEN"), env("GDRIVE_CLIENT_ID"), env("GDRIVE_CLIENT_SECRET")
    if not (token and client and secret):
        raise AuthExpired("Drive is not signed in.")
    creds = Credentials(None, refresh_token=token, client_id=client, client_secret=secret, token_uri="https://oauth2.googleapis.com/token")
    return build("drive", "v3", http=google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=TIMEOUT_SECONDS)), cache_discovery=False, static_discovery=True)


def config() -> dict:
    return load_yaml("storage.yaml")


def get_store():
    if faults.active("drive_down"):
        raise Unavailable(UNAVAILABLE_MESSAGE)
    cfg = config()
    backend = os.environ.get("STORAGE_BACKEND") or cfg["storage"]["backend"]
    if backend == "gdrive":
        root = cfg["gdrive"]["root_folder_id"]
        if not root:
            raise AuthExpired("Drive root folder is not configured.")
        return DriveStore(root)
    root = os.environ.get("STORAGE_LOCAL_ROOT") or cfg["storage"]["local"]["root"]
    return LocalStore(Path(root) if Path(root).is_absolute() else ROOT / root)
