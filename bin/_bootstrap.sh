# shellcheck shell=bash
# Shared bootstrap logic for the committed MCP launchers in bin/.
# Sourced by bin/run-code-search and bin/code-graph; not executable on its own.
#
# The launchers are what .mcp.json points at, so they must exist before
# install.sh has ever run. On first launch they install the exact components
# pinned in component-bom.json, then exec the real server.
#
# stdout is the MCP protocol channel. Everything this file prints goes to
# stderr; the installer's output is redirected to a log file.

code_search_executable() {
    local plugin_dir="$1"
    if [ -x "$plugin_dir/.venv/bin/code-search-mcp" ]; then
        printf '%s\n' "$plugin_dir/.venv/bin/code-search-mcp"
    elif [ -f "$plugin_dir/.venv/Scripts/code-search-mcp.exe" ]; then
        printf '%s\n' "$plugin_dir/.venv/Scripts/code-search-mcp.exe"
    fi
}

code_graph_executable() {
    local plugin_dir="$1"
    if [ -x "$plugin_dir/.runtime/bin/code-graph" ]; then
        printf '%s\n' "$plugin_dir/.runtime/bin/code-graph"
    elif [ -f "$plugin_dir/.runtime/bin/code-graph.exe" ]; then
        printf '%s\n' "$plugin_dir/.runtime/bin/code-graph.exe"
    fi
}

components_installed() {
    local plugin_dir="$1"
    [ -n "$(code_search_executable "$plugin_dir")" ] &&
        [ -n "$(code_graph_executable "$plugin_dir")" ]
}

_bootstrap_log() {
    printf '[code-intelligence] %s\n' "$*" >&2
}

# True while component-bom.json declares that the pinned releases do not exist
# yet. Plain grep so the launcher needs no Python before the venv exists.
_bom_is_pending_first_release() {
    local plugin_dir="$1"
    grep -Eq '"promotion_state"[[:space:]]*:[[:space:]]*"pending-first-release"' \
        "$plugin_dir/component-bom.json" 2>/dev/null
}

# A lock is live while the recorded pid is alive. A lock without a pid file
# is treated as live for a grace period (the owner writes it right after
# mkdir), then as abandoned.
_LOCK_NO_PID_SECONDS=0
_lock_holder_alive() {
    local lock="$1"
    local pid
    pid="$(cat "$lock/pid" 2>/dev/null || true)"
    if [ -z "$pid" ]; then
        _LOCK_NO_PID_SECONDS=$((_LOCK_NO_PID_SECONDS + 2))
        [ "$_LOCK_NO_PID_SECONDS" -le 10 ]
        return
    fi
    _LOCK_NO_PID_SECONDS=0
    kill -0 "$pid" 2>/dev/null
}

# Run install.sh once for this plugin directory, serialized across launchers.
# The installer runs detached so that a client-side MCP startup timeout that
# kills this launcher does not abort a half-finished install; the next launch
# simply waits for it to finish.
ensure_components_installed() {
    local plugin_dir="$1"
    local runtime_dir="$plugin_dir/.runtime"
    local lock="$runtime_dir/bootstrap.lock"
    local log="$runtime_dir/bootstrap.log"
    local status="$runtime_dir/bootstrap.status"
    local wait_limit="${CODE_INTEL_BOOTSTRAP_WAIT_SECONDS:-1800}"
    local waited=0
    local progress_every=30

    if components_installed "$plugin_dir"; then
        return 0
    fi
    if [ "${CODE_INTEL_NO_BOOTSTRAP:-}" = "1" ]; then
        _bootstrap_log "components are not installed and CODE_INTEL_NO_BOOTSTRAP=1; run: bash \"$plugin_dir/install.sh\""
        return 1
    fi
    if [ ! -f "$plugin_dir/install.sh" ]; then
        _bootstrap_log "install.sh not found in $plugin_dir"
        return 1
    fi
    if _bom_is_pending_first_release "$plugin_dir"; then
        _bootstrap_log "components not yet released; see docs/INSTALL.md"
        _bootstrap_log "component-bom.json is in promotion_state pending-first-release: the pinned code-graph and code-search releases are not published, so the plugin cannot install them yet."
        return 1
    fi

    mkdir -p "$runtime_dir"
    while ! mkdir "$lock" 2>/dev/null; do
        if components_installed "$plugin_dir"; then
            return 0
        fi
        if ! _lock_holder_alive "$lock"; then
            # A previous bootstrap died without cleaning up. Take over.
            rm -rf "$lock"
            continue
        fi
        if [ "$waited" -ge "$wait_limit" ]; then
            _bootstrap_log "gave up after ${wait_limit}s waiting for another bootstrap (lock: $lock, log: $log)"
            return 1
        fi
        if [ $((waited % progress_every)) -eq 0 ]; then
            _bootstrap_log "another launcher is installing the components; waiting (log: $log)"
        fi
        sleep 2
        waited=$((waited + 2))
    done

    # Another launcher may have finished the install while we waited.
    if components_installed "$plugin_dir"; then
        rm -rf "$lock"
        return 0
    fi
    # Claim the lock with this launcher's pid first, then hand it to the
    # detached installer so a killed launcher does not orphan the lock.
    printf '%s\n' "$$" > "$lock/pid"
    _bootstrap_log "components are not installed yet; running install.sh (first launch only, log: $log)"
    : > "$status"
    (
        set +e
        trap '' HUP
        cd "$plugin_dir" || exit 1
        bash "$plugin_dir/install.sh" >"$log" 2>&1
        rc=$?
        printf '%s\n' "$rc" > "$status"
        rm -rf "$lock"
        exit "$rc"
    ) &
    printf '%s\n' "$!" > "$lock/pid" 2>/dev/null || true

    while [ -d "$lock" ]; do
        if [ "$waited" -ge "$wait_limit" ]; then
            _bootstrap_log "installer still running after ${wait_limit}s; it continues in the background (log: $log). Reconnect the MCP server once it finishes."
            return 1
        fi
        if [ "$waited" -gt 0 ] && [ $((waited % progress_every)) -eq 0 ]; then
            _bootstrap_log "still installing components (log: $log)"
        fi
        sleep 2
        waited=$((waited + 2))
    done

    if [ "$(cat "$status" 2>/dev/null)" != "0" ] || ! components_installed "$plugin_dir"; then
        _bootstrap_log "component installation failed (exit $(cat "$status" 2>/dev/null || echo "?")); last lines of $log:"
        tail -n 8 "$log" 2>/dev/null | sed 's/^/[code-intelligence]   /' >&2 || true
        _bootstrap_log "fix the cause and restart the MCP server, or run: bash \"$plugin_dir/install.sh\""
        return 1
    fi
    _bootstrap_log "components installed"
    return 0
}
