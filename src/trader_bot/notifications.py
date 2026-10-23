"""Local desktop notifications with no shell or network integration."""

from __future__ import annotations

import math
import shutil
import subprocess
import sys


class NotificationError(RuntimeError):
    """A local desktop notification could not be delivered."""


class DesktopNotifier:
    """Send a bounded local notification using the host's native utility."""

    def __init__(self, *, timeout_seconds: float = 5) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Notification timeout must be positive.")
        self._timeout_seconds = timeout_seconds

    def notify(self, title: str, message: str) -> None:
        if (
            not isinstance(title, str) or not title.strip() or len(title) > 120
            or not isinstance(message, str) or not message.strip() or len(message) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in title + message)
        ):
            raise NotificationError("Notification title/message is invalid or exceeds bounds.")

        if sys.platform.startswith("linux"):
            self._notify_linux(title, message)
        elif sys.platform == "darwin":
            self._notify_macos(title, message)
        elif sys.platform == "win32":
            self._notify_windows(title, message)
        else:
            raise NotificationError(
                "Desktop notifications are supported only on Linux, macOS and Windows."
            )

    def _notify_linux(self, title: str, message: str) -> None:
        executable = shutil.which("notify-send")
        if executable is None:
            raise NotificationError("notify-send is not installed or unavailable on PATH.")
        self._run((executable, "--app-name=Trader Bot", title, message))

    def _notify_macos(self, title: str, message: str) -> None:
        executable = shutil.which("osascript")
        if executable is None:
            raise NotificationError("osascript is unavailable on this macOS host.")
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
        escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'display notification "{escaped_message}" '
            f'with title "{escaped_title}"'
        )
        self._run((executable, "-e", script))

    def _notify_windows(self, title: str, message: str) -> None:
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if executable is None:
            raise NotificationError("PowerShell is unavailable on this Windows host.")
        escaped_title = title.replace("'", "''")
        escaped_message = message.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$icon = New-Object System.Windows.Forms.NotifyIcon; "
            "$icon.Icon = [System.Drawing.SystemIcons]::Information; "
            "$icon.Visible = $true; "
            f"$icon.BalloonTipTitle = '{escaped_title}'; "
            f"$icon.BalloonTipText = '{escaped_message}'; "
            "$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info; "
            "$icon.ShowBalloonTip(3000); "
            "Start-Sleep -Milliseconds 3500; "
            "$icon.Dispose()"
        )
        self._run((executable, "-NoProfile", "-NonInteractive", "-STA", "-Command", script))

    def _run(self, command: tuple[str, ...]) -> None:
        try:
            subprocess.run(
                command, check=True, timeout=self._timeout_seconds,
                capture_output=True, text=True, shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotificationError("Local desktop notification command failed.") from exc
