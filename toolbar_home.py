"""Cableado del botón Home de la toolbar por figura (sin monkey-patch de clase)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from matplotlib.backend_bases import NavigationToolbar2

_HOME_TOOLITEM = ("Home", "Volver a elegir red", "home", "home")
_toolitems_patched = False


def _ensure_home_toolitem_label() -> None:
    """Ajusta la etiqueta del toolitem Home una sola vez (atributo de clase)."""
    global _toolitems_patched
    if _toolitems_patched:
        return
    items = list(NavigationToolbar2.toolitems)
    for i, item in enumerate(items):
        if item[0] == "Home":
            items[i] = _HOME_TOOLITEM
            break
    NavigationToolbar2.toolitems = tuple(items)
    _toolitems_patched = True


def install_home_handler(fig: Any, handler: Callable[[], None]) -> None:
    """
    Sustituye toolbar.home solo en esta figura.
    Si aún no hay toolbar, se registra un callback al primer draw.
    """
    _ensure_home_toolitem_label()

    def _bind() -> bool:
        manager = getattr(fig.canvas, "manager", None)
        toolbar = getattr(manager, "toolbar", None) if manager is not None else None
        if toolbar is None:
            return False
        toolbar.home = lambda *args, **kwargs: handler()  # type: ignore[method-assign]
        return True

    if _bind():
        return

    def _on_draw(_event=None) -> None:
        if _bind():
            fig.canvas.mpl_disconnect(cid)

    cid = fig.canvas.mpl_connect("draw_event", _on_draw)
