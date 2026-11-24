"""Local certificate authority and server certificate management.

Browsers only allow microphone access on HTTPS pages. A private network has no
public certificate, so the server runs its own small CA: install ``ca.crt`` once
on each phone and every certificate the server issues afterwards (new IP address,
yearly renewal) is trusted without further warnings.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("lancall.certs")

CA_VALID_DAYS = 3650
# Apple platforms reject TLS server certificates valid for more than 398 days.
SERVER_VALID_DAYS = 397
RENEW_BEFORE_DAYS = 30


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _write_public(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _load_pair(crt: Path, key: Path):
    cert = x509.load_pem_x509_certificate(crt.read_bytes())
    private = serialization.load_pem_private_key(key.read_bytes(), password=None)
    return cert, private


def _ensure_ca(cert_dir: Path, label: str):
    crt, key = cert_dir / "ca.crt", cert_dir / "ca.key"
    if crt.exists() and key.exists():
        try:
            cert, private = _load_pair(crt, key)
            if cert.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_BEFORE_DAYS):
                return cert, private, False
            log.warning("Local CA is about to expire; creating a new one. Phones must install the new ca.crt.")
        except Exception as exc:  # corrupt or unreadable files
            log.warning("Could not load the local CA (%s); creating a new one.", exc)

    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LAN Call"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"LAN Call Local CA ({label})"[:64]),
        ]
    )
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(private.public_key()), critical=False)
        .sign(private, hashes.SHA256())
    )
    _write_private(key, _key_pem(private))
    _write_public(crt, cert.public_bytes(serialization.Encoding.PEM))
    log.info("Created local certificate authority: %s", crt)
    return cert, private, True


def _split_names(names: list[str]) -> tuple[set[str], set[str]]:
    dns: set[str] = set()
    ips: set[str] = set()
    for raw in names:
        item = raw.strip().lower()
        if not item:
            continue
        try:
            ips.add(str(ipaddress.ip_address(item)))
        except ValueError:
            dns.add(item)
    return dns, ips


def _server_ok(cert_dir: Path, ca_cert, dns: set[str], ips: set[str]) -> bool:
    crt, key = cert_dir / "server.crt", cert_dir / "server.key"
    if not (crt.exists() and key.exists()):
        return False
    try:
        cert, _ = _load_pair(crt, key)
        cert.verify_directly_issued_by(ca_cert)
        if cert.not_valid_after_utc - _now() < dt.timedelta(days=RENEW_BEFORE_DAYS):
            return False
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have_dns = {n.lower() for n in san.get_values_for_type(x509.DNSName)}
        have_ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
        return dns <= have_dns and ips <= have_ips
    except Exception:
        return False


def ensure(cert_dir: Path, names: list[str], label: str = "server") -> bool:
    """Make sure a CA and a server certificate covering ``names`` exist.

    Returns True when the server certificate was (re)issued.
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key, ca_new = _ensure_ca(cert_dir, label)
    dns, ips = _split_names(["localhost", "127.0.0.1", "::1", *names])
    if not ca_new and _server_ok(cert_dir, ca_cert, dns, ips):
        return False

    private = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    san = [x509.DNSName(n) for n in sorted(dns)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LAN Call"),
                    x509.NameAttribute(NameOID.COMMON_NAME, f"LAN Call ({label})"[:64]),
                ]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=SERVER_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=False, crl_sign=False,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(private.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_private(cert_dir / "server.key", _key_pem(private))
    _write_public(cert_dir / "server.crt", cert.public_bytes(serialization.Encoding.PEM))
    log.info("Issued server certificate for: %s", ", ".join(sorted(dns) + sorted(ips)))
    return True
