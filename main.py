"""Punto de entrada del mapa de calor WiFi."""

from __future__ import annotations

from app import WifiHeatmapApp
from ui_dialogs import show_network_picker


def main() -> None:
    while True:
        if not show_network_picker():
            print("Inicio cancelado: no se eligió red.")
            return
        app = WifiHeatmapApp()
        app.run()
        if not app.return_to_picker:
            return


if __name__ == "__main__":
    main()
