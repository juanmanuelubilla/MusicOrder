#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
FALLBACK_VENV_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/musicfusion/venv"
FALLBACK_VENV_PY="$FALLBACK_VENV_DIR/bin/python"
NO_INSTALL=0
CHECK_ONLY=0
PYTHON_CMD="${MUSICFUSION_PYTHON:-}"

# Si el FS local no soporta symlinks, recordamos dónde quedó el venv de respaldo.
VENV_LOCATION_MARKER="$SCRIPT_DIR/.venv.location"
if [[ ! -x "$VENV_PY" ]] && [[ -r "$VENV_LOCATION_MARKER" ]] \
   && read -r _loc < "$VENV_LOCATION_MARKER" && [[ -n "$_loc" ]] \
   && [[ -x "$_loc/bin/python" ]]; then
  VENV_DIR="$_loc"
  VENV_PY="$_loc/bin/python"
fi

warn() { echo "[musicfusion] $*" >&2; }
info() { echo "[musicfusion] $*"; }

find_system_python() {
  if [[ -n "$PYTHON_CMD" ]] && command -v "$PYTHON_CMD" >/dev/null 2>&1; then
    return 0
  fi
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_CMD="$candidate"
      return 0
    fi
  done
  warn "No se encontró Python. Instale python3 (>= 3.12)."
  return 1
}

system_python_ok() {
  "$PYTHON_CMD" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null
}

venv_exists() {
  [[ -x "$VENV_PY" ]]
}

venv_python_ok() {
  "$VENV_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null
}

deps_installed() {
  "$VENV_PY" -c "import musicfusion, sqlalchemy, mutagen, rapidfuzz, yaml, PySide6" >/dev/null 2>&1
}

create_venv() {
  info "Creando entorno virtual ($VENV_DIR)…"
  rm -rf "$VENV_DIR"
  if command -v uv >/dev/null 2>&1; then
    if uv venv "$VENV_DIR" >/dev/null 2>&1 && [[ -x "$VENV_PY" ]]; then
      return 0
    fi
    rm -rf "$VENV_DIR"
    info "uv venv falló en este sistema de ficheros; se reintenta con --copies…"
  fi
  if ! "$PYTHON_CMD" -m venv --copies "$VENV_DIR" >/dev/null 2>&1; then
    warn "No se pudo crear venv en $VENV_DIR (¿montaje sin symlinks?)."
    return 1
  fi
  # Un FS sin symlinks no acepta lib64 -> lib; lo dejamos como dir real.
  if [[ -L "$VENV_DIR/lib64" ]]; then
    rm -f "$VENV_DIR/lib64"
    mkdir "$VENV_DIR/lib64"
  fi
  [[ -x "$VENV_PY" ]] && return 0
  warn "El sistema de ficheros no soporta symlinks y el venv quedó incompleto."
  return 1
}

use_fallback_venv() {
  if [[ "$VENV_PY" == "$FALLBACK_VENV_PY" ]]; then
    return 0
  fi
  info "Se usará un venv dedicado en: $FALLBACK_VENV_DIR"
  VENV_DIR="$FALLBACK_VENV_DIR"
  VENV_PY="$FALLBACK_VENV_PY"
  printf '%s\n' "$VENV_DIR" > "$VENV_LOCATION_MARKER"
}

install_python_deps() {
  info "Instalando dependencias de Python con extras de test…"
  local pip_cmd=""
  if command -v uv >/dev/null 2>&1; then
    (cd "$SCRIPT_DIR" && uv pip install --python "$VENV_PY" -e ".[test]") || return 1
  elif "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    (cd "$SCRIPT_DIR" && "$VENV_PY" -m pip install -e ".[test]") || return 1
  else
    "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 && \
      (cd "$SCRIPT_DIR" && "$VENV_PY" -m pip install -e ".[test]") || return 1
  fi
}

missing_system_deps() {
  local missing=()
  command -v ffmpeg >/dev/null 2>&1 || missing+=("ffmpeg")
  printf '%s\n' "${missing[@]}"
}

install_system_ffmpeg() {
  info "Instalando ffmpeg (herramienta de sondeo/fingerprint)…"
  local manager=""
  command -v apt-get >/dev/null 2>&1 && manager="apt"
  command -v dnf      >/dev/null 2>&1 && manager="dnf"
  command -v pacman   >/dev/null 2>&1 && manager="pacman"
  command -v zypper   >/dev/null 2>&1 && manager="zypper"
  case "$manager" in
    apt)
      sudo apt-get update >/dev/null 2>&1 || true
      sudo apt-get install -y ffmpeg
     ;;
    dnf)
      sudo dnf install -y ffmpeg
      ;;
    pacman)
      sudo pacman -Sy --noconfirm ffmpeg
      ;;
    zypper)
      sudo zypper install -y ffmpeg
      ;;
    *)
      warn "Distribución no reconocida. Instale ffmpeg manualmente."
      return 1
      ;;
  esac
}

ensure_ready() {
  if ! find_system_python; then return 1; fi

  local need_install=0
  if ! system_python_ok; then
    warn "Se requiere Python >= 3.12 (instalado: $("$PYTHON_CMD" --version 2>&1 | cut -d' ' -f2))."
    return 1
  fi

  if ! venv_exists; then
    if (( NO_INSTALL )); then warn "Falta .venv y se omite la instalación (--no-install)."; return 1; fi
    if ! create_venv; then
      use_fallback_venv || true
      if [[ ! -x "$VENV_PY" ]]; then
        create_venv || return 1
      fi
    fi
    if ! deps_installed; then
      install_python_deps || return 1
      need_install=1
    fi
  elif ! venv_python_ok; then
    warn "El intérprete del .venv es demasiado antiguo; recreando…"
    rm -rf "$VENV_DIR"
    if ! create_venv; then
      use_fallback_venv || true
      if [[ ! -x "$VENV_PY" ]]; then
        create_venv || return 1
      fi
    fi
    install_python_deps || return 1
    need_install=1
  elif ! deps_installed; then
    info "Faltan dependencias en el .venv."
    if (( NO_INSTALL )); then warn "--no-install: no se instalan dependencias."; return 1; fi
    install_python_deps || return 1
    need_install=1
  fi

  local missing
  missing=$(missing_system_deps)
  if [[ -n "$missing" ]]; then
    info "Dependencias del sistema no encontradas: $(echo "$missing" | tr '\n' ' ')"
    if (( NO_INSTALL )); then
      warn "No se instala ffmpeg (--no-install). El sondeo de calidad estará limitado."
    else
      install_system_ffmpeg
    fi
  fi

  if (( need_install )); then
    info "Dependencias listas."
  else
    info "Todo en orden: no se necesita instalar nada."
  fi
}

main() {
  local app_args=()
  for arg in "$@"; do
    case "$arg" in
      --check-only) CHECK_ONLY=1 ;;
      --no-install) NO_INSTALL=1 ;;
      --check) CHECK_ONLY=1 ;;
      *) app_args+=("$arg") ;;
    esac
  done

  if ! ensure_ready; then
    warn "La aplicación no puede arrancar sin preparar las dependencias."
    warn "Ejecute:  bash \"$0\"  (o quite --no-install) para auto-instalar."
    exit 1
  fi

  if (( CHECK_ONLY )); then
    info "Comprobación correcta: entorno preparado."
    "$VENV_PY" -c "import musicfusion; print('musicfusion', musicfusion.__version__, '| python', __import__('sys').version.split()[0])"
    exit 0
  fi

  if [[ "$(id -u)" -eq 0 ]]; then
    warn "Ejecutando como root. Esto es innecesario y suele romper permisos."
    warn "Use:  ./musicfusion.sh   (sin sudo)"
    if [[ -n "${SUDO_USER:-}" ]]; then
      warn "Sugerencia: sudo -u \"$SUDO_USER\" \"$0\" \"$*\""
    fi
    exit 1
  fi

  if ((${#app_args[@]} == 0)) && [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    warn "No se detecta pantalla gráfica (X11/Wayland)."
    warn "Ejecute la GUI desde el escritorio, o use un subcomando en consola:"
    warn "   $0 scan"
    warn "   $0 duplicates --report informe.html"
    warn "   $0 merge /musica/A /musica/B /musica/Destino --dry-run"
    if [[ -n "${SSH_CONNECTION:-}" ]]; then
      warn "(sesión SSH: export DISPLAY=:0 antes de lanzar la GUI si procede)"
    fi
    exit 1
  fi

  info "Ejecutando $VENV_PY -m musicfusion ${app_args[*]}"
  exec "$VENV_PY" -m musicfusion "${app_args[@]}"
}

main "$@"