"""Regression guard for the Community Shaders release channel.

GitHub's ``GET /repos/{owner}/{repo}/releases/latest`` endpoint returns the
latest published *non-draft, non-prerelease* release.  Sky-Claw deliberately
uses that endpoint for Community Shaders so an RC/beta published upstream does
not become an install candidate automatically.

This test is intentionally hermetic: it freezes the integration contract (the
endpoint Sky-Claw calls) instead of reaching GitHub or duplicating GitHub's
release-selection algorithm locally.
"""

from __future__ import annotations

import sky_claw.local.tools_installer as tools_installer


def test_community_shaders_uses_latest_stable_github_release_endpoint() -> None:
    """Community Shaders must stay on GitHub's stable-only ``latest`` channel."""
    assert tools_installer._CS_RELEASES_URL == (
        "https://api.github.com/repos/community-shaders/"
        "skyrim-community-shaders/releases/latest"
    )
