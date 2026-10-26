"""Obtiene, una sola vez, el refresh token de Google Drive para el respaldo de comprobantes.

    uv run python scripts/drive_authorize.py --client-id <ID> --client-secret <SECRETO>

También lee `GOOGLE_DRIVE_CLIENT_ID` y `GOOGLE_DRIVE_CLIENT_SECRET` (del `.env` o del
entorno). Abre el navegador para que el dueño de la cuenta de Google autorice la app
(flujo OAuth 2.0 para apps de escritorio, con redirección a `127.0.0.1` y PKCE;
https://developers.google.com/identity/protocols/oauth2/native-app), canjea el código y
crea en su Drive la carpeta raíz privada del respaldo. Imprime las dos variables que hay
que cargar en el servicio del API: `GOOGLE_DRIVE_REFRESH_TOKEN` y `GOOGLE_DRIVE_FOLDER_ID`.

Pide solo el alcance `drive.file`: la app únicamente ve los archivos y carpetas que ella
misma crea, nunca el resto del Drive. El refresh token es una credencial: va a las variables
del servicio, no a un archivo del repositorio ni a un chat.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from resthub.core.config import get_settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105  # nosec B105
FILES_URL = "https://www.googleapis.com/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"
WAIT_SECONDS = 300
DONE_PAGE = "<html><body><h3>Listo. Cierra esta pestaña y vuelve a la terminal.</h3></body></html>"


class _CallbackHandler(BaseHTTPRequestHandler):
    """Recibe la redirección de Google con el código (o el error)."""

    params: dict[str, list[str]] = {}  # noqa: RUF012 - estado de una sola ejecución del script
    received = threading.Event()

    def do_GET(self) -> None:
        type(self).params = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(DONE_PAGE.encode())
        type(self).received.set()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Sin registro: la URL de la redirección lleva el código."""


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _authorize(client_id: str) -> tuple[str, str, str]:
    """Abre el navegador y devuelve (código, verificador PKCE, redirect_uri)."""
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            # Sin `offline` y `consent`, Google puede no devolver el refresh token.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    url = f"{AUTH_URL}?{query}"
    print("Abriendo el navegador para autorizar el acceso a tu Google Drive...")
    print(f"Si no se abre, copia esta dirección en el navegador:\n\n{url}\n")
    webbrowser.open(url)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    if not _CallbackHandler.received.wait(WAIT_SECONDS):
        server.server_close()
        sys.exit("No llegó la autorización en cinco minutos. Vuelve a correr el script.")
    server.server_close()
    params = _CallbackHandler.params
    if params.get("state", [""])[0] != state or "code" not in params:
        sys.exit(f"Google no entregó el código ({params.get('error', ['respuesta inválida'])[0]}).")
    return params["code"][0], verifier, redirect_uri


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument("--client-id", default=settings.google_drive_client_id)
    parser.add_argument("--client-secret", default=settings.google_drive_client_secret)
    parser.add_argument("--folder-name", default="RestHub comprobantes")
    args = parser.parse_args()
    if not args.client_id or not args.client_secret:
        sys.exit("Falta el ID y el secreto del cliente OAuth (ver el README, «Respaldo en Drive»).")

    code, verifier, redirect_uri = _authorize(args.client_id)
    token = httpx.post(
        TOKEN_URL,
        data={
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    body = token.json()
    if token.status_code != httpx.codes.OK or "refresh_token" not in body:
        sys.exit(
            "Google no devolvió un refresh token. Quita el acceso de la app en "
            "https://myaccount.google.com/permissions y vuelve a correr el script."
        )
    folder = httpx.post(
        FILES_URL,
        params={"fields": "id"},
        headers={"Authorization": f"Bearer {body['access_token']}"},
        json={"name": args.folder_name, "mimeType": FOLDER_MIME},
        timeout=30,
    )
    if folder.status_code != httpx.codes.OK:
        sys.exit(f"No se pudo crear la carpeta en Drive (HTTP {folder.status_code}).")

    print("\nListo. Carga estas variables en el servicio del API (no las compartas):\n")
    print(f"GOOGLE_DRIVE_CLIENT_ID={args.client_id}")
    print("GOOGLE_DRIVE_CLIENT_SECRET=<el mismo secreto que usaste>")
    print(f"GOOGLE_DRIVE_REFRESH_TOKEN={body['refresh_token']}")
    print(f"GOOGLE_DRIVE_FOLDER_ID={folder.json()['id']}")


if __name__ == "__main__":
    main()
