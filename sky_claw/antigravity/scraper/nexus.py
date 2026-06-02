"""Nexus Mods scraper placeholder.

Audit PM-2: this module was originally scoped to a Playwright-based
headless browsing path. That direction has been **permanently dropped**
because evasive scraping violates the Nexus Mods Terms of Service. All
production Nexus access goes through the official API surface
(see :mod:`sky_claw.antigravity.scraper.scraper_agent` and its
``_api_request`` path).

The class below is kept only as a typed placeholder so legacy imports
that referenced it do not break; it has no runtime behavior.
"""

from __future__ import annotations


class NexusScraper:
    """Stub scraper for Nexus Mods."""

    pass
