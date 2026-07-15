# PAM SSH 2FA Audit, Remediation, and User Administration Plan

Status: proposed after a full repository audit on 2026-07-14; Phase 0 and
Phase 1 of the "Implementation sequence" below (request-state atomicity,
configuration, and secret/logging handling) landed the same day. See the
`[FIXED]`/`[PARTIAL]` tags on individual findings below for exactly what
changed and what's still open. Phases 2-6 (installer/PAM-SSH
verification, native providers, admin CLI, unprivileged daemon,
packaging) have not started.

This is the implementation plan for the next work. It supersedes the
"Phase 1 is essentially complete" conclusion in `MODERNIZATION_PLAN.md`.
The earlier fixes improved the project substantially, but several of their
atomicity, installer, and PAM-stack completion claims do not hold under a
deeper concurrency and lifecycle review.

## Recommended outcome

1. Fix the remaining authentication-state, configuration, logging, installer,
   and PAM guidance defects before adding an administration interface.
2. Add a root-only administration CLI as the first user-management interface.
   Do not add a persistent web-admin login to the current root/Python design.
3. Make native Pushover and ntfy integrations the normal notification path.
   Keep Apprise behind an explicitly named legacy adapter for one migration
   release, then remove it as a mandatory dependency.
4. Give every authentication attempt one atomically reserved request/lease,
   regardless of whether it uses `code`, `link`, or `both`.
5. Move notification delivery and approval serving out of the PAM/`sshd`
   process and into the planned unprivileged daemon as the longer-term target.

The choices above are intentional defaults, not unresolved questions. They
fit the actual requirement--Pushover and ntfy only--while keeping a migration
path for existing Apprise URL configurations.

## Direct answers to the notification questions

### Pushover setup

A Pushover user key is not sufficient by itself. Pushover requires both:

- an application API token identifying the sending application; and
- a user or group key identifying the recipient.

Register one private Pushover application for each pam-ssh-2fa installation
or environment, for example `SSH 2FA - production`. Store that application
token once in the global protected configuration. Each enrolled SSH user then
supplies only their Pushover user/group key. One application token may send to
multiple user keys, so there is no need to create one Pushover application per
SSH user.

Do not ship a project-wide Pushover token in this repository. Each operator
should register their own application. During enrollment, validate the user
key with Pushover's user-validation endpoint and then send a test message.

Official references:

- Pushover Message API: <https://pushover.net/api>
- Pushover application-token guidance:
  <https://support.pushover.net/i175-how-to-get-a-pushover-api-or-pushover-application-token>
- Current Apprise Pushover syntax, for migration:
  <https://appriseit.com/services/pushover/>

### ntfy setup

The native configuration should ask for an HTTPS publish URL such as
`https://ntfy.example.com/ssh-alice` and, when applicable, a bearer access
token. Store the token separately from the URL in the typed configuration so
it cannot be leaked through ordinary URL logging.

An unauthenticated topic on `ntfy.sh` is public. Its topic name acts as a
password, and anyone who learns it can subscribe to the OTP/approval messages
and can also publish misleading messages to that topic. A long random topic
is acceptable for initial testing, but production guidance should prefer one
of:

- a reserved/protected hosted topic with an access token; or
- a self-hosted ntfy instance using HTTPS, deny-by-default access control,
  and a dedicated token/user for this application.

Official references:

- Publishing and authentication: <https://docs.ntfy.sh/publish/>
- Self-hosted access control and tokens: <https://docs.ntfy.sh/config/>
- Current Apprise ntfy syntax, for migration:
  <https://appriseit.com/services/ntfy/>

### Apprise decision

Apprise is a good general notification library, and the current release has
useful timeout, retry, and even optional Pushover encryption support. It is
nevertheless a poor permanent fit for this specific application:

- only two simple HTTPS APIs are required;
- it and its transitive dependencies are imported into a privileged PAM path;
- the installer currently modifies system Python with `pip` and may use
  `--break-system-packages`;
- opaque URL strings make validation, secret redaction, and interactive setup
  harder;
- the current code's documented "any destination succeeds" policy does not
  match Apprise's default required-destination semantics; and
- supporting 150+ providers adds code and configuration surface that this
  project does not use.

The safe migration is not an abrupt deletion. Introduce a small notifier
interface, add native `pushover` and `ntfy` implementations, and retain
`apprise` only as a legacy provider type while existing configuration is
migrated. After a deprecation window and parity tests, remove Apprise from the
default installation and eventually from the project.

## Audit scope and verification baseline

The audit covered every tracked source, test, configuration, example, service,
installer, documentation, and plan file, plus Git commit/reflog history. There
are no tracked or untracked runtime log files in the repository; `*.log` is
ignored. The only available historical log is the Git history.

Baseline checks on 2026-07-14:

- Git was clean at commit `8044bfb` before these plan edits.
- `python3 -m unittest discover -v`: 52 tests passed.
- The seven real `pamtester` integration cases ran on this Debian 13 host.
- `python3 -m compileall -q .`: passed.
- `bash -n install.sh`: passed.
- `./install.sh --dry-run --yes --no-link-approval`: completed.
- No `shellcheck`, Ruff, mypy, Bandit, or pylint executable is installed in
  the audit environment, so those checks were not represented by the green
  baseline.

The green suite is not sufficient evidence for the earlier completion claims.
Two deterministic audit probes demonstrated:

```text
concurrent successful validations of one OTP: 2 [True, True]
requests admitted with max_concurrent_per_user=1: 5
```

The normal test run also emitted `ResourceWarning` messages for unclosed file
and syslog handlers.

## Findings and required fixes

Severity means:

- P0: correct before relying on this for production authentication or before
  adding feature surface;
- P1: correct in the same stabilization release;
- P2: maintainability, operational, or documentation work that may follow P1
  but must be tracked.

### P0-1: OTP validation is not atomic or single-use under concurrency [FIXED]

Fixed: `CodeManager.validate()` now opens the code file once and holds an
exclusive `flock()` across the whole read/check/attempt-update/delete
transition, with an inode-staleness check (`os.fstat` vs `os.stat`) so a
racing caller can detect that its open file was since consumed/replaced
instead of trusting stale data. See `CodeManager._validate_locked()` and
`test_atomicity_regressions.py::CodeManagerAtomicityTests`, which
reproduced the double-validation and state-resurrection races against the
pre-fix code and now pass deterministically (run 5x with no flakiness).

Current state:

- `CodeManager.validate()` performs an existence check, reads JSON, compares,
  and only then unlinks the file.
- Two validators can read the same state before either unlinks it and both can
  return success. The audit forced this interleaving and observed two successful
  validations of one OTP.
- A concurrent invalid attempt can also replace/recreate state after a valid
  attempt unlinks it, because failed-attempt updates use `os.replace()` without
  a lock covering the complete read/compare/update/delete transition.
- `test_code_is_single_use` is sequential, and
  `test_concurrent_generate_and_validate_is_race_free` gives every worker a
  different request. Neither test exercises concurrent consumption of one
  request.

Preferred fix:

1. Keep the OTP itself in the current PAM invocation's memory; generation,
   prompting, and validation already occur synchronously in one process.
2. Store only a pending-request lease on disk for concurrency accounting. Do
   not store a plaintext OTP in a JSON file.
3. If file-backed OTP state is temporarily retained, hold an exclusive lock
   across read, schema validation, expiration check, attempt update, successful
   consume, and deletion. Never write an update after ownership was lost.
4. Make every terminal path close the lease in `finally`.

Acceptance tests:

- A barrier-controlled same-request race yields exactly one success.
- A valid-vs-invalid race cannot resurrect state.
- Attempt increments are never lost.
- Success, expiration, cancellation, notification failure, and unexpected
  exceptions leave no reusable OTP state.

### P0-2: the concurrent-request cap is a check-then-create race [FIXED]

Fixed: added `RateLimiter.reserve_request()`/`release_request()`, an
atomic prune-count-reserve operation under one per-user `flock()`ed
lease-list file, replacing the old scan-then-create pattern in
`pam_sm_authenticate()`. One lease now covers one authentication attempt
regardless of `auth_method`, so `both` is counted once instead of twice.
See `test_atomicity_regressions.py::RateLimiterReservationTests`
(20-thread barrier test against a cap of 3 admits exactly 3).

Current state:

- `RateLimiter.count_active()` scans request files, returns a number, and the
  caller creates state later with no shared lock or reservation.
- Multiple PAM processes can all observe `pending < limit` and all proceed.
  The audit synchronized five callers with a cap of one; all five created a
  request.
- A `both` request creates one OTP file and one approval file, so one SSH
  attempt is counted twice. The setting is documented as requests, not
  credentials.
- The standalone `count_active()` tests validate scanning but do not test an
  atomic cap-and-reserve operation.

Required fix:

1. Replace scan-then-create with `reserve_request(user, expires, limit)` under
   a per-user interprocess lock.
2. Use one lease/request ID for `code`, `link`, and `both`.
3. Prune expired leases while holding that lock, count live leases, and create
   the new lease in the same critical section.
4. Release by request ID in a guaranteed `finally` block.
5. Keep the sliding user/rhost event limiter separate from the active-request
   lease count.

Acceptance tests:

- Multiprocess and multithread tests never admit more than the configured cap.
- One `both` authentication counts once.
- Crashed/abandoned requests stop counting after expiration.
- Cleanup and reservation racing cannot delete or exceed live leases.

### P0-3: authentication state is leaked on many terminal paths [FIXED]

Fixed: Steps 5-7 of `pam_sm_authenticate()` were extracted into
`_run_challenge()`, called from one outer `try/finally` in
`pam_sm_authenticate()` that always releases the P0-2 lease and calls
`_best_effort_cleanup()` -- which removes any OTP/approval state
`_run_challenge()` created, regardless of whether it returned normally,
returned early, or raised. This is a safety net on top of (not a
replacement for) the existing per-branch cleanup calls. See
`test_atomicity_regressions.py::RequestStateCleanupTests`, which
reproduces the audit's own example (a notification-delivery failure in
`both` mode previously left the OTP file behind because only the
approval file was cleaned up) and confirms both are now gone.

Current examples include:

- code generation succeeds and approval setup fails;
- notification delivery fails (only approval state is cleaned);
- a code-only PAM conversation is cancelled;
- link-only expiration returns without cleanup;
- `both` succeeds by link but leaves the OTP file;
- `both` exhausts attempts using blank input and leaves the OTP file; and
- manager construction or an unhandled exception occurs after another piece
  of state was created.

This creates unnecessary plaintext OTP retention and can produce false
concurrency-limit denials until expiration.

Required fix:

- Introduce one request context/lease before generating provider-specific
  artifacts.
- Put all cleanup in one outer `try/finally`, not in scattered branches.
- Make cleanup idempotent and safe after partial construction.
- Explicitly record the terminal state for observability, then remove secrets.
- Add a fault-injection test at every construction and authentication step.

### P0-4: valid percent-encoded notification URLs can crash configuration loading [FIXED]

Fixed: `pam_ssh_2fa.Config`, `approval_server.ServerConfig`, and
`notify_check.py` now all construct `configparser.ConfigParser` with
`interpolation=None`. See
`test_atomicity_regressions.py::ConfigInterpolationTests`.

Not done in this pass: the broader per-setting bounds/type validation
and a real `config check` command described in the rest of this
finding remain open.

All three parsers use `configparser.ConfigParser()` with interpolation enabled.
An ordinary encoded URL such as one containing `%40` raises
`InterpolationSyntaxError` during `parser.get()`. The main parser catches only
`ValueError` and `TypeError` around that call, so a valid Apprise/ntfy URL can
escape configuration loading and abort the PAM path.

Required fix:

- Use `ConfigParser(interpolation=None)` (or `RawConfigParser`) consistently in
  the PAM module, approval server, admin tool, migration tool, and tests.
- Do not silently fall back field-by-field on invalid configuration. A
  `config check` command must report the file, section/key, and a redacted
  error. Authentication must fail closed with a non-secret log message.
- Add encoded URL, literal `%`, multiline template, duplicate section, invalid
  boolean, and malformed INI tests.
- Bound `timeout`, `max_attempts`, `server.port`, and message/template sizes;
  they are documented as bounded but several are parsed with bare `int`.

### P0-5: bearer tokens and provider secrets can be logged or printed [PARTIAL]

Fixed: `ApprovalRequestHandler.log_message()` no longer logs `args[0]`
(the raw request line, which contained `/approve/<bearer-token>`); it
logs only the method and a redacted route. `notify_check.py`'s
`_redact_url()` now shows only the URL scheme instead of a
split-on-'@' heuristic that printed the Pushover application token in
full. See `test_atomicity_regressions.py::ApprovalServerLoggingTests`.

Not done in this pass: there is no general allowlist-based redaction
helper used across all logs/CLI/exceptions, and provider-aware display
formatting (`pushover(user=****abcd)` etc.) does not exist yet -- both
depend on the native-provider work in Phase 3, which hasn't started.

Current state:

- `ApprovalRequestHandler.log_message()` logs `args[0]`, which is the HTTP
  request line and includes `/approve/<bearer-token>` when debug logging is
  enabled.
- Reverse-proxy access logging is not documented as another approval-token
  leak path.
- `test_notify.py` masks the part before `@` and prints the part after it. For
  `pover://USER_KEY@APP_TOKEN`, this exposes the application token.
- ntfy topic names, URL query credentials, Pushover user keys, and Pushover app
  tokens all need provider-aware redaction; a generic `@` split is unsafe.
- `PAMLogger` claims to sanitize data but does not remove control characters or
  secrets from arbitrary context values.

Required fix:

1. Never log approval request paths. Log only route name, response status,
   peer address after validation, and a short non-reversible request
   fingerprint if correlation is required.
2. Implement one allowlist-based redaction helper and use it in all logs, CLI
   display, diagnostics, exceptions, and provider results.
3. Display providers as `pushover(user=****abcd)` or
   `ntfy(host=ntfy.example.com, topic=****abcd)` without reconstructing a URL.
4. Document disabling/redacting access logs at reverse proxies.
5. Add tests asserting that known secrets never appear in captured stdout,
   stderr, file logs, syslog messages, or HTTP access logs.

### P0-6: installer backups are deleted rather than restored

The manifest records `BACKUP|original|backup`, but uninstall stores only the
backup path and deletes it. It never restores the original. This contradicts
the installer comments, README, CLAUDE.md, commit message, and modernization
status claiming exact restoration.

Other manifest correctness defects:

- a pre-existing `config.ini.new` is overwritten and recorded as a newly
  created file without backup;
- example files are overwritten and recorded as new even if they existed;
- the manifest is appended across runs without an explicit transaction or
  install-generation model;
- `systemctl daemon-reload` is guarded by a service-file existence check after
  the file may already have been removed, so the reload is skipped;
- an uninstall dry-run does not accurately show directory removals because
  simulated file removals do not change the later emptiness checks; and
- the approval service is stopped/disabled even when the manifest does not say
  this installer owned it.

Required fix:

1. Version the manifest and give every install/upgrade a transaction ID.
2. Distinguish `CREATED`, `REPLACED`, `BACKUP`, `SERVICE_STATE`, and
   `DIRECTORY` records.
3. On rollback, process the current transaction in reverse order and restore
   replaced files atomically with original ownership/mode.
4. On uninstall, remove installer-created files and restore files that existed
   before the first managed install; do not delete the only backup.
5. Validate every manifest path is an expected absolute path before mutation.
6. Set `umask 077`, enforce ownership/modes on existing directories, and fsync
   manifest/file updates.
7. Add disposable-container tests for fresh install, repeat install, upgrade,
   failed mid-upgrade rollback, config preservation/purge, link-service
   ownership, dry-run, and legacy migration.

Until those tests pass, documentation must not say uninstall is precise or
that backups are restored.

### P0-7: some documented PAM/SSH alternatives are incorrect

The primary Debian 13 recommendation--replace `common-auth` and require this
module behind `AuthenticationMethods publickey,keyboard-interactive:pam`--was
empirically tested and is the strongest part of the current work. The
alternative examples were not tested to the same standard:

- `auth optional pam_python.so ...` as the only auth module does not mean
  "allow login if 2FA fails". Linux-PAM documents that an optional module's
  result is important when it is the only module in that service/type stack.
- The `pam_succeed_if` numeric jump uses an `ignore` side effect for
  `pam_authenticate`; it can skip the only success-producing auth module and
  leave bypassed users denied, repeating the previously discovered
  `PAM_IGNORE` problem.
- The SSH example labeled "password + 2FA" uses
  `keyboard-interactive:pam` alone as its second alternative. With
  `common-auth` removed, that is 2FA-only, not password plus 2FA.
- `pam_ssh_2fa.py`'s top-level PAM instructions still say to add the module
  after `common-auth`, contradicting the corrected examples.

Required fix:

1. Remove all unvalidated soft-fail, group-bypass, and password alternatives
   from user-facing docs immediately, or label them unsafe/unverified.
2. Build real `pamtester` cases for each intended stack before publishing a
   replacement example.
3. For every stack, test success, wrong OTP, provider unavailable, unknown
   user, configured bypass, unconfigured-user policy, cancellation, locked
   Unix password, and preceding/following module failures.
4. Test the paired effective `sshd -T -C user=...,addr=...,host=...` output for
   global and `Match` cases; plain `sshd -T` does not exercise every conditional
   context.
5. Keep the supported-platform claim limited to Debian 13 until the same
   matrix runs on each advertised Debian/Ubuntu release.

Linux-PAM control semantics reference:
<https://man7.org/linux/man-pages/man5/pam.d.5.html>

### P1-1: approval state is marked atomically on disk but not consumed atomically [FIXED]

Fixed: added `ApprovalManager.consume_approval()` in pam_ssh_2fa.py,
using the same `flock()` + inode-staleness technique as P0-1's
`CodeManager._validate_locked()` to check-and-delete an approved request
in one locked operation. All four grant sites in `_run_challenge()`
(link-only poll loop, and the three grant checks in `both` mode) now call
`consume_approval()` instead of a separate `is_approved()` +
`cleanup()` pair. See
`test_atomicity_regressions.py::ApprovalConsumptionAtomicityTests`.

`os.replace()` prevents partial JSON, but it does not make the logical
read/check/write transition atomic. `is_approved()` can return true repeatedly
until a later cleanup, and the PAM process checks then unlinks in separate
operations. The current single-threaded HTTP server serializes normal POSTs,
but the state manager itself has no transition lock and will become racy when
the server is made concurrent.

Required fix:

- Define explicit `pending -> approved -> consumed` and terminal expired/
  cancelled states.
- Hold a shared interprocess lock for each transition, or consolidate the state
  in the daemon.
- Make confirmation-token use single-use with the pending-to-approved
  transition.
- Have PAM call `consume_approval()` rather than `is_approved()` followed by
  `cleanup()`.
- Add simultaneous POST, POST-vs-expire, consume-vs-cleanup, and replay tests.

### P1-2: the approval HTTP server is trivially blockable

The server uses single-threaded `HTTPServer` and does not set connection/header/
body deadlines. One slow client can occupy the only request handler and block
all approvals; a TLS handshake can also block acceptance.

Required fix for the current Python server:

- use a bounded concurrent server, not an unbounded thread-per-connection
  design;
- set short read, header, body, idle, and TLS-handshake deadlines;
- retain the 4 KiB form limit and also bound header count/size;
- return fixed-size error responses;
- cap active connections globally and per source;
- hide the Python/BaseHTTP version banner; and
- add slow-header, slow-body, idle-connection, oversized-header, and connection
  saturation tests.

The preferred final fix is the unprivileged daemon with production-grade HTTP
timeouts and limits.

### P1-3: the network-facing approval service still runs as root

The systemd unit explicitly uses `User=root`, writes broadly under `/var/log`,
and its `Documentation=` target is not installed. It also uses `Restart=always`
for clean exits and configuration failures.

Required fix:

- Create a dedicated `pam-ssh-2fa` service user.
- Let systemd create narrowly owned runtime/state/log directories with
  `RuntimeDirectory=`, `StateDirectory=` if persistence is required, and
  `LogsDirectory=` or journald.
- Share only the minimum approval-state path with PAM through a controlled
  group/socket protocol.
- Use `UMask=0077`, `Restart=on-failure`, a start-limit, and a readiness check.
- Narrow `ReadWritePaths`; do not grant all of `/var/log`.
- Add compatible hardening such as private devices, protected kernel/control
  groups, restricted address families, syscall filtering, and capability
  removal after testing.
- Install any file referenced by `Documentation=` or use a valid hosted URL.

### P1-4: provider delivery behavior is underspecified and misreported

Current issues:

- The code says success means at least one destination succeeded, but Apprise's
  default destinations are required; a partial failure can make the overall
  result false unless optional/escalation behavior is explicitly configured.
- `apobj.add(url)` return values are ignored, so invalid URLs are discovered
  late and opaquely.
- Provider count multiplies worst-case PAM latency, with no application-level
  total notification deadline.
- Comma-splitting `apprise_urls` is ambiguous and corrupts valid provider URLs;
  for example, ntfy legitimately uses commas in message-tag query values.
- There are no automated notification-provider tests; `test_notify.py` is a
  manual utility that happens to match unittest's discovery pattern.
- Provider responses and retry/rate-limit signals are not represented in a
  structured result.

Required policy:

- Add explicit `delivery_policy = any|all`; default to `any` for redundant
  personal endpoints so one working delivery permits the prompt.
- Return one redacted result per provider and an aggregate result.
- Fail before authentication if configuration has zero valid providers.
- Use strict TLS verification, no cross-origin credential redirects, bounded
  response bodies, short connect/read timeouts, and one overall deadline.
- Do not perform automatic delayed retries that outlive the useful OTP window.
- Honor provider `429` responses without logging secrets.

### P1-5: user policy and notification configuration have surprising semantics

Current issues:

- Notification URLs are required before `auth_method=none` is evaluated, so
  the documented service-account config containing only `[auth] method=none`
  does not work when no global URL exists.
- A deleted user file can expose a stale `.ini` or extensionless file because
  three filename variants are searched in priority order.
- Removing a user config can silently fall back to global notification URLs,
  or even bypass 2FA when `allow_unconfigured_users=true`; "remove user" does
  not have one predictable security meaning.
- A blank per-user URL does not override a global URL.
- Per-user auth method can be `none`, despite comments saying per-user files
  cannot override security settings.
- The global file is loaded once for bypass/policy and then loaded again in a
  new `Config` object for user settings. An admin change between those reads
  can make one authentication use an inconsistent mixed snapshot.
- Parse and conversion errors are silently ignored, hiding dangerous operator
  mistakes.

Required fix:

1. Use one canonical filename: `users/<exact-system-username>.conf`.
2. Refuse duplicate legacy variants and provide an explicit migration command.
3. Separate enrollment from provider inheritance. A user should be in one
   explicit state: `enforced`, `disabled/bypassed`, or `absent/denied`.
4. Do not use absence of a provider URL as the enrollment policy.
5. Rename `none` to an explicit administrative bypass action, log it, and
   require a warning/confirmation in the CLI.
6. Make `user remove` compute and display the effective post-removal policy;
   refuse an accidental bypass or global fallback unless `--force` confirms it.
7. Validate that the Unix account exists by default, with an explicit option
   for pre-provisioning.
8. Resolve global and per-user policy into one immutable validated snapshot per
   authentication request; do not reread the global file mid-decision.

### P1-6: URL and state schemas need strict validation [PARTIAL]

Fixed: OTP and approval JSON state is now validated before use --
non-dict payloads and non-numeric `expires`/`attempts` fields fail
closed instead of raising inside `validate()`/`consume_approval()` (see
`test_atomicity_regressions.py`'s `test_non_dict_json_state_...` and
`test_non_numeric_expires_field_...` tests). In-process poll-loop
deadlines (`ApprovalManager.wait_for_approval()` and the link-only loop
in `_run_challenge()`) now use `time.monotonic()` instead of
`time.time()`, so a wall-clock adjustment mid-wait can't affect them.

Not done in this pass: approval-URL scheme/host/port/userinfo
validation, `O_EXCL` for approval file creation, and symlink/ownership
checks on state directories all remain open.

Required additions:

- Validate approval URLs have a supported scheme, non-empty host, valid port,
  no userinfo, no query/fragment, and a deliberate base-path policy.
- Prefer HTTPS for every non-loopback approval URL. A non-global IP is not
  automatically a trusted network; RFC1918, link-local, reserved, and CGNAT
  ranges do not all imply confidentiality. Keep any plain-HTTP exception
  explicit and deployment-specific.
- Use `O_EXCL` for approval creation rather than truncation.
- Validate every JSON state field and type before comparison/arithmetic; valid
  JSON with strings, booleans, NaN-like values, or missing fields must fail
  closed without crashing the server/PAM path.
- Reject symlinks/non-regular files and enforce ownership/mode on security
  directories and config files.
- Use monotonic time for in-process wait deadlines and wall-clock timestamps
  only where cross-process expiration requires them.

### P1-7: exception handling does not cover partial setup safely

Constructors for storage, rate limiting, and approval managers can raise
outside the current narrow `try` blocks. Some write paths double-close an FD
after `os.fdopen()` has already taken ownership, potentially masking the
original exception. Failed attempt-counter writes are ignored, which can grant
extra guesses if state persistence fails. Notification template formatting
catches `KeyError` but not malformed-brace `ValueError`, so an invalid admin
template can escape the sender and strand request state.

Required fix:

- Wrap the complete authentication orchestration in a fail-closed boundary
  that logs a redacted error and always cleans request state.
- Use clear descriptor ownership (`with os.fdopen(...)`) without a second
  `os.close(fd)`.
- Treat inability to persist a failed attempt as a terminal authentication
  failure and consume/cancel the request.
- Add fault-injection tests for mkdir/open/read/write/fsync/replace/unlink,
  malformed configuration, provider timeout, and PAM conversation exceptions.

### P1-8: logging resources and log-file permissions are unsafe [PARTIAL]

Fixed: `PAMLogger` now closes each prior handler before clearing them
(instead of just dropping the reference), sets `propagate=False`, and
exposes an idempotent `close()`. The `ResourceWarning`s the audit
observed for unclosed file/syslog handlers are gone: the full suite now
runs clean under `python3 -W error::ResourceWarning -m unittest
discover`.

Not done in this pass: log rotation, pre-created private-mode log files
(vs. relying on umask), and control-character escaping in
user/rhost/context values all remain open.

`PAMLogger` clears handlers without closing them, producing the observed file
and syslog `ResourceWarning`s and potentially leaking descriptors in a
long-lived process. Propagation also produces duplicate root-logger output in
tests. File handlers use process umask rather than an explicit private mode,
and no rotation/journald-only policy is installed.

Required fix:

- Close every prior handler before removal, set `propagate=False`, and expose an
  idempotent `close()` used in `finally`.
- Pre-create private logs or use journald; do not depend on a permissive umask.
- Install log rotation if file logging remains.
- Escape control characters in user/rhost/context values.
- Add repeated-authentication descriptor-count and permission tests.

### P1-9: installer/package validation remains incomplete

Additional required work:

- Use `set -Eeuo pipefail` with a tested error trap and transaction rollback.
- Detect installed packages with an exact dpkg query; `dpkg -l | grep` can
  treat removed/config-only packages as installed.
- Stop installing an unpinned latest Apprise into system Python as root. The
  native-provider migration removes this need; the temporary legacy adapter
  should use a packaged/isolated dependency with a supported version range.
- Make `--enable-link-approval` wording match behavior: it currently installs
  but does not enable/start the service.
- Do not print `Installation Complete` during dry-run; label it `Preview
  Complete`, and do not claim a manifest was written.
- Make the self-test return nonzero for real failures rather than catching most
  failures and finishing successfully.
- Parse INI with the application parser instead of `grep -A5` for the port.
- Add a real `config check`, effective SSH checks with `-C`, and explicit
  supported-distribution/version gates.

### P2-1: cleanup tooling and retention need consolidation

`cleanup_codes.py` handles only OTP files, not approvals, leases, or rate-limit
state; no cron job or systemd timer is installed despite "cron-driven" wording.
Its dry-run `--all` summary double-counts expired files. It follows ordinary
filesystem paths without the validation expected of a root cleanup utility.

Replace it with an admin `state cleanup` command using the same validated state
manager as authentication. If periodic cleanup remains necessary, install a
systemd timer. Do not maintain a second independent parser/deletion algorithm.

### P2-2: test organization and safety need improvement

- Rename the manual `test_notify.py` utility so unittest discovery does not
  import it as a test module.
- Correct the status wording: there are 52 tests in eight actual automated
  test modules; the ninth `test_*.py` file is the manual notification utility.
- Avoid a fixed system username and `/etc/pam-ssh-2fa` lifecycle in ordinary
  test discovery where possible. Run destructive PAM integration in an
  explicit container/VM job, or generate a test-specific copied module with a
  temporary config path.
- Ensure cleanup cannot delete an installation that appears after the initial
  import-time precondition check.
- Add coverage reporting and static checks (Ruff/pyflakes, mypy where useful,
  Bandit, ShellCheck) to CI.
- Add provider contract tests with fake HTTPS servers, concurrency tests using
  processes as well as threads, installer container tests, and supported-OS
  OpenSSH/PAM VM tests.

### P2-3: documentation and source comments have drifted

Examples include:

- stale `common-auth` guidance in the main module docstring;
- README claiming the link flag enables the service when it only installs it;
- README "Contributing" listing rate limiting and integration tests as future
  work even though they exist;
- claims that approval tokens are single-use and installer restoration is
  precise when current behavior does not prove those claims;
- a documented `[auth] method=none` example that does not work without an
  effective notification URL;
- `Documentation=file:/etc/pam-ssh-2fa/README.md` while README is not installed;
  and
- outdated four-digit test examples.

After implementation, generate a config-key/documentation cross-check and
review every security claim against a named test. Do not status-tag a finding
`FIXED` until its completion test exists and passes.

## Native notification design

### Interface

Use a small provider-neutral interface whose implementation can move from
Python to the daemon without changing user configuration:

```text
Notifier.send(Notification) -> DeliveryResult

Notification:
  request_id, title, body, optional click_url, expires_at

DeliveryResult:
  provider, success, retryable, status_code, redacted_detail, elapsed_ms
```

The aggregate sender applies the explicit `delivery_policy`. It must never put
credentials, full URLs, topics, user keys, tokens, OTPs, or approval links in a
result destined for logs.

### Pushover provider

- Fixed endpoint: `POST https://api.pushover.net/1/messages.json`.
- Send `token`, `user`, `title`, and `message`; use Pushover's supplemental
  `url`/`url_title` for approval links when appropriate.
- Require normal TLS certificate and hostname verification.
- Validate the application and user/group keys as 30 case-sensitive
  alphanumeric characters before network use, then optionally validate the
  recipient using `POST /1/users/validate.json` during enrollment.
- Bound title/message/URL sizes to Pushover's documented limits before send.
- Parse both HTTP status and JSON `status`; cap the response body.
- Record rate-limit response metadata without tokens or message contents.
- Use normal priority by default. Do not introduce emergency retry/acknowledge
  behavior for SSH OTPs without a separate design.
- Treat Pushover end-to-end encryption as an optional later feature. Do not
  hand-roll cryptography; use a reviewed dependency if this is selected.

### ntfy provider

- Accept a normalized `https://host[:port]/topic` publish URL.
- Allow `http://` only for an explicitly approved loopback/test deployment.
- POST the body and set bounded `Title`, `Priority`, and `Click` headers.
- Put an access token in `Authorization: Bearer ...`, never in the URL/query.
- Do not follow redirects with credentials. If redirects are ever supported,
  permit only a tightly validated same-origin redirect.
- Cap response size and accept only documented successful responses.
- Warn and require confirmation for unauthenticated `ntfy.sh` topics; recommend
  protected/reserved or authenticated self-hosted topics for production.
- Do not let an arbitrary notification action perform an HTTP approval. The
  user should open the approval page and explicitly confirm there.

### Suggested typed configuration

Global protected configuration:

```ini
[notifications]
delivery_policy = any
connect_timeout = 3
read_timeout = 4
total_timeout = 8

[pushover]
app_token_file = /etc/pam-ssh-2fa/secrets/pushover-app-token
```

Per-user Pushover configuration:

```ini
[auth]
method = code

[notification]
providers = pushover

[pushover]
user_key = <private recipient key>
```

Per-user ntfy configuration:

```ini
[auth]
method = both

[notification]
providers = ntfy

[ntfy]
publish_url = https://ntfy.example.com/ssh-alice
access_token_file = /etc/pam-ssh-2fa/secrets/users/alice-ntfy-token
```

Rules:

- Secret files: root-owned, mode `0600` in the current PAM design.
- Per-user configs: canonical filename, root-owned, mode `0600`.
- Directories: root-owned and not writable by non-root.
- Do not put secrets in command-line arguments, environment variables by
  default, generated shell commands, logs, or backups with looser modes.
- Keep provider names and schema stable when implementation moves to the
  daemon.

### Apprise compatibility

Support this only during migration:

```ini
[notification]
providers = apprise

[apprise]
urls = ...
```

Migration behavior:

1. `config migrate-notifications --dry-run` recognizes Pushover and ntfy
   Apprise URLs and shows a fully redacted conversion plan.
2. It extracts fields into native typed configuration without printing them.
3. If the legacy comma-delimited value is ambiguous, stop for manual review
   rather than guessing where one URL ends.
4. Unknown Apprise schemes remain on the legacy adapter and produce a clear
   deprecation warning.
5. The admin confirms and tests the native provider before the old setting is
   removed.
6. Backups retain mode `0600` and are included in transactional rollback.
7. A later release removes the legacy adapter only after no supported upgrade
   path still depends on it.

## User administration CLI

### Interface choice

Implement `/usr/sbin/pam-ssh-2fa-admin` (or a subcommand on the future single
binary) first. A CLI is appropriate because:

- user enrollment is an infrequent root administration action;
- the current app has no admin identity, session, password storage, CSRF,
  authorization, or audit framework;
- a web UI would add another privileged network parser before the existing
  network service has been made unprivileged; and
- a CLI works locally, over an already authenticated SSH session, in recovery
  consoles, and in automation.

Do not auto-open a browser or create a persistent admin listener in the first
version.

### Commands

```text
pam-ssh-2fa-admin config check
pam-ssh-2fa-admin config show-effective USER
pam-ssh-2fa-admin provider configure pushover
pam-ssh-2fa-admin provider test pushover [--user USER]
pam-ssh-2fa-admin user add USER
pam-ssh-2fa-admin user edit USER
pam-ssh-2fa-admin user show USER
pam-ssh-2fa-admin user list
pam-ssh-2fa-admin user test USER
pam-ssh-2fa-admin user disable USER
pam-ssh-2fa-admin user enable USER
pam-ssh-2fa-admin user remove USER
pam-ssh-2fa-admin state list --redacted
pam-ssh-2fa-admin state cleanup
pam-ssh-2fa-admin doctor
```

### Interactive add/edit workflow

1. Require effective root and acquire an exclusive admin lock.
2. Validate the exact username with the system account database. Reject path
   characters and ambiguous legacy config filenames.
3. Select `code`, `link`, or `both`. A bypass is a separate explicit action,
   not an innocuous auth-method choice.
4. Select one or more providers.
5. Pushover:
   - if no global app token exists, explain that the admin must register a
     Pushover application and prompt for its token without echo;
   - prompt for the recipient user/group key without echo;
   - locally validate format, call the validation endpoint, and show only a
     redacted recipient fingerprint.
6. ntfy:
   - prompt for the full HTTPS publish URL;
   - prompt separately and without echo for an optional access token;
   - warn if `ntfy.sh` is unauthenticated/public;
   - reject credentials embedded in URL userinfo/query and suggest the token
     prompt instead.
7. Stage the new configuration in the target directory with mode `0600`.
8. Parse the staged configuration with the same production parser and show a
   redacted effective-policy summary.
9. Send a clearly labeled test notification through the same provider code the
   PAM/daemon path will use.
10. Ask the admin to confirm receipt. Permit a noninteractive explicit flag to
    skip this only when automation truly requires it.
11. Atomically replace the user config, fsync the file and directory, and keep
    a bounded private rollback copy.
12. Release the lock and log a secret-free administrative audit event.

### Secret input and output rules

- Use `getpass`/no-echo terminal input.
- For automation, accept a protected file descriptor or `--secret-file`; do
  not accept token values as command-line arguments because process listings
  and shell history expose them.
- Never print a command containing the entered secret.
- `show`, `list`, `doctor`, dry-run, tracebacks, and errors must be redacted.
- Refuse debug modes from provider libraries that print reconstructed URLs.
- Backups and temporary files must be created with private permissions before
  secret bytes are written.

### Remove/disable safety

`user remove` must not merely unlink a file. Before committing, calculate the
effective policy after removal and display one of:

```text
DENY (safe removal; user cannot complete SSH 2FA)
INHERIT GLOBAL PROVIDER (user remains enrolled with different destination)
BYPASS (dangerous; SSH key alone may grant access)
```

Refuse `INHERIT` or `BYPASS` unless the admin explicitly confirms with a
separate force option. `user disable` should create a clearly visible,
auditable administrative bypass record; it must not be conflated with removal.
Removing 2FA configuration must never delete the Unix user account.

### CLI acceptance tests

- Add/edit/remove/disable/enable for Pushover and ntfy.
- Wrong user key, wrong app token, wrong ntfy token, provider timeout, and
  partial multi-provider failure.
- Ctrl-C/EOF at every prompt leaves configuration unchanged.
- Concurrent admin writers serialize correctly.
- Disk-full, permission, fsync, and replace failures roll back.
- No secret appears in captured output/logs/process arguments/backups.
- Canonical usernames, nonexistent users, Unicode/path edge cases, and stale
  legacy extensions.
- Removal under every global/unconfigured-user policy.
- Root/non-root behavior and automation secret-file behavior.

## Why a web admin should be deferred

A web admin is not just a nicer prompt. It requires an authenticated admin
identity, password/key lifecycle, session cookies, CSRF protection, login rate
limits, authorization, TLS, recovery, secret-display rules, audit logging, and
safe interaction with root-owned files. Adding it to the current root approval
server would materially increase the highest-risk attack surface.

Reconsider a web UI only after the daemon exists and the CLI/provider API is
stable. If added later:

- bind the admin API to a Unix socket or loopback by default;
- require an SSH tunnel or separately authenticated reverse proxy;
- keep it in the unprivileged daemon, with a narrowly scoped privileged helper
  only for atomic config commits;
- use short-lived sessions, strong password hashing or external identity,
  CSRF tokens, origin checks, secure cookies, and login rate limits;
- never return stored secrets after initial entry;
- require reauthentication for bypass/removal/provider changes; and
- share all validation and business logic with the CLI rather than maintaining
  a second implementation.

An optional one-shot loopback wizard may be considered later, but it should be
an ephemeral view over the same CLI/API, use a random short-lived bootstrap
token, and never listen on a public interface.

## Implementation sequence

### Phase 0: correct claims and add failing regression tests [DONE]

Goal: make the repository accurately describe its current safety level.

- Reopen the relevant modernization findings. -- done in this document and
  `MODERNIZATION_PLAN.md`'s status table.
- Add deterministic failing tests for P0-1 through P0-7 before changing code.
  -- done for P0-1, P0-2, P0-4, P0-5 (`test_atomicity_regressions.py`,
  written against and confirmed failing on commit `c04f8a7`). P0-6
  (installer restore) and P0-7 (PAM/SSH alternatives) are Phase 2 work
  per this document's own phasing and do not yet have tests.
- Rename the manual notification utility so it is not test-discovered. --
  done (`test_notify.py` -> `notify_check.py`).
- Add CI jobs for Python compile/unit tests and ShellCheck/Ruff. -- done
  (`.github/workflows/ci.yml`); also ran `ruff check --fix` once over the
  existing codebase (unused imports, a bare `except`, extraneous
  f-strings -- all pre-existing, none security-relevant).

Exit criteria: met for the P0-1/P0-2/P0-4/P0-5 scope above. P0-6/P0-7
remain reopened findings without regression tests yet -- see Phase 2.

### Phase 1: repair request state, configuration, and secret handling [DONE for P0-1/P0-2/P0-3/P0-4/P0-5/P1-1; PARTIAL for P1-6/P1-8]

Goal: establish a trustworthy Python reference implementation.

- Implement one atomic request lease and one outer lifecycle cleanup boundary.
  -- done: `RateLimiter.reserve_request()`/`release_request()` (P0-2),
  `_run_challenge()` + `_best_effort_cleanup()` (P0-3).
- Make OTP and approval transitions single-consumer. -- done:
  `CodeManager._validate_locked()` (P0-1), `ApprovalManager.consume_approval()`
  (P1-1).
- Disable INI interpolation and validate the complete typed schema. --
  interpolation disabled everywhere (P0-4); typed *schema* validation is
  still only the OTP/approval JSON state (P1-6), not the full config
  schema described elsewhere in this document.
- Fix log/CLI secret redaction and handler lifecycle. -- bearer-token
  logging leak fixed (P0-5); handler lifecycle fixed (P1-8). The general
  allowlist-based redaction helper and provider-aware display formatting
  are not built (they depend on Phase 3's native providers).
- Validate URL/state schemas and use monotonic wait deadlines. -- done
  for JSON state type-checks and poll-loop monotonic deadlines (P1-6);
  approval-URL scheme/host/port validation and symlink/ownership checks
  remain open.

Exit criteria:

- Atomicity probes yield one consumer and never exceed a cap. -- met, see
  `CodeManagerAtomicityTests`, `RateLimiterReservationTests`,
  `ApprovalConsumptionAtomicityTests`.
- Fault injection leaves no credential/request state. -- met for the
  notification-failure case the audit specifically called out
  (`RequestStateCleanupTests`); not exhaustively fuzzed across every
  construction/exception point.
- Encoded URLs parse correctly. -- met, see `ConfigInterpolationTests`.
- Secret-canary tests find no disclosure. -- met for the specific P0-5
  leak (approval bearer token in debug HTTP logs, Pushover app token in
  `notify_check.py`); not a general-purpose secret-canary sweep.
- The existing 52 tests plus new regression tests pass without resource
  warnings. -- met: 63 tests total, `python3 -W error::ResourceWarning -m
  unittest discover` is clean.

### Phase 2: repair installer and PAM/SSH verification

Goal: eliminate data-loss and lockout guidance risks.

- Implement transactional, reversible manifest semantics.
- Correct service reload/state ownership and dry-run output.
- Remove or replace all untested PAM/SSH alternatives.
- Expand real-stack tests across supported platforms and Match contexts.

Exit criteria:

- Container tests prove install/upgrade/failure/rollback/uninstall behavior.
- Pre-existing files are restored byte-for-byte with correct metadata.
- Each published PAM stack has an integration test on every advertised OS.

### Phase 3: add native providers behind a stable interface

Goal: remove the mandatory broad notification dependency.

- Add provider-neutral request/result types.
- Implement and test native Pushover and ntfy providers.
- Add `delivery_policy`, total deadline, redaction, and response caps.
- Retain a legacy Apprise adapter and a redacted migration command.

Exit criteria:

- Provider contract tests cover all HTTP/status/timeout/redirect/rate-limit
  paths.
- Pushover validation and ntfy token authentication work end to end.
- A one-provider failure behaves exactly according to `any`/`all` policy.
- Default installation no longer needs Apprise once migration is complete.

### Phase 4: add the root administration CLI

Goal: make enrollment safe and simple without a new network attack surface.

- Implement commands and workflows above.
- Integrate provider validation/test sends.
- Add atomic config writes, locks, backups, removal-policy previews, and doctor.
- Update installer to invoke or clearly point to `user add`.

Exit criteria:

- A new admin can enroll Pushover or ntfy without manually constructing an
  Apprise URL.
- Interrupted or failed enrollment changes nothing.
- Removal cannot accidentally create an unreported bypass.
- All output is secret-safe.

### Phase 5: move complex work to the unprivileged daemon

Goal: keep network and parser complexity out of `sshd`.

- Complete the Unix-socket protocol and peer-credential checks from
  `MODERNIZATION_PLAN.md`.
- Move provider delivery, leases/state, rate limiting, and approval HTTP into
  the daemon.
- Run it as a dedicated user with systemd-created directories and tight limits.
- Reduce the PAM component to bounded local IPC and PAM conversation mapping.

Exit criteria:

- The PAM client performs no external network calls and parses no provider
  configuration.
- Daemon outage, malformed IPC, and timeout paths fail closed as documented.
- The approval server is not root.

### Phase 6: packaging, migration, and optional admin UI decision

Goal: ship a recoverable supported release.

- Package signed artifacts and make the installer a verified bootstrapper.
- Provide config migration, rollback, purge, and recovery paths.
- Test Debian/Ubuntu versions and architectures in disposable VMs.
- Remove the Apprise adapter after the announced compatibility period.
- Re-evaluate whether real demand justifies a local-only web UI.

Exit criteria:

- Upgrade and rollback from the current Apprise configuration are tested.
- No supported configuration needs `pip --break-system-packages`.
- Release artifacts and platform behavior meet the modernization definition of
  done.

## Required test matrix before production use

### Authentication state

- Correct/wrong/expired OTP; max attempts; replay; same-request races.
- Approval GET, confirm, replay, consume, expire, cleanup, and all transition
  races.
- Simultaneous code/link/both requests for the same user/source.
- Notification failure, cancellation, SSH disconnect, process crash, and
  daemon restart at every state.
- Sliding-window and active-lease limits under processes, not only threads.

### Configuration and administration

- Every numeric/string/boolean bound and unknown key policy.
- Percent-encoded URLs, literal percent signs, multiline templates, corrupt
  and wrong-typed state.
- Owner/mode/symlink checks.
- All CLI prompt interruption and filesystem fault paths.
- Migration from global/per-user Pushover and ntfy Apprise URLs.

### Providers

- TLS verification, hostname failure, connect/read/total timeout, redirects,
  oversized response, invalid JSON, 4xx/5xx/429, and partial redundancy.
- Pushover recipient validation and fixed API-host enforcement.
- ntfy bearer token, public-topic warning, and click link behavior.
- Secret-canary assertions across all captured diagnostics.

### PAM/OpenSSH/platform

- Debian and every advertised Ubuntu release.
- Password-locked and password-enabled accounts.
- Global and Match-specific `sshd -T -C` resolution.
- Bypass, absent user, provider outage, cancellation, and each PAM return code.
- Installer rollback/recovery with a live backup SSH session in disposable VMs.

## Definition of done for this plan

This plan is complete only when:

- one authentication request can be admitted and consumed exactly once;
- the concurrent cap cannot be raced and `both` counts as one request;
- every terminal path removes secrets and releases its lease;
- valid provider URLs cannot crash configuration loading;
- no logs or tools disclose provider or approval credentials;
- installer rollback and uninstall restore rather than delete pre-existing
  content;
- every documented PAM/SSH stack is empirically tested on its claimed OS;
- admins can add, test, disable, and remove users safely through the CLI;
- Pushover uses an operator-owned application token plus per-user keys;
- ntfy production guidance uses protected/authenticated topics;
- Pushover and ntfy work through native, bounded providers;
- Apprise is no longer mandatory and has a tested migration path; and
- network-facing code runs outside `sshd` without root privileges.
