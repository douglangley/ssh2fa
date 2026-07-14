# PAM SSH 2FA Modernization Plan

## Executive summary

The project is compact and its Python files compile successfully, but several security and reliability issues should be addressed before expanding link-based authentication or beginning a Go rewrite.

The recommended target architecture is:

```text
sshd
  -> small native PAM module
       -> local Unix socket
            -> unprivileged Go daemon
                 -> notification providers
                 -> approval HTTPS endpoint
                 -> short-lived authentication state
```

This provides a single Go service binary for the complex server functionality without embedding the Go runtime, HTTP handling, and notification code directly inside `sshd`.

The safest sequence is:

1. Fix and test the current security-sensitive behavior.
2. Specify the PAM-to-daemon protocol and trust boundaries.
3. Implement the Go daemon.
4. Add a small native PAM client.
5. Package it and provide a transactional installer.
6. Run a controlled migration and security review.

## Repository findings

### Critical: approval token handling is inconsistent

The PAM module generates URL-safe tokens that may contain letters, digits, `-`, and `_`. It uses those exact characters when naming approval files.

The approval server strips `-` and `_` before looking up those files. Valid approval links containing either character will therefore fail intermittently.

Recommended changes:

- Define one strict token format and use it everywhere.
- Reject tokens that do not match the format instead of modifying them.
- Add tests containing `-` and `_`, malformed tokens, and traversal attempts.

### Critical: a GET request approves an SSH login

Opening `/approve/<token>` immediately approves the pending authentication request. Notification services, chat applications, email systems, browsers, and security products commonly preview or scan links automatically. A scanner could therefore approve a login without an intentional user action.

Recommended changes:

- Make `GET /approve/<token>` display request details and a confirmation form only.
- Require an explicit `POST` to approve the request.
- Protect the POST with a per-request anti-CSRF value.
- Make approval consumption atomic and single-use.
- Clearly display the SSH username, host, source address, and request time before confirmation.

### High: approval links are bearer credentials

The documentation presents plain HTTP as a normal public deployment option. Anyone able to observe an approval URL in transit can approve the associated login.

Recommended changes:

- Require HTTPS for non-loopback and non-private development deployments.
- Support deployment behind a TLS reverse proxy.
- Reject insecure public server URLs by default.
- Avoid logging full URLs or tokens.

### High: unescaped values are inserted into HTML

The approval page places the username, hostname, and remote host directly into HTML output.

Recommended changes:

- HTML-escape all dynamic fields.
- Add a restrictive Content Security Policy.
- Add `X-Content-Type-Options: nosniff`, clickjacking protection, and a strict referrer policy.
- Avoid placing sensitive tokens in outbound referrer information.

### High: OTP requests can overwrite each other

OTP state is named only from the username and remote address. Two simultaneous connections by the same user from the same NAT address share a state file, so one connection can replace the other connection's code.

State reads, attempt updates, and deletion are also not atomic, allowing concurrent PAM processes to race.

Recommended changes:

- Assign every authentication attempt a cryptographically random request ID.
- Keep state per request rather than per `username + source address`.
- Use atomic state transitions or keep state in the daemon's bounded memory store.
- Ensure successful codes and approvals can be consumed exactly once.
- Add concurrency and replay tests.

### High: four-digit OTPs and notification flooding

A four-digit code has only 10,000 possibilities. The three-attempt limit helps within one request, but an attacker can repeatedly initiate authentication to obtain new guessing windows and generate notification spam.

Recommended changes:

- Change the default to six digits.
- Rate-limit request creation by account and source address.
- Limit concurrent pending requests per account.
- Add notification cooldowns and flood detection.
- Avoid revealing whether an account is configured through externally visible behavior.

### High: the documented PAM stack may not match the promised flow

The example adds the custom module after `@include common-auth`. With keyboard-interactive PAM, the distribution's normal authentication stack may still prompt for and validate a password. Depending on the host configuration, the resulting flow may be SSH key plus password plus push verification rather than SSH key plus push verification.

PAM control-flow errors can also cause either lockout or a 2FA bypass.

Recommended changes:

- Test the effective PAM flow on every supported Debian and Ubuntu release.
- Define a controlled PAM configuration strategy rather than relying on an assumed `common-auth` layout.
- Test success, rejection, unavailable service, unknown user, bypass, and cancellation return paths.
- Verify the final SSH configuration with both `sshd -t` and `sshd -T`.
- Document console and break-glass recovery before activation.

### Medium: the approval service runs as root

The network-facing Python approval server currently runs as root. Some systemd hardening is present, but a remotely reachable process should not have root privileges when it can be avoided.

Recommended changes:

- Run the future daemon as a dedicated system user.
- Make notification secrets readable only by that account.
- Use a root-controlled Unix socket group or peer-credential checks for PAM communication.
- Restrict writable paths to a dedicated runtime directory.
- Add stronger systemd sandboxing and syscall restrictions after compatibility testing.

### Medium: the installer exists but leaves risky work manual

The repository already contains an interactive `install.sh`, but it deliberately stops before modifying PAM and SSH. This leaves the most error-prone steps to the user.

Additional installer issues:

- It installs Apprise into the system Python environment using `--break-system-packages`.
- Documentation refers to `test_notify.py`, but that utility is not installed.
- Uninstallation does not remove or restore PAM and SSH changes.
- It installs the approval server even if link authentication is not desired.
- Backups are not tracked in an installation manifest.
- It does not validate the effective SSH configuration.
- It does not provide transactional rollback.
- It does not fully validate an end-to-end PAM authentication before activation.

### Medium: implementation responsibilities are too concentrated

The main PAM module is approximately 2,000 lines and combines:

- PAM conversations and return codes
- configuration parsing
- logging
- notification delivery
- bypass policy
- OTP generation and validation
- approval state
- filesystem operations
- authentication orchestration

This makes security review, testing, and failure isolation harder. Network calls and complex provider parsing also occur in the privileged SSH authentication process.

### Medium: there is no automated test suite

The Python files pass compilation checks and the shell installer passes `bash -n`, but the repository has no unit or integration test suite. `test_notify.py` is a manual diagnostic utility.

Required test areas include:

- PAM return values and stack behavior
- configuration validation and overrides
- token parsing
- OTP expiration, attempts, replay, and concurrent sessions
- atomic approval consumption
- link-preview behavior
- notification timeout and failure
- bypass users and networks
- malformed or corrupt state
- daemon restarts
- upgrades, rollback, and uninstall
- supported Debian and Ubuntu versions

## Go architecture recommendation

### Why not make the PAM module itself a Go binary?

PAM does not normally execute an authentication binary. It loads a shared object into the calling process, which in this case is associated with `sshd`.

Go can produce a shared object using `c-shared`, but that embeds the Go runtime in the host process. This introduces additional lifecycle, threading, signal, compatibility, and audit concerns inside a critical security process. It is possible, but it should not be the default design without substantial platform testing and review.

### Recommended split

Use two components:

1. A very small native PAM shared module, preferably written in C or Rust.
2. A statically linked Go daemon containing the complex logic.

The PAM module should only:

- Read trusted PAM fields such as username and remote host.
- Connect to a root-controlled local Unix socket.
- Start an authentication request.
- Conduct the PAM conversation when instructed by the daemon.
- Submit the response to the daemon.
- Map the daemon's result to an explicit PAM return code.
- Apply strict size limits and short timeouts to all local communication.

The Go daemon should own:

- configuration validation
- notification provider integrations
- rate limiting
- OTP and approval state
- the approval HTTPS endpoint
- expiration and replay protection
- logging and metrics
- health checks

### Notification provider tradeoff

The existing implementation obtains more than 80 provider integrations through Apprise. A native Go implementation will not automatically preserve that coverage.

Recommended approach:

- Define a small notifier interface.
- Initially support ntfy, Pushover, Telegram, and generic HTTPS webhooks.
- Allow several notifiers per user for redundancy.
- Keep an optional external Apprise adapter for users who need providers not implemented natively.
- Put explicit connection, TLS, response-size, and total request timeouts on every provider.

## Phased implementation plan

### Phase 1: stabilize the current implementation

Goal: create a safer, tested behavioral reference before changing languages.

- Fix token validation and filename consistency.
- Change approval to an explicit confirmation POST.
- Escape all HTML and add security headers.
- Require HTTPS for public approval endpoints.
- Give each authentication attempt a unique request ID.
- Make OTP and approval consumption atomic and one-time.
- Change the default OTP length to six digits.
- Add account, source, and concurrent-request rate limits.
- Add notification-flood protection.
- Consolidate duplicated approval-state logic.
- Validate configuration ranges and reject invalid security settings.
- Add focused Python unit and concurrency tests.

Completion criteria:

- No automatic link preview can approve a request.
- Tokens containing every valid character work consistently.
- Simultaneous sessions do not interfere with one another.
- Codes and approvals cannot be replayed.
- Security-critical behavior is covered by repeatable tests.

### Phase 2: define the protocol and trust boundaries

Goal: make the native PAM client and Go daemon independently testable.

- Specify a small, versioned Unix-socket protocol.
- Define request IDs and explicit state transitions.
- Use length-prefixed messages or another bounded encoding.
- Set maximum message sizes and strict deadlines.
- Authenticate local clients using socket ownership, permissions, and peer credentials.
- Define which PAM fields are trusted and how missing remote-host data is handled.
- Define fail-closed behavior for timeouts, crashes, malformed responses, and unavailable notifications.
- Map each daemon result to a documented PAM return value.
- Create protocol fixtures and compatibility tests before implementing both sides.

Suggested authentication sequence:

```text
PAM client -> daemon: begin(user, rhost, service)
daemon -> PAM client: prompt(request_id, text, echo)
PAM client -> daemon: answer(request_id, value)
daemon -> PAM client: allow | deny | unavailable
```

Link-only authentication can instead return a bounded wait instruction, with the PAM client waiting on the local socket until approval, rejection, or timeout.

### Phase 3: implement the Go daemon

Goal: replace Python service logic with one deployable binary.

- Implement typed configuration loading and validation.
- Add `config check`, `doctor`, and notification-test commands to the binary.
- Implement bounded in-memory request state with expiration.
- Avoid storing plaintext OTPs; store a keyed digest if persistence is necessary.
- Implement atomic single-use state transitions.
- Implement per-user and global policy resolution.
- Implement notifier interfaces and initial providers.
- Implement rate limiting and notification cooldowns.
- Implement the HTTPS approval and confirmation flow.
- Add structured journald logging with automatic secret redaction.
- Add health and readiness endpoints that expose no authentication details.
- Handle graceful shutdown without accepting stale approvals after restart.
- Produce reproducible static binaries for supported architectures.
- Add unit, race-detector, fuzz, and integration tests.

### Phase 4: implement the native PAM client

Goal: keep privileged in-process logic small and auditable.

- Implement only the defined Unix-socket protocol and PAM conversation glue.
- Avoid network access, provider SDKs, general configuration parsing, and state storage.
- Bound all allocation, input, output, and wait times.
- Handle daemon crashes and malformed responses by failing closed.
- Expose a deliberately named emergency policy rather than silently failing open.
- Test cancellation, SSH disconnects, daemon timeout, invalid users, and every PAM return path.
- Exercise it against real OpenSSH/PAM stacks on supported systems.
- Run memory-safety and static-analysis tooling appropriate to the selected language.

### Phase 5: packaging and installer

Goal: make installation safe for a normal administrator without hiding dangerous changes.

#### Preferred delivery

Publish signed Debian packages containing:

- the Go daemon
- the native PAM shared module
- systemd unit and tmpfiles configuration
- default configuration
- documentation and example provider configuration
- maintainer scripts that avoid enabling SSH authentication automatically

Use the shell installer as a friendly bootstrapper that selects the correct signed package rather than compiling or installing language dependencies on the target machine.

#### Installer commands

Support:

```text
install
upgrade
uninstall
doctor
rollback
```

Useful noninteractive flags should include:

```text
--dry-run
--yes
--provider
--user
--enable-link-approval
--no-ssh-changes
--rollback <installation-id>
```

#### Installer workflow

1. Verify root privileges and a supported distribution.
2. Detect architecture, SSH service name, PAM layout, init system, and firewall tooling.
3. Download the release through HTTPS.
4. Verify its checksum and signature before installation.
5. Install files with explicit ownership and permissions.
6. Create a dedicated service account and runtime directory.
7. Prompt for a notification provider without echoing or logging secrets.
8. Write configuration atomically.
9. Test notification delivery.
10. Back up every file that will be edited and record an installation manifest.
11. Add uniquely marked, idempotent PAM and SSH configuration blocks.
12. Run the daemon's configuration check.
13. Run `sshd -t` and inspect `sshd -T`.
14. Start the daemon and check its readiness.
15. Run a PAM smoke test that cannot grant a real remote session.
16. Print exact rollback and recovery commands.
17. Ask explicitly before reloading or restarting SSH.
18. Require the administrator to test a separate SSH session while retaining the existing one.

#### Rollback and uninstall requirements

- Track installer-owned files and edits in a manifest.
- Restore the immediately previous known-good configuration if validation fails.
- Remove only marked configuration blocks rather than overwriting entire files.
- Preserve user secrets by default during uninstall, with a separate purge option.
- Never remove packages or configuration not installed by this project.
- Validate SSH configuration again before completing rollback or uninstall.

### Phase 6: migration and release

Goal: replace the Python implementation without risking lockout or changed policy.

- Give the new module and service distinct names during migration.
- Support a dry-run audit mode that records the decision the Go daemon would have made without granting access.
- Compare Python and Go decisions using a shared behavioral test suite.
- Test upgrades and rollback in disposable virtual machines for every supported OS release.
- Test x86-64 and ARM64 release artifacts.
- Publish a threat model, supported-platform matrix, recovery guide, and upgrade policy.
- Perform an external security review before calling link approval production-ready.
- Deprecate the Python implementation only after feature parity and migration testing.

## Suggested priority order

### Immediate

- Fix link token lookup.
- Prevent GET/link-preview approval.
- Require explicit HTTPS guidance.
- Escape approval-page fields.
- Add unique request IDs and atomic one-time consumption.

### Next

- Add tests and rate limiting.
- Correct and test the PAM/SSH configuration guidance.
- Improve the existing installer's validation and rollback behavior.

### Then

- Specify the Unix-socket protocol.
- Build the Go daemon.
- Build the small native PAM client.
- Package signed releases and turn the installer into a safe bootstrap workflow.

## Definition of done

The modernization should be considered complete when:

- Approval requires an intentional user action and is resistant to link scanning.
- OTPs and approvals are unique per request, atomic, expiring, and single-use.
- The network-facing daemon runs without root privileges.
- Complex notification and HTTP code no longer executes inside `sshd`.
- PAM and SSH behavior is tested on every supported platform.
- Installation, upgrade, rollback, and uninstall are idempotent and validated.
- Administrators receive clear recovery instructions before SSH configuration changes.
- Release artifacts are reproducible, checksummed, and signed.
- An independent security review has evaluated the final authentication design.
