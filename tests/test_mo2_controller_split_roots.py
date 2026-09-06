"""Tests de contrato para MO2Controller con separación de raíces (Issue #557).

Verifica la topología estricta:
    install != data != mods
con trampas en las rutas erróneas para demostrar que cada operación usa su raíz
canónica y no contamina las demás.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import patch

import pytest

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.mo2.vfs import MO2Controller


@pytest.fixture()
def split_topology(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Crea una topología completa install != data != mods con trampas."""
    install = tmp_path / "Install"
    data = tmp_path / "Instance"
    mods = tmp_path / "CustomMods"

    install.mkdir(parents=True)
    data.mkdir(parents=True)
    mods.mkdir(parents=True)

    # 1. Archivos legítimos
    (install / "ModOrganizer.exe").write_bytes(b"fake-mo2-exe")

    data_profile_dir = data / "profiles" / "Test"
    data_profile_dir.mkdir(parents=True)
    (data_profile_dir / "modlist.txt").write_text("+DataMod\n", encoding="utf-8-sig")

    (data / "overwrite").mkdir(parents=True)

    legit_mod_dir = mods / "TestMod"
    legit_mod_dir.mkdir(parents=True)
    (legit_mod_dir / "mod_file.txt").write_text("legit content", encoding="utf-8")

    # 2. Trampas (rutas erróneas donde una implementación monorroot caería)
    install_profile_dir = install / "profiles" / "Test"
    install_profile_dir.mkdir(parents=True)
    (install_profile_dir / "modlist.txt").write_text("+InstallTrap\n", encoding="utf-8-sig")

    install_mod_trap = install / "mods" / "TestMod"
    install_mod_trap.mkdir(parents=True)
    (install_mod_trap / "trap.txt").write_text("install trap", encoding="utf-8")

    data_mod_trap = data / "mods" / "TestMod"
    data_mod_trap.mkdir(parents=True)
    (data_mod_trap / "trap.txt").write_text("data trap", encoding="utf-8")

    return {
        "install": install,
        "data": data,
        "mods": mods,
        "install_profile_modlist": install_profile_dir / "modlist.txt",
        "data_profile_modlist": data_profile_dir / "modlist.txt",
        "install_mod_trap": install_mod_trap,
        "data_mod_trap": data_mod_trap,
        "legit_mod": legit_mod_dir,
    }


class TestMO2ControllerSplitRootsContract:
    """Contrato E2E de MO2Controller operando sobre raíces separadas."""

    def test_constructor_explicito_requiere_las_tres_raices(self, tmp_path: pathlib.Path) -> None:
        """El modo nuevo exige install_root + data_root + mods_dir sin omisiones parciales."""
        validator = PathValidator(roots=[tmp_path])
        install = tmp_path / "Install"
        data = tmp_path / "Data"
        mods = tmp_path / "Mods"
        install.mkdir()
        data.mkdir()
        mods.mkdir()

        # Falta mods_dir
        with pytest.raises(ValueError, match="install_root, data_root y mods_dir"):
            MO2Controller(install_root=install, data_root=data, path_validator=validator)

        # Falta install_root
        with pytest.raises(ValueError, match="install_root, data_root y mods_dir"):
            MO2Controller(data_root=data, mods_dir=mods, path_validator=validator)

        # Falta data_root
        with pytest.raises(ValueError, match="install_root, data_root y mods_dir"):
            MO2Controller(install_root=install, mods_dir=mods, path_validator=validator)

    def test_constructor_legacy_portable_sigue_funcionando(self, tmp_path: pathlib.Path) -> None:
        """El modo legacy portable infiere data_root e install_root iguales y mods_dir=data/mods."""
        validator = PathValidator(roots=[tmp_path])
        ctrl = MO2Controller(tmp_path, path_validator=validator)

        assert ctrl.install_root == tmp_path.resolve()
        assert ctrl.data_root == tmp_path.resolve()
        assert ctrl.mods_dir == (tmp_path / "mods").resolve()
        assert ctrl.root == tmp_path.resolve()

    @pytest.mark.asyncio
    async def test_read_modlist_lee_data_root_y_no_install(self, split_topology: dict[str, pathlib.Path]) -> None:
        """read_modlist debe leer de data_root/profiles y jamás de install_root/profiles."""
        validator = PathValidator(roots=[split_topology["install"], split_topology["data"], split_topology["mods"]])
        ctrl = MO2Controller(
            install_root=split_topology["install"],
            data_root=split_topology["data"],
            mods_dir=split_topology["mods"],
            path_validator=validator,
        )

        entries = [name async for name, _ in ctrl.read_modlist(profile="Test")]

        assert "DataMod" in entries
        assert "InstallTrap" not in entries

    @pytest.mark.asyncio
    async def test_add_mod_to_modlist_escribe_data_root_y_no_toca_install(
        self, split_topology: dict[str, pathlib.Path]
    ) -> None:
        """add_mod_to_modlist muta data_root y no toca install_root/profiles."""
        validator = PathValidator(roots=[split_topology["install"], split_topology["data"], split_topology["mods"]])
        ctrl = MO2Controller(
            install_root=split_topology["install"],
            data_root=split_topology["data"],
            mods_dir=split_topology["mods"],
            path_validator=validator,
        )

        install_modlist_antes = split_topology["install_profile_modlist"].read_text(encoding="utf-8-sig")

        await ctrl.add_mod_to_modlist("NuevoMod", profile="Test")

        data_modlist_despues = split_topology["data_profile_modlist"].read_text(encoding="utf-8-sig")
        install_modlist_despues = split_topology["install_profile_modlist"].read_text(encoding="utf-8-sig")

        assert "+NuevoMod" in data_modlist_despues
        assert install_modlist_despues == install_modlist_antes
        assert "NuevoMod" not in install_modlist_despues

    @pytest.mark.asyncio
    async def test_toggle_mod_in_modlist_muta_data_root_y_no_install(
        self, split_topology: dict[str, pathlib.Path]
    ) -> None:
        """toggle_mod_in_modlist modifica data_root y no toca install_root/profiles."""
        validator = PathValidator(roots=[split_topology["install"], split_topology["data"], split_topology["mods"]])
        ctrl = MO2Controller(
            install_root=split_topology["install"],
            data_root=split_topology["data"],
            mods_dir=split_topology["mods"],
            path_validator=validator,
        )

        install_modlist_antes = split_topology["install_profile_modlist"].read_text(encoding="utf-8-sig")

        await ctrl.toggle_mod_in_modlist("DataMod", profile="Test", enable=False)

        data_modlist_despues = split_topology["data_profile_modlist"].read_text(encoding="utf-8-sig")
        install_modlist_despues = split_topology["install_profile_modlist"].read_text(encoding="utf-8-sig")

        assert "-DataMod" in data_modlist_despues
        assert install_modlist_despues == install_modlist_antes

    @pytest.mark.asyncio
    async def test_remove_mod_from_modlist_muta_data_root_y_no_install(
        self, split_topology: dict[str, pathlib.Path]
    ) -> None:
        """remove_mod_from_modlist elimina del modlist de data_root y no toca install_root."""
        validator = PathValidator(roots=[split_topology["install"], split_topology["data"], split_topology["mods"]])
        ctrl = MO2Controller(
            install_root=split_topology["install"],
            data_root=split_topology["data"],
            mods_dir=split_topology["mods"],
            path_validator=validator,
        )

        install_modlist_antes = split_topology["install_profile_modlist"].read_text(encoding="utf-8-sig")

        await ctrl.remove_mod_from_modlist("DataMod", profile="Test")

        data_modlist_despues = split_topology["data_profile_modlist"].read_text(encoding="utf-8-sig")
        install_modlist_despues = split_topology["install_profile_modlist"].read_text(encoding="utf-8-sig")

        assert "DataMod" not in data_modlist_despues
        assert install_modlist_despues == install_modlist_antes

    @pytest.mark.asyncio
    async def test_delete_mod_files_borra_en_mods_dir_y_no_toca_trampas(
        self, split_topology: dict[str, pathlib.Path]
    ) -> None:
        """delete_mod_files borra exclusivamente en mods_dir y no toca install/mods ni data/mods."""
        validator = PathValidator(roots=[split_topology["install"], split_topology["data"], split_topology["mods"]])
        ctrl = MO2Controller(
            install_root=split_topology["install"],
            data_root=split_topology["data"],
            mods_dir=split_topology["mods"],
            path_validator=validator,
        )

        assert split_topology["legit_mod"].exists()
        assert split_topology["install_mod_trap"].exists()
        assert split_topology["data_mod_trap"].exists()

        await ctrl.delete_mod_files("TestMod")

        # El mod legítimo en MODS_DIR fue eliminado
        assert not split_topology["legit_mod"].exists()

        # Las trampas siguen intactas
        assert split_topology["install_mod_trap"].exists()
        assert (split_topology["install_mod_trap"] / "trap.txt").read_text(encoding="utf-8") == "install trap"
        assert split_topology["data_mod_trap"].exists()
        assert (split_topology["data_mod_trap"] / "trap.txt").read_text(encoding="utf-8") == "data trap"

    @pytest.mark.asyncio
    async def test_launch_game_ejecuta_en_install_root(self, split_topology: dict[str, pathlib.Path]) -> None:
        """launch_game ejecuta ModOrganizer.exe en install_root con cwd=install_root."""
        validator = PathValidator(roots=[split_topology["install"], split_topology["data"], split_topology["mods"]])
        ctrl = MO2Controller(
            install_root=split_topology["install"],
            data_root=split_topology["data"],
            mods_dir=split_topology["mods"],
            path_validator=validator,
        )

        fake_proc = asyncio.create_task(asyncio.sleep(0.01))
        fake_proc.pid = 99999

        with (
            patch("asyncio.create_subprocess_exec") as mock_spawn,
            patch("sky_claw.local.mo2.vfs._verify_pid_alive", return_value=None),
            patch("psutil.Process") as mock_psutil_proc,
        ):
            mock_psutil_proc.return_value.create_time.return_value = 12345.0
            mock_spawn.return_value = fake_proc

            res = await ctrl.launch_game(profile="Test")

            assert res["pid"] == 99999
            mock_spawn.assert_called_once()
            cmd_args, kwargs = mock_spawn.call_args
            expected_exe = str(split_topology["install"] / "ModOrganizer.exe")
            assert cmd_args[0] == expected_exe
            assert cmd_args[1:3] == ("-p", "Test")
            assert kwargs["cwd"] == str(split_topology["install"])
