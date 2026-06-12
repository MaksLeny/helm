"""
Helm — самоподписанный TLS-сертификат для HTTPS-режима.

HTTPS нужен, чтобы браузер на телефоне считал Helm «защищённым контекстом» —
тогда становятся доступны гироскоп (для WinWave) и Web Push.

Сертификат генерируется один раз и кладётся рядом с проектом (helm_cert.pem +
helm_key.pem). Способы по приоритету:
  1. библиотека cryptography (чистый Python, кроссплатформенно);
  2. системный openssl (если есть в PATH).
Если ни одного нет — HTTPS недоступен, вернём (None, причина).

Сертификат самоподписанный: браузер покажет предупреждение «не защищено» —
это нормально для локальной сети, нужно один раз нажать «всё равно открыть».
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("helm")

_DIR = Path(__file__).parent.parent
_CERT = _DIR / "helm_cert.pem"
_KEY = _DIR / "helm_key.pem"


def cert_paths() -> Tuple[Path, Path]:
    return _CERT, _KEY


def cert_exists() -> bool:
    return _CERT.exists() and _KEY.exists()


def _local_ips() -> list[str]:
    """Локальные IP для вписывания в SAN сертификата."""
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:  # noqa: BLE001
        pass
    return sorted(ips)


def _gen_with_cryptography() -> bool:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except Exception:  # noqa: BLE001
        return False
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Helm")])
        san = [x509.DNSName("localhost")]
        for ip in _local_ips():
            try:
                san.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except Exception:  # noqa: BLE001
                pass
        now = datetime.datetime.utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256())
        )
        _KEY.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
        _CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("TLS: генерация через cryptography не удалась: %s", e)
        return False


def _gen_with_openssl() -> bool:
    ossl = shutil.which("openssl")
    if not ossl:
        return False
    try:
        subprocess.run(
            [ossl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(_KEY), "-out", str(_CERT),
             "-days", "3650", "-subj", "/CN=Helm",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            capture_output=True, timeout=30, check=True,
        )
        return _CERT.exists() and _KEY.exists()
    except Exception as e:  # noqa: BLE001
        log.warning("TLS: генерация через openssl не удалась: %s", e)
        return False


def ensure_cert() -> Tuple[Optional[Path], Optional[Path], str]:
    """Гарантировать наличие сертификата. Вернуть (cert, key, сообщение).

    Если уже есть — отдаём пути. Иначе пытаемся сгенерировать. При неудаче —
    (None, None, причина).
    """
    if cert_exists():
        return _CERT, _KEY, "сертификат уже есть"
    if _gen_with_cryptography():
        log.info("TLS: самоподписанный сертификат создан (cryptography)")
        return _CERT, _KEY, "создан через cryptography"
    if _gen_with_openssl():
        log.info("TLS: самоподписанный сертификат создан (openssl)")
        return _CERT, _KEY, "создан через openssl"
    return (None, None,
            "нет ни cryptography, ни openssl — установите: pip install cryptography")
