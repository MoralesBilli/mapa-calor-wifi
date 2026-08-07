"""Rutas, tiempos y colores compartidos de la aplicación."""

from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
PROJECTS_DIR = DATA_DIR / "proyectos"
PLANOS_DIR = ROOT_DIR / "planos"

# Plano centrado en (0,0): ±15 m en X e Y (30×30 m totales) sin imagen
DEFAULT_HALF_EXTENT_M = 15.0
DEFAULT_WIDTH_M = DEFAULT_HALF_EXTENT_M * 2.0
DEFAULT_HEIGHT_M = DEFAULT_HALF_EXTENT_M * 2.0
EXPAND_STEP_M = 10.0
# Escala al cargar imagen del plano (metros reales del lugar)
DEFAULT_PLAN_WIDTH_M = 50.0  # ancho (eje X)
DEFAULT_PLAN_LENGTH_M = 96.90  # largo midiendo todo (eje Y)
# Formato de imagen al guardar (PNG)
IMAGE_EXTENSION = ".png"
HEATMAP_ALPHA_DEFAULT = 0.88
HEATMAP_ALPHA_WITH_PLAN = 0.55
# Color para puntos sin cobertura / señal perdida (0%)
NO_SIGNAL_COLOR = "#7B2D8E"  # violeta
# Cada cuánto se consulta el AP/red asociados (ms)
CONNECTION_POLL_MS = 2500
# Autoguardado del proyecto .wifimap (ms)
AUTOSAVE_INTERVAL_MS = 60_000
# Cada cuántos ciclos de conexión se reescanean los BSSID del SSID
AP_SCAN_EVERY_N_POLLS = 3
AP_MARKER_ACTIVE = "#C2410C"  # naranja: AP al que estás conectado
AP_MARKER_OTHER = "#64748B"  # gris: otros AP marcados
AP_LIST_OTHER = "#334155"
# Contorno de mediciones cuyo AP aún no está marcado en el mapa
UNMARKED_AP_EDGE = "#2563EB"
AP_MARKER_ESTIMATED = "#2563EB"  # azul: ubicación estimada (no marcada a mano)

# Interfaz (paneles / chrome)
UI_FIG_BG = "#E8ECF1"
UI_PANEL_BG = "#FFFFFF"
UI_PANEL_EDGE = "#C9D1DB"
UI_TEXT = "#1C2430"
UI_MUTED = "#5B6775"
UI_SOFT = "#F4F6F8"
UI_CONNECTED = "#0F766E"
UI_CONNECTED_SOFT = "#E7F5F2"
UI_DISCONNECTED = "#9F1239"
UI_DISCONNECTED_SOFT = "#FDF2F4"
UI_CARD_EDGE = "#D5DCE5"
UI_CARD_BG = "#F7F9FB"
# Radio (m) para info (clic) / borrar (clic der.) punto o marca de AP
REMOVE_HIT_RADIUS_M = 0.6
# Estimación de AP no marcado: mínimo de lecturas y peso por señal
AP_ESTIMATE_MIN_READINGS = 2
AP_ESTIMATE_WEIGHT_POWER = 2.5
