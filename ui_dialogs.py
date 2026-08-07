"""Diálogos Tkinter (selector de red, archivos, ayuda, escala del plano)."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from constants import (
    AP_MARKER_ACTIVE,
    AP_MARKER_ESTIMATED,
    AP_MARKER_OTHER,
    DEFAULT_PLAN_LENGTH_M,
    DEFAULT_PLAN_WIDTH_M,
    NO_SIGNAL_COLOR,
    PLANOS_DIR,
    PROJECTS_DIR,
    UI_CONNECTED,
    UI_MUTED,
    UI_TEXT,
)
from project_io import PROJECT_EXTENSION
from ui_async import run_tk_background
from wifi_scanner import (
    InterfaceStatus,
    VisibleNetwork,
    WifiScannerError,
    connect_to_network,
    get_interface_status,
    list_visible_networks,
)

_hidden_root: tk.Tk | None = None


def _get_hidden_root() -> tk.Tk:
    """Root oculto reutilizable para filedialogs / simpledialog."""
    global _hidden_root
    if _hidden_root is not None:
        try:
            if _hidden_root.winfo_exists():
                return _hidden_root
        except tk.TclError:
            pass
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    _hidden_root = root
    return root


def ask_plan_dimensions_m(
    *,
    initial_width: float | None = None,
    initial_length: float | None = None,
    parent: Any | None = None,
) -> tuple[float, float] | None:
    """Pregunta ancho (X) y largo (Y) reales del plano en metros."""
    root = parent if parent is not None else _get_hidden_root()
    width = simpledialog.askfloat(
        "Escala del plano — Ancho",
        (
            "¿Cuántos metros mide el ANCHO del plano (eje X, horizontal)?\n\n"
            f"(Por defecto {DEFAULT_PLAN_WIDTH_M:.0f} m)"
        ),
        parent=root,
        minvalue=5.0,
        maxvalue=500.0,
        initialvalue=initial_width if initial_width is not None else DEFAULT_PLAN_WIDTH_M,
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
        parent=root,
        minvalue=5.0,
        maxvalue=500.0,
        initialvalue=(
            initial_length if initial_length is not None else DEFAULT_PLAN_LENGTH_M
        ),
    )
    if length is None:
        return None

    return float(width), float(length)


def ask_floor_plan_image_path() -> Path | None:
    root = _get_hidden_root()
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
        return None
    return Path(path_str)


def ask_project_save_path(
    *,
    initialdir: Path,
    initialfile: str,
) -> Path | None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    root = _get_hidden_root()
    path_str = filedialog.asksaveasfilename(
        parent=root,
        title="Guardar proyecto para continuar después",
        initialdir=str(initialdir),
        initialfile=initialfile,
        defaultextension=PROJECT_EXTENSION,
        filetypes=[
            ("Proyecto mapa WiFi", f"*{PROJECT_EXTENSION}"),
            ("Todos los archivos", "*.*"),
        ],
    )
    if not path_str:
        return None
    return Path(path_str)


def ask_project_open_path(*, initialdir: Path) -> Path | None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    root = _get_hidden_root()
    path_str = filedialog.askopenfilename(
        parent=root,
        title="Abrir proyecto guardado",
        initialdir=str(initialdir),
        filetypes=[
            ("Proyecto mapa WiFi", f"*{PROJECT_EXTENSION}"),
            ("Todos los archivos", "*.*"),
        ],
    )
    if not path_str:
        return None
    return Path(path_str)


def show_usage_guide() -> None:
    """Ventana con guía rápida de uso y simbología del mapa."""
    root = tk.Tk()
    root.title("Guía de uso — Mapa de calor WiFi")
    root.geometry("580x640")
    root.minsize(480, 420)
    root.attributes("-topmost", True)
    root.configure(bg="#F4F6F8")

    outer = ttk.Frame(root, padding=(10, 10, 6, 10))
    outer.pack(fill=tk.BOTH, expand=True)

    canvas = tk.Canvas(
        outer,
        highlightthickness=0,
        bg="#F4F6F8",
        borderwidth=0,
    )
    scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    frm = ttk.Frame(canvas, padding=(8, 4, 16, 8))
    window_id = canvas.create_window((0, 0), window=frm, anchor="nw")

    def _on_frame_configure(_event=None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event) -> None:
        canvas.itemconfigure(window_id, width=event.width)

    def _on_mousewheel(event) -> None:
        delta = getattr(event, "delta", 0)
        if delta:
            canvas.yview_scroll(int(-delta / 120), "units")
        elif getattr(event, "num", None) == 4:
            canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            canvas.yview_scroll(1, "units")

    frm.bind("<Configure>", _on_frame_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    root.bind_all("<MouseWheel>", _on_mousewheel)
    root.bind_all("<Button-4>", _on_mousewheel)
    root.bind_all("<Button-5>", _on_mousewheel)

    def _unbind_wheel() -> None:
        root.unbind_all("<MouseWheel>")
        root.unbind_all("<Button-4>")
        root.unbind_all("<Button-5>")
        root.destroy()

    ttk.Label(
        frm,
        text="Cómo usarlo",
        font=("Segoe UI", 13, "bold"),
    ).pack(anchor=tk.W)

    guide = (
        "1. Elige la red WiFi al iniciar (o con el icono casa).\n"
        "2. Pulsa p y carga el plano del lugar; indica ancho y largo reales.\n"
        "3. Colócate en un sitio del mapa y haz clic para medir la señal.\n"
        "4. Repite en varios puntos (recomendado: 8 o más).\n"
        "5. Pulsa h para generar / actualizar el mapa de calor.\n"
        "6. Pulsa a y luego clic para marcar la ubicación de un AP.\n"
        "7. Guarda con g (proyecto .wifimap) para continuar después.\n"
        "8. Exporta imágenes PNG con s."
    )
    ttk.Label(
        frm,
        text=guide,
        justify=tk.LEFT,
        font=("Segoe UI", 10),
    ).pack(anchor=tk.W, pady=(6, 14))

    ttk.Label(
        frm,
        text="Simbología",
        font=("Segoe UI", 13, "bold"),
    ).pack(anchor=tk.W)

    legend_frame = ttk.Frame(frm)
    legend_frame.pack(fill=tk.X, pady=(8, 12))

    legend_items = [
        ("▲", AP_MARKER_ACTIVE, "AP marcado al que estás conectado ahora"),
        ("▲", AP_MARKER_OTHER, "Otro AP marcado en el mapa"),
        ("△", AP_MARKER_ESTIMATED, "AP estimado (aún no marcado a mano)"),
        ("●", UI_CONNECTED, "Punto de medición con buena señal"),
        ("●", "#EAB308", "Punto de medición con señal media"),
        ("●", "#DC2626", "Punto de medición con señal baja"),
        ("●", NO_SIGNAL_COLOR, "Punto sin señal / 0% (sin cobertura)"),
        ("●", "#111111", "Origen (0, 0) — centro del plano"),
        (None, None, "Heatmap: rojo = baja · amarillo = media · verde = alta"),
        ("■", UI_MUTED, "Panel izquierdo: AP / repetidores del mismo SSID"),
        ("■", UI_MUTED, "Panel derecho: detalle del punto al hacer clic"),
    ]

    for symbol, color, desc in legend_items:
        row = tk.Frame(legend_frame, bg="#F4F6F8")
        row.pack(fill=tk.X, pady=3)

        if symbol is None:
            swatch = tk.Canvas(
                row, width=72, height=18, bg="#F4F6F8", highlightthickness=0
            )
            swatch.pack(side=tk.LEFT, padx=(0, 10))
            for i, c in enumerate(("#DC2626", "#EAB308", "#16A34A")):
                swatch.create_rectangle(
                    2 + i * 23, 2, 22 + i * 23, 16, fill=c, outline=""
                )
        else:
            swatch = tk.Label(
                row,
                text=symbol,
                width=3,
                font=("Segoe UI", 14, "bold"),
                fg=color or UI_TEXT,
                bg="#F4F6F8",
            )
            swatch.pack(side=tk.LEFT, padx=(0, 8))

        tk.Label(
            row,
            text=desc,
            font=("Segoe UI", 10),
            fg=UI_TEXT,
            bg="#F4F6F8",
            anchor="w",
            justify=tk.LEFT,
            wraplength=420,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

    ttk.Label(
        frm,
        text="Atajos principales",
        font=("Segoe UI", 13, "bold"),
    ).pack(anchor=tk.W, pady=(4, 0))

    shortcuts = (
        "Clic = medir o ver info   ·   clic der. = quitar punto/AP\n"
        "a = marcar AP   ·   p/c = plano   ·   e/w/r = expandir\n"
        "h = heatmap   ·   g / Ctrl+g = guardar   ·   o = abrir\n"
        "s = PNG   ·   z = deshacer   ·   F1 o ? = esta guía\n"
        "casa (toolbar) = volver a elegir red   ·   q = salir"
    )
    ttk.Label(
        frm,
        text=shortcuts,
        justify=tk.LEFT,
        font=("Segoe UI", 10),
    ).pack(anchor=tk.W, pady=(6, 14))

    btn_row = ttk.Frame(frm)
    btn_row.pack(fill=tk.X)
    ttk.Button(btn_row, text="Cerrar", command=_unbind_wheel).pack(anchor=tk.E)

    root.protocol("WM_DELETE_WINDOW", _unbind_wheel)
    root.mainloop()


def show_network_picker() -> bool:
    """
    Menú previo: elegir y conectar a una red WiFi.
    Devuelve False si el usuario cancela / cierra la ventana.
    """
    root = tk.Tk()
    root.title("Mapa de calor WiFi — Elegir red")
    root.geometry("560x460")
    root.minsize(480, 380)
    root.attributes("-topmost", True)

    networks: list[VisibleNetwork] = []
    result = {"continue": False}
    busy = {"scan": False, "connect": False}

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

    def _apply_status(status: InterfaceStatus | None, error: str | None = None) -> None:
        if error is not None:
            current_var.set(f"Ahora: no se pudo leer la interfaz ({error})")
            return
        if status is not None and status.connected and status.ssid:
            ap = status.bssid or "—"
            current_var.set(f"Ahora: {status.ssid}  ·  AP: {ap}")
        else:
            current_var.set("Ahora: sin conexión WiFi activa")

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

    def _set_busy_ui(*, scanning: bool = False, connecting: bool = False) -> None:
        if connecting:
            btn_refresh.state(["disabled"])
            btn_connect.state(["disabled"])
            btn_continue.state(["disabled"])
        elif scanning:
            btn_refresh.state(["disabled"])
            btn_connect.state(["disabled"])
            btn_continue.state(["!disabled"])
        else:
            btn_refresh.state(["!disabled"])
            btn_connect.state(["!disabled"])
            btn_continue.state(["!disabled"])

    def _refresh_current_async() -> None:
        def work() -> InterfaceStatus:
            return get_interface_status()

        def on_done(status: InterfaceStatus) -> None:
            if not root.winfo_exists():
                return
            _apply_status(status)

        def on_error(exc: BaseException) -> None:
            if not root.winfo_exists():
                return
            _apply_status(None, error=str(exc))

        run_tk_background(root, work, on_done=on_done, on_error=on_error)

    def scan_networks() -> None:
        if busy["scan"] or busy["connect"]:
            return
        busy["scan"] = True
        status_var.set("Escaneando redes…")
        _set_busy_ui(scanning=True)

        def work() -> tuple[list[VisibleNetwork], InterfaceStatus | None]:
            items = list_visible_networks()
            try:
                status = get_interface_status()
            except WifiScannerError:
                status = None
            return items, status

        def on_done(payload: tuple[list[VisibleNetwork], InterfaceStatus | None]) -> None:
            busy["scan"] = False
            if not root.winfo_exists():
                return
            _set_busy_ui()
            items, status = payload
            _fill_list(items)
            _apply_status(status)
            if items:
                status_var.set(
                    f"{len(items)} redes encontradas. "
                    "Selecciona una y pulsa «Conectar»."
                )
            else:
                status_var.set(
                    "No se encontraron redes. ¿WiFi activado? Prueba Actualizar."
                )

        def on_error(exc: BaseException) -> None:
            busy["scan"] = False
            if not root.winfo_exists():
                return
            _set_busy_ui()
            status_var.set(f"Error al escanear: {exc}")

        run_tk_background(root, work, on_done=on_done, on_error=on_error)

    def selected_network() -> VisibleNetwork | None:
        sel = tree.selection()
        if not sel:
            return None
        try:
            return networks[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def connect_selected() -> None:
        if busy["connect"] or busy["scan"]:
            return
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
        needs_password = (
            not net.has_profile
            and "open" not in auth_l
            and "abiert" not in auth_l
        )
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

        busy["connect"] = True
        status_var.set(f"Conectando a «{net.ssid}»…")
        _set_busy_ui(connecting=True)
        ssid = net.ssid
        authentication = net.authentication
        encryption = net.encryption

        def work() -> InterfaceStatus:
            return connect_to_network(
                ssid,
                authentication=authentication,
                encryption=encryption,
                password=password,
            )

        def on_done(status: InterfaceStatus) -> None:
            busy["connect"] = False
            if not root.winfo_exists():
                return
            _apply_status(status)
            status_var.set(f"Conectado a «{ssid}». Abriendo mapa…")
            result["continue"] = True
            root.after(400, root.destroy)

        def on_error(exc: BaseException) -> None:
            busy["connect"] = False
            if not root.winfo_exists():
                return
            _set_busy_ui()
            status_var.set(f"No se pudo conectar: {exc}")
            messagebox.showerror("Conexión WiFi", str(exc), parent=root)

        run_tk_background(root, work, on_done=on_done, on_error=on_error)

    def continue_current() -> None:
        if busy["connect"]:
            return
        result["continue"] = True
        root.destroy()

    def cancel() -> None:
        if busy["connect"]:
            # Evitar cerrar a mitad de conexión y dejar el estado a medias
            status_var.set("Espera a que termine la conexión…")
            return
        result["continue"] = False
        root.destroy()

    btn_refresh = ttk.Button(btn_row, text="Actualizar", command=scan_networks)
    btn_refresh.pack(side=tk.LEFT, padx=(0, 6))
    btn_connect = ttk.Button(btn_row, text="Conectar", command=connect_selected)
    btn_connect.pack(side=tk.LEFT, padx=(0, 6))
    btn_continue = ttk.Button(
        btn_row, text="Continuar con la red actual", command=continue_current
    )
    btn_continue.pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(btn_row, text="Salir", command=cancel).pack(side=tk.RIGHT)

    tree.bind("<Double-1>", lambda _e: connect_selected())
    root.protocol("WM_DELETE_WINDOW", cancel)

    _refresh_current_async()
    root.after(100, scan_networks)
    root.mainloop()
    return bool(result["continue"])
