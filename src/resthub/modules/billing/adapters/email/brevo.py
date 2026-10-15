"""Correo transaccional por la API HTTPS de Brevo.

Railway (plan Hobby) bloquea el SMTP saliente, así que el correo va por HTTPS:
`POST https://api.brevo.com/v3/smtp/email` con la cabecera `api-key`; responde
201 con el `messageId`. Los adjuntos van en base64 (`attachment`). Documentación:
https://developers.brevo.com/reference/sendtransacemail

La clave y la dirección del destinatario nunca se escriben en los logs.
"""

from __future__ import annotations

import base64
from http import HTTPStatus
from typing import Any

import httpx

from resthub.core.logs import get_logger
from resthub.modules.billing.domain.exceptions import EmailDeliveryError
from resthub.modules.billing.ports.delivery_ports import OutgoingEmail

logger = get_logger("resthub.billing.brevo")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_SECONDS = 10.0
_ACCEPTED = (HTTPStatus.CREATED, HTTPStatus.ACCEPTED)


class BrevoEmailSender:
    def __init__(
        self,
        api_key: str,
        from_address: str,
        from_name: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._from_address = from_address.strip()
        self._from_name = from_name.strip() or "RestHub"
        # El transporte se inyecta en las pruebas: ninguna llama a Brevo de verdad.
        self._transport = transport

    def is_enabled(self) -> bool:
        return bool(self._api_key and self._from_address)

    async def send(self, email: OutgoingEmail) -> None:
        recipient: dict[str, str] = {"email": email.to_address}
        if email.to_name:
            recipient["name"] = email.to_name
        payload: dict[str, Any] = {
            "sender": {"name": self._from_name, "email": self._from_address},
            "to": [recipient],
            "subject": email.subject,
            "htmlContent": email.html,
            "textContent": email.text,
        }
        if email.attachments:
            payload["attachment"] = [
                {"name": item.filename, "content": base64.b64encode(item.content).decode("ascii")}
                for item in email.attachments
            ]
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    BREVO_URL,
                    json=payload,
                    headers={"api-key": self._api_key, "accept": "application/json"},
                )
        except httpx.HTTPError as error:
            logger.warning("billing.email_unreachable", error=type(error).__name__)
            message = "No se pudo conectar con el servicio de correo. Inténtalo de nuevo."
            raise EmailDeliveryError(message) from error
        if response.status_code in _ACCEPTED:
            logger.info("billing.email_sent")
            return
        logger.warning(
            "billing.email_rejected",
            status=response.status_code,
            brevo_code=_error_code(response),
        )
        if response.status_code == HTTPStatus.BAD_REQUEST:
            message = "El servicio de correo rechazó el mensaje. Revisa la dirección."
        else:
            message = "El servicio de correo no pudo enviar el mensaje. Inténtalo más tarde."
        raise EmailDeliveryError(message)


def _error_code(response: httpx.Response) -> str:
    """El `code` del error de Brevo (`invalid_parameter`…); no el mensaje, que puede citar datos."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("code", ""))[:60] if isinstance(body, dict) else ""
