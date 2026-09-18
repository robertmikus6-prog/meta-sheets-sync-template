#!/usr/bin/env python3
"""One-time helper: mint a Microsoft Ads refresh token for the sync.

Usage:
    python3 get_refresh_token.py <MS_CLIENT_ID> [--port 8080]

The Azure app must have redirect URI http://localhost:<port> registered
(platform "Mobile and desktop applications") and public client flows allowed.

Opens the browser for consent, catches the redirect locally, exchanges the
code and writes the refresh token to ms_refresh_token.txt. Store it with:

    gh secret set MS_REFRESH_TOKEN -R <owner>/<repo> < ms_refresh_token.txt
    rm ms_refresh_token.txt
"""

import argparse
import http.server
import threading
import urllib.parse
import webbrowser

import requests

AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
SCOPE = "https://ads.microsoft.com/msads.manage offline_access"

code_holder = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code_holder["code"] = q.get("code", [None])[0]
        code_holder["error"] = q.get("error_description", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>Hotovo, vrat sa do terminalu.</h2>".encode())
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *a):
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("client_id")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    redirect = f"http://localhost:{args.port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": SCOPE,
        "prompt": "select_account",
    })
    print("Otvaram prehliadac, prihlas sa uctom s pristupom do Microsoft Advertising")
    print("(pre read-only pouzi usera s rolou Viewer)...")
    webbrowser.open(url)

    server = http.server.HTTPServer(("localhost", args.port), Handler)
    server.serve_forever()

    if not code_holder.get("code"):
        raise SystemExit(f"Consent zlyhal: {code_holder.get('error')}")

    r = requests.post(TOKEN_URL, data={
        "client_id": args.client_id,
        "grant_type": "authorization_code",
        "code": code_holder["code"],
        "redirect_uri": redirect,
        "scope": SCOPE,
    }, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"Token exchange zlyhal: {r.text[:400]}")

    with open("ms_refresh_token.txt", "w") as f:
        f.write(r.json()["refresh_token"])
    print("Refresh token zapisany do ms_refresh_token.txt")
    print("Uloz ho: gh secret set MS_REFRESH_TOKEN -R <owner>/<repo> < ms_refresh_token.txt")
    print("A potom subor zmaz: rm ms_refresh_token.txt")


if __name__ == "__main__":
    main()
