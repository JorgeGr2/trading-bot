"""Análisis de traders: posiciones, métricas, comportamientos no copiables y puntuación.

El núcleo (``positions``, ``metrics``, ``scoring``) son funciones puras: reciben los
eventos de una wallet y una fecha ``as_of`` y nunca miran nada posterior a ella.
"""
