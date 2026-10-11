#!/usr/bin/env bash
# Run the Remote Compose player comparison over a bundle written by rc_compare_bundle.py.
#
# Mirrors the "PNG vs Remote Compose parity page" step of compose-ai-tools'
# design-artifacts-reusable.yml: rc-compare.mjs stages the documents once, each embedded
# player's Gradle harness in yschimke/rc-players renders them, and a final rc-compare.mjs run
# renders the js lane and diffs every lane against the baked PNG.
#
# Lanes are fail-soft one by one: a harness that cannot run drops its column, not the job.
# Only the js lane (the base of rc-compare.mjs) is required.
#
# Environment:
#   DRIVER_DIR         the installed @design-parity/export-driver (holds rc-compare.mjs)
#   COMPOSE_AI_TOOLS   compose-ai-tools checkout (player staging tasks, vendored fonts)
#   RC_PLAYERS         rc-players checkout (embedded harnesses); lanes skip if absent
#   BUNDLE             bundle zip from `rc_compare_bundle.py bundle`
#   OUT                rc-compare.mjs output directory
#   WORK               scratch directory for staged inputs, lane renders and logs
#   SYSTEM             catalog id for the page (default remote-compose-examples)
#   RC_EMBEDDED_LANE      true|false  vendored + androidx embedded Android lanes (default true)
#   RC_EMBEDDED_JVM_LANE  true|false  cmp-jvm desktop lane (default true)
#   RC_CMP_WASM_LANE      true|false  cmp-wasm lane, report-only here (default false)

set -euo pipefail

: "${DRIVER_DIR:?}" "${COMPOSE_AI_TOOLS:?}" "${BUNDLE:?}" "${OUT:?}" "${WORK:?}"
RC_PLAYERS="${RC_PLAYERS:-}"
SYSTEM="${SYSTEM:-remote-compose-examples}"
RC_EMBEDDED_LANE="${RC_EMBEDDED_LANE:-true}"
RC_EMBEDDED_JVM_LANE="${RC_EMBEDDED_JVM_LANE:-true}"
RC_CMP_WASM_LANE="${RC_CMP_WASM_LANE:-false}"

BUNDLE="$(realpath "$BUNDLE")"
mkdir -p "$OUT" "$WORK"
OUT="$(realpath "$OUT")"
WORK="$(realpath "$WORK")"
FONTS="$COMPOSE_AI_TOOLS/samples/cmp-wasm-catalog/src/wasmJsMain/resources/fonts"
# The shared Google Fonts cache, laid out as the Gradle plugin's composeAiFontsCacheDir. A cold
# cache fetches, the same way the browser lane fetches the same families from the CSS API.
FONTS_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/composeai/fonts"
summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
# Every requested lane that dropped its column, one per line. `rc_compare_bundle.py install` reads
# it so an incomplete comparison never records the cache key, and the next run retries the lane.
DROPPED="$WORK/dropped-lanes"
: > "$DROPPED"

(cd "$COMPOSE_AI_TOOLS" && ./gradlew stageVendoredRcPlayerJs --quiet)
PLAYER="$COMPOSE_AI_TOOLS/build/vendored-rc-player-js/bundle.js"
EXTRA_ARGS=()

has_png() { [ -n "$(find "$1" -maxdepth 1 -name '*.png' -print -quit 2>/dev/null)" ]; }

# Say WHY a lane dropped its column: every harness writes `<id>.error` per failed document and
# exits zero, so a lane failing on every document leaves only those files behind.
report_dropped_lane() {
  local label="$1" outdir="$2" log="$3" errors=0
  echo "::warning::${label} lane produced no renders; publishing the comparison without its column"
  echo "$label" >> "$DROPPED"
  [ -d "$outdir" ] && errors=$(find "$outdir" -maxdepth 1 -name '*.error' | wc -l)
  {
    echo "### ${label} lane dropped its column"
    echo
    if [ "$errors" -gt 0 ]; then
      echo "The harness failed on every document: **${errors}** \`.error\` file(s), no PNGs. Most common reasons:"
      echo
      echo '```'
      for f in "$outdir"/*.error; do [ -f "$f" ] && { cat "$f"; echo; }; done \
        | sort | uniq -c | sort -rn | awk 'NR <= 5'  # awk drains the pipe; `head` would SIGPIPE
      echo '```'
    elif [ -s "$log" ]; then
      echo "The harness wrote nothing. Last lines of its Gradle output:"
      echo
      echo '```'
      tail -n 30 "$log"
      echo '```'
    else
      echo "The harness wrote nothing and logged nothing."
    fi
    echo
  } >> "$summary"
}

run_compare() {
  node "$DRIVER_DIR/rc-compare.mjs" --bundle "$BUNDLE" --player "$PLAYER" --fonts "$FONTS" \
    --out "$OUT" "$@"
}

if [ "$RC_EMBEDDED_LANE" = "true" ] || [ "$RC_EMBEDDED_JVM_LANE" = "true" ]; then
  if [ -z "$RC_PLAYERS" ] || [ ! -x "$RC_PLAYERS/gradlew" ]; then
    echo "::warning::no rc-players checkout at '${RC_PLAYERS}'; the embedded player lanes are skipped"
    RC_EMBEDDED_LANE=false
    RC_EMBEDDED_JVM_LANE=false
  else
    RC_IN="$WORK/rc-embedded-in"
    rm -rf "$RC_IN"
    run_compare --stage-embedded "$RC_IN"
  fi
fi

# Robolectric (Android) lanes: the vendored/local-patch player and AndroidX's released one.
# `--rerun` because the harness's PNGs are not declared task outputs, so Gradle could otherwise
# report the task UP-TO-DATE having written nothing.
if [ "$RC_EMBEDDED_LANE" = "true" ]; then
  RC_OUT="$WORK/rc-embedded-out"
  rm -rf "$RC_OUT"
  if (cd "$RC_PLAYERS" && ./gradlew :third-party-rc-embedded-player:testDebugUnitTest \
        --no-daemon --rerun --quiet \
        --tests ee.schimke.composeai.rcembedded.RcEmbeddedRenderHarness \
        -Prc.embedded.input="$RC_IN" -Prc.embedded.output="$RC_OUT" \
        -Pcomposeai.fonts.cacheDir="$FONTS_CACHE") >"$WORK/rc-embedded.log" 2>&1 \
     && has_png "$RC_OUT"; then
    EXTRA_ARGS+=(--embedded "$RC_OUT")
  else
    report_dropped_lane "vendored embedded-player" "$RC_OUT" "$WORK/rc-embedded.log"
  fi

  RC_ANDROIDX_OUT="$WORK/rc-androidx-embedded-out"
  rm -rf "$RC_ANDROIDX_OUT"
  if (cd "$RC_PLAYERS" && ./gradlew :third-party-rc-embedded-player:testDebugUnitTest \
        --no-daemon --rerun --quiet \
        --tests ee.schimke.composeai.rcembedded.RcAndroidxEmbeddedRenderHarness \
        -Prc.androidx.embedded.input="$RC_IN" -Prc.androidx.embedded.output="$RC_ANDROIDX_OUT" \
        -Pcomposeai.fonts.cacheDir="$FONTS_CACHE") >"$WORK/rc-androidx-embedded.log" 2>&1 \
     && has_png "$RC_ANDROIDX_OUT"; then
    EXTRA_ARGS+=(--androidx-embedded "$RC_ANDROIDX_OUT")
  else
    report_dropped_lane "upstream embedded-player" "$RC_ANDROIDX_OUT" \
      "$WORK/rc-androidx-embedded.log"
  fi
fi

# cmp-jvm: the CMP player on Compose Desktop / Skiko links libGL, so it renders under xvfb.
# The apt install sits in the `if` condition so a failure degrades the lane instead of `set -e`.
if [ "$RC_EMBEDDED_JVM_LANE" = "true" ]; then
  if ! command -v xvfb-run >/dev/null 2>&1 \
     && ! (sudo apt-get update -qq && sudo apt-get install -y -qq --no-install-recommends \
             xvfb libgl1 libglx-mesa0 libgl1-mesa-dri libfreetype6 libfontconfig1 fontconfig); then
    echo "::warning::could not install libGL/xvfb; the cmp-jvm lane will degrade to a missing column"
  fi
  RC_JVM_OUT="$WORK/rc-embedded-jvm-out"
  rm -rf "$RC_JVM_OUT"
  if (cd "$RC_PLAYERS" && xvfb-run -a -s "-screen 0 2000x1400x24" \
        ./gradlew :rc-player-compose:jvmTest --tests '*RcCmpRenderHarness*' \
          --no-daemon --rerun --quiet \
          -Prc.cmp.input="$RC_IN" -Prc.cmp.output="$RC_JVM_OUT") >"$WORK/rc-embedded-jvm.log" 2>&1 \
     && has_png "$RC_JVM_OUT"; then
    EXTRA_ARGS+=(--embedded-jvm "$RC_JVM_OUT")
  else
    report_dropped_lane "cmp-jvm player" "$RC_JVM_OUT" "$WORK/rc-embedded-jvm.log"
  fi
fi

# cmp-wasm, report-only. Never `--require-cmp-wasm`: that gate fails on any document the CMP
# player cannot draw, and about 15% of this corpus uses features it lacks.
WASM_ARGS=()
if [ "$RC_CMP_WASM_LANE" = "true" ]; then
  if (cd "$COMPOSE_AI_TOOLS" && ./gradlew stageVendoredRcPlayerWasm --no-daemon --quiet) \
       >"$WORK/rc-cmp-wasm-build.log" 2>&1; then
    WASM_ARGS+=(--cmp-wasm "$COMPOSE_AI_TOOLS/build/vendored-rc-player-wasm")
  else
    report_dropped_lane "cmp-wasm player" "$WORK/none" "$WORK/rc-cmp-wasm-build.log"
  fi
fi

compare_args=(--system "$SYSTEM" --title "Remote Compose Examples"
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"})
if [ ${#WASM_ARGS[@]} -gt 0 ]; then
  # The Wasm lane runs inside the same driver invocation, so a crash there would take the js
  # lane with it. Retry without it rather than lose the whole comparison.
  if ! run_compare "${compare_args[@]}" "${WASM_ARGS[@]}"; then
    echo "::warning::rc-compare failed with the cmp-wasm lane; retrying without it"
    rm -rf "$OUT/rc-cmp-wasm" "$OUT/rc-cmp-wasm-diff" "$OUT/rc-cmp-wasm-errors"
    run_compare "${compare_args[@]}"
  fi
else
  run_compare "${compare_args[@]}"
fi
