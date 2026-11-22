# MusicFusion

**MusicFusion** es una aplicación de escritorio y consola para Linux diseñada para consolidar y fusionar múltiples bibliotecas y carpetas de música en un único directorio de destino, garantizando la eliminación absoluta de duplicados mediante análisis de contenido real (hash SHA-256).

---

## 1. Características Principales

- **Consolidación multi-origen:** Permite seleccionar cualquier número de carpetas de origen (Carpeta A, Carpeta B, etc.) y consolidarlas en una sola carpeta de destino.
- **Detección real de duplicados por SHA-256:**
  - Dos archivos con el mismo SHA-256 son duplicados, sin importar si tienen nombres diferentes o si están repetidos múltiples veces dentro de la misma carpeta de origen o en distintas carpetas.
  - Se copia **una sola instancia** al destino.
  - Los archivos ya existentes en el destino son analizados para no volver a copiarlos ni duplicarlos. El proceso es **100% idempotente** y puede reanudarse o ejecutarse repetidamente de forma segura.
- **Resolución inteligente de conflictos de nombres:**
  - Dos archivos con el mismo nombre pero **distinto contenido** (distinto SHA-256) se conservan ambos.
  - El segundo archivo se renombra automáticamente con un sufijo incremental: `canción.mp3`, `canción (2).mp3`, `canción (3).mp3`.
- **Lectura por bloques (Streaming):** Hashes calculados en bloques de 1 MiB, optimizado para bibliotecas de decenas o cientos de miles de canciones sin agotar la memoria RAM.
- **Copiado atómico y seguro:** Los archivos se copian a un archivo temporal y luego se renombran de forma atómica (`os.replace`). Si está activa la opción de verificación, se valida el SHA-256 del archivo copiado antes de confirmarlo. **Nunca se eliminan ni modifican los archivos de origen.**
- **Interfaz Gráfica Moderna y Oscura:** Desarrollada con PySide6 (Qt), con panel de orígenes dinámico, selección de destino, opciones configurables (simulación, subdirectorios, verificación, estructura), panel de estadísticas en tiempo real, barra de progreso, velocidad (archivos/s y MB/s) y consola de eventos.
- **Informes exportables:** Resumen detallado con métricas de espacio ahorrado, listado de duplicados omitidos y exportación directa a **TXT** y **CSV**.

---

## 2. Arquitectura del Proyecto

```
src/musicfusion/
├── merge/
│   ├── engine.py         # Motor principal de fusión y deduplicación SHA-256
│   ├── planner.py        # Planificador de fusiones
│   ├── executor.py       # Ejecutor con copiado seguro e historial de deshacer
│   └── consolidator.py   # Consolidación avanzada de álbumes
├── ui/
│   ├── main_window.py    # Ventana principal y navegación
│   ├── theme.py          # Paleta y estilos oscuros modernos
│   ├── widgets.py        # Widgets reutilizables (StatCard, ProgressBar, etc.)
│   └── pages/
│       └── fusion.py     # Página interactiva de Fusión (orígenes, destino, progreso)
├── audio/
│   ├── hashing.py        # Cálculo de hashes SHA-256 por bloques
│   └── metadata.py       # Metadatos y formatos de audio soportados
├── duplicates/           # Detección y clasificación de duplicados
└── __main__.py           # Punto de entrada para GUI y CLI
```

---

## 3. Instalación

### Requisitos del Sistema
- Linux (Ubuntu, Debian, Fedora, Arch, etc.)
- Python 3.12+
- FFmpeg (opcional para análisis acústico avanzado)

### Preparación del Entorno
```bash
# Otorgar permisos de ejecución al script lanzador
chmod +x musicfusion.sh

# Comprobar y preparar el entorno automáticamente
./musicfusion.sh --check
```

---

## 4. Uso de la Aplicación

### Interfaz Gráfica (GUI)
Para iniciar la interfaz gráfica con tema oscuro:
```bash
./musicfusion.sh
# O mediante el módulo de python:
python3 -m musicfusion
```

En la pestaña **Fusión**:
1. **Orígenes:** Haz clic en *Agregar carpeta…* para sumar todas las carpetas que deseas fusionar.
2. **Destino:** Selecciona la carpeta donde deseas consolidar tu música.
3. **Opciones:**
   - *Modo simulación:* Analiza y genera el informe sin realizar copias físicas.
   - *Incluir subdirectorios:* Recorre todas las carpetas hijas de los orígenes.
   - *Verificar archivos mediante SHA-256:* Comprueba que el archivo copiado sea idéntico al original antes de darlo por bueno.
   - *Conservar estructura original:* Replica las subcarpetas de origen en el destino.
4. Pulsa **Iniciar Fusión**. Puedes cancelarla en cualquier momento de forma segura con el botón **Cancelar**.
5. Al terminar, exporta el informe con **Exportar informe TXT…** o **Exportar informe CSV…**.

---

### Línea de Comandos (CLI)
MusicFusion incluye un comando `merge` completo para automatización o ejecución headless:

```bash
# Fusión básica de dos o más directorios hacia un destino
python3 -m musicfusion merge /ruta/origen1 /ruta/origen2 /ruta/destino

# Modo simulación (dry-run)
python3 -m musicfusion merge /ruta/origen1 /ruta/origen2 /ruta/destino --dry-run

# Exportar informe detallado a archivos TXT y CSV
python3 -m musicfusion merge --sources /carpetaA /carpetaB --dest /carpetaDestino --report-txt informe.txt --report-csv informe.csv

# Conservar la jerarquía de subdirectorios
python3 -m musicfusion merge /ruta/origen1 /ruta/origen2 /ruta/destino --preserve-structure
```

---

## 5. Pruebas Automatizadas

La suite de pruebas cubre exhaustivamente los 10 escenarios críticos:
1. Duplicados con nombres iguales.
2. Duplicados con nombres diferentes.
3. Archivos con nombres iguales pero contenido diferente (resolución con sufijos incrementales `canción (2).mp3`).
4. Archivos ya existentes en el destino (idempotencia en ejecuciones repetidas).
5. Directorios anidados / jerarquías profundas.
6. Archivos vacíos (0 bytes).
7. Archivos grandes (lectura por bloques mayores a 1 MB).
8. Nombres Unicode (tildes, caracteres japoneses, cirílico, emojis).
9. Manejo de errores de permisos y archivos no legibles.
10. Cancelación cooperativa durante el procesamiento.
11. Duplicados repetidos múltiples veces en una carpeta y en otra (copia una sola instancia).

Para ejecutar los tests:
```bash
pytest tests/test_musicfusion_engine.py -v
```

---

## 6. Compilación y Distribución

Para empaquetar MusicFusion como paquete estándar de Python (Wheel y Source Distribution):
```bash
uv build
# Los archivos generados se ubicarán en dist/:
# - dist/musicfusion-0.1.0-py3-none-any.whl
# - dist/musicfusion-0.1.0.tar.gz
```

---

## 7. Licencia

GPL-3.0-or-later.
