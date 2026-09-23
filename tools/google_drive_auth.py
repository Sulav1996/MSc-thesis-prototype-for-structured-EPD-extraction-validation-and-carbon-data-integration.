#!/usr/bin/env python3
"""
One-time helper: authorise the EPD prototype to use your Google Drive and print
the refresh token for Render (standard library + requests only).

1. Google Cloud console → create a project → enable "Google Drive API".
2. "Google Auth Platform" / OAuth consent screen: User type External, add yourself
   as a test user, then **Publish app** (In production). In "Testing" status Google
   expires refresh tokens after 7 days.
3. Credentials → Create credentials → OAuth client ID → Application type "Desktop app"
   → download the JSON file.
4. Run on your laptop (a browser window opens):

       python tools/google_drive_auth.py --client-secrets path/to/client_secret.json

5. Copy the three printed values into Render → your service → Environment:
   GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN
   (and optionally EPD_STORAGE_BACKEND=gdrive). Use --write-secrets to also store
   them in .streamlit/secrets.toml for local runs (that file is git-ignored).

Scope: https://www.googleapis.com/auth/drive.file — the app only sees files it created.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"
PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_client(path: str | None, client_id: str | None, client_secret: str | None) -> tuple[str, str]:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        section = data.get("installed") or data.get("web") or data
        return section["client_id"], section["client_secret"]
    if client_id and client_secret:
        return client_id, client_secret
    sys.exit("Provide --client-secrets FILE or --client-id and --client-secret.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-secrets", help="OAuth client JSON downloaded from Google Cloud (Desktop app)")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--port", type=int, default=0, help="Local port for the redirect (default: random)")
    parser.add_argument("--write-secrets", action="store_true", help="Also write .streamlit/secrets.toml")
    args = parser.parse_args(argv)
    client_id, client_secret = _load_client(args.client_secrets, args.client_id, args.client_secret)

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>EPD prototype: authorisation received. You can close this tab.</h3>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening the Google consent page. If no browser opens, visit:\n" + url + "\n")
    webbrowser.open(url)
    server.timeout = 600
    while "code" not in result and "error" not in result:
        server.handle_request()  # ignores extra requests such as /favicon.ico
    server.server_close()

    if result.get("state") != state or "code" not in result:
        print(f"Authorisation failed: {result}", file=sys.stderr)
        return 1

    response = requests.post(TOKEN_URL, data={
        "code": result["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
    }, timeout=60)
    if response.status_code != 200:
        print(f"Token exchange failed ({response.status_code}): {response.text}", file=sys.stderr)
        return 1
    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        print("Google returned no refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this script again.", file=sys.stderr)
        return 1

    print("Set these environment variables on Render (Environment tab):\n")
    print(f"GDRIVE_CLIENT_ID={client_id}")
    print(f"GDRIVE_CLIENT_SECRET={client_secret}")
    print(f"GDRIVE_REFRESH_TOKEN={refresh_token}")
    print("EPD_STORAGE_BACKEND=gdrive\n")

    if args.write_secrets:
        target = PROJECT_DIR / ".streamlit" / "secrets.toml"
        target.parent.mkdir(exist_ok=True)
        target.write_text(
            f'GDRIVE_CLIENT_ID = "{client_id}"\nGDRIVE_CLIENT_SECRET = "{client_secret}"\n'
            f'GDRIVE_REFRESH_TOKEN = "{refresh_token}"\nEPD_STORAGE_BACKEND = "gdrive"\n', encoding="utf-8")
        print(f"Also written to {target} (git-ignored).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
