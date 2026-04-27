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
STATS_INTERVAL   = 30       # seconds
COMMAND_INTERVAL = 10       # seconds
POLICY_INTERVAL  = 900      # 15 minutes
APP_USAGE_INTERVAL = 60     # seconds
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
        self.serial  = serial
        self.usage   = {}   # {app_name: minutes}
        self.today   = date.today()
        self._lock   = threading.Lock()

    def tick(self):
        """Called every minute — detect foreground process and add 1 min."""
        try:
            today = date.today()
            if today != self.today:
                self._flush()
                self.usage = {}
                self.today = today

            # Get foreground window process name
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            pid  = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = psutil.Process(pid.value)
            name = proc.name()
            with self._lock:
                self.usage[name] = self.usage.get(name, 0) + 1
        except Exception:
            pass

    def _flush(self):
        """POST today's usage to Flask."""
        try:
            with self._lock:
                if not self.usage:
                    return
                apps = [
                    {"app_name": k, "package_name": k, "foreground_mins": v}
                    for k, v in self.usage.items()
                ]
            payload = {
                "date": self.today.isoformat(),
                "apps": apps
            }
            requests.post(
                f"{SERVER_URL}/api/devices/{self.serial}/app_usage",
                json=payload, timeout=10
            )
            log(f"App usage flushed: {len(apps)} apps")
        except Exception as e:
            log(f"App usage flush error: {e}", 'error')

    def flush_now(self):
        self._flush()

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
class LoginWindow:
    def __init__(self, client, on_success):
        self.client     = client
        self.on_success = on_success
        self.root       = tk.Tk()
        self.root.title("ZeroAxis")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)  # prevent close
        self._build()

    def _build(self):
        frame = tk.Frame(self.root, bg="#1a1a2e")
        frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(frame, text="ZeroAxis", font=("Arial", 28, "bold"),
                 fg="white", bg="#1a1a2e").pack(pady=(0, 4))
        tk.Label(frame, text="Sign in to continue", font=("Arial", 11),
                 fg="#aaa", bg="#1a1a2e").pack(pady=(0, 24))

        tk.Label(frame, text="Username", fg="white",
                 bg="#1a1a2e", font=("Arial", 10)).pack(anchor="w")
        self.username = tk.Entry(frame, width=32, font=("Arial", 13))
        self.username.pack(pady=(2, 12))
        self.username.focus()

        tk.Label(frame, text="PIN", fg="white",
                 bg="#1a1a2e", font=("Arial", 10)).pack(anchor="w")
        self.pin = tk.Entry(frame, width=32, font=("Arial", 13), show="●")
        self.pin.pack(pady=(2, 20))

        self.btn = tk.Button(frame, text="Login", command=self._login,
                             bg="#206bc4", fg="white", font=("Arial", 12, "bold"),
                             relief="flat", padx=20, pady=8, cursor="hand2",
                             activebackground="#1a5aad", activeforeground="white")
        self.btn.pack(fill="x")

        self.status = tk.Label(frame, text="", fg="#e74c3c",
                               bg="#1a1a2e", font=("Arial", 10))
        self.status.pack(pady=(10, 0))

        self.username.bind("<Return>", lambda e: self.pin.focus())
        self.pin.bind("<Return>",      lambda e: self._login())

    def _login(self):
        u = self.username.get().strip()
        p = self.pin.get().strip()
        if not u:
            self.status.config(text="Username required")
            return
        self.btn.config(state="disabled", text="Signing in...")
        self.status.config(text="")
        self.root.update()
        threading.Thread(target=self._do_auth, args=(u, p), daemon=True).start()

    def _do_auth(self, username, pin):
        result = self.client.login(username, pin)
        self.root.after(0, lambda: self._auth_done(result))

    def _auth_done(self, result):
        self.btn.config(state="normal", text="Login")
        if result and result.get('success'):
            self.root.destroy()
            self.on_success(result)
        else:
            self.status.config(text="Invalid username or PIN")

    def run(self):
        self.root.mainloop()

# ========== Launcher window ==========
class LauncherWindow:
    def __init__(self, client, user_data: dict, enforcer: PolicyEnforcer,
                 usage_tracker: AppUsageTracker, serial: str):
        self.client        = client
        self.user_data     = user_data
        self.enforcer      = enforcer
        self.tracker       = usage_tracker
        self.serial        = serial
        self.root          = tk.Tk()
        self.root.title("ZeroAxis Launcher")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self._build()
        self._start_threads()

    def _build(self):
        # Top bar
        top = tk.Frame(self.root, bg="#206bc4", height=48)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text=f"Welcome, {self.user_data.get('username', '')}",
                 fg="white", bg="#206bc4",
                 font=("Arial", 13, "bold")).pack(side="left", padx=16)
        tk.Button(top, text="Logout", command=self._logout,
                  bg="#dc3545", fg="white", font=("Arial", 10),
                  relief="flat", padx=12, cursor="hand2").pack(side="right", padx=12, pady=8)
        tk.Button(top, text="⟳ Refresh", command=self._sync_and_refresh,
                  bg="#444", fg="white", font=("Arial", 10),
                  relief="flat", padx=10, cursor="hand2").pack(side="right", pady=8)

        # Quick launch buttons (browser + docs)
        self.quick_frame = tk.Frame(self.root, bg="#1a1a2e")
        self.quick_frame.pack(fill="x", padx=16, pady=(10, 0))

        # Screen time bar
        self.time_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.time_var,
                 bg="#1a1a2e", fg="#aaa", font=("Arial", 10)).pack(pady=(4, 0))
        self.progress = ttk.Progressbar(self.root, length=400, mode='determinate')
        self.progress.pack(pady=(2, 8))

        # Apps grid
        self.apps_frame = tk.Frame(self.root, bg="#1a1a2e")
        self.apps_frame.pack(fill="both", expand=True, padx=24, pady=8)

        # Status bar
        self.status_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.status_var,
                 bg="#222", fg="#aaa", font=("Arial", 9),
                 anchor="w").pack(side="bottom", fill="x", padx=8)

        self._refresh_ui()

    def _refresh_ui(self):
        self._build_quick_buttons()
        self._build_app_grid()
        self._update_screen_time_bar()

    def _build_quick_buttons(self):
        for w in self.quick_frame.winfo_children():
            w.destroy()
        browser = self.enforcer.allowed_browser
        docs    = self.enforcer.allowed_document_viewer
        if browser:
            tk.Button(self.quick_frame, text="🌐 Browser",
                      command=lambda: self._launch(browser),
                      bg="#2E86AB", fg="white", font=("Arial", 10),
                      relief="flat", padx=14, pady=6,
                      cursor="hand2").pack(side="left", padx=(0, 8))
        if docs:
            tk.Button(self.quick_frame, text="📄 Documents",
                      command=lambda: self._launch(docs),
                      bg="#2E86AB", fg="white", font=("Arial", 10),
                      relief="flat", padx=14, pady=6,
                      cursor="hand2").pack(side="left")

    def _build_app_grid(self):
        for w in self.apps_frame.winfo_children():
            w.destroy()

        apps = self.enforcer.allowed_apps
        # Filter to only entries that resolve on this platform
        resolved = []
        for entry in apps:
            path = resolve_app_path(entry)
            if path:
                resolved.append((get_app_display_name(entry), path))

        if not resolved:
            tk.Label(self.apps_frame,
                     text="No apps configured for this device.\nContact your administrator.",
                     fg="white", bg="#1a1a2e",
                     font=("Arial", 14)).pack(expand=True)
            return

        cols = 4
        for idx, (display_name, path) in enumerate(resolved):
            r, c = divmod(idx, cols)
            btn = tk.Button(
                self.apps_frame,
                text=display_name,
                command=lambda p=path: self._launch(p),
                bg="#2563eb", fg="white",
                font=("Arial", 12), width=18, height=2,
                relief="flat", cursor="hand2",
                activebackground="#1a4ec4"
            )
            btn.grid(row=r, column=c, padx=10, pady=10, sticky="nsew")

        for c in range(cols):
            self.apps_frame.columnconfigure(c, weight=1)
        rows_count = (len(resolved) - 1) // cols + 1
        for r in range(rows_count):
            self.apps_frame.rowconfigure(r, weight=1)

    def _update_screen_time_bar(self):
        limit = self.enforcer.screen_time_limit
        used  = self.enforcer.today_usage
        if limit > 0:
            remaining = max(0, limit - used)
            pct = min(100, int(used * 100 / limit))
            self.time_var.set(f"Screen time: {used} min used / {limit} min limit  ({remaining} min remaining)")
            self.progress['value'] = pct
            self.progress.pack()
        else:
            self.time_var.set(f"Screen time: {used} min (no limit)")
            self.progress.pack_forget()

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

    def _logout(self):
        self.client.logout()
        self.tracker.flush_now()
        self.root.destroy()

    def _force_logout(self):
        self.client.logout()
        self.tracker.flush_now()
        self.root.destroy()
        lock_workstation()

    def _sync_and_refresh(self):
        def _sync():
            pol = self.client.sync_policy()
            if pol:
                self.enforcer.apply(pol)
                self.root.after(0, self._refresh_ui)
        threading.Thread(target=_sync, daemon=True).start()
        self.status_var.set("Syncing policies...")

    def _start_threads(self):
        # Policy sync every 15 min
        def policy_loop():
            while True:
                time.sleep(POLICY_INTERVAL)
                try:
                    pol = self.client.sync_policy()
                    if pol:
                        self.enforcer.apply(pol)
                        self.root.after(0, self._refresh_ui)
                except Exception as e:
                    log(f"Policy sync error: {e}", 'error')
        threading.Thread(target=policy_loop, daemon=True).start()

        # Screen time + curfew check every minute
        def time_loop():
            while True:
                time.sleep(60)
                self.enforcer.today_usage += 1
                self.tracker.tick()
                self.root.after(0, self._update_screen_time_bar)
                if self.enforcer.screen_time_exceeded():
                    self.root.after(0, lambda: (
                        messagebox.showwarning("ZeroAxis", "Screen time limit reached."),
                        self._force_logout()
                    ))
                    break
                if self.enforcer.is_curfew_active():
                    self.root.after(0, lambda: (
                        messagebox.showwarning("ZeroAxis", "Curfew active. Device locked."),
                        self._force_logout()
                    ))
                    break
        threading.Thread(target=time_loop, daemon=True).start()

        # Pending messages from command thread
        def msg_loop():
            while True:
                time.sleep(1)
                if _pending_messages:
                    msg = _pending_messages.pop(0)
                    self.root.after(0, lambda m=msg: messagebox.showinfo("Message from Administrator", m))
        threading.Thread(target=msg_loop, daemon=True).start()

    def run(self):
        self.root.mainloop()

# ========== HTTP client ==========
class ZeroAxisClient:
    def __init__(self, serial: str):
        self.serial  = serial
        self.session = requests.Session()

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

# ========== Background workers (stats + commands) ==========
def start_background_workers(serial: str, enforcer: PolicyEnforcer):
    usage_tracker = AppUsageTracker(serial)
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

    threading.Thread(target=stats_loop, daemon=True).start()
    threading.Thread(target=cmd_loop,   daemon=True).start()
    threading.Thread(target=usage_loop, daemon=True).start()

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