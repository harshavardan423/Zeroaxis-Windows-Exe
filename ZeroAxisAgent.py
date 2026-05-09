#!/usr/bin/env python
# ZeroAxis Windows Agent - Kiosk + Multi-User RBAC
# Compile with: pyinstaller --onefile --noconsole --icon=icon.ico ZeroAxisAgent.py

import sys
import os
import json
import time
import threading
import subprocess
import logging
import ctypes
import winreg
import requests
import psutil
import socket
import platform
from datetime import datetime, date
from pathlib import Path
from typing import List, Dict, Optional
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.request

# ========== Configuration ==========
SERVER_URL  = "https://zeroaxis.live"
STATS_INTERVAL        = 30       # seconds
COMMAND_INTERVAL      = 10       # seconds
POLICY_INTERVAL       = 900      # 15 minutes
APP_USAGE_INTERVAL    = 60       # seconds
DNS_FLUSH_INTERVAL    = 60       # seconds
SCREEN_TIME_INTERVAL  = 300      # push screen time every 5 minutes
# ====================================

# Logging
LOG_FILE = r"C:\ProgramData\ZeroAxis\agent.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def log(msg, level='info'):
    getattr(logging, level)(msg)
    print(msg)

# ========== Serial ==========
def get_serial():
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Cryptography")
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        if guid and guid.strip().upper() not in ('', 'UNKNOWN', 'NONE', 'NULL'):
            return guid.strip()
    except Exception as e:
        log(f"Registry serial failed: {e}", 'error')
    try:
        r = subprocess.run(["wmic", "bios", "get", "serialnumber"],
                           capture_output=True, text=True, timeout=5)
        s = r.stdout.strip().split("\n")[-1].strip()
        if s and s.upper() not in ('UNKNOWN', 'TO BE FILLED BY O.E.M.', 'NONE', ''):
            return s
    except Exception as e:
        log(f"WMIC serial failed: {e}", 'error')
    import uuid
    return str(uuid.uuid4())

# ========== User folder helpers ==========
USER_FILES_BASE = r"C:\ZeroAxisUsers"

def get_user_folder(username: str) -> str:
    """Return and create the dedicated folder for this end user."""
    folder = os.path.join(USER_FILES_BASE, username)
    os.makedirs(folder, exist_ok=True)
    return folder

def open_user_folder(username: str, parent_root=None):
    """No-op — file browser is now embedded in the sidebar."""
    pass

# ========== In-app File Browser ==========
class FileBrowserWindow:
    """A simple in-app file manager — no Explorer involved."""

    ICON = {"folder": "📁", "file": "📄", "image": "🖼", "pdf": "📕",
            "video": "🎬", "audio": "🎵", "zip": "🗜"}

    def __init__(self, root_folder: str, parent=None):
        self.root_folder  = root_folder
        self.current_path = root_folder

        self.win = tk.Toplevel(parent)
        self.win.title("My Files")
        self.win.geometry("820x560")
        self.win.configure(bg="#1a1a2e")
        self.win.grab_set()
        self._build()
        self._load(root_folder)

    # ---------- UI ----------
    def _build(self):
        # ── Top bar ──
        top = tk.Frame(self.win, bg="#206bc4", height=44)
        top.pack(fill="x")
        top.pack_propagate(False)

        tk.Button(top, text="← Back", command=self._go_up,
                  bg="#1a5aad", fg="white", font=("Arial", 10),
                  relief="flat", padx=10, cursor="hand2").pack(side="left", padx=8, pady=7)

        self.path_var = tk.StringVar()
        tk.Label(top, textvariable=self.path_var, fg="white", bg="#206bc4",
                 font=("Arial", 10), anchor="w").pack(side="left", padx=4, fill="x", expand=True)

        tk.Button(top, text="➕ New Folder", command=self._new_folder,
                  bg="#28a745", fg="white", font=("Arial", 10),
                  relief="flat", padx=10, cursor="hand2").pack(side="right", padx=4, pady=7)
        tk.Button(top, text="🗑 Delete", command=self._delete_selected,
                  bg="#dc3545", fg="white", font=("Arial", 10),
                  relief="flat", padx=10, cursor="hand2").pack(side="right", padx=4, pady=7)
        tk.Button(top, text="✏ Rename", command=self._rename_selected,
                  bg="#fd7e14", fg="white", font=("Arial", 10),
                  relief="flat", padx=10, cursor="hand2").pack(side="right", padx=4, pady=7)

        # ── File list (Treeview) ──
        cols = ("icon", "name", "size", "modified")
        self.tree = ttk.Treeview(self.win, columns=cols, show="headings",
                                 selectmode="browse")
        self.tree.heading("icon",     text="")
        self.tree.heading("name",     text="Name")
        self.tree.heading("size",     text="Size")
        self.tree.heading("modified", text="Modified")
        self.tree.column("icon",     width=36,  stretch=False, anchor="center")
        self.tree.column("name",     width=340, stretch=True)
        self.tree.column("size",     width=90,  stretch=False, anchor="e")
        self.tree.column("modified", width=150, stretch=False)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                        background="#16213e", foreground="white",
                        fieldbackground="#16213e", rowheight=28,
                        font=("Arial", 10))
        style.configure("Treeview.Heading",
                        background="#206bc4", foreground="white",
                        font=("Arial", 10, "bold"))
        style.map("Treeview", background=[("selected", "#2563eb")])

        sb = ttk.Scrollbar(self.win, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)

        self.tree.bind("<Double-1>",  self._on_double_click)
        self.tree.bind("<Return>",    self._on_double_click)

        # ── Status bar ──
        self.status_var = tk.StringVar(value="")
        tk.Label(self.win, textvariable=self.status_var,
                 bg="#222", fg="#aaa", font=("Arial", 9),
                 anchor="w").pack(fill="x", side="bottom", padx=6)

    # ---------- Navigation ----------
    def _load(self, path: str):
        if not os.path.isdir(path):
            return
        # Guard: never navigate above the root folder
        try:
            Path(path).relative_to(self.root_folder)
        except ValueError:
            return

        self.current_path = path
        rel = os.path.relpath(path, self.root_folder)
        self.path_var.set("📁  My Files" + ("" if rel == "." else f" / {rel.replace(os.sep, ' / ')}"))

        for row in self.tree.get_children():
            self.tree.delete(row)

        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            self.status_var.set("Permission denied")
            return

        count = 0
        for entry in entries:
            try:
                stat  = entry.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d  %H:%M")
                if entry.is_dir():
                    icon  = self.ICON["folder"]
                    size  = ""
                else:
                    icon  = self._file_icon(entry.name)
                    size  = self._human_size(stat.st_size)
                self.tree.insert("", "end", iid=entry.path,
                                 values=(icon, entry.name, size, mtime))
                count += 1
            except Exception:
                pass
        self.status_var.set(f"{count} item(s)  —  {path}")

    def _go_up(self):
        parent = os.path.dirname(self.current_path)
        self._load(parent)

    def _on_double_click(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        if os.path.isdir(path):
            self._load(path)
        else:
            self._open_file(path)

    # ---------- File actions ----------
    def _open_file(self, path: str):
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror("Open failed", str(e), parent=self.win)

    def _new_folder(self):
        name = self._prompt("New Folder", "Folder name:")
        if not name:
            return
        dest = os.path.join(self.current_path, name)
        try:
            os.makedirs(dest, exist_ok=True)
            self._load(self.current_path)
        except Exception as e:
            messagebox.showerror("Error", str(e), parent=self.win)

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        name = os.path.basename(path)
        if not messagebox.askyesno("Delete", f"Delete '{name}'?", parent=self.win):
            return
        try:
            import shutil
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            self._load(self.current_path)
        except Exception as e:
            messagebox.showerror("Error", str(e), parent=self.win)

    def _rename_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        old_path = sel[0]
        old_name = os.path.basename(old_path)
        new_name = self._prompt("Rename", "New name:", default=old_name)
        if not new_name or new_name == old_name:
            return
        new_path = os.path.join(self.current_path, new_name)
        try:
            os.rename(old_path, new_path)
            self._load(self.current_path)
        except Exception as e:
            messagebox.showerror("Error", str(e), parent=self.win)

    # ---------- Helpers ----------
    @staticmethod
    def _human_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    @staticmethod
    def _file_icon(name: str) -> str:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in ("png","jpg","jpeg","gif","bmp","webp","ico"):  return "🖼"
        if ext == "pdf":                                           return "📕"
        if ext in ("mp4","mkv","avi","mov","wmv"):                return "🎬"
        if ext in ("mp3","wav","aac","flac","ogg"):               return "🎵"
        if ext in ("zip","rar","7z","tar","gz"):                  return "🗜"
        return "📄"

    def _prompt(self, title: str, label: str, default: str = "") -> Optional[str]:
        """Simple modal input dialog."""
        result = [None]
        dlg = tk.Toplevel(self.win)
        dlg.title(title)
        dlg.configure(bg="#1a1a2e")
        dlg.resizable(False, False)
        dlg.grab_set()
        tk.Label(dlg, text=label, fg="white", bg="#1a1a2e",
                 font=("Arial", 11)).pack(padx=20, pady=(16, 4))
        entry = tk.Entry(dlg, width=36, font=("Arial", 11))
        entry.insert(0, default)
        entry.pack(padx=20, pady=4)
        entry.focus()
        entry.select_range(0, "end")

        def _ok(_=None):
            result[0] = entry.get().strip()
            dlg.destroy()

        tk.Button(dlg, text="OK", command=_ok, bg="#206bc4", fg="white",
                  font=("Arial", 11), relief="flat", padx=16, pady=6,
                  cursor="hand2").pack(pady=(8, 16))
        entry.bind("<Return>", _ok)
        dlg.wait_window()
        return result[0]


# ========== Windows lockdown helpers ==========
def apply_lockdown():
    """Disable Task Manager, Win keys, Sticky Keys via registry."""
    try:
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Policies\System")
        winreg.SetValueEx(k, "DisableTaskMgr", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(k)
    except Exception as e:
        log(f"DisableTaskMgr failed: {e}", 'error')
    try:
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer")
        winreg.SetValueEx(k, "NoWinKeys", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(k)
    except Exception as e:
        log(f"NoWinKeys failed: {e}", 'error')
    try:
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
            r"Control Panel\Accessibility\StickyKeys")
        winreg.SetValueEx(k, "Flags", 0, winreg.REG_SZ, "506")
        winreg.CloseKey(k)
    except Exception as e:
        log(f"StickyKeys failed: {e}", 'error')
    log("Lockdown registry keys applied")

def lock_workstation():
    ctypes.windll.user32.LockWorkStation()

def set_wallpaper(image_path):
    try:
        SPI_SETDESKWALLPAPER = 20
        ctypes.windll.user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, image_path, 3)
        log(f"Wallpaper set to {image_path}")
    except Exception as e:
        log(f"Wallpaper failed: {e}", 'error')

# ========== App resolver ==========
COMMON_PATHS = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\Windows\System32",
    r"C:\Windows",
]

def resolve_app_path(app_entry: str) -> Optional[str]:
    """
    Resolve an app entry to a full executable path.
    Supports:
    - Full path: C:\Program Files\Google\Chrome\Application\chrome.exe
    - Exe name:  chrome.exe  (searches common dirs + PATH)
    - Display name from registry: Google Chrome
    """
    app_entry = app_entry.strip()
    if not app_entry:
        return None

    # Already a full path
    if os.path.isabs(app_entry) and app_entry.lower().endswith('.exe'):
        return app_entry if os.path.exists(app_entry) else None

    # Bare exe name — search common dirs
    if app_entry.lower().endswith('.exe'):
        # Check PATH first
        for p in os.environ.get('PATH', '').split(';'):
            full = os.path.join(p.strip(), app_entry)
            if os.path.exists(full):
                return full
        # Search common install dirs recursively (max depth 3)
        for base in COMMON_PATHS:
            for root, dirs, files in os.walk(base):
                depth = root[len(base):].count(os.sep)
                if depth > 3:
                    dirs[:] = []
                    continue
                if app_entry.lower() in [f.lower() for f in files]:
                    return os.path.join(root, app_entry)
        return None

    # Try registry uninstall keys for display name match
    for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
        for sub in [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ]:
            try:
                key = winreg.OpenKey(hive, sub)
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        subkey = winreg.OpenKey(key, winreg.EnumKey(key, i))
                        name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                        if name.strip().lower() == app_entry.lower():
                            try:
                                loc, _ = winreg.QueryValueEx(subkey, "InstallLocation")
                                # Find first exe in install location
                                for f in os.listdir(loc):
                                    if f.lower().endswith('.exe'):
                                        return os.path.join(loc, f)
                            except Exception:
                                pass
                    except Exception:
                        continue
            except Exception:
                continue
    return None

def get_app_display_name(app_entry: str) -> str:
    """Return a friendly display name for the app button."""
    name = os.path.basename(app_entry).replace('.exe', '').replace('_', ' ')
    return name.title()

# ========== Stats collector ==========
def collect_stats(serial: str):
    """Collect device stats and POST to Flask."""
    try:
        stats = {}

        # Battery
        try:
            batt = psutil.sensors_battery()
            if batt:
                stats['battery_level'] = int(batt.percent)
                stats['battery_charging'] = batt.power_plugged
            else:
                stats['battery_level'] = 100  # desktop, no battery
                stats['battery_charging'] = True
        except Exception:
            stats['battery_level'] = 100
            stats['battery_charging'] = True

        # Storage (C: drive)
        try:
            disk = psutil.disk_usage('C:\\')
            stats['storage_free_bytes']  = disk.free
            stats['storage_total_bytes'] = disk.total
        except Exception:
            pass

        # CPU + RAM
        try:
            stats['cpu_usage_pct'] = int(psutil.cpu_percent(interval=1))
            ram = psutil.virtual_memory()
            stats['ram_usage_pct'] = int(ram.percent)
        except Exception:
            pass

        # WiFi SSID + IP
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True, timeout=5
            )
            ssid = ''
            for line in result.stdout.splitlines():
                if 'SSID' in line and 'BSSID' not in line:
                    ssid = line.split(':', 1)[-1].strip()
                    break
            stats['wifi_ssid'] = ssid
        except Exception:
            stats['wifi_ssid'] = ''

        try:
            hostname = socket.gethostname()
            stats['ip_address'] = socket.gethostbyname(hostname)
        except Exception:
            stats['ip_address'] = ''

        # OS info
        stats['os_version'] = platform.version()
        stats['model']      = platform.node()
        stats['make']       = 'Windows'
        stats['status']     = 'online'

        requests.post(
            f"{SERVER_URL}/api/devices/{serial}/stats",
            json=stats, timeout=10
        )
        log(f"Stats posted: cpu={stats.get('cpu_usage_pct')} ram={stats.get('ram_usage_pct')}")
    except Exception as e:
        log(f"collect_stats error: {e}", 'error')

# ========== App usage tracker ==========
class AppUsageTracker:
    def __init__(self, serial: str):
        self.serial          = serial
        self.usage           = {}   # {app_name: minutes}
        self.session_usage   = {}   # {app_name: minutes} since last login
        self.today           = date.today()
        self._lock           = threading.Lock()
        self._active_user    = None   # set by LauncherWindow
        self._session_start  = None   # datetime of login

    def set_active_user(self, username: Optional[str]):
        """Call on login (with username) and logout (with None)."""
        with self._lock:
            if username and username != self._active_user:
                self._session_usage = {}
                self._session_start = datetime.now()
                log(f"AppUsageTracker: session started for {username}")
            elif not username:
                log(f"AppUsageTracker: session ended for {self._active_user}")
            self._active_user = username

    def tick(self):
        """Called every minute — detect foreground process and add 1 min."""
        try:
            today = date.today()
            if today != self.today:
                self._flush()
                with self._lock:
                    self.usage = {}
                    self._session_usage = {}
                self.today = today

            hwnd = ctypes.windll.user32.GetForegroundWindow()
            pid  = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = psutil.Process(pid.value)
            name = proc.name()
            with self._lock:
                self.usage[name] = self.usage.get(name, 0) + 1
                if self._active_user:
                    self._session_usage[name] = self._session_usage.get(name, 0) + 1
        except Exception:
            pass

    def _flush(self):
        """POST device-level usage; also POST per-user usage if a user is logged in."""
        try:
            with self._lock:
                if not self.usage:
                    return
                apps = [
                    {"app_name": k, "package_name": k, "foreground_mins": v}
                    for k, v in self.usage.items()
                ]
                active_user   = self._active_user
                session_apps  = [
                    {"app_name": k, "package_name": k, "foreground_mins": v}
                    for k, v in self._session_usage.items()
                ] if self._session_usage else []

            today_str = self.today.isoformat()

            # Device-level POST (no username — server stores in WindowsAppUsage)
            requests.post(
                f"{SERVER_URL}/api/devices/{self.serial}/app_usage",
                json={"date": today_str, "apps": apps},
                timeout=10
            )
            log(f"Device app usage flushed: {len(apps)} apps")

            # Per-user POST (with username — server attributes to end user)
            if active_user and session_apps:
                requests.post(
                    f"{SERVER_URL}/api/devices/{self.serial}/app_usage",
                    json={"date": today_str, "apps": session_apps, "username": active_user},
                    timeout=10
                )
                log(f"User app usage flushed: {len(session_apps)} apps for {active_user}")

        except Exception as e:
            log(f"App usage flush error: {e}", 'error')

    def flush_now(self):
        self._flush()

    def get_session_minutes(self) -> int:
        """Total minutes used in the current session (for screen time tracking)."""
        with self._lock:
            return sum(self._session_usage.values()) if self._session_usage else 0

# ========== DNS tracker ==========
class DnsTracker:
    """Periodically scrapes DNS client cache and flushes to the server."""
    def __init__(self, serial: str):
        self.serial    = serial
        self._batch    = []
        self._lock     = threading.Lock()
        self._last_seen: set = set()

    def collect(self):
        """Read Windows DNS client cache via ipconfig /displaydns."""
        try:
            result = subprocess.run(
                ['ipconfig', '/displaydns'],
                capture_output=True, text=True, timeout=10
            )
            domains = set()
            for line in result.stdout.splitlines():
                line = line.strip()
                if 'Record Name' in line and ':' in line:
                    domain = line.split(':', 1)[-1].strip().lower().rstrip('.')
                    if domain and '.' in domain and not domain.replace('.', '').isdigit():
                        domains.add(domain)
            new_domains = domains - self._last_seen
            self._last_seen = domains
            if new_domains:
                with self._lock:
                    self._batch.extend(list(new_domains))
                log(f"DNS: collected {len(new_domains)} new domains")
        except Exception as e:
            log(f"DNS collect error: {e}", 'error')

    def flush(self):
        """POST batched domains to the server."""
        with self._lock:
            if not self._batch:
                return
            batch = list(self._batch)
            self._batch.clear()
        try:
            requests.post(
                f"{SERVER_URL}/api/devices/{self.serial}/network_usage",
                json={"dns_domains": batch},
                timeout=10
            )
            log(f"DNS: flushed {len(batch)} domains")
        except Exception as e:
            log(f"DNS flush error: {e}", 'error')


# ========== Command executor ==========
class CommandExecutor:
    def __init__(self, serial: str, on_lock, on_reload_policy):
        self.serial          = serial
        self.on_lock         = on_lock
        self.on_reload_policy = on_reload_policy

    def poll_and_execute(self):
        try:
            r = requests.get(
                f"{SERVER_URL}/api/devices/{self.serial}/pending_commands",
                timeout=10
            )
            if not r.ok:
                return
            cmds = r.json()
            for cmd in cmds:
                cid     = cmd['id']
                command = cmd['command']
                payload = cmd.get('payload', {})
                status  = 'done'
                try:
                    self._execute(command, payload)
                    log(f"Command {command} id={cid} ok")
                except Exception as e:
                    log(f"Command {command} id={cid} failed: {e}", 'error')
                    status = 'failed'
                # Ack
                try:
                    requests.post(
                        f"{SERVER_URL}/api/devices/{self.serial}/command_ack",
                        json={"command_id": cid, "status": status},
                        timeout=5
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"poll_and_execute error: {e}", 'error')

    def _execute(self, command, payload):
        if command == 'lock':
            lock_workstation()

        elif command == 'wipe':
            os.system("shutdown /s /f /t 0")

        elif command == 'message':
            msg = payload.get('text', '')
            # Run in main thread via after() — use a flag
            _pending_messages.append(msg)

        elif command == 'wallpaper':
            url = payload.get('url', '')
            if url:
                dest = r"C:\ProgramData\ZeroAxis\wallpaper.jpg"
                urllib.request.urlretrieve(url, dest)
                set_wallpaper(dest)

        elif command == 'uninstall':
            pkg = payload.get('package') or payload.get('name', '')
            if pkg:
                subprocess.run(
                    f'winget uninstall --name "{pkg}" --silent '
                    f'--accept-source-agreements',
                    shell=True, timeout=60
                )

        elif command == 'install':
            pkg = payload.get('package', '')
            if pkg:
                if pkg.startswith('http'):
                    dest = r"C:\ProgramData\ZeroAxis\install_pkg.exe"
                    urllib.request.urlretrieve(pkg, dest)
                    subprocess.run([dest, '/S', '/silent', '/quiet'],
                                   timeout=120)
                else:
                    subprocess.run(
                        f'winget install --id "{pkg}" --silent '
                        f'--accept-package-agreements '
                        f'--accept-source-agreements',
                        shell=True, timeout=120
                    )

        elif command == 'block_domains':
            domains = payload.get('domains', [])
            _apply_hosts_block(domains)
            log(f"block_domains applied: {len(domains)} domains")

        elif command == 'av_scan':
            scan_type = payload.get('type', 'quick')
            log(f"av_scan command received (type={scan_type}) — not implemented on Windows agent")

        elif command == 'lock':
            lock_workstation()

        elif command == 'reboot':
            os.system('shutdown /r /t 10')

        elif command == 'shell':
            cmd = payload.get('cmd', '')
            if cmd:
                subprocess.Popen(cmd, shell=True)

        else:
            raise Exception(f"Unknown command: {command}")

# Pending messages from command thread → main thread
_pending_messages: List[str] = []

def _apply_hosts_block(domains: List[str]):
    """Write blocked domains to Windows hosts file."""
    hosts_path = r"C:\Windows\System32\drivers\etc\hosts"
    try:
        with open(hosts_path, 'r') as f:
            lines = [l for l in f.readlines()
                     if '# ZeroAxis' not in l and '0.0.0.0' not in l]
        lines.append('\n# ZeroAxis-Blocked-Start\n')
        for d in domains:
            lines.append(f'0.0.0.0 {d}\n')
            lines.append(f'0.0.0.0 www.{d}\n')
        lines.append('# ZeroAxis-Blocked-End\n')
        with open(hosts_path, 'w') as f:
            f.writelines(lines)
        subprocess.run(['ipconfig', '/flushdns'],
                       capture_output=True, timeout=5)
        log(f"Hosts file updated: {len(domains)} domains blocked")
    except Exception as e:
        log(f"Hosts block failed: {e}", 'error')

# ========== Policy ==========
class PolicyEnforcer:
    def __init__(self):
        self.allowed_apps             = []
        self.blocked_apps             = []
        self.kiosk_mode               = False
        self.kiosk_package            = ''
        self.screen_time_limit        = 0
        self.curfew_start             = None
        self.curfew_end               = None
        self.internet_filter          = 'none'
        self.allowed_domains          = []
        self.allowed_browser          = ''
        self.allowed_document_viewer  = ''
        self.today_usage              = 0
        self._last_date               = date.today()

    def apply(self, policies: dict):
        self.allowed_apps            = policies.get('allowed_apps', [])
        self.blocked_apps            = policies.get('blocked_apps', [])
        self.kiosk_mode              = policies.get('kiosk_mode', False)
        self.kiosk_package           = policies.get('kiosk_package', '')
        self.screen_time_limit       = int(policies.get('screen_time_limit_mins', 0) or 0)
        self.curfew_start            = policies.get('curfew_start') or None
        self.curfew_end              = policies.get('curfew_end') or None
        self.internet_filter         = policies.get('internet_filter', 'none')
        self.allowed_domains         = policies.get('allowed_domains', [])
        self.allowed_browser         = policies.get('allowed_browser', '')
        self.allowed_document_viewer = policies.get('allowed_document_viewer', '')
        # Reset daily usage if new day
        today = date.today()
        if today != self._last_date:
            self.today_usage = 0
            self._last_date  = today
        log(f"Policies applied: apps={self.allowed_apps} limit={self.screen_time_limit}")

    def is_curfew_active(self) -> bool:
        if not self.curfew_start or not self.curfew_end:
            return False
        try:
            now   = datetime.now().time()
            start = datetime.strptime(self.curfew_start, '%H:%M').time()
            end   = datetime.strptime(self.curfew_end,   '%H:%M').time()
            # Curfew = locked during these hours
            if start <= end:
                return start <= now <= end
            else:
                # Overnight curfew e.g. 22:00 - 06:00
                return now >= start or now <= end
        except Exception:
            return False

    def screen_time_exceeded(self) -> bool:
        return self.screen_time_limit > 0 and self.today_usage >= self.screen_time_limit

# ========== Login window ==========
BG        = "#0f1117"
BG2       = "#181c27"
CARD      = "#1e2336"
ACCENT    = "#3b82f6"
ACCENT2   = "#1d4ed8"
BORDER    = "#2a3048"
TEXT      = "#f1f5f9"
TEXT2     = "#94a3b8"
SUCCESS   = "#10b981"
DANGER    = "#ef4444"

class LoginWindow:
    def __init__(self, client, on_success):
        self.client     = client
        self.on_success = on_success
        self.root       = tk.Tk()
        self.root.title("ZeroAxis")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self._build()

    def _build(self):
        # Full-screen canvas background
        canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        canvas.place(relwidth=1, relheight=1)
        # Subtle grid lines
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        for x in range(0, sw, 80):
            canvas.create_line(x, 0, x, sh, fill="#1a2035", width=1)
        for y in range(0, sh, 80):
            canvas.create_line(0, y, sw, y, fill="#1a2035", width=1)

        # Center card
        card = tk.Frame(self.root, bg=CARD, bd=0, highlightbackground=BORDER,
                        highlightthickness=1)
        card.place(relx=0.5, rely=0.5, anchor="center", width=420)

        # Top accent bar
        accent_bar = tk.Frame(card, bg=ACCENT, height=4)
        accent_bar.pack(fill="x")

        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", padx=40, pady=36)

        # Logo / wordmark
        logo_frame = tk.Frame(inner, bg=CARD)
        logo_frame.pack(pady=(0, 32))
        tk.Label(logo_frame, text="◈", font=("Segoe UI", 26),
                 fg=ACCENT, bg=CARD).pack()
        tk.Label(logo_frame, text="ZeroAxis", font=("Segoe UI Semibold", 22),
                 fg=TEXT, bg=CARD).pack()
        tk.Label(logo_frame, text="Device Management", font=("Segoe UI", 10),
                 fg=TEXT2, bg=CARD).pack()

        # Fields
        def make_field(parent, label):
            tk.Label(parent, text=label, font=("Segoe UI", 9),
                     fg=TEXT2, bg=CARD, anchor="w").pack(fill="x", pady=(0, 4))
            e = tk.Entry(parent, font=("Segoe UI", 13), bg=BG2,
                         fg=TEXT, insertbackground=TEXT,
                         relief="flat", bd=0,
                         highlightbackground=BORDER,
                         highlightthickness=1,
                         highlightcolor=ACCENT)
            e.pack(fill="x", ipady=10, pady=(0, 18))
            return e

        self.username = make_field(inner, "USERNAME")
        self.pin = make_field(inner, "PIN")
        self.pin.config(show="●")
        self.username.focus()

        # Login button
        self.btn = tk.Button(inner, text="Sign In", command=self._login,
                             bg=ACCENT, fg=TEXT,
                             font=("Segoe UI Semibold", 12),
                             relief="flat", bd=0, cursor="hand2",
                             activebackground=ACCENT2, activeforeground=TEXT)
        self.btn.pack(fill="x", ipady=12)

        self.status = tk.Label(inner, text="", fg=DANGER,
                               bg=CARD, font=("Segoe UI", 10))
        self.status.pack(pady=(14, 0))

        self.username.bind("<Return>", lambda e: self.pin.focus())
        self.pin.bind("<Return>",      lambda e: self._login())

    def _login(self):
        u = self.username.get().strip()
        p = self.pin.get().strip()
        if not u:
            self.status.config(text="Username is required")
            return
        self.btn.config(state="disabled", text="Signing in…")
        self.status.config(text="")
        self.root.update()
        threading.Thread(target=self._do_auth, args=(u, p), daemon=True).start()

    def _do_auth(self, username, pin):
        result = self.client.login(username, pin)
        self.root.after(0, lambda: self._auth_done(result))

    def _auth_done(self, result):
        self.btn.config(state="normal", text="Sign In")
        if result and result.get('success'):
            self.root.destroy()
            self.on_success(result)
        else:
            self.status.config(text="Invalid username or PIN")

    def run(self):
        self.root.mainloop()

# ========== In-app File Browser (panel, not popup) ==========
class FileBrowserPanel:
    """Embedded file browser — renders inside a given parent Frame."""

    def __init__(self, parent: tk.Frame, root_folder: str):
        self.root_folder  = root_folder
        self.current_path = root_folder
        self.frame        = parent
        self._prompt_bar  = None
        self._pending_rename = None
        self._clipboard   = None  # (path, 'copy'|'cut')
        self._build()
        self._load(root_folder)

    def _file_icon(self, name: str) -> str:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        type_map = {
            ("png","jpg","jpeg","gif","bmp","webp","ico"): "IMG",
            ("pdf",):                                      "PDF",
            ("mp4","mkv","avi","mov","wmv"):               "VID",
            ("mp3","wav","aac","flac","ogg"):              "AUD",
            ("zip","rar","7z","tar","gz"):                 "ZIP",
            ("docx","doc","odt"):                          "DOC",
            ("xlsx","xls","csv"):                          "XLS",
            ("pptx","ppt"):                                "PPT",
            ("py","js","ts","html","css","json","xml"):    "CODE",
            ("txt","md","log"):                            "TXT",
        }
        for exts, label in type_map.items():
            if ext in exts:
                return label
        return "FILE"

    @staticmethod
    def _human_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _build(self):
        f = self.frame
        f.configure(bg=BG)

        # ── Top toolbar ──
        toolbar = tk.Frame(f, bg=BG2, height=48)
        toolbar.pack(fill="x", side="top")
        toolbar.pack_propagate(False)

        self.back_btn = tk.Button(
            toolbar, text="←", command=self._go_up,
            bg=BG2, fg=TEXT, font=("Segoe UI", 14),
            relief="flat", bd=0, cursor="hand2",
            padx=16, pady=0,
            activebackground=CARD, activeforeground=TEXT
        )
        self.back_btn.pack(side="left")

        self.path_var = tk.StringVar()
        tk.Label(
            toolbar, textvariable=self.path_var,
            bg=BG2, fg=TEXT2,
            font=("Segoe UI", 10), anchor="w", padx=8
        ).pack(side="left", fill="x", expand=True)

        # Action buttons right side
        for label, cmd, color in [
            ("+ New Folder", self._new_folder,      "#059669"),
            ("Rename",       self._rename_selected,  "#d97706"),
            ("Delete",       self._delete_selected,  DANGER),
        ]:
            tk.Button(
                toolbar, text=label, command=cmd,
                bg=color, fg=TEXT, font=("Segoe UI", 9, "bold"),
                relief="flat", bd=0, cursor="hand2",
                padx=12, pady=0,
                activebackground=BORDER, activeforeground=TEXT
            ).pack(side="right", padx=2, pady=8, ipady=4)

        # ── Divider ──
        tk.Frame(f, bg=BORDER, height=1).pack(fill="x")

        # ── Main area: file list ──
        main = tk.Frame(f, bg=BG)
        main.pack(fill="both", expand=True)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("FB.Treeview",
                        background=BG, foreground=TEXT,
                        fieldbackground=BG, rowheight=36,
                        font=("Segoe UI", 10),
                        borderwidth=0)
        style.configure("FB.Treeview.Heading",
                        background=BG2, foreground=TEXT2,
                        font=("Segoe UI", 9, "bold"),
                        borderwidth=0, relief="flat")
        style.map("FB.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", TEXT)])

        cols = ("icon", "name", "size", "modified")
        self.tree = ttk.Treeview(
            main, columns=cols,
            show="headings", selectmode="browse",
            style="FB.Treeview"
        )
        self.tree.heading("icon",     text="")
        self.tree.heading("name",     text="Name")
        self.tree.heading("size",     text="Size")
        self.tree.heading("modified", text="Modified")
        self.tree.column("icon",     width=60,  stretch=False, anchor="center")
        self.tree.column("name",     width=300, stretch=True)
        self.tree.column("size",     width=80,  stretch=False, anchor="e")
        self.tree.column("modified", width=150, stretch=False)

        vsb = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self.tree.bind("<Double-1>",    self._on_double_click)
        self.tree.bind("<Return>",      self._on_double_click)
        self.tree.bind("<BackSpace>",   lambda e: self._go_up())
        self.tree.bind("<Delete>",      lambda e: self._delete_selected())
        self.tree.bind("<Button-3>",    self._on_right_click)

        # ── Inline prompt area (hidden by default) ──
        self._prompt_frame = tk.Frame(f, bg=CARD,
                                      highlightbackground=ACCENT,
                                      highlightthickness=1)
        # Not packed yet — shown on demand

        # ── Status bar ──
        self.status_var = tk.StringVar(value="")
        tk.Label(
            f, textvariable=self.status_var,
            bg=BG2, fg=TEXT2, font=("Segoe UI", 9),
            anchor="w", padx=12
        ).pack(fill="x", side="bottom", ipady=5)

        # Right-click context menu
        self._ctx_menu = tk.Menu(
            self.tree, tearoff=0,
            bg=CARD, fg=TEXT,
            activebackground=ACCENT, activeforeground=TEXT,
            relief="flat", bd=0
        )
        self._ctx_menu.add_command(label="Open",         command=self._open_selected)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Copy",         command=self._copy_selected)
        self._ctx_menu.add_command(label="Cut",          command=self._cut_selected)
        self._ctx_menu.add_command(label="Paste",        command=self._paste)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Rename",       command=self._rename_selected)
        self._ctx_menu.add_command(label="Delete",       command=self._delete_selected)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="New Folder",   command=self._new_folder)

    # ── Navigation ──────────────────────────────────────────

    def _load(self, path: str):
        try:
            Path(path).relative_to(self.root_folder)
        except ValueError:
            return
        if not os.path.isdir(path):
            return
        self.current_path = path
        rel = os.path.relpath(path, self.root_folder)
        display = "My Files" + ("" if rel == "." else
                                 "  /  " + rel.replace(os.sep, "  /  "))
        self.path_var.set(display)

        for row in self.tree.get_children():
            self.tree.delete(row)

        try:
            entries = sorted(
                os.scandir(path),
                key=lambda e: (not e.is_dir(), e.name.lower())
            )
        except PermissionError:
            self.status_var.set("Permission denied")
            return

        count = 0
        for entry in entries:
            try:
                stat  = entry.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d  %H:%M")
                icon  = "DIR" if entry.is_dir() else self._file_icon(entry.name)
                size  = "" if entry.is_dir() else self._human_size(stat.st_size)
                self.tree.insert("", "end", iid=entry.path,
                                 values=(icon, entry.name, size, mtime))
                count += 1
            except Exception:
                pass

        noun = "items" if count != 1 else "item"
        self.status_var.set(f"{count} {noun}")

    def _go_up(self):
        parent = os.path.dirname(self.current_path)
        self._load(parent)

    def _on_double_click(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        if os.path.isdir(path):
            self._load(path)
        else:
            self._open_file(path)

    def _on_right_click(self, event):
        # Select row under cursor first
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    # ── File actions ────────────────────────────────────────

    def _open_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        if os.path.isdir(path):
            self._load(path)
        else:
            self._open_file(path)

    def _open_file(self, path: str):
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror("Cannot open file", str(e))

    def _copy_selected(self):
        sel = self.tree.selection()
        if sel:
            self._clipboard = (sel[0], 'copy')
            self.status_var.set(f"Copied: {os.path.basename(sel[0])}")

    def _cut_selected(self):
        sel = self.tree.selection()
        if sel:
            self._clipboard = (sel[0], 'cut')
            self.status_var.set(f"Cut: {os.path.basename(sel[0])}")

    def _paste(self):
        if not self._clipboard:
            return
        src, mode = self._clipboard
        dst = os.path.join(self.current_path, os.path.basename(src))
        try:
            import shutil
            if mode == 'copy':
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            else:  # cut
                shutil.move(src, dst)
                self._clipboard = None
            self._load(self.current_path)
        except Exception as e:
            messagebox.showerror("Paste failed", str(e))

    def _new_folder(self):
        self._show_prompt("New folder name:", self._confirm_new_folder)

    def _confirm_new_folder(self, name: str):
        if not name:
            return
        try:
            os.makedirs(os.path.join(self.current_path, name), exist_ok=True)
            self._load(self.current_path)
        except Exception as e:
            self.status_var.set(f"Error: {e}")

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        name = os.path.basename(path)
        if not messagebox.askyesno("Delete", f"Delete '{name}'?\nThis cannot be undone."):
            return
        try:
            import shutil
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            self._load(self.current_path)
        except Exception as e:
            messagebox.showerror("Delete failed", str(e))

    def _rename_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        self._pending_rename = sel[0]
        default = os.path.basename(sel[0])
        self._show_prompt("Rename to:", self._confirm_rename, default=default)

    def _confirm_rename(self, new_name: str):
        old = self._pending_rename
        if not old or not new_name:
            return
        new_path = os.path.join(self.current_path, new_name)
        try:
            os.rename(old, new_path)
            self._load(self.current_path)
        except Exception as e:
            self.status_var.set(f"Error: {e}")

    # ── Inline prompt ────────────────────────────────────────

    def _show_prompt(self, label: str, callback, default: str = ""):
        """Show a non-popup inline prompt bar at the bottom of the panel."""
        self._dismiss_prompt()

        bar = self._prompt_frame
        # Clear old widgets
        for w in bar.winfo_children():
            w.destroy()

        tk.Label(
            bar, text=label, fg=TEXT2, bg=CARD,
            font=("Segoe UI", 10)
        ).pack(side="left", padx=(12, 8), pady=10)

        entry = tk.Entry(
            bar, font=("Segoe UI", 11),
            bg=BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0,
            highlightbackground=BORDER, highlightthickness=1
        )
        entry.insert(0, default)
        entry.pack(side="left", fill="x", expand=True, ipady=7, pady=8)
        entry.select_range(0, "end")
        entry.focus_set()

        def _ok(_=None):
            val = entry.get().strip()
            self._dismiss_prompt()
            if val:
                callback(val)

        def _cancel(_=None):
            self._dismiss_prompt()

        tk.Button(
            bar, text="OK", command=_ok,
            bg=ACCENT, fg=TEXT, font=("Segoe UI Semibold", 10),
            relief="flat", bd=0, padx=16, pady=6, cursor="hand2"
        ).pack(side="left", padx=4, pady=8)

        tk.Button(
            bar, text="✕", command=_cancel,
            bg=CARD, fg=TEXT2, font=("Segoe UI", 10),
            relief="flat", bd=0, padx=10, pady=6, cursor="hand2"
        ).pack(side="left", padx=(0, 8), pady=8)

        entry.bind("<Return>", _ok)
        entry.bind("<Escape>", _cancel)

        # Show the prompt bar above the status bar
        bar.pack(fill="x", side="bottom", before=self.frame.winfo_children()[-1])

    def _dismiss_prompt(self):
        self._prompt_frame.pack_forget()

    EXT_ICONS = {
        ("png","jpg","jpeg","gif","bmp","webp","ico"): "🖼",
        ("pdf",):                                      "📕",
        ("mp4","mkv","avi","mov","wmv"):               "🎬",
        ("mp3","wav","aac","flac","ogg"):              "🎵",
        ("zip","rar","7z","tar","gz"):                 "🗜",
        ("docx","doc","odt"):                          "📝",
        ("xlsx","xls","csv"):                          "📊",
        ("pptx","ppt"):                                "📋",
        ("py","js","ts","html","css","json","xml"):    "💻",
        ("txt","md","log"):                            "📃",
    }

    def __init__(self, parent: tk.Frame, root_folder: str):
        self.root_folder  = root_folder
        self.current_path = root_folder
        self.frame        = parent
        self._build()
        self._load(root_folder)

    def _file_icon(self, name: str) -> str:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        type_map = {
            ("png","jpg","jpeg","gif","bmp","webp","ico"): "IMG",
            ("pdf",):                                      "PDF",
            ("mp4","mkv","avi","mov","wmv"):               "VID",
            ("mp3","wav","aac","flac","ogg"):              "AUD",
            ("zip","rar","7z","tar","gz"):                 "ZIP",
            ("docx","doc","odt"):                          "DOC",
            ("xlsx","xls","csv"):                          "XLS",
            ("pptx","ppt"):                                "PPT",
            ("py","js","ts","html","css","json","xml"):    "CODE",
            ("txt","md","log"):                            "TXT",
        }
        for exts, label in type_map.items():
            if ext in exts:
                return label
        return "FILE"

    @staticmethod
    def _human_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _build(self):
        f = self.frame
        f.configure(bg=BG2)

        # ── Breadcrumb / toolbar row ──
        toolbar = tk.Frame(f, bg=BG2)
        toolbar.pack(fill="x", padx=0, pady=0)

        self.back_btn = tk.Button(toolbar, text="←", command=self._go_up,
                                  bg=CARD, fg=TEXT, font=("Segoe UI", 13),
                                  relief="flat", bd=0, cursor="hand2",
                                  padx=12, pady=6,
                                  activebackground=BORDER, activeforeground=TEXT)
        self.back_btn.pack(side="left", padx=(0, 1))

        self.path_var = tk.StringVar()
        path_label = tk.Label(toolbar, textvariable=self.path_var,
                              bg=CARD, fg=TEXT2,
                              font=("Segoe UI", 10), anchor="w", padx=12)
        path_label.pack(side="left", fill="x", expand=True, ipady=8)

        for label, cmd, color in [
            ("+ Folder", self._new_folder, "#059669"),
            ("Rename",   self._rename_selected, "#d97706"),
            ("Delete",   self._delete_selected, DANGER),
        ]:
            tk.Button(toolbar, text=label, command=cmd,
                      bg=color, fg=TEXT, font=("Segoe UI", 9),
                      relief="flat", bd=0, cursor="hand2",
                      padx=10, pady=6,
                      activebackground=BORDER, activeforeground=TEXT
                      ).pack(side="right", padx=(1, 0))

        # ── File list ──
        list_frame = tk.Frame(f, bg=BG2)
        list_frame.pack(fill="both", expand=True)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("FB.Treeview",
                        background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=32,
                        font=("Segoe UI", 10),
                        borderwidth=0)
        style.configure("FB.Treeview.Heading",
                        background=CARD, foreground=TEXT2,
                        font=("Segoe UI", 9),
                        borderwidth=0, relief="flat")
        style.map("FB.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", TEXT)])

        cols = ("icon", "name", "size", "modified")
        self.tree = ttk.Treeview(list_frame, columns=cols,
                                 show="headings", selectmode="browse",
                                 style="FB.Treeview")
        self.tree.heading("icon",     text="")
        self.tree.heading("name",     text="Name")
        self.tree.heading("size",     text="Size")
        self.tree.heading("modified", text="Modified")
        self.tree.column("icon",     width=50,  stretch=False, anchor="center")
        self.tree.column("name",     width=260, stretch=True)
        self.tree.column("size",     width=80,  stretch=False, anchor="e")
        self.tree.column("modified", width=140, stretch=False)

        vsb = ttk.Scrollbar(list_frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>",   self._on_double_click)

        # ── Status bar ──
        self.status_var = tk.StringVar(value="")
        tk.Label(f, textvariable=self.status_var,
                 bg=CARD, fg=TEXT2, font=("Segoe UI", 9),
                 anchor="w", padx=10).pack(fill="x", side="bottom", ipady=4)

    def _load(self, path: str):
        try:
            Path(path).relative_to(self.root_folder)
        except ValueError:
            return
        if not os.path.isdir(path):
            return
        self.current_path = path
        rel = os.path.relpath(path, self.root_folder)
        display = "My Files" + ("" if rel == "." else
                                 "  /  " + rel.replace(os.sep, "  /  "))
        self.path_var.set(display)
        for row in self.tree.get_children():
            self.tree.delete(row)
        try:
            entries = sorted(os.scandir(path),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            self.status_var.set("Permission denied")
            return
        count = 0
        for entry in entries:
            try:
                stat  = entry.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d  %H:%M")
                icon  = "DIR" if entry.is_dir() else self._file_icon(entry.name)
                size  = "" if entry.is_dir() else self._human_size(stat.st_size)
                self.tree.insert("", "end", iid=entry.path,
                                 values=(icon, entry.name, size, mtime))
                count += 1
            except Exception:
                pass
        self.status_var.set(f"{count} item(s)")

    def _go_up(self):
        self._load(os.path.dirname(self.current_path))

    def _on_double_click(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        if os.path.isdir(path):
            self._load(path)
        else:
            try:
                os.startfile(path)
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _new_folder(self):
        self._inline_prompt("New folder name:", self._confirm_new_folder)

    def _confirm_new_folder(self, name: str):
        if not name:
            return
        try:
            os.makedirs(os.path.join(self.current_path, name), exist_ok=True)
            self._load(self.current_path)
        except Exception as e:
            self._show_inline_error(str(e))

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        if not messagebox.askyesno("Delete", f"Delete '{os.path.basename(path)}'?"):
            return
        try:
            import shutil
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            self._load(self.current_path)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _rename_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        self._pending_rename = sel[0]
        default = os.path.basename(sel[0])
        self._inline_prompt(f"Rename to:", self._confirm_rename, default=default)

    def _confirm_rename(self, new_name: str):
        old = getattr(self, '_pending_rename', None)
        if not old or not new_name:
            return
        try:
            os.rename(old, os.path.join(self.current_path, new_name))
            self._load(self.current_path)
        except Exception as e:
            self._show_inline_error(str(e))

    def _inline_prompt(self, label: str, callback, default: str = ""):
        """Show an inline prompt bar inside the panel — no popup window."""
        # Remove any existing prompt bar
        self._dismiss_inline_prompt()

        bar = tk.Frame(self.frame, bg=CARD,
                       highlightbackground=ACCENT, highlightthickness=1)
        bar.pack(fill="x", side="bottom")
        self._prompt_bar = bar

        tk.Label(bar, text=label, fg=TEXT2, bg=CARD,
                 font=("Segoe UI", 10)).pack(side="left", padx=(12, 8), pady=10)

        entry = tk.Entry(bar, font=("Segoe UI", 11),
                         bg=BG2, fg=TEXT, insertbackground=TEXT,
                         relief="flat", bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
        entry.insert(0, default)
        entry.pack(side="left", fill="x", expand=True, ipady=6, pady=8)
        entry.select_range(0, "end")
        entry.focus()

        def _ok(_=None):
            val = entry.get().strip()
            self._dismiss_inline_prompt()
            callback(val)

        def _cancel(_=None):
            self._dismiss_inline_prompt()

        tk.Button(bar, text="OK", command=_ok,
                  bg=ACCENT, fg=TEXT, font=("Segoe UI Semibold", 10),
                  relief="flat", bd=0, padx=14, pady=6,
                  cursor="hand2").pack(side="left", padx=4, pady=8)
        tk.Button(bar, text="✕", command=_cancel,
                  bg=CARD, fg=TEXT2, font=("Segoe UI", 10),
                  relief="flat", bd=0, padx=10, pady=6,
                  cursor="hand2").pack(side="left", padx=(0, 8), pady=8)

        entry.bind("<Return>", _ok)
        entry.bind("<Escape>", _cancel)

    def _dismiss_inline_prompt(self):
        bar = getattr(self, '_prompt_bar', None)
        if bar and bar.winfo_exists():
            bar.destroy()
        self._prompt_bar = None

    def _show_inline_error(self, msg: str):
        self.status_var.set(f"Error: {msg}")


# ========== Launcher window ==========
class LauncherWindow:
    """Full-screen launcher with sidebar navigation and embedded panels."""

    NAV_ITEMS = [
        ("apps",  "Apps",     "⊞"),
        ("files", "My Files", "📁"),
    ]

    def __init__(self, client, user_data: dict, enforcer: PolicyEnforcer,
                 usage_tracker: AppUsageTracker, serial: str):
        self.client        = client
        self.user_data     = user_data
        self.enforcer      = enforcer
        self.tracker       = usage_tracker
        self.serial        = serial
        self._active_nav   = "apps"
        self._panels: dict = {}
        self._file_panel: Optional[FileBrowserPanel] = None

        self.root = tk.Tk()
        self.root.title("ZeroAxis")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

        username = self.user_data.get('username', '')
        self._user_folder = get_user_folder(username)
        os.environ['ZEROAXIS_USER_HOME'] = self._user_folder

        self.tracker.set_active_user(username)
        self._build()
        self._start_threads()

    # ──────────────────────────────────────────────
    #  Layout
    # ──────────────────────────────────────────────
    def _build(self):
        # Root grid: sidebar | content
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_content_area()

    def _build_sidebar(self):
        sb = tk.Frame(self.root, bg=BG2, width=200)
        sb.grid(row=0, column=0, sticky="ns")
        sb.grid_propagate(False)
        sb.pack_propagate(False)

        # Logo
        logo = tk.Frame(sb, bg=BG2)
        logo.pack(fill="x", pady=(28, 24), padx=20)
        tk.Label(logo, text="◈  ZeroAxis", font=("Segoe UI Semibold", 14),
                 fg=TEXT, bg=BG2).pack(anchor="w")

        # User chip
        uname = self.user_data.get('username', '')
        chip = tk.Frame(sb, bg=CARD)
        chip.pack(fill="x", padx=12, pady=(0, 24))
        tk.Label(chip, text=uname[0].upper() if uname else "?",
                 font=("Segoe UI Semibold", 14),
                 fg=ACCENT, bg=CARD, width=3).pack(side="left", ipady=10)
        tk.Label(chip, text=uname, font=("Segoe UI", 11),
                 fg=TEXT, bg=CARD, anchor="w").pack(side="left", fill="x", expand=True)

        # Divider
        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(0, 12))

        # Nav buttons
        self._nav_btns = {}
        for key, label, icon in self.NAV_ITEMS:
            btn = tk.Button(
                sb,
                text=f"  {icon}   {label}",
                command=lambda k=key: self._switch_nav(k),
                font=("Segoe UI", 11), anchor="w",
                relief="flat", bd=0, cursor="hand2",
                padx=16, pady=10,
            )
            btn.pack(fill="x", padx=8, pady=2)
            self._nav_btns[key] = btn

        # Screen time block
        self.st_frame = tk.Frame(sb, bg=BG2)
        self.st_frame.pack(fill="x", padx=12, pady=(20, 0))
        self.st_label = tk.Label(self.st_frame, text="", font=("Segoe UI", 9),
                                 fg=TEXT2, bg=BG2, justify="left", anchor="w")
        self.st_label.pack(fill="x")
        self.st_bar_bg = tk.Frame(self.st_frame, bg=BORDER, height=4)
        self.st_bar_bg.pack(fill="x", pady=(4, 0))
        self.st_bar_fg = tk.Frame(self.st_bar_bg, bg=ACCENT, height=4)
        self.st_bar_fg.place(relwidth=0, relheight=1)

        # Bottom: refresh + logout
        bottom = tk.Frame(sb, bg=BG2)
        bottom.pack(side="bottom", fill="x", padx=12, pady=16)

        tk.Button(bottom, text="⟳  Refresh",
                  command=self._sync_and_refresh,
                  font=("Segoe UI", 10), anchor="w",
                  bg=BG2, fg=TEXT2, relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=8,
                  activebackground=CARD, activeforeground=TEXT
                  ).pack(fill="x", pady=(0, 4))

        tk.Button(bottom, text="Logout",
                  command=self._logout,
                  font=("Segoe UI", 10), anchor="w",
                  bg=BG2, fg=DANGER, relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=8,
                  activebackground=CARD, activeforeground=DANGER
                  ).pack(fill="x")

        self._update_nav_style()

    def _build_content_area(self):
        self._content = tk.Frame(self.root, bg=BG)
        self._content.grid(row=0, column=1, sticky="nsew")
        self._content.columnconfigure(0, weight=1)
        self._content.rowconfigure(1, weight=1)

        # Top bar
        topbar = tk.Frame(self._content, bg=BG2, height=56)
        topbar.grid(row=0, column=0, sticky="ew")
        topbar.grid_propagate(False)
        self._topbar_title = tk.Label(topbar, text="Apps",
                                      font=("Segoe UI Semibold", 16),
                                      fg=TEXT, bg=BG2)
        self._topbar_title.pack(side="left", padx=24, pady=16)

        self.status_var = tk.StringVar(value="")
        tk.Label(topbar, textvariable=self.status_var,
                 fg=TEXT2, bg=BG2, font=("Segoe UI", 10)
                 ).pack(side="right", padx=24)

        # Panel container
        self._panel_container = tk.Frame(self._content, bg=BG)
        self._panel_container.grid(row=1, column=0, sticky="nsew")
        self._panel_container.columnconfigure(0, weight=1)
        self._panel_container.rowconfigure(0, weight=1)

        self._build_apps_panel()
        self._build_files_panel()
        self._switch_nav("apps")

    def _build_apps_panel(self):
        panel = tk.Frame(self._panel_container, bg=BG)
        self._panels["apps"] = panel

        # Quick-launch row (browser + docs)
        self._quick_frame = tk.Frame(panel, bg=BG)
        self._quick_frame.pack(fill="x", padx=28, pady=(20, 0))

        # Scrollable app grid
        outer = tk.Frame(panel, bg=BG)
        outer.pack(fill="both", expand=True, padx=28, pady=16)

        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._apps_inner = tk.Frame(canvas, bg=BG)
        self._apps_window = canvas.create_window((0, 0), window=self._apps_inner,
                                                  anchor="nw")

        def _on_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(self._apps_window, width=canvas.winfo_width())

        self._apps_inner.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(self._apps_window,
                                                width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(
                            int(-1 * (e.delta / 120)), "units"))

        self._apps_canvas  = canvas
        self._refresh_apps_panel()

    def _build_files_panel(self):
        panel = tk.Frame(self._panel_container, bg=BG2)
        self._panels["files"] = panel
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)

        file_frame = tk.Frame(panel, bg=BG2)
        file_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        file_frame.columnconfigure(0, weight=1)
        file_frame.rowconfigure(0, weight=1)

        username = self.user_data.get('username', '')
        self._file_panel = FileBrowserPanel(file_frame, self._user_folder)

    # ──────────────────────────────────────────────
    #  Apps panel helpers
    # ──────────────────────────────────────────────
    def _refresh_apps_panel(self):
        self._build_quick_buttons()
        self._build_app_grid()

    def _build_quick_buttons(self):
        for w in self._quick_frame.winfo_children():
            w.destroy()
        browser = self.enforcer.allowed_browser
        docs    = self.enforcer.allowed_document_viewer

        def _is_android_pkg(s):
            """Return True if string looks like an Android package name, not a Windows exe."""
            return bool(s) and '.' in s and not s.lower().endswith('.exe') and not os.path.isabs(s) and s.replace('.','').replace('_','').isalpha()

        for label, entry, color in [
            ("🌐  Browser",   browser, "#0ea5e9"),
            ("📄  Documents", docs,    "#8b5cf6"),
        ]:
            if entry and not _is_android_pkg(entry):
                tk.Button(
                    self._quick_frame, text=label,
                    command=lambda e=entry: self._launch(e),
                    font=("Segoe UI", 10),
                    bg=color, fg=TEXT,
                    relief="flat", bd=0, cursor="hand2",
                    padx=16, pady=8,
                    activebackground=BG2, activeforeground=TEXT
                ).pack(side="left", padx=(0, 10))

    def _build_app_grid(self):
        for w in self._apps_inner.winfo_children():
            w.destroy()

        apps = self.enforcer.allowed_apps
        resolved = []
        for entry in apps:
            path = resolve_app_path(entry)
            if path:
                resolved.append((get_app_display_name(entry), path))

        if not resolved:
            tk.Label(self._apps_inner,
                     text="No apps configured for this device.\nContact your administrator.",
                     fg=TEXT2, bg=BG,
                     font=("Segoe UI", 14)).pack(expand=True, pady=60)
            return

        COLS      = 5
        CARD_W    = 148
        CARD_H    = 100
        ICON_SIZE = 22

        for idx, (display_name, path) in enumerate(resolved):
            r, c = divmod(idx, COLS)

            card = tk.Frame(self._apps_inner, bg=CARD, width=CARD_W, height=CARD_H,
                            highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=r, column=c, padx=8, pady=8, sticky="nsew")
            card.grid_propagate(False)

            # App initial letter as icon stand-in
            initial = display_name[0].upper() if display_name else "?"
            icon_lbl = tk.Label(card, text=initial,
                                font=("Segoe UI Semibold", ICON_SIZE),
                                fg=ACCENT, bg=CARD)
            icon_lbl.pack(pady=(20, 4))

            name_lbl = tk.Label(card, text=display_name,
                                font=("Segoe UI", 9),
                                fg=TEXT2, bg=CARD,
                                wraplength=CARD_W - 16)
            name_lbl.pack()

            # Hover effect + click
            def _enter(e, f=card):
                f.configure(bg=BORDER, highlightbackground=ACCENT)
                for child in f.winfo_children():
                    child.configure(bg=BORDER)

            def _leave(e, f=card):
                f.configure(bg=CARD, highlightbackground=BORDER)
                for child in f.winfo_children():
                    child.configure(bg=CARD)

            def _click(e=None, p=path):
                self._launch(p)

            for widget in (card, icon_lbl, name_lbl):
                widget.bind("<Enter>",  _enter)
                widget.bind("<Leave>",  _leave)
                widget.bind("<Button-1>", _click)
                widget.configure(cursor="hand2")

        for col in range(COLS):
            self._apps_inner.columnconfigure(col, weight=1)

    # ──────────────────────────────────────────────
    #  Navigation
    # ──────────────────────────────────────────────
    def _switch_nav(self, key: str):
        self._active_nav = key
        for k, panel in self._panels.items():
            panel.grid_forget()
        self._panels[key].grid(row=0, column=0, sticky="nsew",
                               in_=self._panel_container)
        titles = {"apps": "Apps", "files": "My Files"}
        self._topbar_title.config(text=titles.get(key, ""))
        self._update_nav_style()

    def _update_nav_style(self):
        for key, btn in self._nav_btns.items():
            if key == self._active_nav:
                btn.config(bg=ACCENT, fg=TEXT, activebackground=ACCENT2)
            else:
                btn.config(bg=BG2, fg=TEXT2, activebackground=CARD,
                           activeforeground=TEXT)

    # ──────────────────────────────────────────────
    #  Screen time bar
    # ──────────────────────────────────────────────
    def _update_screen_time_bar(self):
        limit = self.enforcer.screen_time_limit
        used  = self.enforcer.today_usage
        if limit > 0:
            remaining = max(0, limit - used)
            pct = min(1.0, used / limit)
            self.st_label.config(
                text=f"Screen time\n{used} min used · {remaining} min left")
            self.st_bar_fg.place(relwidth=pct, relheight=1)
            # Turn bar red when > 80%
            self.st_bar_fg.config(bg=DANGER if pct > 0.8 else ACCENT)
        else:
            self.st_label.config(text=f"Screen time\n{used} min (no limit)")
            self.st_bar_fg.place(relwidth=0, relheight=1)

    # ──────────────────────────────────────────────
    #  App launch
    # ──────────────────────────────────────────────
    def _launch(self, app_path: str):
        if self.enforcer.screen_time_exceeded():
            messagebox.showwarning("ZeroAxis", "Daily screen time limit reached.")
            self._force_logout()
            return
        if self.enforcer.is_curfew_active():
            messagebox.showwarning("ZeroAxis", "Device is locked during curfew hours.")
            self._force_logout()
            return
        path = resolve_app_path(app_path) or app_path
        try:
            subprocess.Popen(path, shell=True)
            log(f"Launched: {path}")
        except Exception as e:
            messagebox.showerror("ZeroAxis", f"Failed to launch app:\n{e}")

    # ──────────────────────────────────────────────
    #  Logout / sync
    # ──────────────────────────────────────────────
    def _logout(self):
        self.tracker.set_active_user(None)
        self.client.logout()
        self.tracker.flush_now()
        self._restore_env()
        self.root.destroy()

    def _force_logout(self):
        self.tracker.set_active_user(None)
        self.client.logout()
        self.tracker.flush_now()
        self._restore_env()
        self.root.destroy()
        lock_workstation()

    def _restore_env(self):
        os.environ.pop('ZEROAXIS_USER_HOME', None)

    def _sync_and_refresh(self):
        def _sync():
            pol = self.client.sync_policy()
            if pol:
                self.enforcer.apply(pol)
                self.root.after(0, self._refresh_apps_panel)
                self.root.after(0, self._update_screen_time_bar)
        threading.Thread(target=_sync, daemon=True).start()
        self.status_var.set("Syncing…")
        self.root.after(3000, lambda: self.status_var.set(""))

    # ──────────────────────────────────────────────
    #  Background threads
    # ──────────────────────────────────────────────
    def _start_threads(self):
        self._dns_tracker = DnsTracker(self.serial)

        def policy_loop():
            while True:
                time.sleep(POLICY_INTERVAL)
                try:
                    pol = self.client.sync_policy()
                    if pol:
                        self.enforcer.apply(pol)
                        self.root.after(0, self._refresh_apps_panel)
                        self.root.after(0, self._update_screen_time_bar)
                except Exception as e:
                    log(f"Policy sync error: {e}", 'error')
        threading.Thread(target=policy_loop, daemon=True).start()

        def time_loop():
            tick = 0
            while True:
                time.sleep(60)
                tick += 1
                self.enforcer.today_usage += 1
                self.tracker.tick()
                self.root.after(0, self._update_screen_time_bar)
                if tick % 5 == 0:
                    sm = self.tracker.get_session_minutes()
                    threading.Thread(
                        target=self.client.push_screen_time,
                        args=(sm,), daemon=True).start()
                    threading.Thread(
                        target=self.tracker.flush_now, daemon=True).start()
                if self.enforcer.screen_time_exceeded():
                    self.root.after(0, lambda: (
                        messagebox.showwarning("ZeroAxis", "Screen time limit reached."),
                        self._force_logout()))
                    break
                if self.enforcer.is_curfew_active():
                    self.root.after(0, lambda: (
                        messagebox.showwarning("ZeroAxis", "Curfew active. Device locked."),
                        self._force_logout()))
                    break
        threading.Thread(target=time_loop, daemon=True).start()

        def dns_loop():
            while True:
                self._dns_tracker.collect()
                self._dns_tracker.flush()
                time.sleep(DNS_FLUSH_INTERVAL)
        threading.Thread(target=dns_loop, daemon=True).start()

        def msg_loop():
            while True:
                time.sleep(1)
                if _pending_messages:
                    msg = _pending_messages.pop(0)
                    self.root.after(0,
                        lambda m=msg: messagebox.showinfo(
                            "Message from Administrator", m))
        threading.Thread(target=msg_loop, daemon=True).start()

    def run(self):
        self.root.mainloop()

# ========== HTTP client ==========
class ZeroAxisClient:
    def __init__(self, serial: str):
        self.serial          = serial
        self.session         = requests.Session()
        self.active_username = None   # set on successful login, cleared on logout

    def login(self, username: str, pin: str) -> Optional[dict]:
        try:
            r = self.session.post(
                f"{SERVER_URL}/api/enduser/login",
                json={"device_serial": self.serial,
                      "username": username, "pin": pin},
                timeout=10
            )
            if r.ok:
                data = r.json()
                if data.get('success'):
                    self.active_username = username
                    return data
                log(f"Login rejected: {data.get('error')}")
        except Exception as e:
            log(f"Login error: {e}", 'error')
        return None

    def logout(self):
        try:
            self.session.post(
                f"{SERVER_URL}/api/enduser/logout",
                json={"device_serial": self.serial},
                timeout=5
            )
        except Exception:
            pass
        self.active_username = None

    def sync_policy(self) -> Optional[dict]:
        try:
            r = self.session.get(
                f"{SERVER_URL}/api/device/policy/{self.serial}",
                timeout=10
            )
            if r.ok:
                return r.json()
        except Exception as e:
            log(f"sync_policy error: {e}", 'error')
        return None

    def push_screen_time(self, minutes: int):
        if not self.active_username:
            return
        try:
            self.session.post(
                f"{SERVER_URL}/api/enduser/screen_time/{self.serial}",
                json={
                    "username": self.active_username,
                    "date": date.today().isoformat(),
                    "screen_time_mins": minutes,
                },
                timeout=5
            )
        except Exception as e:
            log(f"push_screen_time error: {e}", 'error')

# ========== Background workers (stats + commands) ==========
def start_background_workers(serial: str, enforcer: PolicyEnforcer):
    usage_tracker = AppUsageTracker(serial)
    dns_tracker   = DnsTracker(serial)
    client_cmd    = ZeroAxisClient(serial)
    executor      = CommandExecutor(
        serial,
        on_lock=lock_workstation,
        on_reload_policy=lambda: None
    )

    def stats_loop():
        while True:
            collect_stats(serial)
            time.sleep(STATS_INTERVAL)

    def cmd_loop():
        while True:
            executor.poll_and_execute()
            time.sleep(COMMAND_INTERVAL)

    def usage_loop():
        while True:
            time.sleep(APP_USAGE_INTERVAL)
            usage_tracker.tick()
            usage_tracker.flush_now()

    def dns_bg_loop():
        """Collect DNS even when no user is logged in (device-level baseline)."""
        while True:
            dns_tracker.collect()
            dns_tracker.flush()
            time.sleep(DNS_FLUSH_INTERVAL)

    threading.Thread(target=stats_loop,  daemon=True).start()
    threading.Thread(target=cmd_loop,    daemon=True).start()
    threading.Thread(target=usage_loop,  daemon=True).start()
    threading.Thread(target=dns_bg_loop, daemon=True).start()

    return usage_tracker

# ========== Main ==========
def main():
    serial = get_serial()
    if not serial:
        log("Could not determine device serial — exiting", 'error')
        sys.exit(1)
    log(f"ZeroAxis Agent starting. Serial={serial}")

    # Apply lockdown registry keys
    apply_lockdown()

    # Start stats + command workers immediately (device shows online even on login screen)
    enforcer      = PolicyEnforcer()
    usage_tracker = start_background_workers(serial, enforcer)
    client        = ZeroAxisClient(serial)

    # Main loop: login → launcher → back to login
    while True:
        login_result = [None]
        done_event   = threading.Event()

        def on_login_success(data):
            login_result[0] = data
            done_event.set()

        login_win = LoginWindow(client, on_login_success)
        login_win.run()

        if login_result[0] is None:
            # Login window closed without success — restart
            continue

        # Apply policies from login response
        policies = login_result[0].get('policies', {})
        enforcer.apply(policies)

        launcher = LauncherWindow(
            client, login_result[0], enforcer, usage_tracker, serial
        )
        launcher.run()
        # When launcher window closes (logout/lock), loop restarts → login screen

if __name__ == "__main__":
    main()