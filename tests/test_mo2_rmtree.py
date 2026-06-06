"""M-4: deleting a mod directory must remove read-only files (common in extracted
mods / ``.git`` on Windows) instead of silently leaving a partially-deleted tree
the way ``shutil.rmtree(ignore_errors=True)`` did.
"""

from __future__ import annotations

import stat

from sky_claw.local.mo2.vfs import _rmtree_force


def test_rmtree_force_removes_readonly_file(tmp_path):
    mod_dir = tmp_path / "SomeMod"
    sub = mod_dir / "meshes"
    sub.mkdir(parents=True)
    f = sub / "armor.nif"
    f.write_bytes(b"data")
    f.chmod(stat.S_IREAD)  # read-only — blocks rmtree on Windows

    _rmtree_force(mod_dir)

    assert not mod_dir.exists()
