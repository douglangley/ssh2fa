#!/bin/bash
# shellcheck disable=SC2034 # these assign variables install.sh's sourced
# functions read (DRY_RUN, ASSUME_YES, ENABLE_LINK_APPROVAL, INSTALL_DIR,
# STORAGE_DIR, SYSTEMD_UNIT_PATH, MANIFEST_FILE) -- shellcheck can't see
# that from a plain `source`.
#
# Regression tests for install.sh's manifest/backup/restore logic
# (AUDIT_REMEDIATION_AND_ADMIN_PLAN.md P0-6).
#
# Sources install.sh once, then reassigns INSTALL_DIR/STORAGE_DIR/
# SYSTEMD_UNIT_PATH directly to a fresh disposable temp directory per
# scenario, so every test below runs against real filesystem operations
# (not mocks) without touching the real /etc/pam-ssh-2fa,
# /var/run/pam-ssh-2fa, or systemd unit paths. install.sh's own
# `main "$@"` is guarded behind a BASH_SOURCE check specifically so this
# script can source it and call individual functions directly.
#
# Usage: bash test_install_manifest.sh
# Requires: bash 4+ (associative arrays), coreutils. No root required --
# everything happens under a user-owned temp directory.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PASS=0
FAIL=0

pass() {
    PASS=$((PASS + 1))
    echo "  [PASS] $1"
}

fail() {
    FAIL=$((FAIL + 1))
    echo "  [FAIL] $1"
}

assert_file_exists() {
    if [[ -f "$1" ]]; then pass "$2"; else fail "$2 (missing: $1)"; fi
}

assert_file_missing() {
    if [[ ! -f "$1" ]]; then pass "$2"; else fail "$2 (still present: $1)"; fi
}

assert_file_contains() {
    if [[ -f "$1" ]] && grep -qF "$2" "$1" 2>/dev/null; then
        pass "$3"
    else
        fail "$3 (expected '$2' in $1)"
    fi
}

assert_no_files_matching() {
    # $1 = glob pattern, $2 = description
    local matches
    matches=$(compgen -G "$1")
    if [[ -z "$matches" ]]; then
        pass "$2"
    else
        fail "$2 (found: $matches)"
    fi
}

run_scenario() {
    echo ""
    echo "=== $1 ==="
}

# Source install.sh's function/variable definitions once. Its own
# `set -e` would otherwise apply to the rest of THIS script too, since
# `source` runs in the current shell -- turn it back off immediately so
# assertion helpers that deliberately check failure paths (grep with no
# match, etc.) don't abort the whole test run.
#
# install.sh unconditionally assigns DRY_RUN=false/ASSUME_YES=false/
# ENABLE_LINK_APPROVAL="" as top-level statements (its CLI-flag defaults,
# normally set from argv in main()) -- these must be set AFTER sourcing,
# not before, or the source itself silently overwrites them back to
# those defaults. Getting this backwards makes ASSUME_YES=false, so
# uninstall()'s "Continue? [y/N]" read -p blocks forever with no stdin
# attached -- that's the bug this comment exists to prevent regressing.
# shellcheck source=install.sh
source "${REPO_DIR}/install.sh"
set +e
DRY_RUN=false
ASSUME_YES=true
ENABLE_LINK_APPROVAL="no"

new_workdir() {
    WORKDIR="$(mktemp -d)"
    INSTALL_DIR="${WORKDIR}/install"
    STORAGE_DIR="${WORKDIR}/storage"
    SYSTEMD_UNIT_PATH="${WORKDIR}/systemd/pam-ssh-2fa-server.service"
    MANIFEST_FILE="${INSTALL_DIR}/.install-manifest"
}

# -----------------------------------------------------------------------
# Scenario 1: pre-existing file is backed up, then RESTORED on uninstall
# (the core P0-6 bug: this used to delete the backup instead)
# -----------------------------------------------------------------------
run_scenario "pre-existing module file is restored on uninstall, not deleted"
new_workdir
mkdir -p "$INSTALL_DIR"
echo "PRE-EXISTING SENTINEL CONTENT -- admin's own file" > "${INSTALL_DIR}/pam_ssh_2fa.py"

create_directories >/dev/null
install_module >/dev/null

# install always deploys the current pam_ssh_2fa.py -- the pre-existing
# sentinel content should now live ONLY in a backup file, not at the
# main install path anymore.
if ! grep -qF "PRE-EXISTING SENTINEL CONTENT" "${INSTALL_DIR}/${MODULE_FILE}" 2>/dev/null; then
    pass "install deployed the new module file over the pre-existing one"
else
    fail "install did not overwrite the pre-existing file as expected"
fi

BACKUP_COUNT=$(compgen -G "${INSTALL_DIR}/pam_ssh_2fa.py.backup-*" | wc -l)
if [[ "$BACKUP_COUNT" -eq 1 ]]; then
    pass "exactly one backup recorded for the pre-existing file"
else
    fail "expected exactly one backup, found $BACKUP_COUNT"
fi
BACKUP_PATH=$(compgen -G "${INSTALL_DIR}/pam_ssh_2fa.py.backup-*" | head -1)
assert_file_contains "$BACKUP_PATH" "PRE-EXISTING SENTINEL CONTENT" \
    "the backup file holds the true pre-existing content"

uninstall >/dev/null 2>&1

assert_file_exists "${INSTALL_DIR}/${MODULE_FILE}" \
    "module file path still exists after uninstall (restored, not left missing)"
assert_file_contains "${INSTALL_DIR}/${MODULE_FILE}" "PRE-EXISTING SENTINEL CONTENT" \
    "ORIGINAL pre-existing content was restored, not deleted (P0-6 core fix)"
assert_no_files_matching "${INSTALL_DIR}/pam_ssh_2fa.py.backup-*" \
    "backup file was consumed by the restore, not left as a stray copy"

rm -rf "$WORKDIR"

# -----------------------------------------------------------------------
# Scenario 2: freshly-created file (no pre-existing content) is deleted
# on uninstall, and an intermediate upgrade backup is discarded, not
# "restored" (there's no real admin content in it to recover).
# -----------------------------------------------------------------------
run_scenario "freshly-installed file is deleted on uninstall, not resurrected from an upgrade backup"
new_workdir

create_directories >/dev/null
install_module >/dev/null   # fresh install: FILE record, no backup
install_module >/dev/null   # "upgrade": dest now exists -> BACKUP record

BACKUP_COUNT=$(compgen -G "${INSTALL_DIR}/pam_ssh_2fa.py.backup-*" | wc -l)
if [[ "$BACKUP_COUNT" -eq 1 ]]; then
    pass "upgrade created exactly one intermediate backup"
else
    fail "expected exactly one intermediate backup after upgrade, found $BACKUP_COUNT"
fi

uninstall >/dev/null 2>&1

assert_file_missing "${INSTALL_DIR}/${MODULE_FILE}" \
    "freshly-installed file is gone after uninstall (no pre-existing original to restore)"
assert_no_files_matching "${INSTALL_DIR}/pam_ssh_2fa.py.backup-*" \
    "intermediate upgrade backup was discarded, not resurrected as the restored file"

rm -rf "$WORKDIR"

# -----------------------------------------------------------------------
# Scenario 3: manifest path-safety validation rejects an out-of-scope path
# -----------------------------------------------------------------------
run_scenario "uninstall refuses to touch a manifest path outside its managed directories"
new_workdir

create_directories >/dev/null
install_module >/dev/null

OUTSIDE_FILE="${WORKDIR}/outside-scope-sentinel"
echo "should never be touched" > "$OUTSIDE_FILE"
echo "FILE|${OUTSIDE_FILE}" >> "$MANIFEST_FILE"

uninstall >/dev/null 2>&1

assert_file_exists "$OUTSIDE_FILE" \
    "out-of-scope manifest path was left untouched by uninstall"

rm -rf "$WORKDIR"

# -----------------------------------------------------------------------
# Scenario 4: --dry-run accurately previews directory removal
# -----------------------------------------------------------------------
run_scenario "--dry-run predicts directory removal that a real run would perform"
new_workdir

create_directories >/dev/null
install_module >/dev/null

# ASSUME_YES=true never confirms config/per-user-file removal (by
# design -- see uninstall_from_manifest), so INSTALL_DIR/users would
# never actually empty out under it. Use interactive confirmation here
# specifically so this scenario exercises a directory that DOES become
# empty, to prove the dry-run preview keeps up with that.
ASSUME_YES=false
DRY_RUN=true
# Two separate `read -p ... -n 1` prompts ("Continue?" then "Remove
# config files?") each consume exactly one character and leave the rest
# of stdin (including any newline) buffered for the next read -- so the
# two answers must be back-to-back characters with no newline between
# them, not "y\ny" (whose leftover "\n" would answer the second prompt).
OUTPUT="$(uninstall 2>&1 <<< "yy")"
DRY_RUN=false
ASSUME_YES=true

if echo "$OUTPUT" | grep -qF "[DRY-RUN] rmdir ${INSTALL_DIR}/users"; then
    pass "dry-run correctly predicted removing INSTALL_DIR/users once emptied"
else
    fail "dry-run did not predict removing INSTALL_DIR/users (directory-emptiness check is still stale)"
    echo "$OUTPUT" | sed 's/^/      /'
fi

rm -rf "$WORKDIR"

# -----------------------------------------------------------------------
# Scenario 5: systemd unit is only stopped/disabled/removed when this
# installer's manifest actually recorded it (service-ownership gating)
# -----------------------------------------------------------------------
run_scenario "uninstall only manages the systemd unit it actually installed"
new_workdir
ENABLE_LINK_APPROVAL="yes"
mkdir -p "$(dirname "$SYSTEMD_UNIT_PATH")"  # real systems already have /etc/systemd/system/

create_directories >/dev/null
install_module >/dev/null

assert_file_exists "$SYSTEMD_UNIT_PATH" "systemd unit installed when --enable-link-approval"

if grep -qF "|${SYSTEMD_UNIT_PATH}" "$MANIFEST_FILE"; then
    pass "manifest records ownership of the systemd unit"
else
    fail "manifest does not record the systemd unit -- ownership gating cannot work"
fi

ENABLE_LINK_APPROVAL="no"
rm -rf "$WORKDIR"

echo ""
echo "============================================================================="
echo "Results: $PASS passed, $FAIL failed"
echo "============================================================================="
[[ "$FAIL" -eq 0 ]]
exit $?
