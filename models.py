"""Modelos de dominio ligeros (sin dependencia de Matplotlib)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ApLocation:
    """Ubicación marcada (o estimada) de un AP en el plano."""

    x_m: float
    y_m: float
    bssid: str
    ssid: str
