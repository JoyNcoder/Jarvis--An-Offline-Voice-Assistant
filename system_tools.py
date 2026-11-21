"""
system_tools.py - Windows system actions for Jarvis.

Batch 1 (stability):
  * Every third-party import is guarded. A missing or broken library only
    disables its own feature; Jarvis still starts.
  * Every action returns one short sentence that can be spoken, and does not
    raise for ordinary failures. Technical details go to the console.
  * Game launching is intentionally not included.

Batch 2 (apps and folders):
  * App aliases cleaned up (all lower case, typos fixed, mishearings added).
  * Start Menu scan: any installed app with a Start Menu shortcut can be
    opened by name ("open spotify", "open visual studio code"), including
    slightly misheard names ("note pad", "spotifi").
  * "close <app>" (polite taskkill, no force, so unsaved work still prompts).
    It refuses to close terminals / Python, which may be running Jarvis.
  * Real folder paths are read from Windows, so OneDrive-redirected
    Desktop / Documents / Pictures work.
  * Extra folders: home, temp, appdata, program files, screenshots.
  * Your own spoken shortcuts live in custom_locations.json (see below).
  * "rescan apps" refreshes the Start Menu list after installing something.

custom_locations.json (optional, next to this file), for example:
    {
      "project": "D:\\\\AI-Voice-Assistant",
      "games folder": "E:\\\\Games",
      "obs": "C:\\\\Program Files\\\\obs-studio\\\\bin\\\\64bit\\\\obs64.exe"
    }
Saying "open project" or "open project folder" then opens that path.
"""

import ctypes
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.parse
import webbrowser
from ctypes import POINTER, cast
import ctypes.wintypes
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
BASE_DIR = Path(__file__).resolve().parent
CUSTOM_FILE = BASE_DIR / "custom_locations.json"

# feature name -> the error that disabled it. Filled in by the guarded imports.
MISSING = {}

try:
    import psutil
except Exception as _error:
    psutil = None
    MISSING["system stats (psutil)"] = _error

try:
    import screen_brightness_control as sbc
except Exception as _error:
    sbc = None
    MISSING["brightness (screen_brightness_control)"] = _error

try:
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
except Exception as _error:
    CLSCTX_ALL = AudioUtilities = IAudioEndpointVolume = None
    MISSING["volume (pycaw / comtypes)"] = _error

try:
    import GPUtil          # NVIDIA only. Optional; other GPUs are skipped.
except Exception:
    GPUtil = None  
    
try:
    import winreg
except Exception:
    winreg = None

try:
    from PIL import ImageGrab
except Exception as _error:
    ImageGrab = None
    MISSING["window screenshots (pillow)"] = _error        # not reported as missing: it is optional


def report_missing():
    """Print which features are disabled and why (call once at startup)."""
    if not MISSING:
        print("System tools: all features available.")
        return
    for feature, error in MISSING.items():
        print(f"System tools: {feature} is disabled ({error}).")


def _fail(what, error):
    """Log technical details to the console, return a sentence for speech."""
    print(f"[system_tools] {what} failed: {error!r}")
    return f"Sorry, I couldn't {what}."

def spoken_duration(seconds):
    """1200 -> '20 minutes', 5400 -> '1 hour and 30 minutes'."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{n} {word}{'' if n == 1 else 's'}"
             for n, word in ((hours, "hour"), (minutes, "minute"), (secs, "second")) if n]
    return " and ".join(parts[:2]) or "0 seconds"


def _hotkey(*keys):
    """Press keys together (e.g. Win+M), then release in reverse order."""
    user32 = ctypes.windll.user32
    for vk in keys:
        user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    for vk in reversed(keys):
        user32.keybd_event(vk, 0, 2, 0)


def _still_running(process):
    if psutil is None:
        return False
    try:
        return any((p.info["name"] or "").lower() == process.lower()
                   for p in psutil.process_iter(["name"]))
    except Exception:
        return False

# Programs that may be hosting Jarvis. Window tools never close these.
_OWN_HOSTS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe", "windowsterminal.exe",
              "python.exe", "pythonw.exe", "conhost.exe", "openconsole.exe"}
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _focused_window():
    """(handle, process name, window class) of the window you are using, or None."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    if psutil is not None:
        try:
            name = psutil.Process(pid.value).name().lower()
        except Exception:
            pass
    return hwnd, name, cls.value


def _is_jarvis_window(hwnd, process):
    return process in _OWN_HOSTS or hwnd == ctypes.windll.kernel32.GetConsoleWindow()


HELP_ALIASES = {
    "windows": "windows", "window": "windows",
    "apps": "apps", "folders": "apps",
    "volume": "sound", "brightness": "sound",
    "power": "power",
    "screen": "screen", "screenshots": "screen",
    "stats": "stats",
}

HELP_TEXT = {
    "general": "I can control volume and brightness, open and close apps and folders, "
               "manage windows, take screenshots, switch dark mode, lock, sleep or shut down "
               "your PC, and read CPU and memory use. Ask for help with windows, apps, volume, "
               "power, screen, or stats for examples.",
    "windows": "For windows, say minimize, maximize, restore, snap left, snap right, "
               "minimize all, switch window, or close this window.",
    "apps": "Say open or close followed by an app name, like open spotify. "
            "You can also open folders like downloads, and drives like e drive. "
            "Say rescan apps after installing something.",
    "sound": "Say volume up, volume down, mute, unmute, or set volume to fifty percent. "
             "Brightness works the same way.",
    "power": "Say lock, sleep, shut down the computer, restart the computer, "
             "or shut down in twenty minutes. Say abort to cancel a timer.",
    "screen": "Say take a screenshot for the whole screen, or screenshot this window for the "
              "one you are using. Say dark mode, light mode, or toggle theme to change how "
              "Windows looks.",
    "stats": "Say CPU, RAM, or system stats to hear how hard your computer is working.",
}


# Spoken page name -> Windows Settings address.
SETTINGS_PAGES = {
    "display": "ms-settings:display", "screen": "ms-settings:display",
    "sound": "ms-settings:sound", "audio": "ms-settings:sound",
    "wifi": "ms-settings:network-wifi", "wi fi": "ms-settings:network-wifi",
    "network": "ms-settings:network", "internet": "ms-settings:network",
    "bluetooth": "ms-settings:bluetooth", "devices": "ms-settings:bluetooth",
    "battery": "ms-settings:batterysaver", "power": "ms-settings:powersleep",
    "apps": "ms-settings:appsfeatures", "default apps": "ms-settings:defaultapps",
    "startup apps": "ms-settings:startupapps",
    "windows update": "ms-settings:windowsupdate", "update": "ms-settings:windowsupdate",
    "personalization": "ms-settings:personalization",
    "background": "ms-settings:personalization-background",
    "theme": "ms-settings:themes", "taskbar": "ms-settings:taskbar",
    "privacy": "ms-settings:privacy", "storage": "ms-settings:storagesense",
    "notifications": "ms-settings:notifications", "night light": "ms-settings:nightlight",
    "mouse": "ms-settings:mousetouchpad", "keyboard": "ms-settings:easeofaccess-keyboard",
    "printers": "ms-settings:printers", "date and time": "ms-settings:dateandtime",
    "time": "ms-settings:dateandtime", "language": "ms-settings:regionlanguage",
    "accounts": "ms-settings:yourinfo", "about": "ms-settings:about",
    "vpn": "ms-settings:network-vpn", "airplane mode": "ms-settings:network-airplanemode",
    "security": "ms-settings:windowsdefender", "windows security": "ms-settings:windowsdefender",
}


_WINDOWS_ONLY = "That only works on Windows."


# ---------------------------------------------------------------
# App names
# ---------------------------------------------------------------

# Spoken name (lower case only!) -> something Windows can start.
APP_ALIASES = {
    "notepad": "notepad.exe",
    "note pad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "browser": "chrome.exe",
    "task manager": "taskmgr.exe",
    "taskmanager": "taskmgr.exe",
    "tosc manager": "taskmgr.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "files": "explorer.exe",
    "this pc": "shell:MyComputerFolder",
    "my computer": "shell:MyComputerFolder",
    "recycle bin": "shell:RecycleBinFolder",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "power shell": "powershell.exe",
    "power show": "powershell.exe",
    "terminal": "wt.exe",
    "paint": "mspaint.exe",
    "settings": "ms-settings:",
    "control panel": "control.exe",
    "snipping tool": "snippingtool.exe",
    "device manager": "devmgmt.msc",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
}

# Spoken name -> the name to look up in the Start Menu list.
APP_SYNONYMS = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "code editor": "visual studio code",
    "obs": "obs studio",
    "whats app": "whatsapp",
}

# Process names for "close <app>" when the launch name is not the process name.
PROCESS_NAMES = {
    "calc.exe": "CalculatorApp.exe",
    "ms-settings:": "SystemSettings.exe",
    "wt.exe": "WindowsTerminal.exe",
    "snippingtool.exe": "SnippingTool.exe",
}

# Never closed by voice: Jarvis itself may be running inside these.
_PROTECTED_PROCESSES = {
    "explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe",
    "windowsterminal.exe", "python.exe", "pythonw.exe", "conhost.exe",
}

_SKIP_SHORTCUT_WORDS = ("uninstall", "readme", "read me", "help", "documentation",
                        "license", "website", "release notes", "manual")


def _compact(text):
    """'Note Pad' -> 'notepad' (so spacing mistakes from Whisper do not matter)."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


_ALIAS_COMPACT = {_compact(name): (target, name) for name, target in APP_ALIASES.items()}

_menu_cache = None     # compact name -> (path to .lnk, display name)


def _start_menu_apps(force=False):
    """Scan Start Menu shortcuts once (and again on 'rescan apps')."""
    global _menu_cache
    if _menu_cache is not None and not force:
        return _menu_cache

    found = {}
    if IS_WINDOWS:
        roots = [
            Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        ]
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                for shortcut in root.rglob("*.lnk"):
                    name = shortcut.stem
                    lowered = name.lower()
                    if any(word in lowered for word in _SKIP_SHORTCUT_WORDS):
                        continue
                    key = _compact(name)
                    if key and key not in found:
                        found[key] = (str(shortcut), name)
            except Exception as error:
                print(f"[system_tools] Start Menu scan problem in {root}: {error!r}")
    _menu_cache = found
    return found


def _load_custom():
    try:
        data = json.loads(CUSTOM_FILE.read_text(encoding="utf-8"))
        return {_compact(k): (str(v), k) for k, v in data.items()}
    except Exception:
        return {}


def _resolve_app(spoken):
    """Return (target, display_name) for a spoken app name, or None."""
    name = APP_SYNONYMS.get(spoken.strip().lower(), spoken.strip().lower())
    key = _compact(name)
    if not key:
        return None

    custom = _load_custom()
    if key in custom:
        return custom[key]
    if key in _ALIAS_COMPACT:
        target, label = _ALIAS_COMPACT[key]
        return target, label

    menu = _start_menu_apps()
    if key in menu:
        path, display = menu[key]
        return path, display

    if len(key) >= 4:
        starts = [k for k in menu if k.startswith(key)]
        inside = [k for k in menu if key in k]
        hits = starts or inside
        if hits:
            best = min(hits, key=len)
            return menu[best]

    pool = list(menu) + list(_ALIAS_COMPACT) + list(custom)
    close = difflib.get_close_matches(key, pool, n=1, cutoff=0.8)
    if close:
        k = close[0]
        if k in custom:
            return custom[k]
        if k in _ALIAS_COMPACT:
            return _ALIAS_COMPACT[k]
        return menu[k]
    return None


# ---------------------------------------------------------------
# Folders
# ---------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


_FOLDER_IDS = {
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "desktop":   "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "pictures":  "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music":     "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos":    "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}


def _known_folder(guid_text):
    """Ask Windows where a standard folder really is (handles OneDrive)."""
    if not IS_WINDOWS:
        return None
    try:
        u = uuid.UUID(guid_text)
        guid = _GUID(u.time_low, u.time_mid, u.time_hi_version,
                     (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        buffer = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                                      ctypes.byref(buffer)) != 0:
            return None
        path = buffer.value
        ctypes.windll.ole32.CoTaskMemFree(buffer)
        return Path(path) if path else None
    except Exception:
        return None


_PLURAL = {"download": "downloads", "document": "documents", "picture": "pictures",
           "photo": "pictures", "photos": "pictures", "video": "videos"}

_folder_cache = None


def _folders():
    """spoken name -> Path. Built once, real Windows locations first."""
    global _folder_cache
    if _folder_cache is not None:
        return _folder_cache

    home = Path.home()
    base = {}
    for name, guid in _FOLDER_IDS.items():
        base[name] = _known_folder(guid) or (home / name.capitalize())
    for single, plural in _PLURAL.items():
        base[single] = base[plural]

    base["home"] = home
    base["user"] = home
    base["temp"] = Path(tempfile.gettempdir())
    base["screenshots"] = base["pictures"] / "Screenshots"
    base["screenshot"] = base["screenshots"]
    if os.environ.get("APPDATA"):
        base["appdata"] = Path(os.environ["APPDATA"])
    if os.environ.get("ProgramFiles"):
        base["program files"] = Path(os.environ["ProgramFiles"])
    _folder_cache = base
    return base


def _resolve_folder(spoken):
    """Return (path_string, label) for a spoken folder name, or None."""
    clean = re.sub(r"\s+", " ", spoken.strip().lower())
    clean = re.sub(r"^(?:my |the )", "", clean)
    clean = re.sub(r" folder$", "", clean).strip()
    if clean in ("home folder", "user folder"):
        clean = "home"

    custom = _load_custom()
    key = _compact(clean)
    for ckey in (key, _compact(clean + " folder")):
        if ckey in custom:
            path, label = custom[ckey]
            if os.path.isdir(path):
                return path, label

    path = _folders().get(clean)
    if path is not None:
        return str(path), clean
    return None


def _shortcut_process(lnk_path):
    """Real .exe behind a Start Menu shortcut (Code.exe for VS Code)."""
    try:
        path = lnk_path.replace("'", "''")
        script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{path}');"
                  "$s.TargetPath;$s.Arguments")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=8,
                             creationflags=0x08000000).stdout.splitlines()
        target = out[0].strip() if out else ""
        args = out[1] if len(out) > 1 else ""
        started = re.search(r"--processStart\s+(\S+)", args)   # Discord, Slack, Teams
        name = started.group(1) if started else os.path.basename(target)
        return name if name.lower().endswith(".exe") else None
    except Exception:
        return None


class WindowsSystem:

    # --- 1. Volume Control (pycaw) ---
    @staticmethod
    def _get_volume_endpoint():
        if AudioUtilities is None:
            return None
        devices = AudioUtilities.GetSpeakers()
        if hasattr(devices, "EndpointVolume"):
            return devices.EndpointVolume
        if hasattr(devices, "_dev"):
            interface = devices._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return cast(interface, POINTER(IAudioEndpointVolume))
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))

    _NO_VOLUME = "Volume control isn't available on this computer."

    @classmethod
    def set_volume(cls, target_pct: int) -> str:
        if AudioUtilities is None:
            return cls._NO_VOLUME
        try:
            target_pct = max(0, min(100, int(target_pct)))
            vol = cls._get_volume_endpoint()
            vol.SetMute(0, None)
            vol.SetMasterVolumeLevelScalar(target_pct / 100.0, None)
            return f"Volume set to {target_pct} percent."
        except Exception as error:
            return _fail("change the volume", error)

    @classmethod
    def change_volume(cls, delta_pct: int) -> str:
        if AudioUtilities is None:
            return cls._NO_VOLUME
        try:
            vol = cls._get_volume_endpoint()
            current = int(round(vol.GetMasterVolumeLevelScalar() * 100))
            new_vol = max(0, min(100, current + int(delta_pct)))
            vol.SetMute(0, None)
            vol.SetMasterVolumeLevelScalar(new_vol / 100.0, None)
            return f"Volume adjusted to {new_vol} percent."
        except Exception as error:
            return _fail("change the volume", error)

    @classmethod
    def mute(cls, state: bool = True) -> str:
        if AudioUtilities is None:
            return cls._NO_VOLUME
        try:
            vol = cls._get_volume_endpoint()
            vol.SetMute(1 if state else 0, None)
            return "Volume muted." if state else "Volume unmuted."
        except Exception as error:
            return _fail("change the mute setting", error)

    # --- 2. Brightness Control ---
    _NO_BRIGHTNESS = "Brightness control isn't available on this computer."

    @classmethod
    def set_brightness(cls, target_pct: int) -> str:
        if sbc is None:
            return cls._NO_BRIGHTNESS
        try:
            target_pct = max(0, min(100, int(target_pct)))
            sbc.set_brightness(target_pct)
            return f"Brightness set to {target_pct} percent."
        except Exception as error:
            print(f"[system_tools] brightness error: {error!r}")
            return "I couldn't change the brightness. Some desktop monitors don't support it."

    @classmethod
    def change_brightness(cls, delta_pct: int) -> str:
        if sbc is None:
            return cls._NO_BRIGHTNESS
        try:
            current = sbc.get_brightness()
            curr_val = current[0] if isinstance(current, list) else current
            new_val = max(0, min(100, int(curr_val) + int(delta_pct)))
            sbc.set_brightness(new_val)
            return f"Brightness set to {new_val} percent."
        except Exception as error:
            print(f"[system_tools] brightness error: {error!r}")
            return "I couldn't change the brightness. Some desktop monitors don't support it."

            # --- Batch 4: windows, screenshot, settings ---
    @staticmethod
    def minimize_all() -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            _hotkey(0x5B, 0x4D)                 # Win + M
            return "Minimizing all windows."
        except Exception as error:
            return _fail("minimize the windows", error)

    @staticmethod
    def restore_windows() -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            _hotkey(0x5B, 0x10, 0x4D)           # Win + Shift + M
            return "Restoring your windows."
        except Exception as error:
            return _fail("restore the windows", error)

    @staticmethod
    def switch_window() -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            _hotkey(0x12, 0x09)                 # Alt + Tab
            return "Switching window."
        except Exception as error:
            return _fail("switch the window", error)

        

    @staticmethod
    def open_settings(page: str) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        uri = SETTINGS_PAGES.get(page.strip().lower())
        if uri is None:
            return f"I don't know a settings page called {page}."
        try:
            os.startfile(uri)
            return f"Opening {page} settings."
        except Exception as error:
            return _fail(f"open the {page} settings", error)

    @staticmethod
    def screenshot() -> str:
        """Whole screen (all monitors) saved as a PNG in Pictures\\Screenshots."""
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            import base64
            folder = _folders()["screenshots"]
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / time.strftime("Jarvis screenshot %Y-%m-%d %H-%M-%S.png")
            path = str(target).replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                "Add-Type -TypeDefinition 'using System.Runtime.InteropServices;"
                "public class Dpi { [DllImport(\"user32.dll\")] "
                "public static extern bool SetProcessDPIAware(); }';"
                "[Dpi]::SetProcessDPIAware() | Out-Null;"
                "$v=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
                "$b=New-Object System.Drawing.Bitmap $v.Width,$v.Height;"
                "$g=[System.Drawing.Graphics]::FromImage($b);"
                "$g.CopyFromScreen($v.Left,$v.Top,0,0,$b.Size);"
                f"$b.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Png)"
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode()
            subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                           capture_output=True, timeout=20, creationflags=0x08000000)
            if target.exists():
                return "Screenshot saved in your Screenshots folder."
            return "I couldn't save the screenshot."
        except Exception as error:
            return _fail("take a screenshot", error)

    # --- 3. Apps and folders (Batch 2) ---
    @staticmethod
    def open_app(app_name: str) -> str:
        """Open a folder shortcut, custom shortcut, alias, or Start Menu app."""
        if not IS_WINDOWS:
            return _WINDOWS_ONLY

        # "open downloads", "open project" -> folders win over apps.
        folder = _resolve_folder(app_name)
        if folder is not None and os.path.isdir(folder[0]):
            try:
                os.startfile(folder[0])
                return f"Opening your {folder[1]} folder."
            except Exception as error:
                return _fail(f"open the {folder[1]} folder", error)

        resolved = _resolve_app(app_name)
        if resolved is not None:
            target, display = resolved
            try:
                os.startfile(target)
                return f"Opening {display}."
            except Exception as error:
                print(f"[system_tools] could not start {target!r}: {error!r}")

        # Last resort: let Windows try the raw name (PATH / App Paths).
        raw = app_name.strip().lower()
        for attempt in (raw, raw + ".exe") if "." not in raw else (raw,):
            try:
                os.startfile(attempt)
                return f"Opening {app_name}."
            except Exception:
                continue
        return f"Sorry, I couldn't find an app called {app_name}."

    @staticmethod
    def close_app(app_name: str) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY

        resolved = _resolve_app(app_name)
        if resolved is not None:
            target, display = resolved
        else:
            target, display = app_name.strip().lower(), app_name

        if target.lower().endswith(".lnk"):
            process = _shortcut_process(target) or (_compact(display) + ".exe")
        elif target in PROCESS_NAMES:
            process = PROCESS_NAMES[target]
        elif target.lower().endswith(".exe"):
            process = os.path.basename(target)
        elif resolved is None:
            process = _compact(app_name) + ".exe"
        else:
            return f"I can't close {display} by name."

        if process.lower() in _PROTECTED_PROCESSES:
            return f"I won't close {display}, because I might be running inside it."

        try:
            # No /f: windows are asked to close, so unsaved work still prompts.
            result = subprocess.run(["taskkill", "/im", process],
                                    capture_output=True, timeout=10)
            if result.returncode == 128:
                return f"{display.capitalize()} isn't running."
            if result.returncode != 0:
                print(f"[system_tools] taskkill {process} returned {result.returncode}")
                return f"I couldn't close {display}. It may have no window, so I left it alone."
            if psutil is None:
                return f"Closing {display}."
            for _ in range(10):                      # wait up to 2 seconds
                time.sleep(0.2)
                if not _still_running(process):
                    return f"Closed {display}."
            return f"I asked {display} to close. If it's still open, it may want you to save first."
        except Exception as error:
            return _fail(f"close {display}", error)
        except Exception as error:
            return _fail(f"close {display}", error)

    @staticmethod
    def rescan_apps() -> str:
        found = _start_menu_apps(force=True)
        if not found:
            return "I couldn't find any installed apps to scan."
        return f"Done. I know {len(found)} installed apps."


        # --- Websites ---
    SITES = {
        "youtube": "https://www.youtube.com",
        "gmail": "https://mail.google.com",
        "google": "https://www.google.com",
        "google maps": "https://www.google.com/maps",
        "maps": "https://www.google.com/maps",
        "google drive": "https://drive.google.com",
        "google docs": "https://docs.google.com",
        "github": "https://github.com",
        "chatgpt": "https://chatgpt.com",
        "claude": "https://claude.ai",
        "whatsapp": "https://web.whatsapp.com",
        "facebook": "https://www.facebook.com",
        "twitter": "https://x.com",
        "reddit": "https://www.reddit.com",
        "wikipedia": "https://www.wikipedia.org",
        "netflix": "https://www.netflix.com",
        "amazon": "https://www.amazon.com",
        "linkedin": "https://www.linkedin.com",
        "stack overflow": "https://stackoverflow.com",
    }
    # Whisper often splits or respells these.
    SITE_ALIASES = {
        "you tube": "youtube", "g mail": "gmail", "google mail": "gmail",
        "git hub": "github", "chat gpt": "chatgpt", "face book": "facebook",
        "x": "twitter", "whats app": "whatsapp", "wiki": "wikipedia",
        "maps": "google maps", "drive": "google drive", "docs": "google docs",
    }
    SEARCH_URLS = {
        "google": "https://www.google.com/search?q=",
        "youtube": "https://www.youtube.com/results?search_query=",
        "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
        "maps": "https://www.google.com/maps/search/",
    }

    @classmethod
    def open_website(cls, name: str) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            name = " ".join(name.lower().split())
            name = cls.SITE_ALIASES.get(name, name)
            url = cls.SITES.get(name)
            if url is None:
                return f"I don't have a website called {name}."
            webbrowser.open(url)
            return f"Opening {name}."
        except Exception as error:
            return _fail(f"open {name}", error)

    @classmethod
    def search_web(cls, engine: str, query: str) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            engine = "maps" if "map" in engine else engine
            base = cls.SEARCH_URLS.get(engine, cls.SEARCH_URLS["google"])
            webbrowser.open(base + urllib.parse.quote_plus(query.strip()))
            return f"Searching {engine} for {query.strip()}."
        except Exception as error:
            return _fail("search the web", error)

    @staticmethod
    def open_folder(folder_name: str) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        clean = folder_name.strip().lower()

        # Drives: "e drive", "edrive", "drive e", "c drive", ...
        drive_match = re.fullmatch(r"(?:([a-z])\s*drive|drive\s*([a-z]))", clean)
        if drive_match:
            letter = (drive_match.group(1) or drive_match.group(2)).upper()
            path = f"{letter}:\\"
            if not os.path.exists(path):
                return f"Drive {letter} does not exist or is disconnected."
            try:
                os.startfile(path)
                return f"Opening drive {letter}."
            except Exception as error:
                return _fail(f"open drive {letter}", error)

        folder = _resolve_folder(clean)
        if folder is not None:
            path, label = folder
            if not os.path.isdir(path):
                return f"I found your {label} folder in my list, but it doesn't exist on disk."
            try:
                os.startfile(path)
                return f"Opening your {label} folder."
            except Exception as error:
                return _fail(f"open the {label} folder", error)

        return f"I couldn't find the location for {folder_name}."


        # --- Batch 5: help, windows, theme, window screenshot ---
    @staticmethod
    def pc_help(topic=None) -> str:
        return HELP_TEXT[HELP_ALIASES.get(topic, "general")]

    @staticmethod
    def window_action(action: str) -> str:
        """action: minimize, maximize, restore or close (the window you are using)."""
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            found = _focused_window()
            if found is None:
                return "I can't tell which window you are using."
            hwnd, process, window_class = found
            if window_class in _SHELL_CLASSES:
                return "That's the desktop, not a window."
            user32 = ctypes.windll.user32
            if action == "close":
                if _is_jarvis_window(hwnd, process):
                    return "I won't close my own window."
                user32.PostMessageW(hwnd, 0x0010, 0, 0)    # WM_CLOSE: unsaved work still prompts
                return "Closing the window."
            flag = {"minimize": 6, "maximize": 3, "restore": 9}[action]
            user32.ShowWindow(hwnd, flag)
            return {"minimize": "Window minimized.", "maximize": "Window maximized.",
                    "restore": "Window restored."}[action]
        except Exception as error:
            return _fail(f"{action} the window", error)

    @staticmethod
    def snap_window(side: str) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            user32 = ctypes.windll.user32
            vk = 0x25 if side == "left" else 0x27
            user32.keybd_event(0x5B, 0, 0, 0)              # Win down
            user32.keybd_event(vk, 0, 1, 0)                # arrow keys need the extended flag
            time.sleep(0.05)
            user32.keybd_event(vk, 0, 1 | 2, 0)
            user32.keybd_event(0x5B, 0, 2, 0)              # Win up
            return f"Snapped {side}."
        except Exception as error:
            return _fail(f"snap the window {side}", error)

    @staticmethod
    def set_theme(mode=None) -> str:
        """mode: 'dark', 'light', or None to toggle."""
        if not IS_WINDOWS or winreg is None:
            return _WINDOWS_ONLY
        try:
            path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                try:
                    is_light = winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 1
                except FileNotFoundError:
                    is_light = True
                go_light = (not is_light) if mode is None else (mode == "light")
                for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(go_light))
            # Tell running programs the theme changed so it applies right away.
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "ImmersiveColorSet", 2, 3000, ctypes.byref(ctypes.c_ulong()))
            return "Light mode on." if go_light else "Dark mode on."
        except Exception as error:
            return _fail("change the theme", error)

    @staticmethod
    def screenshot_window() -> str:
        """The window you are using, saved as a PNG in Pictures\\Screenshots."""
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        if ImageGrab is None:
            return "Window screenshots need the pillow library, which isn't installed."
        try:
            found = _focused_window()
            if found is None or found[2] in _SHELL_CLASSES:
                return "I can't tell which window you are using."
            hwnd = found[0]
            ctypes.windll.user32.SetProcessDPIAware()      # real pixels on scaled displays
            rect = ctypes.wintypes.RECT()
            # Extended frame bounds = the visible window without its invisible shadow border.
            if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                    hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                                   all_screens=True)
            folder = _folders()["screenshots"]
            folder.mkdir(parents=True, exist_ok=True)
            image.save(folder / time.strftime("Jarvis window %Y-%m-%d %H-%M-%S.png"))
            return "Window screenshot saved in your Screenshots folder."
        except Exception as error:
            return _fail("take a window screenshot", error)

    # --- 4. Lock & power ---
    @staticmethod
    def lock() -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            ctypes.windll.user32.LockWorkStation()
            return "Locking Windows."
        except Exception as error:
            return _fail("lock the computer", error)


    @staticmethod
    def sleep() -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            # Popen so Jarvis doesn't wait; the PC suspends right after this returns.
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                            creationflags=0x08000000)
            return "Going to sleep."
        except Exception as error:
            return _fail("put the computer to sleep", error)

    @staticmethod
    def _run_shutdown(args):
        return subprocess.run(["shutdown", *args], capture_output=True, timeout=10)

    @classmethod
    def shutdown(cls, seconds: int = 30) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            result = cls._run_shutdown(["/s", "/t", str(int(seconds))])
            if result.returncode == 1190:
                return "A shutdown or restart is already scheduled."
            if result.returncode != 0:
                return "Windows refused the shutdown request."
            return f"Shutting down in {spoken_duration(seconds)}. Say abort to cancel."
        except Exception as error:
            return _fail("shut down the computer", error)

    @classmethod
    def restart(cls, seconds: int = 30) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            result = cls._run_shutdown(["/r", "/t", str(int(seconds))])
            if result.returncode == 1190:
                return "A shutdown or restart is already scheduled."
            if result.returncode != 0:
                return "Windows refused the restart request."
            return f"Restarting in {spoken_duration(seconds)}. Say abort to cancel."
        except Exception as error:
            return _fail("restart the computer", error)

    @classmethod
    def cancel_shutdown(cls) -> str:
        if not IS_WINDOWS:
            return _WINDOWS_ONLY
        try:
            result = cls._run_shutdown(["/a"])
            if result.returncode == 1116:
                return "There is no shutdown to cancel."
            if result.returncode != 0:
                return "I couldn't cancel the shutdown."
            return "Shutdown canceled."
        except Exception as error:
            return _fail("cancel the shutdown", error)

    # --- 5. System resource monitor ---
    @staticmethod
    def get_disk(drive: str = "C") -> str:
        if psutil is None:
            return "System statistics aren't available because a required library is missing."
        try:
            d = psutil.disk_usage(f"{drive.upper()}:\\")
            free = round(d.free / 1024**3)
            total = round(d.total / 1024**3)
            return f"Drive {drive.upper()} has {free} gigabytes free out of {total}."
        except Exception:
            return f"I couldn't read drive {drive.upper()}."

    @staticmethod
    def get_uptime() -> str:
        if psutil is None:
            return "System statistics aren't available because a required library is missing."
        return f"Your computer has been on for {spoken_duration(time.time() - psutil.boot_time())}."

    @staticmethod
    def get_top_app() -> str:
        if psutil is None:
            return "System statistics aren't available because a required library is missing."
        totals = {}
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                name = (p.info["name"] or "").replace(".exe", "")
                totals[name] = totals.get(name, 0) + p.info["memory_info"].rss
            except Exception:
                continue
        name, mem = max(totals.items(), key=lambda kv: kv[1])
        return f"{name} is using the most memory, at {round(mem / 1024**3, 1)} gigabytes."



    @staticmethod
    def get_cpu_info() -> str:
        if psutil is None:
            return "System statistics aren't available because a required library is missing."
        try:
            cores = psutil.cpu_count(logical=False)
            threads = psutil.cpu_count(logical=True)
            if cores:
                text = f"Your processor has {cores} cores and {threads} threads"
            else:
                text = f"Your processor has {threads} threads"
            freq = psutil.cpu_freq()
            if freq and freq.current:
                text += f", running at {freq.current / 1000:.1f} gigahertz"
            return text + "."
        except Exception as error:
            return _fail("read the processor info", error)

    @staticmethod
    def get_process_count() -> str:
        if psutil is None:
            return "System statistics aren't available because a required library is missing."
        try:
            return f"{len(psutil.pids())} processes are running."
        except Exception as error:
            return _fail("count the processes", error)


    @staticmethod
    def get_system_stats(part: str = "all") -> str:
        """part: 'all', 'cpu' or 'ram'."""
        if psutil is None:
            return "System statistics aren't available because a required library is missing."
        print("Getting system information...")
        speech = []
        try:
            if part in ("all", "cpu"):
                cpu = int(round(psutil.cpu_percent(interval=1.0)))
                speech.append(f"CPU is at {cpu} percent.")
            if part in ("all", "ram"):
                ram = psutil.virtual_memory()
                ram_used = round(ram.used / (1024 ** 3), 1)
                speech.append(
                    f"Memory is at {int(round(ram.percent))} percent, using {ram_used} gigabytes."
                )
        except Exception as error:
            return _fail("read the system statistics", error)

        if part == "all" and GPUtil is not None:
            try:
                gpus = GPUtil.getGPUs()
                if gpus:
                    gpu = gpus[0]
                    speech.append(
                        f"Graphics card load is {int(gpu.load * 100)} percent "
                        f"at {int(gpu.temperature)} degrees Celsius."
                    )
            except Exception:
                pass

        if part == "all":
            try:
                speech.append(WindowsSystem.get_cpu_info())
                speech.append(WindowsSystem.get_disk("C"))
                speech.append(WindowsSystem.get_uptime())
                speech.append(WindowsSystem.get_process_count())
                speech.append(WindowsSystem.get_top_app())
            except Exception as error:
                print(f"[system_tools] extra stats failed: {error!r}")

        return " ".join(speech)
