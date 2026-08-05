"""Escaneo WiFi en Windows mediante netsh (SSID actual + BSSIDs/repetidores)."""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
import xml.sax.saxutils
from dataclasses import dataclass
from pathlib import Path


class WifiScannerError(Exception):
    """Error al leer o interpretar la información WiFi del sistema."""


@dataclass(frozen=True)
class ApSignal:
    """Señal observada de un BSSID (AP / repetidor)."""

    bssid: str
    signal_pct: int


@dataclass(frozen=True)
class WifiSample:
    ssid: str
    signal_pct: int  # señal del AP asociado (conectado)
    bssid: str  # BSSID del AP asociado
    # Otros BSSID del mismo SSID vistos en el escaneo (menor u otra señal)
    nearby_aps: tuple[ApSignal, ...] = ()


@dataclass(frozen=True)
class InterfaceStatus:
    connected: bool
    state: str
    ssid: str | None
    signal_pct: int | None
    bssid: str


@dataclass(frozen=True)
class VisibleNetwork:
    ssid: str
    signal_pct: int
    authentication: str
    encryption: str
    has_profile: bool


_SIGNAL_KEYS = ("Signal", "Señal", "Senal")
_BSSID_KEYS = ("BSSID", "Dirección física", "Direccion fisica", "AP BSSID")
_STATE_KEYS = ("State", "Estado")
_AUTH_KEYS = ("Authentication", "Autenticación", "Autenticacion")
_CIPHER_KEYS = ("Cipher", "Cifrado", "Encryption", "Cifrado")
_CONNECTED_STATES = frozenset(
    {"connected", "conectado", "connecting", "conectando"}
)
_OPEN_AUTH = frozenset({"open", "abierta", "abierto"})


def _run_netsh(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["netsh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise WifiScannerError("No se encontró 'netsh'. ¿Estás en Windows?") from exc
    except OSError as exc:
        raise WifiScannerError(f"No se pudo ejecutar netsh: {exc}") from exc

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0 and not result.stdout.strip():
        raise WifiScannerError(
            f"netsh falló (código {result.returncode}). "
            "Comprueba que el WiFi esté activado."
        )
    return output


def _parse_kv_lines(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        pairs.append((key.strip(), value.strip()))
    return pairs


def _key_matches(key: str, candidates: tuple[str, ...]) -> bool:
    key_norm = key.casefold()
    for candidate in candidates:
        cand = candidate.casefold()
        if key_norm == cand:
            return True
        if cand == "ssid" and key_norm.endswith("ssid") and "bssid" not in key_norm:
            return True
        if cand != "ssid" and cand in key_norm:
            return True
    return False


def _parse_signal_pct(value: str) -> int | None:
    match = re.search(r"(\d+)\s*%?", value)
    if not match:
        return None
    pct = int(match.group(1))
    return max(0, min(100, pct))


def get_interface_status() -> InterfaceStatus:
    """Lee estado de la interfaz WiFi (conectado o no, SSID, señal asociada)."""
    output = _run_netsh(["wlan", "show", "interfaces"])
    state = ""
    ssid: str | None = None
    signal: int | None = None
    bssid = ""

    for key, value in _parse_kv_lines(output):
        key_norm = key.casefold().strip()
        if _key_matches(key, _STATE_KEYS):
            state = value
        elif key_norm == "ssid" or key_norm.endswith(" de ssid") or key_norm == "nombre ssid":
            if value:
                ssid = value
        elif _key_matches(key, _SIGNAL_KEYS):
            signal = _parse_signal_pct(value)
        elif _key_matches(key, _BSSID_KEYS) or key_norm in {"bssid", "ap bssid"}:
            bssid = value

    connected = state.casefold() in _CONNECTED_STATES and bool(ssid)
    return InterfaceStatus(
        connected=connected,
        state=state or "desconocido",
        ssid=ssid,
        signal_pct=signal,
        bssid=bssid,
    )


def get_current_ssid() -> str:
    """Devuelve el SSID de la red WiFi conectada actualmente."""
    status = get_interface_status()
    if not status.connected or not status.ssid:
        raise WifiScannerError(
            f"No hay conexión WiFi activa (estado: {status.state}). "
            "Conéctate a una red e inténtalo de nuevo."
        )
    return status.ssid


def list_wlan_profiles() -> set[str]:
    """Nombres de perfiles WiFi guardados en Windows."""
    output = _run_netsh(["wlan", "show", "profiles"])
    profiles: set[str] = set()
    for key, value in _parse_kv_lines(output):
        key_norm = key.casefold()
        if "perfil de todos los usuarios" in key_norm or "all user profile" in key_norm:
            if value:
                profiles.add(value)
        elif "perfil de usuario" in key_norm or "user profile" in key_norm:
            if value:
                profiles.add(value)
        elif key_norm.endswith("profile") and value:
            profiles.add(value)
    return profiles


def list_visible_networks() -> list[VisibleNetwork]:
    """
    Escanea redes WiFi visibles y agrupa por SSID (mejor señal).
    Marca si ya existe un perfil guardado para conectar sin pedir clave.
    """
    output = _run_netsh(["wlan", "show", "networks", "mode=bssid"])
    profiles = list_wlan_profiles()

    # ssid -> (signal, auth, cipher)
    best: dict[str, tuple[int, str, str]] = {}
    current_ssid: str | None = None
    current_auth = ""
    current_cipher = ""
    current_signal: int | None = None

    def _flush() -> None:
        nonlocal current_signal
        if not current_ssid or current_signal is None:
            return
        prev = best.get(current_ssid)
        if prev is None or current_signal > prev[0]:
            best[current_ssid] = (current_signal, current_auth, current_cipher)

    for raw in output.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        key_norm = key.casefold()

        if re.match(r"^ssid\s*\d*$", key_norm) or (
            "ssid" in key_norm
            and "bssid" not in key_norm
            and re.search(r"ssid\s*\d*", key_norm)
        ):
            _flush()
            current_ssid = value or None
            current_auth = ""
            current_cipher = ""
            current_signal = None
            continue

        if key_norm == "ssid" or key_norm.endswith(" de ssid"):
            _flush()
            current_ssid = value or None
            current_auth = ""
            current_cipher = ""
            current_signal = None
            continue

        if _key_matches(key, _AUTH_KEYS):
            current_auth = value
            continue

        if _key_matches(key, _CIPHER_KEYS):
            current_cipher = value
            continue

        if _key_matches(key, _SIGNAL_KEYS):
            pct = _parse_signal_pct(value)
            if pct is not None:
                if current_signal is None or pct > current_signal:
                    current_signal = pct
            continue

    _flush()

    profiles_cf = {p.casefold() for p in profiles}
    networks = [
        VisibleNetwork(
            ssid=ssid,
            signal_pct=signal,
            authentication=auth or "desconocida",
            encryption=cipher or "desconocido",
            has_profile=ssid.casefold() in profiles_cf,
        )
        for ssid, (signal, auth, cipher) in best.items()
        if ssid
    ]
    networks.sort(key=lambda n: (-n.signal_pct, n.ssid.casefold()))
    return networks


def _auth_is_open(authentication: str) -> bool:
    return authentication.casefold().strip() in _OPEN_AUTH


def _profile_auth_encryption(authentication: str, encryption: str) -> tuple[str, str]:
    """Mapea texto de netsh a valores del perfil XML de Windows."""
    auth_l = authentication.casefold()
    enc_l = encryption.casefold()

    if _auth_is_open(authentication) or "open" in auth_l:
        return "open", "none"
    if "wpa3" in auth_l and "sae" in auth_l:
        return "WPA3SAE", "AES"
    if "wpa3" in auth_l:
        return "WPA3SAE", "AES"
    if "wpa2" in auth_l or "rsn" in auth_l:
        return "WPA2PSK", "AES" if "tkip" not in enc_l else "TKIP"
    if "wpa" in auth_l:
        return "WPAPSK", "AES" if "aes" in enc_l else "TKIP"
    # Por defecto redes domésticas modernas
    return "WPA2PSK", "AES"


def _build_wlan_profile_xml(
    ssid: str,
    authentication: str,
    encryption: str,
    password: str | None,
) -> str:
    auth, cipher = _profile_auth_encryption(authentication, encryption)
    ssid_esc = xml.sax.saxutils.escape(ssid)
    name_esc = ssid_esc

    if auth == "open":
        security = f"""
        <authEncryption>
            <authentication>open</authentication>
            <encryption>none</encryption>
            <useOneX>false</useOneX>
        </authEncryption>"""
    else:
        if not password:
            raise WifiScannerError(
                f"La red «{ssid}» requiere contraseña y no hay perfil guardado."
            )
        key_esc = xml.sax.saxutils.escape(password)
        security = f"""
        <authEncryption>
            <authentication>{auth}</authentication>
            <encryption>{cipher}</encryption>
            <useOneX>false</useOneX>
        </authEncryption>
        <sharedKey>
            <keyType>passPhrase</keyType>
            <protected>false</protected>
            <keyMaterial>{key_esc}</keyMaterial>
        </sharedKey>"""

    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{name_esc}</name>
    <SSIDConfig>
        <SSID>
            <name>{ssid_esc}</name>
        </SSID>
    </SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>manual</connectionMode>
    <MSM>
        <security>{security}
        </security>
    </MSM>
</WLANProfile>
"""


def connect_to_network(
    ssid: str,
    *,
    authentication: str = "WPA2PSK",
    encryption: str = "AES",
    password: str | None = None,
    timeout_s: float = 20.0,
) -> InterfaceStatus:
    """
    Conecta a un SSID. Usa perfil guardado si existe; si no, crea uno temporal.
    """
    if not ssid.strip():
        raise WifiScannerError("SSID vacío.")

    profiles = list_wlan_profiles()
    profile_name: str | None = None
    for saved in profiles:
        if saved.casefold() == ssid.casefold():
            profile_name = saved
            break

    # Ya conectado a esa red: no hace falta reconectar
    try:
        current = get_interface_status()
        if (
            current.connected
            and current.ssid
            and current.ssid.casefold() == ssid.casefold()
        ):
            return current
    except WifiScannerError:
        pass

    temp_path: Path | None = None
    try:
        if profile_name is None:
            xml_text = _build_wlan_profile_xml(
                ssid, authentication, encryption, password
            )
            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".xml",
                delete=False,
                encoding="utf-8",
            )
            with tmp:
                tmp.write(xml_text)
                temp_path = Path(tmp.name)
            add_out = _run_netsh(
                ["wlan", "add", "profile", f"filename={temp_path}"]
            )
            if "error" in add_out.casefold() and "correctamente" not in add_out.casefold():
                raise WifiScannerError(
                    f"No se pudo crear el perfil para «{ssid}».\n{add_out.strip()}"
                )
            profile_name = ssid

        connect_out = _run_netsh(
            [
                "wlan",
                "connect",
                f"name={profile_name}",
                f"ssid={ssid}",
            ]
        )
        connect_l = connect_out.casefold()
        if "error" in connect_l or "no se pudo" in connect_l or "failed" in connect_l:
            if "correctamente" not in connect_l and "completed successfully" not in connect_l:
                raise WifiScannerError(
                    f"netsh no pudo iniciar la conexión a «{ssid}».\n"
                    f"{connect_out.strip()}"
                )

        deadline = time.time() + timeout_s
        last = get_interface_status()
        while time.time() < deadline:
            last = get_interface_status()
            if (
                last.connected
                and last.ssid
                and last.ssid.casefold() == ssid.casefold()
            ):
                return last
            time.sleep(0.6)

        raise WifiScannerError(
            f"No se pudo conectar a «{ssid}» a tiempo "
            f"(estado: {last.state}). Comprueba la contraseña o el WiFi."
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def scan_bssids_for_ssid(ssid: str) -> list[tuple[str, int]]:
    """
    Escanea redes visibles y devuelve lista de (bssid, signal_pct)
    para entradas con el SSID indicado.
    Solo debe usarse cuando la interfaz está conectada a esa red.
    """
    output = _run_netsh(["wlan", "show", "networks", "mode=bssid"])
    lines = output.splitlines()

    results: list[tuple[str, int]] = []
    current_ssid: str | None = None
    current_bssid: str | None = None

    for raw in lines:
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        key_norm = key.casefold()
        if re.match(r"^ssid\s*\d*$", key_norm) or (
            "ssid" in key_norm
            and "bssid" not in key_norm
            and re.search(r"ssid\s*\d*", key_norm)
        ):
            current_ssid = value
            current_bssid = None
            continue

        if key_norm == "ssid" or key_norm.endswith(" de ssid"):
            current_ssid = value
            current_bssid = None
            continue

        if _key_matches(key, _BSSID_KEYS) or re.match(r"^bssid\s*\d*$", key_norm):
            current_bssid = value
            continue

        if _key_matches(key, _SIGNAL_KEYS):
            if (
                current_ssid
                and current_ssid.casefold() == ssid.casefold()
                and current_bssid
            ):
                pct = _parse_signal_pct(value)
                if pct is not None:
                    results.append((current_bssid, pct))
            continue

    # Deduplicar por BSSID (quedarse con la mejor señal)
    best: dict[str, tuple[str, int]] = {}
    for bssid, pct in results:
        key = bssid.casefold()
        prev = best.get(key)
        if prev is None or pct > prev[1]:
            best[key] = (bssid, pct)
    return sorted(best.values(), key=lambda item: (-item[1], item[0].casefold()))


def measure_current_network(
    scan_delay_s: float = 0.6,
    fallback_ssid: str | None = None,
) -> WifiSample:
    """
    Mide la señal solo si hay una red WiFi ASOCIADA (conectada).

    - WiFi encendido pero sin red conectada → 0% (sin señal).
    - WiFi apagado / error de interfaz → 0% o error controlado.
    - Conectado → señal del AP asociado + lista de otros BSSID del mismo SSID.
    """
    label_ssid = (
        fallback_ssid
        if fallback_ssid and fallback_ssid not in {"?", ""}
        else "desconocida"
    )

    try:
        status = get_interface_status()
    except WifiScannerError:
        # Adaptador apagado o netsh no disponible: marcar sin señal
        return WifiSample(ssid=label_ssid, signal_pct=0, bssid="sin-señal")

    # Caso clave: WiFi prendido pero desconectado de la red → 0%,
    # NO usar el escaneo de redes visibles (eso reportaba 90% engañoso).
    if not status.connected:
        return WifiSample(
            ssid=status.ssid or label_ssid,
            signal_pct=0,
            bssid="sin-señal",
        )

    ssid = status.ssid or label_ssid
    time.sleep(scan_delay_s)

    assoc_bssid = status.bssid or "desconocido"
    assoc_signal = status.signal_pct if status.signal_pct is not None else 0

    try:
        scanned = scan_bssids_for_ssid(ssid)
    except WifiScannerError:
        scanned = []

    by_key: dict[str, tuple[str, int]] = {}
    for bssid, pct in scanned:
        key = bssid.casefold()
        prev = by_key.get(key)
        if prev is None or pct > prev[1]:
            by_key[key] = (bssid, pct)

    # Incorporar señal de la interfaz asociada (puede ser más reciente)
    if assoc_bssid and assoc_bssid.casefold() not in {"", "desconocido", "sin-señal"}:
        key = assoc_bssid.casefold()
        if key in by_key:
            listed_bssid, listed_pct = by_key[key]
            assoc_signal = max(listed_pct, assoc_signal)
            by_key[key] = (listed_bssid, assoc_signal)
        else:
            by_key[key] = (assoc_bssid, assoc_signal)

    assoc_key = assoc_bssid.casefold()
    nearby = tuple(
        ApSignal(bssid=b, signal_pct=p)
        for b, p in sorted(
            by_key.values(),
            key=lambda item: (-item[1], item[0].casefold()),
        )
        if b.casefold() != assoc_key
    )

    return WifiSample(
        ssid=ssid,
        signal_pct=assoc_signal,
        bssid=assoc_bssid,
        nearby_aps=nearby,
    )
