"""Lectura del certificado digital del local: .pfx/.p12 con su contraseña, o .pem.

Lycet firma con un PEM que lleva la llave privada y el certificado juntos; acá se
abre lo que subió el local, se comprueba que la llave corresponde al certificado
y se devuelve ese PEM (la llave sin contraseña: se guarda cifrada en reposo).
Ningún mensaje lleva la contraseña ni el contenido del archivo.
"""

from __future__ import annotations

from datetime import UTC

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12

from resthub.modules.billing.domain.exceptions import InvalidCertificateError
from resthub.modules.billing.ports.billing_ports import ParsedCertificate

_PEM_MARKER = b"-----BEGIN"
_NO_KEY = "El certificado no trae su llave privada: sube el .pfx o .p12 completo."
# SUNAT firma con RSA: un certificado de otro tipo no le sirve.
_NOT_RSA = "El certificado no es RSA: SUNAT no lo acepta para firmar."
_MISMATCH = "La llave privada no corresponde al certificado."


def _subject(certificate: x509.Certificate) -> str:
    names = certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return str(names[0].value)[:120] if names else ""


def _pem(key: rsa.RSAPrivateKey, certificate: x509.Certificate) -> str:
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (key_pem + certificate.public_bytes(serialization.Encoding.PEM)).decode("ascii")


def _from_pem(content: bytes, password: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    try:
        certificates = x509.load_pem_x509_certificates(content)
    except ValueError:
        message = "El archivo .pem no trae un certificado válido."
        raise InvalidCertificateError(message) from None
    try:
        key = serialization.load_pem_private_key(content, password.encode() or None)
    except (ValueError, TypeError):
        raise InvalidCertificateError(_NO_KEY) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidCertificateError(_NOT_RSA)
    return key, certificates[0]


def _from_pkcs12(content: bytes, password: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    try:
        key, certificate, _ = pkcs12.load_key_and_certificates(content, password.encode() or None)
    except ValueError:
        message = "No se pudo abrir el certificado: revisa el archivo y su contraseña."
        raise InvalidCertificateError(message) from None
    if key is None or certificate is None:
        raise InvalidCertificateError(_NO_KEY)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidCertificateError(_NOT_RSA)
    return key, certificate


class CryptographyCertificateReader:
    def read(self, content: bytes, password: str) -> ParsedCertificate:
        if not content:
            message = "El archivo del certificado está vacío."
            raise InvalidCertificateError(message)
        if content.lstrip().startswith(_PEM_MARKER):
            key, certificate = _from_pem(content, password)
        else:
            key, certificate = _from_pkcs12(content, password)
        if key.public_key() != certificate.public_key():
            raise InvalidCertificateError(_MISMATCH)
        return ParsedCertificate(
            pem=_pem(key, certificate),
            expires_at=certificate.not_valid_after_utc.astimezone(UTC),
            subject=_subject(certificate),
        )
