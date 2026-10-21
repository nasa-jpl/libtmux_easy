#!/usr/bin/env bash
# ./run.sh [pytest args]      e.g. ./run.sh --tmux, ./run.sh failing, ./run.sh --keep-open
#   lte-session ./run.sh -s   with the dashboard
set -e
here="$(cd "$(dirname "$0")" && pwd)"
[ -n "$ROS_DISTRO" ] || source /opt/ros/jazzy/setup.bash
# The library from this checkout, unless it's installed (e.g. the .deb).
python3 -c 'import libtmux_easy' 2>/dev/null ||
    export PYTHONPATH="$here/../../src${PYTHONPATH:+:$PYTHONPATH}"
cd "$here"
exec python3 -m pytest -p no:cacheprovider "$@"
