""""Terminal Connect": push a security from this dashboard straight into the
actual Bloomberg Terminal application, the same action as a user typing it
into the Terminal's own command line and pressing <GO> - via Windows UI
automation (window activation + simulated keystrokes), not a Bloomberg API.

Why UI automation and not an API call: blpapi (the Desktop API this whole
project is built on) is a pure market-data channel - reference/historical/
subscription requests only. There is no method in the SDK to control the
Terminal's own window ("navigate to this security" has no blpapi call). The
only way to drive the Terminal's own UI is the same way a human does: type
into it.

Confirmed live on this machine (2026-09-10) - window discovery only, no
keystrokes actually sent yet (see caveat below):
  - The Terminal's own rendering process is `wintrv.exe`, not the
    `bplus64.exe` process a login dialog belongs to - it owns four
    top-level windows titled "1-BLOOMBERG" through "4-BLOOMBERG" (the
    classic 4-panel Terminal layout), confirmed via EnumWindows.
  - At the time this was written, all four of those panel windows reported
    IsWindowVisible()=False and the only visible Bloomberg-titled window on
    the machine was "BLOOMBERG: Login" (a *different* process) - consistent
    with nobody actually being logged into the visible Terminal UI at that
    moment, even though the separate Desktop API data feed (`bbcomm.exe`)
    was working fine (that service doesn't require the visible Terminal
    window to be logged in - which is exactly why this dashboard had live
    prices throughout).

**This module is therefore UNVERIFIED end-to-end.** Window discovery is
confirmed against live handles; the actual "type a ticker and press GO"
step has never been run against a logged-in Terminal, because there wasn't
one visible to test against. Test it for real the first time someone is
actually logged in, before trusting it in front of anyone else.
"""

from __future__ import annotations

import time

import win32com.client
import win32gui


def _enum_bloomberg_panel_windows():
    """[(hwnd, title), ...] for every top-level window whose title matches
    the Terminal's numbered-panel convention ("1-BLOOMBERG" .. "4-BLOOMBERG"),
    sorted so panel 1 comes first - confirmed live as the panel windows
    `wintrv.exe` (the Terminal's own process) owns, as opposed to whatever
    separate window a login dialog belongs to."""
    found = []

    def callback(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if "-" in title:
            prefix, _, suffix = title.partition("-")
            if prefix.isdigit() and suffix == "BLOOMBERG":
                found.append((hwnd, title))
        return True

    win32gui.EnumWindows(callback, None)
    found.sort(key=lambda pair: pair[1])
    return found


def find_visible_panel():
    """(hwnd, title) for the lowest-numbered visible, non-minimized
    Bloomberg panel window, or None if none is currently visible (e.g.
    nobody is logged into the Terminal UI right now - see module
    docstring)."""
    for hwnd, title in _enum_bloomberg_panel_windows():
        if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            return hwnd, title
    return None


def open_security(ticker: str) -> dict:
    """Types `ticker` into the Bloomberg Terminal's command line and
    presses Enter - the same action as a user typing a security and
    hitting <GO>. Returns {"ok": True, "panel": "1-BLOOMBERG"} or
    {"ok": False, "error": "..."} - never raises for an expected failure
    (no Terminal visible, activation refused), so a caller can show the
    error directly rather than a stack trace.

    Uses WScript.Shell's AppActivate + SendKeys (the standard, well-worn
    way to drive another application's window on Windows) rather than
    raw SetForegroundWindow/keybd_event calls, which Windows' foreground-
    lock rules make unreliable from a background process."""
    panel = find_visible_panel()
    if panel is None:
        return {"ok": False, "error": "No visible Bloomberg Terminal panel window found - "
                                       "is the Terminal application logged in on this machine?"}
    hwnd, title = panel
    shell = win32com.client.Dispatch("WScript.Shell")
    if not shell.AppActivate(title):
        return {"ok": False, "error": f"Could not bring the Terminal panel {title!r} to the foreground"}
    time.sleep(0.2)  # let the window actually take focus before typing
    # SendKeys treats several characters as special (+^%~(){}[]) - a
    # ticker like "EURUSD Curncy" or "SPX Index" never contains any of
    # them, so no escaping is done here; a future caller passing anything
    # else through this should escape first.
    shell.SendKeys(ticker)
    time.sleep(0.05)
    shell.SendKeys("~")  # WScript.Shell's SendKeys code for Enter/<GO>
    return {"ok": True, "panel": title}
