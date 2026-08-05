"""Guardar y cargar sesiones de mapa de calor WiFi (.wifimap)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from heatmap import NearbyApReading, SamplePoint

PROJECT_FORMAT = "wifi-heatmap-project"
PROJECT_VERSION = 2
PROJECT_EXTENSION = ".wifimap"


class ProjectIOError(Exception):
    """Error al leer o escribir un archivo de proyecto."""


def _rel_path(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def resolve_floor_plan_path(
    project_path: Path,
    relative: str | None,
    absolute: str | None,
) -> Path | None:
    """Resuelve la imagen del plano relativa al .wifimap o por ruta absoluta."""
    candidates: list[Path] = []
    if relative:
        candidates.append((project_path.parent / relative).resolve())
    if absolute:
        candidates.append(Path(absolute).expanduser().resolve())
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def build_project_dict(
    *,
    width_m: float,
    height_m: float,
    samples: list[SamplePoint],
    ap_locations: dict[str, Any],
    floor_plan_path: Path | None,
    plan_width_m: float,
    plan_length_m: float,
    heatmap_visible: bool,
    project_path: Path | None = None,
) -> dict[str, Any]:
    base = project_path.parent if project_path is not None else Path.cwd()
    floor_plan: dict[str, Any] | None = None
    if floor_plan_path is not None:
        abs_path = floor_plan_path.resolve()
        floor_plan = {
            "path": _rel_path(abs_path, base),
            "path_absolute": abs_path.as_posix(),
            "width_m": float(plan_width_m),
            "length_m": float(plan_length_m),
        }

    aps = []
    for ap in ap_locations.values():
        aps.append(
            {
                "x_m": float(ap.x_m),
                "y_m": float(ap.y_m),
                "bssid": str(ap.bssid),
                "ssid": str(ap.ssid),
            }
        )

    return {
        "format": PROJECT_FORMAT,
        "version": PROJECT_VERSION,
        "canvas": {
            "width_m": float(width_m),
            "height_m": float(height_m),
        },
        "floor_plan": floor_plan,
        "samples": [asdict(s) for s in samples],
        "ap_locations": aps,
        "view": {
            "heatmap_visible": bool(heatmap_visible),
        },
    }


def ensure_project_path(path: Path) -> Path:
    """Normaliza la ruta para que termine en .wifimap."""
    path = Path(path).expanduser()
    if path.suffix.lower() != PROJECT_EXTENSION:
        path = path.with_suffix(PROJECT_EXTENSION)
    return path


def save_project_file(path: Path, payload: dict[str, Any]) -> Path:
    path = ensure_project_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Regenerar rutas del plano relativas al archivo elegido
    floor = payload.get("floor_plan")
    if isinstance(floor, dict) and floor.get("path_absolute"):
        abs_plan = Path(str(floor["path_absolute"]))
        if abs_plan.is_file():
            floor["path"] = _rel_path(abs_plan, path.parent)
            floor["path_absolute"] = abs_plan.resolve().as_posix()
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ProjectIOError(f"No se pudo guardar el proyecto: {exc}") from exc
    return path.resolve()


def load_project_file(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ProjectIOError(f"No existe el archivo: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectIOError(f"No se pudo leer el proyecto: {exc}") from exc

    if not isinstance(data, dict):
        raise ProjectIOError("El archivo de proyecto no es un objeto JSON válido.")
    if data.get("format") != PROJECT_FORMAT:
        raise ProjectIOError(
            "Formato desconocido. ¿Es un archivo .wifimap de esta aplicación?"
        )
    version = data.get("version")
    if not isinstance(version, int) or version < 1 or version > PROJECT_VERSION:
        raise ProjectIOError(f"Versión de proyecto no soportada: {version!r}")
    return data


def _nearby_aps_from_item(item: dict[str, Any]) -> list[NearbyApReading]:
    raw = item.get("nearby_aps") or []
    if not isinstance(raw, list):
        return []
    nearby: list[NearbyApReading] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            nearby.append(
                NearbyApReading(
                    bssid=str(entry.get("bssid", "")),
                    signal_pct=int(entry["signal_pct"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    nearby.sort(key=lambda ap: (-ap.signal_pct, ap.bssid.casefold()))
    return nearby


def samples_from_project(data: dict[str, Any]) -> list[SamplePoint]:
    raw = data.get("samples") or []
    if not isinstance(raw, list):
        raise ProjectIOError("Campo «samples» inválido.")
    samples: list[SamplePoint] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ProjectIOError(f"Muestra #{i + 1} inválida.")
        try:
            samples.append(
                SamplePoint(
                    x_m=float(item["x_m"]),
                    y_m=float(item["y_m"]),
                    signal_pct=float(item["signal_pct"]),
                    bssid=str(item.get("bssid", "")),
                    ssid=str(item.get("ssid", "")),
                    timestamp=str(item.get("timestamp", "")),
                    nearby_aps=_nearby_aps_from_item(item),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectIOError(f"Muestra #{i + 1} incompleta: {exc}") from exc
    return samples
