# Mapa de calor WiFi

Programa para Windows que genera un mapa de calor de la cobertura WiFi del lugar donde estás.

Mide la red actual incluyendo todos los repetidores (BSSID) del mismo SSID. El lienzo es un **plano cartesiano** con el origen **(0, 0) en el centro** y alcance por defecto de **±15 m**. Puedes cargar una **imagen del plano del lugar**, medir sobre ella y expandir el área. Al guardar se exporta una **imagen PNG**.

## Requisitos

- Windows con adaptador WiFi
- Python 3.10+

## Instalación

```powershell
cd $env:USERPROFILE\Desktop\mapa-calor-wifi
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Uso

```powershell
python main.py
```

1. (Opcional) Pulsa **p** y elige una imagen del plano (PNG/JPG). También puedes dejarla en la carpeta `planos/`.
2. Indica la escala real del lugar:
   - **Ancho** (eje X): por defecto **50 m**
   - **Largo** (eje Y, midiendo todo): por defecto **96.90 m**
3. El origen **(0, 0)** es el centro del plano. Alinea mentalmente tu posición real con la imagen.
4. Haz **clic** donde estés para medir la señal.
5. Repite en varios puntos (recomendado: 8–10 o más).
6. Pulsa **h** para generar el mapa de calor (semitransparente sobre el plano).
7. Pulsa **s** para guardar como **imagen PNG** en `data/`.

### Teclas

| Tecla | Acción |
|-------|--------|
| Clic | Medir señal en esa posición |
| `p` | Cargar imagen del plano del lugar |
| `c` | Quitar el plano (volver al lienzo vacío) |
| `e` | Expandir ancho y alto (+5 m al total) |
| `w` | Expandir solo ancho (+5 m al total) |
| `r` | Expandir solo alto (+5 m al total) |
| `h` | Generar / actualizar heatmap |
| `s` | Guardar imagen PNG |
| `z` | Deshacer último punto |
| `q` | Salir |

## Notas

- La señal se toma como el **máximo %** entre todos los BSSID del SSID actual.
- Si el WiFi está **sin red conectada** (aunque el adaptador siga encendido) o se pierde la asociación, el punto se marca **0% en violeta**. No se usa el escaneo de redes “visibles” en ese caso (evita falsos 90%).
- Al cargar un plano se pregunta **ancho** (por defecto 50 m) y **largo** (por defecto 96.90 m). La imagen se ajusta a ese rectángulo en metros.
- Con menos de 8 puntos el heatmap es preliminar.
