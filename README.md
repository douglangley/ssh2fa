# PAM SSH 2FA - Push Notification One-Time Password Authentication

A PAM (Pluggable Authentication Module) for SSH that sends one-time verification codes or approval links via push notification services like ntfy, Pushover, Telegram, and 80+ others.

## How It Works

### Code-Based Authentication (Default)

1. User connects via SSH (with SSH key authentication)
2. PAM module generates a random 4-digit code
3. Code is sent to user's phone/device via push notification
4. User enters the code at the SSH prompt
5. Access is granted if the code is correct

```
+----------+     SSH Key     +----------+     Push     +----------+
|  User    | --------------> |  Server  | -----------> |  Phone   |
|          |                 |  (PAM)   |    "1234"    |          |
|          | <-------------- |          |              |          |
|          |  Enter Code:    |          |              |          |
|          | --------------> |          |              |          |
|          |     "1234"      |          |              |          |
|          | <-------------- |          |              |          |
|          |   Access OK     |          |              |          |
+----------+                 +----------+              +----------+
```

### Link-Based Authentication (Optional)

1. User connects via SSH (with SSH key authentication)
2. PAM module creates an approval request
3. Approval link is sent to user's phone via push notification
4. User opens the link and taps the approval button on their phone
5. Access is granted without typing a code

```
+----------+     SSH Key     +----------+     Push     +----------+
|  User    | --------------> |  Server  | -----------> |  Phone   |
|          |                 |  (PAM)   |   [Link]     |          |
|          | <-------------- |          |              |          |
|          |   Waiting...    |          |              |          |
|          |                 |          | <----------- |          |
|          |                 |          |  Click Link  |          |
|          | <-------------- |          |              |          |
|          |   Access OK     |          |              |          |
+----------+                 +----------+              +----------+
```

## Features

- **Push-based codes**: No app to open, code comes to you
- **Link-based approval**: Click a link instead of typing a code
- **Multiple notification services**: ntfy, Pushover, Telegram, Slack, Discord, email, and 80+ more
- **Per-user configuration**: Each user can have different auth method and notification service
- **Redundancy support**: Send to multiple services simultaneously
- **Configurable timeouts**: Codes/links expire after 5 minutes by default
- **Bypass options**: Skip 2FA for specific users or networks
- **Full logging**: Track all authentication attempts
- **Well-documented code**: Easy to audit, modify, and debug

## Authentication Methods

This module supports four authentication methods, configurable per-user:

| Method | Description | User Experience |
|--------|-------------|-----------------|
| `code` | Send a 4-digit code | User types the code |
| `link` | Send an approval link | User opens the link and confirms on phone |
| `both` | Send code AND link | User can type code OR click link then press Enter |
| `none` | Skip 2FA | No verification required |

### Link-Based Authentication

For users who prefer not to type codes, link-based auth lets them simply click a link in the notification. This requires running the approval server.

**Setup:**

1. Configure the server URL in config.ini:
   ```ini
   [server]
   port = 9110
   url = http://your-server.example.com:9110
   ```

2. Open the firewall port:
   ```bash
   sudo ufw allow 9110/tcp
   ```

3. Start the approval server:
   ```bash
   sudo systemctl enable --now pam-ssh-2fa-server
   ```

4. Set per-user auth method:
   ```ini
   # In /etc/pam-ssh-2fa/users/doug.conf
   [auth]
   method = link
   ```

## Requirements

- Debian 11+ or Ubuntu 20.04+
- Python 3.8+
- SSH key authentication configured
- A push notification service (ntfy.sh is free and easy)

## Quick Start

### 1. Install

```bash
git clone <this-repo> pam-ssh-2fa
cd pam-ssh-2fa
sudo ./install.sh
```

### 2. Configure Notifications

Edit `/etc/pam-ssh-2fa/config.ini` and add your notification URL:

```ini
[notifications]
# Free option - ntfy.sh (use a random topic name)
apprise_urls = ntfy://ntfy.sh/my-secret-ssh-codes-abc123xyz
```

### 3. Test Notifications

```bash
sudo python3 /etc/pam-ssh-2fa/pam_ssh_2fa.py --test-notify
```

Check your phone - you should receive a test code!

### 4. Configure PAM

Add to `/etc/pam.d/sshd` (after `@include common-auth`):

```
auth required pam_python.so /etc/pam-ssh-2fa/pam_ssh_2fa.py
```

### 5. Configure SSH

Edit `/etc/ssh/sshd_config`:

```
UsePAM yes
KbdInteractiveAuthentication yes
AuthenticationMethods publickey,keyboard-interactive:pam
```

### 6. Test (Keep Your Current Session Open!)

```bash
# In a NEW terminal, try connecting
ssh user@your-server

# You should:
# 1. Authenticate with your SSH key
# 2. Receive a push notification with a code
# 3. Be prompted to enter the code
```

### 7. Restart SSH

Only after testing works:

```bash
sudo systemctl restart sshd
```

## Setting Up Link-Based Authentication

Link-based auth lets users click a link instead of typing a code. This requires the approval server.

### 1. Configure Server URL

Edit `/etc/pam-ssh-2fa/config.ini`:

```ini
[server]
port = 9110
url = http://YOUR_PUBLIC_IP_OR_HOSTNAME:9110
```

The URL must be reachable from the user's phone. Options:
- Public IP: `http://203.0.113.50:9110`
- Public hostname: `http://ssh.example.com:9110`
- Tailscale: `http://myserver.tailnet.ts.net:9110`

### 2. Open Firewall

```bash
# UFW
sudo ufw allow 9110/tcp

# firewalld
sudo firewall-cmd --permanent --add-port=9110/tcp
sudo firewall-cmd --reload

# iptables
sudo iptables -A INPUT -p tcp --dport 9110 -j ACCEPT
```

### 3. Start Approval Server

```bash
sudo systemctl enable --now pam-ssh-2fa-server
```

### 4. Verify Server is Running

```bash
# Check status
systemctl status pam-ssh-2fa-server

# Test health endpoint (from server)
curl http://localhost:9110/health

# Test from external (from your phone's browser or another machine)
curl http://YOUR_SERVER:9110/health
```

### 5. Configure Users for Link Auth

Create per-user config files:

```ini
# /etc/pam-ssh-2fa/users/doug.conf
[notifications]
apprise_urls = pover://USERKEY@APPTOKEN

[auth]
method = link
```

Or set as default for all users:

```ini
# In /etc/pam-ssh-2fa/config.ini
[users]
auth_method = link
```

### 6. Test Link Authentication

```bash
# Open a new SSH connection
ssh user@your-server

# You should see: "Approval link sent to your device. Waiting for approval..."
# Check your phone for the notification
# Open the link and tap the approval button
# SSH session should grant access
```

## Notification Services

This module uses [Apprise](https://github.com/caronc/apprise) for notifications, which supports 80+ services. Here are common examples:

### Per-User Configuration

Different users can receive codes via different services and use different auth methods. Create per-user config files:

```
/etc/pam-ssh-2fa/users/doug.conf    # Doug uses Pushover with link auth
/etc/pam-ssh-2fa/users/ben.conf     # Ben uses ntfy with both options
```

Example:

```ini
# /etc/pam-ssh-2fa/users/doug.conf
[notifications]
apprise_urls = pover://DOUG_USER_KEY@APP_TOKEN

[auth]
method = link
```

See the Configuration Reference section for all per-user options.

Users without a personal config file use the global settings from config.ini.

### ntfy (Recommended for Testing)

Free, open-source, works immediately with no account:

```ini
apprise_urls = ntfy://ntfy.sh/your-random-topic-name
```

**Security note**: Anyone who knows your topic name can subscribe. Use a long random string, or self-host ntfy for private use.

### Pushover ($5 one-time)

Reliable, full-featured:

```ini
apprise_urls = pover://YOUR_USER_KEY@YOUR_APP_TOKEN
```

Get credentials at https://pushover.net

### Telegram

Free, requires creating a bot:

```ini
apprise_urls = tgram://BOT_TOKEN/CHAT_ID
```

### Multiple Services (Redundancy)

```ini
apprise_urls = ntfy://ntfy.sh/my-topic, pover://user@token
```

### Full List

See the [Apprise Wiki](https://github.com/caronc/apprise/wiki) for all supported services.

## Configuration Reference

All settings are in `/etc/pam-ssh-2fa/config.ini`:

### [general]

| Setting | Default | Description |
|---------|---------|-------------|
| `debug` | `false` | Enable verbose logging |
| `log_file` | `/var/log/pam-ssh-2fa.log` | Log file location |

### [codes]

| Setting | Default | Description |
|---------|---------|-------------|
| `length` | `4` | Number of digits in code |
| `timeout` | `300` | Seconds until code/link expires |
| `max_attempts` | `3` | Failed attempts before lockout |
| `storage_dir` | `/var/run/pam-ssh-2fa` | Temporary code storage |

### [notifications]

| Setting | Default | Description |
|---------|---------|-------------|
| `apprise_urls` | (empty) | Comma-separated notification URLs |
| `title` | `SSH Login` | Notification title |
| `body` | (template) | Body template for code-only auth |
| `body_link` | (template) | Body template for link-only auth |
| `body_both` | (template) | Body template for code + link auth |

Template variables: `{code}`, `{link}`, `{user}`, `{host}`, `{rhost}`, `{timeout}`

### [messages]

| Setting | Default | Description |
|---------|---------|-------------|
| `prompt` | `Enter verification code: ` | Prompt for code-only auth |
| `prompt_both` | `Enter code OR press Enter after clicking link: ` | Prompt for both auth |
| `success` | `Verification successful.` | Success message |
| `failure` | `Verification failed.` | Failure message |
| `expired` | `Code expired...` | Expiration message |

### [bypass]

| Setting | Default | Description |
|---------|---------|-------------|
| `users` | (empty) | Comma-separated usernames to skip 2FA |
| `networks` | (empty) | Comma-separated CIDR ranges to skip 2FA |

Example:
```ini
[bypass]
users = ansible, backup
networks = 192.168.1.0/24, 10.0.0.0/8
```

### [users]

| Setting | Default | Description |
|---------|---------|-------------|
| `allow_unconfigured_users` | `false` | If true, users without config bypass 2FA; if false, denied |
| `auth_method` | `code` | Default authentication method for all users |

**allow_unconfigured_users** controls what happens when a user has no notification URLs configured:

- `false` (default, recommended): Users without config are denied with an error message
- `true` (use during rollout): Users without config bypass 2FA entirely

**auth_method** sets the default authentication method:

- `code` - Send a 4-digit code, user types it in (default)
- `link` - Send an approval link; the user opens it and explicitly confirms
- `both` - Send both code and link, user can use either
- `none` - Skip 2FA entirely

Example:
```ini
[users]
allow_unconfigured_users = false
auth_method = code
```

### [server]

Required for link-based authentication (`auth_method = link` or `both`):

| Setting | Default | Description |
|---------|---------|-------------|
| `port` | `9110` | Port the approval server listens on |
| `url` | (empty) | Public URL for approval links (REQUIRED for link auth) |
| `log_file` | `/var/log/pam-ssh-2fa-server.log` | Approval server log file |

The `url` must be reachable from the user's phone. Examples:
```ini
[server]
port = 9110
url = http://203.0.113.50:9110           # Public IP
url = http://myserver.example.com:9110   # Public hostname
url = http://myserver.tailnet.ts.net:9110  # Tailscale
```

## Per-User Configuration

Create files in `/etc/pam-ssh-2fa/users/<username>.conf` to customize settings per user.

### Available Per-User Settings

```ini
# /etc/pam-ssh-2fa/users/doug.conf

[notifications]
# User's notification service
apprise_urls = pover://DOUG_USER_KEY@APP_TOKEN

# Optional: custom notification templates
# title = SSH Login for Doug
# body = Your code: {code}
# body_link = Click to approve: {link}
# body_both = Click {link} or enter {code}

[auth]
# Authentication method for this user
# Options: code, link, both, none
method = link
```

### Per-User Auth Methods

| Method | Description | When to Use |
|--------|-------------|-------------|
| `code` | 4-digit code | Default, works everywhere |
| `link` | Click to approve | No typing, best UX |
| `both` | Code or link | Maximum flexibility |
| `none` | Skip 2FA | Emergency/service accounts |

### Example User Configs

**Doug uses Pushover with link-only auth:**
```ini
# /etc/pam-ssh-2fa/users/doug.conf
[notifications]
apprise_urls = pover://USERKEY@APPTOKEN

[auth]
method = link
```

**Ben uses ntfy with both options:**
```ini
# /etc/pam-ssh-2fa/users/ben.conf
[notifications]
apprise_urls = ntfy://ntfy.sh/ben-secret-topic

[auth]
method = both
```

**Service account skips 2FA:**
```ini
# /etc/pam-ssh-2fa/users/ansible.conf
[auth]
method = none
```

## Troubleshooting

### Enable Debug Logging

In `/etc/pam-ssh-2fa/config.ini`:

```ini
[general]
debug = true
```

Then check `/var/log/pam-ssh-2fa.log` and `journalctl -u sshd`.

### Test Without Risking Lockout

Use `pamtester`:

```bash
sudo apt install pamtester
sudo pamtester sshd yourusername authenticate
```

### Common Issues

**No notification received:**
- Check your Apprise URL format
- Run the self-test: `python3 /etc/pam-ssh-2fa/pam_ssh_2fa.py --test-notify`
- Test specific user: `python3 /etc/pam-ssh-2fa/test_notify.py --user doug`
- Check if outbound HTTPS is allowed

**SSH hangs after key auth:**
- PAM module might be failing - check logs
- Ensure `KbdInteractiveAuthentication yes` is set

**Code rejected even when correct:**
- Check server time is accurate (NTP)
- Code may have expired (default 5 minutes)
- Check for trailing spaces when entering code

**Link-based auth not working:**
- Check approval server is running: `systemctl status pam-ssh-2fa-server`
- Verify `[server] url` is set in config.ini
- Ensure firewall allows the port: `sudo ufw allow 9110/tcp`
- Test URL is reachable from phone: `curl http://your-server:9110/health`
- Check approval server log: `tail -f /var/log/pam-ssh-2fa-server.log`

**"2FA not configured for this user" error:**
- User has no per-user config AND no global apprise_urls
- Either create `/etc/pam-ssh-2fa/users/<username>.conf`
- Or set `allow_unconfigured_users = true` (less secure)

**Locked out:**
- Use console/IPMI/serial access
- Edit `/etc/pam.d/sshd` to remove the PAM line
- Restart SSH

### View Logs

```bash
# PAM module log
sudo tail -f /var/log/pam-ssh-2fa.log

# Approval server log (if using link auth)
sudo tail -f /var/log/pam-ssh-2fa-server.log

# System auth log
sudo journalctl -u sshd -f

# PAM debug
sudo grep pam /var/log/auth.log
```

### Test Approval Server

```bash
# Check server is running
systemctl status pam-ssh-2fa-server

# Check health endpoint
curl http://localhost:9110/health

# Check from external (replace with your URL)
curl http://your-server:9110/health
```

## Security Considerations

1. **Keep your notification topic/URL secret** - It's effectively a shared secret
2. **Use HTTPS** for self-hosted notification services
3. **Set appropriate timeouts** - Balance security vs usability (default 5 min)
4. **Monitor logs** for failed authentication attempts
5. **Test thoroughly** before deploying to production
6. **Have a recovery plan** - Console access, bypass user, etc.
7. **Approval server exposure** - The approval server must be internet-accessible for link auth. Consider:
   - Use a firewall to limit source IPs if possible
   - Tokens are cryptographically random and single-use
   - Consider reverse proxy with HTTPS for additional security
8. **Per-user configs** - Store API keys in per-user configs with 0600 permissions
9. **Avoid `allow_unconfigured_users = true`** in production - it bypasses 2FA for unknown users

## File Structure

```
/etc/pam-ssh-2fa/
|-- pam_ssh_2fa.py         # Main PAM module (Python)
|-- approval_server.py     # Approval server for link-based auth
|-- config.ini             # Global configuration file
+-- users/                 # Per-user configuration directory
    |-- doug.conf          # Doug's notification settings
    +-- ben.conf           # Ben's notification settings

/etc/systemd/system/
+-- pam-ssh-2fa-server.service  # Systemd service for approval server

/var/run/pam-ssh-2fa/      # Runtime storage (tmpfs recommended)
|-- code_*.json            # OTP code files
+-- approvals/             # Approval request files
    +-- <token>.json

/var/log/pam-ssh-2fa.log        # PAM module log
/var/log/pam-ssh-2fa-server.log # Approval server log
```

## Uninstalling

```bash
sudo ./install.sh --uninstall
```

Then manually:
1. Remove the PAM line from `/etc/pam.d/sshd`
2. Revert `/etc/ssh/sshd_config` changes
3. Stop approval server: `sudo systemctl disable --now pam-ssh-2fa-server`
4. Restart SSH: `sudo systemctl restart sshd`

## How the Code Works

The module consists of two main components:

### PAM Module (pam_ssh_2fa.py)

A single Python file with these main components:

1. **Config** - Loads settings from INI file, supports per-user overrides
2. **PAMLogger** - Writes to both file and syslog
3. **CodeManager** - Generates, stores, validates OTPs
4. **ApprovalManager** - Creates/checks link-based approval requests
5. **NotificationSender** - Sends via Apprise (80+ services)
6. **BypassChecker** - Determines if 2FA should be skipped
7. **pam_sm_authenticate** - Main PAM entry point

### Approval Server (approval_server.py)

A lightweight HTTP server for link-based authentication:

1. **ApprovalManager** - Reads/writes approval request files
2. **ApprovalRequestHandler** - Handles HTTP requests
3. **CleanupThread** - Removes expired approvals
4. **Endpoints**:
   - `GET /approve/<token>` - Display request details and confirmation form
   - `POST /approve/<token>` - Mark approval as granted after confirmation
   - `GET /health` - Health check

The code is extensively commented for easy auditing and modification.

## Contributing

Contributions welcome! Areas for improvement:

- Rate limiting (per-user, per-IP)
- WebAuthn/FIDO2 support
- Backup codes
- Time-based lockouts
- Integration tests

## License

MIT License - Use and modify freely.

## Acknowledgments

- [Apprise](https://github.com/caronc/apprise) - Amazing multi-service notification library
- [pam-python](https://pam-python.sourceforge.net/) - Python PAM module framework
- [ntfy](https://ntfy.sh/) - Simple, free push notifications
