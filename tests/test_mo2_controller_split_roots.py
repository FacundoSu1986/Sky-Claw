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

from sky_claw.app.security.path_validator import PathValidator, PathViolationError
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

    def test_mo2_controller_posicion_launch_timeout_compatible(self, tmp_path: pathlib.Path) -> None:
        """MO2Controller conserva compatibilidad posicional con 3 argumentos: (root, validator, timeout)."""
        (tmp_path / "ModOrganizer.exe").write_bytes(b"exe")
        validator = PathValidator(roots=[tmp_path])
        ctrl = MO2Controller(tmp_path, validator, 42)
        assert ctrl._spawn_timeout == 42
        assert ctrl.install_root == tmp_path.resolve()
        assert ctrl.data_root == tmp_path.resolve()
        assert ctrl.mods_dir == (tmp_path / "mods").resolve()

    def test_recovery_productores_usan_mods_dir_sin_tocar_trampas(
        self, split_topology: dict[str, pathlib.Path]
    ) -> None:
        """construir_productores_de_move_aside usa mods_dir de MO2Controller sin volver a install/mods."""
        from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner
        from sky_claw.local.tools.rollback_reconciler import construir_productores_de_move_aside

        ctrl = MO2Controller(
            install_root=split_topology["install"],
            data_root=split_topology["data"],
            mods_dir=split_topology["mods"],
            path_validator=PathValidator(
                roots=[split_topology["install"], split_topology["data"], split_topology["mods"]]
            ),
        )
        game = split_topology["install"] / "Skyrim"
        game.mkdir(parents=True, exist_ok=True)

        productores = construir_productores_de_move_aside(
            mo2_root=ctrl.install_root,
            mods_dir=ctrl.mods_dir,
            game=game,
        )
        dyndolod = next(p for p in productores if p.nombre == "dyndolod")
        assert ctrl.mods_dir / DynDOLODRunner.DYNDOLLOD_MOD_NAME in dyndolod.destinos
        assert split_topology["install"] / "mods" / DynDOLODRunner.DYNDOLLOD_MOD_NAME not in dyndolod.destinos
        assert split_topology["data"] / "mods" / DynDOLODRunner.DYNDOLLOD_MOD_NAME not in dyndolod.destinos

    def test_mo2_mods_path_relativo_rechazado_en_sandbox_y_scanner(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MO2_MODS_PATH relativo (p.ej. '.') se rechaza y no se registra como raíz de sandbox ni en scanner."""
        from sky_claw.app_context import _mods_candidatos_para_sandbox
        from sky_claw.local.discovery.scanner import EnvironmentScanner

        mo2_root = tmp_path / "MO2_Install"
        mo2_root.mkdir()
        (mo2_root / "ModOrganizer.exe").write_bytes(b"fake-exe")

        # Con MO2_MODS_PATH relativo a un directorio existente como '.'
        monkeypatch.setenv("MO2_MODS_PATH", ".")
        candidatos = _mods_candidatos_para_sandbox(mo2_root)
        cwd_resolved = pathlib.Path(".").resolve()
        assert cwd_resolved not in candidatos

        scanner = EnvironmentScanner()
        data_root, mods_dir = scanner._resolve_mo2_instance_roots(mo2_root)
        assert mods_dir != cwd_resolved
        assert mods_dir == mo2_root / "mods"

    def test_constructor_explicito_rechaza_install_root_fuera_de_sandbox(self, tmp_path: pathlib.Path) -> None:
        """P1-A1.1: install_root fuera de sandbox -> PathViolationError fail-closed."""
        sandbox = tmp_path / "Sandbox"
        sandbox.mkdir()
        validator = PathValidator(roots=[sandbox])
        outside = tmp_path / "Outside"
        outside.mkdir()
        data = sandbox / "Data"
        mods = sandbox / "Mods"
        data.mkdir()
        mods.mkdir()
        with pytest.raises(PathViolationError):
            MO2Controller(install_root=outside, data_root=data, mods_dir=mods, path_validator=validator)

    def test_constructor_explicito_rechaza_data_root_fuera_de_sandbox(self, tmp_path: pathlib.Path) -> None:
        """P1-A1.2: data_root fuera de sandbox -> PathViolationError fail-closed."""
        sandbox = tmp_path / "Sandbox"
        sandbox.mkdir()
        validator = PathValidator(roots=[sandbox])
        outside = tmp_path / "Outside"
        outside.mkdir()
        install = sandbox / "Install"
        mods = sandbox / "Mods"
        install.mkdir()
        mods.mkdir()
        with pytest.raises(PathViolationError):
            MO2Controller(install_root=install, data_root=outside, mods_dir=mods, path_validator=validator)

    def test_constructor_explicito_rechaza_mods_dir_fuera_de_sandbox(self, tmp_path: pathlib.Path) -> None:
        """P1-A1.3: mods_dir fuera de sandbox -> PathViolationError fail-closed."""
        sandbox = tmp_path / "Sandbox"
        sandbox.mkdir()
        validator = PathValidator(roots=[sandbox])
        outside = tmp_path / "Outside"
        outside.mkdir()
        install = sandbox / "Install"
        data = sandbox / "Data"
        install.mkdir()
        data.mkdir()
        with pytest.raises(PathViolationError):
            MO2Controller(install_root=install, data_root=data, mods_dir=outside, path_validator=validator)

    def test_constructor_explicito_rechaza_symlink_que_escapa_del_sandbox(self, tmp_path: pathlib.Path) -> None:
        """P1-A1.4: symlink/junction que escapa del sandbox -> PathViolationError fail-closed."""
        sandbox = tmp_path / "Sandbox"
        sandbox.mkdir()
        validator = PathValidator(roots=[sandbox])
        outside = tmp_path / "Outside"
        outside.mkdir()
        data = sandbox / "Data"
        mods = sandbox / "Mods"
        data.mkdir()
        mods.mkdir()
        symlink_install = sandbox / "SymlinkInstall"
        try:
            symlink_install.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks no soportados o privilegios insuficientes en esta plataforma")
        with pytest.raises(PathViolationError):
            MO2Controller(install_root=symlink_install, data_root=data, mods_dir=mods, path_validator=validator)

    def test_constructor_explicito_raices_separadas_validas_y_autorizadas_aceptadas(
        self, tmp_path: pathlib.Path
    ) -> None:
        """P1-A1.5: roots separadas válidas y autorizadas siguen funcionando."""
        install = tmp_path / "Install"
        data = tmp_path / "Data"
        mods = tmp_path / "Mods"
        install.mkdir()
        data.mkdir()
        mods.mkdir()
        validator = PathValidator(roots=[install, data, mods])
        ctrl = MO2Controller(install_root=install, data_root=data, mods_dir=mods, path_validator=validator)
        assert ctrl.install_root == install.resolve()
        assert ctrl.data_root == data.resolve()
        assert ctrl.mods_dir == mods.resolve()

    def test_bootstrap_mo2_mods_path_con_ini_corrupto_falla_y_no_toca_install_traps(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-A2.A: MO2_MODS_PATH válido + ModOrganizer.ini corrupto aborta y no degrada a INSTALL."""
        from sky_claw.app.core.path_resolver import PathResolutionService
        from sky_claw.app_context import _construir_raices_sandbox

        install = tmp_path / "MO2_Install"
        install.mkdir()
        (install / "ModOrganizer.exe").write_bytes(b"fake-exe")
        (install / "ModOrganizer.ini").write_text("[Settings]\nbase_directory = relative_bad\n", encoding="utf-8")

        install_profiles = install / "profiles" / "Default"
        install_profiles.mkdir(parents=True)
        trap_modlist = install_profiles / "modlist.txt"
        trap_modlist.write_text("# INSTALL TRAP\n", encoding="utf-8")
        install_overwrite = install / "overwrite"
        install_overwrite.mkdir()
        trap_overwrite = install_overwrite / "trap.txt"
        trap_overwrite.write_text("trap", encoding="utf-8")

        custom_mods = tmp_path / "CustomMods"
        custom_mods.mkdir()
        monkeypatch.setenv("MO2_MODS_PATH", str(custom_mods))

        sandbox_roots = _construir_raices_sandbox(mo2_root=install, install_dir=None, skyrim_path=None)
        validator = PathValidator(roots=sandbox_roots)
        svc = PathResolutionService(path_validator=validator, profile_name="Default", mo2_install_dir=install)

        with pytest.raises(RuntimeError):
            svc.get_mo2_instance_data_root_estricto()

        assert trap_modlist.read_text(encoding="utf-8") == "# INSTALL TRAP\n"
        assert trap_overwrite.read_text(encoding="utf-8") == "trap"

    def test_bootstrap_base_directory_fuera_de_sandbox_falla_y_no_toca_install_traps(
        self, tmp_path: pathlib.Path
    ) -> None:
        """P1-A2.B: base_directory fuera de sandbox aborta y no degrada a INSTALL."""
        from sky_claw.app.core.path_resolver import PathResolutionService

        install = tmp_path / "MO2_Install"
        install.mkdir()
        (install / "ModOrganizer.exe").write_bytes(b"fake-exe")
        outside_data = tmp_path / "OutsideData"
        outside_data.mkdir()
        (install / "ModOrganizer.ini").write_text(
            f"[Settings]\nbase_directory = {outside_data}\n",
            encoding="utf-8",
        )

        install_profiles = install / "profiles" / "Default"
        install_profiles.mkdir(parents=True)
        trap_modlist = install_profiles / "modlist.txt"
        trap_modlist.write_text("# INSTALL TRAP\n", encoding="utf-8")
        install_overwrite = install / "overwrite"
        install_overwrite.mkdir()
        trap_overwrite = install_overwrite / "trap.txt"
        trap_overwrite.write_text("trap", encoding="utf-8")

        validator = PathValidator(roots=[install])
        svc = PathResolutionService(path_validator=validator, profile_name="Default", mo2_install_dir=install)

        with pytest.raises(RuntimeError, match="queda fuera de las raíces permitidas del sandbox"):
            svc.get_mo2_instance_data_root_estricto()

        assert trap_modlist.read_text(encoding="utf-8") == "# INSTALL TRAP\n"
        assert trap_overwrite.read_text(encoding="utf-8") == "trap"

    def test_bootstrap_portable_sin_metadata_install_data_iguales_funciona(self, tmp_path: pathlib.Path) -> None:
        """P1-A2.C: Layout portable sin metadata resuelve install == data y funciona normalmente."""
        from sky_claw.app.core.path_resolver import PathResolutionService

        install = tmp_path / "MO2_Portable"
        install.mkdir()
        (install / "ModOrganizer.exe").write_bytes(b"fake-exe")

        validator = PathValidator(roots=[install])
        svc = PathResolutionService(path_validator=validator, profile_name="Default", mo2_install_dir=install)

        resolved_install = svc.get_mo2_path()
        resolved_data = svc.get_mo2_instance_data_root_estricto()
        assert resolved_install == install.resolve()
        assert resolved_data == install.resolve()

    def test_sandbox_roots_no_autoriza_metadata_mods_shadowed_por_env(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-A3.1: MO2_MODS_PATH válido autoriza solo custom_a, no custom_b (shadowed)."""
        from sky_claw.app.core.path_resolver import PathResolutionService
        from sky_claw.app_context import _construir_raices_sandbox, _mods_candidatos_para_sandbox

        mo2_root = tmp_path / "MO2_Install"
        mo2_root.mkdir()
        (mo2_root / "ModOrganizer.exe").write_bytes(b"fake")

        custom_a = tmp_path / "CustomA_Env"
        custom_a.mkdir()
        custom_b = tmp_path / "CustomB_Meta"
        custom_b.mkdir()

        (mo2_root / "ModOrganizer.ini").write_text(
            f"[Settings]\nmod_directory = {custom_b}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MO2_MODS_PATH", str(custom_a))

        candidatos = _mods_candidatos_para_sandbox(mo2_root)
        assert custom_a.resolve() in candidatos
        assert custom_b.resolve() not in candidatos

        roots = _construir_raices_sandbox(mo2_root, None, None)
        validator = PathValidator(roots=roots)
        assert custom_a.resolve() in validator.roots
        assert custom_b.resolve() not in validator.roots

        svc = PathResolutionService(path_validator=validator, profile_name="Default", mo2_install_dir=mo2_root)
        assert svc.get_mo2_mods_path() == custom_a.resolve()

    def test_sandbox_roots_env_mods_invalido_no_autoriza_metadata_como_fallback(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-A3.2: MO2_MODS_PATH inválido no autoriza metadata como fallback silencioso y falla cerrado."""
        from sky_claw.app.core.path_resolver import PathResolutionService
        from sky_claw.app_context import _construir_raices_sandbox, _mods_candidatos_para_sandbox

        mo2_root = tmp_path / "MO2_Install"
        mo2_root.mkdir()
        (mo2_root / "ModOrganizer.exe").write_bytes(b"fake")

        custom_b = tmp_path / "CustomB_Meta"
        custom_b.mkdir()
        (mo2_root / "ModOrganizer.ini").write_text(
            f"[Settings]\nmod_directory = {custom_b}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MO2_MODS_PATH", "relativo_invalido")

        candidatos = _mods_candidatos_para_sandbox(mo2_root)
        assert custom_b.resolve() not in candidatos
        assert len(candidatos) == 0

        roots = _construir_raices_sandbox(mo2_root, None, None)
        validator = PathValidator(roots=roots)
        assert custom_b.resolve() not in validator.roots

        svc = PathResolutionService(path_validator=validator, profile_name="Default", mo2_install_dir=mo2_root)
        with pytest.raises(RuntimeError):
            svc.get_mo2_mods_path()

    def test_sandbox_roots_sin_env_autoriza_metadata_mods(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-A3.3: Sin MO2_MODS_PATH, metadata.mods custom se autoriza y resuelve con normalidad."""
        from sky_claw.app.core.path_resolver import PathResolutionService
        from sky_claw.app_context import _construir_raices_sandbox, _mods_candidatos_para_sandbox

        monkeypatch.delenv("MO2_MODS_PATH", raising=False)
        mo2_root = tmp_path / "MO2_Install"
        mo2_root.mkdir()
        (mo2_root / "ModOrganizer.exe").write_bytes(b"fake")

        custom_b = tmp_path / "CustomB_Meta"
        custom_b.mkdir()
        (mo2_root / "ModOrganizer.ini").write_text(
            f"[Settings]\nmod_directory = {custom_b}\n",
            encoding="utf-8",
        )

        candidatos = _mods_candidatos_para_sandbox(mo2_root)
        assert custom_b.resolve() in candidatos

        roots = _construir_raices_sandbox(mo2_root, None, None)
        validator = PathValidator(roots=roots)
        assert custom_b.resolve() in validator.roots

        svc = PathResolutionService(path_validator=validator, profile_name="Default", mo2_install_dir=mo2_root)
        assert svc.get_mo2_mods_path() == custom_b.resolve()
