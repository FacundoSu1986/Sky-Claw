import asyncio
import contextlib
import logging

from sky_claw.core.models import LootExecutionParams, WSLInteropError

logger = logging.getLogger("SkyClaw.Interop")


class ModdingToolsAgent:
    @staticmethod
    async def translate_path_wsl_to_win(wsl_path: str, timeout: float = 10.0) -> str:
        r"""Usa wslpath para convertir rutas de Linux a formato Windows (C:\...).

        Args:
            wsl_path: Ruta WSL a traducir (ej. /mnt/c/...).
            timeout: Timeout en segundos para la invocación de wslpath (default: 10s).

        Raises:
            WSLInteropError: Si wslpath falla, retorna código no-cero, o excede el timeout.
        """
        proc = await asyncio.create_subprocess_exec(
            "wslpath",
            "-w",
            wsl_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            raise WSLInteropError(f"wslpath tardó más de {timeout}s para: {wsl_path}")
        if proc.returncode != 0:
            err_str = stderr.decode("utf-8", errors="replace").strip()
            raise WSLInteropError(f"Fallo en wslpath: {err_str}")
        return stdout.decode("utf-8", errors="replace").strip()

    async def run_loot(self, params: LootExecutionParams) -> dict:
        """Ejecuta LOOT.exe de forma asincrónica previniendo crash por decodificación."""
        loot_exe_wsl = "/mnt/c/Program Files/LOOT/loot.exe"
        game_path_win = "C:\\Steam\\steamapps\\common\\Skyrim Special Edition"

        logger.info("Ejecutando LOOT para el perfil: %s", params.profile_name)

        args = [
            loot_exe_wsl,
            "--game",
            "SkyrimSE",
            "--game-path",
            game_path_win,
            "--sort",
        ]

        if params.update_masterlist:
            args.append("--update-masterlist")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("Ejecutable LOOT no encontrado: %s", loot_exe_wsl)
            return {
                "status": "error",
                "logs": f"Ejecutable LOOT no encontrado: {loot_exe_wsl}",
            }

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=120.0,
            )
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            logger.error("LOOT excedió el timeout de 120s")
            return {
                "status": "error",
                "logs": "LOOT excedió el timeout de 120 segundos",
            }

        # Parche Estándar 2026: Prevención de crash por decodificación en WSL2 cruzando I/O de Windows
        out_str = stdout.decode("utf-8", errors="replace").strip()
        err_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.error(
                "RCA: LOOT falló con código %d. Stderr: %s", proc.returncode, err_str
            )
            return {"status": "error", "logs": err_str}

        return {"status": "success", "logs": out_str}
