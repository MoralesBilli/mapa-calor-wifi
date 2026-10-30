"""UI Matplotlib del mapa de calor WiFi (capa de presentación)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backend_bases import KeyEvent, MouseEvent
from matplotlib.collections import PathCollection
from matplotlib.patches import FancyBboxPatch
from matplotlib.text import Annotation
from matplotlib.widgets import Button, TextBox

# Corregir bug interno de Matplotlib donde TextBox._resize está mal decorado
# y causa AttributeError al no tener ResizeEvent el atributo 'inaxes'.
TextBox._resize = lambda self, event: self.stop_typing()

from constants import (
    AP_ESTIMATE_MIN_READINGS,
    AP_ESTIMATE_PATH_LOSS_N,
    AP_ESTIMATE_RSSI_REF_DBM,
    AP_ESTIMATE_TOP_N_READINGS,
    AP_ESTIMATE_WEIGHT_POWER,
    AP_MARKER_ACTIVE,
    AP_MARKER_ESTIMATED,
    AP_MARKER_OTHER,
    AP_SCAN_EVERY_N_POLLS,
    AUTOSAVE_INTERVAL_MS,
    CONNECTION_POLL_MS,
    DATA_DIR,
    DEFAULT_HALF_EXTENT_M,
    DEFAULT_HEIGHT_M,
    DEFAULT_PLAN_LENGTH_M,
    DEFAULT_PLAN_WIDTH_M,
    DEFAULT_WIDTH_M,
    EXPAND_STEP_M,
    HEATMAP_ALPHA_DEFAULT,
    HEATMAP_ALPHA_WITH_PLAN,
    IMAGE_EXTENSION,
    NEARBY_AP_MIN_SIGNAL_PCT,
    NO_SIGNAL_COLOR,
    PLANOS_DIR,
    PROJECTS_DIR,
    REMOVE_HIT_RADIUS_M,
    UI_CARD_BG,
    UI_CARD_EDGE,
    UI_CONNECTED,
    UI_CONNECTED_SOFT,
    UI_DISCONNECTED,
    UI_DISCONNECTED_SOFT,
    UI_FIG_BG,
    UI_MUTED,
    UI_PANEL_BG,
    UI_PANEL_EDGE,
    UI_SOFT,
    UI_TEXT,
    UNMARKED_AP_EDGE,
)
from heatmap import (
    MIN_POINTS_FOR_HEATMAP,
    NearbyApReading,
    SamplePoint,
    draw_heatmap,
    save_figure_image,
)
from models import ApLocation
from project_io import (
    PROJECT_EXTENSION,
    ProjectIOError,
    load_project_file,
    resolve_floor_plan_path,
    samples_from_project,
    save_project_file,
)
from project_state import ProjectState
from toolbar_home import install_home_handler
from ui_async import BackgroundJobs
from ui_dialogs import (
    ask_floor_plan_image_path,
    ask_plan_dimensions_m,
    ask_project_open_path,
    ask_project_save_path,
    show_usage_guide as open_usage_guide,
)
from wifi_scanner import (
    InterfaceStatus,
    ScannedBssid,
    WifiSample,
    WifiScannerError,
    get_interface_status,
    measure_current_network,
    normalize_bssid,
    scan_bssids_for_ssid,
)


class WifiHeatmapApp:
    def __init__(self) -> None:
        self.state = ProjectState()
        # Estado de conexión en tiempo real (no va en el .wifimap)
        self.ssid: str = "?"
        self.bssid: str = ""
        self.signal_pct: int | None = None
        self.channel: int | None = None
        self.band: str = ""
        self.connected: bool = False
        self.status: str = ""
        self.heatmap_mesh = None
        self.colorbar = None
        self.scatter: PathCollection | None = None
        self.scatter_no_signal: PathCollection | None = None
        self.origin_plot = None
        self.origin_label = None
        self.floor_plan_artist = None
        self._measuring = False
        self._poll_busy = False
        self._ap_scan_busy = False
        self._placing_ap = False
        # BSSID visibles del mismo SSID (con canal/banda)
        self.visible_aps: list[ScannedBssid] = []
        self._visible_aps_ssid: str = ""
        self._help_artist = None
        self._toolbar_buttons: list[Button] = []
        self._toolbar_axes: list = []
        self._toolbar_key_artists: list = []
        self._connection_artist = None
        self._status_artist = None
        self._meta_artist = None
        self._chrome_patches: list = []
        self._connection_timer = None
        self._autosave_timer = None
        self._jobs: BackgroundJobs | None = None
        self.return_to_picker = False
        self._ap_list_artists: list = []
        self._ap_scan_counter = 0
        self._ap_list_snapshot: tuple | None = None
        self._point_info_artists: list = []
        self._inspected_sample: SamplePoint | None = None
        self._ap_scatter_active: PathCollection | None = None
        self._ap_scatter_other: PathCollection | None = None
        self._ap_scatter_estimated: PathCollection | None = None
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
            self.channel = None
            self.band = ""
            self.status = f"Aviso: {exc}"

        self.fig, self.ax = plt.subplots(figsize=(13.2, 7.6))
        self.fig.patch.set_facecolor(UI_FIG_BG)
        self.fig.canvas.manager.set_window_title("Mapa de calor WiFi")
        self._jobs = BackgroundJobs(self.fig)
        install_home_handler(self.fig, self._go_back_to_network_picker)
        self._draw_chrome()
        self._setup_axes()
        self._update_title()
        self._draw_help()
        # Escaneo BSSID en segundo plano (no congelar el arranque)
        self._request_ap_scan(force=True)

        self.fig.canvas.mpl_connect("button_press_event", self.on_click)
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)
        self.fig.canvas.mpl_connect("close_event", self._on_close)
        self._start_connection_monitor()
        self._start_autosave()

    # --- Delegación al estado del proyecto ---
    @property
    def width_m(self) -> float:
        return self.state.width_m

    @width_m.setter
    def width_m(self, value: float) -> None:
        self.state.width_m = value

    @property
    def height_m(self) -> float:
        return self.state.height_m

    @height_m.setter
    def height_m(self, value: float) -> None:
        self.state.height_m = value

    @property
    def samples(self) -> list[SamplePoint]:
        return self.state.samples

    @samples.setter
    def samples(self, value: list[SamplePoint]) -> None:
        self.state.samples = value

    @property
    def ap_locations(self) -> dict[str, ApLocation]:
        return self.state.ap_locations

    @ap_locations.setter
    def ap_locations(self, value: dict[str, ApLocation]) -> None:
        self.state.ap_locations = value

    @property
    def floor_plan_array(self):
        return self.state.floor_plan_array

    @floor_plan_array.setter
    def floor_plan_array(self, value) -> None:
        self.state.floor_plan_array = value

    @property
    def floor_plan_path(self) -> Path | None:
        return self.state.floor_plan_path

    @floor_plan_path.setter
    def floor_plan_path(self, value: Path | None) -> None:
        self.state.floor_plan_path = value

    @property
    def plan_width_m(self) -> float:
        return self.state.plan_width_m

    @plan_width_m.setter
    def plan_width_m(self, value: float) -> None:
        self.state.plan_width_m = value

    @property
    def plan_length_m(self) -> float:
        return self.state.plan_length_m

    @plan_length_m.setter
    def plan_length_m(self, value: float) -> None:
        self.state.plan_length_m = value

    @property
    def project_path(self) -> Path | None:
        return self.state.project_path

    @project_path.setter
    def project_path(self, value: Path | None) -> None:
        self.state.project_path = value

    @property
    def _project_dirty(self) -> bool:
        return self.state.dirty

    @_project_dirty.setter
    def _project_dirty(self, value: bool) -> None:
        self.state.dirty = value

    def _x_min(self) -> float:
        return self.state.x_min()

    def _x_max(self) -> float:
        return self.state.x_max()

    def _y_min(self) -> float:
        return self.state.y_min()

    def _y_max(self) -> float:
        return self.state.y_max()

    def _floor_plan_extent(self) -> list[float]:
        """Rectángulo en metros del plano calibrado (no del lienzo expandido)."""
        return self.state.floor_plan_extent()

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
            extent=self._floor_plan_extent(),
            origin="upper",
            aspect="equal",
            zorder=0,
            interpolation="bilinear",
        )

    def _draw_chrome(self) -> None:
        """Fondos fijos de paneles laterales y barra inferior."""
        for patch in self._chrome_patches:
            try:
                patch.remove()
            except ValueError:
                pass
        self._chrome_patches = []

        specs = (
            (0.006, 0.125, 0.138, 0.70),  # panel AP (estrecho)
            (0.856, 0.125, 0.138, 0.70),  # panel info (estrecho)
            (0.006, 0.004, 0.988, 0.128),  # barra de acciones
        )
        for x, y, w, h in specs:
            patch = FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0.008,rounding_size=0.012",
                transform=self.fig.transFigure,
                facecolor=UI_PANEL_BG,
                edgecolor=UI_PANEL_EDGE,
                linewidth=1.0,
                alpha=0.98,
                zorder=1,
                clip_on=False,
            )
            self.fig.add_artist(patch)
            self._chrome_patches.append(patch)

    def _apply_cartesian_style(self) -> None:
        """Plano cartesiano con (0,0) en el centro y cotas en los bordes."""
        self.ax.set_xlim(self._x_min(), self._x_max())
        self.ax.set_ylim(self._y_min(), self._y_max())
        self.ax.set_aspect("equal", adjustable="box")
        self.ax.set_xlabel("X (metros)", labelpad=4, color=UI_MUTED, fontsize=9)
        self.ax.set_ylabel("Y (metros)", labelpad=4, color=UI_MUTED, fontsize=9)
        self.ax.set_facecolor("#FBFCFD")
        grid_alpha = 0.22 if self.floor_plan_array is not None else 0.38
        self.ax.grid(True, which="major", linestyle="--", alpha=grid_alpha, zorder=2)
        # Cruz del origen (sin poner las cotas numéricas encima del centro)
        self.ax.axhline(0, color="#94A3B8", linewidth=1.0, zorder=3)
        self.ax.axvline(0, color="#94A3B8", linewidth=1.0, zorder=3)

        # Ejes y números de metros siempre en las orillas de la imagen
        for spine in self.ax.spines.values():
            spine.set_visible(True)
            spine.set_color(UI_PANEL_EDGE)
            spine.set_linewidth(1.0)
            spine.set_position(("outward", 0))

        self.ax.xaxis.set_ticks_position("both")
        self.ax.yaxis.set_ticks_position("both")
        self.ax.tick_params(
            axis="both",
            which="major",
            labelsize=8,
            direction="out",
            length=4,
            labelbottom=True,
            labeltop=True,
            labelleft=True,
            labelright=True,
            colors=UI_MUTED,
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
            fontsize=7.5,
            color=UI_MUTED,
            fontweight="bold",
            bbox={
                "boxstyle": "round,pad=0.2",
                "facecolor": UI_PANEL_BG,
                "edgecolor": UI_PANEL_EDGE,
                "alpha": 0.95,
            },
            arrowprops={
                "arrowstyle": "-",
                "color": UI_PANEL_EDGE,
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
        self._ap_scatter_estimated = None
        self._ap_annotations = []
        self.origin_plot = None
        self.origin_label = None
        self.floor_plan_artist = None
        self._draw_floor_plan()
        self._apply_cartesian_style()
        self._redraw_points()
        self._redraw_ap_markers()

    def _apply_interface_status(self, status: InterfaceStatus) -> None:
        """Aplica un InterfaceStatus ya leído (hilo UI)."""
        self.connected = status.connected
        self.ssid = status.ssid if status.connected and status.ssid else "?"
        self.bssid = status.bssid if status.connected else ""
        self.signal_pct = status.signal_pct if status.connected else None
        self.channel = status.channel if status.connected else None
        self.band = status.band if status.connected else ""

    def _clear_connection_info(self) -> None:
        self.connected = False
        self.ssid = "?"
        self.bssid = ""
        self.signal_pct = None
        self.channel = None
        self.band = ""

    def _refresh_connection_info(self) -> None:
        """Actualiza SSID/BSSID (bloqueante; solo para arranque o acciones puntuales)."""
        self._apply_interface_status(get_interface_status())

    def _figure_alive(self) -> bool:
        try:
            return bool(plt.fignum_exists(self.fig.number))
        except Exception:
            return False

    def _connection_snapshot(self) -> tuple[bool, str, str]:
        return (self.connected, self.ssid, normalize_bssid(self.bssid))

    def _ap_list_view_snapshot(self) -> tuple:
        """Estado visual de la lista lateral (para evitar redibujos innecesarios)."""
        current = normalize_bssid(self.bssid) if self.connected and self.bssid else ""
        rows = tuple(
            (
                normalize_bssid(ap.bssid),
                ap.signal_pct,
                ap.band,
                ap.channel,
                normalize_bssid(ap.bssid) == current,
            )
            for ap in self.visible_aps
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
            key = normalize_bssid(self.bssid)
            assoc_pct = self.signal_pct if self.signal_pct is not None else 0
            by_key: dict[str, ScannedBssid] = {
                normalize_bssid(ap.bssid): ap for ap in self.visible_aps
            }
            if key not in by_key:
                by_key[key] = ScannedBssid(
                    bssid=self.bssid,
                    signal_pct=assoc_pct,
                    channel=self.channel,
                    band=self.band,
                )
            else:
                listed = by_key[key]
                by_key[key] = ScannedBssid(
                    bssid=listed.bssid,
                    signal_pct=max(listed.signal_pct, assoc_pct),
                    channel=self.channel if self.channel is not None else listed.channel,
                    band=self.band or listed.band,
                    radio_type=listed.radio_type,
                )
            # Mantener asociados siempre; descartar otros por debajo del umbral
            filtered = [
                ap
                for ap in by_key.values()
                if normalize_bssid(ap.bssid) == key
                or ap.signal_pct >= NEARBY_AP_MIN_SIGNAL_PCT
            ]
            self.visible_aps = sorted(
                filtered,
                key=lambda ap: (
                    0 if normalize_bssid(ap.bssid) == key else 1,
                    -ap.signal_pct,
                    normalize_bssid(ap.bssid),
                ),
            )
        after = self._ap_list_view_snapshot()
        if after != before:
            self._ap_list_snapshot = after
            return True
        return False

    def _apply_scanned_aps(
        self,
        found: list[ScannedBssid],
        *,
        ssid: str,
        force: bool = False,
    ) -> bool:
        """Fusiona un escaneo BSSID en la lista lateral. Devuelve si cambió."""
        before = self._ap_list_view_snapshot()
        assoc_key = (
            normalize_bssid(self.bssid)
            if self.bssid
            else ""
        )
        by_key: dict[str, ScannedBssid] = {}
        for ap in found:
            key = normalize_bssid(ap.bssid)
            # AP asociado siempre; el resto solo si señal ≥ umbral
            if key != assoc_key and ap.signal_pct < NEARBY_AP_MIN_SIGNAL_PCT:
                continue
            prev = by_key.get(key)
            if prev is None or ap.signal_pct > prev.signal_pct:
                by_key[key] = ap

        # Incluir siempre el AP asociado, aunque el escaneo aún no lo liste
        if self.bssid:
            key = normalize_bssid(self.bssid)
            assoc_pct = self.signal_pct if self.signal_pct is not None else 0
            if key not in by_key:
                by_key[key] = ScannedBssid(
                    bssid=self.bssid,
                    signal_pct=assoc_pct,
                    channel=self.channel,
                    band=self.band,
                )
            else:
                listed = by_key[key]
                by_key[key] = ScannedBssid(
                    bssid=listed.bssid,
                    signal_pct=max(listed.signal_pct, assoc_pct),
                    channel=self.channel if self.channel is not None else listed.channel,
                    band=self.band or listed.band,
                    radio_type=listed.radio_type,
                )

        # Conectado primero; luego el resto por señal (solo ≥ umbral ya filtrados)
        def _ap_sort_key(ap: ScannedBssid) -> tuple:
            is_assoc = normalize_bssid(ap.bssid) == assoc_key
            return (0 if is_assoc else 1, -ap.signal_pct, normalize_bssid(ap.bssid))

        self.visible_aps = sorted(by_key.values(), key=_ap_sort_key)
        self._visible_aps_ssid = ssid
        after = self._ap_list_view_snapshot()
        changed = after != before or force
        if changed:
            self._ap_list_snapshot = after
        return changed

    def _request_ap_scan(self, *, force: bool = False) -> None:
        """Lanza escaneo BSSID en segundo plano (no bloquea la UI)."""
        if self._jobs is None or self._ap_scan_busy or self._measuring:
            return
        if not self.connected or self.ssid in {"?", ""}:
            before = self._ap_list_view_snapshot()
            self.visible_aps = []
            self._visible_aps_ssid = ""
            after = self._ap_list_view_snapshot()
            if after != before or force:
                self._ap_list_snapshot = after
                self._update_title()
                self.fig.canvas.draw_idle()
            return

        ssid = self.ssid
        self._ap_scan_busy = True

        def work() -> list[ScannedBssid]:
            try:
                return scan_bssids_for_ssid(ssid)
            except WifiScannerError:
                return []

        def on_done(found: list[ScannedBssid]) -> None:
            self._ap_scan_busy = False
            if not self._figure_alive() or self._measuring:
                return
            # Resultado obsoleto si cambió la red mientras escaneaba
            if ssid.casefold() != self.ssid.casefold():
                return
            if self._apply_scanned_aps(found, ssid=ssid, force=force):
                self._update_title()
                self.fig.canvas.draw_idle()

        def on_error(_exc: BaseException) -> None:
            self._ap_scan_busy = False

        self._jobs.submit(work, on_done=on_done, on_error=on_error)

    def _refresh_visible_aps(self, *, force: bool = False) -> bool:
        """
        Compatibilidad: solicita escaneo async.
        Ya no bloquea; el redibujo ocurre al terminar el job.
        """
        self._request_ap_scan(force=force)
        return False

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

    def _mark_project_dirty(self) -> None:
        self.state.mark_dirty()

    def _start_autosave(self) -> None:
        """Guarda el .wifimap cada minuto si hay proyecto y cambios pendientes."""
        if self._autosave_timer is not None:
            return
        timer = self.fig.canvas.new_timer(interval=AUTOSAVE_INTERVAL_MS)
        timer.add_callback(self._autosave_tick)
        self._autosave_timer = timer
        timer.start()

    def _stop_autosave(self) -> None:
        timer = self._autosave_timer
        self._autosave_timer = None
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

    def _autosave_tick(self) -> None:
        if self.project_path is None or not self._project_dirty:
            return
        if self._measuring:
            return
        self.save_project(ask_path=False, autosave=True)

    def _write_project_file(self, path: Path) -> Path:
        previous = self.state.project_path
        self.state.project_path = path
        try:
            payload = self.state.to_project_dict(
                heatmap_visible=self.heatmap_mesh is not None
            )
            return save_project_file(path, payload)
        finally:
            self.state.project_path = previous

    def _go_back_to_network_picker(self) -> None:
        """Cierra el mapa y vuelve a la pantalla de elegir red."""
        if self._measuring:
            self.status = "Espera a que termine la medición antes de cambiar de red."
            self._refresh()
            return
        self.return_to_picker = True
        self.status = "Volviendo a elegir red…"
        print("Volviendo a la pantalla de elegir red…")
        plt.close(self.fig)

    def _on_close(self, _event) -> None:
        self._stop_connection_monitor()
        self._stop_autosave()
        if self._jobs is not None:
            self._jobs.close()
            self._jobs = None
        # Último intento de no perder trabajo al cerrar (sin refrescar la UI)
        if self.project_path is not None and self._project_dirty and not self._measuring:
            try:
                saved = self._write_project_file(self.project_path)
                self.project_path = saved
                self._project_dirty = False
                print(f"Autoguardado al cerrar: {saved}")
            except Exception as exc:
                print(f"No se pudo autoguardar al cerrar: {exc}")

    def _poll_connection_change(self) -> None:
        """Detecta cambio de SSID/AP en segundo plano (no bloquea el canvas)."""
        if self._measuring or self._poll_busy or self._jobs is None:
            return
        self._poll_busy = True
        before = self._connection_snapshot()

        def work() -> InterfaceStatus | None:
            try:
                return get_interface_status()
            except WifiScannerError:
                return None

        def on_done(status: InterfaceStatus | None) -> None:
            self._poll_busy = False
            if not self._figure_alive() or self._measuring:
                return
            if status is None:
                self._clear_connection_info()
            else:
                self._apply_interface_status(status)
            after = self._connection_snapshot()
            connection_changed = after != before

            if connection_changed:
                prev_ssid, prev_bssid = before[1], before[2]
                if self.connected and self.ssid not in {"?", ""}:
                    ap = self.bssid or "desconocido"
                    band_txt = f" · {self.band}" if self.band else ""
                    if prev_ssid not in {"?", ""} and prev_ssid != self.ssid:
                        self.status = (
                            f"Cambio de red: {prev_ssid} → {self.ssid}  "
                            f"·  AP: {ap}{band_txt}"
                        )
                    elif prev_bssid and prev_bssid != normalize_bssid(self.bssid):
                        self.status = (
                            f"Cambio de AP en «{self.ssid}»: ahora {ap}{band_txt}"
                        )
                    else:
                        self.status = f"Conectado a «{self.ssid}»  ·  AP: {ap}"
                    print(self._connection_label())
                else:
                    self.status = "Se perdió la conexión WiFi activa"
                    print(self.status)

                self._sync_current_ap_into_list()
                self._redraw_ap_markers()
                self._update_title()
                self.fig.canvas.draw_idle()

            self._ap_scan_counter += 1
            should_scan = (
                connection_changed
                or self._ap_scan_counter >= AP_SCAN_EVERY_N_POLLS
                or (self.connected and not self.visible_aps)
            )
            if should_scan:
                self._ap_scan_counter = 0
                self._request_ap_scan()

        def on_error(_exc: BaseException) -> None:
            self._poll_busy = False

        self._jobs.submit(work, on_done=on_done, on_error=on_error)

    def _connection_label(self) -> str:
        if self.connected and self.ssid not in {"?", ""}:
            ap = self.bssid or "desconocido"
            return f"●  {self.ssid}   ·   AP {ap}"
        return "○  Sin conexión WiFi activa"

    def _draw_connection_indicator(self) -> None:
        """Barra fija bajo el título con la red y el AP asociados."""
        if self._connection_artist is not None:
            try:
                self._connection_artist.remove()
            except ValueError:
                pass
        color = UI_CONNECTED if self.connected else UI_DISCONNECTED
        soft = UI_CONNECTED_SOFT if self.connected else UI_DISCONNECTED_SOFT
        self._connection_artist = self.fig.text(
            0.50,
            0.955,
            self._connection_label(),
            ha="center",
            va="top",
            fontsize=10,
            fontweight="bold",
            color=color,
            bbox={
                "boxstyle": "round,pad=0.38",
                "facecolor": soft,
                "edgecolor": color,
                "linewidth": 1.1,
                "alpha": 0.98,
            },
            zorder=30,
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
        self._draw_dimensions_inputs()

        # Centro del panel izquierdo (chrome: x=0.006, w=0.138)
        panel_x = 0.075
        top_y = 0.80
        line_h = 0.034

        title = "AP"
        if self.connected and self.ssid not in {"?", ""}:
            title = f"AP · {self.ssid}"
        title_artist = self.fig.text(
            panel_x,
            top_y,
            title,
            ha="center",
            va="top",
            fontsize=12,
            fontweight="bold",
            color=UI_TEXT,
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._ap_list_artists.append(title_artist)

        subtitle = self.fig.text(
            panel_x,
            top_y - 0.024,
            f"● conectado · resto ≥{NEARBY_AP_MIN_SIGNAL_PCT}%",
            ha="center",
            va="top",
            fontsize=7.5,
            color=UI_MUTED,
            transform=self.fig.transFigure,
            linespacing=1.15,
            zorder=20,
        )
        self._ap_list_artists.append(subtitle)

        y = top_y - 0.048
        if not self.connected or self.ssid in {"?", ""}:
            empty = self.fig.text(
                panel_x,
                y,
                "Sin red activa.\nConéctate para\nver los AP.",
                ha="center",
                va="top",
                fontsize=10,
                color=UI_MUTED,
                transform=self.fig.transFigure,
                linespacing=1.25,
                zorder=20,
            )
            self._ap_list_artists.append(empty)
            return

        if not self.visible_aps:
            empty = self.fig.text(
                panel_x,
                y,
                "Buscando AP…",
                ha="center",
                va="top",
                fontsize=10,
                color=UI_MUTED,
                transform=self.fig.transFigure,
                zorder=20,
            )
            self._ap_list_artists.append(empty)
            return

        current = normalize_bssid(self.bssid) if self.bssid else ""
        # Lista ya filtrada: AP conectado + otros con señal ≥ NEARBY_AP_MIN_SIGNAL_PCT
        display_aps = [
            ap
            for ap in self.visible_aps
            if normalize_bssid(ap.bssid) == current
            or ap.signal_pct >= NEARBY_AP_MIN_SIGNAL_PCT
        ]
        max_rows = 8 if self.floor_plan_array is None else 12
        for ap in display_aps[:max_rows]:
            is_current = normalize_bssid(ap.bssid) == current
            color = AP_MARKER_ACTIVE if is_current else UI_TEXT
            weight = "bold" if is_current else "normal"
            short = self._short_bssid(ap.bssid)
            bar = self._signal_bar(ap.signal_pct, width=6)
            band_txt = ap.band or "?"
            ch_txt = f" ch{ap.channel}" if ap.channel is not None else ""
            if is_current:
                label = f"● {short}  {ap.signal_pct}%\n{bar}\n{band_txt}{ch_txt}"
            else:
                label = f"○ {short}  {ap.signal_pct}%\n{bar}\n{band_txt}{ch_txt}"

            row = self.fig.text(
                panel_x,
                y,
                label,
                ha="center",
                va="top",
                fontsize=10,
                fontweight=weight,
                color=color,
                family="monospace",
                transform=self.fig.transFigure,
                linespacing=1.2,
                bbox={
                    "boxstyle": "round,pad=0.20",
                    "facecolor": "#FFF4EB" if is_current else UI_CARD_BG,
                    "edgecolor": AP_MARKER_ACTIVE if is_current else UI_CARD_EDGE,
                    "linewidth": 1.0 if is_current else 0.7,
                    "alpha": 1.0,
                },
                zorder=20,
            )
            self._ap_list_artists.append(row)
            y -= line_h * (1.55 if is_current else 1.45)

        remaining = len(display_aps) - max_rows
        if remaining > 0:
            more = self.fig.text(
                panel_x,
                y,
                f"+{remaining} más…",
                ha="center",
                va="top",
                fontsize=9,
                color=UI_MUTED,
                transform=self.fig.transFigure,
                zorder=20,
            )
            self._ap_list_artists.append(more)

    def _clear_dimensions_inputs(self) -> None:
        """Remueve los inputs y el botón de dimensiones del panel izquierdo."""
        for artist in getattr(self, "_dims_label_artists", []):
            try:
                artist.remove()
            except ValueError:
                pass
        self._dims_label_artists = []

        for attr_prefix in ("_width_input", "_height_input", "_apply_dims_btn"):
            ax_name = f"{attr_prefix}_ax"
            widget_name = attr_prefix if attr_prefix == "_apply_dims_btn" else f"{attr_prefix}_textbox"
            
            widget = getattr(self, widget_name, None)
            if widget is not None:
                setattr(self, widget_name, None)
                
            ax = getattr(self, ax_name, None)
            if ax is not None:
                try:
                    self.fig.delaxes(ax)
                except ValueError:
                    pass
                setattr(self, ax_name, None)

    def _draw_dimensions_inputs(self) -> None:
        """Dibuja los campos de texto y botón para modificar dimensiones del plano si no hay imagen."""
        if self.floor_plan_array is not None:
            self._clear_dimensions_inputs()
            return

        if getattr(self, "_width_textbox", None) is not None:
            return

        self._dims_label_artists = []
        
        label_sec = self.fig.text(
            0.075,
            0.38,
            "Medidas Plano",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
            color=UI_TEXT,
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._dims_label_artists.append(label_sec)

        label_w = self.fig.text(
            0.075,
            0.34,
            "Ancho X (metros):",
            ha="center",
            va="bottom",
            fontsize=8.5,
            color=UI_MUTED,
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._dims_label_artists.append(label_w)

        self._width_input_ax = self.fig.add_axes([0.02, 0.30, 0.11, 0.038])
        self._width_input_ax.set_zorder(50)
        self._width_textbox = TextBox(
            self._width_input_ax,
            "",
            initial=f"{self.width_m:.1f}",
            color="#F1F5F9",
            hovercolor="#E2E8F0",
        )
        self._width_textbox.label.set_visible(False)
        self._width_textbox.text_disp.set_fontsize(9.5)
        self._width_textbox.text_disp.set_color("#0F172A")
        self._width_textbox.text_disp.set_fontweight("bold")
        for spine in self._width_input_ax.spines.values():
            spine.set_color(UI_PANEL_EDGE)
            spine.set_linewidth(1.0)

        label_h = self.fig.text(
            0.075,
            0.26,
            "Largo Y (metros):",
            ha="center",
            va="bottom",
            fontsize=8.5,
            color=UI_MUTED,
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._dims_label_artists.append(label_h)

        self._height_input_ax = self.fig.add_axes([0.02, 0.22, 0.11, 0.038])
        self._height_input_ax.set_zorder(50)
        self._height_textbox = TextBox(
            self._height_input_ax,
            "",
            initial=f"{self.height_m:.1f}",
            color="#F1F5F9",
            hovercolor="#E2E8F0",
        )
        self._height_textbox.label.set_visible(False)
        self._height_textbox.text_disp.set_fontsize(9.5)
        self._height_textbox.text_disp.set_color("#0F172A")
        self._height_textbox.text_disp.set_fontweight("bold")
        for spine in self._height_input_ax.spines.values():
            spine.set_color(UI_PANEL_EDGE)
            spine.set_linewidth(1.0)

        self._apply_dims_btn_ax = self.fig.add_axes([0.02, 0.14, 0.11, 0.045])
        self._apply_dims_btn_ax.set_zorder(50)
        self._apply_dims_btn = Button(
            self._apply_dims_btn_ax,
            "Aceptar",
            color="#E2E8F0",
            hovercolor="#CBD5E1",
        )
        self._apply_dims_btn.label.set_fontsize(9.5)
        self._apply_dims_btn.label.set_color("#0F172A")
        self._apply_dims_btn.label.set_fontweight("bold")
        self._apply_dims_btn.on_clicked(self._on_apply_dimensions)

    def _on_apply_dimensions(self, _event=None) -> None:
        """Aplica las dimensiones ingresadas por el usuario en el panel izquierdo."""
        try:
            w_str = self._width_textbox.text.strip()
            h_str = self._height_textbox.text.strip()
            w = float(w_str)
            h = float(h_str)
        except ValueError:
            import tkinter.messagebox as messagebox
            messagebox.showerror(
                "Error de formato",
                "Por favor, ingresa valores numéricos válidos para el ancho y largo."
            )
            return

        if w < 5.0 or w > 500.0 or h < 5.0 or h > 500.0:
            import tkinter.messagebox as messagebox
            messagebox.showerror(
                "Dimensiones inválidas",
                "Las dimensiones deben estar entre 5.0 y 500.0 metros."
            )
            return

        self.width_m = w
        self.height_m = h
        self.plan_width_m = w
        self.plan_length_m = h
        self._mark_project_dirty()

        had_heatmap = self.heatmap_mesh is not None and len(self.samples) >= 3
        self._setup_axes()
        if had_heatmap:
            self._apply_heatmap()
        
        self.status = f"Medidas del plano actualizadas a {w:.1f}x{h:.1f} m."
        self._refresh(reset_view=True)


    def _draw_help(self) -> None:
        """Barra inferior: botones de acción con la tecla debajo de cada uno."""
        self._draw_action_toolbar()
        # Paneles estrechos → más protagonismo al mapa
        self.fig.subplots_adjust(bottom=0.168, left=0.165, right=0.835, top=0.86)

    def _clear_action_toolbar(self) -> None:
        for artist in self._toolbar_key_artists:
            try:
                artist.remove()
            except ValueError:
                pass
        self._toolbar_key_artists = []
        if self._help_artist is not None:
            try:
                self._help_artist.remove()
            except ValueError:
                pass
            self._help_artist = None
        for ax in self._toolbar_axes:
            try:
                self.fig.delaxes(ax)
            except ValueError:
                pass
        self._toolbar_axes = []
        self._toolbar_buttons = []

    def _draw_action_toolbar(self) -> None:
        """Botones en el pie; debajo de cada uno, la tecla equivalente."""
        if self._toolbar_buttons:
            return

        self._clear_action_toolbar()

        # Borde azul de los botones (mismo tono que AP estimado / acento UI)
        btn_border = "#2563EB"
        btn_face = "#EFF6FF"
        btn_hover = "#DBEAFE"

        # (icono, etiqueta, tecla, callback) — icono arriba, texto abajo
        actions: list[tuple[str, str, str, object]] = [
            ("▲", "AP", "a", self._action_mark_ap),
            ("▦", "Plano", "p", self.load_floor_plan),
            ("✕", "Quitar", "c", self.clear_floor_plan),
            ("＋", "Expandir", "e", lambda: self.expand(width=True, height=True)),
            ("↔", "+Ancho", "w", lambda: self.expand(width=True, height=False)),
            ("↕", "+Alto", "r", lambda: self.expand(width=False, height=True)),
            ("▣", "Heatmap", "h", self._action_heatmap),
            ("⬇", "Guardar", "g", lambda: self.save_project(ask_path=self.project_path is None)),
            ("⇩", "G. como", "Ctrl+g", lambda: self.save_project(ask_path=True)),
            ("▸", "Abrir", "o", self.open_project),
            ("▤", "PNG", "s", self.save),
            ("↶", "Deshacer", "z", self.undo),
            ("?", "Ayuda", "F1", self.show_usage_guide),
            ("■", "Salir", "q", lambda: plt.close(self.fig)),
        ]

        n = len(actions)
        left = 0.012
        right = 0.988
        gap = 0.004
        usable = right - left - gap * (n - 1)
        btn_w = usable / n
        btn_h = 0.056
        btn_y = 0.040
        key_y = 0.016

        self._help_artist = self.fig.text(
            0.012,
            0.112,
            "Clic = medir / info   ·   clic der. = quitar",
            ha="left",
            va="center",
            fontsize=7.5,
            color=UI_MUTED,
            zorder=25,
        )

        for i, (icon, label, key_hint, callback) in enumerate(actions):
            x = left + i * (btn_w + gap)
            ax = self.fig.add_axes([x, btn_y, btn_w, btn_h])
            ax.set_zorder(40)
            btn = Button(
                ax,
                f"{icon}\n{label}",
                color=btn_face,
                hovercolor=btn_hover,
            )
            btn.label.set_fontsize(7.2)
            btn.label.set_color(UI_TEXT)
            btn.label.set_fontweight("bold")
            btn.label.set_linespacing(1.15)
            # Borde azul (después del Button, que toca el frame del axes)
            ax.patch.set_edgecolor(btn_border)
            ax.patch.set_linewidth(1.8)
            for spine in ax.spines.values():
                spine.set_visible(True)
                spine.set_color(btn_border)
                spine.set_linewidth(1.8)
            # Evitar captura tardía incorrecta en el bucle
            btn.on_clicked(lambda _event, cb=callback: cb())
            self._toolbar_axes.append(ax)
            self._toolbar_buttons.append(btn)

            key_artist = self.fig.text(
                x + btn_w / 2.0,
                key_y,
                key_hint,
                ha="center",
                va="center",
                fontsize=7,
                color=btn_border,
                fontweight="bold",
                zorder=25,
            )
            self._toolbar_key_artists.append(key_artist)

    def _action_mark_ap(self) -> None:
        """Activa/cancela el modo marcar ubicación del AP asociado."""
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

    def _action_heatmap(self) -> None:
        """Genera o actualiza el mapa de calor si hay puntos suficientes."""
        if len(self.samples) < 3:
            self.status = "Se necesitan al menos 3 puntos para el heatmap."
            self._refresh()
            return
        self._apply_heatmap()

    def show_usage_guide(self) -> None:
        """Abre la guía de uso (diálogo Tk)."""
        open_usage_guide()

    def _update_title(self) -> None:
        n = len(self.samples)
        muestras = "muestra" if n == 1 else "muestras"
        size = f"{self.width_m:.0f}×{self.height_m:.0f} m"
        ssid = self.ssid if self.ssid not in {"?", ""} else "sin red"
        self.ax.set_title(
            f"{ssid}  ·  {n} {muestras}  ·  {size}",
            fontsize=12,
            fontweight="bold",
            color=UI_TEXT,
            pad=10,
        )

        if self._meta_artist is not None:
            try:
                self._meta_artist.remove()
            except ValueError:
                pass
            self._meta_artist = None
        extras = []
        if self.project_path is not None:
            extras.append(self.project_path.name)
        if self.floor_plan_path is not None:
            extras.append(self.floor_plan_path.name)
        if extras:
            self._meta_artist = self.fig.text(
                0.50,
                0.905,
                "  ·  ".join(extras),
                ha="center",
                va="top",
                fontsize=8,
                color=UI_MUTED,
                zorder=30,
            )

        if self._status_artist is not None:
            try:
                self._status_artist.remove()
            except ValueError:
                pass
            self._status_artist = None
        if self.status and self._inspected_sample is None:
            self._status_artist = self.fig.text(
                0.50,
                0.875,
                self.status,
                ha="center",
                va="top",
                fontsize=8.5,
                color=UI_TEXT,
                bbox={
                    "boxstyle": "round,pad=0.28",
                    "facecolor": UI_SOFT,
                    "edgecolor": UI_PANEL_EDGE,
                    "linewidth": 0.8,
                    "alpha": 0.96,
                },
                zorder=30,
            )

        self._draw_connection_indicator()
        self._draw_ap_list_panel()
        self._draw_point_info_panel()

    def _sample_edge_color(self, sample: SamplePoint) -> str:
        """Azul si el AP de la medición no tiene marca en el mapa; negro si sí."""
        key = normalize_bssid(sample.bssid or "")
        if not key or key in {"sin-señal", "desconocido"}:
            return "black"
        if key in self.ap_locations or any(
            normalize_bssid(k) == key for k in self.ap_locations
        ):
            return "black"
        return UNMARKED_AP_EDGE

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
            edges = [self._sample_edge_color(s) for s in with_signal]
            self.scatter = self.ax.scatter(
                [s.x_m for s in with_signal],
                [s.y_m for s in with_signal],
                c=[s.signal_pct for s in with_signal],
                cmap="RdYlGn",
                vmin=0,
                vmax=100,
                edgecolors=edges,
                linewidths=[
                    1.6 if e == UNMARKED_AP_EDGE else 0.8 for e in edges
                ],
                s=70,
                zorder=5,
            )
        if no_signal:
            # Violeta: sin cobertura / señal perdida (0%)
            edges = [self._sample_edge_color(s) for s in no_signal]
            self.scatter_no_signal = self.ax.scatter(
                [s.x_m for s in no_signal],
                [s.y_m for s in no_signal],
                c=NO_SIGNAL_COLOR,
                edgecolors=edges,
                linewidths=[
                    1.8 if e == UNMARKED_AP_EDGE else 1.0 for e in edges
                ],
                s=90,
                marker="X",
                zorder=5,
            )

    def _clear_ap_artists(self) -> None:
        for attr in (
            "_ap_scatter_active",
            "_ap_scatter_other",
            "_ap_scatter_estimated",
        ):
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
        # Tres octetos finales para distinguir radios casi iguales (…17:1c:db vs …18:1c:db)
        parts = bssid.replace("-", ":").split(":")
        if len(parts) >= 3:
            return f"…{parts[-3]}:{parts[-2]}:{parts[-1]}"
        if len(parts) >= 2:
            return f"…{parts[-2]}:{parts[-1]}"
        return bssid[-8:] if len(bssid) > 8 else bssid

    def _is_estimable_bssid(self, bssid: str) -> bool:
        key = normalize_bssid(bssid or "")
        return bool(key) and key not in {"sin-señal", "desconocido"}

    def _collect_ap_signal_observations(
        self,
    ) -> dict[str, list[tuple[float, float, float, str, str]]]:
        """
        Por BSSID: lecturas (x, y, señal%, bssid, ssid) desde el AP conectado
        y desde nearby_aps en cada medición.
        """
        obs: dict[str, list[tuple[float, float, float, str, str]]] = {}
        for sample in self.samples:
            if self._is_estimable_bssid(sample.bssid) and sample.signal_pct > 0:
                key = normalize_bssid(sample.bssid)
                obs.setdefault(key, []).append(
                    (
                        float(sample.x_m),
                        float(sample.y_m),
                        float(sample.signal_pct),
                        sample.bssid,
                        sample.ssid or "",
                    )
                )
            for nearby in sample.nearby_aps:
                if not self._is_estimable_bssid(nearby.bssid):
                    continue
                if nearby.signal_pct <= 0:
                    continue
                key = normalize_bssid(nearby.bssid)
                obs.setdefault(key, []).append(
                    (
                        float(sample.x_m),
                        float(sample.y_m),
                        float(nearby.signal_pct),
                        nearby.bssid,
                        sample.ssid or "",
                    )
                )
        return obs

    def _estimate_unmarked_ap_locations(self) -> list[ApLocation]:
        """
        Estima posición de APs aún no marcados a mano.

        Mejoras sobre el centroide simple original:
        - Mejora 2: usa solo las top-N lecturas de señal más alta, descartando
          mediciones lejanas o ruidosas que sesgan el resultado.
        - Mejora 3: convierte señal% → distancia estimada con modelo Log-Distance
          (física de propagación WiFi) y pondera por 1/distancia² (IDW),
          en lugar de señal^power que era un proxy sin base física.
        """
        marked = {normalize_bssid(k) for k in self.ap_locations}
        estimated: list[ApLocation] = []
        for key, readings in self._collect_ap_signal_observations().items():
            if key in marked:
                continue
            if len(readings) < AP_ESTIMATE_MIN_READINGS:
                continue

            # Mejora 2: filtrar las top-N lecturas de señal más alta.
            # Las lecturas de señal baja corresponden a puntos lejos del AP
            # y tienen gran incertidumbre por multipath y obstáculos.
            top_readings = sorted(readings, key=lambda r: -r[2])[:AP_ESTIMATE_TOP_N_READINGS]

            # Mejora 3: convertir señal% → distancia estimada y ponderar por IDW.
            # Modelo Log-Distance: RSSI(dBm) = RSSI_ref - 10*n*log10(d)
            # → d = 10^((RSSI_ref - RSSI) / (10*n))
            # Windows reporta señal como % (0–100), conversión aproximada:
            # RSSI(dBm) ≈ (signal_pct / 2) - 100   (−0% → -100 dBm, 100% → -50 dBm)
            weights: list[float] = []
            for _, _, pct, _, _ in top_readings:
                rssi_dbm = (max(pct, 1) / 2.0) - 100.0
                dist_m = 10.0 ** (
                    (AP_ESTIMATE_RSSI_REF_DBM - rssi_dbm) / (10.0 * AP_ESTIMATE_PATH_LOSS_N)
                )
                dist_m = max(dist_m, 0.1)  # evitar división por cero en señal=100%
                weights.append(1.0 / dist_m ** 2)

            weights_arr = np.array(weights, dtype=float)
            wsum = float(weights_arr.sum())
            if wsum <= 0:
                continue

            xs = np.array([r[0] for r in top_readings], dtype=float)
            ys = np.array([r[1] for r in top_readings], dtype=float)
            x = float(np.sum(weights_arr * xs) / wsum)
            y = float(np.sum(weights_arr * ys) / wsum)

            best = max(readings, key=lambda r: r[2])
            estimated.append(
                ApLocation(
                    x_m=round(x, 3),
                    y_m=round(y, 3),
                    bssid=best[3],
                    ssid=best[4] or "?",
                )
            )
        estimated.sort(key=lambda ap: normalize_bssid(ap.bssid))
        return estimated

    def _redraw_ap_markers(self) -> None:
        """Dibuja AP marcados a mano y estimados (solo si aún no están marcados)."""
        self._clear_ap_artists()

        current = normalize_bssid(self.bssid) if self.connected and self.bssid else ""
        active = [
            ap
            for key, ap in self.ap_locations.items()
            if normalize_bssid(key) == current
        ]
        others = [
            ap
            for key, ap in self.ap_locations.items()
            if normalize_bssid(key) != current
        ]
        estimated = self._estimate_unmarked_ap_locations()

        if estimated:
            self._ap_scatter_estimated = self.ax.scatter(
                [ap.x_m for ap in estimated],
                [ap.y_m for ap in estimated],
                facecolors="none",
                edgecolors=AP_MARKER_ESTIMATED,
                marker="^",
                s=260,
                linewidths=1.8,
                zorder=8,
            )
            for ap in estimated:
                ann = self.ax.annotate(
                    f"AP est. {self._short_bssid(ap.bssid)}",
                    xy=(ap.x_m, ap.y_m),
                    xytext=(8, -14),
                    textcoords="offset points",
                    fontsize=7.5,
                    color=AP_MARKER_ESTIMATED,
                    bbox={
                        "boxstyle": "round,pad=0.2",
                        "facecolor": "#EFF6FF",
                        "edgecolor": AP_MARKER_ESTIMATED,
                        "alpha": 0.92,
                    },
                    zorder=11,
                )
                self._ap_annotations.append(ann)

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
            is_current = normalize_bssid(ap.bssid) == current
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

        key = normalize_bssid(self.bssid)
        self.ap_locations[key] = ApLocation(
            x_m=round(x, 3),
            y_m=round(y, 3),
            bssid=self.bssid,
            ssid=self.ssid,
        )
        self._mark_project_dirty()
        self._placing_ap = False
        band_txt = f" · {self.band}" if self.band else ""
        self.status = (
            f"AP marcado en ({x:.1f}, {y:.1f}) — "
            f"{self.ssid} · {self.bssid}{band_txt}"
        )
        self._redraw_ap_markers()
        self._redraw_points()
        self._refresh()

    def _refresh(self, *, reset_view: bool = False) -> None:
        """
        Actualiza título/artistas.
        Por defecto conserva el zoom actual; usa reset_view=True para ver todo el lienzo.
        """
        if reset_view:
            self.ax.set_xlim(self._x_min(), self._x_max())
            self.ax.set_ylim(self._y_min(), self._y_max())
        if self.floor_plan_artist is not None:
            self.floor_plan_artist.set_extent(self._floor_plan_extent())
        if self.origin_label is not None:
            self.origin_label.set_position((0, self._y_max()))
        self._update_title()
        self.fig.canvas.draw_idle()

    def _ask_plan_dimensions_m(self, parent=None) -> tuple[float, float] | None:
        """Pregunta ancho (X) y largo (Y) reales del plano en metros."""
        return ask_plan_dimensions_m(
            initial_width=self.plan_width_m,
            initial_length=self.plan_length_m,
            parent=parent,
        )

    def _read_floor_plan_image(self, path: Path) -> np.ndarray:
        img = mpimg.imread(path)
        if img.dtype != np.float32 and img.dtype != np.float64:
            img = img.astype(float)
            if img.max() > 1.0:
                img = img / 255.0
        return img

    def load_floor_plan(self) -> None:
        """Abre un diálogo para elegir la imagen del plano y calibrar su escala."""
        path = ask_floor_plan_image_path()
        if path is None:
            self.status = "Carga de plano cancelada."
            self._refresh()
            return

        dims = self._ask_plan_dimensions_m()
        if dims is None:
            self.status = "Carga de plano cancelada (sin escala)."
            self._refresh()
            return
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
        self._mark_project_dirty()

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
        self._mark_project_dirty()
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

        # Clic derecho: quitar punto o marca de AP más cercanos
        if event.button == 3:
            self._remove_nearest_marker(x, y)
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

        if self._jobs is None:
            return

        self._measuring = True
        self.status = "Midiendo señal…"
        self._update_title()
        self.fig.canvas.draw_idle()

        fallback = self.ssid if self.ssid not in {"?", ""} else None

        def work() -> tuple[WifiSample, InterfaceStatus | None]:
            reading = measure_current_network(fallback_ssid=fallback)
            try:
                status = get_interface_status()
            except WifiScannerError:
                status = None
            return reading, status

        def on_done(result: tuple[WifiSample, InterfaceStatus | None]) -> None:
            reading, status = result
            self._complete_measurement(x, y, fallback, reading, status, error=None)

        def on_error(exc: BaseException) -> None:
            self._complete_measurement(x, y, fallback, None, None, error=exc)

        self._jobs.submit(work, on_done=on_done, on_error=on_error)

    def _complete_measurement(
        self,
        x: float,
        y: float,
        fallback: str | None,
        reading: WifiSample | None,
        status: InterfaceStatus | None,
        *,
        error: BaseException | None,
    ) -> None:
        """Aplica el resultado de una medición (siempre en hilo UI)."""
        try:
            if not self._figure_alive():
                return
            if status is not None:
                self._apply_interface_status(status)
            elif reading is not None and reading.ssid:
                self.ssid = reading.ssid

            if reading is not None and error is None:
                sample = SamplePoint(
                    x_m=round(x, 3),
                    y_m=round(y, 3),
                    signal_pct=reading.signal_pct,
                    bssid=reading.bssid,
                    ssid=reading.ssid,
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    nearby_aps=[
                        NearbyApReading(
                            bssid=ap.bssid,
                            signal_pct=ap.signal_pct,
                            channel=ap.channel,
                            band=ap.band,
                        )
                        for ap in reading.nearby_aps
                    ],
                    channel=reading.channel,
                    band=reading.band,
                )
                self.samples.append(sample)
                self._mark_project_dirty()
                if sample.signal_pct <= 0:
                    self.status = (
                        f"Punto ({sample.x_m:.1f}, {sample.y_m:.1f}) → "
                        "0% sin señal (violeta)"
                    )
                else:
                    band_txt = f" {sample.band}" if sample.band else ""
                    self.status = (
                        f"Punto ({sample.x_m:.1f}, {sample.y_m:.1f}) → "
                        f"{sample.signal_pct}%  AP conectado {sample.bssid}{band_txt}"
                    )
                    if sample.nearby_aps:
                        self.status += f"  ·  +{len(sample.nearby_aps)} AP cercanos"
                if len(self.samples) < MIN_POINTS_FOR_HEATMAP:
                    self.status += (
                        f"  ({len(self.samples)}/{MIN_POINTS_FOR_HEATMAP} "
                        "recomendados para heatmap)"
                    )
            else:
                # Error o lectura nula → punto 0% (sin cobertura)
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
                self._mark_project_dirty()
                detail = f" {error}" if error is not None else ""
                self.status = (
                    f"Punto ({sample.x_m:.1f}, {sample.y_m:.1f}) → "
                    f"0% sin señal (violeta).{detail}"
                )

            self._redraw_points()
            self._redraw_ap_markers()
            self._refresh()
        finally:
            self._measuring = False
            if self._figure_alive():
                self._request_ap_scan()

    def expand(self, width: bool = True, height: bool = True) -> None:
        if width:
            self.width_m += EXPAND_STEP_M
        if height:
            self.height_m += EXPAND_STEP_M
        self._mark_project_dirty()
        self.status = (
            f"Área: {self.width_m:.0f}×{self.height_m:.0f} m "
            f"[x∈[{self._x_min():.0f},{self._x_max():.0f}], "
            f"y∈[{self._y_min():.0f},{self._y_max():.0f}]]"
        )
        # El plano mantiene su escala calibrada; solo crece el lienzo alrededor.
        if self.heatmap_mesh is not None and len(self.samples) >= 3:
            self._apply_heatmap()
        else:
            self._refresh(reset_view=True)

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
        self._mark_project_dirty()
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

        # Centro del panel derecho (chrome: x=0.856, w=0.138)
        panel_x = 0.925
        top_y = 0.80
        title = self.fig.text(
            panel_x,
            top_y,
            "Punto",
            ha="center",
            va="top",
            fontsize=12,
            fontweight="bold",
            color=UI_TEXT,
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._point_info_artists.append(title)

        sample = self._inspected_sample
        if sample is None:
            empty = self.fig.text(
                panel_x,
                top_y - 0.032,
                "Clic en un punto\npara ver señal.",
                ha="center",
                va="top",
                fontsize=10,
                color=UI_MUTED,
                transform=self.fig.transFigure,
                linespacing=1.25,
                zorder=20,
            )
            self._point_info_artists.append(empty)
            return

        if sample.signal_pct <= 0:
            signal_txt = "0% sin señal"
            signal_color = NO_SIGNAL_COLOR
            signal_bg = "#F5EEF8"
            signal_edge = NO_SIGNAL_COLOR
        else:
            signal_txt = f"{sample.signal_pct:.0f}%"
            signal_color = UI_CONNECTED
            signal_bg = UI_CONNECTED_SOFT
            signal_edge = UI_CONNECTED

        band_line = sample.band or "?"
        if sample.channel is not None:
            band_line = f"{band_line} ch{sample.channel}"

        def _card(
            text: str,
            *,
            y: float,
            color: str = UI_TEXT,
            weight: str = "normal",
            facecolor: str = UI_CARD_BG,
            edgecolor: str = UI_CARD_EDGE,
            linewidth: float = 0.7,
        ):
            artist = self.fig.text(
                panel_x,
                y,
                text,
                ha="center",
                va="top",
                fontsize=10,
                fontweight=weight,
                color=color,
                transform=self.fig.transFigure,
                linespacing=1.15,
                bbox={
                    "boxstyle": "round,pad=0.20",
                    "facecolor": facecolor,
                    "edgecolor": edgecolor,
                    "linewidth": linewidth,
                    "alpha": 1.0,
                },
                zorder=20,
            )
            self._point_info_artists.append(artist)
            return artist

        y = top_y - 0.034
        _card(
            f"Pos\n({sample.x_m:.1f}, {sample.y_m:.1f})",
            y=y,
            weight="bold",
        )
        y -= 0.058
        _card(
            f"Señal\n{signal_txt}",
            y=y,
            color=signal_color,
            weight="bold",
            facecolor=signal_bg,
            edgecolor=signal_edge,
            linewidth=1.0,
        )
        y -= 0.058
        _card(f"AP\n{self._short_bssid(sample.bssid or '—')}", y=y)
        y -= 0.058
        _card(f"Banda\n{band_line}", y=y)
        y -= 0.058
        _card(f"Red\n{sample.ssid or '?'}", y=y)
        y -= 0.058
        fecha = (sample.timestamp or "—").replace("T", " ")
        if len(fecha) > 16:
            fecha = fecha[:16]
        _card(f"Fecha\n{fecha}", y=y, color=UI_MUTED)
        y -= 0.062

        nearby_title = self.fig.text(
            panel_x,
            y,
            "Otros AP",
            ha="center",
            va="top",
            fontsize=12,
            fontweight="bold",
            color=UI_TEXT,
            transform=self.fig.transFigure,
            zorder=20,
        )
        self._point_info_artists.append(nearby_title)
        y -= 0.028

        nearby_strong = [
            ap
            for ap in sample.nearby_aps
            if ap.signal_pct >= NEARBY_AP_MIN_SIGNAL_PCT
        ]
        if not nearby_strong:
            none_txt = self.fig.text(
                panel_x,
                y,
                f"Ninguno ≥{NEARBY_AP_MIN_SIGNAL_PCT}%",
                ha="center",
                va="top",
                fontsize=10,
                color=UI_MUTED,
                transform=self.fig.transFigure,
                zorder=20,
            )
            self._point_info_artists.append(none_txt)
            return

        for ap in nearby_strong[:8]:
            band_bit = f"\n{ap.band}" if ap.band else ""
            row = self.fig.text(
                panel_x,
                y,
                f"{self._short_bssid(ap.bssid)}\n{ap.signal_pct}%{band_bit}",
                ha="center",
                va="top",
                fontsize=9,
                color=UI_TEXT,
                family="monospace",
                transform=self.fig.transFigure,
                linespacing=1.15,
                bbox={
                    "boxstyle": "round,pad=0.16",
                    "facecolor": UI_CARD_BG,
                    "edgecolor": UI_CARD_EDGE,
                    "linewidth": 0.6,
                    "alpha": 1.0,
                },
                zorder=20,
            )
            self._point_info_artists.append(row)
            y -= 0.046 if ap.band else 0.036

        remaining = len(nearby_strong) - 8
        if remaining > 0:
            more = self.fig.text(
                panel_x,
                y,
                f"+{remaining} más…",
                ha="center",
                va="top",
                fontsize=9,
                color=UI_MUTED,
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
        self._mark_project_dirty()
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
                self._redraw_ap_markers()
                self._refresh()
        else:
            self._redraw_points()
            self._redraw_ap_markers()
            self._refresh()

    def _find_nearest_ap_key(self, x: float, y: float) -> tuple[str, float] | None:
        """Clave y distancia del AP marcado más cercano dentro del radio, o None."""
        if not self.ap_locations:
            return None
        best_key: str | None = None
        best_dist = REMOVE_HIT_RADIUS_M
        for key, ap in self.ap_locations.items():
            dist = float(np.hypot(ap.x_m - x, ap.y_m - y))
            if dist <= best_dist:
                best_dist = dist
                best_key = key
        if best_key is None:
            return None
        return best_key, best_dist

    def _remove_ap_at_key(self, key: str) -> None:
        removed = self.ap_locations.pop(key, None)
        if removed is None:
            self.status = "No se encontró la marca de AP."
            self._refresh()
            return
        self._mark_project_dirty()
        self.status = (
            f"Eliminada marca AP ({removed.x_m:.1f}, {removed.y_m:.1f}) — "
            f"{removed.ssid} · {removed.bssid}"
        )
        self._redraw_ap_markers()
        self._redraw_points()
        self._refresh()

    def _remove_nearest_marker(self, x: float, y: float) -> None:
        """Quita el punto o la marca de AP más cercanos al clic derecho."""
        sample_i = self._find_nearest_sample_index(x, y)
        sample_dist = (
            float(np.hypot(self.samples[sample_i].x_m - x, self.samples[sample_i].y_m - y))
            if sample_i is not None
            else None
        )
        ap_hit = self._find_nearest_ap_key(x, y)

        if sample_dist is None and ap_hit is None:
            self.status = (
                f"Ningún punto ni AP cerca del clic "
                f"(radio {REMOVE_HIT_RADIUS_M:.1f} m)."
            )
            self._refresh()
            return

        # Si ambos están cerca, quitar el más próximo
        if ap_hit is not None and (
            sample_dist is None or ap_hit[1] <= sample_dist
        ):
            self._remove_ap_at_key(ap_hit[0])
            return
        assert sample_i is not None
        self._remove_sample_at(sample_i)

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
        initialdir = (
            self.project_path.parent
            if self.project_path is not None
            else PROJECTS_DIR
        )
        initialfile = (
            self.project_path.name
            if self.project_path is not None
            else self._default_project_name()
        )
        return ask_project_save_path(
            initialdir=initialdir, initialfile=initialfile
        )

    def _ask_project_open_path(self) -> Path | None:
        initialdir = (
            self.project_path.parent
            if self.project_path is not None
            else PROJECTS_DIR
        )
        return ask_project_open_path(initialdir=initialdir)

    def save_project(self, *, ask_path: bool = True, autosave: bool = False) -> None:
        """
        Guarda la sesión (puntos, plano, AP, tamaño) en un .wifimap
        para poder reabrirla y seguir midiendo.
        """
        path = self.project_path
        if ask_path or path is None:
            if autosave:
                return
            chosen = self._ask_project_save_path()
            if chosen is None:
                self.status = "Guardado de proyecto cancelado."
                self._refresh()
                return
            path = chosen

        try:
            saved = self._write_project_file(path)
        except ProjectIOError as exc:
            if not autosave:
                self.status = str(exc)
                self._refresh()
            else:
                print(f"Autoguardado falló: {exc}")
            return

        self.project_path = saved
        self._project_dirty = False
        if autosave:
            self.status = (
                f"Autoguardado: {saved.name}  ({len(self.samples)} puntos)"
            )
            print(f"Autoguardado: {saved}")
        else:
            self.status = (
                f"Proyecto guardado: {saved.name}  "
                f"({len(self.samples)} puntos). Pulsa o para abrirlo después."
            )
            print(f"Proyecto guardado: {saved}")
        self._refresh()

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
            aps[normalize_bssid(ap.bssid)] = ap

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
        self._project_dirty = False

        self._setup_axes()
        if want_heatmap and len(self.samples) >= 3:
            self._apply_heatmap()
            # El heatmap restaurado no cuenta como cambio pendiente
            self._project_dirty = False

        try:
            self._refresh_connection_info()
        except WifiScannerError:
            pass
        self._refresh_visible_aps(force=True)

        self.status = (
            f"Proyecto abierto: {path.name} — {len(self.samples)} puntos"
            f"{plan_warning}. Autoguardado cada 1 min."
        )
        self._update_title()
        self.fig.canvas.draw_idle()
        print(f"Proyecto abierto: {self.project_path}")

    def on_key(self, event: KeyEvent) -> None:
        if (getattr(self, "_width_textbox", None) is not None and self._width_textbox.capturekeystrokes) or \
           (getattr(self, "_height_textbox", None) is not None and self._height_textbox.capturekeystrokes):
            return
        key = (event.key or "").lower()
        if key in {"f1", "?", "shift+/"}:
            self.show_usage_guide()
            return
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
            self._action_mark_ap()
        elif key == "escape":
            if self._placing_ap:
                self._placing_ap = False
                self.status = "Marcado de AP cancelado."
                self._refresh()
        elif key == "h":
            self._action_heatmap()
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
            "Controles: clic=medir (info si hay punto cerca) | "
            "clic der.=quitar punto/AP | "
            "a=marcar AP | "
            "p=cargar plano | c=quitar plano | e=expandir | h=heatmap | "
            "g=guardar proyecto | Ctrl+g=guardar como | o=abrir proyecto | "
            "s=PNG | z=deshacer | F1/?=ayuda | q=salir"
        )
        print(
            "Autoguardado: cada 1 min si hay proyecto abierto/guardado "
            "(g u o) y cambios pendientes."
        )
        plt.show()
