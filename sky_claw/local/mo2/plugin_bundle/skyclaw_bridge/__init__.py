"""MO2 entry point del Sky-Claw VFS Bridge."""


def createPlugin():  # noqa: N802 - nombre requerido por la API de plugins MO2
    # Import perezoso: ``protocol`` también lo usa el daemon, donde mobase no existe.
    from .plugin import SkyClawBridgePlugin

    return SkyClawBridgePlugin()
