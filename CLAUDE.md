# CLAUDE.md - PAM SSH 2FA Project Guide

## Project Overview

This is a PAM (Pluggable Authentication Module) for SSH two-factor authentication using push notifications. Users receive a code or approval link via services like ntfy, Pushover, or Telegram.

**Key Features:**
- OTP codes (6 digits by default, configurable 6-10) sent via push notification (80+ services via Apprise)
- Link-based approval (open a link and explicitly confirm, no code typing)
- Per-user configuration (different services/methods per user)
- Configurable bypass for users/networks
- Rate limiting (per-user, per-source-address, and concurrent-request caps)

## File Structure

```
pam-ssh-2fa/
|-- pam_ssh_2fa.py          # Main PAM module (~2600 lines)
|-- approval_server.py      # HTTP server for link-based auth (~950 lines)
|-- config.ini              # Global configuration (~400 lines)
|-- install.sh              # Installation script (~980 lines)
|-- test_notify.py          # Notification testing utility (~320 lines)
|-- cleanup_codes.py        # Expired code cleanup utility (~210 lines)
|-- pam-ssh-2fa-server.service  # Systemd service for approval server
|-- README.md               # User documentation (~740 lines)
|-- CLAUDE.md               # This file
|-- MODERNIZATION_PLAN.md   # Security audit and roadmap
|-- test_*.py               # Automated test suite (unittest)
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
| `RateLimiter` | Per-user/per-source/concurrency request limits |

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
| `/approve/<token>` | GET | Display request details and confirmation form |
| `/approve/<token>` | POST | Approve after explicit confirmation |
| `/health` | GET | Health check (returns JSON) |
| `/` | GET | Info page |

### install.sh - Installation Script

Bash installer. Deliberately does **not** touch `/etc/pam.d/sshd` or
`/etc/ssh/sshd_config` -- those edits are printed as manual instructions
(see the file's top-of-file comment for why: the correct PAM stack
position hasn't been validated across every supported distro release
yet).

**Flags:** `--dry-run` (preview, no root needed), `--yes` (non-interactive,
never implies deleting config), `--enable-link-approval` /
`--no-link-approval`, `--uninstall`.

**Key mechanisms:**
- `run()` -- every mutating command goes through this so `--dry-run` is
  a complete preview, not a partial one. New install/uninstall steps
  must use it (or `install_tracked_file`/`create_tracked_dir`, which
  already wrap it).
- Installation manifest (`${INSTALL_DIR}/.install-manifest`) -- records
  every directory/file created and every backup made before an
  overwrite (`DIR|path`, `FILE|path`, `BACKUP|original|backup`).
  `--uninstall` reads this to remove exactly what was installed
  (`uninstall_from_manifest`); if no manifest exists (pre-existing
  install), it falls back to hardcoded default paths
  (`uninstall_legacy_fallback`).
- `verify_installation()` returns an error *count*, not a boolean --
  the call site branches explicitly on it (`if verify_installation;
  then ... else ... fi`) rather than letting `set -e` handle a nonzero
  return. Do not use `((errors++))` in this codebase: under `set -e`,
  it returns the pre-increment value as its exit status, so the first
  increment from 0 evaluates to a "failed" command and silently kills
  the whole script. Use `errors=$((errors + 1))` instead.

## Configuration

### Global Config: /etc/pam-ssh-2fa/config.ini

**Sections:**

| Section | Purpose |
|---------|---------|
| `[general]` | Debug mode, log file path |
| `[codes]` | Code length, timeout, max attempts, storage |
| `[notifications]` | Apprise URLs, message templates |
| `[server]` | Approval server port, URL, TLS |
| `[messages]` | User-facing prompts and messages |
| `[bypass]` | Users and networks to skip 2FA |
| `[ratelimit]` | Per-user/per-source/concurrency request limits |
| `[users]` | Default auth method, unconfigured user handling |

**Key Settings:**

```ini
[general]
debug = false
log_file = /var/log/pam-ssh-2fa.log

[codes]
length = 6                              # OTP code length (valid range 6-10)
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
                                         # http:// rejected for public hosts by default
allow_insecure_http = false             # Override the http:// rejection above
tls_cert =                              # PEM cert; native HTTPS if set with tls_key
tls_key =                               # PEM key matching tls_cert
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

[ratelimit]
window = 300                            # Sliding window in seconds
max_per_user = 5                        # Max new requests per user per window
max_per_rhost = 15                      # Max new requests per source address per window
max_concurrent_per_user = 3             # Max outstanding requests per user

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
5. `RateLimiter` checks per-user/per-source/concurrency limits
6. `CodeManager.generate()` creates a code (6 digits by default) bound to a fresh per-attempt request ID, saves to file
7. `NotificationSender.send()` pushes code via Apprise
8. User prompted for code
9. `CodeManager.validate()` checks code against that request ID (constant-time comparison)
10. Return PAM_SUCCESS or PAM_AUTH_ERR

### Link-Based (auth_method = link)

1. User connects via SSH with key
2. `pam_sm_authenticate()` called
3. `RateLimiter` checks per-user/per-source/concurrency limits
4. `ApprovalManager.create_approval()` rejects an insecure `http://` URL
   to a public host (see `_check_server_url_security()`), then
   generates a token and saves the request file
5. `NotificationSender.send()` pushes link via Apprise
6. User shown "Waiting for approval..."
7. PAM polls `ApprovalManager.is_approved()` every second
8. User opens the link and taps the explicit approval button
9. `approval_server.py` marks request approved
10. PAM sees approval, returns PAM_SUCCESS

### Combined (auth_method = both)

1. Both code and approval request created
2. Notification contains both code and link
3. User prompted: "Enter code OR press Enter after clicking link"
4. If user enters code: validate with CodeManager
5. If user presses Enter: check if link was clicked
6. Either method grants access

### Validated: PAM Stack Composition

Per MODERNIZATION_PLAN.md's "the documented PAM stack may not match the
promised flow" finding, this was tested empirically with `pamtester`
against a real PAM stack (not just reasoned about) on Debian 13, and the
results are encoded as regression tests in
`test_pam_stack_integration.py`. Two findings drove real fixes:

1. **The `/etc/pam.d/sshd` auth stack must NOT include `@include
   common-auth`.** SSH key verification already happened at the OpenSSH
   layer before this stack is ever invoked (it only runs for the
   keyboard-interactive step tagged `:pam`). Confirmed with pamtester:
   adding this module AFTER `@include common-auth` makes `pam_unix.so`
   prompt for and validate a Unix password FIRST -- if it's wrong,
   `pam_deny.so requisite` aborts the whole stack immediately and this
   module never even runs. For a password-locked (SSH-key-only) account
   -- this module's primary target -- login becomes impossible, full
   stop, regardless of 2FA. Fix: `examples/pam.d-sshd.example` now says
   to REPLACE `@include common-auth`, not follow it.

2. **`BypassChecker`-triggered bypass must return `PAM_SUCCESS`, not
   `PAM_IGNORE`.** `PAM_IGNORE` tells libpam to disregard this module's
   result and let some other module's result decide overall success --
   but once fix #1 is applied, this module is the ONLY auth line in the
   stack, so there's nothing else to decide it. Confirmed with
   pamtester: a bypassed user got "Permission denied" even though
   `should_bypass()` correctly logged the bypass. This was a real bug in
   `pam_sm_authenticate()` (Step 3), not just a docs issue -- fixed by
   returning `PAM_SUCCESS` instead.

If you ever touch `BypassChecker`, the bypass/unconfigured-user return
paths in `pam_sm_authenticate()`, or the recommended PAM stack in
`examples/pam.d-sshd.example`, run `test_pam_stack_integration.py`
(requires root + `pamtester`; skips itself otherwise) -- it drives the
actual `pam_python.so` entry point, not just the Python API, and is the
only test in this repo that would catch a regression of either finding
above.

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

1. Add default value to `DEFAULTS` dict in pam_ssh_2fa.py (~line 120)
2. Add to `section_mapping` in `Config._parse_config_file()` (~line 438)
3. If per-user configurable, add to the `user_only` mapping (~line 489)
4. If numeric and security-relevant, validate with `_bounded_int(min, max)`
   as the converter instead of the bare `int`/`str` type (see `code_length`
   or the `ratelimit_*` settings for examples)
5. Add to config.ini with comments
6. Update README.md Configuration Reference section
7. Update this CLAUDE.md file

### Adding a New Notification Template Variable

1. Add to `template_vars` dict in `NotificationSender.send()` (~line 1459)
2. Document in config.ini comments
3. Update README.md template variables list

### Adding a New Auth Method

1. Add to validation in `pam_sm_authenticate()` (~line 2048)
2. Add handling logic in the Step 7 section (~line 2215)
3. Update config.ini comments
4. Update README.md Authentication Methods section
5. Update per-user example configs

### Adding a New Bypass Condition

1. Add check method to `BypassChecker` class (~line 1578)
2. Call from `should_bypass()` method
3. Add config option following "Adding a New Config Option"
4. Do not change the bypass return code in `pam_sm_authenticate()` away
   from `PAM_SUCCESS` -- see "Validated: PAM Stack Composition" above
   for why `PAM_IGNORE` looks equally plausible but silently denies
   instead of granting access. Run `test_pam_stack_integration.py`
   after any change here.

### Adding a New Rate Limit

1. Add default value to `DEFAULTS` in pam_ssh_2fa.py, and a `_bounded_int`
   converter entry in `section_mapping` (see the existing `ratelimit_*`
   settings for the pattern)
2. Call `RateLimiter.check_window()` (sliding window) or
   `count_active()` (concurrency cap) from Step 4.5 of
   `pam_sm_authenticate()`, before any code/approval state is created
3. Add to config.ini `[ratelimit]` with comments
4. Update README.md Configuration Reference section

### Modifying Code Length

1. Update `DEFAULTS["code_length"]` in pam_ssh_2fa.py
2. Update `CodeManager.__init__()` default parameter
3. Update `CODE_LENGTH_MIN`/`CODE_LENGTH_MAX` if the valid range itself
   should change (these bound what config.ini can set -- see `_bounded_int`)
4. Update config.ini `length = X`
5. Update all documentation references (README, config comments)
6. Update test code examples (test_notify.py, docstrings)

### Changing Approval Server URL/TLS Rules

The HTTPS requirement is split across two files -- keep both in sync:

1. `ApprovalManager._check_server_url_security()` and
   `_is_non_global_host()` in pam_ssh_2fa.py decide whether a
   `server_url` is allowed to build a link at all (this is the
   enforcement point -- it runs in the privileged PAM auth path)
2. `ApprovalServer.__init__()` in approval_server.py wraps the listener
   socket in TLS when `tls_cert`/`tls_key` are both set (this is where
   the server actually terminates HTTPS, if not using a reverse proxy)
3. Both read `[server]` settings independently (`pam_ssh_2fa.Config`
   and `approval_server.ServerConfig` are separate parsers) -- a new
   `[server]` option needs a mapping entry added to both if both
   processes need to see it

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

Manual, ad-hoc check against your own real `/etc/pam.d/sshd` -- only run
this on a box where you understand and accept the risk of testing your
actual login stack:

```bash
sudo apt install pamtester
sudo pamtester sshd yourusername authenticate
```

For an automated, repeatable check that doesn't touch a real `sshd` PAM
service at all (it creates and tears down its own isolated PAM service
file), see `test_pam_stack_integration.py` under Testing below.

### Test Approval Server

```bash
# Check service status
systemctl status pam-ssh-2fa-server

# Test health endpoint
curl http://localhost:9110/health

# Check logs
tail -f /var/log/pam-ssh-2fa-server.log
```

### Automated Test Suite (unittest)

```bash
python3 -m unittest discover -p "test_*.py"
```

Most `test_*.py` files are pure-Python unit tests with no special
requirements. One exception: `test_pam_stack_integration.py` drives the
real `pam_python.so` entry point via `pamtester` against an isolated PAM
service it creates and tears down itself (never the real `sshd`
service). It requires root and `pamtester`, and skips itself cleanly
(with a stated reason) if either is missing, or if a real install
already exists at `/etc/pam-ssh-2fa` (so it never touches a genuine
deployment). This is the regression suite for the PAM stack findings in
"Validated: PAM Stack Composition" above.

### Self-Test Mode

```bash
# Run built-in tests
python3 /etc/pam-ssh-2fa/pam_ssh_2fa.py
```

## Installation Paths

| File | Installed Location |
|------|-------------------|
| pam_ssh_2fa.py | /etc/pam-ssh-2fa/pam_ssh_2fa.py |
| approval_server.py | /etc/pam-ssh-2fa/approval_server.py (only if `--enable-link-approval`) |
| test_notify.py | /etc/pam-ssh-2fa/test_notify.py |
| cleanup_codes.py | /etc/pam-ssh-2fa/cleanup_codes.py |
| config.ini | /etc/pam-ssh-2fa/config.ini |
| Per-user configs | /etc/pam-ssh-2fa/users/*.conf |
| Installation manifest | /etc/pam-ssh-2fa/.install-manifest |
| Systemd service | /etc/systemd/system/pam-ssh-2fa-server.service (only if `--enable-link-approval`) |
| Code storage | /var/run/pam-ssh-2fa/ |
| Approval storage | /var/run/pam-ssh-2fa/approvals/ |
| Rate-limit counters | /var/run/pam-ssh-2fa/ratelimit/ |
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
| 0 | PAM_SUCCESS | Authentication successful, OR 2FA bypassed (bypass list, or allow_unconfigured_users=true) |
| 7 | PAM_AUTH_ERR | Authentication failed |
| 9 | PAM_AUTHINFO_UNAVAIL | Cannot obtain auth info (notification failed) |
| 10 | PAM_USER_UNKNOWN | Could not get username from PAM |
| 11 | PAM_MAXTRIES | Rate limit exceeded (per-user, per-source, or too many concurrent requests) |

Bypass intentionally returns `PAM_SUCCESS`, not `PAM_IGNORE`. `PAM_IGNORE` (25, still defined as a constant) tells the PAM framework to disregard this module's result and defer to some OTHER module in the stack -- but the recommended stack (see `examples/pam.d-sshd.example`) has no other auth module, since SSH key verification already happened at the OpenSSH layer. Verified with `pamtester`: `PAM_IGNORE` there resolved to an overall **deny**, not a pass-through. See `test_pam_stack_integration.py`.

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

**/etc/pam.d/sshd** -- REPLACE `@include common-auth`, don't add after it
(see "Validated: PAM Stack Composition" below for why this is load-bearing):
```
auth required pam_python.so /etc/pam-ssh-2fa/pam_ssh_2fa.py
```

**/etc/ssh/sshd_config:**
```
UsePAM yes
KbdInteractiveAuthentication yes
AuthenticationMethods publickey,keyboard-interactive:pam
```

Verify with `sshd -t` (syntax) AND `sshd -T | grep -iE '^(usepam|kbdinteractiveauthentication|authenticationmethods)'`
(effective/resolved config -- `sshd -t` alone won't catch a Match block
overriding these elsewhere in the file).

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
