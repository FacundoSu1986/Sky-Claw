"""H-06: la blocklist del SSRFValidator cubre CGN/ULA/multicast/reserved e IPv4-mapped IPv6.

El validador acepta un ``dns_resolver`` inyectable, así que se testea sin red:
se fuerza la IP resuelta para cada hostname y se verifica el bloqueo.
"""

from __future__ import annotations

import pytest

from sky_claw.antigravity.core.validators.ssrf import SSRFValidator


def _validator_for(ip: str) -> SSRFValidator:
    return SSRFValidator(dns_resolver=lambda _host: [ip])


@pytest.mark.parametrize(
    "ip",
    [
        "100.64.0.1",  # CGN / shared (RFC 6598)
        "100.127.255.254",  # CGN (borde)
        "224.0.0.1",  # multicast IPv4
        "239.255.255.250",  # multicast IPv4 (SSDP)
        "240.0.0.1",  # reserved IPv4
        "fc00::1",  # ULA IPv6
        "fd12:3456::1",  # ULA IPv6
        "ff02::1",  # multicast IPv6
        "169.254.169.254",  # link-local metadata (ya existía; regresión)
        "10.0.0.5",  # RFC1918 (regresión)
    ],
)
def test_ip_en_rango_bloqueado_se_rechaza(ip: str) -> None:
    result = _validator_for(ip).validate("https://interno.example.com/x")
    assert result.is_valid is False
    assert result.blocked_reason is not None


def test_ipv4_mapped_ipv6_no_evade_el_bloqueo() -> None:
    """H-06: ::ffff:169.254.169.254 se normaliza a su IPv4 y se bloquea igual."""
    result = _validator_for("::ffff:169.254.169.254").validate("https://meta.example.com/")
    assert result.is_valid is False


def test_ipv4_mapped_de_ip_publica_pasa() -> None:
    """Una IPv4-mapped de una IP pública no se bloquea (no hay falso positivo)."""
    result = _validator_for("::ffff:93.184.216.34").validate("https://example.com/")
    assert result.is_valid is True


def test_ip_publica_pasa() -> None:
    result = _validator_for("93.184.216.34").validate("https://example.com/")
    assert result.is_valid is True
