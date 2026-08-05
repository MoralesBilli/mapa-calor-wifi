"""Mapa de calor WiFi: plano cartesiano centrado en (0,0) + medición por clic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backend_bases import KeyEvent, MouseEvent
from matplotlib.collections import PathCollection
from matplotlib.text import Annotation

from heatmap import (
    MIN_POINTS_FOR_HEATMAP,
    NearbyApReading,
    SamplePoint,
    bounds_from_size,
    draw_heatmap,
    save_figure_image,
)
from project_io import (
    PROJECT_EXTENSION,
    ProjectIOError,
    build_project_dict,
    load_project_file,
    resolve_floor_plan_path,
    samples_from_project,
    save_project_file,
)
from wifi_scanner import (
    VisibleNetwork,
    WifiScannerError,
    connect_to_network,
    get_interface_status,
    list_visible_networks,
    measure_current_network,
    scan_bssids_for_ssid,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
PROJECTS_DIR = DATA_DIR / "proyectos"
PLANOS_DIR = Path(__file__).resolve().parent / "planos"
# Plano centrado en (0,0): ±15 m en X e Y (30×30 m totales) sin imagen
DEFAULT_HALF_EXTENT_M = 15.0
DEFAULT_WIDTH_M = DEFAULT_HALF_EXTENT_M * 2.0
DEFAULT_HEIGHT_M = DEFAULT_HALF_EXTENT_M * 2.0
EXPAND_STEP_M = 5.0
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
# Cada cuántos ciclos de conexión se reescanean los BSSID del SSID
AP_SCAN_EVERY_N_POLLS = 3
AP_MARKER_ACTIVE = "#C2410C"  # naranja: AP al que estás conectado
AP_MARKER_OTHER = "#64748B"  # gris: otros AP marcados
AP_LIST_OTHER = "#334155"
# Radio (m) para info (clic) / borrar (clic der.) sobre un punto existente
REMOVE_HIT_RADIUS_M = 0.6


@dataclass
class ApLocation:
    x_m: float
    y_m: float
    bssid: str
    ssid: str


class WifiHeatmapApp:
    def __init__(self) -> None:
        self.width_m = DEFAULT_WIDTH_M
        self.height_m = DEFAULT_HEIGHT_M
        self.samples: list[SamplePoint] = []
        self.ssid: str = "?"
        self.bssid: str = ""
        self.signal_pct: int | None = None
        self.connected: bool = False
        self.status: str = ""
        self.heatmap_mesh = None
        self.colorbar = None
        self.scatter: PathCollection | None = None
        self.scatter_no_signal: PathCollection | None = None
        self.origin_plot = None
        self.origin_label = None
        self.floor_plan_artist = None
        self.floor_plan_array: np.ndarray | None = None
        self.floor_plan_path: Path | None = None
        self.plan_width_m = DEFAULT_PLAN_WIDTH_M
        self.plan_length_m = DEFAULT_PLAN_LENGTH_M
        self.project_path: Path | None = None
        self._measuring = False
        self._placing_ap = False
        self.ap_locations: dict[str, ApLocation] = {}
        # BSSID visibles del mismo SSID: (bssid, signal_pct)
        self.visible_aps: list[tuple[str, int]] = []
        self._visible_aps_ssid: str = ""
        self._help_artist = None
        self._connection_artist = None
        self._connection_timer = None
        self._ap_list_artists: list = []
        self._ap_scan_counter = 0
        self._ap_list_snapshot: tuple | None = None
        self._point_info_artists: list = []
        self._inspected_sample: SamplePoint | None = None
        self._ap_scatter_active: PathCollection | None = None
        self._ap_scatter_other: PathCollection | None = None
        self._ap_annotations: list[Annotation] = []

        PLANOS_DIR.mkdir(parents=True, exist_ok=True)

        try:
            self._refresh_connection_info()
            self.status = (
                f"Origen (0,0) en el centro. "
                f"Alcance ±{DEFAULT_HALF_EXTENT_M:.0f} m. "
                "Pulsa p para cargar el plano del lugar."
            )
            if not self.connected:
                self.status = f"Aviso: sin conexión WiFi activa. {self.status}"
        except WifiScannerError as exc:
            self.connected = False
            self.ssid = "?"
            self.bssid = ""
            self.signal_pct = None
            self.status = f"Aviso: {exc}"

        self.fig, self.ax = plt.subplots(figsize=(12.5, 7.2))
        self.fig.canvas.manager.set_window_title("Mapa de calor WiFi")
        self._setup_axes()
        self._refresh_visible_aps(force=True)
        self._update_title()
        self._draw_help()

        self.fig.canvas.mpl_connect("button_press_event", self.on_click)
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)
        self.fig.canvas.mpl_connect("close_event", self._on_close)
        self._start_connection_monitor()

    def _x_min(self) -> float:
        return bounds_from_size(self.width_m, self.height_m)[0]

    def _x_max(self) -> float:
        return bounds_from_size(self.width_m, self.height_m)[1]

    def _y_min(self) -> float:
        return bounds_from_size(self.width_m, self.height_m)[2]

    def _y_max(self) -> float:
        return bounds_from_size(self.width_m, self.height_m)[3]

    def _heatmap_alpha(self) -> float:
        if self.floor_plan_array is not None:
            return HEATMAP_ALPHA_WITH_PLAN
        return HEATMAP_ALPHA_DEFAULT

    def _draw_floor_plan(self) -> None:
        if self.floor_plan_artist is not None:
            try:
                self.floor_plan_artist.remove()
            except ValueError:
                pass
            self.floor_plan_artist = None

        if self.floor_plan_array is None:
            return

        self.floor_plan_artist = self.ax.imshow(
            self.floor_plan_array,
            extent=[self._x_min(), self._x_max(), self._y_min(), self._y_max()],
            origin="upper",
            aspect="equal",
            zorder=0,
            interpolation="bilinear",
        )

    def _apply_cartesian_style(self) -> None:
        """Plano cartesiano con (0,0) en el centro y cotas en los bordes."""
        self.ax.set_xlim(self._x_min(), self._x_max())
        self.ax.set_ylim(self._y_min(), self._y_max())
        self.ax.set_aspect("equal", adjustable="box")
        self.ax.set_xlabel("X (metros)", labelpad=6)
        self.ax.set_ylabel("Y (metros)", labelpad=6)
        self.ax.set_facecolor("#f7f7f7")
        grid_alpha = 0.25 if self.floor_plan_array is not None else 0.45
        self.ax.grid(True, which="major", linestyle="--", alpha=grid_alpha, zorder=2)
        # Cruz del origen (sin poner las cotas numéricas encima del centro)
        self.ax.axhline(0, color="#222222", linewidth=1.2, zorder=3)
        self.ax.axvline(0, color="#222222", linewidth=1.2, zorder=3)

        # Ejes y números de metros siempre en las orillas de la imagen
        for spine in self.ax.spines.values():
            spine.set_visible(True)
            spine.set_color("#555555")
            spine.set_linewidth(1.1)
            spine.set_position(("outward", 0))

        self.ax.xaxis.set_ticks_position("both")
        self.ax.yaxis.set_ticks_position("both")
        self.ax.tick_params(
            axis="both",
            which="major",
            labelsize=9,
            direction="out",
            length=5,
            labelbottom=True,
            labeltop=True,
            labelleft=True,
            labelright=True,
            colors="#111111",
        )

        if self.origin_plot is not None:
            try:
                self.origin_plot.remove()
            except ValueError:
                pass
        if self.origin_label is not None:
            try:
                self.origin_label.remove()
            except ValueError:
                pass
            self.origin_label = None

        self.origin_plot = self.ax.plot(
            0,
            0,
            marker="o",
            markersize=7,
            color="#111111",
            markerfacecolor="white",
            markeredgewidth=1.5,
            zorder=7,
        )[0]
        # Etiqueta del origen en la orilla superior (no tapa el centro)
        self.origin_label = self.ax.annotate(
            "origen (0, 0)",
            xy=(0, 0),
            xytext=(0, self._y_max()),
            textcoords="data",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#111111",
            fontweight="bold",
            bbox={
                "boxstyle": "round,pad=0.2",
                "facecolor": "white",
                "edgecolor": "#333333",
                "alpha": 0.9,
            },
            arrowprops={
                "arrowstyle": "-",
                "color": "#444444",
                "lw": 0.7,
                "shrinkA": 0,
                "shrinkB": 2,
            },
            zorder=8,
        )

    def _setup_axes(self) -> None:
        self.ax.clear()
        self.heatmap_mesh = None
        self.colorbar = None
        self.scatter = None
        self.scatter_no_signal = None
        self._ap_scatter_active = None
        self._ap_scatter_other = None
        self._ap_annotations = []
        self.origin_plot = None
        self.origin_label = None
        self.floor_plan_artist = None
        self._draw_floor_plan()
        self._apply_cartesian_style()
        self._redraw_points()
        self._redraw_ap_markers()

    def _refresh_connection_info(self) -> None:
        """Actualiza SSID y BSSID del AP al que está asociada la interfaz."""
        status = get_interface_status()
        self.connected = status.connected
        self.ssid = status.ssid if status.connected and status.ssid else "?"
        self.bssid = status.bssid if status.connected else ""
        self.signal_pct = status.signal_pct if status.connected else None

    def _connection_snapshot(self) -> tuple[bool, str, str]:
        return (self.connected, self.ssid, self.bssid.casefold())

    def _ap_list_view_snapshot(self) -> tuple:
        """Estado visual de la lista lateral (para evitar redibujos innecesarios)."""
        current = self.bssid.casefold() if self.connected and self.bssid else ""
        rows = tuple(
            (bssid.casefold(), pct, bssid.casefold() == current)
            for bssid, pct in self.visible_aps
        )
        ssid = self.ssid if self.connected else ""
        return (ssid, current, rows)

    def _sync_current_ap_into_list(self) -> bool:
        """
        Ajusta la lista en memoria al AP asociado (rápido, sin netsh).
        Útil para resaltar de inmediato un roaming entre AP.
        """
        before = self._ap_list_view_snapshot()
        if not self.connected or self.ssid in {"?", ""} or not self.bssid:
            self.visible_aps = []
            self._visible_aps_ssid = ""
        else:
            # Si cambió de red, no mezclar BSSID del SSID anterior
            if self._visible_aps_ssid.casefold() != self.ssid.casefold():
                self.visible_aps = []
            self._visible_aps_ssid = self.ssid
            key = self.bssid.casefold()
            assoc_pct = self.signal_pct if self.signal_pct is not None else 0
            by_key: dict[str, tuple[str, int]] = {
                b.casefold(): (b, p) for b, p in self.visible_aps
            }
            if key not in by_key:
                by_key[key] = (self.bssid, assoc_pct)
            else:
                listed_bssid, listed_pct = by_key[key]
                by_key[key] = (listed_bssid, max(listed_pct, assoc_pct))
            self.visible_aps = sorted(
                by_key.values(),
                key=lambda item: (-item[1], item[0].casefold()),
            )
        after = self._ap_list_view_snapshot()
        if after != before:
            self._ap_list_snapshot = after
            return True
        return False

    def _refresh_visible_aps(self, *, force: bool = False) -> bool:
        """
        Escanea BSSID del SSID actual. Devuelve True si la lista visual cambió.
        """
        before = self._ap_list_view_snapshot()
        if not self.connected or self.ssid in {"?", ""}:
            self.visible_aps = []
            self._visible_aps_ssid = ""
            after = self._ap_list_view_snapshot()
            changed = after != before or force
            if changed:
                self._ap_list_snapshot = after
            return changed

        found: list[tuple[str, int]] = []
        try:
            found = scan_bssids_for_ssid(self.ssid)
        except WifiScannerError:
            found = []

        by_key: dict[str, tuple[str, int]] = {}
        for bssid, pct in found:
            key = bssid.casefold()
            prev = by_key.get(key)
            if prev is None or pct > prev[1]:
                by_key[key] = (bssid, pct)

        # Incluir siempre el AP asociado, aunque el escaneo aún no lo liste
        if self.bssid:
            key = self.bssid.casefold()
            assoc_pct = self.signal_pct if self.signal_pct is not None else 0
            if key not in by_key:
                by_key[key] = (self.bssid, assoc_pct)
            else:
                listed_bssid, listed_pct = by_key[key]
                by_key[key] = (listed_bssid, max(listed_pct, assoc_pct))

        self.visible_aps = sorted(
            by_key.values(),
            key=lambda item: (-item[1], item[0].casefold()),
        )
        self._visible_aps_ssid = self.ssid
        after = self._ap_list_view_snapshot()
        changed = after != before or force
        if changed:
            self._ap_list_snapshot = after
        return changed

    def _start_connection_monitor(self) -> None:
        """Consulta periódicamente si cambió la red o el AP asociado."""
        if self._connection_timer is not None:
            return
        timer = self.fig.canvas.new_timer(interval=CONNECTION_POLL_MS)
        timer.add_callback(self._poll_connection_change)
        self._connection_timer = timer
        timer.start()

    def _stop_connection_monitor(self) -> None:
        timer = self._connection_timer
        self._connection_timer = None
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

    def _on_close(self, _event) -> None:
        self._stop_connection_monitor()

    def _poll_connection_change(self) -> None:
        """Detecta cambio de SSID/AP y refresca la lista de AP del mismo SSID."""
        if self._measuring:
            return
        before = self._connection_snapshot()
        try:
            self._refresh_connection_info()
        except WifiScannerError:
            self.connected = False
            self.ssid = "?"
            self.bssid = ""
            self.signal_pct = None
        after = self._connection_snapshot()
        connection_changed = after != before

        if connection_changed:
            prev_ssid, prev_bssid = before[1], before[2]
            if self.connected and self.ssid not in {"?", ""}:
                ap = self.bssid or "desconocido"
                if prev_ssid not in {"?", ""} and prev_ssid != self.ssid:
                    self.status = (
                        f"Cambio de red: {prev_ssid} → {self.ssid}  ·  AP: {ap}"
                    )
                elif prev_bssid and prev_bssid != self.bssid.casefold():
                    self.status = (
                        f"Cambio de AP en «{self.ssid}»: ahora {ap}"
                    )
                else:
                    self.status = f"Conectado a «{self.ssid}»  ·  AP: {ap}"
                print(self._connection_label())
            else:
                self.status = "Se perdió la conexión WiFi activa"
                print(self.status)

            # Resaltado inmediato del AP asociado (antes del escaneo lento)
            self._sync_current_ap_into_list()
            self._redraw_ap_markers()
            self._update_title()
            self.fig.canvas.draw_idle()
            self.fig.canvas.flush_events()

        self._ap_scan_counter += 1
        should_scan = (
            connection_changed
            or self._ap_scan_counter >= AP_SCAN_EVERY_N_POLLS
            or (self.connected and not self.visible_aps)
        )
        if not should_scan:
            return

        self._ap_scan_counter = 0
        if not self._refresh_visible_aps():
            return

        self._update_title()
        self.fig.canvas.draw_idle()

    def _connection_label(self) -> str:
        if self.connected and self.ssid not in {"?", ""}:
            ap = self.bssid or "desconocido"
            return f"Conectado a red: {self.ssid}   ·   AP: {ap}"
        return "Sin conexión WiFi activa"

    def _draw_connection_indicator(self) -> None:
        """Barra fija bajo el título con la red y el AP asociados."""
        if self._connection_artist is not None:
            try:
                self._connection_artist.remove()
            except ValueError:
                pass
        color = "#0B6E4F" if self.connected else "#8B2942"
        self._connection_artist = self.fig.text(
            0.58,
            0.935,
            self._connection_label(),
            ha="center",
            va="top",
            fontsize=10,
            fontweight="bold",
            color=color,
            bbox={
                "boxstyle": "round,pad=0.35",
                "facecolor": "#F4F7F5" if self.connected else "#F8EEF0",
                "edgecolor": color,
                "linewidth": 1.0,
                "alpha": 0.95,
            },
        )

    def _clear_ap_list_artists(self) -> None:
        for artist in self._ap_list_artists:
            try:
                artist.remove()
            except ValueError:
                pass
        self._ap_list_artists = []

    def _signal_bar(self, pct: int, width: int = 10) -> str:
        filled = max(0, min(width, round(pct / 100 * width)))
        return "█" * filled + "░" * (width - filled)

    def _draw_ap_list_panel(self) -> None:
        """Panel izquierdo: AP del mismo SSID; resalta el asociado."""
        self._clear_ap_list_artists()

        panel_x = 0.015
        top_y = 0.82
        line_h = 0.038

        title = "AP de la red"
        if self.connected and self.ssid not in {"?", ""}:
            title = f"AP de «{self.ssid}»"
        title_artist = self.fig.text(
            panel_x,
            top_y,
            title,
            ha="left",
            va="top",
            fontsize=10,
            fontweight="bold",
            color="#0F172A",
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._ap_list_artists.append(title_artist)

        subtitle = self.fig.text(
            panel_x,
            top_y - 0.028,
            "naranja = conectado ahora",
            ha="left",
            va="top",
            fontsize=7.5,
            color="#64748B",
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._ap_list_artists.append(subtitle)

        y = top_y - 0.055
        if not self.connected or self.ssid in {"?", ""}:
            empty = self.fig.text(
                panel_x,
                y,
                "Sin red activa.\nConéctate para ver\nlos AP / repetidores.",
                ha="left",
                va="top",
                fontsize=8.5,
                color="#64748B",
                transform=self.fig.transFigure,
                linespacing=1.35,
                zorder=20,
            )
            self._ap_list_artists.append(empty)
            return

        if not self.visible_aps:
            empty = self.fig.text(
                panel_x,
                y,
                "Buscando AP…\n(actualiza solo)",
                ha="left",
                va="top",
                fontsize=8.5,
                color="#64748B",
                transform=self.fig.transFigure,
                linespacing=1.35,
                zorder=20,
            )
            self._ap_list_artists.append(empty)
            return

        current = self.bssid.casefold() if self.bssid else ""
        max_rows = 14
        for idx, (bssid, pct) in enumerate(self.visible_aps[:max_rows]):
            is_current = bssid.casefold() == current
            color = AP_MARKER_ACTIVE if is_current else AP_LIST_OTHER
            weight = "bold" if is_current else "normal"
            marker = "●" if is_current else "○"
            short = self._short_bssid(bssid)
            bar = self._signal_bar(pct)
            label = f"{marker} {short}\n   {bar} {pct}%"
            if is_current:
                label = f"{marker} CONECTADO\n   {short}\n   {bar} {pct}%"

            row = self.fig.text(
                panel_x,
                y,
                label,
                ha="left",
                va="top",
                fontsize=8,
                fontweight=weight,
                color=color,
                family="monospace",
                transform=self.fig.transFigure,
                linespacing=1.25,
                bbox={
                    "boxstyle": "round,pad=0.28",
                    "facecolor": "#FFF7ED" if is_current else "#F8FAFC",
                    "edgecolor": color,
                    "linewidth": 1.15 if is_current else 0.7,
                    "alpha": 0.96,
                },
                zorder=20,
            )
            self._ap_list_artists.append(row)
            y -= line_h * (1.55 if is_current else 1.25)

        remaining = len(self.visible_aps) - max_rows
        if remaining > 0:
            more = self.fig.text(
                panel_x,
                y,
                f"+{remaining} AP más…",
                ha="left",
                va="top",
                fontsize=7.5,
                color="#64748B",
                transform=self.fig.transFigure,
                zorder=20,
            )
            self._ap_list_artists.append(more)

    def _draw_help(self) -> None:
        if self._help_artist is not None:
            try:
                self._help_artist.remove()
            except ValueError:
                pass
        help_text = (
            "Clic: medir (o info si hay punto cerca)  |  clic der.: quitar  |  "
            "a: AP  |  p/c: plano  |  e/w/r: expandir\n"
            "h: heatmap  |  g: guardar proyecto  |  Ctrl+g: guardar como  |  "
            "o: abrir  |  s: PNG  |  z: deshacer  |  q: salir"
        )
        self._help_artist = self.fig.text(
            0.58,
            0.01,
            help_text,
            ha="center",
            va="bottom",
            fontsize=9,
            color="#333333",
        )
        # Márgenes: panel AP izq. + panel info punto der. + cotas del mapa
        self.fig.subplots_adjust(bottom=0.14, left=0.28, right=0.72, top=0.84)

    def _update_title(self) -> None:
        size = f"{self.width_m:.0f}×{self.height_m:.0f} m (origen en el centro)"
        extras = []
        if self.project_path is not None:
            extras.append(f"proyecto: {self.project_path.name}")
        if self.floor_plan_path is not None:
            extras.append(f"plano: {self.floor_plan_path.name}")
        extra = (" — " + " · ".join(extras)) if extras else ""
        title = (
            f"Mapa de calor WiFi — {self.ssid} — "
            f"{len(self.samples)} muestras — {size}{extra}"
        )
        # Estados generales arriba; la info de un punto va al panel derecho
        if self.status and self._inspected_sample is None:
            title = f"{title}\n{self.status}"
        self.ax.set_title(title, fontsize=11)
        self._draw_connection_indicator()
        self._draw_ap_list_panel()
        self._draw_point_info_panel()

    def _redraw_points(self) -> None:
        """Dibuja solo los puntos de medición (sin líneas ni % que tapen la vista)."""
        for attr in ("scatter", "scatter_no_signal"):
            artist = getattr(self, attr)
            if artist is not None:
                try:
                    artist.remove()
                except ValueError:
                    pass
                setattr(self, attr, None)

        if not self.samples:
            return

        with_signal = [s for s in self.samples if s.signal_pct > 0]
        no_signal = [s for s in self.samples if s.signal_pct <= 0]

        if with_signal:
            self.scatter = self.ax.scatter(
                [s.x_m for s in with_signal],
                [s.y_m for s in with_signal],
                c=[s.signal_pct for s in with_signal],
                cmap="RdYlGn",
                vmin=0,
                vmax=100,
                edgecolors="black",
                linewidths=0.8,
                s=70,
                zorder=5,
            )
        if no_signal:
            # Violeta: sin cobertura / señal perdida (0%)
            self.scatter_no_signal = self.ax.scatter(
                [s.x_m for s in no_signal],
                [s.y_m for s in no_signal],
                c=NO_SIGNAL_COLOR,
                edgecolors="black",
                linewidths=1.0,
                s=90,
                marker="X",
                zorder=5,
            )

    def _clear_ap_artists(self) -> None:
        for attr in ("_ap_scatter_active", "_ap_scatter_other"):
            artist = getattr(self, attr)
            if artist is not None:
                try:
                    artist.remove()
                except ValueError:
                    pass
                setattr(self, attr, None)
        for ann in self._ap_annotations:
            try:
                ann.remove()
            except ValueError:
                pass
        self._ap_annotations = []

    def _short_bssid(self, bssid: str) -> str:
        parts = bssid.replace("-", ":").split(":")
        if len(parts) >= 2:
            return f"…{parts[-2]}:{parts[-1]}"
        return bssid[-8:] if len(bssid) > 8 else bssid

    def _redraw_ap_markers(self) -> None:
        """Dibuja los AP marcados; resalta el asociado actualmente."""
        self._clear_ap_artists()
        if not self.ap_locations:
            return

        current = self.bssid.casefold() if self.connected and self.bssid else ""
        active = [
            ap for key, ap in self.ap_locations.items() if key == current
        ]
        others = [
            ap for key, ap in self.ap_locations.items() if key != current
        ]

        if others:
            self._ap_scatter_other = self.ax.scatter(
                [ap.x_m for ap in others],
                [ap.y_m for ap in others],
                c=AP_MARKER_OTHER,
                marker="^",
                s=220,
                edgecolors="white",
                linewidths=1.2,
                zorder=9,
            )
        if active:
            self._ap_scatter_active = self.ax.scatter(
                [ap.x_m for ap in active],
                [ap.y_m for ap in active],
                c=AP_MARKER_ACTIVE,
                marker="^",
                s=280,
                edgecolors="white",
                linewidths=1.4,
                zorder=10,
            )

        for ap in (*others, *active):
            is_current = ap.bssid.casefold() == current
            label = f"AP {self._short_bssid(ap.bssid)}"
            if is_current:
                label = f"AP actual {self._short_bssid(ap.bssid)}"
            ann = self.ax.annotate(
                label,
                xy=(ap.x_m, ap.y_m),
                xytext=(8, 10),
                textcoords="offset points",
                fontsize=8,
                fontweight="bold" if is_current else "normal",
                color=AP_MARKER_ACTIVE if is_current else AP_MARKER_OTHER,
                bbox={
                    "boxstyle": "round,pad=0.2",
                    "facecolor": "white",
                    "edgecolor": AP_MARKER_ACTIVE if is_current else AP_MARKER_OTHER,
                    "alpha": 0.92,
                },
                zorder=11,
            )
            self._ap_annotations.append(ann)

    def _place_ap_at(self, x: float, y: float) -> None:
        """Registra la ubicación del AP asociado en el plano."""
        try:
            self._refresh_connection_info()
        except WifiScannerError as exc:
            self.status = f"No se pudo leer el AP: {exc}"
            self._placing_ap = False
            self._refresh()
            return

        if not self.connected or not self.bssid:
            self.status = "No hay AP asociado. Conéctate a una red WiFi primero."
            self._placing_ap = False
            self._refresh()
            return

        key = self.bssid.casefold()
        self.ap_locations[key] = ApLocation(
            x_m=round(x, 3),
            y_m=round(y, 3),
            bssid=self.bssid,
            ssid=self.ssid,
        )
        self._placing_ap = False
        self.status = (
            f"AP marcado en ({x:.1f}, {y:.1f}) — "
            f"{self.ssid} · {self.bssid}"
        )
        self._redraw_ap_markers()
        self._refresh()

    def _refresh(self) -> None:
        self.ax.set_xlim(self._x_min(), self._x_max())
        self.ax.set_ylim(self._y_min(), self._y_max())
        if self.floor_plan_artist is not None:
            self.floor_plan_artist.set_extent(
                [self._x_min(), self._x_max(), self._y_min(), self._y_max()]
            )
        if self.origin_label is not None:
            self.origin_label.set_position((0, self._y_max()))
        self._update_title()
        self.fig.canvas.draw_idle()

    def _ask_plan_dimensions_m(self, parent) -> tuple[float, float] | None:
        """
        Pregunta ancho (X) y largo (Y) reales del plano en metros.
        Por defecto: ancho 50 m, largo 96.90 m (midiendo todo).
        """
        from tkinter import simpledialog

        width = simpledialog.askfloat(
            "Escala del plano — Ancho",
            (
                "¿Cuántos metros mide el ANCHO del plano (eje X, horizontal)?\n\n"
                f"(Por defecto {DEFAULT_PLAN_WIDTH_M:.0f} m)"
            ),
            parent=parent,
            minvalue=5.0,
            maxvalue=500.0,
            initialvalue=self.plan_width_m,
        )
        if width is None:
            return None

        length = simpledialog.askfloat(
            "Escala del plano — Largo",
            (
                "¿Cuántos metros mide el LARGO del plano (eje Y, vertical),\n"
                "midiendo todo el lugar?\n\n"
                f"(Por defecto {DEFAULT_PLAN_LENGTH_M:.2f} m)"
            ),
            parent=parent,
            minvalue=5.0,
            maxvalue=500.0,
            initialvalue=self.plan_length_m,
        )
        if length is None:
            return None

        return float(width), float(length)

    def _read_floor_plan_image(self, path: Path) -> np.ndarray:
        img = mpimg.imread(path)
        if img.dtype != np.float32 and img.dtype != np.float64:
            img = img.astype(float)
            if img.max() > 1.0:
                img = img / 255.0
        return img

    def load_floor_plan(self) -> None:
        """Abre un diálogo para elegir la imagen del plano y calibrar su escala."""
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial = str(PLANOS_DIR if PLANOS_DIR.exists() else Path.home())
        path_str = filedialog.askopenfilename(
            parent=root,
            title="Seleccionar imagen del plano del lugar",
            initialdir=initial,
            filetypes=[
                ("Imágenes", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg *.jpeg"),
                ("Todos los archivos", "*.*"),
            ],
        )

        if not path_str:
            root.destroy()
            self.status = "Carga de plano cancelada."
            self._refresh()
            return

        dims = self._ask_plan_dimensions_m(root)
        root.destroy()

        if dims is None:
            self.status = "Carga de plano cancelada (sin escala)."
            self._refresh()
            return

        path = Path(path_str)
        try:
            img = self._read_floor_plan_image(path)
        except Exception as exc:
            self.status = f"No se pudo cargar la imagen: {exc}"
            self._refresh()
            return

        # Escala real indicada por el usuario (ancho × largo)
        self.plan_width_m, self.plan_length_m = dims
        self.width_m = self.plan_width_m
        self.height_m = self.plan_length_m

        self.floor_plan_array = img
        self.floor_plan_path = path

        had_heatmap = self.heatmap_mesh is not None and len(self.samples) >= 3
        self._setup_axes()
        if had_heatmap:
            self._apply_heatmap()
        self.status = (
            f"Plano cargado: {path.name} — "
            f"ancho {self.width_m:.2f} m × largo {self.height_m:.2f} m. "
            "Haz clic para medir."
        )
        self._update_title()
        self.fig.canvas.draw_idle()
        print(
            f"Plano cargado: {path} | "
            f"ancho={self.width_m:.2f} m | largo={self.height_m:.2f} m"
        )

    def clear_floor_plan(self) -> None:
        """Quita la imagen de fondo del plano."""
        if self.floor_plan_array is None:
            self.status = "No hay plano cargado."
            self._refresh()
            return

        self.floor_plan_array = None
        self.floor_plan_path = None
        had_heatmap = self.heatmap_mesh is not None and len(self.samples) >= 3
        self._setup_axes()
        if had_heatmap:
            self._apply_heatmap()
        self.status = "Plano eliminado. Continúas con el lienzo vacío."
        self._update_title()
        self.fig.canvas.draw_idle()

    def on_click(self, event: MouseEvent) -> None:
        if event.inaxes != self.ax:
            return
        if event.xdata is None or event.ydata is None:
            return
        if self._measuring:
            return

        x = float(event.xdata)
        y = float(event.ydata)
        if not (self._x_min() <= x <= self._x_max() and self._y_min() <= y <= self._y_max()):
            return

        # Clic derecho: quitar el punto de medición más cercano
        if event.button == 3:
            self._remove_nearest_sample(x, y)
            return

        if event.button != 1:
            return

        if self._placing_ap:
            self._place_ap_at(x, y)
            return

        # Clic sobre / cerca de un punto existente: solo mostrar su info
        near_index = self._find_nearest_sample_index(x, y)
        if near_index is not None:
            self._show_sample_info(self.samples[near_index])
            return

        self._measuring = True
        self.status = "Midiendo señal…"
        self._update_title()
        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

        fallback = self.ssid if self.ssid not in {"?", ""} else None
        try:
            reading = measure_current_network(fallback_ssid=fallback)
            try:
                self._refresh_connection_info()
            except WifiScannerError:
                self.ssid = reading.ssid
            sample = SamplePoint(
                x_m=round(x, 3),
                y_m=round(y, 3),
                signal_pct=reading.signal_pct,
                bssid=reading.bssid,
                ssid=reading.ssid,
                timestamp=datetime.now().isoformat(timespec="seconds"),
                nearby_aps=[
                    NearbyApReading(bssid=ap.bssid, signal_pct=ap.signal_pct)
                    for ap in reading.nearby_aps
                ],
            )
            self.samples.append(sample)
            if sample.signal_pct <= 0:
                self.status = (
                    f"Punto ({sample.x_m:.1f}, {sample.y_m:.1f}) → "
                    "0% sin señal (violeta)"
                )
            else:
                self.status = (
                    f"Punto ({sample.x_m:.1f}, {sample.y_m:.1f}) → "
                    f"{sample.signal_pct}%  AP conectado {sample.bssid}"
                )
                if sample.nearby_aps:
                    self.status += f"  ·  +{len(sample.nearby_aps)} AP cercanos"
            if len(self.samples) < MIN_POINTS_FOR_HEATMAP:
                self.status += (
                    f"  ({len(self.samples)}/{MIN_POINTS_FOR_HEATMAP} "
                    "recomendados para heatmap)"
                )
        except WifiScannerError as exc:
            # Aun sin poder escanear, marcar el punto como 0% (zona sin cobertura)
            sample = SamplePoint(
                x_m=round(x, 3),
                y_m=round(y, 3),
                signal_pct=0,
                bssid="sin-señal",
                ssid=fallback or "desconocida",
                timestamp=datetime.now().isoformat(timespec="seconds"),
                nearby_aps=[],
            )
            self.samples.append(sample)
            self.status = (
                f"Punto ({sample.x_m:.1f}, {sample.y_m:.1f}) → "
                f"0% sin señal (violeta). {exc}"
            )
        finally:
            self._measuring = False

        self._redraw_points()
        self._refresh()

    def expand(self, width: bool = True, height: bool = True) -> None:
        if width:
            self.width_m += EXPAND_STEP_M
        if height:
            self.height_m += EXPAND_STEP_M
        self.status = (
            f"Área: {self.width_m:.0f}×{self.height_m:.0f} m "
            f"[x∈[{self._x_min():.0f},{self._x_max():.0f}], "
            f"y∈[{self._y_min():.0f},{self._y_max():.0f}]]"
        )
        if self.floor_plan_artist is not None:
            self.floor_plan_artist.set_extent(
                [self._x_min(), self._x_max(), self._y_min(), self._y_max()]
            )
        if self.heatmap_mesh is not None and len(self.samples) >= 3:
            self._apply_heatmap()
        else:
            self._refresh()

    def _apply_heatmap(self) -> None:
        # Asegurar que el plano quede detrás del heatmap
        if self.floor_plan_array is not None and self.floor_plan_artist is None:
            self._draw_floor_plan()

        mesh, cbar, msg = draw_heatmap(
            self.ax,
            self.samples,
            self.width_m,
            self.height_m,
            existing_mesh=self.heatmap_mesh,
            colorbar=self.colorbar,
            alpha=self._heatmap_alpha(),
        )
        self.heatmap_mesh = mesh
        self.colorbar = cbar
        self.status = msg
        self.ax.axhline(0, color="#222222", linewidth=1.2, zorder=3)
        self.ax.axvline(0, color="#222222", linewidth=1.2, zorder=3)
        self._redraw_points()
        self._redraw_ap_markers()
        self._refresh()

    def _find_nearest_sample_index(self, x: float, y: float) -> int | None:
        """Índice del punto más cercano dentro de REMOVE_HIT_RADIUS_M, o None."""
        if not self.samples:
            return None
        best_i = -1
        best_dist = REMOVE_HIT_RADIUS_M
        for i, sample in enumerate(self.samples):
            dist = float(np.hypot(sample.x_m - x, sample.y_m - y))
            if dist <= best_dist:
                best_dist = dist
                best_i = i
        return best_i if best_i >= 0 else None

    def _clear_point_info_artists(self) -> None:
        for artist in self._point_info_artists:
            try:
                artist.remove()
            except ValueError:
                pass
        self._point_info_artists = []

    def _draw_point_info_panel(self) -> None:
        """Panel derecho con el detalle del punto inspeccionado."""
        self._clear_point_info_artists()

        panel_x = 0.735
        top_y = 0.82
        title = self.fig.text(
            panel_x,
            top_y,
            "Info del punto",
            ha="left",
            va="top",
            fontsize=10,
            fontweight="bold",
            color="#0F172A",
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._point_info_artists.append(title)

        sample = self._inspected_sample
        if sample is None:
            empty = self.fig.text(
                panel_x,
                top_y - 0.04,
                "Haz clic en un punto\n(o cerca) para ver\nseñal y AP.",
                ha="left",
                va="top",
                fontsize=8.5,
                color="#64748B",
                transform=self.fig.transFigure,
                linespacing=1.35,
                zorder=20,
            )
            self._point_info_artists.append(empty)
            return

        if sample.signal_pct <= 0:
            signal_txt = "0% sin señal"
            signal_color = NO_SIGNAL_COLOR
        else:
            signal_txt = f"{sample.signal_pct:.0f}%"
            signal_color = "#0B6E4F"

        lines = [
            (f"Posición\n({sample.x_m:.1f}, {sample.y_m:.1f}) m", "#0F172A", "bold"),
            (f"Señal conectado\n{signal_txt}", signal_color, "bold"),
            (f"AP conectado\n{sample.bssid or 'desconocido'}", AP_MARKER_ACTIVE, "normal"),
            (f"Red\n«{sample.ssid or 'desconocida'}»", "#334155", "normal"),
            (f"Fecha\n{sample.timestamp or 'sin fecha'}", "#64748B", "normal"),
        ]

        y = top_y - 0.045
        for text, color, weight in lines:
            row = self.fig.text(
                panel_x,
                y,
                text,
                ha="left",
                va="top",
                fontsize=8,
                fontweight=weight,
                color=color,
                transform=self.fig.transFigure,
                linespacing=1.25,
                bbox={
                    "boxstyle": "round,pad=0.28",
                    "facecolor": "#F8FAFC",
                    "edgecolor": color,
                    "linewidth": 0.8,
                    "alpha": 0.96,
                },
                zorder=20,
            )
            self._point_info_artists.append(row)
            y -= 0.085

        nearby_title = self.fig.text(
            panel_x,
            y,
            "Otros AP cercanos",
            ha="left",
            va="top",
            fontsize=8.5,
            fontweight="bold",
            color="#0F172A",
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._point_info_artists.append(nearby_title)
        y -= 0.03

        if not sample.nearby_aps:
            none_txt = self.fig.text(
                panel_x,
                y,
                "Ninguno detectado",
                ha="left",
                va="top",
                fontsize=8,
                color="#64748B",
                transform=self.fig.transFigure,
                zorder=20,
            )
            self._point_info_artists.append(none_txt)
            return

        for ap in sample.nearby_aps[:8]:
            row = self.fig.text(
                panel_x,
                y,
                f"{self._short_bssid(ap.bssid)}  {ap.signal_pct}%",
                ha="left",
                va="top",
                fontsize=8,
                color=AP_LIST_OTHER,
                family="monospace",
                transform=self.fig.transFigure,
                bbox={
                    "boxstyle": "round,pad=0.22",
                    "facecolor": "#F8FAFC",
                    "edgecolor": "#CBD5E1",
                    "linewidth": 0.7,
                    "alpha": 0.96,
                },
                zorder=20,
            )
            self._point_info_artists.append(row)
            y -= 0.038

        remaining = len(sample.nearby_aps) - 8
        if remaining > 0:
            more = self.fig.text(
                panel_x,
                y,
                f"+{remaining} más…",
                ha="left",
                va="top",
                fontsize=7.5,
                color="#64748B",
                transform=self.fig.transFigure,
                zorder=20,
            )
            self._point_info_artists.append(more)

    def _show_sample_info(self, sample: SamplePoint) -> None:
        """Muestra el detalle del punto en el panel derecho (sin re-medir)."""
        self._inspected_sample = sample
        self.status = ""
        self._refresh()

    def _remove_sample_at(self, index: int) -> None:
        removed = self.samples.pop(index)
        if (
            self._inspected_sample is not None
            and self._inspected_sample is removed
        ):
            self._inspected_sample = None
        self.status = (
            f"Eliminado punto ({removed.x_m:.1f}, {removed.y_m:.1f}) "
            f"{removed.signal_pct}%"
        )
        if self.heatmap_mesh is not None:
            if len(self.samples) >= 3:
                self._apply_heatmap()
            else:
                self.heatmap_mesh.remove()
                self.heatmap_mesh = None
                self._redraw_points()
                self._refresh()
        else:
            self._redraw_points()
            self._refresh()

    def _remove_nearest_sample(self, x: float, y: float) -> None:
        index = self._find_nearest_sample_index(x, y)
        if index is None:
            self.status = (
                f"Ningún punto cerca del clic "
                f"(radio {REMOVE_HIT_RADIUS_M:.1f} m)."
            )
            self._refresh()
            return
        self._remove_sample_at(index)

    def undo(self) -> None:
        if not self.samples:
            self.status = "No hay puntos para deshacer."
            self._refresh()
            return
        self._remove_sample_at(len(self.samples) - 1)

    def _set_sample_points_visible(self, visible: bool) -> None:
        """Muestra u oculta los puntos de medición (no el origen)."""
        for artist in (self.scatter, self.scatter_no_signal):
            if artist is not None:
                artist.set_visible(visible)

    def save(self) -> None:
        """Guarda dos PNG: con puntos marcados y solo mapa de calor."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_ssid = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.ssid)
        with_points_path = (
            DATA_DIR / f"heatmap_{safe_ssid}_{stamp}_con_puntos{IMAGE_EXTENSION}"
        )
        heatmap_only_path = (
            DATA_DIR / f"heatmap_{safe_ssid}_{stamp}_solo_calor{IMAGE_EXTENSION}"
        )

        # 1) Con puntos de medición + mapa de calor
        save_figure_image(self.fig, with_points_path)

        # 2) Solo mapa de calor (sin puntos marcados)
        self._set_sample_points_visible(False)
        self.fig.canvas.draw()
        try:
            save_figure_image(self.fig, heatmap_only_path)
        finally:
            self._set_sample_points_visible(True)
            self.fig.canvas.draw_idle()

        self.status = (
            f"Guardadas 2 imágenes: {with_points_path.name} y "
            f"{heatmap_only_path.name}"
        )
        self._refresh()
        print(f"Guardado con puntos: {with_points_path}")
        print(f"Guardado solo calor: {heatmap_only_path}")

    def _default_project_name(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_ssid = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in self.ssid
        )
        if safe_ssid in {"", "?"}:
            safe_ssid = "sesion"
        return f"proyecto_{safe_ssid}_{stamp}{PROJECT_EXTENSION}"

    def _ask_project_save_path(self) -> Path | None:
        import tkinter as tk
        from tkinter import filedialog

        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initialdir = str(
            self.project_path.parent
            if self.project_path is not None
            else PROJECTS_DIR
        )
        initialfile = (
            self.project_path.name
            if self.project_path is not None
            else self._default_project_name()
        )
        path_str = filedialog.asksaveasfilename(
            parent=root,
            title="Guardar proyecto para continuar después",
            initialdir=initialdir,
            initialfile=initialfile,
            defaultextension=PROJECT_EXTENSION,
            filetypes=[
                ("Proyecto mapa WiFi", f"*{PROJECT_EXTENSION}"),
                ("Todos los archivos", "*.*"),
            ],
        )
        root.destroy()
        if not path_str:
            return None
        return Path(path_str)

    def _ask_project_open_path(self) -> Path | None:
        import tkinter as tk
        from tkinter import filedialog

        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initialdir = str(
            self.project_path.parent
            if self.project_path is not None
            else PROJECTS_DIR
        )
        path_str = filedialog.askopenfilename(
            parent=root,
            title="Abrir proyecto guardado",
            initialdir=initialdir,
            filetypes=[
                ("Proyecto mapa WiFi", f"*{PROJECT_EXTENSION}"),
                ("Todos los archivos", "*.*"),
            ],
        )
        root.destroy()
        if not path_str:
            return None
        return Path(path_str)

    def save_project(self, *, ask_path: bool = True) -> None:
        """
        Guarda la sesión (puntos, plano, AP, tamaño) en un .wifimap
        para poder reabrirla y seguir midiendo.
        """
        path = self.project_path
        if ask_path or path is None:
            chosen = self._ask_project_save_path()
            if chosen is None:
                self.status = "Guardado de proyecto cancelado."
                self._refresh()
                return
            path = chosen

        payload = build_project_dict(
            width_m=self.width_m,
            height_m=self.height_m,
            samples=self.samples,
            ap_locations=self.ap_locations,
            floor_plan_path=self.floor_plan_path,
            plan_width_m=self.plan_width_m,
            plan_length_m=self.plan_length_m,
            heatmap_visible=self.heatmap_mesh is not None,
            project_path=path,
        )
        try:
            saved = save_project_file(path, payload)
        except ProjectIOError as exc:
            self.status = str(exc)
            self._refresh()
            return

        self.project_path = saved
        self.status = (
            f"Proyecto guardado: {saved.name}  "
            f"({len(self.samples)} puntos). Pulsa o para abrirlo después."
        )
        self._refresh()
        print(f"Proyecto guardado: {saved}")

    def open_project(self) -> None:
        """Carga un .wifimap y restaura el trabajo para continuar midiendo."""
        path = self._ask_project_open_path()
        if path is None:
            self.status = "Apertura de proyecto cancelada."
            self._refresh()
            return

        try:
            data = load_project_file(path)
            samples = samples_from_project(data)
        except ProjectIOError as exc:
            self.status = str(exc)
            self._refresh()
            return

        canvas = data.get("canvas") or {}
        try:
            width_m = float(canvas.get("width_m", DEFAULT_WIDTH_M))
            height_m = float(canvas.get("height_m", DEFAULT_HEIGHT_M))
        except (TypeError, ValueError):
            self.status = "El proyecto tiene dimensiones de lienzo inválidas."
            self._refresh()
            return
        if width_m <= 0 or height_m <= 0:
            self.status = "El proyecto tiene dimensiones de lienzo inválidas."
            self._refresh()
            return

        floor = data.get("floor_plan")
        floor_path: Path | None = None
        floor_array = None
        plan_width = DEFAULT_PLAN_WIDTH_M
        plan_length = DEFAULT_PLAN_LENGTH_M
        plan_warning = ""

        if isinstance(floor, dict):
            try:
                plan_width = float(floor.get("width_m", plan_width))
                plan_length = float(floor.get("length_m", plan_length))
            except (TypeError, ValueError):
                pass
            floor_path = resolve_floor_plan_path(
                path,
                floor.get("path"),
                floor.get("path_absolute"),
            )
            if floor_path is None:
                plan_warning = (
                    " (aviso: no se encontró la imagen del plano; "
                    "pulsa p para volver a cargarla)"
                )
            else:
                try:
                    floor_array = self._read_floor_plan_image(floor_path)
                except Exception as exc:
                    floor_path = None
                    floor_array = None
                    plan_warning = f" (aviso: plano no legible: {exc})"

        aps: dict[str, ApLocation] = {}
        for item in data.get("ap_locations") or []:
            if not isinstance(item, dict):
                continue
            try:
                ap = ApLocation(
                    x_m=float(item["x_m"]),
                    y_m=float(item["y_m"]),
                    bssid=str(item.get("bssid", "")),
                    ssid=str(item.get("ssid", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if not ap.bssid:
                continue
            aps[ap.bssid.casefold()] = ap

        view = data.get("view") or {}
        want_heatmap = bool(view.get("heatmap_visible", False))

        self.width_m = width_m
        self.height_m = height_m
        self.samples = samples
        self.ap_locations = aps
        self.plan_width_m = plan_width
        self.plan_length_m = plan_length
        self.floor_plan_path = floor_path
        self.floor_plan_array = floor_array
        self.project_path = path.resolve()
        self._placing_ap = False
        self._inspected_sample = None

        self._setup_axes()
        if want_heatmap and len(self.samples) >= 3:
            self._apply_heatmap()

        try:
            self._refresh_connection_info()
        except WifiScannerError:
            pass
        self._refresh_visible_aps(force=True)

        self.status = (
            f"Proyecto abierto: {path.name} — {len(self.samples)} puntos"
            f"{plan_warning}. Puedes seguir midiendo."
        )
        self._update_title()
        self.fig.canvas.draw_idle()
        print(f"Proyecto abierto: {self.project_path}")

    def on_key(self, event: KeyEvent) -> None:
        key = (event.key or "").lower()
        if key == "e":
            self.expand(width=True, height=True)
        elif key == "w":
            self.expand(width=True, height=False)
        elif key == "r":
            self.expand(width=False, height=True)
        elif key == "p":
            self.load_floor_plan()
        elif key == "c":
            self.clear_floor_plan()
        elif key == "a":
            if self._placing_ap:
                self._placing_ap = False
                self.status = "Marcado de AP cancelado."
                self._refresh()
                return
            try:
                self._refresh_connection_info()
            except WifiScannerError:
                pass
            if not self.connected or not self.bssid:
                self.status = (
                    "No hay AP asociado. Conéctate a una red para marcarlo."
                )
                self._refresh()
                return
            self._placing_ap = True
            self.status = (
                f"Modo marcar AP («{self.ssid}» · {self.bssid}): "
                "haz clic en su ubicación en el mapa (a otra vez para cancelar)."
            )
            self._refresh()
        elif key == "escape":
            if self._placing_ap:
                self._placing_ap = False
                self.status = "Marcado de AP cancelado."
                self._refresh()
        elif key == "h":
            if len(self.samples) < 3:
                self.status = "Se necesitan al menos 3 puntos para el heatmap."
                self._refresh()
            else:
                self._apply_heatmap()
        elif key == "g":
            # Si ya hay proyecto abierto/guardado, sobrescribe; si no, pide ruta
            self.save_project(ask_path=self.project_path is None)
        elif key == "ctrl+g":
            self.save_project(ask_path=True)
        elif key == "o":
            self.open_project()
        elif key == "s":
            self.save()
        elif key == "z":
            self.undo()
        elif key == "q":
            plt.close(self.fig)

    def run(self) -> None:
        print("Mapa de calor WiFi — plano cartesiano (origen en el centro)")
        print(self._connection_label())
        print(
            f"Rango: x [{self._x_min():.0f}, {self._x_max():.0f}] m, "
            f"y [{self._y_min():.0f}, {self._y_max():.0f}] m"
        )
        print(f"Carpeta de planos sugerida: {PLANOS_DIR}")
        print(f"Proyectos: {PROJECTS_DIR}")
        print(
            "Controles: clic=medir (info si hay punto cerca) | clic der.=quitar | "
            "a=marcar AP | "
            "p=cargar plano | c=quitar plano | e=expandir | h=heatmap | "
            "g=guardar proyecto | Ctrl+g=guardar como | o=abrir proyecto | "
            "s=PNG | z=deshacer | q=salir"
        )
        plt.show()


def show_network_picker() -> bool:
    """
    Menú previo: elegir y conectar a una red WiFi.
    Devuelve False si el usuario cancela / cierra la ventana.
    """
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    root = tk.Tk()
    root.title("Mapa de calor WiFi — Elegir red")
    root.geometry("560x460")
    root.minsize(480, 380)
    root.attributes("-topmost", True)

    networks: list[VisibleNetwork] = []
    result = {"continue": False}

    current_var = tk.StringVar(value="Comprobando conexión…")
    status_var = tk.StringVar(value="Pulsa «Actualizar» para escanear redes.")

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill=tk.BOTH, expand=True)

    ttk.Label(
        frm,
        text="Elige la red WiFi a la que quieres conectarte",
        font=("Segoe UI", 12, "bold"),
    ).pack(anchor=tk.W)

    ttk.Label(frm, textvariable=current_var, foreground="#0B6E4F").pack(
        anchor=tk.W, pady=(4, 10)
    )

    list_frame = ttk.Frame(frm)
    list_frame.pack(fill=tk.BOTH, expand=True)

    columns = ("ssid", "signal", "auth", "profile")
    tree = ttk.Treeview(
        list_frame,
        columns=columns,
        show="headings",
        selectmode="browse",
        height=12,
    )
    tree.heading("ssid", text="Red (SSID)")
    tree.heading("signal", text="Señal")
    tree.heading("auth", text="Seguridad")
    tree.heading("profile", text="Perfil")
    tree.column("ssid", width=220, anchor=tk.W)
    tree.column("signal", width=70, anchor=tk.CENTER)
    tree.column("auth", width=130, anchor=tk.W)
    tree.column("profile", width=90, anchor=tk.CENTER)

    scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)

    ttk.Label(frm, textvariable=status_var, wraplength=520).pack(
        anchor=tk.W, pady=(8, 6)
    )

    btn_row = ttk.Frame(frm)
    btn_row.pack(fill=tk.X, pady=(4, 0))

    def _refresh_current() -> None:
        try:
            status = get_interface_status()
            if status.connected and status.ssid:
                ap = status.bssid or "—"
                current_var.set(f"Ahora: {status.ssid}  ·  AP: {ap}")
            else:
                current_var.set("Ahora: sin conexión WiFi activa")
        except WifiScannerError as exc:
            current_var.set(f"Ahora: no se pudo leer la interfaz ({exc})")

    def _fill_list(items: list[VisibleNetwork]) -> None:
        tree.delete(*tree.get_children())
        networks.clear()
        networks.extend(items)
        for idx, net in enumerate(items):
            tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    net.ssid,
                    f"{net.signal_pct}%",
                    net.authentication,
                    "Guardado" if net.has_profile else "Nueva",
                ),
            )

    def scan_networks() -> None:
        status_var.set("Escaneando redes…")
        root.update_idletasks()
        try:
            items = list_visible_networks()
            _fill_list(items)
            _refresh_current()
            if items:
                status_var.set(
                    f"{len(items)} redes encontradas. "
                    "Selecciona una y pulsa «Conectar»."
                )
            else:
                status_var.set(
                    "No se encontraron redes. ¿WiFi activado? Prueba Actualizar."
                )
        except WifiScannerError as exc:
            status_var.set(f"Error al escanear: {exc}")

    def selected_network() -> VisibleNetwork | None:
        sel = tree.selection()
        if not sel:
            return None
        try:
            return networks[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def connect_selected() -> None:
        net = selected_network()
        if net is None:
            messagebox.showinfo(
                "Elegir red",
                "Selecciona una red de la lista.",
                parent=root,
            )
            return

        password: str | None = None
        auth_l = net.authentication.casefold()
        needs_password = not net.has_profile and "open" not in auth_l and "abiert" not in auth_l
        if needs_password:
            password = simpledialog.askstring(
                "Contraseña WiFi",
                f"Contraseña para «{net.ssid}»:\n"
                "(si la red ya está guardada en Windows, cancela y vuelve a escanear)",
                parent=root,
                show="*",
            )
            if password is None:
                status_var.set("Conexión cancelada.")
                return
            if not password.strip():
                messagebox.showwarning(
                    "Contraseña",
                    "La contraseña no puede estar vacía.",
                    parent=root,
                )
                return

        status_var.set(f"Conectando a «{net.ssid}»…")
        root.update_idletasks()
        try:
            connect_to_network(
                net.ssid,
                authentication=net.authentication,
                encryption=net.encryption,
                password=password,
            )
            _refresh_current()
            status_var.set(f"Conectado a «{net.ssid}». Abriendo mapa…")
            root.update_idletasks()
            result["continue"] = True
            root.after(400, root.destroy)
        except WifiScannerError as exc:
            status_var.set(f"No se pudo conectar: {exc}")
            messagebox.showerror("Conexión WiFi", str(exc), parent=root)

    def continue_current() -> None:
        result["continue"] = True
        root.destroy()

    def cancel() -> None:
        result["continue"] = False
        root.destroy()

    ttk.Button(btn_row, text="Actualizar", command=scan_networks).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(btn_row, text="Conectar", command=connect_selected).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(
        btn_row, text="Continuar con la red actual", command=continue_current
    ).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(btn_row, text="Salir", command=cancel).pack(side=tk.RIGHT)

    tree.bind("<Double-1>", lambda _e: connect_selected())
    root.protocol("WM_DELETE_WINDOW", cancel)

    _refresh_current()
    root.after(100, scan_networks)
    root.mainloop()
    return bool(result["continue"])


def main() -> None:
    if not show_network_picker():
        print("Inicio cancelado: no se eligió red.")
        return
    app = WifiHeatmapApp()
    app.run()


if __name__ == "__main__":
    main()
