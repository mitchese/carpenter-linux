#!/usr/bin/env bash
# Carpenter Plugin Watcher — Setup Script
#
# Sets up a watcher instance for a plugin. Creates the shared folder,
# copies the watcher script, generates config, and installs the systemd
# service.
#
# Usage:
#   ./setup-watcher.sh [plugin-name] [shared-folder]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Helpers ---

prompt_value() {
    local prompt="$1"
    local default="$2"
    local result

    if [ -n "$default" ]; then
        printf "%s [%s]: " "$prompt" "$default" >&2
    else
        printf "%s: " "$prompt" >&2
    fi

    read -r result
    echo "${result:-$default}"
}

info() {
    printf "\033[1;34m==> %s\033[0m\n" "$1" >&2
}

success() {
    printf "\033[1;32m==> %s\033[0m\n" "$1" >&2
}

error() {
    printf "\033[1;31mError: %s\033[0m\n" "$1" >&2
    exit 1
}

# --- Gather inputs ---

PLUGIN_NAME="${1:-}"
if [ -z "$PLUGIN_NAME" ]; then
    PLUGIN_NAME="$(prompt_value "Plugin name" "")"
fi
[ -z "$PLUGIN_NAME" ] && error "Plugin name is required"

# Validate plugin name (alphanumeric, hyphens, underscores)
if ! echo "$PLUGIN_NAME" | grep -qE '^[a-zA-Z0-9_-]+$'; then
    error "Plugin name must contain only letters, numbers, hyphens, and underscores"
fi

DEFAULT_SHARED="$HOME/carpenter-shared/$PLUGIN_NAME"
SHARED_FOLDER="${2:-}"
if [ -z "$SHARED_FOLDER" ]; then
    SHARED_FOLDER="$(prompt_value "Shared folder path" "$DEFAULT_SHARED")"
fi

# Command (ask interactively)
DEFAULT_CMD='["echo", "hello from watcher"]'
if [ -t 0 ]; then
    printf "Command (JSON array, e.g. [\"claude\", \"code\", \"--print\"])\n" >&2
    COMMAND_JSON="$(prompt_value "Command" "$DEFAULT_CMD")"
else
    COMMAND_JSON="$DEFAULT_CMD"
fi

# Validate command JSON
if ! python3 -c "import json, sys; c=json.loads(sys.argv[1]); assert isinstance(c, list) and len(c) > 0" "$COMMAND_JSON" 2>/dev/null; then
    error "Command must be a valid non-empty JSON array"
fi

# Prompt mode
if [ -t 0 ]; then
    PROMPT_MODE="$(prompt_value "Prompt mode (stdin/file/arg)" "stdin")"
else
    PROMPT_MODE="stdin"
fi

case "$PROMPT_MODE" in
    stdin|file|arg) ;;
    *) error "Prompt mode must be stdin, file, or arg" ;;
esac

# --- Create directories ---

INSTALL_DIR="$HOME/.config/carpenter/watchers/$PLUGIN_NAME"

info "Creating shared folder structure: $SHARED_FOLDER"
mkdir -p "$SHARED_FOLDER/triggered"
mkdir -p "$SHARED_FOLDER/completed"

info "Creating watcher install directory: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# --- Copy and generate files ---

info "Copying watcher.py"
cp "$SCRIPT_DIR/watcher.py" "$INSTALL_DIR/watcher.py"
chmod +x "$INSTALL_DIR/watcher.py"

info "Generating watcher_config.json"
python3 -c "
import json, sys
config = {
    'shared_folder': sys.argv[1],
    'command': json.loads(sys.argv[2]),
    'prompt_mode': sys.argv[3],
    'heartbeat_interval': 10,
    'poll_interval': 1,
    'timeout_seconds': 600,
    'log_level': 'INFO',
}
with open(sys.argv[4], 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
" "$SHARED_FOLDER" "$COMMAND_JSON" "$PROMPT_MODE" "$INSTALL_DIR/watcher_config.json"

# --- Install systemd service ---

SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="carpenter-plugin-watcher@.service"

info "Installing systemd service to $SYSTEMD_DIR"
mkdir -p "$SYSTEMD_DIR"
cp "$SCRIPT_DIR/$SERVICE_FILE" "$SYSTEMD_DIR/$SERVICE_FILE"

# Reload systemd
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
fi

# --- Optionally enable and start ---

if [ -t 0 ]; then
    ENABLE="$(prompt_value "Enable and start the service now? (y/n)" "y")"
    if [ "$ENABLE" = "y" ] || [ "$ENABLE" = "Y" ]; then
        info "Enabling and starting carpenter-plugin-watcher@$PLUGIN_NAME"
        systemctl --user enable --now "carpenter-plugin-watcher@$PLUGIN_NAME"
        sleep 1
        systemctl --user status "carpenter-plugin-watcher@$PLUGIN_NAME" --no-pager || true
    fi
fi

# --- Print summary ---

echo ""
success "Watcher setup complete!"
echo ""
echo "  Plugin name:   $PLUGIN_NAME"
echo "  Shared folder: $SHARED_FOLDER"
echo "  Install dir:   $INSTALL_DIR"
echo "  Config file:   $INSTALL_DIR/watcher_config.json"
echo ""
echo "  To manage the service:"
echo "    systemctl --user enable --now carpenter-plugin-watcher@$PLUGIN_NAME"
echo "    systemctl --user status carpenter-plugin-watcher@$PLUGIN_NAME"
echo "    systemctl --user stop carpenter-plugin-watcher@$PLUGIN_NAME"
echo "    journalctl --user -u carpenter-plugin-watcher@$PLUGIN_NAME -f"
echo ""
echo "  To register the plugin with Carpenter, add to plugins.json:"
echo "    \"$PLUGIN_NAME\": {"
echo "      \"enabled\": true,"
echo "      \"description\": \"Your plugin description\","
echo "      \"transport\": \"file-watch\","
echo "      \"transport_config\": {"
echo "        \"shared_folder\": \"$SHARED_FOLDER\""
echo "      }"
echo "    }"
echo ""
