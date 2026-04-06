#!/bin/bash
# Setup script for Carpenter dev/server split.
#
# Creates a server clone from the dev repo so the running server process
# imports from a separate directory. Changes are committed to the dev repo
# and pulled into the server clone via git pull.
#
# Idempotent — safe to re-run.
#
# Usage: ./tools/setup-dev-server-split.sh [--dev-dir DIR] [--server-dir DIR]

set -euo pipefail

DEV_DIR="${1:-$HOME/repos/carpenter-core}"
SERVER_DIR="${2:-$HOME/repos/carpenter-core-server}"
SERVER_SYMLINK="$HOME/repos/carpenter-core-server"

echo "=== Carpenter Dev/Server Split Setup ==="
echo "Dev dir:    $DEV_DIR"
echo "Server dir: $SERVER_DIR"
echo ""

# Verify dev dir exists and is a git repo
if [ ! -d "$DEV_DIR/.git" ]; then
    echo "ERROR: Dev dir is not a git repo: $DEV_DIR"
    exit 1
fi

# Create server clone if it doesn't exist
if [ -d "$SERVER_DIR/.git" ]; then
    echo "Server dir already exists, updating..."
    cd "$SERVER_DIR"
    git pull --ff-only origin main || echo "Warning: pull failed (may need manual resolution)"
    git submodule update --init
else
    echo "Cloning dev repo into server dir..."
    git clone "$DEV_DIR" "$SERVER_DIR"
    cd "$SERVER_DIR"
    git submodule update --init
fi

# Create symlink
if [ -L "$SERVER_SYMLINK" ]; then
    echo "Symlink already exists: $SERVER_SYMLINK"
elif [ -e "$SERVER_SYMLINK" ]; then
    echo "Warning: $SERVER_SYMLINK exists but is not a symlink — skipping"
else
    ln -s "$SERVER_DIR" "$SERVER_SYMLINK"
    echo "Created symlink: $SERVER_SYMLINK -> $SERVER_DIR"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "1. Start the server from the server dir:"
echo "   cd $SERVER_SYMLINK && python3 -m carpenter_linux --host 0.0.0.0"
echo ""
echo "2. Deploy changes by pulling into server dir:"
echo "   cd $SERVER_SYMLINK && git pull"
