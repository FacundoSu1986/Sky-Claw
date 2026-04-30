"""Tests for SEC-03: PurpleScanner taint tracking for ast.Attribute reads.

Verifies that file.read() (and any <obj>.read()) is correctly identified as a
tainted source, so downstream flow into exec()/eval() is flagged.
"""

from __future__ import annotations

from sky_claw.security.purple_scanner import run_scan


class TestPurpleScannerTaintTracking:
    """SEC-03: ast.Attribute read() must be detected as tainted source."""

    def test_file_read_is_tainted_source(self):
        """Variable assigned from f.read() must be marked tainted."""
        code = """
with open('data.txt') as f:
    payload = f.read()
exec(payload)
"""
        findings = run_scan(code, filename="test_read_taint.py")
        taint_findings = [f for f in findings if "Variable sucia" in f["message"]]
        assert len(taint_findings) >= 1, f"Expected taint-flow finding for f.read() → exec(), got: {findings}"
        assert any("payload" in f["message"] for f in taint_findings)

    def test_open_is_tainted_source(self):
        """Variable assigned from open() must be marked tainted."""
        code = """
data = open('malicious.py').read()
eval(data)
"""
        findings = run_scan(code, filename="test_open_taint.py")
        taint_findings = [f for f in findings if "Variable sucia" in f["message"]]
        assert len(taint_findings) >= 1, f"Expected taint-flow finding for open() → eval(), got: {findings}"

    def test_input_is_tainted_source(self):
        """Variable assigned from input() must be marked tainted."""
        code = """
user_input = input("> ")
exec(user_input)
"""
        findings = run_scan(code, filename="test_input_taint.py")
        taint_findings = [f for f in findings if "Variable sucia" in f["message"]]
        assert len(taint_findings) >= 1, f"Expected taint-flow finding for input() → exec(), got: {findings}"

    def test_arbitrary_read_attribute_is_tainted(self):
        """Any obj.read() should be treated as tainted (conservative policy)."""
        code = """
buf = stream.read()
os.system(buf)
"""
        findings = run_scan(code, filename="test_stream_read.py")
        taint_findings = [f for f in findings if "Variable sucia" in f["message"]]
        assert len(taint_findings) >= 1, f"Expected taint-flow finding for stream.read() → os.system(), got: {findings}"

    def test_non_read_attribute_not_tainted(self):
        """Attributes other than 'read' should NOT be tainted."""
        code = """
val = obj.calculate()
exec(val)
"""
        findings = run_scan(code, filename="test_non_read.py")
        taint_findings = [f for f in findings if "Variable sucia" in f["message"]]
        assert len(taint_findings) == 0, f"Did not expect taint finding for obj.calculate(), got: {findings}"
