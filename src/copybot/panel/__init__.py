"""Panel web local del bot (solo lectura y solo simulación).

Una página HTML con su CSS, su JavaScript y sus gráficos SVG dentro (funciona sin
conexión) y una API JSON que la página consulta cada 15 segundos:

* ``/api/estado``: bot de cripto en Binance (cartera de prueba, análisis del día,
  umbrales de la regla, pasos del día y latido del servicio).
* ``/api/sniper``: bot de sniping en Solana (simulado), todavía sin datos.

Todos los números salen de la base de datos; si no hay, se dice «sin datos todavía».
"""
