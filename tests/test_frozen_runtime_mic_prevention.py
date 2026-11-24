"""Tests TDD (AAA) de prevención MIC en aprovisionamiento, verificación y binding de Runtime Clones.

Cubre:
- Low (0x1000) y Untrusted (0x0000) rechazados en ambos ejecutables (SkyrimSE.exe y skse64_loader.exe).
- Medium (0x2000) y superiores (High 0x3000, System 0x4000) aceptados.
- Ausencia de etiqueta MIC tratada como Medium por defecto en Windows; fuera de Windows
  conserva contratos multiplataforma sin fingir validación MIC ejecutada.
- Error Win32 o evidencia SID/ACE incompleta -> INDETERMINATE (nunca PASS y nunca degrada
  un Clone PROVISIONED a INVALID terminal irreversible).
- Archivo inexistente respeta los contratos actuales de provisión, verificación y preflight.
- Directorio padre (clone_parent) con etiqueta Low heredable -> bloqueo fail-closed ANTES
  de copiar archivos, informando ruta exacta, etiqueta detectada, riesgo de herencia y
  opción de otro destino seguro, sin redirigir ni crear directorios alternativos.
- Validación exacta de flags ACE de herencia NTFS (OI, CI, NP, IO, ID).
- Clone existente con directorio Low y ejecutables Medium (caso clone-p7-01) -> VALID.
- Generation bajo directorio Low (no ejecutable, SFR-19) -> NO rechazada indebidamente.
- Recuperación de Clone incompleto (PROVISIONING).
- Consumidores existentes (binder_preflight, autorizacion_hitl_transicion, oracle).
- Cero modificaciones de ACL/SACL por el verificador (medido en NTFS real + ancla AST).
"""

from __future__ import annotations

import ast
import ctypes
import pathlib
import shutil
import sys
from unittest.mock import MagicMock

import pytest

import sky_claw.local.frozen_runtime.clones as clones_mod
import sky_claw.local.frozen_runtime.provisioning as prov_mod
from sky_claw.local.frozen_runtime import (
    GenerationVerificationState,
    RuntimeCloneLifecycle,
    RuntimeSetupError,
    adoptar_copia_stock,
    construir_referencia_stock,
    crear_runtime_clone_desde_generation,
    initialize_frozen_runtime_storage,
    leer_runtime_clone_record,
    publicar_generation_desde_stock,
    verificar_generation,
    verificar_runtime_clone,
    verificar_runtime_setup,
)
from sky_claw.local.frozen_runtime.clones import (
    MicEvaluationResult,
    MicEvaluationStatus,
    evaluar_etiqueta_mic_archivo,
    evaluar_herencia_mic_clone_parent,
)
from sky_claw.local.frozen_runtime.storage import generation_dir

VERSION = "1.6.1170.0"

# ── Helpers Win32 de test (solo usados en tests Windows sobre tmp_path) ───

if sys.platform == "win32":
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _SE_FILE_OBJECT = 1
    _OWNER_INFO = 0x00000001
    _GROUP_INFO = 0x00000002
    _DACL_INFO = 0x00000004
    _LABEL_INFO = 0x00000010
    _ALL_NON_AUDIT = _OWNER_INFO | _GROUP_INFO | _DACL_INFO | _LABEL_INFO

    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    _advapi32.GetSecurityDescriptorSacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorSacl.restype = wintypes.BOOL

    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD

    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL

    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL

    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL

    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _sid_usuario_actual_win32() -> str:
    htok = wintypes.HANDLE()
    assert _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(htok))
    try:
        needed = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(htok, 1, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(needed.value)
        assert _advapi32.GetTokenInformation(htok, 1, buf, needed.value, ctypes.byref(needed))
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        str_ptr = wintypes.LPWSTR()
        assert _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr))
        try:
            assert str_ptr.value
            return str_ptr.value
        finally:
            if str_ptr:
                _kernel32.LocalFree(str_ptr)
    finally:
        _kernel32.CloseHandle(htok)


def _aplicar_sacl_sddl_en_tmp(ruta: pathlib.Path, sacl_sddl: str) -> None:
    """Aplica una etiqueta MIC SDDL sobre un archivo/directorio descartable en tmp_path.

    En unidades secundarias (E:, D:\\a en CI) donde Authenticated Users tiene Modify
    sin WRITE_OWNER, el propietario del temporal habilita temporalmente FA sobre su
    propio nodo (vía WRITE_DAC), escribe la etiqueta MIC y restaura el DACL previo.
    """
    assert sys.platform == "win32"
    dacl_antes = ctypes.c_void_p()
    sd_antes = ctypes.c_void_p()
    rc_get = _advapi32.GetNamedSecurityInfoW(
        str(ruta), _SE_FILE_OBJECT, _DACL_INFO, None, None, ctypes.byref(dacl_antes), None, ctypes.byref(sd_antes)
    )
    assert rc_get == 0, f"GetNamedSecurityInfoW(DACL) falló con rc={rc_get}"

    sid_usr = _sid_usuario_actual_win32()
    sd_temp_dacl = ctypes.c_void_p()
    assert _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:(A;;FA;;;{sid_usr})(A;;FA;;;SY)(A;;FA;;;BA)", 1, ctypes.byref(sd_temp_dacl), None
    )
    sd = ctypes.c_void_p()
    ok = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sacl_sddl, 1, ctypes.byref(sd), None)
    assert ok, f"SDDL inválido {sacl_sddl!r}: err={ctypes.get_last_error()}"
    try:
        p_d, d_d, dacl_tmp = wintypes.BOOL(False), wintypes.BOOL(False), ctypes.c_void_p()
        assert _advapi32.GetSecurityDescriptorDacl(
            sd_temp_dacl, ctypes.byref(p_d), ctypes.byref(dacl_tmp), ctypes.byref(d_d)
        )
        rc_dacl = _advapi32.SetNamedSecurityInfoW(str(ruta), _SE_FILE_OBJECT, _DACL_INFO, None, None, dacl_tmp, None)
        assert rc_dacl == 0, f"SetNamedSecurityInfoW(temp DACL) falló con rc={rc_dacl}"
        try:
            present = wintypes.BOOL(False)
            defaulted = wintypes.BOOL(False)
            sacl = ctypes.c_void_p()
            assert _advapi32.GetSecurityDescriptorSacl(
                sd, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)
            )
            rc = _advapi32.SetNamedSecurityInfoW(
                str(ruta),
                _SE_FILE_OBJECT,
                _LABEL_INFO,
                None,
                None,
                None,
                sacl if present.value else None,
            )
            assert rc == 0, f"SetNamedSecurityInfoW({ruta}, {sacl_sddl!r}) falló con rc={rc}"
        finally:
            _advapi32.SetNamedSecurityInfoW(str(ruta), _SE_FILE_OBJECT, _DACL_INFO, None, None, dacl_antes, None)
    finally:
        if sd:
            _kernel32.LocalFree(sd)
        if sd_temp_dacl:
            _kernel32.LocalFree(sd_temp_dacl)
        if sd_antes:
            _kernel32.LocalFree(sd_antes)


def _aplicar_sacl_arbol_low_en_tmp(raiz: pathlib.Path) -> None:
    """Simula en tmp_path la propagación recursiva Low (tipo icacls /T / sandbox DSH)."""
    _aplicar_sacl_sddl_en_tmp(raiz, "S:AI(ML;OICI;NW;;;LW)")
    for hijo in raiz.rglob("*"):
        if hijo.is_dir():
            _aplicar_sacl_sddl_en_tmp(hijo, "S:AI(ML;OICIID;NW;;;LW)")
        else:
            _aplicar_sacl_sddl_en_tmp(hijo, "S:AI(ML;ID;NW;;;LW)")


def _leer_sddl_completo_en_tmp(ruta: pathlib.Path) -> str:
    """Lee SDDL (Owner+Group+DACL+Label) para demostrar cero mutaciones del verificador."""
    assert sys.platform == "win32"
    sd = ctypes.c_void_p()
    rc = _advapi32.GetNamedSecurityInfoW(
        str(ruta), _SE_FILE_OBJECT, _ALL_NON_AUDIT, None, None, None, None, ctypes.byref(sd)
    )
    assert rc == 0, f"GetNamedSecurityInfoW({ruta}) falló con rc={rc}"
    try:
        ptr = wintypes.LPWSTR()
        ln = wintypes.ULONG(0)
        assert _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            sd, 1, _ALL_NON_AUDIT, ctypes.byref(ptr), ctypes.byref(ln)
        )
        try:
            return ptr.value or ""
        finally:
            if ptr:
                _kernel32.LocalFree(ptr)
    finally:
        if sd:
            _kernel32.LocalFree(sd)


def _escribir_arbol_runtime(raiz: pathlib.Path) -> pathlib.Path:
    (raiz / "Data" / "EmptyFolder").mkdir(parents=True)
    (raiz / "SkyrimSE.exe").write_bytes(b"exe-sintetico-1170")
    (raiz / "steam_api64.dll").write_bytes(b"steam-api-sintetica")
    (raiz / "skse64_loader.exe").write_bytes(b"skse-loader-sintetico")
    (raiz / "skse64_1_6_1170.dll").write_bytes(b"skse-dll-sintetico")
    (raiz / "SkyrimSELauncher.exe").write_bytes(b"launcher-opcional-sintetico")
    (raiz / "Data" / "Skyrim.esm").write_bytes(b"esm-payload")
    return raiz


@pytest.fixture
def parche_identidad_pe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fija la identidad PE sintética para árboles de prueba en tmp_path."""
    from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation

    def _identidad(raiz: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version=VERSION,
            observed_exe_path=str(pathlib.Path(raiz) / "SkyrimSE.exe"),
            observed_at_ns=1,
        )

    monkeypatch.setattr("sky_claw.local.frozen_runtime.provisioning.observe_runtime_identity_from_root", _identidad)
    monkeypatch.setattr("sky_claw.local.frozen_runtime.clones.observe_runtime_identity_from_root", _identidad)
    monkeypatch.setattr("sky_claw.local.frozen_runtime.generations.observe_runtime_identity_from_root", _identidad)
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.effective_runtime.compatibility.observe_runtime_identity_from_root",
        _identidad,
    )


@pytest.fixture
def entorno_p7(
    tmp_path: pathlib.Path, parche_identidad_pe: None
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, str]:
    """Prepara storage inicializado, Generation publicada y directorio clone_parent limpio."""
    base = _escribir_arbol_runtime(tmp_path / "stock-base")
    referencia = construir_referencia_stock(base)
    fuente = pathlib.Path(shutil.copytree(base, tmp_path / "stock-copia"))
    storage = tmp_path / "storage"
    assert initialize_frozen_runtime_storage(storage).success
    copia = adoptar_copia_stock(fuente, referencia, storage_root=storage)
    pub = publicar_generation_desde_stock(storage, copia)
    assert pub.success and pub.generation_id is not None
    clones_dir = tmp_path / "clones-limpios"
    clones_dir.mkdir()
    return storage, fuente, clones_dir, pub.generation_id


# ── 1. Clasificación unitaria de niveles MIC, flags ACE y plataformas ──────


class TestClasificacionMicUnitaria:
    @pytest.mark.parametrize(
        ("rid", "esperado"),
        [
            (0x0000, MicEvaluationStatus.INCOMPATIBLE_LOW),  # Untrusted
            (0x1000, MicEvaluationStatus.INCOMPATIBLE_LOW),  # Low
            (0x1FFF, MicEvaluationStatus.INCOMPATIBLE_LOW),  # Sub-Medium custom
            (0x2000, MicEvaluationStatus.VERIFIED_COMPATIBLE),  # Medium
            (0x2100, MicEvaluationStatus.VERIFIED_COMPATIBLE),  # MediumPlus
            (0x3000, MicEvaluationStatus.VERIFIED_COMPATIBLE),  # High
            (0x4000, MicEvaluationStatus.VERIFIED_COMPATIBLE),  # System
        ],
    )
    def test_clasifica_niveles_de_integridad_en_archivo(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, rid: int, esperado: MicEvaluationStatus
    ) -> None:
        # Arrange
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"pe")
        monkeypatch.setattr(
            clones_mod,
            "_leer_aces_etiqueta_mic",
            lambda _p: (
                clones_mod._EstadoLecturaMic.OK,
                (clones_mod._MicAceObservada(rid=rid, ace_flags=0x0, mask=0x1),),
                "",
            ),
        )

        # Act
        res = evaluar_etiqueta_mic_archivo(exe)

        # Assert
        assert res.status is esperado
        assert res.validated_on_windows is True
        assert res.effective_rid == rid

    def test_ausencia_de_etiqueta_en_windows_se_trata_como_medium_por_defecto(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"pe")
        monkeypatch.setattr(
            clones_mod,
            "_leer_aces_etiqueta_mic",
            lambda _p: (clones_mod._EstadoLecturaMic.OK, (), ""),
        )

        # Act
        res = evaluar_etiqueta_mic_archivo(exe)

        # Assert
        assert res.status is MicEvaluationStatus.UNLABELED_DEFAULT_MEDIUM
        assert res.is_compatible is True
        assert res.validated_on_windows is True
        assert res.effective_rid == 0x2000

    def test_ace_inherit_only_en_archivo_no_cuenta_como_etiqueta_efectiva(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: INHERIT_ONLY_ACE = 0x08 no aplica al objeto en sí
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"pe")
        monkeypatch.setattr(
            clones_mod,
            "_leer_aces_etiqueta_mic",
            lambda _p: (
                clones_mod._EstadoLecturaMic.OK,
                (clones_mod._MicAceObservada(rid=0x1000, ace_flags=0x08, mask=0x1),),
                "",
            ),
        )

        # Act
        res = evaluar_etiqueta_mic_archivo(exe)

        # Assert
        assert res.status is MicEvaluationStatus.UNLABELED_DEFAULT_MEDIUM
        assert res.is_compatible is True

    @pytest.mark.parametrize(
        ("ace_flags", "espera_bloqueo"),
        [
            (0x03, True),  # OI | CI -> propaga a nietos ejecutables
            (0x13, True),  # OI | CI | ID (caso exacto G:\Modding\FrozenClones)
            (0x01, True),  # OI sin NP -> NTFS propaga como IO a subdirectorios y aplica a archivos nietos
            (0x09, True),  # OI | IO sin NP -> propaga a archivos descendientes
            (0x05, False),  # OI | NP -> se consume en hijos directos, NO llega a <clone_id>/<exe>
            (0x02, False),  # CI sin OI -> aplica solo a contenedores, NO a archivos .exe
            (0x00, False),  # sin OI ni CI -> no heredable
        ],
    )
    def test_herencia_en_clone_parent_evalua_flags_ace_ntfs_correctamente(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        ace_flags: int,
        espera_bloqueo: bool,
    ) -> None:
        # Arrange
        padre = tmp_path / "FrozenClones"
        padre.mkdir()
        monkeypatch.setattr(
            clones_mod,
            "_leer_aces_etiqueta_mic",
            lambda _p: (
                clones_mod._EstadoLecturaMic.OK,
                (clones_mod._MicAceObservada(rid=0x1000, ace_flags=ace_flags, mask=0x1),),
                "",
            ),
        )

        # Act
        res = evaluar_herencia_mic_clone_parent(padre)

        # Assert
        if espera_bloqueo:
            assert res.status is MicEvaluationStatus.INCOMPATIBLE_LOW
            assert res.is_compatible is False
            assert str(padre) in res.detail
            assert "low" in res.detail.lower()
        else:
            assert res.is_compatible is True

    def test_no_windows_conserva_compatibilidad_sin_declarar_validacion_mic_ejecutada(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"pe")
        monkeypatch.setattr(
            clones_mod,
            "_leer_aces_etiqueta_mic",
            lambda _p: (
                clones_mod._EstadoLecturaMic.NO_WINDOWS,
                (),
                "inspección MIC Win32 no aplicable fuera de Windows",
            ),
        )

        # Act
        res = evaluar_etiqueta_mic_archivo(exe)

        # Assert
        assert res.status is MicEvaluationStatus.NOT_APPLICABLE_NON_WINDOWS
        assert res.is_compatible is True
        assert res.validated_on_windows is False

    def test_error_win32_o_evidencia_incompleta_devuelve_indeterminate_nunca_pass(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"pe")
        monkeypatch.setattr(
            clones_mod,
            "_leer_aces_etiqueta_mic",
            lambda _p: (
                clones_mod._EstadoLecturaMic.INDETERMINATE,
                (),
                "GetNamedSecurityInfoW falló con código Win32 1359",
            ),
        )

        # Act
        res_archivo = evaluar_etiqueta_mic_archivo(exe)
        res_padre = evaluar_herencia_mic_clone_parent(tmp_path)

        # Assert
        assert res_archivo.status is MicEvaluationStatus.INDETERMINATE
        assert res_archivo.is_compatible is False
        assert res_padre.status is MicEvaluationStatus.INDETERMINATE
        assert res_padre.is_compatible is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Prueba de validación Win32 ctypes y LocalFree")
    @pytest.mark.parametrize(
        "escenario",
        [
            "get_named_error",
            "sacl_get_false",
            "ace_truncada",
            "sid_invalido",
            "autoridad_ajena",
            "subauth_count_invalido",
        ],
    )
    def test_lector_win32_valida_sid_autoridad_ace_y_libera_localfree_siempre(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, escenario: str
    ) -> None:
        # Arrange: asegurar inicialización de APIs Win32 y espiar LocalFree para verificar ausencia de leaks
        exe = tmp_path / "probe.exe"
        exe.write_bytes(b"MZ")
        clones_mod._asegurar_bindings_mic_win32()
        assert clones_mod._advapi32_mic is not None and clones_mod._kernel32_mic is not None

        liberados: list[int] = []
        sentinel_sd = 0xDEADBEEF

        def _fake_local_free(ptr: object) -> int:
            val = getattr(ptr, "value", ptr)
            if val:
                liberados.append(int(val))  # type: ignore[arg-type]
            return 0

        monkeypatch.setattr(clones_mod._kernel32_mic, "LocalFree", _fake_local_free)

        if escenario == "get_named_error":
            monkeypatch.setattr(
                clones_mod._advapi32_mic,
                "GetNamedSecurityInfoW",
                lambda *_a: 5,  # ERROR_ACCESS_DENIED (sin alocar SD)
            )
            estado, _aces, detalle = clones_mod._leer_aces_etiqueta_mic(exe)
            assert estado is clones_mod._EstadoLecturaMic.INDETERMINATE
            assert "5" in detalle
            return

        def _fake_get_named(
            _p: object, _ot: int, _si: int, _o: object, _g: object, _d: object, sacl_out: object, sd_out: object
        ) -> int:
            ctypes.cast(sd_out, ctypes.POINTER(ctypes.c_void_p))[0] = sentinel_sd  # type: ignore[arg-type]
            ctypes.cast(sacl_out, ctypes.POINTER(ctypes.c_void_p))[0] = 0xCAFE  # type: ignore[arg-type]
            return 0

        monkeypatch.setattr(clones_mod._advapi32_mic, "GetNamedSecurityInfoW", _fake_get_named)

        if escenario == "sacl_get_false":
            monkeypatch.setattr(clones_mod._advapi32_mic, "GetSecurityDescriptorSacl", lambda *_a: 0)
        else:

            def _fake_get_sacl(_sd: object, pres: object, sacl_p: object, _def: object) -> int:
                ctypes.cast(pres, ctypes.POINTER(wintypes.BOOL))[0] = 1  # type: ignore[arg-type]
                ctypes.cast(sacl_p, ctypes.POINTER(ctypes.c_void_p))[0] = 0xCAFE  # type: ignore[arg-type]
                return 1

            def _fake_acl_info(_acl: object, info_ptr: object, _sz: int, _cls: int) -> int:
                info = ctypes.cast(info_ptr, ctypes.POINTER(clones_mod._AclSizeInfo)).contents  # type: ignore[arg-type]
                info.AceCount = 1
                return 1

            # Construimos en memoria un buffer de ACE sintético seguro
            ace_size = 8 if escenario == "ace_truncada" else 20
            raw_ace = (ctypes.c_ubyte * 32)()
            raw_ace[0] = 0x11  # SYSTEM_MANDATORY_LABEL_ACE_TYPE
            raw_ace[1] = 0x00
            raw_ace[2] = ace_size & 0xFF
            raw_ace[3] = (ace_size >> 8) & 0xFF

            def _fake_get_ace(_acl: object, _idx: int, ace_out: object) -> int:
                ctypes.cast(ace_out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(raw_ace)  # type: ignore[arg-type]
                return 1

            monkeypatch.setattr(clones_mod._advapi32_mic, "GetSecurityDescriptorSacl", _fake_get_sacl)
            monkeypatch.setattr(clones_mod._advapi32_mic, "GetAclInformation", _fake_acl_info)
            monkeypatch.setattr(clones_mod._advapi32_mic, "GetAce", _fake_get_ace)

            if escenario == "sid_invalido":
                monkeypatch.setattr(clones_mod._advapi32_mic, "IsValidSid", lambda _s: 0)
            elif escenario == "autoridad_ajena":
                bad_auth = clones_mod._SidIdentifierAuthority((0, 0, 0, 0, 0, 5))  # NT AUTHORITY en vez de 16
                monkeypatch.setattr(clones_mod._advapi32_mic, "IsValidSid", lambda _s: 1)
                monkeypatch.setattr(
                    clones_mod._advapi32_mic, "GetSidIdentifierAuthority", lambda _s: ctypes.pointer(bad_auth)
                )
            elif escenario == "subauth_count_invalido":
                ok_auth = clones_mod._SidIdentifierAuthority((0, 0, 0, 0, 0, 16))
                cnt = ctypes.c_ubyte(2)
                monkeypatch.setattr(clones_mod._advapi32_mic, "IsValidSid", lambda _s: 1)
                monkeypatch.setattr(
                    clones_mod._advapi32_mic, "GetSidIdentifierAuthority", lambda _s: ctypes.pointer(ok_auth)
                )
                monkeypatch.setattr(clones_mod._advapi32_mic, "GetSidSubAuthorityCount", lambda _s: ctypes.pointer(cnt))

        # Act
        res = evaluar_etiqueta_mic_archivo(exe)

        # Assert: evidencia incompleta/corrupta -> INDETERMINATE y LocalFree invocado sobre sentinel_sd
        assert res.status is MicEvaluationStatus.INDETERMINATE
        assert liberados == [sentinel_sd], "debe liberar el SecurityDescriptor Win32 con LocalFree en finally"


# ── 2. Integración en provisión, recuperación, verificación y consumidores ─


class TestPrevencionMicEnFlujosDeClone:
    def test_directorio_padre_low_bloquea_antes_de_copiar_y_no_crea_alternativos(
        self, entorno_p7: tuple[pathlib.Path, pathlib.Path, pathlib.Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        storage, _fuente, clones_dir, gid = entorno_p7
        espia_copia = MagicMock(wraps=prov_mod.copiar_arbol_independiente)
        monkeypatch.setattr(prov_mod, "copiar_arbol_independiente", espia_copia)
        monkeypatch.setattr(
            prov_mod,
            "evaluar_herencia_mic_clone_parent",
            lambda p: MicEvaluationResult(
                status=MicEvaluationStatus.INCOMPATIBLE_LOW,
                effective_rid=0x1000,
                validated_on_windows=True,
                detail=(
                    f"el directorio destino '{p}' tiene etiqueta MIC heredable Low Mandatory Level (S-1-16-4096); "
                    "los ejecutables del futuro Clone heredarían Low; elija otro destino seguro"
                ),
            ),
        )
        hijos_previos = set(clones_dir.parent.iterdir())

        # Act
        res = crear_runtime_clone_desde_generation(storage, gid, clones_dir, "clone-bloqueado")

        # Assert
        assert res.success is False
        assert espia_copia.call_count == 0, "no debía iniciar la copia de archivos"
        assert not (clones_dir / "clone-bloqueado").exists()
        assert set(clones_dir.parent.iterdir()) == hijos_previos, "no debe crear directorios alternativos"
        assert str(clones_dir) in res.message
        assert "low" in res.message.lower()
        assert "hered" in res.message.lower()
        assert "destino" in res.message.lower()

    @pytest.mark.parametrize("ejecutable_afectado", ["SkyrimSE.exe", "skse64_loader.exe"])
    @pytest.mark.parametrize("rid_malo", [0x1000, 0x0000], ids=["Low", "Untrusted"])
    def test_rechaza_ejecutable_low_o_untrusted_en_setup_y_verificar_runtime_clone(
        self,
        entorno_p7: tuple[pathlib.Path, pathlib.Path, pathlib.Path, str],
        monkeypatch: pytest.MonkeyPatch,
        ejecutable_afectado: str,
        rid_malo: int,
    ) -> None:
        # Arrange: creamos un Clone válido y luego simulamos etiqueta Low/Untrusted en UNO de los hermanos
        storage, _fuente, clones_dir, gid = entorno_p7
        creado = crear_runtime_clone_desde_generation(storage, gid, clones_dir, "clone-hermano")
        assert creado.success and creado.clone_root is not None

        def _evaluar_selectivo(ruta: pathlib.Path) -> MicEvaluationResult:
            if ruta.name.casefold() == ejecutable_afectado.casefold():
                return MicEvaluationResult(
                    status=MicEvaluationStatus.INCOMPATIBLE_LOW,
                    effective_rid=rid_malo,
                    validated_on_windows=True,
                    detail=f"etiqueta MIC incompatible en '{ruta}' (S-1-16-{rid_malo})",
                )
            return MicEvaluationResult(
                status=MicEvaluationStatus.VERIFIED_COMPATIBLE,
                effective_rid=0x2000,
                validated_on_windows=True,
                detail="Medium (S-1-16-8192)",
            )

        monkeypatch.setattr(clones_mod, "evaluar_etiqueta_mic_archivo", _evaluar_selectivo)
        monkeypatch.setattr(prov_mod, "evaluar_etiqueta_mic_archivo", _evaluar_selectivo)

        # Act & Assert 1: verificar_runtime_setup rechaza
        with pytest.raises(RuntimeSetupError, match=ejecutable_afectado):
            verificar_runtime_setup(creado.clone_root)

        # Act & Assert 2: verificar_runtime_clone devuelve INVALID
        verif = verificar_runtime_clone(storage, "clone-hermano")
        assert verif.state is GenerationVerificationState.INVALID
        assert ejecutable_afectado in verif.message

    def test_error_win32_mic_produce_indeterminate_sin_quemar_clone_provisioned_a_invalid(
        self, entorno_p7: tuple[pathlib.Path, pathlib.Path, pathlib.Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        storage, _fuente, clones_dir, gid = entorno_p7
        creado = crear_runtime_clone_desde_generation(storage, gid, clones_dir, "clone-indet-mic")
        assert creado.success

        monkeypatch.setattr(
            clones_mod,
            "evaluar_etiqueta_mic_archivo",
            lambda p: MicEvaluationResult(
                status=MicEvaluationStatus.INDETERMINATE,
                effective_rid=None,
                validated_on_windows=True,
                detail=f"fallo transitorio Win32 al leer etiqueta MIC en '{p}'",
            ),
        )
        monkeypatch.setattr(prov_mod, "evaluar_etiqueta_mic_archivo", clones_mod.evaluar_etiqueta_mic_archivo)

        # Act
        verif = verificar_runtime_clone(storage, "clone-indet-mic")
        reintento_idemp = crear_runtime_clone_desde_generation(storage, gid, clones_dir, "clone-indet-mic")

        # Assert: NUNCA PASS y NUNCA degrada el registro a INVALID terminal
        assert verif.state is GenerationVerificationState.INDETERMINATE
        assert reintento_idemp.success is False
        registro = leer_runtime_clone_record(storage, "clone-indet-mic")
        assert registro.lifecycle is RuntimeCloneLifecycle.PROVISIONED

    def test_recuperacion_de_clone_en_provisioning_bloquea_si_ejecutable_es_low_y_completa_al_corregir(
        self, entorno_p7: tuple[pathlib.Path, pathlib.Path, pathlib.Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: interrumpir justo antes de actualizar a PROVISIONED
        storage, _fuente, clones_dir, gid = entorno_p7
        original_act = prov_mod._actualizar_lifecycle_clone_bajo_lock
        cortado = False

        def _fallar_una_vez(raiz: pathlib.Path, cid: str, nuevo: RuntimeCloneLifecycle, **kw: object):  # type: ignore[no-untyped-def]
            nonlocal cortado
            if nuevo is RuntimeCloneLifecycle.PROVISIONED and not cortado:
                cortado = True
                raise RuntimeSetupError("corte simulado antes de PROVISIONED")
            return original_act(raiz, cid, nuevo, **kw)

        monkeypatch.setattr(prov_mod, "_actualizar_lifecycle_clone_bajo_lock", _fallar_una_vez)
        primero = crear_runtime_clone_desde_generation(storage, gid, clones_dir, "clone-rec-mic")
        assert primero.success is False
        assert leer_runtime_clone_record(storage, "clone-rec-mic").lifecycle is RuntimeCloneLifecycle.PROVISIONING
        monkeypatch.setattr(prov_mod, "_actualizar_lifecycle_clone_bajo_lock", original_act)

        # Act 1: mientras skse64_loader.exe tenga Low, la recuperación NO debe publicar PROVISIONED ni quemar a INVALID
        monkeypatch.setattr(
            prov_mod,
            "evaluar_etiqueta_mic_archivo",
            lambda p: MicEvaluationResult(
                status=MicEvaluationStatus.INCOMPATIBLE_LOW
                if p.name == "skse64_loader.exe"
                else MicEvaluationStatus.VERIFIED_COMPATIBLE,
                effective_rid=0x1000 if p.name == "skse64_loader.exe" else 0x2000,
                validated_on_windows=True,
                detail=f"Low en {p}",
            ),
        )
        intento_low = crear_runtime_clone_desde_generation(storage, gid, clones_dir, "clone-rec-mic")
        assert intento_low.success is False
        assert leer_runtime_clone_record(storage, "clone-rec-mic").lifecycle is RuntimeCloneLifecycle.PROVISIONING

        # Act 2: cuando ambos ejecutables son Medium, incluso si clone_parent tuviera Low, completa a PROVISIONED
        monkeypatch.setattr(
            prov_mod,
            "evaluar_herencia_mic_clone_parent",
            lambda p: MicEvaluationResult(
                status=MicEvaluationStatus.INCOMPATIBLE_LOW,
                effective_rid=0x1000,
                validated_on_windows=True,
                detail=f"padre Low {p}",
            ),
        )
        monkeypatch.setattr(
            prov_mod,
            "evaluar_etiqueta_mic_archivo",
            lambda _p: MicEvaluationResult(
                status=MicEvaluationStatus.VERIFIED_COMPATIBLE,
                effective_rid=0x2000,
                validated_on_windows=True,
                detail="Medium",
            ),
        )
        monkeypatch.setattr(clones_mod, "evaluar_etiqueta_mic_archivo", prov_mod.evaluar_etiqueta_mic_archivo)

        recuperado = crear_runtime_clone_desde_generation(storage, gid, clones_dir, "clone-rec-mic")

        # Assert
        assert recuperado.success is True, recuperado.message
        assert leer_runtime_clone_record(storage, "clone-rec-mic").lifecycle is RuntimeCloneLifecycle.PROVISIONED

    def test_consumidores_oracle_preflight_y_hitl_respetan_verificacion_mic_del_clone(
        self, entorno_p7: tuple[pathlib.Path, pathlib.Path, pathlib.Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import (
            EvidenciaTransicionError,
            derivar_evidencia_transicion,
        )
        from sky_claw.local.frozen_runtime.binder_preflight import (
            PreflightCheckCode,
            PreflightCheckStatus,
            evaluar_binder_preflight,
        )
        from sky_claw.local.frozen_runtime.effective_runtime.oracle import desired_runtime_from_storage
        from sky_claw.local.frozen_runtime.state import write_json_atomic
        from sky_claw.local.frozen_runtime.storage import active_state_path

        storage, fuente, clones_dir, gid = entorno_p7
        creado = crear_runtime_clone_desde_generation(storage, gid, clones_dir, "clone-consumidores")
        assert creado.success and creado.clone_root is not None

        write_json_atomic(
            active_state_path(storage),
            {
                "schema_version": 2,
                "desired_generation_id": gid,
                "desired_clone_id": "clone-consumidores",
                "updated_at_ns": 1,
            },
        )
        mo2_dir = storage.parent / "mo2-inst"
        mo2_dir.mkdir()
        (mo2_dir / "ModOrganizer.ini").write_text(
            f"[General]\ngameName=Skyrim Special Edition\ngamePath={creado.clone_root.as_posix()}\n",
            encoding="utf-8",
        )
        cfg_toml = storage.parent / "config.toml"
        cfg_toml.write_text(
            f'skyrim_path = "{creado.clone_root.as_posix()}"\n',
            encoding="utf-8",
        )

        # Control previo: con MIC compatible, oracle resuelve clone_root
        assert desired_runtime_from_storage(storage).clone_root == creado.clone_root

        # Simular degradación a Low en SkyrimSE.exe del Clone ya PROVISIONED
        monkeypatch.setattr(
            clones_mod,
            "evaluar_etiqueta_mic_archivo",
            lambda p: MicEvaluationResult(
                status=MicEvaluationStatus.INCOMPATIBLE_LOW,
                effective_rid=0x1000,
                validated_on_windows=True,
                detail=f"Low detectado en '{p}'",
            ),
        )

        # Act
        desired = desired_runtime_from_storage(storage)
        preflight = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2_dir,
            clone_id="clone-consumidores",
            managed_source_root=fuente,
        )

        # Assert: oracle no admite clone_root, preflight falla CLONE_PHYSICAL_INTEGRITY y HITL rechaza
        assert desired.clone_root is None
        chk = next(c for c in preflight.checks if c.check is PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY)
        assert chk.status is PreflightCheckStatus.FAIL
        assert "low" in chk.detail.lower()
        with pytest.raises(EvidenciaTransicionError):
            derivar_evidencia_transicion(
                storage_root=storage,
                mo2_instance_dir=mo2_dir,
                clone_id="clone-consumidores",
                managed_source_root=fuente,
                config_path=cfg_toml,
            )


# ── 3. Integración real Windows NTFS con archivos descartables en tmp_path ─


@pytest.mark.skipif(sys.platform != "win32", reason="Integración real NTFS MIC exclusiva de Windows")
class TestIntegracionWindowsRealMic:
    def test_ntfs_real_bloquea_padre_low_permite_generation_low_y_acepta_clone_reparado(
        self, tmp_path: pathlib.Path, parche_identidad_pe: None
    ) -> None:
        # Arrange: poner storage (y por ende Generation) dentro de un árbol Low real para probar SFR-19
        zona_low = tmp_path / "zona_dsh_low"
        zona_low.mkdir()
        _aplicar_sacl_sddl_en_tmp(zona_low, "S:AI(ML;OICI;NW;;;LW)")

        base = _escribir_arbol_runtime(tmp_path / "stock-limpio")
        ref = construir_referencia_stock(base)
        fuente = pathlib.Path(shutil.copytree(base, zona_low / "stock-bajo-low"))

        storage_low = zona_low / "FrozenRuntime"
        assert initialize_frozen_runtime_storage(storage_low).success
        copia = adoptar_copia_stock(fuente, ref, storage_root=storage_low)

        # Act & Assert 1: Generation bajo directorio Low NO es rechazada (nunca se ejecuta, SFR-19)
        pub = publicar_generation_desde_stock(storage_low, copia)
        assert pub.success is True, pub.message
        assert pub.generation_id is not None
        assert verificar_generation(storage_low, pub.generation_id).state is GenerationVerificationState.VALID
        gen_exe = generation_dir(storage_low, pub.generation_id) / "SkyrimSE.exe"
        assert evaluar_etiqueta_mic_archivo(gen_exe).status is MicEvaluationStatus.INCOMPATIBLE_LOW

        # Act & Assert 2: intentar crear Clone nuevo dentro de padre Low heredable -> bloquea ANTES de copiar
        clones_low = zona_low / "FrozenClones"
        clones_low.mkdir()
        sddl_padre_antes = _leer_sddl_completo_en_tmp(clones_low)
        rechazado = crear_runtime_clone_desde_generation(storage_low, pub.generation_id, clones_low, "clone-nuevo-low")
        assert rechazado.success is False
        assert not (clones_low / "clone-nuevo-low").exists()
        assert _leer_sddl_completo_en_tmp(clones_low) == sddl_padre_antes, "cero mutación de ACL/SACL sobre el padre"

        # Act & Assert 3: crear Clone en destino limpio (Medium por defecto sin etiqueta) -> PASS
        clones_limpios = tmp_path / "FrozenClonesLimpio"
        clones_limpios.mkdir()
        creado = crear_runtime_clone_desde_generation(storage_low, pub.generation_id, clones_limpios, "clone-ok")
        assert creado.success is True, creado.message
        assert verificar_runtime_clone(storage_low, "clone-ok").state is GenerationVerificationState.VALID

        # Act & Assert 4: réplica exacta de clone-p7-01 (directorio Low + ejecutables opcionales Low + 2 exes Medium)
        assert creado.clone_root is not None
        _aplicar_sacl_arbol_low_en_tmp(creado.clone_root)
        # En este instante todos los hijos tienen Low heredado -> verificar_runtime_clone DEBE rechazarlo
        assert verificar_runtime_clone(storage_low, "clone-ok").state is GenerationVerificationState.INVALID

        # Reparar SOLO los 2 ejecutables del arranque a Medium (dejando directorio y SkyrimSELauncher.exe en Low)
        _aplicar_sacl_sddl_en_tmp(creado.clone_root / "SkyrimSE.exe", "S:(ML;;NW;;;ME)")  # type: ignore[operator]
        assert verificar_runtime_clone(storage_low, "clone-ok").state is GenerationVerificationState.INVALID, (
            "debe seguir fallando mientras skse64_loader.exe permanezca en Low"
        )

        _aplicar_sacl_sddl_en_tmp(creado.clone_root / "skse64_loader.exe", "S:(ML;;NW;;;ME)")  # type: ignore[operator]
        sddl_exe_antes = _leer_sddl_completo_en_tmp(creado.clone_root / "SkyrimSE.exe")  # type: ignore[operator]
        sddl_launcher_low = _leer_sddl_completo_en_tmp(
            creado.clone_root / "SkyrimSELauncher.exe"  # type: ignore[operator]
        )

        verif_reparado = verificar_runtime_clone(storage_low, "clone-ok")
        idempotente = crear_runtime_clone_desde_generation(storage_low, pub.generation_id, clones_limpios, "clone-ok")

        assert verif_reparado.state is GenerationVerificationState.VALID, verif_reparado.message
        assert idempotente.success is True, idempotente.message
        assert _leer_sddl_completo_en_tmp(creado.clone_root / "SkyrimSE.exe") == sddl_exe_antes  # type: ignore[operator]
        assert _leer_sddl_completo_en_tmp(creado.clone_root / "SkyrimSELauncher.exe") == sddl_launcher_low  # type: ignore[operator]

    def test_ntfs_real_untrusted_rechazado_y_high_aceptado(self, tmp_path: pathlib.Path) -> None:
        # Arrange
        f_untrusted = tmp_path / "untrusted.exe"
        f_high_or_med = tmp_path / "medium_explicito.exe"
        f_sin_label = tmp_path / "sin_label.exe"
        for f in (f_untrusted, f_high_or_med, f_sin_label):
            f.write_bytes(b"MZ")

        # S-1-16-0 = Untrusted; S-1-16-8192 = Medium explícito
        _aplicar_sacl_sddl_en_tmp(f_untrusted, "S:(ML;;NW;;;S-1-16-0)")
        _aplicar_sacl_sddl_en_tmp(f_high_or_med, "S:(ML;;NW;;;ME)")

        # Act
        r_untrusted = evaluar_etiqueta_mic_archivo(f_untrusted)
        r_med = evaluar_etiqueta_mic_archivo(f_high_or_med)
        r_default = evaluar_etiqueta_mic_archivo(f_sin_label)
        r_missing = evaluar_etiqueta_mic_archivo(tmp_path / "no_existe.exe")

        # Assert
        assert r_untrusted.status is MicEvaluationStatus.INCOMPATIBLE_LOW
        assert r_untrusted.effective_rid == 0x0000
        assert r_med.status is MicEvaluationStatus.VERIFIED_COMPATIBLE
        assert r_med.effective_rid == 0x2000
        assert r_default.status is MicEvaluationStatus.UNLABELED_DEFAULT_MEDIUM
        assert r_default.is_compatible is True
        assert r_missing.status is MicEvaluationStatus.MISSING


# ── 4. Ancla AST: solo lectura, sin subprocess/icacls ni duplicación Win32 ─


class TestAnclaAstSoloLecturaMic:
    def test_clones_y_provisioning_no_usan_subprocess_icacls_ni_mutadores_win32_de_seguridad(self) -> None:
        # Arrange
        modulos = [
            pathlib.Path(clones_mod.__file__),
            pathlib.Path(prov_mod.__file__),
        ]
        prohibidos_atributos = {
            "SetNamedSecurityInfoW",
            "SetNamedSecurityInfoA",
            "SetSecurityInfo",
            "SetFileSecurityW",
            "SetFileSecurityA",
        }

        for ruta in modulos:
            fuente = ruta.read_text(encoding="utf-8")
            arbol = ast.parse(fuente, filename=str(ruta))

            # Assert 1: sin import subprocess ni literales icacls
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Import):
                    nombres = {alias.name for alias in nodo.names}
                    assert "subprocess" not in nombres, f"{ruta.name} no debe importar subprocess"
                elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                    assert not nodo.module.startswith("subprocess"), f"{ruta.name} no debe importar de subprocess"
                elif isinstance(nodo, ast.Attribute):
                    assert nodo.attr not in prohibidos_atributos, (
                        f"{ruta.name} no debe referenciar API mutante {nodo.attr}"
                    )
                elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                    assert "icacls" not in nodo.value.lower(), f"{ruta.name} no debe invocar ni depender de icacls"

        # Assert 2: GetNamedSecurityInfoW vive únicamente en clones.py (sin duplicar Win32 en provisioning.py)
        assert "GetNamedSecurityInfoW" in pathlib.Path(clones_mod.__file__).read_text(encoding="utf-8")
        assert "GetNamedSecurityInfoW" not in pathlib.Path(prov_mod.__file__).read_text(encoding="utf-8")
