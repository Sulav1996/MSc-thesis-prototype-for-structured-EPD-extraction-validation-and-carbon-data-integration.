"""
Minimal Google Drive v3 client (REST over ``requests``) for the EPD store.

Why OAuth user credentials and not a service account?
    Files created by a service account count against the service account's own
    quota, which is 0 for new projects ("Service Accounts do not have storage
    quota"). A personal Google account (e.g. with Google One / Gemini storage)
    must therefore authorise the app once; the resulting refresh token lets the
    Render server upload into *your* Drive and use *your* quota.

Scope: ``https://www.googleapis.com/auth/drive.file`` (non-sensitive). The app
can only see files it created itself, never the rest of your Drive.

Get a refresh token once with ``python tools/google_drive_auth.py`` and set
GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET and GDRIVE_REFRESH_TOKEN on Render.
If the OAuth consent screen stays in "Testing", Google expires refresh tokens
after 7 days; publish the app ("In production") to keep the token valid.
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid as uuid_lib

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
SIMPLE_UPLOAD_LIMIT = 5 * 1024 * 1024
CHUNK = 8 * 1024 * 1024  # multiple of 256 KiB, as required by resumable uploads
RETRY_STATUS = {429, 500, 502, 503, 504}
FILE_FIELDS = "id,name,size,md5Checksum,modifiedTime,appProperties,mimeType"


class DriveError(RuntimeError):
    pass


class DriveClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 root_folder_name: str = "EPD_Prototype_Store", timeout: int = 120):
        if not (client_id and client_secret and refresh_token):
            raise DriveError("Google Drive credentials are incomplete (client id, secret and refresh token).")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.root_folder_name = root_folder_name
        self.timeout = timeout
        self._token = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._folder_cache: dict[tuple, str] = {}

    # ------------------------------------------------------------------ auth
    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            response = self._session.post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }, timeout=self.timeout)
            if response.status_code != 200:
                detail = response.text[:300]
                if "invalid_grant" in detail:
                    detail += (" — the refresh token was revoked or expired. If the OAuth consent screen is in "
                               "'Testing', tokens expire after 7 days; publish the app and create a new token "
                               "with tools/google_drive_auth.py.")
                raise DriveError(f"Google token refresh failed ({response.status_code}): {detail}")
            payload = response.json()
            self._token = payload["access_token"]
            self._token_expiry = time.time() + int(payload.get("expires_in", 3600))
            return self._token

    def _request(self, method: str, url: str, *, expected=(200,), stream=False, **kwargs) -> requests.Response:
        for attempt in range(6):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self._access_token()}"
            response = self._session.request(method, url, headers=headers, timeout=self.timeout,
                                             stream=stream, **kwargs)
            if response.status_code in expected:
                return response
            if response.status_code == 401 and attempt == 0:
                self._token = None  # force refresh once
                kwargs["headers"] = headers
                continue
            if response.status_code in RETRY_STATUS or (
                    response.status_code == 403 and "rateLimitExceeded" in response.text):
                time.sleep(min(32, 2 ** attempt) + random.random())
                kwargs["headers"] = headers
                continue
            raise DriveError(f"Drive API {method} {url.split('?')[0]} failed "
                             f"({response.status_code}): {response.text[:400]}")
        raise DriveError(f"Drive API {method} {url.split('?')[0]} failed after retries.")

    # ------------------------------------------------------------------ folders
    def _find_folder(self, name: str, parent: str | None) -> str | None:
        query = [f"name = '{_q(name)}'", f"mimeType = '{FOLDER_MIME}'", "trashed = false"]
        if parent:
            query.append(f"'{parent}' in parents")
        response = self._request("GET", f"{API}/files", params={
            "q": " and ".join(query), "fields": "files(id,name)", "spaces": "drive", "pageSize": 10})
        files = response.json().get("files", [])
        return files[0]["id"] if files else None

    def folder_id(self, *path: str) -> str:
        """Return (creating if needed) the id of root/path…"""
        key = (self.root_folder_name,) + path
        if key in self._folder_cache:
            return self._folder_cache[key]
        parent = None
        for depth, name in enumerate(key):
            cache_key = key[:depth + 1]
            if cache_key in self._folder_cache:
                parent = self._folder_cache[cache_key]
                continue
            folder = self._find_folder(name, parent)
            if folder is None:
                body = {"name": name, "mimeType": FOLDER_MIME}
                if parent:
                    body["parents"] = [parent]
                folder = self._request("POST", f"{API}/files", params={"fields": "id"},
                                       json=body).json()["id"]
            self._folder_cache[cache_key] = folder
            parent = folder
        return parent

    # ------------------------------------------------------------------ files
    def find_by_key(self, key: str) -> dict | None:
        query = (f"appProperties has {{ key='epd_key' and value='{_q(key)}' }} and trashed = false")
        response = self._request("GET", f"{API}/files", params={
            "q": query, "fields": f"files({FILE_FIELDS})", "spaces": "drive", "pageSize": 5})
        files = response.json().get("files", [])
        return files[0] if files else None

    def list_by_kind(self, kind: str, modified_after: str | None = None) -> list[dict]:
        query = f"appProperties has {{ key='epd_kind' and value='{_q(kind)}' }} and trashed = false"
        if modified_after:
            query += f" and modifiedTime > '{modified_after}'"
        files, token = [], None
        while True:
            params = {"q": query, "fields": f"nextPageToken,files({FILE_FIELDS})", "spaces": "drive",
                      "pageSize": 1000}
            if token:
                params["pageToken"] = token
            payload = self._request("GET", f"{API}/files", params=params).json()
            files.extend(payload.get("files", []))
            token = payload.get("nextPageToken")
            if not token:
                return files

    def upload(self, key: str, data: bytes, mime_type: str, folder: tuple[str, ...],
               app_properties: dict | None = None, overwrite: bool = False) -> dict:
        existing = self.find_by_key(key)
        if existing and not overwrite:
            return existing
        properties = {"epd_key": key, **(app_properties or {})}
        properties = {k: str(v)[:100] for k, v in properties.items() if v is not None}
        name = key.replace("/", "__")
        if existing:
            metadata = {"appProperties": properties}
            return self._upload_bytes("PATCH", f"{UPLOAD_API}/files/{existing['id']}", metadata, data, mime_type)
        metadata = {"name": name, "parents": [self.folder_id(*folder)], "appProperties": properties}
        return self._upload_bytes("POST", f"{UPLOAD_API}/files", metadata, data, mime_type)

    def _upload_bytes(self, method: str, url: str, metadata: dict, data: bytes, mime_type: str) -> dict:
        if len(data) <= SIMPLE_UPLOAD_LIMIT:
            boundary = f"epd{uuid_lib.uuid4().hex}"
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                    f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n").encode()
            body += data + f"\r\n--{boundary}--\r\n".encode()
            return self._request(method, url, params={"uploadType": "multipart", "fields": FILE_FIELDS},
                                 data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"}
                                 ).json()
        # Resumable upload for large files (PDF archives, database snapshots).
        start = self._request(method, url, params={"uploadType": "resumable", "fields": FILE_FIELDS},
                              json=metadata, headers={"X-Upload-Content-Type": mime_type,
                                                      "X-Upload-Content-Length": str(len(data))})
        session_url = start.headers["Location"]
        offset = 0
        total = len(data)
        while offset < total:
            chunk = data[offset:offset + CHUNK]
            end = offset + len(chunk) - 1
            response = self._session.put(session_url, data=chunk, timeout=self.timeout, headers={
                "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}"})
            if response.status_code in (200, 201):
                return response.json()
            if response.status_code == 308:
                received = response.headers.get("Range")
                offset = int(received.split("-")[1]) + 1 if received else offset
                continue
            if response.status_code in RETRY_STATUS:
                time.sleep(2)
                status = self._session.put(session_url, timeout=self.timeout,
                                           headers={"Content-Range": f"bytes */{total}"})
                received = status.headers.get("Range")
                offset = int(received.split("-")[1]) + 1 if received else 0
                continue
            raise DriveError(f"Resumable upload failed ({response.status_code}): {response.text[:300]}")
        raise DriveError("Resumable upload ended without a final response.")

    def download(self, file_id: str) -> bytes:
        return self._request("GET", f"{API}/files/{file_id}", params={"alt": "media"}).content

    def delete(self, file_id: str) -> None:
        """Move a file to the Drive trash (recoverable for 30 days)."""
        self._request("PATCH", f"{API}/files/{file_id}", json={"trashed": True})

    def about(self) -> dict:
        return self._request("GET", f"{API}/about", params={"fields": "user(emailAddress),storageQuota"}).json()


def _q(value: str) -> str:
    """Escape a value for a Drive query string."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")
