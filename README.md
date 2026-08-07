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
7. Pulsa **g** para **guardar el proyecto** (`.wifimap`) y poder continuar después.
8. Pulsa **o** para **abrir un proyecto** guardado y seguir midiendo.
9. Pulsa **s** para exportar **imágenes PNG** en `data/` (no sustituye al proyecto).

### Teclas

| Tecla | Acción |
|-------|--------|
| Clic | Medir señal; si hay un punto cerca, muestra su info (sin re-medir) |
| Clic derecho | Quitar el punto o la marca de AP cercanos |
| `a` | Marcar ubicación del AP conectado |
| `p` | Cargar imagen del plano del lugar |
| `c` | Quitar el plano (volver al lienzo vacío) |
| `e` | Expandir ancho y alto del lienzo (+10 m; el plano no se estira) |
| `w` | Expandir solo ancho del lienzo (+10 m) |
| `r` | Expandir solo alto del lienzo (+10 m) |
| `h` | Generar / actualizar heatmap |
| `g` | Guardar proyecto (`.wifimap`); si ya existe, lo sobrescribe |
| `Ctrl+g` | Guardar proyecto como (elegir otro archivo) |
| `o` | Abrir proyecto guardado |
| `s` | Exportar imágenes PNG |
| `z` | Deshacer último punto |
| `q` | Salir |

### Proyectos (continuar trabajo)

- Archivos en `data/proyectos/` con extensión **`.wifimap`**.
- Incluyen puntos de medición, tamaño del lienzo, AP marcados, si el heatmap estaba visible y la referencia al plano (ruta de la imagen + escala).
- Al abrir un proyecto puedes seguir haciendo clic para medir; el estado WiFi actual se lee del sistema (no del archivo).

## Notas

- En cada medición se guarda la señal del **AP conectado** y, si aparecen, la de **otros BSSID del mismo SSID** (repetidores cercanos). Al hacer clic en un punto se muestran ambos.
- Cada AP muestra su **banda** (2.4 / 5 / 6 GHz) según el canal reportado por Windows, para distinguir radios del mismo equipo.
- Si un AP **no está marcado** a mano (`a`), con al menos 2 lecturas de su señal se muestra una **posición estimada** (triángulo azul vacío, «AP est.»), calculada por centroide ponderado por %. Al marcarlo a mano, la estimación se sustituye.
- El mapa de calor usa la señal del AP al que estabas asociado en cada punto.
- Si el WiFi está **sin red conectada** (aunque el adaptador siga encendido) o se pierde la asociación, el punto se marca **0% en violeta**. No se usa el escaneo de redes “visibles” en ese caso (evita falsos 90%).
- Al cargar un plano se pregunta **ancho** (por defecto 50 m) y **largo** (por defecto 96.90 m). La imagen se ajusta a ese rectángulo en metros.
- Con menos de 8 puntos el heatmap es preliminar.
