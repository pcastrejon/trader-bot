import subprocess
import unittest
from unittest.mock import patch

from trader_bot.notifications import DesktopNotifier, NotificationError


class DesktopNotifierTests(unittest.TestCase):
    def test_linux_notification_uses_fixed_argv_without_shell(self):
        notifier = DesktopNotifier()
        with (
            patch("trader_bot.notifications.sys.platform", "linux"),
            patch("trader_bot.notifications.shutil.which", return_value="/usr/bin/notify-send"),
            patch("trader_bot.notifications.subprocess.run") as run,
        ):
            notifier.notify("Paper order submitted", "DEMO accepted")

        self.assertEqual(
            run.call_args.args[0],
            ("/usr/bin/notify-send", "--app-name=Trader Bot", "Paper order submitted", "DEMO accepted"),
        )
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_macos_notification_escapes_quotes_and_backslashes(self):
        notifier = DesktopNotifier()
        with (
            patch("trader_bot.notifications.sys.platform", "darwin"),
            patch("trader_bot.notifications.shutil.which", return_value="/usr/bin/osascript"),
            patch("trader_bot.notifications.subprocess.run") as run,
        ):
            notifier.notify('Paper "status"', r'File C:\paper')

        script = run.call_args.args[0][2]
        self.assertIn(r'Paper \"status\"', script)
        self.assertIn(r'C:\\paper', script)
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_windows_notification_escapes_powershell_strings_without_shell(self):
        notifier = DesktopNotifier()
        with (
            patch("trader_bot.notifications.sys.platform", "win32"),
            patch(
                "trader_bot.notifications.shutil.which",
                side_effect=lambda name: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
                if name == "powershell.exe" else None,
            ),
            patch("trader_bot.notifications.subprocess.run") as run,
        ):
            notifier.notify("Paper operator's status", "Order O'Neil accepted")

        command = run.call_args.args[0]
        self.assertEqual(command[1:5], ("-NoProfile", "-NonInteractive", "-STA", "-Command"))
        self.assertIn("$icon.BalloonTipTitle = 'Paper operator''s status';", command[5])
        self.assertIn("$icon.BalloonTipText = 'Order O''Neil accepted';", command[5])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_notification_is_bounded_and_reports_missing_host_support(self):
        notifier = DesktopNotifier()
        with self.assertRaises(NotificationError):
            notifier.notify("title", "x" * 501)
        with (
            patch("trader_bot.notifications.sys.platform", "freebsd"),
            self.assertRaises(NotificationError),
        ):
            notifier.notify("title", "message")

    def test_windows_notification_reports_missing_powershell(self):
        with (
            patch("trader_bot.notifications.sys.platform", "win32"),
            patch("trader_bot.notifications.shutil.which", return_value=None),
            self.assertRaisesRegex(NotificationError, "PowerShell is unavailable"),
        ):
            DesktopNotifier().notify("title", "message")

    def test_platform_failure_is_explicit(self):
        notifier = DesktopNotifier()
        with (
            patch("trader_bot.notifications.sys.platform", "linux"),
            patch("trader_bot.notifications.shutil.which", return_value="/usr/bin/notify-send"),
            patch(
                "trader_bot.notifications.subprocess.run",
                side_effect=subprocess.TimeoutExpired("notify-send", 5),
            ),
            self.assertRaises(NotificationError),
        ):
            notifier.notify("title", "message")


if __name__ == "__main__":
    unittest.main()
