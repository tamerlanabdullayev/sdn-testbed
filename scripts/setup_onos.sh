#!/usr/bin/env bash
# One-time ONOS setup for the testbed (no sudo; needs openjdk-11-jdk-headless
# and maven from apt). Rerun after changing controller/onos-instrumented-l2.
#   1. download + verify ONOS 2.7.0 into third_party/ (skipped if present)
#   2. unpack into ONOS_HOME (default ~/.local/opt/onos-2.7.0; ONOS's
#      scripts break on paths with spaces, so not inside this repository)
#   3. build the instrumented app (target/instrumented-l2-1.0.0.oar)
#   4. start ONOS once, install + activate the app via REST, stop ONOS
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION=2.7.0
ONOS_HOME="${ONOS_HOME:-$HOME/.local/opt/onos-$VERSION}"
JAVA_HOME="${ONOS_JAVA_HOME:-/usr/lib/jvm/java-11-openjdk-amd64}"
URL="https://repo1.maven.org/maven2/org/onosproject/onos-releases/$VERSION/onos-$VERSION.tar.gz"
TARBALL="$ROOT/third_party/onos-$VERSION.tar.gz"
APP=org.sdntestbed.instrumentedl2
OAR="$ROOT/controller/onos-instrumented-l2/target/instrumented-l2-1.0.0.oar"
REST=http://127.0.0.1:8181/onos/v1

case "$ONOS_HOME" in *" "*) echo "[ERROR] ONOS_HOME must not contain spaces" >&2; exit 1 ;; esac
[ -x "$JAVA_HOME/bin/java" ] || { echo "[ERROR] no Java 11 at $JAVA_HOME (apt install openjdk-11-jdk-headless)" >&2; exit 1; }
if (exec 3<>/dev/tcp/127.0.0.1/8181) 2>/dev/null; then
  echo "[ERROR] something already listens on 8181 (ONOS running?); stop it first" >&2; exit 1
fi

mkdir -p "$ROOT/third_party"
if [ ! -f "$TARBALL" ]; then
  echo "[SETUP] downloading $URL"
  curl -fSL -o "$TARBALL" "$URL"
  curl -fsSL -o "$TARBALL.sha1" "$URL.sha1"
fi
echo "$(cat "$TARBALL.sha1")  $TARBALL" | sha1sum -c -

if [ ! -d "$ONOS_HOME" ]; then
  echo "[SETUP] unpacking into $ONOS_HOME"
  mkdir -p "$(dirname "$ONOS_HOME")"
  tar -xzf "$TARBALL" -C "$(dirname "$ONOS_HOME")"
  [ "$(dirname "$ONOS_HOME")/onos-$VERSION" = "$ONOS_HOME" ] || mv "$(dirname "$ONOS_HOME")/onos-$VERSION" "$ONOS_HOME"
fi

echo "[SETUP] building the app"
(cd "$ROOT/controller/onos-instrumented-l2" && JAVA_HOME="$JAVA_HOME" mvn -q -B package -DskipTests)

echo "[SETUP] starting ONOS to install the app"
rm -rf "$ONOS_HOME"/apache-karaf-*/data
LOG="$(mktemp)"
(cd "$ONOS_HOME" && JAVA_HOME="$JAVA_HOME" ONOS_APPS=openflow-base \
  JAVA_OPTS="-Xms1g -Xmx1g -Djdk.util.zip.disableZip64ExtraFieldValidation=true" \
  OUTDIR="$(mktemp -d)" bin/onos-service server >"$LOG" 2>&1) &
LAUNCHER=$!
stop_onos() {
  local java; java="$(ps -o pid=,comm= --ppid "$(pgrep -P "$LAUNCHER" | head -1)" 2>/dev/null | awk '$2=="java"{print $1}')"
  [ -n "$java" ] && kill -TERM "$java" 2>/dev/null || true
  wait "$LAUNCHER" 2>/dev/null || true
}
trap stop_onos EXIT

ready=0
for _ in $(seq 1 240); do
  if curl -sf -u onos:rocks "$REST/applications/org.onosproject.openflow-base" 2>/dev/null \
      | grep -q '"state":"ACTIVE"'; then ready=1; break; fi
  sleep 0.5
done
[ "$ready" = 1 ] || { echo "[ERROR] ONOS did not come up; see $LOG" >&2; exit 1; }
curl -s -u onos:rocks -X DELETE "$REST/applications/$APP" >/dev/null 2>&1 || true
installed=0
for _ in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -u onos:rocks -X POST \
    -H "Content-Type: application/octet-stream" --data-binary @"$OAR" \
    "$REST/applications?activate=true")"
  [ "$code" = 200 ] && { installed=1; break; }
  echo "[SETUP] install returned HTTP $code, retrying"
  sleep 2
done
[ "$installed" = 1 ] || { echo "[ERROR] could not install $APP; see $LOG" >&2; exit 1; }
for _ in $(seq 1 40); do
  curl -s -u onos:rocks "$REST/applications/$APP" | grep -q '"state":"ACTIVE"' && break
  sleep 0.5
done
curl -s -u onos:rocks "$REST/applications/$APP" | grep -q '"state":"ACTIVE"' \
  && echo "[SETUP] $APP installed and active" \
  || { echo "[ERROR] $APP did not activate; see $LOG" >&2; exit 1; }
cmp -s "$OAR" "$ONOS_HOME/apps/$APP/$APP.oar" \
  && echo "[SETUP] done: ONOS_HOME=$ONOS_HOME" \
  || { echo "[ERROR] installed .oar differs from $OAR" >&2; exit 1; }
