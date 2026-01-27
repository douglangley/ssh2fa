# CLAUDE.md - PAM SSH 2FA Project Guide

## Project Overview

This is a PAM (Pluggable Authentication Module) for SSH two-factor authentication using push notifications. Users receive a code or approval link via services like ntfy, Pushover, or Telegram.

**Key Features:**
- 4-digit OTP codes sent via push notification (80+ services via Apprise)
- Link-based approval (click to approve, no typing)
- Per-user configuration (different services/methods per user)
- Configurable bypass for users/networks

## File Structure

```
pam-ssh-2fa/
|-- pam_ssh_2fa.py          # Main PAM module (2081 lines)
|-- approval_server.py      # HTTP server for link-based auth (696 lines)
|-- config.ini              # Global configuration (338 lines)
|-- install.sh              # Installation script (599 lines)
|-- test_notify.py          # Notification testing utility (319 lines)
|-- cleanup_codes.py        # Expired code cleanup utility (204 lines)
|-- pam-ssh-2fa-server.service  # Systemd service for approval server
|-- README.md               # User documentation (672 lines)
|-- CLAUDE.md               # This file
+-- examples/
    |-- pam.d-sshd.example      # PAM configuration examples
    |-- sshd_config.example     # SSH daemon configuration examples
    +-- users/
        |-- doug.conf.example   # Per-user config (Pushover + link)
        +-- ben.conf.example    # Per-user config (ntfy + both)
```

## Code Architecture

### pam_ssh_2fa.py - Main PAM Module

**Classes:**

| Class | Purpose |
|-------|---------|
| `PAMLogger` | Dual logging to file and syslog |
| `Config` | INI file parsing with per-user override support |
| `CodeManager` | Generate, store, validate OTP codes |
| `ApprovalManager` | Create/check link-based approval requests |
| `NotificationSender` | Send notifications via Apprise |
| `BypassChecker` | Determine if 2FA should be skipped |

**PAM Entry Points:**

| Function | Purpose |
|----------|---------|
| `pam_sm_authenticate` | Main authentication logic |
| `pam_sm_setcred` | Credential management (returns SUCCESS) |
| `pam_sm_acct_mgmt` | Account management (returns SUCCESS) |
| `pam_sm_open_session` | Session open (returns SUCCESS) |
| `pam_sm_close_session` | Session close (returns SUCCESS) |
| `pam_sm_chauthtok` | Password change (returns SUCCESS) |

**Helper Functions:**

| Function | Purpose |
|----------|---------|
| `pam_prompt()` | Prompt user for input |
| `pam_info()` | Display info message to user |
| `pam_error()` | Display error message to user |

### approval_server.py - Link-Based Auth Server

**Classes:**

| Class | Purpose |
|-------|---------|
| `ServerConfig` | Load server settings from config.ini |
| `ApprovalManager` | Read/write approval request files |
| `ApprovalRequestHandler` | Handle HTTP requests |
| `ApprovalServer` | HTTP server extending HTTPServer |
| `CleanupThread` | Background thread to remove expired approvals |

**Endpoints:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/approve/<token>` | GET | Approve an authentication request |
| `/health` | GET | Health check (returns JSON) |
| `/` | GET | Info page |

## Configuration

### Global Config: /etc/pam-ssh-2fa/config.ini

**Sections:**

| Section | Purpose |
|---------|---------|
| `[general]` | Debug mode, log file path |
| `[codes]` | Code length, timeout, max attempts, storage |
| `[notifications]` | Apprise URLs, message templates |
| `[server]` | Approval server port and URL |
| `[messages]` | User-facing prompts and messages |
| `[bypass]` | Users and networks to skip 2FA |
| `[users]` | Default auth method, unconfigured user handling |

**Key Settings:**

```ini
[general]
debug = false
log_file = /var/log/pam-ssh-2fa.log

[codes]
length = 4                              # OTP code length
timeout = 300                           # Seconds until expiry
max_attempts = 3                        # Failed attempts before lockout
storage_dir = /var/run/pam-ssh-2fa      # Runtime storage

[notifications]
apprise_urls =                          # Comma-separated Apprise URLs
title = SSH Login                       # Notification title
body = Your SSH verification code is: {code}  # Code-only template
body_link = Click to approve: {link}    # Link-only template
body_both = Click: {link} Or code: {code}     # Combined template

[server]
port = 9110                             # Approval server port
url =                                   # Public URL (REQUIRED for link auth)
log_file = /var/log/pam-ssh-2fa-server.log

[messages]
prompt = Enter verification code:
prompt_both = Enter code OR press Enter after clicking link:
success = Verification successful.
failure = Verification failed. Access denied.
expired = Code expired. Please reconnect.

[bypass]
users =                                 # Comma-separated usernames
networks =                              # Comma-separated CIDR ranges

[users]
allow_unconfigured_users = false        # true = bypass, false = deny
auth_method = code                      # code, link, both, none
```

**Template Variables:**
- `{code}` - The OTP code
- `{link}` - The approval link
- `{user}` - Username
- `{host}` - Server hostname
- `{rhost}` - Remote host IP
- `{timeout}` - Timeout in minutes

### Per-User Config: /etc/pam-ssh-2fa/users/<username>.conf

Per-user configs can only override these sections:

```ini
[notifications]
apprise_urls = pover://USERKEY@APPTOKEN
# Optional: title, body, body_link, body_both

[auth]
method = link    # code, link, both, none
```

## Authentication Flow

### Code-Based (auth_method = code)

1. User connects via SSH with key
2. `pam_sm_authenticate()` called
3. `BypassChecker` checks if user/network should skip 2FA
4. `Config` loads user-specific settings
5. `CodeManager.generate()` creates 4-digit code, saves to file
6. `NotificationSender.send()` pushes code via Apprise
7. User prompted for code
8. `CodeManager.validate()` checks code (constant-time comparison)
9. Return PAM_SUCCESS or PAM_AUTH_ERR

### Link-Based (auth_method = link)

1. User connects via SSH with key
2. `pam_sm_authenticate()` called
3. `ApprovalManager.create_approval()` generates token, saves request file
4. `NotificationSender.send()` pushes link via Apprise
5. User shown "Waiting for approval..."
6. PAM polls `ApprovalManager.is_approved()` every second
7. User clicks link on phone
8. `approval_server.py` marks request approved
9. PAM sees approval, returns PAM_SUCCESS

### Combined (auth_method = both)

1. Both code and approval request created
2. Notification contains both code and link
3. User prompted: "Enter code OR press Enter after clicking link"
4. If user enters code: validate with CodeManager
5. If user presses Enter: check if link was clicked
6. Either method grants access

## Code Conventions

### Accessibility
- **No Unicode symbols** - Use ASCII only ([OK], [FAIL], +, -, |)
- All output must work with screen readers
- Use text alternatives for visual indicators

### Documentation
- Every class has a docstring with attributes and usage example
- Every function has a docstring with Args, Returns, Raises
- Section headers use `# ====` comment blocks
- Inline comments explain non-obvious logic

### Error Handling
- Use try/except with specific exceptions
- Log errors before returning failure codes
- Provide user-friendly error messages via `pam_error()`

### Security
- Constant-time comparison for code validation (`secrets.compare_digest`)
- Secure file permissions (0600 for configs, 0700 for storage dirs)
- Token sanitization to prevent directory traversal
- Cryptographically random tokens (`secrets.token_urlsafe`)

### Logging
- Use `PAMLogger` class for consistent formatting
- Log to both file and syslog
- Include user, rhost in log entries
- Debug logging controlled by config

## Common Tasks

### Adding a New Config Option

1. Add default value to `DEFAULTS` dict in pam_ssh_2fa.py (~line 109)
2. Add to `section_mapping` in `Config._parse_config_file()` (~line 370)
3. If per-user configurable, add to user_only mapping (~line 405)
4. Add to config.ini with comments
5. Update README.md Configuration Reference section
6. Update this CLAUDE.md file

### Adding a New Notification Template Variable

1. Add to `template_vars` dict in `NotificationSender.send()` (~line 1235)
2. Document in config.ini comments
3. Update README.md template variables list

### Adding a New Auth Method

1. Add to validation in `pam_sm_authenticate()` (~line 1633)
2. Add handling logic in Step 7 section (~line 1738)
3. Update config.ini comments
4. Update README.md Authentication Methods section
5. Update per-user example configs

### Adding a New Bypass Condition

1. Add check method to `BypassChecker` class (~line 1323)
2. Call from `should_bypass()` method
3. Add config option following "Adding a New Config Option"

### Modifying Code Length

1. Update `DEFAULTS["code_length"]` in pam_ssh_2fa.py
2. Update `CodeManager.__init__()` default parameter
3. Update config.ini `length = X`
4. Update all documentation references (README, config comments)
5. Update test code examples (test_notify.py, docstrings)

## Testing

### Test Notification Delivery

```bash
# Test with global config
python3 /etc/pam-ssh-2fa/pam_ssh_2fa.py --test-notify

# Test specific user
python3 /etc/pam-ssh-2fa/test_notify.py --user doug

# Test specific URL
python3 /etc/pam-ssh-2fa/test_notify.py --url "ntfy://ntfy.sh/test"
```

### Test PAM Module Without SSH

```bash
sudo apt install pamtester
sudo pamtester sshd yourusername authenticate
```

### Test Approval Server

```bash
# Check service status
systemctl status pam-ssh-2fa-server

# Test health endpoint
curl http://localhost:9110/health

# Check logs
tail -f /var/log/pam-ssh-2fa-server.log
```

### Self-Test Mode

```bash
# Run built-in tests
python3 /etc/pam-ssh-2fa/pam_ssh_2fa.py
```

## Installation Paths

| File | Installed Location |
|------|-------------------|
| pam_ssh_2fa.py | /etc/pam-ssh-2fa/pam_ssh_2fa.py |
| approval_server.py | /etc/pam-ssh-2fa/approval_server.py |
| config.ini | /etc/pam-ssh-2fa/config.ini |
| Per-user configs | /etc/pam-ssh-2fa/users/*.conf |
| Systemd service | /etc/systemd/system/pam-ssh-2fa-server.service |
| Code storage | /var/run/pam-ssh-2fa/ |
| Approval storage | /var/run/pam-ssh-2fa/approvals/ |
| PAM module log | /var/log/pam-ssh-2fa.log |
| Server log | /var/log/pam-ssh-2fa-server.log |

## Dependencies

**System Packages:**
- python3 (3.8+)
- libpam-python

**Python Packages:**
- apprise (for notifications)

**Install:**
```bash
sudo apt install libpam-python
pip3 install apprise --break-system-packages
```

## PAM Return Codes

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | PAM_SUCCESS | Authentication successful |
| 7 | PAM_AUTH_ERR | Authentication failed |
| 9 | PAM_AUTHINFO_UNAVAIL | Cannot obtain auth info (notification failed) |
| 25 | PAM_IGNORE | Skip this module (bypass condition met) |

## File Permissions

| Path | Mode | Owner |
|------|------|-------|
| /etc/pam-ssh-2fa/ | 0750 | root:root |
| /etc/pam-ssh-2fa/config.ini | 0600 | root:root |
| /etc/pam-ssh-2fa/users/*.conf | 0600 | root:root |
| /var/run/pam-ssh-2fa/ | 0700 | root:root |
| /var/run/pam-ssh-2fa/approvals/ | 0700 | root:root |

## Quick Reference

### Apprise URL Formats

```
ntfy://ntfy.sh/topic              # ntfy (free)
pover://userkey@apptoken          # Pushover ($5)
tgram://bottoken/chatid           # Telegram (free)
slack://toka/tokb/tokc/#channel   # Slack
discord://webhookid/webhooktoken  # Discord
mailto://user:pass@smtp/to=addr   # Email
```

### SSH/PAM Configuration

**/etc/pam.d/sshd:**
```
auth required pam_python.so /etc/pam-ssh-2fa/pam_ssh_2fa.py
```

**/etc/ssh/sshd_config:**
```
UsePAM yes
KbdInteractiveAuthentication yes
AuthenticationMethods publickey,keyboard-interactive:pam
```

### Systemd Commands

```bash
# Approval server
sudo systemctl enable pam-ssh-2fa-server
sudo systemctl start pam-ssh-2fa-server
sudo systemctl status pam-ssh-2fa-server
sudo journalctl -u pam-ssh-2fa-server -f

# SSH daemon
sudo systemctl restart sshd
```
