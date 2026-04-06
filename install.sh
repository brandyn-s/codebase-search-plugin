#!/usr/bin/env bash
# Install script for codebase-search-plugin
# Downloads and configures both MCP servers (code-search + code-graph)

set -e

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$PLUGIN_DIR/bin"
VENV_DIR="$PLUGIN_DIR/.venv"

echo "=== Codebase Search Plugin Installer ==="
echo ""

# Detect platform
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

case "$OS" in
    linux)  PLATFORM="linux" ; EXT="tar.gz" ; BINARY="codebase-memory-mcp" ;;
    darwin) PLATFORM="darwin" ; EXT="tar.gz" ; BINARY="codebase-memory-mcp" ;;
    mingw*|msys*|cygwin*) PLATFORM="windows" ; EXT="zip" ; BINARY="codebase-memory-mcp.exe" ;;
    *) echo "Unsupported OS: $OS"; exit 1 ;;
esac

echo "Platform: $PLATFORM-$ARCH"
echo ""

# --- code-search (Python) ---
echo "--- Installing code-search (semantic search) ---"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR" 2>/dev/null || python -m venv "$VENV_DIR"
fi

echo "Installing code-search from GitHub..."
"$VENV_DIR/bin/pip" install --quiet \
    "redacted-code-search @ git+https://github.com/redacted-org/code-search.git" \
    2>/dev/null || \
"$VENV_DIR/Scripts/pip" install --quiet \
    "redacted-code-search @ git+https://github.com/redacted-org/code-search.git"

echo "code-search installed."
echo ""

# --- code-graph (Go binary) ---
echo "--- Installing code-graph (structural analysis) ---"
mkdir -p "$BIN_DIR"

# Get latest release URL
RELEASE_TAG=$(gh release list --repo redacted-org/code-graph --limit 1 --json tagName --jq '.[0].tagName' 2>/dev/null || echo "v0.5.0-redacted.4")
ASSET_NAME="codebase-memory-mcp-${PLATFORM}-${ARCH}.${EXT}"
DOWNLOAD_URL="https://github.com/redacted-org/code-graph/releases/download/${RELEASE_TAG}/${ASSET_NAME}"

echo "Downloading code-graph ${RELEASE_TAG} for ${PLATFORM}-${ARCH}..."
curl -sL "$DOWNLOAD_URL" -o "$BIN_DIR/$ASSET_NAME"

if [ "$EXT" = "tar.gz" ]; then
    tar xzf "$BIN_DIR/$ASSET_NAME" -C "$BIN_DIR"
    rm "$BIN_DIR/$ASSET_NAME"
else
    unzip -qo "$BIN_DIR/$ASSET_NAME" -d "$BIN_DIR"
    rm "$BIN_DIR/$ASSET_NAME"
fi

chmod +x "$BIN_DIR/$BINARY" 2>/dev/null || true
echo "code-graph installed at $BIN_DIR/$BINARY"
echo ""

# --- Summary ---
echo "=== Installation Complete ==="
echo ""
echo "code-search: $VENV_DIR/bin/code-search-mcp (or Scripts/code-search-mcp on Windows)"
echo "code-graph:  $BIN_DIR/$BINARY"
echo ""
echo "Set your embedding provider:"
echo "  export EMBEDDING_PROVIDER=voyage-context  # best quality (needs VOYAGE_API_KEY)"
echo "  export EMBEDDING_PROVIDER=jina            # local, free, no data leaves machine"
echo ""
echo "If using Voyage: export VOYAGE_API_KEY=pa-..."
echo ""
echo "Install the plugin in Claude Code:"
echo "  /install-plugin $PLUGIN_DIR"
