# -*- coding: utf-8 -*-
"""
Validador Anti-SSRF para prevenir Server-Side Request Forgery.

Este módulo proporciona validación de URLs para proteger el sistema
contra ataques SSRF que podrían permitir acceso a redes internas
o endpoints de metadata de servicios cloud.
"""

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger("SkyClaw.validators.ssrf")

# Redes bloqueadas según RFC 1918 y otras categorías
BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918 - Clase A
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918 - Clase B
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918 - Clase C
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("0.0.0.0/8"),  # Any-address
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local (AWS/GCP metadata)
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]

# Hostnames bloqueados por defecto
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata",
    "kubernetes.default",
    "kubernetes.default.svc",
}

# Patrones de hostname peligrosos
BLOCKED_HOSTNAME_PATTERNS = [
    re.compile(r"\.local$", re.IGNORECASE),
    re.compile(r"\.internal$", re.IGNORECASE),
    re.compile(r"\.localhost$", re.IGNORECASE),
]

# Schemes permitidos
ALLOWED_SCHEMES = {"http", "https"}


@dataclass
class SSRFValidationResult:
    """Resultado de validación SSRF."""

    is_valid: bool
    normalized_url: Optional[str]
    blocked_reason: Optional[str]
    resolved_ip: Optional[str] = None


class SSRFValidator:
    """
    Validador de URLs para prevenir ataques SSRF.

    Este validador verifica que las URLs no apunten a:
    - Direcciones IP privadas (RFC 1918)
    - Localhost/loopback
    - Endpoints de metadata de cloud providers
    - Dominios internos (.local, .internal, etc.)

    Attributes:
        _dns_resolver: Función para resolver DNS (inyectable para testing)
    """

    def __init__(self, dns_resolver: Optional[Callable[[str], list]] = None):
        """
        Inicializa el validador SSRF.

        Args:
            dns_resolver: Función inyectable para resolver DNS (útil para testing).
                         Debe aceptar un hostname y retornar una lista de IPs.
        """
        self._dns_resolver = dns_resolver or self._default_resolver

    @staticmethod
    def _default_resolver(hostname: str) -> list:
        """
        Resolvedor DNS por defecto usando socket.getaddrinfo.

        Args:
            hostname: Nombre del host a resolver

        Returns:
            Lista de direcciones IP resueltas (únicas)
        """
        try:
            results = socket.getaddrinfo(hostname, None)
            return list({r[4][0] for r in results})
        except socket.gaierror:
            return []

    def validate(self, url: str) -> SSRFValidationResult:
        """
        Valida una URL contra ataques SSRF.

        Pasos de validación:
        1. Parsear URL y validar scheme
        2. Normalizar hostname
        3. Verificar hostname contra lista de bloqueo
        4. Resolver DNS y verificar IP contra redes bloqueadas
        5. Retornar URL normalizada si es válida

        Args:
            url: URL a validar

        Returns:
            SSRFValidationResult con el resultado de la validación
        """
        # Paso 1: Parsear URL
        try:
            parsed = urlparse(url.strip())
        except Exception as e:
            return SSRFValidationResult(
                is_valid=False,
                normalized_url=None,
                blocked_reason=f"URL malformada: {e}",
                resolved_ip=None,
            )

        # Validar scheme
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            return SSRFValidationResult(
                is_valid=False,
                normalized_url=None,
                blocked_reason=f"Scheme no permitido: {parsed.scheme}. Use http o https.",
                resolved_ip=None,
            )

        # Paso 2: Normalizar hostname
        hostname = parsed.hostname
        if not hostname:
            return SSRFValidationResult(
                is_valid=False,
                normalized_url=None,
                blocked_reason="URL sin hostname válido",
                resolved_ip=None,
            )

        hostname_lower = hostname.lower().rstrip(".")

        # Paso 3: Verificar hostname contra lista de bloqueo
        if hostname_lower in BLOCKED_HOSTNAMES:
            logger.warning(f"SSRF blocked: hostname en lista negra - {hostname_lower}")
            return SSRFValidationResult(
                is_valid=False,
                normalized_url=None,
                blocked_reason=f"Hostname bloqueado: {hostname_lower}",
                resolved_ip=None,
            )

        for pattern in BLOCKED_HOSTNAME_PATTERNS:
            if pattern.search(hostname_lower):
                logger.warning(
                    f"SSRF blocked: hostname coincide con patrón - {hostname_lower}"
                )
                return SSRFValidationResult(
                    is_valid=False,
                    normalized_url=None,
                    blocked_reason=f"Hostname bloqueado por patrón: {hostname_lower}",
                    resolved_ip=None,
                )

        # Paso 4: Resolver DNS y verificar IP
        try:
            resolved_ips = self._dns_resolver(hostname_lower)
        except Exception as e:
            logger.warning(f"SSRF: Error resolviendo DNS para {hostname_lower}: {e}")
            return SSRFValidationResult(
                is_valid=False,
                normalized_url=None,
                blocked_reason=f"Error resolviendo hostname: {e}",
                resolved_ip=None,
            )

        if not resolved_ips:
            # Fail-closed: si no se puede resolver DNS, bloquear la request
            logger.warning(
                f"SSRF: DNS resolution failed for {hostname_lower} — blocking (fail-closed)"
            )
            return SSRFValidationResult(
                is_valid=False,
                normalized_url=None,
                blocked_reason=f"DNS resolution returned no IPs for {hostname_lower}",
                resolved_ip=None,
            )
        else:
            for ip_str in resolved_ips:
                try:
                    ip_obj = ipaddress.ip_address(ip_str)
                    for network in BLOCKED_NETWORKS:
                        if ip_obj in network:
                            logger.warning(
                                f"SSRF blocked: IP {ip_str} en red bloqueada {network}"
                            )
                            return SSRFValidationResult(
                                is_valid=False,
                                normalized_url=None,
                                blocked_reason=f"IP resuelta {ip_str} está en red bloqueada",
                                resolved_ip=ip_str,
                            )
                except ValueError:
                    continue

        # Paso 5: Construir URL normalizada
        normalized_url = self._normalize_url(parsed)

        return SSRFValidationResult(
            is_valid=True,
            normalized_url=normalized_url,
            blocked_reason=None,
            resolved_ip=resolved_ips[0] if resolved_ips else None,
        )

    def _normalize_url(self, parsed) -> str:
        """
        Normaliza URL a forma canónica.

        Args:
            parsed: Objeto urlparse con la URL parseada

        Returns:
            URL normalizada como string
        """
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        port = parsed.port
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""

        # No incluir puerto si es el default del scheme
        if port and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            netloc = f"{hostname}:{port}"
        else:
            netloc = hostname

        return f"{scheme}://{netloc}{path}{query}"


def validate_url_ssrf(url: str) -> str:
    """
    Función de conveniencia para usar como field_validator.

    Args:
        url: URL a validar

    Returns:
        str: URL normalizada si es válida

    Raises:
        ValueError: Si la URL es inválida o representa riesgo SSRF
    """
    validator = SSRFValidator()
    result = validator.validate(url)

    if not result.is_valid:
        raise ValueError(f"Validación SSRF fallida: {result.blocked_reason}")

    return result.normalized_url
