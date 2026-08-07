"""Trabajo en segundo plano con callbacks en el hilo de la UI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from queue import Empty, Queue
from typing import Any, TypeVar

T = TypeVar("T")


class BackgroundJobs:
    """
    Ejecuta funciones en hilos daemon y entrega el resultado en el hilo de
    Matplotlib mediante un timer que drena una cola (seguro entre hilos).
    """

    def __init__(self, fig: Any, *, poll_ms: int = 50) -> None:
        self._fig = fig
        self._queue: Queue[tuple[str, Callable[..., Any] | None, Any]] = Queue()
        self._timer = fig.canvas.new_timer(interval=poll_ms)
        self._timer.add_callback(self._drain)
        self._timer.start()
        self._closed = False

    def submit(
        self,
        work: Callable[[], T],
        *,
        on_done: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if self._closed:
            return

        def worker() -> None:
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 — reenviar al hilo UI
                self._queue.put(("err", on_error, exc))
                return
            self._queue.put(("ok", on_done, result))

        threading.Thread(target=worker, daemon=True).start()

    def _drain(self) -> None:
        if self._closed:
            return
        while True:
            try:
                kind, callback, payload = self._queue.get_nowait()
            except Empty:
                break
            if callback is None:
                continue
            try:
                callback(payload)
            except Exception:
                # No tumbar el timer de la UI por un fallo en un callback.
                pass

    def close(self) -> None:
        self._closed = True
        timer = self._timer
        self._timer = None
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        # Vaciar pendientes
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                break


def run_tk_background(
    root: Any,
    work: Callable[[], T],
    *,
    on_done: Callable[[T], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Ejecuta work en un hilo y llama al callback con root.after (Tk)."""

    def worker() -> None:
        try:
            result = work()
        except BaseException as exc:  # noqa: BLE001
            if on_error is not None:
                root.after(0, lambda e=exc: on_error(e))
            return
        if on_done is not None:
            root.after(0, lambda r=result: on_done(r))

    threading.Thread(target=worker, daemon=True).start()
