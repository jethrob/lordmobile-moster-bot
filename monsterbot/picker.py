"""Native Windows folder dialog for the Setup page (browsers can't hand a web page a local folder path)."""
import os
import subprocess
import sys

# The start folder is passed through an environment variable, never pasted into the script.
SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true }
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose the LordsBot config folder (it has one folder per castle IGG ID, usually C:\LordsBot\config)'
$dialog.ShowNewFolderButton = $false
if (Test-Path -LiteralPath $env:MONSTERBOT_START -PathType Container) { $dialog.SelectedPath = $env:MONSTERBOT_START }
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dialog.SelectedPath) }
"""


def supported() -> bool:
    return sys.platform == "win32"


def pick_folder(start: str) -> str | None:
    """Show the dialog on this PC and return the chosen folder, or None if cancelled."""
    if not supported():
        return None
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", SCRIPT],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "MONSTERBOT_START": start},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip() or None
