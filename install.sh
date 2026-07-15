#!/bin/bash
# =============================================================================
# PAM SSH 2FA - Installation Script
# =============================================================================
#
# This script installs the PAM SSH 2FA push notification module. It does
# NOT touch /etc/pam.d/sshd or /etc/ssh/sshd_config -- those edits are
# printed as instructions for you to apply and test manually (see
# CLAUDE.md/MODERNIZATION_PLAN.md for why: the correct PAM stack position
# varies by distro release and hasn't been validated across all of them
# yet, so this script won't guess for you).
#
# WHAT IT DOES:
# 1. Installs required packages (libpam-python, apprise)
# 2. Creates configuration directory and files
# 3. Installs the module, utility scripts, and (if requested) the
#    approval server, recording everything in an installation manifest
# 4. Sets correct file permissions
# 5. Runs validation checks (module self-test, sshd -t, port availability)
#
# USAGE:
#   sudo ./install.sh                       # Interactive installation
#   sudo ./install.sh --dry-run             # Preview with no changes (no sudo needed)
#   sudo ./install.sh --yes                 # Non-interactive, conservative defaults
#   sudo ./install.sh --enable-link-approval  # Also install the approval server
#   sudo ./install.sh --uninstall           # Remove exactly what was installed
#   sudo ./install.sh --help                # Show all options
#
# REQUIREMENTS:
# - Debian 11+ or Ubuntu 20.04+
# - Root privileges (sudo) -- except --dry-run, which works without it
# - Internet connection for package installation
#
# SAFETY:
# - Every created file/directory and every backup made before an
#   overwrite is recorded in an installation manifest, so --uninstall
#   removes precisely what this script installed
# - Does NOT restart SSH by default
# - Does NOT touch /etc/pam.d/sshd or /etc/ssh/sshd_config
# - Does NOT activate the PAM module by default (manual step required)
# - Always keep a backup SSH session open when testing!
#
# =============================================================================

set -e  # Exit on any error

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------

# Installation paths
INSTALL_DIR="/etc/pam-ssh-2fa"
MODULE_FILE="pam_ssh_2fa.py"
CONFIG_FILE="config.ini"
STORAGE_DIR="/var/run/pam-ssh-2fa"
LOG_FILE="/var/log/pam-ssh-2fa.log"

# Script directory (where the source files are)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Backup suffix with timestamp
BACKUP_SUFFIX=".backup-$(date +%Y%m%d-%H%M%S)"

# Installation manifest: every file/dir this run creates and every backup
# it makes is recorded here, so --uninstall can remove exactly what was
# installed and restore exactly what was backed up, instead of guessing.
MANIFEST_FILE="${INSTALL_DIR}/.install-manifest"

# Flags, set from CLI args in main()
DRY_RUN=false
ASSUME_YES=false
# Tri-state: "" (ask interactively), "yes", "no"
ENABLE_LINK_APPROVAL=""

# Colors for output (disabled if not a terminal)
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'  # No Color
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi


# -----------------------------------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------------------------------

# Print colored status messages
info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Check if running as root. In --dry-run mode this only warns, so a
# preview can be run without sudo.
check_root() {
    if [[ $EUID -ne 0 ]]; then
        if $DRY_RUN; then
            warn "Not running as root -- some checks below may be inaccurate in a real run"
        else
            error "This script must be run as root (use sudo)"
            exit 1
        fi
    fi
}

# Check if a command exists
command_exists() {
    command -v "$1" &>/dev/null
}

# Run a mutating command, or just print it under --dry-run.
# Every install/uninstall step that changes the filesystem or system
# state should go through this so --dry-run is a complete, accurate
# preview rather than a partial one.
run() {
    if $DRY_RUN; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

# -----------------------------------------------------------------------------
# INSTALLATION MANIFEST
#
# Records every directory/file this run creates and every backup it makes,
# so --uninstall can remove exactly what was installed and restore exactly
# what was backed up instead of guessing based on hardcoded paths.
# Format: one "TYPE|path[|extra]" entry per line.
#   DIR|<path>              - a directory we created (didn't exist before)
#   FILE|<path>              - a file we created/copied (didn't exist before)
#   BACKUP|<original>|<backup> - we backed up <original> to <backup> before
#                                 overwriting it
# -----------------------------------------------------------------------------

manifest_init() {
    if $DRY_RUN; then
        return
    fi
    mkdir -p "$(dirname "$MANIFEST_FILE")"
    {
        echo "# pam-ssh-2fa installation manifest"
        echo "# Generated $(date -Iseconds) by install.sh"
    } >> "$MANIFEST_FILE"
    chmod 600 "$MANIFEST_FILE"
}

manifest_add() {
    if $DRY_RUN; then
        return
    fi
    echo "$1|$2${3:+|$3}" >> "$MANIFEST_FILE"
}

# Create a backup of a file before it gets overwritten, and record the
# backup in the manifest so --uninstall can restore it.
backup_file() {
    local file="$1"
    if [[ -f "$file" ]]; then
        local backup="${file}${BACKUP_SUFFIX}"
        run cp "$file" "$backup"
        manifest_add "BACKUP" "$file" "$backup"
        info "Backed up $file to $backup"
    fi
}

# Create a directory (with the given mode) only if it doesn't already
# exist, and record it in the manifest so --uninstall removes only
# directories this run actually created.
create_tracked_dir() {
    local dir="$1"
    local mode="$2"
    if [[ ! -d "$dir" ]]; then
        run mkdir -p "$dir"
        run chmod "$mode" "$dir"
        manifest_add "DIR" "$dir"
        return 0
    fi
    return 1
}

# Copy a file into place and record it in the manifest. If the
# destination already exists, back it up first so an upgrade doesn't
# silently clobber a customized file without a way back.
install_tracked_file() {
    local src="$1"
    local dest="$2"
    local mode="$3"
    if [[ -f "$dest" ]]; then
        backup_file "$dest"
    else
        manifest_add "FILE" "$dest"
    fi
    run cp "$src" "$dest"
    run chmod "$mode" "$dest"
}

# Detect the distribution
detect_distro() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO="$ID"
        DISTRO_VERSION="$VERSION_ID"
    elif command_exists lsb_release; then
        DISTRO=$(lsb_release -si | tr '[:upper:]' '[:lower:]')
        DISTRO_VERSION=$(lsb_release -sr)
    else
        DISTRO="unknown"
        DISTRO_VERSION="unknown"
    fi
}


# -----------------------------------------------------------------------------
# INSTALLATION FUNCTIONS
# -----------------------------------------------------------------------------

# Install system packages
install_packages() {
    info "Installing required system packages..."

    # Update package list
    run apt-get update -qq

    # Install libpam-python (provides pam_python.so)
    # This is the key package that allows Python PAM modules
    if ! dpkg -l | grep -q "libpam-python"; then
        run apt-get install -y libpam-python
        success "Installed libpam-python"
    else
        info "libpam-python already installed"
    fi

    # Install Python pip if not present
    if ! command_exists pip3; then
        run apt-get install -y python3-pip
        success "Installed python3-pip"
    fi

    # Install apprise via pip
    info "Installing Apprise notification library..."
    if python3 -c "import apprise" 2>/dev/null; then
        info "Apprise already installed"
    elif $DRY_RUN; then
        echo "[DRY-RUN] pip3 install apprise (falling back to --break-system-packages if needed)"
    else
        # Try a normal install first. Only reach for
        # --break-system-packages (which bypasses PEP 668's protection
        # for the system Python) if the plain install actually fails --
        # older Debian/Ubuntu releases and virtualenvs don't need it.
        local pip_err
        pip_err="$(mktemp)"
        if pip3 install apprise 2>"$pip_err"; then
            success "Installed apprise"
        elif grep -qi "externally-managed-environment" "$pip_err" && \
             pip3 install apprise --break-system-packages; then
            success "Installed apprise (--break-system-packages)"
        else
            cat "$pip_err" >&2
            rm -f "$pip_err"
            error "Failed to install apprise"
            exit 1
        fi
        rm -f "$pip_err"
    fi
}

# Create directories with proper permissions
create_directories() {
    info "Creating directories..."

    # Configuration directory. Created (and, if new, recorded) before the
    # manifest is initialized, since the manifest file lives inside it.
    local install_dir_was_new=false
    if [[ ! -d "$INSTALL_DIR" ]]; then
        run mkdir -p "$INSTALL_DIR"
        run chmod 750 "$INSTALL_DIR"
        install_dir_was_new=true
        success "Created $INSTALL_DIR"
    fi

    manifest_init
    if $install_dir_was_new; then
        manifest_add "DIR" "$INSTALL_DIR"
    fi

    # Per-user configuration directory
    if create_tracked_dir "$INSTALL_DIR/users" 750; then
        success "Created $INSTALL_DIR/users (for per-user configs)"
    fi

    # Runtime storage directory (for OTP codes)
    if create_tracked_dir "$STORAGE_DIR" 700; then
        success "Created $STORAGE_DIR"
    fi

    # Log directory
    LOG_DIR=$(dirname "$LOG_FILE")
    if create_tracked_dir "$LOG_DIR" 755; then
        success "Created $LOG_DIR"
    fi
}

# Install the module files
install_module() {
    info "Installing PAM module..."

    # Copy main module (backed up automatically if this is an upgrade)
    if [[ -f "${SCRIPT_DIR}/${MODULE_FILE}" ]]; then
        install_tracked_file "${SCRIPT_DIR}/${MODULE_FILE}" "${INSTALL_DIR}/${MODULE_FILE}" 644
        success "Installed ${MODULE_FILE}"
    else
        error "Module file not found: ${SCRIPT_DIR}/${MODULE_FILE}"
        exit 1
    fi

    # Utility scripts referenced by the README/CLAUDE.md but historically
    # not installed: manual notification testing and cron-driven cleanup
    # of expired OTP state.
    for util_file in test_notify.py cleanup_codes.py; do
        if [[ -f "${SCRIPT_DIR}/${util_file}" ]]; then
            install_tracked_file "${SCRIPT_DIR}/${util_file}" "${INSTALL_DIR}/${util_file}" 755
            success "Installed ${util_file}"
        else
            warn "${util_file} not found in ${SCRIPT_DIR} - skipping"
        fi
    done

    # Approval server: only install it if the admin actually wants
    # link-based auth. Installing it unconditionally means running an
    # HTTP(S) listener nobody asked for.
    if [[ "$ENABLE_LINK_APPROVAL" == "yes" ]]; then
        if [[ -f "${SCRIPT_DIR}/approval_server.py" ]]; then
            install_tracked_file "${SCRIPT_DIR}/approval_server.py" "${INSTALL_DIR}/approval_server.py" 755
            success "Installed approval_server.py"
        else
            warn "Approval server not found - link-based auth will not be available"
        fi

        if [[ -f "${SCRIPT_DIR}/pam-ssh-2fa-server.service" ]]; then
            install_tracked_file "${SCRIPT_DIR}/pam-ssh-2fa-server.service" \
                "/etc/systemd/system/pam-ssh-2fa-server.service" 644
            run systemctl daemon-reload
            success "Installed systemd service: pam-ssh-2fa-server.service"
            info "To enable link-based auth, run: sudo systemctl enable --now pam-ssh-2fa-server"
        fi
    else
        info "Skipping approval server (link-based auth not enabled)"
        info "Re-run with --enable-link-approval to add it later"
    fi

    # Copy config (only if it doesn't exist)
    if [[ ! -f "${INSTALL_DIR}/${CONFIG_FILE}" ]]; then
        if [[ -f "${SCRIPT_DIR}/${CONFIG_FILE}" ]]; then
            run cp "${SCRIPT_DIR}/${CONFIG_FILE}" "${INSTALL_DIR}/${CONFIG_FILE}"
            run chmod 600 "${INSTALL_DIR}/${CONFIG_FILE}"
            manifest_add "FILE" "${INSTALL_DIR}/${CONFIG_FILE}"
            success "Installed ${CONFIG_FILE}"
        else
            error "Config file not found: ${SCRIPT_DIR}/${CONFIG_FILE}"
            exit 1
        fi
    else
        warn "Config already exists, not overwriting: ${INSTALL_DIR}/${CONFIG_FILE}"
        # Still install the new one as example
        run cp "${SCRIPT_DIR}/${CONFIG_FILE}" "${INSTALL_DIR}/${CONFIG_FILE}.new"
        run chmod 600 "${INSTALL_DIR}/${CONFIG_FILE}.new"
        manifest_add "FILE" "${INSTALL_DIR}/${CONFIG_FILE}.new"
        info "New config saved as ${INSTALL_DIR}/${CONFIG_FILE}.new for reference"
    fi

    # Copy example per-user configs. These are always safe to refresh in
    # place (they're the shipped *.example templates, not a file a user
    # would hand-edit -- real per-user configs are named <user>.conf).
    if [[ -d "${SCRIPT_DIR}/examples/users" ]]; then
        for example_file in "${SCRIPT_DIR}/examples/users"/*.example; do
            if [[ -f "$example_file" ]]; then
                local dest="${INSTALL_DIR}/users/$(basename "$example_file")"
                run cp "$example_file" "$dest"
                run chmod 600 "$dest"
                manifest_add "FILE" "$dest"
            fi
        done
        success "Installed example per-user configs in ${INSTALL_DIR}/users/"
    fi
}

# Verify the installation
verify_installation() {
    info "Verifying installation..."

    local errors=0

    if $DRY_RUN; then
        info "Skipping verification under --dry-run (nothing was actually installed)"
        return 0
    fi

    # Check pam_python.so exists
    local pam_python_paths=(
        "/lib/security/pam_python.so"
        "/lib/x86_64-linux-gnu/security/pam_python.so"
        "/lib/aarch64-linux-gnu/security/pam_python.so"
        "/usr/lib/security/pam_python.so"
        "/usr/lib/x86_64-linux-gnu/security/pam_python.so"
    )

    local found_pam_python=false
    for path in "${pam_python_paths[@]}"; do
        if [[ -f "$path" ]]; then
            found_pam_python=true
            success "Found pam_python.so at $path"
            break
        fi
    done

    # NOTE: use "errors=$((errors + 1))" rather than "((errors++))" below.
    # With `set -e`, "((errors++))" returns the PRE-increment value as its
    # exit status, so the very first increment from 0 evaluates to a
    # "failed" command and silently kills the whole script -- including
    # show_instructions, which never runs. This bit the previous version
    # of this function.
    if ! $found_pam_python; then
        error "pam_python.so not found!"
        errors=$((errors + 1))
    fi

    # Check module file
    if [[ -f "${INSTALL_DIR}/${MODULE_FILE}" ]]; then
        success "Module file installed"
    else
        error "Module file missing!"
        errors=$((errors + 1))
    fi

    # Check config file
    if [[ -f "${INSTALL_DIR}/${CONFIG_FILE}" ]]; then
        success "Config file installed"
    else
        error "Config file missing!"
        errors=$((errors + 1))
    fi

    # Check utility scripts
    for util_file in test_notify.py cleanup_codes.py; do
        if [[ -f "${INSTALL_DIR}/${util_file}" ]]; then
            success "${util_file} installed"
        else
            warn "${util_file} not installed (not fatal -- it's a manual diagnostic tool)"
        fi
    done

    # Check apprise
    if python3 -c "import apprise" 2>/dev/null; then
        success "Apprise module available"
    else
        error "Apprise module not importable!"
        errors=$((errors + 1))
    fi

    # Run module self-test
    info "Running module self-test..."
    if python3 "${INSTALL_DIR}/${MODULE_FILE}" --config "${INSTALL_DIR}/${CONFIG_FILE}" 2>/dev/null; then
        success "Module self-test passed"
    else
        warn "Module self-test had issues (may be expected if no notification URL configured)"
    fi

    # If link-based auth was enabled, check the approval server's port
    # isn't already bound by something else before the admin tries to
    # start the service and gets a confusing failure.
    if [[ "$ENABLE_LINK_APPROVAL" == "yes" ]] && command_exists ss; then
        local server_port
        server_port=$(grep -A5 '^\[server\]' "${INSTALL_DIR}/${CONFIG_FILE}" 2>/dev/null \
            | grep '^port' | head -1 | cut -d= -f2 | tr -d ' ')
        server_port="${server_port:-9110}"
        if ss -ltn "( sport = :$server_port )" 2>/dev/null | grep -q ":$server_port"; then
            warn "Port $server_port is already in use -- the approval server may fail to start"
        else
            success "Approval server port $server_port is free"
        fi
    fi

    # Informational only: validates the CURRENT sshd_config, which this
    # script does not modify. A pre-existing syntax error here isn't
    # something this install caused, so it doesn't count toward $errors,
    # but the admin should know before they go add a PAM line to it.
    if command_exists sshd; then
        local sshd_check_err
        sshd_check_err="$(mktemp)"
        if sshd -t 2>"$sshd_check_err"; then
            success "Current sshd configuration is valid (sshd -t)"
        else
            warn "Current sshd configuration has existing errors (sshd -t):"
            sed 's/^/    /' "$sshd_check_err" >&2
        fi
        rm -f "$sshd_check_err"

        # sshd -t only checks syntax; it says nothing about whether
        # keyboard-interactive PAM auth is actually reachable. sshd -T
        # reports the EFFECTIVE (resolved) config, which is what
        # actually matters -- a Match block or a later line can silently
        # override a setting you think you already made. This is
        # informational (these are the settings BEFORE any manual
        # sshd_config edit you make later, per step 5 of the next-steps
        # instructions) and never fails the install.
        info "Current effective sshd settings relevant to 2FA (sshd -T):"
        sshd -T 2>/dev/null \
            | grep -iE '^(usepam|kbdinteractiveauthentication|authenticationmethods|passwordauthentication)' \
            | sed 's/^/    /'
    fi

    return $errors
}

# Show post-installation instructions
show_instructions() {
    echo ""
    echo "============================================================================="
    echo "Installation Complete!"
    echo "============================================================================="
    echo ""
    echo "NEXT STEPS:"
    echo ""
    echo "1. Configure notifications (choose one option):"
    echo ""
    echo "   OPTION A: Single notification service for all users"
    echo "   Edit ${INSTALL_DIR}/${CONFIG_FILE}"
    echo "   Add your Apprise URL(s) to the [notifications] section"
    echo ""
    echo "   OPTION B: Different services per user (recommended)"
    echo "   Create per-user configs in ${INSTALL_DIR}/users/"
    echo "   Example: ${INSTALL_DIR}/users/doug.conf"
    echo "   See example files in ${INSTALL_DIR}/users/*.example"
    echo ""
    echo "   Examples:"
    echo "     ntfy:     apprise_urls = ntfy://ntfy.sh/your-secret-topic"
    echo "     Pushover: apprise_urls = pover://USERKEY@APPTOKEN"
    echo ""
    if [[ "$ENABLE_LINK_APPROVAL" == "yes" ]]; then
        echo "2. Finish setting up link-based authentication (approval server installed):"
        echo ""
        echo "   a. Set server URL in ${INSTALL_DIR}/${CONFIG_FILE}. Approval links are"
        echo "      bearer credentials, so http:// is rejected for public hosts by"
        echo "      default -- use https:// (native tls_cert/tls_key or a reverse"
        echo "      proxy) unless this is a loopback/private/Tailscale-only deployment:"
        echo "      [server]"
        echo "      port = 9110"
        echo "      url = https://YOUR_SERVER_IP_OR_HOSTNAME:9110"
        echo ""
        echo "   b. Open firewall port:"
        echo "      sudo ufw allow 9110/tcp"
        echo ""
        echo "   c. Start the approval server:"
        echo "      sudo systemctl enable --now pam-ssh-2fa-server"
        echo ""
        echo "   d. Set auth_method in per-user configs:"
        echo "      [auth]"
        echo "      method = link   # or 'both' for code + link"
        echo ""
    else
        echo "2. Link-based (click-to-approve) authentication was NOT installed."
        echo "   To add it later, re-run: sudo $0 --enable-link-approval"
        echo ""
    fi
    echo "3. Test notifications:"
    echo "   python3 ${INSTALL_DIR}/${MODULE_FILE} --test-notify"
    echo ""
    echo "4. Configure PAM (edit /etc/pam.d/sshd):"
    echo ""
    echo "   REPLACE this line:"
    echo "     @include common-auth"
    echo "   with:"
    echo "     auth required pam_python.so ${INSTALL_DIR}/${MODULE_FILE}"
    echo ""
    echo "   IMPORTANT: adding the module AFTER common-auth (instead of"
    echo "   replacing it) silently requires a valid Unix password before"
    echo "   2FA is even attempted, and makes login completely impossible"
    echo "   for password-locked (SSH-key-only) accounts. See"
    echo "   examples/pam.d-sshd.example for the validated explanation and"
    echo "   alternatives (e.g. requiring a password AND 2FA on purpose)."
    echo ""
    echo "5. Configure SSH (/etc/ssh/sshd_config):"
    echo ""
    echo "   Ensure these settings are present:"
    echo "   UsePAM yes"
    echo "   KbdInteractiveAuthentication yes"
    echo "   AuthenticationMethods publickey,keyboard-interactive:pam"
    echo ""
    echo "   Then verify with sshd -T, not just sshd -t: sshd -t only"
    echo "   checks syntax and will pass even if a Match block or another"
    echo "   line overrides these settings elsewhere in the file."
    echo "     sshd -t"
    echo "     sshd -T | grep -iE '^(usepam|kbdinteractiveauthentication|authenticationmethods)'"
    echo "   Confirm the output actually shows the three values above."
    echo ""
    echo "6. Verify the PAM change alone, before touching SSH at all:"
    echo "     sudo apt install pamtester"
    echo "     sudo pamtester sshd <youruser> authenticate"
    echo "   You should be prompted ONLY for the 2FA code/link, never a"
    echo "   Unix password. If a user account is password-locked, confirm"
    echo "   this still works for them specifically."
    echo ""
    echo "7. Test with a SEPARATE SSH session before logging out!"
    echo "   Always keep your current session open as backup."
    echo ""
    echo "8. Restart SSH when ready:"
    echo "   systemctl restart sshd"
    echo ""
    echo "9. Have a recovery plan ready BEFORE step 8: console/IPMI/serial"
    echo "   access, or a way to boot into rescue mode, in case something"
    echo "   still locks you out despite the checks above."
    echo ""
    echo "Every file this script created is recorded in:"
    echo "   ${MANIFEST_FILE}"
    echo "so 'sudo $0 --uninstall' can remove precisely what was installed."
    echo ""
    echo "============================================================================="
    echo "IMPORTANT: Do not close your current session until you have tested!"
    echo "============================================================================="
}


# -----------------------------------------------------------------------------
# UNINSTALL FUNCTION
# -----------------------------------------------------------------------------

uninstall() {
    echo "============================================================================="
    echo "PAM SSH 2FA - Uninstall"
    echo "============================================================================="
    echo ""
    warn "This will remove the PAM SSH 2FA module."
    warn "Your PAM and SSH configs will NOT be automatically reverted."
    echo ""

    if ! $ASSUME_YES; then
        read -p "Continue? [y/N] " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            info "Uninstall cancelled"
            exit 0
        fi
    fi

    # Stop and disable the approval server if it's running. This is a
    # service-state change, not a file removal, so it isn't covered by
    # the manifest and always runs regardless of whether one exists.
    if systemctl is-active --quiet pam-ssh-2fa-server 2>/dev/null; then
        info "Stopping approval server..."
        run systemctl stop pam-ssh-2fa-server
        success "Stopped approval server"
    fi

    if systemctl is-enabled --quiet pam-ssh-2fa-server 2>/dev/null; then
        info "Disabling approval server..."
        run systemctl disable pam-ssh-2fa-server
        success "Disabled approval server"
    fi

    if [[ -f "$MANIFEST_FILE" ]]; then
        info "Using installation manifest for precise removal"
        uninstall_from_manifest
    else
        warn "No installation manifest found (install predates this feature, or the"
        warn "manifest was removed) -- falling back to removing known default paths"
        uninstall_legacy_fallback
    fi

    if [[ -f "/etc/systemd/system/pam-ssh-2fa-server.service" ]]; then
        run systemctl daemon-reload
    fi

    # Clean up runtime files (OTP codes, approval state, rate-limit
    # counters). Always safe to remove in full on uninstall.
    run rm -rf "$STORAGE_DIR"
    success "Removed runtime storage"

    echo ""
    echo "============================================================================="
    echo "Uninstall complete."
    echo ""
    echo "IMPORTANT: You must manually:"
    echo "1. In /etc/pam.d/sshd, remove the pam_python.so line and restore"
    echo "   '@include common-auth'"
    echo "2. Revert changes to /etc/ssh/sshd_config"
    echo "3. Restart SSH: systemctl restart sshd"
    echo "============================================================================="
}

# Remove exactly what this installer created/backed up, per the manifest,
# instead of guessing at hardcoded paths. Config-like files (config.ini,
# its .new companion, and per-user configs) are still gated behind an
# explicit confirmation, same as before -- everything else (program
# files, the systemd unit) is removed unconditionally.
uninstall_from_manifest() {
    local manifest_files=()
    local manifest_dirs=()
    local manifest_backups=()

    local type a b
    while IFS='|' read -r type a b; do
        [[ -z "$type" || "$type" == \#* ]] && continue
        case "$type" in
            FILE) manifest_files+=("$a") ;;
            DIR) manifest_dirs+=("$a") ;;
            BACKUP) manifest_backups+=("$b") ;;  # only need the backup path here
        esac
    done < "$MANIFEST_FILE"

    local config_like=()
    local code_files=()
    local f
    for f in "${manifest_files[@]}"; do
        case "$f" in
            "${INSTALL_DIR}/${CONFIG_FILE}"|"${INSTALL_DIR}/${CONFIG_FILE}.new"|"${INSTALL_DIR}/users/"*)
                config_like+=("$f") ;;
            *)
                code_files+=("$f") ;;
        esac
    done

    for f in "${code_files[@]}"; do
        run rm -f "$f"
    done
    success "Removed installed program files"

    # Stray backups made by re-installs/upgrades of program files -- the
    # current file is being deleted anyway, so its old backup goes too.
    local backup
    for backup in "${manifest_backups[@]}"; do
        run rm -f "$backup"
    done

    if [[ ${#config_like[@]} -gt 0 ]]; then
        local reply="n"
        if $ASSUME_YES; then
            reply="n"  # --yes never implies deleting user config/secrets
            info "Preserving config files under --yes (rerun interactively to remove them)"
        else
            read -p "Remove config file and per-user configs too? [y/N] " -n 1 -r
            echo ""
            reply="$REPLY"
        fi
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            for f in "${config_like[@]}"; do
                run rm -f "$f"
            done
            run rm -rf "${INSTALL_DIR}/users"
            success "Removed config files"
        else
            info "Config files preserved"
        fi
    fi

    run rm -f "$MANIFEST_FILE"

    # Remove directories the installer created, deepest first, and only
    # if now empty (never force-remove a directory with leftover files).
    local d
    for d in $(printf '%s\n' "${manifest_dirs[@]}" | sort -r); do
        if [[ -d "$d" ]] && [[ -z "$(ls -A "$d" 2>/dev/null)" ]]; then
            run rmdir "$d"
        fi
    done
}

# Best-effort removal for installs that predate the manifest. Mirrors
# the original hardcoded-path behavior of this script.
uninstall_legacy_fallback() {
    if [[ -f "/etc/systemd/system/pam-ssh-2fa-server.service" ]]; then
        run rm -f /etc/systemd/system/pam-ssh-2fa-server.service
        success "Removed systemd service"
    fi

    info "Removing module files..."
    run rm -f "${INSTALL_DIR}/${MODULE_FILE}"
    run rm -f "${INSTALL_DIR}/approval_server.py"
    run rm -f "${INSTALL_DIR}/test_notify.py"
    run rm -f "${INSTALL_DIR}/cleanup_codes.py"
    success "Removed module files"

    if [[ -f "${INSTALL_DIR}/${CONFIG_FILE}" ]]; then
        local reply="n"
        if $ASSUME_YES; then
            info "Preserving config files under --yes (rerun interactively to remove them)"
        else
            read -p "Remove config file and per-user configs too? [y/N] " -n 1 -r
            echo ""
            reply="$REPLY"
        fi
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            run rm -f "${INSTALL_DIR}/${CONFIG_FILE}"
            run rm -f "${INSTALL_DIR}/${CONFIG_FILE}.new"
            run rm -rf "${INSTALL_DIR}/users"
            success "Removed config files"
        else
            info "Config files preserved"
        fi
    fi

    if [[ -d "$INSTALL_DIR" ]] && [[ -z "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
        run rmdir "$INSTALL_DIR"
        success "Removed empty install directory"
    fi
}


# -----------------------------------------------------------------------------
# HELP FUNCTION
# -----------------------------------------------------------------------------

show_help() {
    cat << EOF
PAM SSH 2FA - Installation Script

USAGE:
    sudo $0 [OPTIONS]

OPTIONS:
    (none)                    Interactive installation
    --uninstall               Remove the PAM 2FA module
    --dry-run                 Print what would happen without changing anything
                               (works without sudo, for a full preview)
    --yes, -y                 Assume "yes" to confirmation prompts (non-interactive).
                               Never implies deleting config/secrets on uninstall.
    --enable-link-approval    Install and enable the approval server for
                               link-based (click-to-approve) authentication
    --no-link-approval         Skip the approval server (default if --yes is
                               given without either link-approval flag)
    --help, -h                Show this help message

DESCRIPTION:
    Installs a PAM module that provides two-factor authentication for SSH
    by sending one-time codes via push notification (using Apprise).

REQUIREMENTS:
    - Debian 11+ or Ubuntu 20.04+
    - Root privileges (--dry-run excepted)
    - Internet connection

INSTALLED FILES:
    ${INSTALL_DIR}/${MODULE_FILE}   - The PAM module
    ${INSTALL_DIR}/${CONFIG_FILE}   - Configuration file
    ${INSTALL_DIR}/.install-manifest - Tracks what this script created, so
                                        --uninstall can remove precisely that
    ${STORAGE_DIR}/                 - Runtime OTP storage
    ${LOG_FILE}                     - Log file

SUPPORTED NOTIFICATION SERVICES:
    - ntfy (ntfy.sh or self-hosted)
    - Pushover
    - Telegram
    - Slack
    - Discord
    - Email (SMTP)
    - 80+ more via Apprise

For more information, see:
    https://github.com/caronc/apprise/wiki

EOF
}


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

main() {
    local action="install"

    # Parse arguments. A plain while/case loop rather than getopt so this
    # keeps working on minimal systems without extra dependencies.
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help|-h)
                show_help
                exit 0
                ;;
            --uninstall)
                action="uninstall"
                ;;
            --dry-run)
                DRY_RUN=true
                ;;
            --yes|-y)
                ASSUME_YES=true
                ;;
            --enable-link-approval)
                ENABLE_LINK_APPROVAL="yes"
                ;;
            --no-link-approval)
                ENABLE_LINK_APPROVAL="no"
                ;;
            *)
                error "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
        shift
    done

    if $DRY_RUN; then
        warn "DRY RUN -- no changes will be made"
        echo ""
    fi

    if [[ "$action" == "uninstall" ]]; then
        check_root
        uninstall
        exit 0
    fi

    echo "============================================================================="
    echo "PAM SSH 2FA - Installation"
    echo "============================================================================="
    echo ""

    # Pre-flight checks
    check_root
    detect_distro

    info "Detected: $DISTRO $DISTRO_VERSION"

    case "$DISTRO" in
        debian|ubuntu|linuxmint|pop)
            info "Supported distribution detected"
            ;;
        *)
            warn "Distribution not officially supported, attempting anyway..."
            ;;
    esac

    echo ""
    info "This will install the PAM SSH 2FA push notification module."
    info "Your SSH config will NOT be modified automatically."
    echo ""

    if ! $ASSUME_YES; then
        read -p "Continue with installation? [Y/n] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Nn]$ ]]; then
            info "Installation cancelled"
            exit 0
        fi
    fi

    # Decide whether to install the approval server (link-based auth) if
    # the caller didn't already say via --enable-link-approval/--no-link-approval.
    if [[ -z "$ENABLE_LINK_APPROVAL" ]]; then
        if $ASSUME_YES; then
            # Conservative default under --yes: don't stand up a network
            # listener the caller didn't explicitly ask for.
            ENABLE_LINK_APPROVAL="no"
            info "Skipping approval server under --yes (pass --enable-link-approval to include it)"
        else
            echo ""
            read -p "Enable link-based (click-to-approve) authentication? [y/N] " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                ENABLE_LINK_APPROVAL="yes"
            else
                ENABLE_LINK_APPROVAL="no"
            fi
        fi
    fi

    echo ""

    # Installation steps
    install_packages
    echo ""

    create_directories
    echo ""

    install_module
    echo ""

    # Explicitly branch on the result instead of letting a nonzero
    # return trip `set -e` here: that would kill the script right after
    # this line with no explanation and without showing next steps.
    if verify_installation; then
        echo ""
        show_instructions
    else
        echo ""
        error "Installation finished with verification errors (see above)."
        error "Fix the issues above before configuring PAM -- 2FA will not work correctly yet."
        exit 1
    fi
}

# Run main function
main "$@"
