"""Start MonsterBot.exe when the user logs in (HKCU Run key, no admin rights needed)."""
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "MonsterBot"


def supported() -> bool:
    return sys.platform == "win32" and getattr(sys, "frozen", False)  # only the packaged exe has a stable path


def is_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, NAME)[0] == f'"{sys.executable}"'
    except FileNotFoundError:
        return False


def set_enabled(enabled: bool):
    if not supported():
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, NAME)
            except FileNotFoundError:
                pass
