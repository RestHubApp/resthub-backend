"""Respaldo de comprobantes en Google Drive (API v3) con OAuth de la cuenta del dueño.

Una cuenta de servicio no tiene cuota en un Drive personal, así que se usa el
refresh token de la cuenta del dueño (se obtiene una vez con
`scripts/drive_authorize.py`, alcance `drive.file`: la app solo ve lo que ella
misma crea). Documentación:

- Subida multipart (hasta 5 MB, metadatos y contenido en una sola petición):
  https://developers.google.com/drive/api/guides/manage-uploads
- OAuth 2.0 para apps de escritorio y renovación con refresh token:
  https://developers.google.com/identity/protocols/oauth2/native-app
- Carpetas y consultas de archivos (`files.list`, `q`):
  https://developers.google.com/drive/api/guides/search-files

Las carpetas son privadas (nadie más que la cuenta las ve) y se arman como
`<carpeta raíz>/<restaurante>/<año>/<año-mes>`. Subir es idempotente por nombre:
un reintento no duplica el archivo. Ningún log lleva datos del comprobante.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any

import httpx

from resthub.core.logs import get_logger
from resthub.modules.billing.domain.exceptions import DocumentArchiveError
from resthub.modules.billing.ports.delivery_ports import FetchedDocument

logger = get_logger("resthub.billing.drive")

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105  # nosec B105
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
TIMEOUT_SECONDS = 30.0
# Se renueva el token un poco antes de que venza.
_EXPIRY_MARGIN_SECONDS = 60


def _quote(value: str) -> str:
    """Un texto dentro de una consulta `q` de Drive: entre comillas simples y escapado."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _safe_name(value: str) -> str:
    """Un nombre de carpeta sin separadores ni caracteres de control."""
    cleaned = "".join(ch for ch in value if ch.isprintable() and ch not in "/\\")
    return " ".join(cleaned.split())[:100] or "sin-nombre"


class GoogleDriveArchive:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        root_folder_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._refresh_token = refresh_token.strip()
        self._root = root_folder_id.strip()
        self._transport = transport
        self._access_token = ""  # nosec B105
        self._expires_at = 0.0
        self._folders: dict[tuple[str, ...], str] = {}
        self._lock = asyncio.Lock()

    def is_enabled(self) -> bool:
        return bool(self._client_id and self._client_secret and self._refresh_token and self._root)

    async def store(self, folder: Sequence[str], filename: str, document: FetchedDocument) -> str:
        async with (
            self._lock,
            httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT_SECONDS) as client,
        ):
            parent = await self._ensure_folder(client, tuple(_safe_name(p) for p in folder))
            existing = await self._find(client, parent, filename, mime=None)
            if existing is not None:
                return existing[1]
            return await self._upload(client, parent, filename, document)

    # -- Acceso -------------------------------------------------------------

    async def _token(self, client: httpx.AsyncClient, *, force: bool = False) -> str:
        if not force and self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        try:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as error:
            message = "No se pudo conectar con Google para renovar el acceso."
            raise DocumentArchiveError(message) from error
        body = _json(response)
        token = body.get("access_token")
        if response.status_code != HTTPStatus.OK or not isinstance(token, str):
            logger.warning(
                "billing.drive_token_failed",
                status=response.status_code,
                google_error=str(body.get("error", ""))[:60],
            )
            message = (
                "Google rechazó las credenciales del respaldo; hay que volver a autorizarlas "
                "(scripts/drive_authorize.py)."
            )
            raise DocumentArchiveError(message)
        self._access_token = token
        expires_in = body.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3_000.0
        self._expires_at = time.monotonic() + max(lifetime - _EXPIRY_MARGIN_SECONDS, 0)
        return token

    async def _call(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,  # noqa: ANN401 - se reenvían tal cual a `httpx.AsyncClient.request`
    ) -> httpx.Response:
        """Una llamada a Drive; si el token caducó, se renueva y se repite una vez."""
        for attempt in range(2):
            token = await self._token(client, force=attempt == 1)
            try:
                response = await client.request(
                    method,
                    url,
                    headers={**(headers or {}), "Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPError as error:
                message = "No se pudo conectar con Google Drive."
                raise DocumentArchiveError(message) from error
            if response.status_code != HTTPStatus.UNAUTHORIZED:
                break
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            logger.warning("billing.drive_request_failed", status=response.status_code)
            message = f"Google Drive rechazó la operación (HTTP {response.status_code})."
            raise DocumentArchiveError(message)
        return response

    # -- Carpetas y archivos -----------------------------------------------

    async def _find(
        self, client: httpx.AsyncClient, parent: str, name: str, *, mime: str | None
    ) -> tuple[str, str] | None:
        query = f"name = {_quote(name)} and {_quote(parent)} in parents and trashed = false"
        query += (
            f" and mimeType = {_quote(FOLDER_MIME)}"
            if mime
            else f" and mimeType != {_quote(FOLDER_MIME)}"
        )
        response = await self._call(
            client,
            "GET",
            FILES_URL,
            params={"q": query, "fields": "files(id,webViewLink)", "pageSize": 1},
        )
        files = _json(response).get("files")
        if isinstance(files, list) and files:
            first = files[0]
            return str(first["id"]), str(first.get("webViewLink", ""))
        return None

    async def _ensure_folder(self, client: httpx.AsyncClient, path: tuple[str, ...]) -> str:
        parent = self._root
        for depth in range(1, len(path) + 1):
            key = path[:depth]
            cached = self._folders.get(key)
            if cached is not None:
                parent = cached
                continue
            found = await self._find(client, parent, key[-1], mime=FOLDER_MIME)
            if found is not None:
                parent = found[0]
            else:
                created = await self._call(
                    client,
                    "POST",
                    FILES_URL,
                    params={"fields": "id"},
                    json={"name": key[-1], "mimeType": FOLDER_MIME, "parents": [parent]},
                )
                parent = str(_json(created)["id"])
            self._folders[key] = parent
        return parent

    async def _upload(
        self, client: httpx.AsyncClient, parent: str, filename: str, document: FetchedDocument
    ) -> str:
        boundary = f"resthub-{uuid.uuid4().hex}"
        metadata = json.dumps({"name": filename, "parents": [parent]}).encode()
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                metadata,
                f"\r\n--{boundary}\r\nContent-Type: {document.media_type}\r\n\r\n".encode(),
                document.content,
                f"\r\n--{boundary}--".encode(),
            ]
        )
        response = await self._call(
            client,
            "POST",
            UPLOAD_URL,
            params={"uploadType": "multipart", "fields": "id,webViewLink"},
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        return str(_json(response).get("webViewLink", ""))


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
