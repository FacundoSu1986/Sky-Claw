"""Bandit output analyzer — extracts HIGH/MEDIUM severity findings.

Utility script for security audit workflows.
"""

import json
import logging

logger = logging.getLogger("SkyClaw.BanditAnalyzer")


def analyze_bandit_report(path: str = "bandit_output.json") -> None:
    """Load and display HIGH/MEDIUM severity findings from a Bandit JSON report."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    for entry in data.get("results", []):
        if entry["issue_severity"] in ("HIGH", "MEDIUM"):
            logger.warning(
                "%s:%s - %s (%s) - %s",
                entry["filename"],
                entry["line_number"],
                entry["test_id"],
                entry["issue_severity"],
                entry["issue_text"],
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    analyze_bandit_report()
