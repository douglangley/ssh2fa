#!/usr/bin/env python3
"""
PAM SSH 2FA - Notification Test Utility
=======================================

This utility allows you to test notification delivery without going through
the full PAM authentication flow. Use it to verify your Apprise URLs are
correct before enabling the PAM module.

This is a manual diagnostic tool, not part of the automated test suite --
it is deliberately named so it is NOT picked up by `unittest discover -p
"test_*.py"` (see AUDIT_REMEDIATION_AND_ADMIN_PLAN.md P2-2).

USAGE:
    ./notify_check.py                    # Use global config URLs
    ./notify_check.py --user doug        # Test doug's personal config
    ./notify_check.py --url "ntfy://..." # Test a specific URL
    ./notify_check.py --list-services    # Show supported services

EXAMPLES:
    # Test with your configured URLs
    sudo ./notify_check.py

    # Test a specific user's configuration
    sudo ./notify_check.py --user doug

    # Test a specific ntfy topic
    ./notify_check.py --url "ntfy://ntfy.sh/my-topic"

    # Test Pushover
    ./notify_check.py --url "pover://USERKEY@APPTOKEN"

    # Test multiple URLs
    ./notify_check.py --url "ntfy://ntfy.sh/topic1" --url "pover://user@token"
"""

import argparse
import sys
import os
import configparser

# Try to import apprise
try:
    import apprise

    APPRISE_AVAILABLE = True
except ImportError:
    APPRISE_AVAILABLE = False
    print("ERROR: Apprise not installed.")
    print("Install with: pip3 install apprise --break-system-packages")
    sys.exit(1)


def list_services():
    """
    List all notification services supported by Apprise.

    This prints a formatted list of services with their URL prefixes.
    """
    print("Supported Notification Services")
    print("=" * 60)
    print()
    print("Apprise supports 80+ services. Common ones include:")
    print()

    services = [
        ("ntfy", "ntfy://topic or ntfy://server/topic", "Free, open source"),
        ("Pushover", "pover://user@token", "$5 one-time"),
        ("Telegram", "tgram://bot_token/chat_id", "Free, needs bot"),
        ("Slack", "slack://token_a/token_b/token_c/#channel", "Free tier available"),
        ("Discord", "discord://webhook_id/webhook_token", "Free"),
        ("Email", "mailto://user:pass@smtp.server?to=addr", "Any SMTP"),
        ("Gotify", "gotify://server/token", "Self-hosted"),
        ("Pushbullet", "pbul://access_token", "Free tier"),
        ("Join", "join://apikey/device", "Android"),
        ("Simplepush", "spush://apikey", "iOS/Android"),
        ("Prowl", "prowl://apikey", "iOS"),
        ("Matrix", "matrix://user:pass@server/#room", "Self-hosted"),
        ("Rocket.Chat", "rocket://user:pass@server/#channel", "Self-hosted"),
        ("XMPP/Jabber", "xmpp://user:pass@server", "Various"),
        ("SMS (Twilio)", "twilio://sid:token@from/to", "Paid"),
        ("SMS (AWS SNS)", "sns://access_key/secret_key/region/phone", "Paid"),
    ]

    for name, url_format, note in services:
        print(f"  {name:15} {url_format}")
        print(f"  {' ':15} ({note})")
        print()

    print("For complete list and documentation:")
    print("  https://github.com/caronc/apprise/wiki")
    print()


def load_config_urls(config_path="/etc/pam-ssh-2fa/config.ini", user=None):
    """
    Load notification URLs from the config file.

    Args:
        config_path: Path to the main config INI file
        user: Optional username to load user-specific config

    Returns:
        Tuple of (urls_list, source_description)
    """
    urls = []
    source = "defaults"

    # Load global config
    if os.path.exists(config_path):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(config_path)
            urls_str = parser.get("notifications", "apprise_urls", fallback="")
            if urls_str:
                urls = [url.strip() for url in urls_str.split(",") if url.strip()]
                source = config_path
        except Exception as e:
            print(f"Warning: Could not parse config: {e}")

    # Check for user-specific config
    if user:
        config_dir = os.path.dirname(config_path) or "/etc/pam-ssh-2fa"
        user_config_dir = os.path.join(config_dir, "users")

        # Sanitize username
        safe_user = "".join(c for c in user if c.isalnum() or c in "-_.")
        if safe_user != user:
            print(f"Warning: Invalid characters in username, using: {safe_user}")
            user = safe_user

        # Try to find user config
        for ext in [".conf", ".ini", ""]:
            user_config_file = os.path.join(user_config_dir, f"{user}{ext}")
            if os.path.exists(user_config_file):
                parser = configparser.ConfigParser(interpolation=None)
                try:
                    parser.read(user_config_file)
                    urls_str = parser.get("notifications", "apprise_urls", fallback="")
                    if urls_str:
                        urls = [
                            url.strip() for url in urls_str.split(",") if url.strip()
                        ]
                        source = user_config_file
                except Exception as e:
                    print(f"Warning: Could not parse user config: {e}")
                break
        else:
            if user:
                print(
                    f"Note: No config file found for user '{user}', using global config"
                )

    return urls, source


def _redact_url(url: str) -> str:
    """
    Redact a notification URL for display, keeping only the scheme.

    A previous version masked only the part of the URL before '@' and
    printed everything after it verbatim. That is backwards for schemes
    like Pushover's `pover://USER_KEY@APP_TOKEN`, where the secret
    (the application token) is the part AFTER '@' -- it was being
    printed in full. Different providers put their secret on different
    sides of '@' (or in the query string, as ntfy access tokens can
    be), so no single split-on-'@' rule is safe for every scheme. Show
    only the scheme and redact everything else.

    Args:
        url: A notification URL that may contain credentials

    Returns:
        A display-safe string with only the scheme preserved
    """
    scheme = url.split("://", 1)[0] if "://" in url else url
    return f"{scheme}://***REDACTED***"


def send_test_notification(urls, code="1234"):
    """
    Send a test notification to the specified URLs.

    Args:
        urls: List of Apprise notification URLs
        code: Test code to include in the message

    Returns:
        True if at least one notification succeeded
    """
    if not urls:
        print("ERROR: No notification URLs provided.")
        print()
        print("Either:")
        print("  1. Configure URLs in /etc/pam-ssh-2fa/config.ini")
        print("  2. Create per-user config in /etc/pam-ssh-2fa/users/<username>.conf")
        print("  3. Use --url to specify URLs directly")
        print()
        return False

    # Get hostname
    try:
        hostname = os.uname().nodename
    except OSError:
        hostname = "test-host"

    # Create notification content
    title = "SSH 2FA Test Notification"
    body = f"""This is a test notification from PAM SSH 2FA.

If you received this, your notification setup is working!

Test code: {code}
Host: {hostname}
User: test
From: 127.0.0.1

This code would expire in 5 minutes during real authentication."""

    # Create Apprise instance
    apobj = apprise.Apprise()

    print(f"Testing {len(urls)} notification URL(s)...")
    print()

    for url in urls:
        print(f"  Adding: {_redact_url(url)}")
        apobj.add(url)

    print()
    print("Sending notification...")

    # Send
    result = apobj.notify(title=title, body=body, notify_type=apprise.NotifyType.INFO)

    print()
    if result:
        print("[OK] SUCCESS: Notification sent!")
        print()
        print("Check your device - you should have received a message.")
        return True
    else:
        print("[FAIL] FAILED: Could not send notification.")
        print()
        print("Troubleshooting:")
        print("  - Verify your URL format is correct")
        print("  - Check that API keys/tokens are valid")
        print("  - Ensure outbound HTTPS (port 443) is allowed")
        print("  - Try with --debug for more details")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Test PAM SSH 2FA push notifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           Test with global config URLs
  %(prog)s --user doug               Test doug's personal config
  %(prog)s --url "ntfy://ntfy.sh/my-topic"   Test specific URL
  %(prog)s --list-services           Show supported services
        """,
    )

    parser.add_argument(
        "--url",
        "-u",
        action="append",
        dest="urls",
        help="Apprise notification URL (can be specified multiple times)",
    )

    parser.add_argument("--user", help="Username to load per-user config for")

    parser.add_argument(
        "--config",
        "-c",
        default="/etc/pam-ssh-2fa/config.ini",
        help="Path to config file (default: /etc/pam-ssh-2fa/config.ini)",
    )

    parser.add_argument(
        "--code",
        default="1234",
        help="Test code to include in notification (default: 1234)",
    )

    parser.add_argument(
        "--list-services",
        "-l",
        action="store_true",
        help="List supported notification services",
    )

    parser.add_argument("--debug", action="store_true", help="Enable debug output")

    args = parser.parse_args()

    # Handle --list-services
    if args.list_services:
        list_services()
        return 0

    # Enable debug logging if requested
    if args.debug:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    print()
    print("PAM SSH 2FA - Notification Test")
    print("=" * 40)
    print()

    # Get URLs from args or config
    urls = args.urls or []
    source = "command line"

    if not urls:
        print("Loading URLs from config...")
        if args.user:
            print(f"Looking for user-specific config for: {args.user}")
        urls, source = load_config_urls(args.config, args.user)
        print(f"Using config: {source}")
        print()

    # Send test
    success = send_test_notification(urls, args.code)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
