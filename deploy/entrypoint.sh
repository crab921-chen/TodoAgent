#!/bin/sh
set -eu

python -m ops.startup_check
exec "$@"
