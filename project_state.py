"""Estado del proyecto (lienzo, muestras, plano, AP) separado de la UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from constants import (
    DEFAULT_HEIGHT_M,
    DEFAULT_PLAN_LENGTH_M,
    DEFAULT_PLAN_WIDTH_M,
    DEFAULT_WIDTH_M,
)
from heatmap import SamplePoint, bounds_from_size
from models import ApLocation
from project_io import build_project_dict
from wifi_scanner import normalize_bssid


@dataclass
class ProjectState:
    """Datos persistentes / de sesión del mapa (sin artistas Matplotlib)."""

    width_m: float = DEFAULT_WIDTH_M
    height_m: float = DEFAULT_HEIGHT_M
    samples: list[SamplePoint] = field(default_factory=list)
    ap_locations: dict[str, ApLocation] = field(default_factory=dict)
    floor_plan_path: Path | None = None
    floor_plan_array: Any | None = None  # ndarray al cargar plano
    plan_width_m: float = DEFAULT_PLAN_WIDTH_M
    plan_length_m: float = DEFAULT_PLAN_LENGTH_M
    project_path: Path | None = None
    dirty: bool = False

    def bounds(self) -> tuple[float, float, float, float]:
        return bounds_from_size(self.width_m, self.height_m)

    def x_min(self) -> float:
        return self.bounds()[0]

    def x_max(self) -> float:
        return self.bounds()[1]

    def y_min(self) -> float:
        return self.bounds()[2]

    def y_max(self) -> float:
        return self.bounds()[3]

    def floor_plan_extent(self) -> list[float]:
        """Rectángulo en metros del plano calibrado (no del lienzo expandido)."""
        x0, x1, y0, y1 = bounds_from_size(self.plan_width_m, self.plan_length_m)
        return [x0, x1, y0, y1]

    def mark_dirty(self) -> None:
        self.dirty = True

    def clear_dirty(self) -> None:
        self.dirty = False

    def expand(self, *, width: bool = True, height: bool = True, step_m: float) -> None:
        if width:
            self.width_m += step_m
        if height:
            self.height_m += step_m
        self.mark_dirty()

    def set_ap(self, ap: ApLocation) -> None:
        self.ap_locations[normalize_bssid(ap.bssid)] = ap
        self.mark_dirty()

    def remove_ap(self, key: str) -> ApLocation | None:
        removed = self.ap_locations.pop(key, None)
        if removed is not None:
            self.mark_dirty()
        return removed

    def to_project_dict(self, *, heatmap_visible: bool) -> dict[str, Any]:
        return build_project_dict(
            width_m=self.width_m,
            height_m=self.height_m,
            samples=self.samples,
            ap_locations=self.ap_locations,
            floor_plan_path=self.floor_plan_path,
            plan_width_m=self.plan_width_m,
            plan_length_m=self.plan_length_m,
            heatmap_visible=heatmap_visible,
            project_path=self.project_path,
        )
