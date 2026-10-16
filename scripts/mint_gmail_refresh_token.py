#!/usr/bin/env python3
"""Mints a Google OAuth 2.0 refresh token scoped to `gmail.send` using only the Python standard library.

Use an OAuth consent screen set to "Internal" (Google Workspace) or "In production" (published),
because Google expires refresh tokens after 7 days for apps left in "Testing" mode.

Usage:
  python3 scripts/mint_gmail_refresh_token.py --client-secrets client_secret.json
  python3 scripts/mint_gmail_refresh_token.py --client-id <ID> --client-secret <SECRET> --verify
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.message import EmailMessage
from pathlib import Path
from typing import Any

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def load_client_credentials(
    secrets_path: str | Path | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[str, str]:
  """Extracts `(client_id, client_secret)` from flags, env vars, or a downloaded Google OAuth JSON file."""
  cid = (client_id or os.environ.get("FINOPS_GMAIL_CLIENT_ID") or "").strip()
  csec = (client_secret or os.environ.get("FINOPS_GMAIL_CLIENT_SECRET") or "").strip()
  if secrets_path:
    raw = json.loads(Path(secrets_path).read_text(encoding="utf-8"))
    block = raw.get("installed") or raw.get("web") or raw
    if isinstance(block, dict):
      cid = cid or str(block.get("client_id") or "").strip()
      csec = csec or str(block.get("client_secret") or "").strip()
  if not cid or not csec:
    raise ValueError(
        "Provide --client-secrets <client_secret.json> or --client-id and --client-secret."
    )
  return cid, csec


def make_pkce_pair() -> tuple[str, str]:
  """Generates a PKCE `(code_verifier, code_challenge)` pair (S256)."""
  verifier = secrets.token_urlsafe(48)
  digest = hashlib.sha256(verifier.encode("ascii")).digest()
  challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
  return verifier, challenge


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    login_hint: str = "",
) -> str:
  """Builds the Google OAuth 2.0 consent URL requesting offline `gmail.send` access."""
  params = {
      "client_id": client_id,
      "redirect_uri": redirect_uri,
      "response_type": "code",
      "scope": GMAIL_SEND_SCOPE,
      "access_type": "offline",
      "prompt": "consent",
      "state": state,
      "code_challenge": code_challenge,
      "code_challenge_method": "S256",
  }
  if login_hint.strip():
    params["login_hint"] = login_hint.strip()
  return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _post_form_json(url: str, form: dict[str, str], timeout: float = 10.0) -> dict[str, Any]:
  req = urllib.request.Request(
      url,
      data=urllib.parse.urlencode(form).encode("utf-8"),
      headers={"Content-Type": "application/x-www-form-urlencoded"},
      method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      return json.loads(resp.read().decode("utf-8"))
  except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
    raise RuntimeError(f"HTTP {exc.code} from {url}: {body or exc.reason}") from exc


def exchange_code_for_tokens(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
  """Exchanges an authorization code + PKCE verifier for an access_token and refresh_token."""
  payload = _post_form_json(
      GOOGLE_TOKEN_URL,
      {
          "client_id": client_id,
          "client_secret": client_secret,
          "code": code,
          "code_verifier": code_verifier,
          "grant_type": "authorization_code",
          "redirect_uri": redirect_uri,
      },
  )
  if not str(payload.get("refresh_token") or "").strip():
    raise RuntimeError(
        "Google token endpoint did not return a refresh_token. Revoke previous grant at "
        "https://myaccount.google.com/permissions and re-run with prompt=consent."
    )
  return payload


def verify_refresh_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    send_test_to: str = "",
) -> dict[str, Any]:
  """Exchanges `refresh_token` for a fresh `access_token` and optionally sends a test email."""
  token_resp = _post_form_json(
      GOOGLE_TOKEN_URL,
      {
          "client_id": client_id,
          "client_secret": client_secret,
          "refresh_token": refresh_token,
          "grant_type": "refresh_token",
      },
  )
  access_token = str(token_resp.get("access_token") or "").strip()
  if not access_token:
    raise RuntimeError("Refresh token exchange returned no access_token.")
  result: dict[str, Any] = {
      "verified": True,
      "expires_in": int(token_resp.get("expires_in") or 3600),
      "scope": str(token_resp.get("scope") or GMAIL_SEND_SCOPE),
  }
  if send_test_to.strip():
    msg = EmailMessage()
    msg["To"] = send_test_to.strip()
    msg["Subject"] = "[FinOps Governance] Gmail OAuth refresh token verification"
    msg.set_content(
        "This test message confirms your FINOPS_GMAIL_REFRESH_TOKEN can send via gmail.send."
    )
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    req = urllib.request.Request(
        GMAIL_SEND_URL,
        data=json.dumps({"raw": raw}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
      sent = json.loads(resp.read().decode("utf-8"))
    result["test_message_id"] = sent.get("id")
  return result


def run_loopback_auth_flow(
    client_id: str,
    client_secret: str,
    *,
    port: int = 0,
    login_hint: str = "",
    open_browser: bool = True,
) -> dict[str, Any]:
  """Starts a 127.0.0.1 loopback server, opens the consent screen, and exchanges the returned code."""
  state = secrets.token_urlsafe(24)
  verifier, challenge = make_pkce_pair()
  captured: dict[str, str] = {}

  class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
      parsed = urllib.parse.urlsplit(self.path)
      params = urllib.parse.parse_qs(parsed.query)
      for key in ("code", "state", "error"):
        if params.get(key):
          captured[key] = params[key][0]
      ok = bool(captured.get("code")) and captured.get("state") == state and not captured.get("error")
      body = (
          "OAuth grant received. You may close this tab and return to the terminal."
          if ok
          else f"OAuth callback error: {captured.get('error') or 'state mismatch'}."
      )
      self.send_response(200 if ok else 400)
      self.send_header("Content-Type", "text/plain; charset=utf-8")
      self.end_headers()
      self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt: str, *args: Any) -> None:
      del fmt, args

  with http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler) as httpd:
    bound_port = httpd.server_address[1]
    redirect_uri = f"http://127.0.0.1:{bound_port}/"
    auth_url = build_authorization_url(
        client_id, redirect_uri, state, challenge, login_hint=login_hint
    )
    print("Open the following URL in your browser and sign in with the sender mailbox:\n")
    print(f"  {auth_url}\n")
    if open_browser:
      webbrowser.open(auth_url)
    httpd.handle_request()

  if captured.get("error"):
    raise RuntimeError(f"OAuth authorization failed: {captured['error']}")
  if captured.get("state") != state or not captured.get("code"):
    raise RuntimeError("OAuth callback did not return a valid state and authorization code.")

  return exchange_code_for_tokens(
      client_id=client_id,
      client_secret=client_secret,
      code=captured["code"],
      redirect_uri=redirect_uri,
      code_verifier=verifier,
  )


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--client-secrets", help="Path to Google OAuth client_secret.json (Desktop app).")
  parser.add_argument("--client-id", help="OAuth 2.0 Client ID.")
  parser.add_argument("--client-secret", help="OAuth 2.0 Client Secret.")
  parser.add_argument("--refresh-token", help="Existing refresh token to verify with --verify.")
  parser.add_argument("--login-hint", default="", help="Optional email address hint for the consent screen.")
  parser.add_argument("--port", type=int, default=0, help="Loopback port (0 picks an unused ephemeral port).")
  parser.add_argument("--no-browser", action="store_true", help="Print the consent URL without opening a browser.")
  parser.add_argument("--verify", action="store_true", help="Verify the refresh token against oauth2.googleapis.com.")
  parser.add_argument("--send-test-to", default="", help="Send a test email to this address after verifying.")
  args = parser.parse_args(argv)

  client_id, client_secret = load_client_credentials(
      args.client_secrets, args.client_id, args.client_secret
  )
  refresh_token = (args.refresh_token or os.environ.get("FINOPS_GMAIL_REFRESH_TOKEN") or "").strip()

  if not refresh_token:
    tokens = run_loopback_auth_flow(
        client_id,
        client_secret,
        port=args.port,
        login_hint=args.login_hint,
        open_browser=not args.no_browser,
    )
    refresh_token = str(tokens["refresh_token"]).strip()

  if args.verify or args.send_test_to:
    check = verify_refresh_token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        send_test_to=args.send_test_to,
    )
    print(f"Verified refresh token (expires_in={check['expires_in']}s, scope={check['scope']}).")
    if check.get("test_message_id"):
      print(f"Sent test email to {args.send_test_to} (Gmail message_id={check['test_message_id']}).")

  print("\nReminder: ensure your OAuth consent screen publishing status is 'Internal' or 'In production'")
  print("(tokens issued while in 'Testing' mode expire after 7 days).\n")
  print("Store the refresh token in Secret Manager and attach it to Cloud Run:")
  print("  printf '%s' '" + refresh_token + "' | gcloud secrets create finops-gmail-refresh-token --data-file=-")
  print(
      "  gcloud run services update finops-dashboard-mcp-app --region=${GOOGLE_CLOUD_REGION:-us-central1} \\\n"
      f"    --update-env-vars='FINOPS_GMAIL_CLIENT_ID={client_id},FINOPS_ALLOW_AUTOMATED_CLEARANCE=1' \\\n"
      "    --update-secrets='FINOPS_GMAIL_CLIENT_SECRET=finops-gmail-client-secret:latest,"
      "FINOPS_GMAIL_REFRESH_TOKEN=finops-gmail-refresh-token:latest'"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
