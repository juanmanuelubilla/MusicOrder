# MusicFusion Pro — Guía de línea de comandos

## Cómo ejecutar la aplicación

### Lanzador automático (recomendado)

```bash
./musicfusion.sh
```

Comprueba e instala solo lo que falte (venv, dependencias, ffmpeg) y abre la GUI.
Flags útiles:

```bash
./musicfusion.sh --check-only              # solo comprobar el entorno
./musicfusion.sh --no-install              # no instalar nada automáticamente
```

### Modo manual

```bash
.venv/bin/python -m musicfusion            # abre la interfaz gráfica
.venv/bin/python -m musicfusion --headless COMANDO   # sin GUI
```

Si no hay display disponible, la GUI no arranca: usá subcomandos.

---

## COMANDOS

### 1. Escanear bibliotecas

Indexa las bibliotecas registradas (solo re-analiza archivos nuevos o modificados).

```bash
python -m musicfusion scan                 # escaneo incremental
python -m musicfusion scan --full          # re-escaneo completo
python -m musicfusion scan --rehash        # recalcular hashes
```

---

### 2. INCORPORAR música nueva (lo más usado)

Agrega al MAESTRO (PATH 1) únicamente lo que falta de la colección nueva (PATH 2).
Entiende diferencias de tildes, mayúsculas, guiones y usa los tags de los archivos.

```bash
# PASO 1: ver qué pasaría (NO escribe nada)
python -m musicfusion merge-collection \
    --master /ruta/coleccion/ordenada \
    --source /ruta/musica/nueva
```

Salida típica:

```
[EXISTE   NO_HACER_NADA] Babasonicos/1992 - Pasto      ← ya lo tenés
[FALTA    COPIAR       ] Babasonicos/1996 - Dopadromo → Babasónicos/...
[FALTA    COPIAR       ] Charly García                ← artista nuevo
```

```bash
# PASO 2: aplicar de verdad (pide confirmación "si/no")
python -m musicfusion merge-collection \
    --master /ruta/maestro \
    --source /ruta/nueva \
    --execute

# sin pregunta interactiva:
... --execute --yes

# resultado en JSON (para scripts):
... --json
```

Garantías: nunca borra, nunca sobreescribe lo existente del maestro,
el destino usa SIEMPRE el nombre de carpeta del maestro.

Solo análisis sin acciones (para agentes/scripts):

```bash
python -m musicfusion sync-plan --master /P1 --incoming /P2 [--json]
```

---

### 3. Verificar álbumes contra MusicBrainz

Compara tus carpetas de álbum con las tracklists oficiales, tema por tema,
y te dice exactamente qué falta.

```bash
python -m musicfusion albums --path /ruta/coleccion
```

Variantes:

```bash
... --offline        # rápido, SIN internet: detecta duplicados/ediciones locales
... --refresh        # ignorar caché y re-consultar todo
... --ai             # segunda opinión de tu Ollama en casos tipo "Mucho" vs "Mucho +"
... --ai --model hermes-128k:latest --host http://192.168.1.50:11434
```

Categorías del modo offline:
`[=] DUPLICADO_PROBABLE` · `[~] POSIBLE_EDICION` · `[?] REVISAR_MISMO_NOMBRE`

El modo online queda cacheado ~30 días (`~/.config/musicfusion/mb_albums_cache.json`),
así que las corridas siguientes son rápidas y solo completan lo faltante.

---

### 4. Buscar duplicados

```bash
python -m musicfusion duplicates                        # listar grupos
python -m musicfusion duplicates --report dupes.html    # + informe HTML
```

---

### 5. Comparar dos archivos

¿Son el mismo tema? ¿Cuál tiene mejor calidad?

```bash
python -m musicfusion match cancionA.flac cancionB.flac          # desglose humano
python -m musicfusion match a.flac b.flac --json                  # JSON
python -m musicfusion match a.flac b.flac --persist               # guardar en BD
python -m musicfusion match a.flac b.flac --context dirA dirB     # refuerzo por álbum
```

---

### 6. Huellas acústicas

Mejora la precisión de duplicados/fusión calculando huellas chromaprint.

```bash
python -m musicfusion fingerprint                    # bibliotecas configuradas
python -m musicfusion fingerprint --path /ruta/x     # una carpeta concreta
python -m musicfusion fingerprint --force            # recalcular todo
```

---

### 7. Fusión clásica de dos carpetas

Copia al destino conservando solo la mejor copia de cada tema (plan reversible).

```bash
# primero el plan (nada se copia):
python -m musicfusion merge /music/A /music/B /destino --dry-run

# después la ejecución real:
python -m musicfusion merge /music/A /music/B /destino
```

---

### 8. Sugerencias de metadatos

Propone completar tags copiando campos de la copia más completa de cada tema.

```bash
python -m musicfusion suggest                     # solo ver propuestas
python -m musicfusion suggest --apply             # escribirlas en los archivos
python -m musicfusion suggest --dirs A B --min-gain 15
```

---

### 9. Informes

```bash
python -m musicfusion report --kind html     # csv | xlsx | pdf
```

---

### 10. Ayuda

```bash
python -m musicfusion --help
python -m musicfusion COMANDO --help
```

---

## FLUJO TÍPICO COMPLETO

```bash
cd /home/pi/MusicOrder

scan                                                    # 1. indexar tu música
albums --path /maestro --offline                        # 2. revisar duplicados entre álbumes (rápido)
albums --path /maestro                                  # 3. verificación oficial online (cacheada)
duplicates --report dupes.html                          # 4. revisar repetidos
merge-collection --master /maestro --source /nueva      # 5. dry-run de incorporación
merge-collection --master /maestro --source /nueva --execute   # 6. incorporar
report --kind html                                      # 7. informes finales
```

## Configuración

Archivo: `~/.config/musicfusion/config.yaml` (se crea solo en el primer arranque).
Claves usadas frecuentemente:

```yaml
ollama: { enabled: false, host: "http://localhost:11434", model: "" }
sync: { match_threshold: 0.90, ambiguous_threshold: 0.72 }
matching: { version_duration_gap: 0.12 }
candidates: { artist_threshold: 0.75, album_threshold: 0.80 }
```

---

## INTEGRACIÓN CON HERMES (flujo por archivos, sin Python)

Hermes puede usar MusicOrder como motor determinístico y aportar la
inteligencia/internet sobre los casos ambiguos:

```bash
# 1) MusicOrder analiza y emite el plan estructurado
python -m musicfusion sync-plan --master /P1 --incoming /P2 --json > plan.json

# 2) Hermes lee plan.json:
#    - "new_albums_to_copy": lista lista para accionar
#    - "needs_review": ids ambiguos con toda la evidencia textual
#      (nombres, años, conteos de temas, tracklists) — nunca audio

# 3) Hermes escribe sus veredictos
cat > decisions.json << 'JSON'
{
  "artist:Babasonikos": "MATCH",          // o IGNORE
  "album:Babasonikos/X": "NEW_ALBUM"     // o EXISTS / IGNORE
}
JSON

# 4) MusicOrder aplica las decisiones y produce el plan final
python -m musicfusion sync-resolve \
    --plan plan.json --decisions decisions.json --out final.json
```

Notas:
- ids inválidos vuelven en `result.rejected` con motivo `"id desconocido"`
- el plan resuelto recalcula `new_albums_to_copy` y `needs_review`
- la IA local (Ollama) NO necesita internet: MusicOrder le lleva la
  evidencia (MusicBrainz/Wikipedia) como texto. Hermes puede reemplazar
  esa capa cuando prefiera razonamiento conectado.
