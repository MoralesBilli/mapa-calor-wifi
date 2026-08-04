"""Interpolación y dibujo del mapa de calor WiFi."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from matplotlib.axes import Axes
from matplotlib.colorbar import Colorbar
from matplotlib.image import AxesImage
from scipy.interpolate import RBFInterpolator, griddata
from scipy.ndimage import gaussian_filter


MIN_POINTS_FOR_HEATMAP = 8
GRID_RESOLUTION = 220


@dataclass
class SamplePoint:
    x_m: float
    y_m: float
    signal_pct: float
    bssid: str
    ssid: str
    timestamp: str


def bounds_from_size(width_m: float, height_m: float) -> tuple[float, float, float, float]:
    """Límites centrados en el origen: (-w/2, w/2) × (-h/2, h/2)."""
    half_w = width_m / 2.0
    half_h = height_m / 2.0
    return -half_w, half_w, -half_h, half_h


def _mean_neighbor_distance(points: np.ndarray) -> float:
    """Distancia media al vecino más cercano (para escalar el suavizado)."""
    if len(points) < 2:
        return 1.0
    dists = []
    for i, p in enumerate(points):
        others = np.delete(points, i, axis=0)
        d = np.linalg.norm(others - p, axis=1).min()
        dists.append(d)
    return float(max(np.mean(dists), 0.5))


def interpolate_signal(
    samples: list[SamplePoint],
    width_m: float,
    height_m: float,
    resolution: int = GRID_RESOLUTION,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """
    Interpola signal_pct con degradados suaves sobre todo el plano.
    Devuelve (X, Y, Z) o None si no hay puntos suficientes.
    """
    if len(samples) < 3:
        return None

    x_min, x_max, y_min, y_max = bounds_from_size(width_m, height_m)
    points = np.array([[s.x_m, s.y_m] for s in samples], dtype=float)
    values = np.array([s.signal_pct for s in samples], dtype=float)

    xs = np.linspace(x_min, x_max, resolution)
    ys = np.linspace(y_min, y_max, resolution)
    grid_x, grid_y = np.meshgrid(xs, ys)
    flat = np.column_stack([grid_x.ravel(), grid_y.ravel()])

    span = max(width_m, height_m)
    nn = _mean_neighbor_distance(points)
    # Thin-plate + smoothing ligero: degradados sin aplanar el mapa
    smoothing = max(1.0, nn * 0.35)

    try:
        rbf = RBFInterpolator(
            points,
            values,
            kernel="thin_plate_spline",
            smoothing=smoothing,
        )
        grid_z = rbf(flat).reshape(grid_x.shape)
    except Exception:
        # Respaldo: cúbico + relleno con RBF (nunca nearest → evita bloques)
        grid_z = griddata(points, values, (grid_x, grid_y), method="cubic")
        try:
            rbf = RBFInterpolator(
                points, values, kernel="thin_plate_spline", smoothing=smoothing
            )
            filled = rbf(flat).reshape(grid_x.shape)
            nan_mask = np.isnan(grid_z)
            grid_z = np.where(nan_mask, filled, grid_z)
        except Exception:
            linear = griddata(points, values, (grid_x, grid_y), method="linear")
            nan_mask = np.isnan(linear)
            # Extender con media ponderada por distancia (suave) en bordes
            if nan_mask.any():
                for iy, ix in zip(*np.where(nan_mask)):
                    d = np.linalg.norm(points - np.array([xs[ix], ys[iy]]), axis=1)
                    w = 1.0 / np.maximum(d, 0.25) ** 2
                    linear[iy, ix] = np.sum(w * values) / np.sum(w)
            grid_z = linear

    # Difuminado suave en metros (mantiene variación, quita micro-escalones)
    cell_m = span / float(resolution)
    sigma_m = max(span / 45.0, nn * 0.25)
    sigma_px = max(0.8, sigma_m / max(cell_m, 1e-6))
    grid_z = gaussian_filter(grid_z, sigma=sigma_px, mode="nearest")
    grid_z = np.clip(grid_z, 0.0, 100.0)
    return grid_x, grid_y, grid_z


def draw_heatmap(
    ax: Axes,
    samples: list[SamplePoint],
    width_m: float,
    height_m: float,
    existing_mesh: Any | None = None,
    colorbar: Colorbar | None = None,
    alpha: float = 0.88,
) -> tuple[AxesImage | None, Colorbar | None, str]:
    """
    Dibuja (o actualiza) el mapa de calor con degradados suaves.
    Devuelve (image, colorbar, mensaje_estado).
    """
    if existing_mesh is not None:
        existing_mesh.remove()

    result = interpolate_signal(samples, width_m, height_m)
    if result is None:
        return None, colorbar, "Se necesitan al menos 3 puntos para interpolar."

    x_min, x_max, y_min, y_max = bounds_from_size(width_m, height_m)
    _grid_x, _grid_y, grid_z = result

    # imshow + bicubic: degradados visuales continuos (sin bloques)
    image = ax.imshow(
        grid_z,
        extent=[x_min, x_max, y_min, y_max],
        origin="lower",
        cmap="RdYlGn",
        vmin=0,
        vmax=100,
        interpolation="bicubic",
        aspect="equal",
        alpha=alpha,
        zorder=1,
    )

    if colorbar is None:
        colorbar = ax.figure.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
        colorbar.set_label("Señal (%)")
    else:
        colorbar.update_normal(image)

    if len(samples) < MIN_POINTS_FOR_HEATMAP:
        status = (
            f"Heatmap preliminar ({len(samples)} puntos). "
            f"Recomendado: ≥ {MIN_POINTS_FOR_HEATMAP}."
        )
    else:
        status = f"Heatmap con {len(samples)} puntos (degradado suave)."

    return image, colorbar, status


def save_figure_image(fig, path: Path, dpi: int = 150) -> None:
    """Guarda la figura en formato de imagen (PNG o JPG según la extensión)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
        path = path.with_suffix(".png")
    kwargs: dict = {"dpi": dpi, "bbox_inches": "tight", "facecolor": "white"}
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        kwargs["format"] = "jpeg"
        kwargs["pil_kwargs"] = {"quality": 95}
    fig.savefig(path, **kwargs)
