#!/usr/bin/env bash
# RTO Tracker — setup script
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "── RTO Tracker Setup ───────────────────────────────────"
echo ""

# Check Python 3.11+
python3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'" \
  || { echo "ERROR: Python 3.11+ is required."; exit 1; }

echo "✓ Python $(python3 --version)"

# Create virtualenv if not present
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies…"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements.txt

echo "✓ Dependencies installed"
echo ""

# Create config dir
mkdir -p ~/.rto_tracker

echo "Running interactive setup…"
python3 main.py setup

echo ""
echo "── Next steps ──────────────────────────────────────────"
echo ""
echo "  1. Place your Google Calendar OAuth credentials at:"
echo "       ~/.rto_tracker/gcal_credentials.json"
echo ""
echo "  2. Test calendar sync (this will open a browser for OAuth):"
echo "       source .venv/bin/activate && python3 main.py sync"
echo ""
echo "  3. Install as a background service (starts at login):"
echo "       python3 main.py install"
echo ""
echo "  4. Check your status any time:"
echo "       python3 main.py status"
echo ""
