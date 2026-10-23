"""One-time Gmail sign-in (run on Jeff's computer, not on the server).

    uv run python -m scripts.gmail_auth --client-file ~/Downloads/client_secret_XXXX.json

Opens the Google sign-in page for the sending account (the test Gmail address now, the
company's address later), asks for ONE permission (send email, nothing else), and prints the
lines to paste into `.env.gmail` on the server. The client file and the printed token are secrets:
never commit them or paste them in chat.
"""

import argparse
import json
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.tools.gmail import SCOPE, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/"


def load_client(path: Path) -> tuple[str, str]:
    data = json.loads(path.read_text())
    block = data.get("installed") or data.get("web")
    if not block:
        raise SystemExit("This does not look like a Google OAuth client file (no 'installed' section).")
    return block["client_id"], block["client_secret"]


def wait_for_code(state: str) -> str:
    found: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state", [""])[0] == state and "code" in query:
                found["code"] = query["code"][0]
                body = "Connected. You can close this tab and go back to the terminal."
            else:
                found["error"] = query.get("error", ["unexpected request"])[0]
                body = "Sign-in did not complete. Go back to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    while not found:
        server.handle_request()
    server.server_close()
    if "code" not in found:
        raise SystemExit(f"Google answered: {found['error']}")
    return found["code"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--client-file", type=Path, required=True, help="the JSON downloaded from Google Cloud"
    )
    args = parser.parse_args()
    client_id, client_secret = load_client(args.client_file)

    state = secrets.token_urlsafe(16)
    url = (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",  # always return a refresh token
                "state": state,
            }
        )
    )
    print("Opening the Google sign-in page. Sign in with the account Solecra will send from.")
    print(f"If no browser opens, copy this address into one:\n{url}\n")
    webbrowser.open(url)
    code = wait_for_code(state)

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if response.status_code != 200 or "refresh_token" not in response.json():
        print(f"Google did not return a refresh token (HTTP {response.status_code}): {response.text[:300]}")
        return 1
    print("\nConnected. Put these 4 lines in .env.gmail:\n")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={response.json()['refresh_token']}")
    print("GMAIL_SENDER=<the Gmail address you just signed in with>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
