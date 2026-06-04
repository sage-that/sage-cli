"""
Cognito OAuth authentication for Sage CLI.

Uses Cognito's Hosted UI with localhost callback to authenticate via Google OAuth.
Supports browser-based login, token refresh, and persistent credentials.

Flow: sage login → opens browser → Google OAuth → localhost callback → tokens cached
"""

import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console

_COGNITO_DOMAIN = os.environ.get(
    "SAGE_COGNITO_DOMAIN", "dev.auth.sagethat.com"
)
_COGNITO_CLIENT_ID = os.environ.get(
    "SAGE_COGNITO_CLIENT_ID", "72f3d84edho6neu063n4ab6cb"
)
_COGNITO_REDIRECT_PORT = 3000

CREDENTIALS_FILE = Path.home() / ".sage" / "credentials"
CREDENTIALS_DIR = Path.home() / ".sage"

console = Console()


@dataclass
class TokenSet:
    id_token: str
    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        return time.time() + buffer_seconds >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "id_token": self.id_token,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TokenSet":
        return cls(
            id_token=data["id_token"],
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            expires_at=data["expires_at"],
            token_type=data.get("token_type", "Bearer"),
        )


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    result_code: Optional[str] = None
    result_error: Optional[str] = None
    done: threading.Event = threading.Event()

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _OAuthCallbackHandler.result_code = params["code"][0]
            self._respond("✓ Authenticated! You can close this window.", success=True)
        elif "error" in params:
            _OAuthCallbackHandler.result_error = params.get(
                "error_description", params["error"]
            )[0]
            self._respond(
                f"✗ Error: {_OAuthCallbackHandler.result_error}", success=False
            )
        else:
            self._respond("Waiting for redirect...", success=False)
            return
        _OAuthCallbackHandler.done.set()

    def _respond(self, message: str, success: bool):
        color = "#10b981" if success else "#ef4444"
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sage CLI</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#0f172a;color:#e2e8f0}}
.card{{text-align:center;padding:48px;border-radius:16px;background:#1e293b;max-width:400px}}
h1{{color:{color};margin:0 0 8px}}p{{color:#94a3b8;margin:0}}</style></head>
<body><div class="card"><h1>{"✓" if success else "✗"}</h1><p>{message}</p></div></body></html>"""
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())


def _find_free_port(start: int = 8765, end: int = 8780) -> int:
    import socket

    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free ports in range {start}-{end}")


def _generate_pkce() -> tuple[str, str]:
    import base64

    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge_b64 = base64.urlsafe_b64encode(code_challenge).decode().rstrip("=")
    return code_verifier, code_challenge_b64


def load_tokens() -> Optional[TokenSet]:
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
        return TokenSet.from_dict(data)
    except (json.JSONDecodeError, KeyError):
        return None


def save_tokens(tokens: TokenSet) -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(tokens.to_dict(), indent=2))
    CREDENTIALS_FILE.chmod(0o600)


def get_auth_header() -> Optional[str]:
    tokens = load_tokens()
    if not tokens:
        return None
    if tokens.is_expired():
        try:
            tokens = _refresh_tokens(tokens.refresh_token)
            save_tokens(tokens)
        except Exception:
            return None
    return f"Bearer {tokens.id_token}"


def get_auth_header_or_die() -> str:
    header = get_auth_header()
    if not header:
        console.print(
            "\n[bold red]Not logged in.[/bold red] Run [bold cyan]sage login[/bold cyan] to authenticate.\n"
        )
        sys.exit(1)
    return header


def login() -> None:
    client_id = _COGNITO_CLIENT_ID
    if not client_id:
        console.print(
            "[red]SAGE_COGNITO_CLIENT_ID environment variable not set.[/red]\nSet it to your Cognito User Pool Client ID."
        )
        sys.exit(1)

    _OAuthCallbackHandler.result_code = None
    _OAuthCallbackHandler.result_error = None
    _OAuthCallbackHandler.done = threading.Event()

    port = _COGNITO_REDIRECT_PORT
    redirect_uri = f"http://localhost:{port}/auth/callback"
    server = http.server.HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
    server.timeout = 1
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "identity_provider": "Google",
        "scope": "email openid profile",
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    auth_url = f"https://{_COGNITO_DOMAIN}/oauth2/authorize?{urllib.parse.urlencode(auth_params)}"

    console.print()
    console.print("[bold cyan]Opening browser for authentication...[/bold cyan]")
    console.print(f"  If the browser doesn't open, visit:\n  [dim]{auth_url}[/dim]")
    console.print()
    webbrowser.open(auth_url)

    if not _OAuthCallbackHandler.done.wait(timeout=300):
        server.shutdown()
        console.print("[red]Authentication timed out.[/red]")
        sys.exit(1)
    server.shutdown()

    if _OAuthCallbackHandler.result_error:
        console.print(
            f"[red]Authentication error: {_OAuthCallbackHandler.result_error}[/red]"
        )
        sys.exit(1)
    if not _OAuthCallbackHandler.result_code:
        console.print("[red]No authorization code received.[/red]")
        sys.exit(1)

    with console.status("[bold cyan]Exchanging code for tokens...[/bold cyan]"):
        try:
            tokens = _exchange_code_for_tokens(
                code=_OAuthCallbackHandler.result_code,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                client_id=client_id,
            )
        except Exception as e:
            console.print(f"[red]Token exchange failed: {e}[/red]")
            sys.exit(1)

    save_tokens(tokens)
    console.print(f"\n  [bold green]✓[/bold green] Logged in successfully!")
    console.print(f"  [dim]Tokens saved to {CREDENTIALS_FILE}[/dim]\n")


def logout() -> None:
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
        console.print("[dim]Logged out. Tokens removed.[/dim]")
    else:
        console.print("[dim]Not logged in.[/dim]")


def _exchange_code_for_tokens(
    code: str, redirect_uri: str, code_verifier: str, client_id: str
) -> TokenSet:
    with httpx.Client() as client:
        resp = client.post(
            f"https://{_COGNITO_DOMAIN}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    return TokenSet(
        id_token=data["id_token"],
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        expires_at=time.time() + data.get("expires_in", 3600),
        token_type=data.get("token_type", "Bearer"),
    )


def _refresh_tokens(refresh_token: str) -> TokenSet:
    client_id = _COGNITO_CLIENT_ID
    if not client_id:
        raise ValueError("SAGE_COGNITO_CLIENT_ID not set")
    with httpx.Client() as client:
        resp = client.post(
            f"https://{_COGNITO_DOMAIN}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    return TokenSet(
        id_token=data["id_token"],
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", refresh_token),
        expires_at=time.time() + data.get("expires_in", 3600),
        token_type=data.get("token_type", "Bearer"),
    )
