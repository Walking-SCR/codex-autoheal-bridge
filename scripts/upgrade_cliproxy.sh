#!/bin/bash
set -Eeuo pipefail

# Safe macOS CLIProxyAPI release updater.
# Defaults to a verified dry run; use --apply to install and restart the managed service.
VERSION="${CLIPROXYAPI_VERSION:-8.0.22}"
BINARY_PATH="${CLIPROXYAPI_BIN:-$HOME/.local/bin/cli-proxy-api}"
LAUNCHD_LABEL="${CLIPROXYAPI_LAUNCHD_LABEL:-com.zhijian.codex-cli-model-bridge-cliproxyapi}"
APPLY=0
ASSUME_YES=0

usage() {
  printf '%s\n' \
    "Usage: $0 [--version X.Y.Z] [--apply] [--yes]" \
    "  Default: download, verify checksum and inspect without changing the installed binary." \
    "  --apply: replace the binary, create a timestamped backup, and restart the managed LaunchAgent." \
    "  --yes: skip the interactive restart confirmation (use only after confirming no active third-party request)."
}

while (($#)); do
  case "$1" in
    --version)
      (($# >= 2)) || { usage >&2; exit 2; }
      VERSION="$2"
      shift 2
      ;;
    --apply) APPLY=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid release version: $VERSION" >&2; exit 2; }
[[ -x "$BINARY_PATH" ]] || { echo "CLIProxyAPI binary not found or not executable: $BINARY_PATH" >&2; exit 1; }

proxy_version() {
  local output
  output="$("$1" --version 2>&1 || true)"
  printf '%s\n' "$output" | sed -nE '1s/^CLIProxyAPI Version: ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p'
}

version_is_newer() {
  local -a left right
  local i
  IFS=. read -r -a left <<< "$1"
  IFS=. read -r -a right <<< "$2"
  for i in 0 1 2; do
    if ((10#${left[$i]} > 10#${right[$i]})); then return 0; fi
    if ((10#${left[$i]} < 10#${right[$i]})); then return 1; fi
  done
  return 1
}

CURRENT_VERSION="$(proxy_version "$BINARY_PATH")"
[[ -n "$CURRENT_VERSION" ]] || { echo "Could not read installed CLIProxyAPI version." >&2; exit 1; }
if [[ "$CURRENT_VERSION" == "$VERSION" ]]; then
  echo "CLIProxyAPI $VERSION is already installed."
  exit 0
fi
if version_is_newer "$CURRENT_VERSION" "$VERSION"; then
  echo "Refusing downgrade: installed $CURRENT_VERSION is newer than requested $VERSION." >&2
  exit 1
fi

ARM64="$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || echo 0)"
if [[ "$ARM64" == "1" ]]; then
  ASSET_ARCH="aarch64"
else
  case "$(uname -m)" in
    x86_64|amd64) ASSET_ARCH="amd64" ;;
    *) echo "Unsupported macOS architecture: $(uname -m)" >&2; exit 1 ;;
  esac
fi

ASSET="CLIProxyAPI_${VERSION}_darwin_${ASSET_ARCH}.tar.gz"
RELEASE_BASE="https://github.com/router-for-me/CLIProxyAPI/releases/download/v${VERSION}"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cliproxyapi-upgrade.XXXXXX")"
CANDIDATE_PATH=""
cleanup() {
  if [[ -n "$CANDIDATE_PATH" && -e "$CANDIDATE_PATH" ]]; then /bin/rm -f "$CANDIDATE_PATH"; fi
  /bin/rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

curl -fSL --retry 2 -o "$STAGE_DIR/checksums.txt" "$RELEASE_BASE/checksums.txt"
curl -fSL --retry 2 -o "$STAGE_DIR/$ASSET" "$RELEASE_BASE/$ASSET"
EXPECTED_SHA="$(awk -v asset="$ASSET" '$2 == asset {print $1}' "$STAGE_DIR/checksums.txt")"
[[ "$EXPECTED_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || { echo "Release checksum entry not found for $ASSET" >&2; exit 1; }
ACTUAL_SHA="$(shasum -a 256 "$STAGE_DIR/$ASSET" | awk '{print $1}')"
[[ "$ACTUAL_SHA" == "$EXPECTED_SHA" ]] || { echo "Checksum verification failed for $ASSET" >&2; exit 1; }

tar -xzf "$STAGE_DIR/$ASSET" -C "$STAGE_DIR"
STAGED_BINARY="$STAGE_DIR/cli-proxy-api"
[[ -f "$STAGED_BINARY" ]] || { echo "Release archive did not contain cli-proxy-api." >&2; exit 1; }
chmod 0755 "$STAGED_BINARY"
STAGED_VERSION="$(proxy_version "$STAGED_BINARY")"
[[ "$STAGED_VERSION" == "$VERSION" ]] || { echo "Archive version mismatch: expected $VERSION, got ${STAGED_VERSION:-unknown}." >&2; exit 1; }

printf 'Installed: %s\nTarget:    %s (%s)\nSHA-256:   %s\n' "$CURRENT_VERSION" "$VERSION" "$ASSET_ARCH" "$ACTUAL_SHA"
if (( ! APPLY )); then
  echo "Dry run only. No installed files or services were changed; use --apply to install."
  exit 0
fi

if (( ! ASSUME_YES )); then
  if [[ ! -t 0 ]]; then
    echo "Refusing non-interactive service restart without --yes." >&2
    exit 2
  fi
  printf '%s' "This restarts CLIProxyAPI and interrupts active third-party requests. Confirm none are running [y/N]? "
  read -r confirmation
  [[ "$confirmation" == "y" || "$confirmation" == "Y" || "$confirmation" == "yes" || "$confirmation" == "YES" ]] || { echo "Cancelled; installed service was not changed."; exit 0; }
fi

SERVICE_TARGET="gui/$(id -u)/$LAUNCHD_LABEL"
launchctl print "$SERVICE_TARGET" >/dev/null 2>&1 || { echo "Managed LaunchAgent is not loaded: $SERVICE_TARGET" >&2; exit 1; }
BACKUP_PATH="${BINARY_PATH}.backup-${CURRENT_VERSION}-$(date '+%Y%m%d-%H%M%S')"
[[ ! -e "$BACKUP_PATH" ]] || { echo "Backup path already exists; refusing to overwrite: $BACKUP_PATH" >&2; exit 1; }
CANDIDATE_PATH="${BINARY_PATH}.new.$$"
install -m 0755 "$STAGED_BINARY" "$CANDIDATE_PATH"
[[ "$(proxy_version "$CANDIDATE_PATH")" == "$VERSION" ]] || { echo "Installed candidate failed version verification." >&2; exit 1; }
cp -p "$BINARY_PATH" "$BACKUP_PATH"
mv -f "$CANDIDATE_PATH" "$BINARY_PATH"
CANDIDATE_PATH=""

rollback() {
  local restore_path="${BINARY_PATH}.rollback.$$"
  cp -p "$BACKUP_PATH" "$restore_path"
  mv -f "$restore_path" "$BINARY_PATH"
  launchctl kickstart -k "$SERVICE_TARGET" >/dev/null 2>&1 || true
}

if ! launchctl kickstart -k "$SERVICE_TARGET"; then
  echo "LaunchAgent restart failed; restoring $CURRENT_VERSION." >&2
  rollback
  exit 1
fi

READY=0
HTTP_STATUS="000"
for _ in {1..30}; do
  SERVICE_PID="$(launchctl list | awk -v label="$LAUNCHD_LABEL" '$3 == label {print $1; exit}')"
  if [[ "$SERVICE_PID" =~ ^[0-9]+$ ]] && kill -0 "$SERVICE_PID" 2>/dev/null; then
    SERVICE_COMMAND="$(ps -p "$SERVICE_PID" -o command= 2>/dev/null || true)"
    if [[ "$SERVICE_COMMAND" == *"$BINARY_PATH"* ]]; then
      HTTP_STATUS="$(curl --silent --output /dev/null --write-out '%{http_code}' --connect-timeout 1 --max-time 2 http://127.0.0.1:8317/v1/models || true)"
      if [[ "$HTTP_STATUS" == "200" || "$HTTP_STATUS" == "401" ]]; then READY=1; break; fi
    fi
  fi
  sleep 1
done

if (( ! READY )); then
  echo "v$VERSION did not pass the local process/API readiness check (HTTP $HTTP_STATUS); restoring $CURRENT_VERSION." >&2
  rollback
  exit 1
fi

echo "Upgrade complete: $CURRENT_VERSION -> $VERSION"
echo "Verified local /v1/models response: HTTP $HTTP_STATUS"
echo "Rollback binary: $BACKUP_PATH"
echo "Config was not edited. After any v8 Management API configuration write, re-run the Bridge audit."
