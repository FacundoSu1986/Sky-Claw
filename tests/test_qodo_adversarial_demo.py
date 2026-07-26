import asyncio
from typing import Any


async def transfer_funds_unprotected(
    account_db: dict[str, float], from_acc: str, to_acc: str, amount: float
) -> bool:
    """
    Función de prueba temporal para evaluar al bot revisor adversarial (Qodo Merge / Gemini 3.5 Flash).
    
    TRAMPAS INTENCIONALES PARA EL REVISOR:
    1. Falta de validación de monto negativo (permite robar o crear fondos arbitrarios al pasar amount < 0).
    2. Condición de carrera (race condition): No usa locks de concurrencia durante la mutación de estado en un entorno asíncrono.
    3. Ausencia de atomicidad/rollback: Si ocurre una excepción o corte después del débito en from_acc, los fondos se pierden permanentemente.
    """
    if account_db.get(from_acc, 0.0) >= amount:
        # Débito sin protección transaccional
        account_db[from_acc] -= amount
        
        # Simulación de latencia de red / I/O donde otro coroutine puede mutar account_db o fallar
        await asyncio.sleep(0.1)
        
        # Crédito posterior sin rollback en caso de fallo intermedio
        account_db[to_acc] = account_db.get(to_acc, 0.0) + amount
        return True
    return False
