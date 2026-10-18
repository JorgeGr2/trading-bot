"""Motor de simulación (Fase 4): señales → filtros → riesgo → orden simulada → ejecución.

Solo simula. No existe ningún código capaz de firmar o enviar transacciones: el
ejecutor simulado pide cotizaciones de solo lectura a Jupiter y espera la latencia
configurada antes de volver a cotizar, como haría una orden real.
"""
