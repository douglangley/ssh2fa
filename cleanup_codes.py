#!/usr/bin/env python3
"""
PAM SSH 2FA - Code Cleanup Utility
==================================

This utility removes expired OTP codes from the storage directory.
While codes are normally cleaned up during authentication, this handles
edge cases like interrupted connections or abandoned sessions.

USAGE:
    ./cleanup_codes.py              # Clean up expired codes
    ./cleanup_codes.py --dry-run    # Show what would be deleted
    ./cleanup_codes.py --all        # Remove ALL codes (emergency)

CRON SETUP:
    Run every 15 minutes to clean up stale codes:
    */15 * * * * /etc/pam-ssh-2fa/cleanup_codes.py

This script is optional - codes will expire naturally and be rejected
during authentication. This just keeps the storage directory tidy.
"""

import os
import sys
import json
import time
import argparse

# Default storage directory - should match config
STORAGE_DIR = "/var/run/pam-ssh-2fa"


def cleanup_codes(
    storage_dir: str, dry_run: bool = False, remove_all: bool = False
) -> dict:
    """
    Remove expired or all codes from the storage directory.

    Args:
        storage_dir: Path to the code storage directory
        dry_run: If True, only report what would be done
        remove_all: If True, remove ALL codes regardless of expiry

    Returns:
        Dictionary with cleanup statistics:
        - checked: Number of files checked
        - expired: Number of expired codes found
        - removed: Number of files removed
        - errors: Number of errors encountered
    """
    stats = {
        "checked": 0,
        "expired": 0,
        "removed": 0,
        "errors": 0,
        "active": 0,
    }

    # Check if directory exists
    if not os.path.exists(storage_dir):
        return stats

    now = time.time()

    # Iterate through code files
    for filename in os.listdir(storage_dir):
        # Only process our code files
        if not filename.startswith("code_") or not filename.endswith(".json"):
            continue

        filepath = os.path.join(storage_dir, filename)
        stats["checked"] += 1

        should_remove = False
        reason = ""

        if remove_all:
            # Emergency mode - remove everything
            should_remove = True
            reason = "forced removal"
        else:
            # Check if code has expired
            try:
                with open(filepath, "r") as f:
                    code_data = json.load(f)

                expires = code_data.get("expires", 0)

                if now > expires:
                    should_remove = True
                    reason = "expired"
                    stats["expired"] += 1
                else:
                    # Code is still valid
                    remaining = int(expires - now)
                    stats["active"] += 1
                    if dry_run:
                        user = code_data.get("user", "unknown")
                        print(
                            f"  ACTIVE: {filename} (user={user}, expires in {remaining}s)"
                        )

            except (json.JSONDecodeError, IOError) as e:
                # Corrupt file - remove it
                should_remove = True
                reason = f"corrupt: {e}"
                stats["errors"] += 1

        # Remove if needed
        if should_remove:
            if dry_run:
                print(f"  WOULD REMOVE: {filename} ({reason})")
            else:
                try:
                    os.unlink(filepath)
                    stats["removed"] += 1
                except OSError as e:
                    print(f"  ERROR removing {filename}: {e}", file=sys.stderr)
                    stats["errors"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Clean up expired PAM SSH 2FA codes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                 Clean up expired codes
  %(prog)s --dry-run       Show what would be deleted
  %(prog)s --all           Remove ALL codes (emergency cleanup)
  %(prog)s --quiet         Suppress output (for cron)
        """,
    )

    parser.add_argument(
        "--storage-dir",
        "-d",
        default=STORAGE_DIR,
        help=f"Code storage directory (default: {STORAGE_DIR})",
    )

    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be done without making changes",
    )

    parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Remove ALL codes, not just expired ones",
    )

    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress normal output (errors still shown)",
    )

    args = parser.parse_args()

    if args.all and not args.dry_run:
        # Confirm destructive operation
        print("WARNING: This will remove ALL pending 2FA codes!")
        print("Users with pending codes will need to reconnect.")
        response = input("Continue? [y/N] ")
        if response.lower() != "y":
            print("Cancelled.")
            return 0

    if not args.quiet:
        print("PAM SSH 2FA - Code Cleanup")
        print(f"Storage directory: {args.storage_dir}")
        if args.dry_run:
            print("DRY RUN - no changes will be made")
        print()

    # Run cleanup
    stats = cleanup_codes(
        storage_dir=args.storage_dir, dry_run=args.dry_run, remove_all=args.all
    )

    # Report results
    if not args.quiet or stats["errors"] > 0:
        print()
        print("Results:")
        print(f"  Files checked: {stats['checked']}")
        print(f"  Active codes:  {stats['active']}")
        print(f"  Expired codes: {stats['expired']}")

        if args.dry_run:
            print(
                f"  Would remove:  {stats['expired'] + (stats['checked'] if args.all else 0)}"
            )
        else:
            print(f"  Removed:       {stats['removed']}")

        if stats["errors"] > 0:
            print(f"  Errors:        {stats['errors']}")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
