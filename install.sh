#!/usr/bin/env bash
# Install script for codebase-search-plugin
# Downloads and configures both MCP servers (code-search + code-graph)
#
# This installs the exact BOM components and reports the BOM's integrated
# readiness. A blocked BOM must not be presented as dual-index ready.

set -e

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
# Layout: bin/ holds the committed MCP launchers that .mcp.json points at.
# Installed components are runtime state under .runtime/bin (native binaries,
# optional SCIP generators) and .venv (code-search). Both are gitignored.
RUNTIME_DIR="$PLUGIN_DIR/.runtime"
LAUNCHER_DIR="$PLUGIN_DIR/bin"
TARGET_BIN_DIR="$RUNTIME_DIR/bin"
TARGET_VENV_DIR="$PLUGIN_DIR/.venv"
BIN_DIR="$TARGET_BIN_DIR"
VENV_DIR="$TARGET_VENV_DIR"
BOM_FILE="$PLUGIN_DIR/component-bom.json"
# The installed code-graph binary is always named code-graph regardless of the
# name inside the release archive (older releases shipped codebase-memory-mcp).
GRAPH_INSTALLED_NAME="code-graph"

echo "=== Codebase Search Plugin Installer ==="
echo ""

# Detect platform
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "Error: Unsupported architecture: $ARCH"; exit 1 ;;
esac

case "$OS" in
    linux)  PLATFORM="linux" ; EXT="tar.gz" ; GRAPH_BINARY="$GRAPH_INSTALLED_NAME" ;;
    darwin) PLATFORM="darwin" ; EXT="tar.gz" ; GRAPH_BINARY="$GRAPH_INSTALLED_NAME" ;;
    mingw*|msys*|cygwin*) PLATFORM="windows" ; EXT="zip" ; GRAPH_BINARY="$GRAPH_INSTALLED_NAME.exe" ;;
    *) echo "Error: Unsupported OS: $OS"; exit 1 ;;
esac

echo "Platform: $PLATFORM-$ARCH"
echo ""

# Find Python 3.12+
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 12 ]; }; then
            PYTHON="$cmd"
            echo "  Using $cmd ($version)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python 3.12+ is required but not found."
    echo "Install Python from https://www.python.org/downloads/"
    exit 1
fi

if [ ! -f "$BOM_FILE" ]; then
    echo "Error: tested component BOM not found: $BOM_FILE" >&2
    exit 1
fi

# ------------------------------------------------------------------
# 1. Validate the plugin contract and committed readiness evidence
# ------------------------------------------------------------------
echo "[1/5] Validating plugin contract and committed readiness evidence..."
env -u CODE_INTEL_READINESS_EVIDENCE_OVERRIDE \
    "$PYTHON" "$PLUGIN_DIR/scripts/validate_plugin.py"
echo ""

# component-bom.json is the single source of truth for both installers.
CODE_SEARCH_KIND=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["kind"])' \
    "$BOM_FILE")
CODE_SEARCH_REPOSITORY=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["repository"])' \
    "$BOM_FILE")
GRAPH_REPOSITORY=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["repository"])' \
    "$BOM_FILE")
RELEASE_TAG=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["tag"])' \
    "$BOM_FILE")
GRAPH_SOURCE_REVISION=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["source_revision"])' \
    "$BOM_FILE")
GRAPH_CHECKSUMS=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["checksums"]["name"])' \
    "$BOM_FILE")
GRAPH_CHECKSUMS_SHA256=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["checksums"]["sha256"])' \
    "$BOM_FILE")
GRAPH_ATTESTATION_BUNDLE_PATH=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["attestation"]["bundle"]["path"])' \
    "$BOM_FILE")
GRAPH_ATTESTATION_BUNDLE_SHA256=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["attestation"]["bundle"]["sha256"])' \
    "$BOM_FILE")
GRAPH_SIGNER_WORKFLOW=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["attestation"]["signer_workflow"])' \
    "$BOM_FILE")
GRAPH_SOURCE_REF=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["attestation"]["source_ref"])' \
    "$BOM_FILE")
if [ "$GRAPH_CHECKSUMS" != "checksums.txt" ]; then
    echo "Error: code-graph checksum manifest must be checksums.txt." >&2
    exit 1
fi
EXPECTED_GRAPH_ATTESTATION_BUNDLE_PATH="compatibility/attestations/code-graph-${RELEASE_TAG}-provenance.jsonl"
if [ "$GRAPH_ATTESTATION_BUNDLE_PATH" != "$EXPECTED_GRAPH_ATTESTATION_BUNDLE_PATH" ]; then
    echo "Error: code-graph attestation bundle path does not match the tested release." >&2
    exit 1
fi
GRAPH_ATTESTATION_BUNDLE="$PLUGIN_DIR/$GRAPH_ATTESTATION_BUNDLE_PATH"
ASSET_KEY="${PLATFORM}-${ARCH}"
ASSET_NAME=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["assets"][sys.argv[2]]["name"])' \
    "$BOM_FILE" "$ASSET_KEY")
EXPECTED_SHA256=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["assets"][sys.argv[2]]["sha256"])' \
    "$BOM_FILE" "$ASSET_KEY")
READINESS_STATUS=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["integrated_readiness"]["status"])' \
    "$BOM_FILE")
READINESS_REASON=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["integrated_readiness"]["reason"])' \
    "$BOM_FILE")
GO_SCIP_REPOSITORY=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["go-scip"]["repository"])' \
    "$BOM_FILE")
GO_SCIP_TAG=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["go-scip"]["tag"])' \
    "$BOM_FILE")
GO_SCIP_SOURCE_REVISION=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["go-scip"]["source_revision"])' \
    "$BOM_FILE")
GO_SCIP_VERSION=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["go-scip"]["version_output"])' \
    "$BOM_FILE")
GO_SCIP_SUPPORTED=$("$PYTHON" -c \
    'import json,sys; print("yes" if sys.argv[2] in json.load(open(sys.argv[1]))["precision_generators"]["go-scip"]["assets"] else "no")' \
    "$BOM_FILE" "$ASSET_KEY")
if [ "$GO_SCIP_SUPPORTED" = "yes" ]; then
    GO_SCIP_ASSET=$("$PYTHON" -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["go-scip"]["assets"][sys.argv[2]]["name"])' \
        "$BOM_FILE" "$ASSET_KEY")
    GO_SCIP_ARCHIVE_SHA256=$("$PYTHON" -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["go-scip"]["assets"][sys.argv[2]]["archive_sha256"])' \
        "$BOM_FILE" "$ASSET_KEY")
    GO_SCIP_BINARY_SHA256=$("$PYTHON" -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["go-scip"]["assets"][sys.argv[2]]["binary_sha256"])' \
        "$BOM_FILE" "$ASSET_KEY")
fi
TYPESCRIPT_SCIP_PACKAGE=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["package"])' \
    "$BOM_FILE")
TYPESCRIPT_SCIP_VERSION=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["version_output"])' \
    "$BOM_FILE")
TYPESCRIPT_SCIP_LOCKFILE=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["lockfile"])' \
    "$BOM_FILE")
TYPESCRIPT_SCIP_PACKAGE_MANIFEST=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["package_manifest"])' \
    "$BOM_FILE")
TYPESCRIPT_SCIP_LOCKFILE_SHA256=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["lockfile_sha256"])' \
    "$BOM_FILE")
TYPESCRIPT_SCIP_ENTRYPOINT=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["entrypoint"])' \
    "$BOM_FILE")
TYPESCRIPT_SCIP_ENTRYPOINT_SHA256=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["entrypoint_sha256"])' \
    "$BOM_FILE")
TYPESCRIPT_NODE_BASE_URL=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["node_runtime"]["base_url"])' \
    "$BOM_FILE")
TYPESCRIPT_NODE_VERSION=$($PYTHON -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["node_runtime"]["version"])' \
    "$BOM_FILE")
TYPESCRIPT_SCIP_SUPPORTED=$($PYTHON -c \
    'import json,sys; print("yes" if sys.argv[2] in json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["node_runtime"]["assets"] else "no")' \
    "$BOM_FILE" "$ASSET_KEY")
if [ "$TYPESCRIPT_SCIP_SUPPORTED" = "yes" ]; then
    TYPESCRIPT_NODE_ASSET=$($PYTHON -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["node_runtime"]["assets"][sys.argv[2]]["name"])' \
        "$BOM_FILE" "$ASSET_KEY")
    TYPESCRIPT_NODE_ARCHIVE_SHA256=$($PYTHON -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["node_runtime"]["assets"][sys.argv[2]]["archive_sha256"])' \
        "$BOM_FILE" "$ASSET_KEY")
    TYPESCRIPT_NODE_BINARY_SHA256=$($PYTHON -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["precision_generators"]["typescript-scip"]["node_runtime"]["assets"][sys.argv[2]]["binary_sha256"])' \
        "$BOM_FILE" "$ASSET_KEY")
fi

if [[ ! "$EXPECTED_SHA256" =~ ^[[:xdigit:]]{64}$ ]]; then
    echo "Error: BOM SHA-256 is missing or invalid for $ASSET_KEY." >&2
    exit 1
fi

verify_sha256() {
    local file="$1"
    local expected="$2"
    local label="$3"
    local actual

    if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
        echo "Error: BOM SHA-256 is missing or invalid for $label." >&2
        return 1
    fi
    if [ ! -f "$file" ]; then
        echo "Error: downloaded release asset is missing: $file" >&2
        return 1
    fi
    if command -v sha256sum &>/dev/null; then
        actual=$(sha256sum "$file" | awk '{print $1}')
    elif command -v shasum &>/dev/null; then
        actual=$(shasum -a 256 "$file" | awk '{print $1}')
    else
        echo "Error: no SHA-256 verification tool is available." >&2
        return 1
    fi
    actual=$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')
    if [ "$actual" != "$expected" ]; then
        echo "Error: checksum mismatch for $label" >&2
        echo "  expected: $expected" >&2
        echo "  actual:   $actual" >&2
        return 1
    fi
}

verify_checksum_manifest() {
    local manifest="$1"
    local artifact_name="$2"
    local expected="$3"

    "$PYTHON" - "$manifest" "$artifact_name" "$expected" <<'PY'
import pathlib
import re
import sys

manifest = pathlib.Path(sys.argv[1])
artifact_name = sys.argv[2]
expected = sys.argv[3]
if (
    not manifest.is_file()
    or pathlib.Path(artifact_name).name != artifact_name
    or re.fullmatch(r"[0-9a-f]{64}", expected) is None
):
    raise SystemExit("Error: checksum manifest inputs are invalid")
matches = []
try:
    lines = manifest.read_text(encoding="utf-8").splitlines()
except (OSError, UnicodeDecodeError) as exc:
    raise SystemExit(f"Error: checksum manifest is unreadable: {exc}")
for line in lines:
    parsed = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?(.+)", line)
    if parsed is None:
        if line.strip():
            raise SystemExit("Error: checksum manifest contains a malformed entry")
        continue
    digest, name = parsed.groups()
    if name == artifact_name:
        matches.append(digest.lower())
if matches != [expected]:
    raise SystemExit(
        "Error: checksum manifest does not contain exactly one matching artifact entry"
    )
PY
}

run_with_allowed_environment() {
    local -a allowed_environment
    local name

    allowed_environment=("PATH=$PATH")
    allowed_environment+=("HOME=$INSTALL_RUNTIME_HOME")
    for name in \
        LANG LC_ALL LC_CTYPE TZ \
        SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE; do
        if [ -n "${!name:-}" ]; then
            allowed_environment+=("$name=${!name}")
        fi
    done
    env -i "${allowed_environment[@]}" "$@"
}

# GitHub CLI is optional. Public releases are fetched with curl; gh is used
# when present and authenticated (private releases, build provenance).
GH_AUTHENTICATED=""
gh_authenticated() {
    if [ -z "$GH_AUTHENTICATED" ]; then
        if command -v gh &>/dev/null && gh auth status --hostname github.com >/dev/null 2>&1; then
            GH_AUTHENTICATED=yes
        else
            GH_AUTHENTICATED=no
        fi
    fi
    [ "$GH_AUTHENTICATED" = "yes" ]
}

# github_api_get <repos/...> : GET a GitHub REST endpoint and print the body.
github_api_get() {
    local endpoint="$1"
    if gh_authenticated; then
        gh api --method GET "$endpoint"
        return
    fi
    if ! command -v curl &>/dev/null; then
        echo "Error: curl or an authenticated GitHub CLI is required to resolve release tags." >&2
        return 1
    fi
    local -a headers
    headers=(-H "Accept: application/vnd.github+json")
    if [ -n "${GH_TOKEN:-}" ]; then
        headers+=(-H "Authorization: Bearer $GH_TOKEN")
    fi
    curl --fail --location --silent --show-error "${headers[@]}" \
        "https://api.github.com/$endpoint"
}

# download_release_asset <repository> <tag> <asset> <dest-dir>
# Public releases download directly; gh is the fallback for private ones.
download_release_asset() {
    local repository="$1"
    local tag="$2"
    local asset="$3"
    local dest_dir="$4"
    local url="https://github.com/${repository}/releases/download/${tag}/${asset}"
    mkdir -p "$dest_dir"
    if command -v curl &>/dev/null; then
        if curl --fail --location --silent --show-error --retry 2 \
            "$url" --output "$dest_dir/$asset" 2>/dev/null; then
            return 0
        fi
        rm -f "$dest_dir/$asset"
    fi
    if gh_authenticated; then
        gh release download "$tag" \
            --repo "$repository" \
            --pattern "$asset" \
            --dir "$dest_dir" \
            --clobber
        return
    fi
    echo "Error: could not download $asset from $repository ($tag)." >&2
    echo "  The public release URL was unavailable and the GitHub CLI is not authenticated." >&2
    echo "  For private releases: install gh, run 'gh auth login' (or set GH_TOKEN), and re-run." >&2
    return 1
}

# verify_release_attestation <artifact> <bundle> <repository> <signer-workflow> <source-digest> <source-ref>
# Checksums against the BOM are always mandatory (see verify_sha256). Build
# provenance additionally needs the GitHub CLI; without it we say so once.
verify_release_attestation() {
    local artifact="$1"
    local bundle="$2"
    local repository="$3"
    local signer_workflow="$4"
    local source_digest="$5"
    local source_ref="$6"
    if gh_authenticated; then
        run_with_allowed_environment \
            gh attestation verify "$artifact" \
            --bundle "$bundle" \
            --repo "$repository" \
            --signer-workflow "$signer_workflow" \
            --source-digest "$source_digest" \
            --source-ref "$source_ref" \
            --deny-self-hosted-runners
        return
    fi
    echo "  Provenance attestation not verified for $artifact: GitHub CLI is not available or not authenticated." >&2
    echo "  Checksums matched the tested BOM. To also verify build provenance, install gh, run 'gh auth login', and re-run install.sh." >&2
}

resolve_release_tag_commit() {
    local repository="$1"
    local tag="$2"
    local response
    local target_type
    local target_sha
    local extra
    local depth

    response=$(github_api_get "repos/${repository}/git/ref/tags/${tag}" \
        | "$PYTHON" -c 'import json,sys; o=json.load(sys.stdin)["object"]; print(f"{o[\"type\"]}\t{o[\"sha\"]}")')
    depth=0
    while [ "$depth" -lt 16 ]; do
        depth=$((depth + 1))
        target_type=""
        target_sha=""
        extra=""
        IFS=$'\t' read -r target_type target_sha extra <<< "$response"
        if [ -n "$extra" ] || \
            [[ ! "$target_sha" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
            echo "Error: GitHub tag response is malformed." >&2
            return 1
        fi
        case "$target_type" in
            commit)
                printf '%s\n' "$target_sha"
                return 0
                ;;
            tag)
                response=$(github_api_get "repos/${repository}/git/tags/${target_sha}" \
                    | "$PYTHON" -c 'import json,sys; o=json.load(sys.stdin)["object"]; print(f"{o[\"type\"]}\t{o[\"sha\"]}")')
                ;;
            *)
                echo "Error: GitHub tag resolves to unsupported object type: $target_type" >&2
                return 1
                ;;
        esac
    done
    echo "Error: GitHub annotated tag chain exceeds 16 objects." >&2
    return 1
}

INSTALL_COMMITTED=0
INSTALL_PROMOTING=0
HAD_TARGET_BIN=0
HAD_TARGET_VENV=0
NEW_BIN_PROMOTED=0
NEW_VENV_PROMOTED=0
ROLLBACK_BIN_DIR="$RUNTIME_DIR/bin.rollback.$$"
ROLLBACK_VENV_DIR="$PLUGIN_DIR/.venv.rollback.$$"
INSTALL_STAGE=""

rollback_install() {
    local exit_code=$?

    set +e
    if [ "$INSTALL_COMMITTED" -ne 1 ]; then
        if [ "$INSTALL_PROMOTING" -eq 1 ]; then
            echo "Restoring previous installation..." >&2
            if [ "$NEW_BIN_PROMOTED" -eq 1 ]; then
                rm -rf "$TARGET_BIN_DIR"
            fi
            if [ "$NEW_VENV_PROMOTED" -eq 1 ]; then
                rm -rf "$TARGET_VENV_DIR"
            fi
            if [ "$HAD_TARGET_BIN" -eq 1 ] && [ -e "$ROLLBACK_BIN_DIR" ]; then
                mv "$ROLLBACK_BIN_DIR" "$TARGET_BIN_DIR"
            fi
            if [ "$HAD_TARGET_VENV" -eq 1 ] && [ -e "$ROLLBACK_VENV_DIR" ]; then
                mv "$ROLLBACK_VENV_DIR" "$TARGET_VENV_DIR"
            fi
        fi
        if [ -n "$INSTALL_STAGE" ] && [ -e "$INSTALL_STAGE" ]; then
            rm -rf "$INSTALL_STAGE"
        fi
    fi
    return "$exit_code"
}

mkdir -p "$RUNTIME_DIR"
INSTALL_STAGE=$(mktemp -d "$PLUGIN_DIR/.install-staging.XXXXXX")
INSTALL_RUNTIME_HOME="$INSTALL_STAGE/runtime-home"
mkdir -p "$INSTALL_RUNTIME_HOME"
BIN_DIR="$INSTALL_STAGE/bin"
VENV_DIR="$TARGET_VENV_DIR"
trap rollback_install EXIT
if [ -e "$ROLLBACK_BIN_DIR" ] || [ -e "$ROLLBACK_VENV_DIR" ]; then
    echo "Error: rollback path already exists; refusing installation." >&2
    exit 1
fi
INSTALL_PROMOTING=1
if [ -e "$TARGET_VENV_DIR" ]; then
    mv "$TARGET_VENV_DIR" "$ROLLBACK_VENV_DIR"
    HAD_TARGET_VENV=1
fi

# ------------------------------------------------------------------
# 2. Install code-search (Python, exact Git revision or release wheel)
# ------------------------------------------------------------------
echo "[2/5] Installing code-search (semantic search)..."
mkdir -p "$BIN_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "  Creating virtual environment..."
    NEW_VENV_PROMOTED=1
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Determine pip and python paths (cross-platform)
if [ -f "$VENV_DIR/bin/pip" ]; then
    VENV_PIP="$VENV_DIR/bin/pip"
    VENV_PYTHON="$VENV_DIR/bin/python"
    CODE_SEARCH_MCP="$VENV_DIR/bin/code-search-mcp"
elif [ -f "$VENV_DIR/Scripts/pip.exe" ]; then
    VENV_PIP="$VENV_DIR/Scripts/pip"
    VENV_PYTHON="$VENV_DIR/Scripts/python"
    CODE_SEARCH_MCP="$VENV_DIR/Scripts/code-search-mcp.exe"
else
    echo "Error: Could not find pip in venv"
    exit 1
fi

case "$CODE_SEARCH_KIND" in
    git)
        CODE_SEARCH_REF=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["revision"])' \
            "$BOM_FILE")
        echo "  Installing code-search from GitHub..."
        # Distribution name of the pinned source; older pins used redacted-code-search.
        CODE_SEARCH_DIST=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"].get("distribution", "redacted-code-search"))' \
            "$BOM_FILE")
        "$VENV_PIP" install --quiet \
            "${CODE_SEARCH_DIST} @ git+${CODE_SEARCH_REPOSITORY}@${CODE_SEARCH_REF}"
        run_with_allowed_environment \
            "$VENV_PYTHON" "$PLUGIN_DIR/scripts/verify_code_search_revision.py" \
                "$CODE_SEARCH_REF" \
                --repository "$CODE_SEARCH_REPOSITORY"
        ;;
    github-release)
        CODE_SEARCH_TAG=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["tag"])' \
            "$BOM_FILE")
        CODE_SEARCH_SOURCE_REVISION=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["source_revision"])' \
            "$BOM_FILE")
        CODE_SEARCH_WHEEL=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["asset"]["name"])' \
            "$BOM_FILE")
        CODE_SEARCH_WHEEL_SHA256=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["asset"]["sha256"])' \
            "$BOM_FILE")
        CODE_SEARCH_BUNDLE=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["attestation"]["bundle"]["name"])' \
            "$BOM_FILE")
        CODE_SEARCH_BUNDLE_SHA256=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["attestation"]["bundle"]["sha256"])' \
            "$BOM_FILE")
        CODE_SEARCH_CHECKSUMS=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["checksums"]["name"])' \
            "$BOM_FILE")
        CODE_SEARCH_CHECKSUMS_SHA256=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["checksums"]["sha256"])' \
            "$BOM_FILE")
        CODE_SEARCH_SIGNER_WORKFLOW=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["attestation"]["signer_workflow"])' \
            "$BOM_FILE")
        CODE_SEARCH_SOURCE_REF=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["attestation"]["source_ref"])' \
            "$BOM_FILE")
        CODE_SEARCH_DOWNLOAD_DIR="$BIN_DIR/.code-search-download"
        mkdir -p "$CODE_SEARCH_DOWNLOAD_DIR"

        RESOLVED_CODE_SEARCH_REVISION=$(resolve_release_tag_commit \
            "$CODE_SEARCH_REPOSITORY" \
            "$CODE_SEARCH_TAG")
        if [ "$RESOLVED_CODE_SEARCH_REVISION" != "$CODE_SEARCH_SOURCE_REVISION" ]; then
            echo "Error: code-search tag source revision mismatch." >&2
            echo "  expected: $CODE_SEARCH_SOURCE_REVISION" >&2
            echo "  actual:   $RESOLVED_CODE_SEARCH_REVISION" >&2
            exit 1
        fi
        echo "  Downloading tested code-search wheel and attestation bundle..."
        for release_file in "$CODE_SEARCH_WHEEL" "$CODE_SEARCH_BUNDLE" "$CODE_SEARCH_CHECKSUMS"; do
            download_release_asset "$CODE_SEARCH_REPOSITORY" "$CODE_SEARCH_TAG" \
                "$release_file" "$CODE_SEARCH_DOWNLOAD_DIR"
        done

        verify_sha256 \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_WHEEL" \
            "$CODE_SEARCH_WHEEL_SHA256" \
            "$CODE_SEARCH_WHEEL"
        verify_sha256 \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_BUNDLE" \
            "$CODE_SEARCH_BUNDLE_SHA256" \
            "$CODE_SEARCH_BUNDLE"
        verify_sha256 \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_CHECKSUMS" \
            "$CODE_SEARCH_CHECKSUMS_SHA256" \
            "$CODE_SEARCH_CHECKSUMS"
        verify_checksum_manifest \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_CHECKSUMS" \
            "$CODE_SEARCH_WHEEL" \
            "$CODE_SEARCH_WHEEL_SHA256"

        echo "  Verifying offline build provenance..."
        (
            cd "$CODE_SEARCH_DOWNLOAD_DIR"
            verify_release_attestation "$CODE_SEARCH_WHEEL" "$CODE_SEARCH_BUNDLE" \
                "$CODE_SEARCH_REPOSITORY" "$CODE_SEARCH_SIGNER_WORKFLOW" \
                "$CODE_SEARCH_SOURCE_REVISION" "$CODE_SEARCH_SOURCE_REF"
        )

        echo "  Installing the verified local code-search wheel..."
        run_with_allowed_environment \
            "$VENV_PIP" install --quiet --force-reinstall \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_WHEEL"
        run_with_allowed_environment \
            "$VENV_PYTHON" "$PLUGIN_DIR/scripts/verify_code_search_wheel.py" \
            "$CODE_SEARCH_TAG" \
            --asset-name "$CODE_SEARCH_WHEEL" \
            --sha256 "$CODE_SEARCH_WHEEL_SHA256"
        rm -f \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_WHEEL" \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_BUNDLE" \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_CHECKSUMS"
        rmdir "$CODE_SEARCH_DOWNLOAD_DIR" 2>/dev/null || true
        ;;
    *)
        echo "Error: unsupported code-search install kind: $CODE_SEARCH_KIND" >&2
        exit 1
        ;;
esac

echo "  code-search installed."
echo ""

# ------------------------------------------------------------------
# 3. Install code-graph (Go binary from GitHub releases)
# ------------------------------------------------------------------
echo "[3/5] Installing code-graph (structural analysis)..."

echo "  Tested release: $RELEASE_TAG"

GRAPH_DOWNLOAD_DIR="$BIN_DIR/.code-graph-download"
mkdir -p "$GRAPH_DOWNLOAD_DIR"
RESOLVED_GRAPH_REVISION=$(resolve_release_tag_commit \
    "$GRAPH_REPOSITORY" \
    "$RELEASE_TAG")
if [ "$RESOLVED_GRAPH_REVISION" != "$GRAPH_SOURCE_REVISION" ]; then
    echo "Error: code-graph tag source revision mismatch." >&2
    echo "  expected: $GRAPH_SOURCE_REVISION" >&2
    echo "  actual:   $RESOLVED_GRAPH_REVISION" >&2
    exit 1
fi
echo "  Downloading code-graph ${RELEASE_TAG} for ${PLATFORM}-${ARCH}..."
for release_file in "$ASSET_NAME" "$GRAPH_CHECKSUMS"; do
    download_release_asset "$GRAPH_REPOSITORY" "$RELEASE_TAG" \
        "$release_file" "$GRAPH_DOWNLOAD_DIR"
done

if [ ! -f "$GRAPH_DOWNLOAD_DIR/$ASSET_NAME" ]; then
    echo "Error: failed to download code-graph binary." >&2
    exit 1
fi

# Verify the archive and release manifest against the tested BOM.
echo "  Verifying checksums and checksum manifest..."
EXPECTED_SHA256=$(printf '%s' "$EXPECTED_SHA256" | tr '[:upper:]' '[:lower:]')
verify_sha256 \
    "$GRAPH_DOWNLOAD_DIR/$ASSET_NAME" \
    "$EXPECTED_SHA256" \
    "$ASSET_NAME"
verify_sha256 \
    "$GRAPH_DOWNLOAD_DIR/$GRAPH_CHECKSUMS" \
    "$GRAPH_CHECKSUMS_SHA256" \
    "$GRAPH_CHECKSUMS"
verify_checksum_manifest \
    "$GRAPH_DOWNLOAD_DIR/$GRAPH_CHECKSUMS" \
    "$ASSET_NAME" \
    "$EXPECTED_SHA256"
echo "  Checksum OK."

verify_sha256 \
    "$GRAPH_ATTESTATION_BUNDLE" \
    "$GRAPH_ATTESTATION_BUNDLE_SHA256" \
    "$GRAPH_ATTESTATION_BUNDLE_PATH"

echo "  Verifying code-graph build provenance..."
(
    cd "$GRAPH_DOWNLOAD_DIR"
    verify_release_attestation "$ASSET_NAME" "$GRAPH_ATTESTATION_BUNDLE" \
        "$GRAPH_REPOSITORY" "$GRAPH_SIGNER_WORKFLOW" \
        "$GRAPH_SOURCE_REVISION" "$GRAPH_SOURCE_REF"
)

GRAPH_EXTRACT_DIR="$GRAPH_DOWNLOAD_DIR/extracted"
mkdir -p "$GRAPH_EXTRACT_DIR"
if [ "$EXT" = "tar.gz" ]; then
    run_with_allowed_environment tar xzf \
        "$GRAPH_DOWNLOAD_DIR/$ASSET_NAME" -C "$GRAPH_EXTRACT_DIR"
else
    run_with_allowed_environment unzip -qo \
        "$GRAPH_DOWNLOAD_DIR/$ASSET_NAME" -d "$GRAPH_EXTRACT_DIR"
fi
# Release archives contain exactly one binary. Current releases name it
# code-graph; releases before the rename shipped codebase-memory-mcp. Install
# it under the stable name the launcher expects.
EXTRACTED_GRAPH_BINARY=""
for candidate in "code-graph" "codebase-memory-mcp"; do
    if [ "$EXT" = "zip" ]; then
        candidate="$candidate.exe"
    fi
    if [ -f "$GRAPH_EXTRACT_DIR/$candidate" ]; then
        EXTRACTED_GRAPH_BINARY="$candidate"
        break
    fi
done
if [ -z "$EXTRACTED_GRAPH_BINARY" ]; then
    echo "Error: release archive $ASSET_NAME does not contain a code-graph binary." >&2
    exit 1
fi
mv "$GRAPH_EXTRACT_DIR/$EXTRACTED_GRAPH_BINARY" "$BIN_DIR/$GRAPH_BINARY"
rm -rf "$GRAPH_DOWNLOAD_DIR"
chmod +x "$BIN_DIR/$GRAPH_BINARY" 2>/dev/null || true

echo "  code-graph installed."
echo ""

# Installing optional Go SCIP precision generator. This remains outside the
# MCP server: /index-repo opts in explicitly and validates the binary again.
echo "Installing optional Go SCIP precision generator..."
if [ "$GO_SCIP_SUPPORTED" = "yes" ]; then
    GO_SCIP_DOWNLOAD_DIR="$BIN_DIR/.go-scip-download"
    mkdir -p "$GO_SCIP_DOWNLOAD_DIR"
    RESOLVED_GO_SCIP_REVISION=$(resolve_release_tag_commit \
        "$GO_SCIP_REPOSITORY" \
        "$GO_SCIP_TAG")
    if [ "$RESOLVED_GO_SCIP_REVISION" != "$GO_SCIP_SOURCE_REVISION" ]; then
        echo "Error: go-scip tag source revision mismatch." >&2
        exit 1
    fi
    download_release_asset "$GO_SCIP_REPOSITORY" "$GO_SCIP_TAG" \
        "$GO_SCIP_ASSET" "$GO_SCIP_DOWNLOAD_DIR"
    verify_sha256 \
        "$GO_SCIP_DOWNLOAD_DIR/$GO_SCIP_ASSET" \
        "$GO_SCIP_ARCHIVE_SHA256" \
        "$GO_SCIP_ASSET"
    run_with_allowed_environment tar xzf \
        "$GO_SCIP_DOWNLOAD_DIR/$GO_SCIP_ASSET" -C "$GO_SCIP_DOWNLOAD_DIR"
    verify_sha256 \
        "$GO_SCIP_DOWNLOAD_DIR/scip-go" \
        "$GO_SCIP_BINARY_SHA256" \
        "scip-go binary"
    mv "$GO_SCIP_DOWNLOAD_DIR/scip-go" "$BIN_DIR/scip-go"
    chmod +x "$BIN_DIR/scip-go"
    rm -f "$GO_SCIP_DOWNLOAD_DIR/$GO_SCIP_ASSET" \
        "$GO_SCIP_DOWNLOAD_DIR/LICENSE"
    rmdir "$GO_SCIP_DOWNLOAD_DIR"
    run_with_allowed_environment \
        "$PYTHON" "$PLUGIN_DIR/scripts/prepare_scip_index.py" verify \
        --generator "$BIN_DIR/scip-go" \
        --component-bom "$BOM_FILE"
    echo "  scip-go $GO_SCIP_VERSION installed and verified."
else
    echo "  Auto SCIP precision unavailable for $ASSET_KEY; heuristic and supplied SCIP modes remain available."
fi
echo ""

# Install the TypeScript compiler indexer into an isolated plugin runtime.
# Target repositories supply their own already-installed dependency trees;
# this installer never runs npm in a target checkout.
echo "Installing optional TypeScript SCIP precision generator..."
if [ "$TYPESCRIPT_SCIP_SUPPORTED" = "yes" ]; then
    if ! command -v curl >/dev/null 2>&1; then
        echo "Error: curl is required to install the pinned Node runtime." >&2
        exit 1
    fi
    TYPESCRIPT_SCIP_RUNTIME="$BIN_DIR/scip-typescript-runtime"
    TYPESCRIPT_SCIP_DOWNLOAD="$BIN_DIR/.typescript-scip-download"
    mkdir -p "$TYPESCRIPT_SCIP_DOWNLOAD"
    run_with_allowed_environment curl --fail --location --silent --show-error \
        "$TYPESCRIPT_NODE_BASE_URL/$TYPESCRIPT_NODE_ASSET" \
        --output "$TYPESCRIPT_SCIP_DOWNLOAD/$TYPESCRIPT_NODE_ASSET"
    verify_sha256 \
        "$TYPESCRIPT_SCIP_DOWNLOAD/$TYPESCRIPT_NODE_ASSET" \
        "$TYPESCRIPT_NODE_ARCHIVE_SHA256" \
        "$TYPESCRIPT_NODE_ASSET"
    case "$TYPESCRIPT_NODE_ASSET" in
        *.tar.gz)
            run_with_allowed_environment tar xzf \
                "$TYPESCRIPT_SCIP_DOWNLOAD/$TYPESCRIPT_NODE_ASSET" \
                -C "$TYPESCRIPT_SCIP_DOWNLOAD"
            TYPESCRIPT_NODE_DIRECTORY=${TYPESCRIPT_NODE_ASSET%.tar.gz}
            ;;
        *.tar.xz)
            run_with_allowed_environment tar xJf \
                "$TYPESCRIPT_SCIP_DOWNLOAD/$TYPESCRIPT_NODE_ASSET" \
                -C "$TYPESCRIPT_SCIP_DOWNLOAD"
            TYPESCRIPT_NODE_DIRECTORY=${TYPESCRIPT_NODE_ASSET%.tar.xz}
            ;;
        *.zip)
            run_with_allowed_environment unzip -qo \
                "$TYPESCRIPT_SCIP_DOWNLOAD/$TYPESCRIPT_NODE_ASSET" \
                -d "$TYPESCRIPT_SCIP_DOWNLOAD"
            TYPESCRIPT_NODE_DIRECTORY=${TYPESCRIPT_NODE_ASSET%.zip}
            ;;
        *)
            echo "Error: unsupported Node runtime archive." >&2
            exit 1
            ;;
    esac
    mkdir -p "$TYPESCRIPT_SCIP_RUNTIME"
    mv \
        "$TYPESCRIPT_SCIP_DOWNLOAD/$TYPESCRIPT_NODE_DIRECTORY" \
        "$TYPESCRIPT_SCIP_RUNTIME/node"
    rm -f "$TYPESCRIPT_SCIP_DOWNLOAD/$TYPESCRIPT_NODE_ASSET"
    rmdir "$TYPESCRIPT_SCIP_DOWNLOAD"
    if [ -f "$TYPESCRIPT_SCIP_RUNTIME/node/bin/node" ]; then
        TYPESCRIPT_NODE_BINARY="$TYPESCRIPT_SCIP_RUNTIME/node/bin/node"
        TYPESCRIPT_NPM_CLI="$TYPESCRIPT_SCIP_RUNTIME/node/lib/node_modules/npm/bin/npm-cli.js"
    else
        TYPESCRIPT_NODE_BINARY="$TYPESCRIPT_SCIP_RUNTIME/node/node.exe"
        TYPESCRIPT_NPM_CLI="$TYPESCRIPT_SCIP_RUNTIME/node/node_modules/npm/bin/npm-cli.js"
    fi
    verify_sha256 \
        "$TYPESCRIPT_NODE_BINARY" \
        "$TYPESCRIPT_NODE_BINARY_SHA256" \
        "Node runtime binary"
    mkdir -p "$TYPESCRIPT_SCIP_RUNTIME/package"
    verify_sha256 \
        "$PLUGIN_DIR/$TYPESCRIPT_SCIP_LOCKFILE" \
        "$TYPESCRIPT_SCIP_LOCKFILE_SHA256" \
        "$TYPESCRIPT_SCIP_LOCKFILE"
    cp \
        "$PLUGIN_DIR/$TYPESCRIPT_SCIP_PACKAGE_MANIFEST" \
        "$TYPESCRIPT_SCIP_RUNTIME/package/package.json"
    cp \
        "$PLUGIN_DIR/$TYPESCRIPT_SCIP_LOCKFILE" \
        "$TYPESCRIPT_SCIP_RUNTIME/package/package-lock.json"
    run_with_allowed_environment \
        "$TYPESCRIPT_NODE_BINARY" "$TYPESCRIPT_NPM_CLI" ci \
        --prefix "$TYPESCRIPT_SCIP_RUNTIME/package" \
        --ignore-scripts --no-audit --no-fund
    TYPESCRIPT_SCIP_GENERATOR="$TYPESCRIPT_SCIP_RUNTIME/package/$TYPESCRIPT_SCIP_ENTRYPOINT"
    verify_sha256 \
        "$TYPESCRIPT_SCIP_GENERATOR" \
        "$TYPESCRIPT_SCIP_ENTRYPOINT_SHA256" \
        "$TYPESCRIPT_SCIP_PACKAGE entrypoint"
    run_with_allowed_environment \
        "$PYTHON" "$PLUGIN_DIR/scripts/prepare_scip_index.py" verify \
        --language typescript \
        --runtime "$TYPESCRIPT_NODE_BINARY" \
        --generator "$TYPESCRIPT_SCIP_GENERATOR" \
        --component-bom "$BOM_FILE"
    echo "  $TYPESCRIPT_SCIP_PACKAGE $TYPESCRIPT_SCIP_VERSION installed with Node $TYPESCRIPT_NODE_VERSION and verified."
else
    echo "  Automatic TypeScript SCIP precision unavailable for $ASSET_KEY."
fi
echo ""

# ------------------------------------------------------------------
# 4. Create launcher script (cross-platform wrapper)
# ------------------------------------------------------------------
echo "[4/5] Preparing launcher scripts..."

# The launchers are committed in bin/ (run-code-search, code-graph) and are
# what .mcp.json points at. They exec the components installed below and
# bootstrap this installer on first launch if the components are missing.
for launcher in run-code-search code-graph; do
    if [ ! -f "$LAUNCHER_DIR/$launcher" ]; then
        echo "Error: committed launcher is missing: $LAUNCHER_DIR/$launcher" >&2
        exit 1
    fi
    chmod +x "$LAUNCHER_DIR/$launcher"
done

echo "  Launchers ready."
echo ""

# ------------------------------------------------------------------
# 5. Verify the installed MCP contracts
# ------------------------------------------------------------------
echo "[5/5] Validating installed MCP tool contracts..."
run_with_allowed_environment \
    "$VENV_PYTHON" "$PLUGIN_DIR/scripts/validate_installed.py" \
        --server "code-search=$CODE_SEARCH_MCP" \
        --server "code-graph=$BIN_DIR/$GRAPH_BINARY"
echo ""

rm -rf "$INSTALL_RUNTIME_HOME"
echo "Promoting validated installation..."
if [ -e "$TARGET_BIN_DIR" ]; then
    mv "$TARGET_BIN_DIR" "$ROLLBACK_BIN_DIR"
    HAD_TARGET_BIN=1
fi
mv "$BIN_DIR" "$TARGET_BIN_DIR"
NEW_BIN_PROMOTED=1
INSTALL_COMMITTED=1
INSTALL_PROMOTING=0
rm -rf "$ROLLBACK_BIN_DIR" "$ROLLBACK_VENV_DIR"
rmdir "$INSTALL_STAGE"
trap - EXIT

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
echo "=== Component Installation Complete ==="
echo ""
case "$READINESS_STATUS" in
    blocked)
        echo "=== INTEGRATED READINESS: BLOCKED ==="
        echo "$READINESS_REASON"
        echo ""
        echo "The component schemas validated, but do not run /index-repo with this BOM."
        echo "Wait for a BOM whose tested capabilities and readiness evidence pass validation."
        ;;
    ready)
        echo "=== INTEGRATED READINESS: READY ==="
        echo "Components installed under .venv/ and .runtime/bin/; bin/ launchers are ready."
        echo "If the plugin is not installed yet:"
        echo "  claude plugin marketplace add brandyn-s/codebase-search-plugin"
        echo "  claude plugin install codebase-search@code-intelligence --scope user"
        echo "Then index a repo from Claude Code:"
        echo "  /index-repo <repo-path>"
        ;;
    *)
        echo "Error: unknown integrated readiness status: $READINESS_STATUS" >&2
        exit 1
        ;;
esac
