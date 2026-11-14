#!/usr/bin/env bash
# Cut a release with scripts/release.py.
set -euo pipefail

exec python3 "$(dirname "$0")/release.py" "$@"
