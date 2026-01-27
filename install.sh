#!/bin/bash
# =============================================================================
# PAM SSH 2FA - Installation Script
# =============================================================================
#
# This script installs and configures the PAM SSH 2FA push notification module.
#
# WHAT IT DOES:
# 1. Installs required packages (libpam-python, apprise)
# 2. Creates configuration directory and files
# 3. Optionally configures PAM and SSH (with backup)
# 4. Sets correct file permissions
# 5. Runs a basic test
#
# USAGE:
#   sudo ./install.sh              # Interactive installation
#   sudo ./install.sh --help       # Show help
#   sudo ./install.sh --uninstall  # Remove the module
#
# REQUIREMENTS:
# - Debian 11+ or Ubuntu 20.04+
# - Root privileges (sudo)
# - Internet connection for package installation
#
# SAFETY:
# - Backs up all modified files
# - Does NOT restart SSH by default
# - Does NOT activate PAM module by default (manual step required)
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

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Check if a command exists
command_exists() {
    command -v "$1" &>/dev/null
}

# Create a backup of a file
backup_file() {
    local file="$1"
    if [[ -f "$file" ]]; then
        cp "$file" "${file}${BACKUP_SUFFIX}"
        info "Backed up $file to ${file}${BACKUP_SUFFIX}"
    fi
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
    apt-get update -qq
    
    # Install libpam-python (provides pam_python.so)
    # This is the key package that allows Python PAM modules
    if ! dpkg -l | grep -q "libpam-python"; then
        apt-get install -y libpam-python
        success "Installed libpam-python"
    else
        info "libpam-python already installed"
    fi
    
    # Install Python pip if not present
    if ! command_exists pip3; then
        apt-get install -y python3-pip
        success "Installed python3-pip"
    fi
    
    # Install apprise via pip
    # Using --break-system-packages for newer Python versions
    info "Installing Apprise notification library..."
    if python3 -c "import apprise" 2>/dev/null; then
        info "Apprise already installed"
    else
        pip3 install apprise --break-system-packages 2>/dev/null || \
        pip3 install apprise
        success "Installed apprise"
    fi
}

# Create directories with proper permissions
create_directories() {
    info "Creating directories..."
    
    # Configuration directory
    if [[ ! -d "$INSTALL_DIR" ]]; then
        mkdir -p "$INSTALL_DIR"
        chmod 750 "$INSTALL_DIR"
        success "Created $INSTALL_DIR"
    fi
    
    # Per-user configuration directory
    if [[ ! -d "$INSTALL_DIR/users" ]]; then
        mkdir -p "$INSTALL_DIR/users"
        chmod 750 "$INSTALL_DIR/users"
        success "Created $INSTALL_DIR/users (for per-user configs)"
    fi
    
    # Runtime storage directory (for OTP codes)
    if [[ ! -d "$STORAGE_DIR" ]]; then
        mkdir -p "$STORAGE_DIR"
        chmod 700 "$STORAGE_DIR"
        success "Created $STORAGE_DIR"
    fi
    
    # Log directory
    LOG_DIR=$(dirname "$LOG_FILE")
    if [[ ! -d "$LOG_DIR" ]]; then
        mkdir -p "$LOG_DIR"
        success "Created $LOG_DIR"
    fi
}

# Install the module files
install_module() {
    info "Installing PAM module..."
    
    # Copy main module
    if [[ -f "${SCRIPT_DIR}/${MODULE_FILE}" ]]; then
        cp "${SCRIPT_DIR}/${MODULE_FILE}" "${INSTALL_DIR}/${MODULE_FILE}"
        chmod 644 "${INSTALL_DIR}/${MODULE_FILE}"
        success "Installed ${MODULE_FILE}"
    else
        error "Module file not found: ${SCRIPT_DIR}/${MODULE_FILE}"
        exit 1
    fi
    
    # Copy approval server
    if [[ -f "${SCRIPT_DIR}/approval_server.py" ]]; then
        cp "${SCRIPT_DIR}/approval_server.py" "${INSTALL_DIR}/approval_server.py"
        chmod 755 "${INSTALL_DIR}/approval_server.py"
        success "Installed approval_server.py"
    else
        warn "Approval server not found - link-based auth will not be available"
    fi
    
    # Copy config (only if it doesn't exist)
    if [[ ! -f "${INSTALL_DIR}/${CONFIG_FILE}" ]]; then
        if [[ -f "${SCRIPT_DIR}/${CONFIG_FILE}" ]]; then
            cp "${SCRIPT_DIR}/${CONFIG_FILE}" "${INSTALL_DIR}/${CONFIG_FILE}"
            chmod 600 "${INSTALL_DIR}/${CONFIG_FILE}"
            success "Installed ${CONFIG_FILE}"
        else
            error "Config file not found: ${SCRIPT_DIR}/${CONFIG_FILE}"
            exit 1
        fi
    else
        warn "Config already exists, not overwriting: ${INSTALL_DIR}/${CONFIG_FILE}"
        # Still install the new one as example
        cp "${SCRIPT_DIR}/${CONFIG_FILE}" "${INSTALL_DIR}/${CONFIG_FILE}.new"
        chmod 600 "${INSTALL_DIR}/${CONFIG_FILE}.new"
        info "New config saved as ${INSTALL_DIR}/${CONFIG_FILE}.new for reference"
    fi
    
    # Copy example per-user configs
    if [[ -d "${SCRIPT_DIR}/examples/users" ]]; then
        for example_file in "${SCRIPT_DIR}/examples/users"/*.example; do
            if [[ -f "$example_file" ]]; then
                cp "$example_file" "${INSTALL_DIR}/users/"
                chmod 600 "${INSTALL_DIR}/users/$(basename "$example_file")"
            fi
        done
        success "Installed example per-user configs in ${INSTALL_DIR}/users/"
    fi
    
    # Install systemd service for approval server
    if [[ -f "${SCRIPT_DIR}/pam-ssh-2fa-server.service" ]]; then
        cp "${SCRIPT_DIR}/pam-ssh-2fa-server.service" /etc/systemd/system/
        chmod 644 /etc/systemd/system/pam-ssh-2fa-server.service
        systemctl daemon-reload
        success "Installed systemd service: pam-ssh-2fa-server.service"
        info "To enable link-based auth, run: sudo systemctl enable --now pam-ssh-2fa-server"
    fi
}

# Verify the installation
verify_installation() {
    info "Verifying installation..."
    
    local errors=0
    
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
    
    if ! $found_pam_python; then
        error "pam_python.so not found!"
        ((errors++))
    fi
    
    # Check module file
    if [[ -f "${INSTALL_DIR}/${MODULE_FILE}" ]]; then
        success "Module file installed"
    else
        error "Module file missing!"
        ((errors++))
    fi
    
    # Check config file
    if [[ -f "${INSTALL_DIR}/${CONFIG_FILE}" ]]; then
        success "Config file installed"
    else
        error "Config file missing!"
        ((errors++))
    fi
    
    # Check apprise
    if python3 -c "import apprise" 2>/dev/null; then
        success "Apprise module available"
    else
        error "Apprise module not importable!"
        ((errors++))
    fi
    
    # Run module self-test
    info "Running module self-test..."
    if python3 "${INSTALL_DIR}/${MODULE_FILE}" --config "${INSTALL_DIR}/${CONFIG_FILE}" 2>/dev/null; then
        success "Module self-test passed"
    else
        warn "Module self-test had issues (may be expected if no notification URL configured)"
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
    echo "2. (OPTIONAL) Enable link-based authentication:"
    echo ""
    echo "   a. Set server URL in ${INSTALL_DIR}/${CONFIG_FILE}:"
    echo "      [server]"
    echo "      port = 9110"
    echo "      url = http://YOUR_SERVER_IP:9110"
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
    echo "3. Test notifications:"
    echo "   python3 ${INSTALL_DIR}/${MODULE_FILE} --test-notify"
    echo ""
    echo "4. Configure PAM (add to /etc/pam.d/sshd):"
    echo ""
    echo "   After '@include common-auth' or similar, add:"
    echo "   auth required pam_python.so ${INSTALL_DIR}/${MODULE_FILE}"
    echo ""
    echo "5. Configure SSH (/etc/ssh/sshd_config):"
    echo ""
    echo "   Ensure these settings are present:"
    echo "   UsePAM yes"
    echo "   KbdInteractiveAuthentication yes"
    echo "   AuthenticationMethods publickey,keyboard-interactive:pam"
    echo ""
    echo "6. Test with a SEPARATE SSH session before logging out!"
    echo "   Always keep your current session open as backup."
    echo ""
    echo "7. Restart SSH when ready:"
    echo "   systemctl restart sshd"
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
    read -p "Continue? [y/N] " -n 1 -r
    echo ""
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        info "Uninstall cancelled"
        exit 0
    fi
    
    # Stop and disable approval server if running
    if systemctl is-active --quiet pam-ssh-2fa-server 2>/dev/null; then
        info "Stopping approval server..."
        systemctl stop pam-ssh-2fa-server
        success "Stopped approval server"
    fi
    
    if systemctl is-enabled --quiet pam-ssh-2fa-server 2>/dev/null; then
        info "Disabling approval server..."
        systemctl disable pam-ssh-2fa-server
        success "Disabled approval server"
    fi
    
    # Remove systemd service file
    if [[ -f "/etc/systemd/system/pam-ssh-2fa-server.service" ]]; then
        rm -f /etc/systemd/system/pam-ssh-2fa-server.service
        systemctl daemon-reload
        success "Removed systemd service"
    fi
    
    info "Removing module files..."
    rm -f "${INSTALL_DIR}/${MODULE_FILE}"
    rm -f "${INSTALL_DIR}/approval_server.py"
    rm -f "${INSTALL_DIR}/test_notify.py"
    rm -f "${INSTALL_DIR}/cleanup_codes.py"
    success "Removed module files"
    
    # Ask about config
    if [[ -f "${INSTALL_DIR}/${CONFIG_FILE}" ]]; then
        read -p "Remove config file and per-user configs too? [y/N] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -f "${INSTALL_DIR}/${CONFIG_FILE}"
            rm -f "${INSTALL_DIR}/${CONFIG_FILE}.new"
            rm -rf "${INSTALL_DIR}/users"
            success "Removed config files"
        else
            info "Config files preserved"
        fi
    fi
    
    # Remove directory if empty
    if [[ -d "$INSTALL_DIR" ]] && [[ -z "$(ls -A "$INSTALL_DIR")" ]]; then
        rmdir "$INSTALL_DIR"
        success "Removed empty install directory"
    fi
    
    # Clean up runtime files
    rm -rf "$STORAGE_DIR"
    success "Removed runtime storage"
    
    echo ""
    echo "============================================================================="
    echo "Uninstall complete."
    echo ""
    echo "IMPORTANT: You must manually:"
    echo "1. Remove the PAM line from /etc/pam.d/sshd"
    echo "2. Revert changes to /etc/ssh/sshd_config"
    echo "3. Restart SSH: systemctl restart sshd"
    echo "============================================================================="
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
    (none)          Interactive installation
    --uninstall     Remove the PAM 2FA module
    --help, -h      Show this help message

DESCRIPTION:
    Installs a PAM module that provides two-factor authentication for SSH
    by sending one-time codes via push notification (using Apprise).

REQUIREMENTS:
    - Debian 11+ or Ubuntu 20.04+
    - Root privileges
    - Internet connection

INSTALLED FILES:
    ${INSTALL_DIR}/${MODULE_FILE}   - The PAM module
    ${INSTALL_DIR}/${CONFIG_FILE}   - Configuration file
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
    # Parse arguments
    case "${1:-}" in
        --help|-h)
            show_help
            exit 0
            ;;
        --uninstall)
            check_root
            uninstall
            exit 0
            ;;
        "")
            # Default: install
            ;;
        *)
            error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
    
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
    read -p "Continue with installation? [Y/n] " -n 1 -r
    echo ""
    
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        info "Installation cancelled"
        exit 0
    fi
    
    echo ""
    
    # Installation steps
    install_packages
    echo ""
    
    create_directories
    echo ""
    
    install_module
    echo ""
    
    verify_installation
    echo ""
    
    show_instructions
}

# Run main function
main "$@"
