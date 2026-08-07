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
    channel: int | None = None
    band: str = ""  # "2.4 GHz", "5 GHz", "6 GHz" o ""


@dataclass(frozen=True)
class ScannedBssid:
    """BSSID visible del SSID actual, con canal/banda si netsh lo reporta."""

    bssid: str
    signal_pct: int
    channel: int | None = None
    band: str = ""
    radio_type: str = ""


@dataclass(frozen=True)
class WifiSample:
    ssid: str
    signal_pct: int  # señal del AP asociado (conectado)
    bssid: str  # BSSID del AP asociado
    channel: int | None = None
    band: str = ""
    # Otros BSSID del mismo SSID vistos en el escaneo (menor u otra señal)
    nearby_aps: tuple[ApSignal, ...] = ()


@dataclass(frozen=True)
class InterfaceStatus:
    connected: bool
    state: str
    ssid: str | None
    signal_pct: int | None
    bssid: str
    channel: int | None = None
    band: str = ""


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
_CHANNEL_KEYS = ("Channel", "Canal")
_BAND_KEYS = ("Band", "Banda")
_RADIO_KEYS = ("Radio type", "Tipo de radio")
_CONNECTED_STATES = frozenset(
    {"connected", "conectado", "connecting", "conectando"}
)
_OPEN_AUTH = frozenset({"open", "abierta", "abierto"})
# Evita el parpadeo de CMD al lanzar netsh desde un .exe sin consola (PyInstaller).
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _parse_channel(value: str) -> int | None:
    match = re.search(r"(\d+)", value)
    if not match:
        return None
    return int(match.group(1))


def band_from_channel(channel: int | None, band_hint: str = "") -> str:
    """Deriva 2.4 / 5 / 6 GHz a partir del canal o de un texto de banda de netsh."""
    hint = (band_hint or "").casefold()
    if "6" in hint and "ghz" in hint:
        return "6 GHz"
    if "5" in hint and "ghz" in hint:
        return "5 GHz"
    if "2.4" in hint or "2,4" in hint or ("2" in hint and "ghz" in hint):
        return "2.4 GHz"

    if channel is None:
        return ""
    # Canales Wi‑Fi típicos en reportes de Windows
    if 1 <= channel <= 14:
        return "2.4 GHz"
    if 15 <= channel <= 196:
        return "5 GHz"
    if channel >= 197:
        return "6 GHz"
    return ""


def normalize_bssid(bssid: str) -> str:
    """Unifica separadores a ':' y minúsculas para comparar."""
    return bssid.replace("-", ":").casefold().strip()


def _run_netsh(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["netsh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=_CREATE_NO_WINDOW,
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
    channel: int | None = None
    band_hint = ""

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
        elif _key_matches(key, _CHANNEL_KEYS):
            channel = _parse_channel(value)
        elif _key_matches(key, _BAND_KEYS):
            band_hint = value

    connected = state.casefold() in _CONNECTED_STATES and bool(ssid)
    return InterfaceStatus(
        connected=connected,
        state=state or "desconocido",
        ssid=ssid,
        signal_pct=signal,
        bssid=bssid,
        channel=channel,
        band=band_from_channel(channel, band_hint),
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


def scan_bssids_for_ssid(ssid: str) -> list[ScannedBssid]:
    """
    Escanea redes visibles y devuelve BSSID del SSID indicado
    con señal, canal y banda (2.4 / 5 / 6 GHz) cuando netsh lo reporta.
    """
    output = _run_netsh(["wlan", "show", "networks", "mode=bssid"])
    lines = output.splitlines()

    results: list[ScannedBssid] = []
    current_ssid: str | None = None
    current_bssid: str | None = None
    current_signal: int | None = None
    current_channel: int | None = None
    current_band_hint = ""
    current_radio = ""

    def _flush_bssid() -> None:
        nonlocal current_bssid, current_signal, current_channel
        nonlocal current_band_hint, current_radio
        if (
            current_ssid
            and current_ssid.casefold() == ssid.casefold()
            and current_bssid
            and current_signal is not None
        ):
            results.append(
                ScannedBssid(
                    bssid=current_bssid,
                    signal_pct=current_signal,
                    channel=current_channel,
                    band=band_from_channel(current_channel, current_band_hint),
                    radio_type=current_radio,
                )
            )
        current_bssid = None
        current_signal = None
        current_channel = None
        current_band_hint = ""
        current_radio = ""

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
            _flush_bssid()
            current_ssid = value
            continue

        if key_norm == "ssid" or key_norm.endswith(" de ssid"):
            _flush_bssid()
            current_ssid = value
            continue

        if _key_matches(key, _BSSID_KEYS) or re.match(r"^bssid\s*\d*$", key_norm):
            _flush_bssid()
            current_bssid = value
            continue

        if _key_matches(key, _SIGNAL_KEYS):
            pct = _parse_signal_pct(value)
            if pct is not None:
                current_signal = pct
            continue

        if _key_matches(key, _CHANNEL_KEYS):
            current_channel = _parse_channel(value)
            continue

        if _key_matches(key, _BAND_KEYS):
            current_band_hint = value
            continue

        if _key_matches(key, _RADIO_KEYS):
            current_radio = value
            continue

    _flush_bssid()

    # Deduplicar por BSSID normalizado (mejor señal; conservar canal/banda)
    best: dict[str, ScannedBssid] = {}
    for item in results:
        key = normalize_bssid(item.bssid)
        prev = best.get(key)
        if prev is None or item.signal_pct > prev.signal_pct:
            best[key] = item
        elif prev is not None and item.signal_pct == prev.signal_pct:
            if not prev.band and item.band:
                best[key] = item
    return sorted(
        best.values(),
        key=lambda item: (-item.signal_pct, normalize_bssid(item.bssid)),
    )


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
    assoc_channel = status.channel
    assoc_band = status.band

    try:
        scanned = scan_bssids_for_ssid(ssid)
    except WifiScannerError:
        scanned = []

    by_key: dict[str, ScannedBssid] = {}
    for item in scanned:
        key = normalize_bssid(item.bssid)
        prev = by_key.get(key)
        if prev is None or item.signal_pct > prev.signal_pct:
            by_key[key] = item

    # Incorporar señal de la interfaz asociada (puede ser más reciente)
    if normalize_bssid(assoc_bssid) not in {"", "desconocido", "sin-señal"}:
        key = normalize_bssid(assoc_bssid)
        if key in by_key:
            listed = by_key[key]
            assoc_signal = max(listed.signal_pct, assoc_signal)
            if assoc_channel is None:
                assoc_channel = listed.channel
            if not assoc_band:
                assoc_band = listed.band
            by_key[key] = ScannedBssid(
                bssid=listed.bssid,
                signal_pct=assoc_signal,
                channel=assoc_channel if assoc_channel is not None else listed.channel,
                band=assoc_band or listed.band,
                radio_type=listed.radio_type,
            )
        else:
            by_key[key] = ScannedBssid(
                bssid=assoc_bssid,
                signal_pct=assoc_signal,
                channel=assoc_channel,
                band=assoc_band,
            )

    assoc_key = normalize_bssid(assoc_bssid)
    nearby = tuple(
        ApSignal(
            bssid=item.bssid,
            signal_pct=item.signal_pct,
            channel=item.channel,
            band=item.band,
        )
        for item in sorted(
            by_key.values(),
            key=lambda it: (-it.signal_pct, normalize_bssid(it.bssid)),
        )
        if normalize_bssid(item.bssid) != assoc_key
    )

    return WifiSample(
        ssid=ssid,
        signal_pct=assoc_signal,
        bssid=assoc_bssid,
        channel=assoc_channel,
        band=assoc_band,
        nearby_aps=nearby,
    )
