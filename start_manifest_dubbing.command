#!/usr/bin/env bash
# Start the selected current-manifest renderer profile; legacy invocation remains GPU.
set -euo pipefail
PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$PROJECT_ROOT"
PYTHON="$PROJECT_ROOT/work/python/cpython-3.12.14-macos-aarch64-none/bin/python3.12"
[[ -x "$PYTHON" ]] || { printf 'Portable Python-Laufzeit fehlt.\n' >&2; exit 1; }
MANIFEST="${1:-$(dirname -- "$PROJECT_ROOT")/mi1-render.manifest.json}"
if [[ $# -ge 2 ]]; then
  MODE="$2"
elif [[ -t 0 ]]; then
  printf '\nRendermodus auswählen:\n'
  printf '  1) Schnellstes Rendern – ANE+GPU, freie Zeilenvergabe, mittlere Qualität\n'
  printf '  2) Schnelles Rendern – ANE+GPU, feste Figurenbindung, mittlere Qualität\n'
  printf '  3) Mittlere Geschwindigkeit – ANE, mittlere Qualität\n'
  printf '  4) Langsames Rendern – GPU, höchste Qualität\n'
  printf '  q) Abbrechen\n'
  read -r -p 'Auswahl [1–4/q]: ' CHOICE
  case "$CHOICE" in
    1) MODE=ane-gpu-free ;;
    2) MODE=ane-gpu ;;
    3) MODE=ane ;;
    4) MODE=gpu ;;
    *) printf 'Abgebrochen; Render wurde nicht gestartet.\n'; exit 0 ;;
  esac
else
  MODE=gpu
fi
ANE_CONFIG="${3:-}"
[[ -f "$MANIFEST" ]] || { printf 'Rendermanifest fehlt: %s\n' "$MANIFEST" >&2; exit 1; }
case "$MODE" in
  gpu|cpu|ane) BACKEND="$MODE"; OPERATING_MODE=single; ROUTING_POLICY=fixed-character-v1 ;;
  ane-gpu) BACKEND=gpu; OPERATING_MODE=parallel_gpu_ane; ROUTING_POLICY=fixed-character-v1 ;;
  ane-gpu-free) BACKEND=gpu; OPERATING_MODE=parallel_gpu_ane; ROUTING_POLICY=free-cue-v1 ;;
  *) printf 'Modus muss gpu, cpu, ane, ane-gpu oder ane-gpu-free sein.\n' >&2; exit 2 ;;
esac
if [[ "$MODE" == ane || "$MODE" == ane-gpu || "$MODE" == ane-gpu-free ]]; then
  if [[ -z "$ANE_CONFIG" ]]; then ANE_CONFIG="$PROJECT_ROOT/work/ane-worker-config-split.json"; fi
  ANE_PYTHON="$PROJECT_ROOT/work/ane-runtime-venv/bin/python"
  [[ -x "$ANE_PYTHON" ]] || { printf 'ANE-Umgebung fehlt. Setup: python3 tools/setup_ane_runtime.py . --install-python-runtime\n' >&2; exit 1; }
  PYTHON="$ANE_PYTHON"
fi
printf 'Vorläufige Hardwareempfehlung (ändert die Auswahl nicht):\n'
"$PYTHON" -m manifest_renderer.hardware --disk-path "$PROJECT_ROOT"
mkdir -p "$PROJECT_ROOT/logs"
export HF_HOME="$PROJECT_ROOT/cache/hf"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export UV_CACHE_DIR="$PROJECT_ROOT/cache/uv"
export XDG_CACHE_HOME="$PROJECT_ROOT/cache"
export TMPDIR="$PROJECT_ROOT/work/manifest-tmp"
mkdir -p "$TMPDIR"
export PYTHONDONTWRITEBYTECODE=1
LOG_FILE="$PROJECT_ROOT/logs/manifest_full_$(date '+%Y%m%d_%H%M%S').log"
printf 'Manifest: %s\nAusgewählter Modus: %s\nLog: %s\n\n' "$MANIFEST" "$MODE" "$LOG_FILE"
RUN_LABEL="route-${ROUTING_POLICY}-${MODE}"
set +e
if [[ -n "$ANE_CONFIG" ]]; then
  caffeinate -i "$PYTHON" -u render_dubbing_manifest.py run-mi1 "$MANIFEST" --asset-root "$PROJECT_ROOT" --backend "$BACKEND" --operating-mode "$OPERATING_MODE" --routing-policy "$ROUTING_POLICY" --run-label "$RUN_LABEL" --ane-worker-config "$ANE_CONFIG" 2>&1 | tee "$LOG_FILE"
else
  caffeinate -i "$PYTHON" -u render_dubbing_manifest.py run-mi1 "$MANIFEST" --asset-root "$PROJECT_ROOT" --backend "$BACKEND" --operating-mode "$OPERATING_MODE" --routing-policy "$ROUTING_POLICY" --run-label "$RUN_LABEL" 2>&1 | tee "$LOG_FILE"
fi
RUN_STATUS=${PIPESTATUS[0]}
set -e
printf '\nLauf beendet. Bericht und review.html liegen unter output/manifest-runs/.\n'
if [[ "$RUN_STATUS" -eq 2 ]]; then printf 'Alle IDs abgearbeitet; einzelne Zeilen sind nach allen Versuchen fehlgeschlagen. Siehe qa-summary.json.\n'; fi
if [[ "$RUN_STATUS" -eq 1 ]]; then printf 'Lauf unterbrochen. Fehler steht im Log; nach Behebung denselben Starter erneut ausführen.\n'; fi
exit "$RUN_STATUS"
