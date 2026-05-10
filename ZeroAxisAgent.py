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
SERVER_URL            = "https://zeroaxis.live"
STATS_INTERVAL        = 30
COMMAND_INTERVAL      = 10
POLICY_INTERVAL       = 900
APP_USAGE_INTERVAL    = 60
DNS_FLUSH_INTERVAL    = 60
SCREEN_TIME_INTERVAL  = 300
# ====================================

LOG_FILE = r"C:\ProgramData\ZeroAxis\agent.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def log(msg, level='info'):
    getattr(logging, level)(msg)
    print(msg)

# ========== Design tokens (matching ThreatMind) ==========
BG        = "#f0f4f8"       # light background (main)
BG_DARK   = "#f0f4f8"
BG_CARD   = "#ffffff"
BG_SIDEBAR= "#ffffff"
BG_HOVER  = "#e8eef5"
ACCENT    = "#0066cc"
ACCENT2   = "#0052a3"
SUCCESS   = "#00875a"
WARNING   = "#b86800"
DANGER    = "#cc1f3a"
BORDER    = "#c8d4de"
TEXT      = "#0d1117"
TEXT2     = "#5a6a7a"
TEXT_DIM  = "#8a9baa"
CARD_HL   = "#dce8f5"

# Dark variants for login screen
D_BG      = "#0f1117"
D_BG2     = "#181c27"
D_CARD    = "#1e2336"
D_BORDER  = "#2a3048"
D_TEXT    = "#f1f5f9"
D_TEXT2   = "#94a3b8"
D_ACCENT  = "#3b82f6"
D_ACCENT2 = "#1d4ed8"
D_DANGER  = "#ef4444"

FONT_MONO = "Consolas"
FONT_UI   = "Segoe UI"

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
    folder = os.path.join(USER_FILES_BASE, username)
    os.makedirs(folder, exist_ok=True)
    return folder

# ========== UI helpers ==========
def styled_button(parent, text, cmd, color=ACCENT2, fg=BG_CARD, width=None, font_size=10):
    kw = dict(
        bg=color, fg=fg,
        font=(FONT_MONO, font_size, "bold"),
        relief="flat", cursor="hand2",
        activebackground=ACCENT, activeforeground=fg,
        bd=0, padx=16, pady=8, command=cmd
    )
    if width:
        kw["width"] = width
    btn = tk.Button(parent, text=text, **kw)
    btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT))
    btn.bind("<Leave>", lambda e: btn.config(bg=color))
    return btn

def section_label(parent, text, bg=BG_DARK):
    tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=30, pady=(16, 8))
    tk.Label(parent, text=text, font=(FONT_MONO, 9, "bold"),
             fg=ACCENT, bg=bg).pack(anchor="w", padx=30, pady=(0, 8))

# ========== Windows lockdown helpers ==========
def apply_lockdown():
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
    app_entry = app_entry.strip()
    if not app_entry:
        return None
    if os.path.isabs(app_entry) and app_entry.lower().endswith('.exe'):
        return app_entry if os.path.exists(app_entry) else None
    if app_entry.lower().endswith('.exe'):
        for p in os.environ.get('PATH', '').split(';'):
            full = os.path.join(p.strip(), app_entry)
            if os.path.exists(full):
                return full
        for base in COMMON_PATHS:
            for root, dirs, files in os.walk(base):
                depth = root[len(base):].count(os.sep)
                if depth > 3:
                    dirs[:] = []
                    continue
                if app_entry.lower() in [f.lower() for f in files]:
                    return os.path.join(root, app_entry)
        return None
    app_entry_display = app_entry[:-4] if app_entry.lower().endswith('.exe') else app_entry
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
                        if name.strip().lower() == app_entry_display.lower():
                            try:
                                loc, _ = winreg.QueryValueEx(subkey, "InstallLocation")
                                if loc and os.path.isdir(loc):
                                    best = None
                                    for root, dirs, files in os.walk(loc):
                                        depth = root[len(loc):].count(os.sep)
                                        if depth > 3:
                                            dirs[:] = []
                                            continue
                                        for f in files:
                                            fl = f.lower()
                                            if not fl.endswith('.exe'):
                                                continue
                                            if any(x in fl for x in ('uninstall', 'setup', 'update', 'helper', 'installer', 'redist', 'scanner', 'swapper', 'connector', 'relaunch')):
                                                continue
                                            f_no_ext = fl[:-4]
                                            if f_no_ext == app_entry_display.lower():
                                                return os.path.join(root, f)
                                            if best is None:
                                                best = os.path.join(root, f)
                                    if best:
                                        return best
                            except Exception:
                                pass
                    except Exception:
                        continue
            except Exception:
                continue
    return None

def get_app_display_name(app_entry: str) -> str:
    base = os.path.basename(app_entry)
    display = base[:-4] if base.lower().endswith('.exe') else base
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
                        reg_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                        if reg_name.strip().lower() == display.lower():
                            return reg_name.strip()
                    except Exception:
                        continue
            except Exception:
                continue
    return display.replace('_', ' ')

# ========== Stats collector ==========
def collect_stats(serial: str):
    try:
        stats = {}
        try:
            batt = psutil.sensors_battery()
            if batt:
                stats['battery_level'] = int(batt.percent)
                stats['battery_charging'] = batt.power_plugged
            else:
                stats['battery_level'] = 100
                stats['battery_charging'] = True
        except Exception:
            stats['battery_level'] = 100
            stats['battery_charging'] = True
        try:
            disk = psutil.disk_usage('C:\\')
            stats['storage_free_bytes']  = disk.free
            stats['storage_total_bytes'] = disk.total
        except Exception:
            pass
        try:
            stats['cpu_usage_pct'] = int(psutil.cpu_percent(interval=1))
            ram = psutil.virtual_memory()
            stats['ram_usage_pct'] = int(ram.percent)
        except Exception:
            pass
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
        stats['os_version'] = platform.version()
        stats['model']      = platform.node()
        stats['make']       = 'Windows'
        stats['status']     = 'online'
        requests.post(f"{SERVER_URL}/api/devices/{serial}/stats", json=stats, timeout=10)
        log(f"Stats posted: cpu={stats.get('cpu_usage_pct')} ram={stats.get('ram_usage_pct')}")
    except Exception as e:
        log(f"collect_stats error: {e}", 'error')

# ========== App usage tracker ==========
class AppUsageTracker:
    def __init__(self, serial: str):
        self.serial         = serial
        self.usage          = {}
        self._session_usage = {}
        self.today          = date.today()
        self._lock          = threading.Lock()
        self._active_user   = None
        self._session_start = None

    def set_active_user(self, username: Optional[str]):
        with self._lock:
            if username and username != self._active_user:
                self._session_usage = {}
                self._session_start = datetime.now()
                log(f"AppUsageTracker: session started for {username}")
            elif not username:
                log(f"AppUsageTracker: session ended for {self._active_user}")
            self._active_user = username

    def tick(self):
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
        try:
            with self._lock:
                if not self.usage:
                    return
                apps = [{"app_name": k, "package_name": k, "foreground_mins": v}
                        for k, v in self.usage.items()]
                active_user  = self._active_user
                session_apps = [{"app_name": k, "package_name": k, "foreground_mins": v}
                                for k, v in self._session_usage.items()] if self._session_usage else []
            today_str = self.today.isoformat()
            requests.post(f"{SERVER_URL}/api/devices/{self.serial}/app_usage",
                          json={"date": today_str, "apps": apps}, timeout=10)
            if active_user and session_apps:
                requests.post(f"{SERVER_URL}/api/devices/{self.serial}/app_usage",
                              json={"date": today_str, "apps": session_apps, "username": active_user},
                              timeout=10)
        except Exception as e:
            log(f"App usage flush error: {e}", 'error')

    def flush_now(self):
        self._flush()

    def get_session_minutes(self) -> int:
        with self._lock:
            return sum(self._session_usage.values()) if self._session_usage else 0

# ========== DNS tracker ==========
class DnsTracker:
    def __init__(self, serial: str):
        self.serial     = serial
        self._batch     = []
        self._lock      = threading.Lock()
        self._last_seen: set = set()

    def collect(self):
        try:
            result = subprocess.run(['ipconfig', '/displaydns'],
                                    capture_output=True, text=True, timeout=10)
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
        except Exception as e:
            log(f"DNS collect error: {e}", 'error')

    def flush(self):
        with self._lock:
            if not self._batch:
                return
            batch = list(self._batch)
            self._batch.clear()
        try:
            requests.post(f"{SERVER_URL}/api/devices/{self.serial}/network_usage",
                          json={"dns_domains": batch}, timeout=10)
        except Exception as e:
            log(f"DNS flush error: {e}", 'error')

# ========== Command executor ==========
_pending_messages: List[str] = []

def _apply_hosts_block(domains: List[str]):
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
        subprocess.run(['ipconfig', '/flushdns'], capture_output=True, timeout=5)
        log(f"Hosts file updated: {len(domains)} domains blocked")
    except Exception as e:
        log(f"Hosts block failed: {e}", 'error')

class CommandExecutor:
    def __init__(self, serial: str, on_lock, on_reload_policy):
        self.serial           = serial
        self.on_lock          = on_lock
        self.on_reload_policy = on_reload_policy

    def poll_and_execute(self):
        try:
            r = requests.get(f"{SERVER_URL}/api/devices/{self.serial}/pending_commands", timeout=10)
            if not r.ok:
                return
            for cmd in r.json():
                cid     = cmd['id']
                command = cmd['command']
                payload = cmd.get('payload', {})
                status  = 'done'
                try:
                    self._execute(command, payload)
                except Exception as e:
                    log(f"Command {command} id={cid} failed: {e}", 'error')
                    status = 'failed'
                try:
                    requests.post(f"{SERVER_URL}/api/devices/{self.serial}/command_ack",
                                  json={"command_id": cid, "status": status}, timeout=5)
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
            _pending_messages.append(payload.get('text', ''))
        elif command == 'wallpaper':
            url = payload.get('url', '')
            if url:
                dest = r"C:\ProgramData\ZeroAxis\wallpaper.jpg"
                urllib.request.urlretrieve(url, dest)
                set_wallpaper(dest)
        elif command == 'uninstall':
            pkg = payload.get('package') or payload.get('name', '')
            if pkg:
                subprocess.run(f'winget uninstall --name "{pkg}" --silent --accept-source-agreements',
                               shell=True, timeout=60)
        elif command == 'install':
            pkg = payload.get('package', '')
            if pkg:
                if pkg.startswith('http'):
                    dest = r"C:\ProgramData\ZeroAxis\install_pkg.exe"
                    urllib.request.urlretrieve(pkg, dest)
                    subprocess.run([dest, '/S', '/silent', '/quiet'], timeout=120)
                else:
                    subprocess.run(f'winget install --id "{pkg}" --silent --accept-package-agreements --accept-source-agreements',
                                   shell=True, timeout=120)
        elif command == 'block_domains':
            _apply_hosts_block(payload.get('domains', []))
        elif command == 'av_scan':
            log(f"av_scan command received (type={payload.get('type','quick')}) — handled by ThreatMind")
        elif command == 'reboot':
            os.system('shutdown /r /t 10')
        elif command == 'shell':
            cmd = payload.get('cmd', '')
            if cmd:
                subprocess.Popen(cmd, shell=True)
        else:
            raise Exception(f"Unknown command: {command}")

# ========== Policy ==========
class PolicyEnforcer:
    def __init__(self):
        self.allowed_apps            = []
        self.blocked_apps            = []
        self.kiosk_mode              = False
        self.kiosk_package           = ''
        self.screen_time_limit       = 0
        self.curfew_start            = None
        self.curfew_end              = None
        self.internet_filter         = 'none'
        self.allowed_domains         = []
        self.allowed_browser         = ''
        self.allowed_document_viewer = ''
        self.today_usage             = 0
        self._last_date              = date.today()

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
        today = date.today()
        if today != self._last_date:
            self.today_usage = 0
            self._last_date  = today

    def is_curfew_active(self) -> bool:
        if not self.curfew_start or not self.curfew_end:
            return False
        try:
            now   = datetime.now().time()
            start = datetime.strptime(self.curfew_start, '%H:%M').time()
            end   = datetime.strptime(self.curfew_end,   '%H:%M').time()
            if start <= end:
                return start <= now <= end
            else:
                return now >= start or now <= end
        except Exception:
            return False

    def screen_time_exceeded(self) -> bool:
        return self.screen_time_limit > 0 and self.today_usage >= self.screen_time_limit


# ═══════════════════════════════════════════════════════════════════════
# LOGIN WINDOW  (dark, full-screen — same as before, just consistent)
# ═══════════════════════════════════════════════════════════════════════
class LoginWindow:
    def __init__(self, client, on_success):
        self.client     = client
        self.on_success = on_success
        self.root       = tk.Tk()
        self.root.title("ZeroAxis")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg=D_BG)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self._build()

    def _build(self):
        canvas = tk.Canvas(self.root, bg=D_BG, highlightthickness=0)
        canvas.place(relwidth=1, relheight=1)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # Grid lines
        for x in range(0, sw, 80):
            canvas.create_line(x, 0, x, sh, fill="#1a2035", width=1)
        for y in range(0, sh, 80):
            canvas.create_line(0, y, sw, y, fill="#1a2035", width=1)

        # Center card
        card = tk.Frame(self.root, bg=D_CARD, bd=0,
                        highlightbackground=D_BORDER, highlightthickness=1)
        card.place(relx=0.5, rely=0.5, anchor="center", width=440)

        # Top accent bar
        tk.Frame(card, bg=D_ACCENT, height=4).pack(fill="x")

        inner = tk.Frame(card, bg=D_CARD)
        inner.pack(fill="both", padx=44, pady=40)

        # Logo
        logo_f = tk.Frame(inner, bg=D_CARD)
        logo_f.pack(pady=(0, 36))
        tk.Label(logo_f, text="◈", font=(FONT_MONO, 28, "bold"),
                 fg=D_ACCENT, bg=D_CARD).pack()
        tk.Label(logo_f, text="ZEROAXIS", font=(FONT_MONO, 20, "bold"),
                 fg=D_TEXT, bg=D_CARD).pack()
        tk.Label(logo_f, text="SECURE WORKSTATION", font=(FONT_MONO, 9),
                 fg=D_TEXT2, bg=D_CARD).pack()

        def make_field(label_text, show=""):
            tk.Label(inner, text=label_text, font=(FONT_MONO, 8, "bold"),
                     fg=D_TEXT2, bg=D_CARD, anchor="w").pack(fill="x", pady=(0, 4))
            e = tk.Entry(inner, font=(FONT_MONO, 13), bg=D_BG2,
                         fg=D_TEXT, insertbackground=D_TEXT,
                         relief="flat", bd=0,
                         highlightbackground=D_BORDER,
                         highlightthickness=1,
                         highlightcolor=D_ACCENT,
                         show=show)
            e.pack(fill="x", ipady=11, pady=(0, 18))
            return e

        self.username = make_field("USERNAME")
        self.pin      = make_field("PIN", show="●")
        self.username.focus()

        self.btn = tk.Button(inner, text="SIGN IN", command=self._login,
                             bg=D_ACCENT, fg=D_TEXT,
                             font=(FONT_MONO, 11, "bold"),
                             relief="flat", bd=0, cursor="hand2",
                             activebackground=D_ACCENT2, activeforeground=D_TEXT)
        self.btn.pack(fill="x", ipady=13)
        self.btn.bind("<Enter>", lambda e: self.btn.config(bg=D_ACCENT2))
        self.btn.bind("<Leave>", lambda e: self.btn.config(bg=D_ACCENT))

        self.status = tk.Label(inner, text="", fg=D_DANGER,
                               bg=D_CARD, font=(FONT_MONO, 9))
        self.status.pack(pady=(14, 0))

        self.username.bind("<Return>", lambda e: self.pin.focus())
        self.pin.bind("<Return>",      lambda e: self._login())

        # Bottom serial info
        serial = get_serial()
        tk.Label(self.root,
                 text=f"DEVICE  {serial[:8].upper()}...  ·  ZeroAxis MDM",
                 font=(FONT_MONO, 8), fg="#2a3048", bg=D_BG
                 ).place(relx=0.5, rely=0.97, anchor="center")

    def _login(self):
        u = self.username.get().strip()
        p = self.pin.get().strip()
        if not u:
            self.status.config(text="Username is required")
            return
        self.btn.config(state="disabled", text="AUTHENTICATING...")
        self.status.config(text="")
        self.root.update()
        threading.Thread(target=self._do_auth, args=(u, p), daemon=True).start()

    def _do_auth(self, username, pin):
        result = self.client.login(username, pin)
        self.root.after(0, lambda: self._auth_done(result))

    def _auth_done(self, result):
        self.btn.config(state="normal", text="SIGN IN")
        if result and result.get('success'):
            self.root.destroy()
            self.on_success(result)
        else:
            self.status.config(text="Invalid username or PIN")

    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════════════════════
# FILE BROWSER PANEL  (light theme, matching ThreatMind card style)
# ═══════════════════════════════════════════════════════════════════════
class FileBrowserPanel:
    """Embedded file browser — renders inside a given parent Frame."""

    def __init__(self, parent: tk.Frame, root_folder: str):
        self.root_folder     = root_folder
        self.current_path    = root_folder
        self.frame           = parent
        self._prompt_frame   = None
        self._pending_rename = None
        self._clipboard      = None
        self._build()
        self._load(root_folder)

    # ── File type icons ────────────────────────────────────────────────
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
        f.configure(bg=BG_DARK)

        # ── Top toolbar ──
        toolbar = tk.Frame(f, bg=BG_SIDEBAR, height=50)
        toolbar.pack(fill="x", side="top")
        toolbar.pack_propagate(False)
        tk.Frame(toolbar, bg=BORDER, height=1).pack(fill="x", side="bottom")

        tk.Button(toolbar, text="←", command=self._go_up,
                  bg=BG_SIDEBAR, fg=TEXT, font=(FONT_MONO, 14),
                  relief="flat", bd=0, cursor="hand2",
                  padx=16, activebackground=BG_HOVER, activeforeground=TEXT
                  ).pack(side="left")

        self.path_var = tk.StringVar()
        tk.Label(toolbar, textvariable=self.path_var,
                 bg=BG_SIDEBAR, fg=TEXT2,
                 font=(FONT_MONO, 9), anchor="w", padx=8
                 ).pack(side="left", fill="x", expand=True)

        for label_text, cmd, color in [
            ("+ NEW FOLDER", self._new_folder,     SUCCESS),
            ("+ NEW FILE",   self._new_file,       ACCENT),
            ("RENAME",       self._rename_selected, WARNING),
            ("DELETE",       self._delete_selected, DANGER),
        ]:
            btn = tk.Button(toolbar, text=label_text, command=cmd,
                            bg=color, fg=BG_CARD,
                            font=(FONT_MONO, 8, "bold"),
                            relief="flat", bd=0, cursor="hand2",
                            padx=10, pady=0,
                            activebackground=ACCENT, activeforeground=BG_CARD)
            btn.pack(side="right", padx=2, pady=10, ipady=4)

        # ── File list ──
        main = tk.Frame(f, bg=BG_DARK)
        main.pack(fill="both", expand=True)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("FB.Treeview",
                        background=BG_CARD, foreground=TEXT,
                        fieldbackground=BG_CARD, rowheight=32,
                        font=(FONT_MONO, 9), borderwidth=0)
        style.configure("FB.Treeview.Heading",
                        background=BG_SIDEBAR, foreground=TEXT2,
                        font=(FONT_MONO, 8, "bold"),
                        borderwidth=0, relief="flat")
        style.map("FB.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", BG_CARD)])

        cols = ("icon", "name", "size", "modified")
        self.tree = ttk.Treeview(main, columns=cols, show="headings",
                                 selectmode="browse", style="FB.Treeview")
        self.tree.heading("icon",     text="")
        self.tree.heading("name",     text="NAME")
        self.tree.heading("size",     text="SIZE")
        self.tree.heading("modified", text="MODIFIED")
        self.tree.column("icon",     width=56,  stretch=False, anchor="center")
        self.tree.column("name",     width=320, stretch=True)
        self.tree.column("size",     width=80,  stretch=False, anchor="e")
        self.tree.column("modified", width=150, stretch=False)

        vsb = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self.tree.bind("<Double-1>",  self._on_double_click)
        self.tree.bind("<Return>",    self._on_double_click)
        self.tree.bind("<BackSpace>", lambda e: self._go_up())
        self.tree.bind("<Delete>",    lambda e: self._delete_selected())
        self.tree.bind("<Button-3>",  self._on_right_click)

        # ── Inline prompt (hidden) ──
        self._prompt_frame = tk.Frame(f, bg=BG_CARD,
                                      highlightbackground=ACCENT,
                                      highlightthickness=1)

        # ── Status bar ──
        self.status_var = tk.StringVar(value="")
        sb_bar = tk.Frame(f, bg=BG_SIDEBAR, height=28)
        sb_bar.pack(fill="x", side="bottom")
        sb_bar.pack_propagate(False)
        tk.Frame(sb_bar, bg=BORDER, height=1).pack(fill="x", side="top")
        tk.Label(sb_bar, textvariable=self.status_var,
                 bg=BG_SIDEBAR, fg=TEXT_DIM,
                 font=(FONT_MONO, 8), anchor="w", padx=12
                 ).pack(fill="x", pady=4)

        # Right-click menu
        self._ctx_menu = tk.Menu(self.tree, tearoff=0,
                                 bg=BG_CARD, fg=TEXT,
                                 activebackground=ACCENT, activeforeground=BG_CARD,
                                 relief="flat", bd=0,
                                 font=(FONT_MONO, 9))
        for label_text, cmd in [
            ("Open",        self._open_selected),
            ("Edit (text)", self._edit_selected),
            (None, None),
            ("Copy",        self._copy_selected),
            ("Cut",         self._cut_selected),
            ("Paste",       self._paste),
            (None, None),
            ("Rename",      self._rename_selected),
            ("Delete",      self._delete_selected),
            (None, None),
            ("New Folder",  self._new_folder),
        ]:
            if label_text is None:
                self._ctx_menu.add_separator()
            else:
                self._ctx_menu.add_command(label=label_text, command=cmd)

    # ── Navigation ──────────────────────────────────────────────────────
    def _load(self, path: str):
        try:
            Path(path).relative_to(self.root_folder)
        except ValueError:
            return
        if not os.path.isdir(path):
            return
        self.current_path = path
        rel = os.path.relpath(path, self.root_folder)
        display = "MY FILES" + ("" if rel == "." else
                                 "  ›  " + rel.replace(os.sep, "  ›  ").upper())
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
        self.status_var.set(f"{count} item{'s' if count != 1 else ''}  ·  {path}")

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
            self._open_file(path)

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    # ── File actions ─────────────────────────────────────────────────────
    def _open_selected(self):
        sel = self.tree.selection()
        if sel:
            path = sel[0]
            if os.path.isdir(path):
                self._load(path)
            else:
                self._open_file(path)

    def _edit_selected(self):
        sel = self.tree.selection()
        if sel and os.path.isfile(sel[0]):
            self._open_text_editor(sel[0])

    def _open_file(self, path: str):
        if self._is_text_file(os.path.basename(path)):
            self._open_text_editor(path)
            return
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
                shutil.copytree(src, dst) if os.path.isdir(src) else shutil.copy2(src, dst)
            else:
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

    def _new_file(self):
        self._show_prompt("New file name (e.g. notes.txt):", self._confirm_new_file)

    def _confirm_new_file(self, name: str):
        if not name or '/' in name or '\\' in name:
            return
        target = os.path.join(self.current_path, name)
        if os.path.exists(target):
            self.status_var.set(f"Error: '{name}' already exists")
            return
        try:
            open(target, 'w').close()
            self._load(self.current_path)
            if self._is_text_file(name):
                self._open_text_editor(target)
        except Exception as e:
            self.status_var.set(f"Error: {e}")

    @staticmethod
    def _is_text_file(name: str) -> bool:
        exts = ('.txt', '.md', '.csv', '.log', '.json', '.xml',
                '.html', '.htm', '.py', '.js', '.ts', '.css', '.ini', '.cfg')
        return name.lower().endswith(exts)

    def _open_text_editor(self, path: str):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception as e:
            self.status_var.set(f"Cannot open: {e}")
            return

        win = tk.Toplevel(self.frame)
        win.title(f"Edit  —  {os.path.basename(path)}")
        win.geometry("780x520")
        win.configure(bg=BG_DARK)
        win.grab_set()

        # Titlebar
        tb = tk.Frame(win, bg=BG_SIDEBAR, height=48)
        tb.pack(fill="x")
        tb.pack_propagate(False)
        tk.Frame(tb, bg=BORDER, height=1).pack(fill="x", side="bottom")
        tk.Label(tb, text=f"✏  {os.path.basename(path)}",
                 font=(FONT_MONO, 10, "bold"), fg=TEXT, bg=BG_SIDEBAR
                 ).pack(side="left", padx=16, pady=12)

        def _save():
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text_area.get("1.0", tk.END))
                self.status_var.set(f"Saved ✓  {os.path.basename(path)}")
                win.destroy()
            except Exception as e:
                messagebox.showerror("Save failed", str(e), parent=win)

        styled_button(tb, "SAVE", _save).pack(side="right", padx=8, pady=8)
        tk.Button(tb, text="CANCEL", command=win.destroy,
                  bg=BG_HOVER, fg=TEXT2, font=(FONT_MONO, 9, "bold"),
                  relief="flat", bd=0, padx=12, pady=6,
                  cursor="hand2").pack(side="right", padx=0, pady=8)

        editor_frame = tk.Frame(win, bg=BG_DARK)
        editor_frame.pack(fill="both", expand=True)

        line_nums = tk.Text(editor_frame, width=4, bg=BG_SIDEBAR, fg=TEXT_DIM,
                            font=(FONT_MONO, 10), state="disabled",
                            relief="flat", bd=0, padx=8)
        line_nums.pack(side="left", fill="y")
        tk.Frame(editor_frame, bg=BORDER, width=1).pack(side="left", fill="y")

        text_area = tk.Text(editor_frame, bg=BG_CARD, fg=TEXT,
                            insertbackground=ACCENT,
                            font=(FONT_MONO, 10),
                            relief="flat", bd=0,
                            wrap="none", padx=12, pady=6, undo=True)
        text_area.insert("1.0", content)

        vsb = ttk.Scrollbar(editor_frame, orient="vertical", command=text_area.yview)
        hsb = ttk.Scrollbar(win, orient="horizontal", command=text_area.xview)
        text_area.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        text_area.pack(side="left", fill="both", expand=True)

        def _update_line_nums(_=None):
            line_nums.config(state="normal")
            line_nums.delete("1.0", tk.END)
            count = int(text_area.index(tk.END).split('.')[0])
            line_nums.insert("1.0", "\n".join(str(i) for i in range(1, count)))
            line_nums.config(state="disabled")

        text_area.bind("<KeyRelease>", _update_line_nums)
        text_area.bind("<Control-s>", lambda e: _save())
        _update_line_nums()
        text_area.focus()

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
        self._show_prompt("Rename to:", self._confirm_rename,
                          default=os.path.basename(sel[0]))

    def _confirm_rename(self, new_name: str):
        old = self._pending_rename
        if not old or not new_name:
            return
        try:
            os.rename(old, os.path.join(self.current_path, new_name))
            self._load(self.current_path)
        except Exception as e:
            self.status_var.set(f"Error: {e}")

    # ── Inline prompt bar ────────────────────────────────────────────────
    def _show_prompt(self, label: str, callback, default: str = ""):
        self._dismiss_prompt()
        bar = self._prompt_frame
        for w in bar.winfo_children():
            w.destroy()

        tk.Label(bar, text=label, fg=TEXT2, bg=BG_CARD,
                 font=(FONT_MONO, 9)).pack(side="left", padx=(12, 8), pady=10)

        entry = tk.Entry(bar, font=(FONT_MONO, 10),
                         bg=BG_DARK, fg=TEXT, insertbackground=TEXT,
                         relief="flat", bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
        entry.insert(0, default)
        entry.pack(side="left", fill="x", expand=True, ipady=7, pady=8)
        entry.select_range(0, "end")
        entry.focus_set()

        def _ok(_=None):
            val = entry.get().strip()
            self._dismiss_prompt()
            if val:
                callback(val)

        styled_button(bar, "OK", _ok, font_size=9).pack(side="left", padx=4, pady=8)
        tk.Button(bar, text="✕", command=self._dismiss_prompt,
                  bg=BG_CARD, fg=TEXT2, font=(FONT_MONO, 9),
                  relief="flat", bd=0, padx=10, pady=6,
                  cursor="hand2").pack(side="left", padx=(0, 8), pady=8)

        entry.bind("<Return>", _ok)
        entry.bind("<Escape>", lambda e: self._dismiss_prompt())
        bar.pack(fill="x", side="bottom", before=self.frame.winfo_children()[-1])

    def _dismiss_prompt(self):
        self._prompt_frame.pack_forget()


# ═══════════════════════════════════════════════════════════════════════
# LAUNCHER WINDOW  (full-screen OS shell layer)
# ═══════════════════════════════════════════════════════════════════════
class LauncherWindow:
    NAV_ITEMS = [
        ("apps",    "APPS",         "⊞"),
        ("files",   "MY FILES",     "◫"),
        ("tickets", "REPORT ISSUE", "⚑"),
    ]

    def __init__(self, client, user_data: dict, enforcer: PolicyEnforcer,
                 usage_tracker: AppUsageTracker, serial: str):
        self.client      = client
        self.user_data   = user_data
        self.enforcer    = enforcer
        self.tracker     = usage_tracker
        self.serial      = serial
        self._active_nav = "apps"
        self._panels: dict = {}
        self._file_panel: Optional[FileBrowserPanel] = None

        self.root = tk.Tk()
        self.root.title("ZeroAxis")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg=BG_DARK)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

        username = self.user_data.get('username', '')
        self._user_folder = get_user_folder(username)
        os.environ['ZEROAXIS_USER_HOME'] = self._user_folder

        log(f"LauncherWindow: building UI for {username}")
        try:
            self._build()
        except Exception as e:
            log(f"LauncherWindow._build failed: {e}", 'error')
            import traceback
            log(traceback.format_exc(), 'error')
            raise
        self.tracker.set_active_user(username)
        self._start_threads()
        log("LauncherWindow: ready")

    # ── Layout ──────────────────────────────────────────────────────────
    def _build(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)
        self._build_titlebar()
        self._build_sidebar()
        self._build_content_area()
        self._build_statusbar()

    def _build_titlebar(self):
        """Top bar — logo + username + clock."""
        bar = tk.Frame(self.root, bg=BG_SIDEBAR, height=52)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x", side="bottom")

        # Left: logo
        logo = tk.Frame(bar, bg=BG_SIDEBAR)
        logo.pack(side="left", padx=20, pady=10)
        tk.Label(logo, text="◈", font=(FONT_MONO, 18, "bold"),
                 fg=ACCENT, bg=BG_SIDEBAR).pack(side="left")
        tk.Label(logo, text=" ZEROAXIS", font=(FONT_MONO, 13, "bold"),
                 fg=TEXT, bg=BG_SIDEBAR).pack(side="left")
        tk.Label(logo, text=" WORKSTATION", font=(FONT_MONO, 8),
                 fg=TEXT2, bg=BG_SIDEBAR).pack(side="left", padx=(2, 0))

        # Right: clock + status pill
        self._clock_var = tk.StringVar()
        tk.Label(bar, textvariable=self._clock_var,
                 font=(FONT_MONO, 9), fg=TEXT2, bg=BG_SIDEBAR
                 ).pack(side="right", padx=20)

        self._status_pill = tk.Label(bar, text="● ACTIVE",
                                     font=(FONT_MONO, 8, "bold"),
                                     fg=SUCCESS, bg=BG_SIDEBAR)
        self._status_pill.pack(side="right", padx=(0, 8))

        self._update_clock()

    def _update_clock(self):
        self._clock_var.set(datetime.now().strftime("%a %d %b  %H:%M:%S"))
        try:
            if self.root.winfo_exists():
                self.root.after(1000, self._update_clock)
        except Exception:
            pass

    def _build_sidebar(self):
        sb = tk.Frame(self.root, bg=BG_SIDEBAR, width=210)
        sb.grid(row=1, column=0, sticky="ns")
        sb.grid_propagate(False)
        sb.pack_propagate(False)
        tk.Frame(sb, bg=BORDER, width=1).pack(side="right", fill="y")

        # User chip
        uname = self.user_data.get('username', '')
        chip = tk.Frame(sb, bg=CARD_HL, padx=14, pady=14)
        chip.pack(fill="x", padx=12, pady=(20, 0))

        avatar = tk.Label(chip, text=uname[0].upper() if uname else "?",
                          font=(FONT_MONO, 16, "bold"),
                          fg=BG_CARD, bg=ACCENT, width=2)
        avatar.pack(side="left", ipadx=4, ipady=6)

        info = tk.Frame(chip, bg=CARD_HL)
        info.pack(side="left", padx=(10, 0))
        tk.Label(info, text=uname.upper(), font=(FONT_MONO, 9, "bold"),
                 fg=TEXT, bg=CARD_HL, anchor="w").pack(anchor="w")
        tk.Label(info, text="STANDARD USER", font=(FONT_MONO, 7),
                 fg=TEXT2, bg=CARD_HL, anchor="w").pack(anchor="w")

        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x", padx=12, pady=16)

        # Nav items
        self._nav_btns = {}
        for key, label_text, icon in self.NAV_ITEMS:
            btn = tk.Button(
                sb,
                text=f"  {icon}   {label_text}",
                command=lambda k=key: self._switch_nav(k),
                font=(FONT_MONO, 9, "bold"), anchor="w",
                relief="flat", bd=0, cursor="hand2",
                padx=16, pady=11,
            )
            btn.pack(fill="x", padx=8, pady=1)
            self._nav_btns[key] = btn

        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x", padx=12, pady=16)

        # Screen time
        self.st_frame = tk.Frame(sb, bg=BG_SIDEBAR)
        self.st_frame.pack(fill="x", padx=16)
        self.st_label = tk.Label(self.st_frame, text="",
                                 font=(FONT_MONO, 8), fg=TEXT2, bg=BG_SIDEBAR,
                                 justify="left", anchor="w")
        self.st_label.pack(fill="x")
        self.st_bar_bg = tk.Frame(self.st_frame, bg=BORDER, height=4)
        self.st_bar_bg.pack(fill="x", pady=(4, 0))
        self.st_bar_fg = tk.Frame(self.st_bar_bg, bg=ACCENT, height=4)
        self.st_bar_fg.place(relwidth=0, relheight=1)

        # Bottom buttons
        bottom = tk.Frame(sb, bg=BG_SIDEBAR)
        bottom.pack(side="bottom", fill="x", padx=12, pady=16)

        for text, cmd, fg_color in [
            ("⟳  SYNC",    self._sync_and_refresh, TEXT2),
            ("LOGOUT",     self._logout,            DANGER),
        ]:
            btn = tk.Button(bottom, text=text, command=cmd,
                            font=(FONT_MONO, 9, "bold"), anchor="w",
                            bg=BG_SIDEBAR, fg=fg_color,
                            relief="flat", bd=0, cursor="hand2",
                            padx=12, pady=9,
                            activebackground=BG_HOVER, activeforeground=fg_color)
            btn.pack(fill="x", pady=1)
            btn.bind("<Enter>", lambda e, b=btn: b.config(bg=BG_HOVER))
            btn.bind("<Leave>", lambda e, b=btn: b.config(bg=BG_SIDEBAR))

        self._update_nav_style()

    def _build_content_area(self):
        self._content = tk.Frame(self.root, bg=BG_DARK)
        self._content.grid(row=1, column=1, sticky="nsew")
        self._content.columnconfigure(0, weight=1)
        self._content.rowconfigure(1, weight=1)

        # Section header bar
        self._section_bar = tk.Frame(self._content, bg=BG_SIDEBAR, height=44)
        self._section_bar.grid(row=0, column=0, sticky="ew")
        self._section_bar.grid_propagate(False)
        tk.Frame(self._section_bar, bg=BORDER, height=1).pack(fill="x", side="bottom")

        self._section_title = tk.Label(self._section_bar, text="APPS",
                                       font=(FONT_MONO, 11, "bold"),
                                       fg=TEXT, bg=BG_SIDEBAR)
        self._section_title.pack(side="left", padx=24, pady=10)

        self._section_status = tk.StringVar(value="")
        tk.Label(self._section_bar, textvariable=self._section_status,
                 fg=TEXT_DIM, bg=BG_SIDEBAR, font=(FONT_MONO, 8)
                 ).pack(side="right", padx=20)

        # Panel container
        self._panel_container = tk.Frame(self._content, bg=BG_DARK)
        self._panel_container.grid(row=1, column=0, sticky="nsew")
        self._panel_container.columnconfigure(0, weight=1)
        self._panel_container.rowconfigure(0, weight=1)

        self._build_apps_panel()
        self._build_files_panel()
        self._build_tickets_panel()
        self._switch_nav("apps")

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=BG_SIDEBAR, height=26)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x", side="top")
        self._statusbar_var = tk.StringVar(value="Ready")
        tk.Label(bar, textvariable=self._statusbar_var,
                 font=(FONT_MONO, 8), fg=TEXT_DIM, bg=BG_SIDEBAR,
                 anchor="w").pack(side="left", padx=16)
        tk.Label(bar, text=f"ZeroAxis Agent  ·  {get_serial()[:12]}",
                 font=(FONT_MONO, 8), fg=TEXT_DIM, bg=BG_SIDEBAR
                 ).pack(side="right", padx=16)

    def _set_status(self, msg: str):
        self._statusbar_var.set(msg)

    # ── Apps panel ───────────────────────────────────────────────────────
    def _build_apps_panel(self):
        panel = tk.Frame(self._panel_container, bg=BG_DARK)
        self._panels["apps"] = panel

        # Quick launch row
        self._quick_frame = tk.Frame(panel, bg=BG_DARK)
        self._quick_frame.pack(fill="x", padx=28, pady=(20, 0))

        # App grid (scrollable)
        outer = tk.Frame(panel, bg=BG_DARK)
        outer.pack(fill="both", expand=True, padx=28, pady=16)

        canvas = tk.Canvas(outer, bg=BG_DARK, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._apps_inner  = tk.Frame(canvas, bg=BG_DARK)
        self._apps_window = canvas.create_window((0, 0), window=self._apps_inner,
                                                  anchor="nw")

        def _on_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(self._apps_window, width=canvas.winfo_width())

        self._apps_inner.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(self._apps_window, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        self._apps_canvas = canvas
        self._refresh_apps_panel()

    def _refresh_apps_panel(self):
        self._build_quick_buttons()
        self._build_app_grid()

    def _build_quick_buttons(self):
        for w in self._quick_frame.winfo_children():
            w.destroy()
        browser = self.enforcer.allowed_browser
        docs    = self.enforcer.allowed_document_viewer

        def _is_android_pkg(s):
            return (bool(s) and '.' in s and not s.lower().endswith('.exe')
                    and not os.path.isabs(s) and s.replace('.','').replace('_','').isalpha())

        for label_text, entry, color in [
            ("🌐  BROWSER",   browser, "#0891b2"),
            ("📄  DOCUMENTS", docs,    "#7c3aed"),
        ]:
            if entry and not _is_android_pkg(entry):
                styled_button(self._quick_frame, label_text,
                              lambda e=entry: self._launch(e),
                              color=color, font_size=9
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
            empty = tk.Frame(self._apps_inner, bg=BG_DARK)
            empty.pack(expand=True, pady=60)
            tk.Label(empty, text="⊞", font=(FONT_MONO, 36), fg=BORDER, bg=BG_DARK).pack()
            tk.Label(empty, text="NO APPS CONFIGURED",
                     font=(FONT_MONO, 12, "bold"), fg=TEXT2, bg=BG_DARK).pack(pady=(8, 4))
            tk.Label(empty, text="Contact your administrator to assign applications.",
                     font=(FONT_MONO, 9), fg=TEXT_DIM, bg=BG_DARK).pack()
            return

        COLS   = 5
        CARD_W = 150
        CARD_H = 108

        for idx, (display_name, path) in enumerate(resolved):
            r, c = divmod(idx, COLS)

            card_frame = tk.Frame(self._apps_inner, bg=BG_CARD, width=CARD_W, height=CARD_H,
                                  highlightbackground=BORDER, highlightthickness=1)
            card_frame.grid(row=r, column=c, padx=8, pady=8, sticky="nsew")
            card_frame.grid_propagate(False)

            # Colour accent strip at top
            accent_strip = tk.Frame(card_frame, bg=ACCENT, height=3)
            accent_strip.pack(fill="x")

            # Initial-letter avatar
            initial = display_name[0].upper() if display_name else "?"
            avatar = tk.Label(card_frame, text=initial,
                              font=(FONT_MONO, 20, "bold"),
                              fg=ACCENT, bg=BG_CARD)
            avatar.pack(pady=(14, 4))

            name_lbl = tk.Label(card_frame, text=display_name.upper(),
                                font=(FONT_MONO, 7, "bold"),
                                fg=TEXT2, bg=BG_CARD,
                                wraplength=CARD_W - 16)
            name_lbl.pack()

            def _enter(e, f=card_frame, s=accent_strip, a=avatar, n=name_lbl):
                f.configure(bg=CARD_HL, highlightbackground=ACCENT)
                s.configure(bg=ACCENT2)
                a.configure(bg=CARD_HL)
                n.configure(bg=CARD_HL)

            def _leave(e, f=card_frame, s=accent_strip, a=avatar, n=name_lbl):
                f.configure(bg=BG_CARD, highlightbackground=BORDER)
                s.configure(bg=ACCENT)
                a.configure(bg=BG_CARD)
                n.configure(bg=BG_CARD)

            def _click(e=None, p=path):
                self._launch(p)

            for widget in (card_frame, avatar, name_lbl):
                widget.bind("<Enter>",    _enter)
                widget.bind("<Leave>",    _leave)
                widget.bind("<Button-1>", _click)
                widget.configure(cursor="hand2")

        for col in range(COLS):
            self._apps_inner.columnconfigure(col, weight=1)

    # ── Files panel ──────────────────────────────────────────────────────
    def _build_files_panel(self):
        panel = tk.Frame(self._panel_container, bg=BG_SIDEBAR)
        self._panels["files"] = panel
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)

        file_frame = tk.Frame(panel, bg=BG_SIDEBAR)
        file_frame.grid(row=0, column=0, sticky="nsew")
        file_frame.columnconfigure(0, weight=1)
        file_frame.rowconfigure(0, weight=1)

        self._file_panel = FileBrowserPanel(file_frame, self._user_folder)

    # ── Tickets panel ────────────────────────────────────────────────────
    def _build_tickets_panel(self):
        panel = tk.Frame(self._panel_container, bg=BG_DARK)
        self._panels["tickets"] = panel

        canvas = tk.Canvas(panel, bg=BG_DARK, highlightthickness=0)
        vsb = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        frame = tk.Frame(canvas, bg=BG_DARK)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        win = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

        # Header
        hdr = tk.Frame(frame, bg=BG_DARK)
        hdr.pack(fill="x", padx=30, pady=(30, 0))
        tk.Label(hdr, text="REPORT AN ISSUE", font=(FONT_MONO, 16, "bold"),
                 fg=TEXT, bg=BG_DARK).pack(anchor="w")
        tk.Label(hdr, text="Describe your problem and submit it to your administrator.",
                 font=(FONT_MONO, 9), fg=TEXT2, bg=BG_DARK).pack(anchor="w", pady=(4, 0))

        # Form card
        form_card = tk.Frame(frame, bg=BG_CARD)
        form_card.pack(fill="x", padx=30, pady=20)
        tk.Frame(form_card, bg=ACCENT, height=3).pack(fill="x")
        form = tk.Frame(form_card, bg=BG_CARD)
        form.pack(fill="x", padx=24, pady=20)

        # Category
        tk.Label(form, text="CATEGORY", font=(FONT_MONO, 8, "bold"),
                 fg=TEXT2, bg=BG_CARD).pack(anchor="w", pady=(0, 6))
        self._ticket_category = tk.StringVar(value="other")
        cat_frame = tk.Frame(form, bg=BG_CARD)
        cat_frame.pack(fill="x", pady=(0, 16))
        for cat, label_text in [("hardware","Hardware"), ("software","Software"),
                                 ("network","Network"), ("other","Other")]:
            tk.Radiobutton(cat_frame, text=label_text, value=cat,
                           variable=self._ticket_category,
                           font=(FONT_MONO, 9), fg=TEXT, bg=BG_CARD,
                           selectcolor=BG_HOVER, activebackground=BG_CARD,
                           activeforeground=ACCENT
                           ).pack(side="left", padx=(0, 16))

        # Description
        tk.Label(form, text="DESCRIPTION", font=(FONT_MONO, 8, "bold"),
                 fg=TEXT2, bg=BG_CARD).pack(anchor="w", pady=(0, 6))
        self._ticket_desc = tk.Text(form, height=6, font=(FONT_MONO, 9),
                                    bg=BG_DARK, fg=TEXT,
                                    insertbackground=ACCENT,
                                    relief="flat", bd=0,
                                    highlightbackground=BORDER,
                                    highlightthickness=1,
                                    padx=12, pady=10, wrap="word")
        self._ticket_desc.pack(fill="x", pady=(0, 16))

        # Submit
        btn_row = tk.Frame(form, bg=BG_CARD)
        btn_row.pack(fill="x")
        self._ticket_status = tk.StringVar(value="")
        self._ticket_btn = styled_button(btn_row, "⚑  SUBMIT TICKET",
                                         self._submit_ticket)
        self._ticket_btn.pack(side="left")
        tk.Label(btn_row, textvariable=self._ticket_status,
                 font=(FONT_MONO, 9), fg=SUCCESS, bg=BG_CARD
                 ).pack(side="left", padx=16)

        # Feedback section
        section_label(frame, "FEEDBACK FORMS", bg=BG_DARK)
        tk.Label(frame, text="Active feedback forms from your administrator.",
                 font=(FONT_MONO, 9), fg=TEXT2, bg=BG_DARK
                 ).pack(anchor="w", padx=30, pady=(0, 12))

        self._feedback_container = tk.Frame(frame, bg=BG_DARK)
        self._feedback_container.pack(fill="x", padx=30)
        self._feedback_status = tk.StringVar(value="")
        tk.Label(frame, textvariable=self._feedback_status,
                 font=(FONT_MONO, 9), fg=TEXT2, bg=BG_DARK
                 ).pack(anchor="w", padx=30, pady=(8, 0))

        tk.Frame(frame, bg=BG_DARK, height=30).pack()
        threading.Thread(target=self._load_feedback_forms, daemon=True).start()

    def _submit_ticket(self):
        desc = self._ticket_desc.get("1.0", tk.END).strip()
        if not desc:
            self._ticket_status.set("Please describe your issue first.")
            return
        username = self.user_data.get('username', '')
        category = self._ticket_category.get()
        self._ticket_btn.config(state="disabled", text="SUBMITTING...")
        self._ticket_status.set("")
        self.root.update()

        def _do():
            ok = self.client.submit_ticket(username, desc, category)
            self.root.after(0, lambda: _done(ok))

        def _done(ok):
            self._ticket_btn.config(state="normal", text="⚑  SUBMIT TICKET")
            if ok:
                self._ticket_desc.delete("1.0", tk.END)
                self._ticket_category.set("other")
                self._ticket_status.set("✓ Ticket submitted successfully.")
                self.root.after(4000, lambda: self._ticket_status.set(""))
            else:
                self._ticket_status.set("Failed to submit. Check your connection.")

        threading.Thread(target=_do, daemon=True).start()

    def _load_feedback_forms(self):
        username = self.user_data.get('username', '')
        forms = self.client.get_feedback_forms(username)
        self.root.after(0, lambda: self._render_feedback_forms(forms))

    def _render_feedback_forms(self, forms: list):
        for w in self._feedback_container.winfo_children():
            w.destroy()
        if not forms:
            tk.Label(self._feedback_container,
                     text="No active feedback forms right now.",
                     font=(FONT_MONO, 9), fg=TEXT2, bg=BG_DARK).pack(anchor="w")
            return
        for form in forms:
            card = tk.Frame(self._feedback_container, bg=BG_CARD,
                            highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", pady=(0, 12))
            tk.Frame(card, bg=ACCENT, height=3).pack(fill="x")
            inner = tk.Frame(card, bg=BG_CARD)
            inner.pack(fill="x", padx=20, pady=16)
            tk.Label(inner, text=form['title'].upper(),
                     font=(FONT_MONO, 10, "bold"), fg=TEXT, bg=BG_CARD).pack(anchor="w")
            if form.get('description'):
                tk.Label(inner, text=form['description'],
                         font=(FONT_MONO, 9), fg=TEXT2, bg=BG_CARD,
                         wraplength=500, justify="left").pack(anchor="w", pady=(2, 8))

            # Star rating
            rating_var  = tk.IntVar(value=0)
            stars_frame = tk.Frame(inner, bg=BG_CARD)
            stars_frame.pack(anchor="w", pady=(6, 8))
            star_btns   = []

            def _make_hover(btns, rv, n):
                def _enter(_):
                    for i, b in enumerate(btns):
                        b.config(fg="#f59f00" if i < n else TEXT_DIM)
                def _leave(_):
                    v = rv.get()
                    for i, b in enumerate(btns):
                        b.config(fg="#f59f00" if i < v else TEXT_DIM)
                return _enter, _leave

            for i in range(1, 6):
                sb = tk.Label(stars_frame, text="★", font=(FONT_MONO, 18),
                              fg=TEXT_DIM, bg=BG_CARD, cursor="hand2")
                sb.pack(side="left", padx=1)
                star_btns.append(sb)
            for i, sb in enumerate(star_btns, 1):
                _e, _l = _make_hover(star_btns, rating_var, i)
                sb.bind("<Enter>", _e)
                sb.bind("<Leave>", _l)
                sb.bind("<Button-1>", lambda e, n=i, rv=rating_var, btns=star_btns: (
                    rv.set(n),
                    [b.config(fg="#f59f00" if j < n else TEXT_DIM)
                     for j, b in enumerate(btns)]
                ))

            tk.Label(inner, text="COMMENTS (OPTIONAL)", font=(FONT_MONO, 8, "bold"),
                     fg=TEXT2, bg=BG_CARD).pack(anchor="w")
            text_box = tk.Text(inner, height=3, font=(FONT_MONO, 9),
                               bg=BG_DARK, fg=TEXT, insertbackground=ACCENT,
                               relief="flat", bd=0,
                               highlightbackground=BORDER, highlightthickness=1,
                               padx=10, pady=8, wrap="word")
            text_box.pack(fill="x", pady=(4, 10))

            fb_status  = tk.StringVar(value="")
            submit_btn = styled_button(inner, "SUBMIT FEEDBACK", lambda: None,
                                       color="#7c3aed")
            submit_btn.pack(side="left")
            tk.Label(inner, textvariable=fb_status,
                     font=(FONT_MONO, 9), fg=SUCCESS, bg=BG_CARD
                     ).pack(side="left", padx=12)

            def _submit_fb(fid=form['id'], rv=rating_var, tb=text_box,
                           btn=submit_btn, sv=fb_status):
                rating = rv.get()
                if rating == 0:
                    sv.set("Please select a star rating.")
                    return
                username = self.user_data.get('username', '')
                text = tb.get("1.0", tk.END).strip()
                btn.config(state="disabled", text="SUBMITTING...")
                sv.set("")

                def _do():
                    ok = self.client.submit_feedback(username, fid, rating, text)
                    self.root.after(0, lambda: _done(ok, btn, sv, tb, rv))

                def _done(ok, b, s, t, r):
                    if ok:
                        b.config(state="disabled", text="SUBMITTED ✓")
                        s.set("Thank you for your feedback!")
                        t.delete("1.0", tk.END)
                        r.set(0)
                    else:
                        b.config(state="normal", text="SUBMIT FEEDBACK")
                        s.set("Failed. Try again.")

                threading.Thread(target=_do, daemon=True).start()

            submit_btn.config(command=_submit_fb)

    # ── Navigation ───────────────────────────────────────────────────────
    def _switch_nav(self, key: str):
        self._active_nav = key
        for k, panel in self._panels.items():
            panel.grid_forget()
        self._panels[key].grid(row=0, column=0, sticky="nsew",
                               in_=self._panel_container)
        titles = {"apps": "APPS", "files": "MY FILES", "tickets": "REPORT ISSUE"}
        self._section_title.config(text=titles.get(key, ""))
        self._update_nav_style()

    def _update_nav_style(self):
        for key, btn in self._nav_btns.items():
            if key == self._active_nav:
                btn.config(bg=CARD_HL, fg=ACCENT,
                           activebackground=CARD_HL, activeforeground=ACCENT)
            else:
                btn.config(bg=BG_SIDEBAR, fg=TEXT2,
                           activebackground=BG_HOVER, activeforeground=TEXT)

    # ── Screen time bar ──────────────────────────────────────────────────
    def _update_screen_time_bar(self):
        limit = self.enforcer.screen_time_limit
        used  = self.enforcer.today_usage
        if limit > 0:
            remaining = max(0, limit - used)
            pct = min(1.0, used / limit)
            self.st_label.config(
                text=f"SCREEN TIME\n{used} min used  ·  {remaining} min left")
            self.st_bar_fg.place(relwidth=pct, relheight=1)
            self.st_bar_fg.config(bg=DANGER if pct > 0.8 else ACCENT)
        else:
            self.st_label.config(text=f"SCREEN TIME\n{used} min (no limit)")
            self.st_bar_fg.place(relwidth=0, relheight=1)

    # ── App launch ───────────────────────────────────────────────────────
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
            DETACHED_PROCESS = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(path, shell=False,
                             creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                             close_fds=True)
            log(f"Launched: {path}")
            self._set_status(f"Launched: {os.path.basename(path)}")
            self.root.after(3000, lambda: self._set_status("Ready"))
        except Exception:
            try:
                subprocess.Popen(path, shell=True, creationflags=0x08000000)
            except Exception as e2:
                messagebox.showerror("ZeroAxis", f"Failed to launch app:\n{e2}")

    # ── Logout / sync ────────────────────────────────────────────────────
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

    def _safe_after(self, ms, fn):
        try:
            if self.root.winfo_exists():
                self.root.after(ms, fn)
        except Exception:
            pass

    def _sync_and_refresh(self):
        def _sync():
            try:
                pol = self.client.sync_policy()
                if pol:
                    self.enforcer.apply(pol)
                    self._safe_after(0, self._refresh_apps_panel)
                    self._safe_after(0, self._update_screen_time_bar)
                self._safe_after(0, self._load_feedback_forms)
            except Exception as e:
                log(f"Sync error: {e}", 'error')
        threading.Thread(target=_sync, daemon=True).start()
        self._set_status("Syncing with server...")
        self._safe_after(3000, lambda: self._set_status("Ready"))

    # ── Background threads ───────────────────────────────────────────────
    def _start_threads(self):
        self._dns_tracker = DnsTracker(self.serial)

        def policy_loop():
            while True:
                time.sleep(POLICY_INTERVAL)
                try:
                    pol = self.client.sync_policy()
                    if pol:
                        self.enforcer.apply(pol)
                        self._safe_after(0, self._refresh_apps_panel)
                        self._safe_after(0, self._update_screen_time_bar)
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
                self._safe_after(0, self._update_screen_time_bar)
                if tick % 5 == 0:
                    sm = self.tracker.get_session_minutes()
                    threading.Thread(target=self.client.push_screen_time,
                                     args=(sm,), daemon=True).start()
                    threading.Thread(target=self.tracker.flush_now,
                                     daemon=True).start()
                if self.enforcer.screen_time_exceeded():
                    self._safe_after(0, lambda: (
                        messagebox.showwarning("ZeroAxis", "Screen time limit reached."),
                        self._force_logout()
                    ))
                    break
                if self.enforcer.is_curfew_active():
                    self._safe_after(0, lambda: (
                        messagebox.showwarning("ZeroAxis", "Curfew active. Device locked."),
                        self._force_logout()
                    ))
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
                    self._safe_after(0,
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
        self.active_username = None

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
            self.session.post(f"{SERVER_URL}/api/enduser/logout",
                              json={"device_serial": self.serial}, timeout=5)
        except Exception:
            pass
        self.active_username = None

    def sync_policy(self) -> Optional[dict]:
        try:
            r = self.session.get(f"{SERVER_URL}/api/device/policy/{self.serial}",
                                 timeout=10)
            if r.ok:
                return r.json()
        except Exception as e:
            log(f"sync_policy error: {e}", 'error')
        return None

    def submit_ticket(self, username: str, description: str, category: str = 'other') -> bool:
        try:
            r = self.session.post(f"{SERVER_URL}/api/enduser/ticket",
                                  json={"device_serial": self.serial,
                                        "username": username,
                                        "description": description,
                                        "category": category},
                                  timeout=10)
            return r.ok
        except Exception as e:
            log(f"submit_ticket error: {e}", 'error')
            return False

    def get_feedback_forms(self, username: str) -> list:
        try:
            r = self.session.get(
                f"{SERVER_URL}/api/enduser/feedback/forms/{self.serial}/{username}",
                timeout=10)
            if r.ok:
                return r.json()
        except Exception as e:
            log(f"get_feedback_forms error: {e}", 'error')
        return []

    def submit_feedback(self, username: str, form_id: int, rating: int, text: str = '') -> bool:
        try:
            r = self.session.post(f"{SERVER_URL}/api/enduser/feedback/submit",
                                  json={"device_serial": self.serial,
                                        "username": username,
                                        "form_id": form_id,
                                        "rating": rating,
                                        "text": text},
                                  timeout=10)
            return r.ok
        except Exception as e:
            log(f"submit_feedback error: {e}", 'error')
            return False

    def push_screen_time(self, minutes: int):
        if not self.active_username:
            return
        try:
            self.session.post(
                f"{SERVER_URL}/api/enduser/screen_time/{self.serial}",
                json={"username": self.active_username,
                      "date": date.today().isoformat(),
                      "screen_time_mins": minutes},
                timeout=5)
        except Exception as e:
            log(f"push_screen_time error: {e}", 'error')


# ========== Background workers ==========
def start_background_workers(serial: str, enforcer: PolicyEnforcer):
    usage_tracker = AppUsageTracker(serial)
    dns_tracker   = DnsTracker(serial)
    executor      = CommandExecutor(serial,
                                    on_lock=lock_workstation,
                                    on_reload_policy=lambda: None)

    def stats_loop():
        while True:
            collect_stats(serial)
            time.sleep(STATS_INTERVAL)

    def cmd_loop():
        while True:
            executor.poll_and_execute()
            time.sleep(COMMAND_INTERVAL)

    def dns_bg_loop():
        while True:
            dns_tracker.collect()
            dns_tracker.flush()
            time.sleep(DNS_FLUSH_INTERVAL)

    threading.Thread(target=stats_loop,  daemon=True).start()
    threading.Thread(target=cmd_loop,    daemon=True).start()
    threading.Thread(target=dns_bg_loop, daemon=True).start()
    return usage_tracker


# ========== Main ==========
def main():
    serial = get_serial()
    if not serial:
        log("Could not determine device serial — exiting", 'error')
        sys.exit(1)
    log(f"ZeroAxis Agent starting. Serial={serial}")

    apply_lockdown()

    enforcer      = PolicyEnforcer()
    usage_tracker = start_background_workers(serial, enforcer)
    client        = ZeroAxisClient(serial)

    while True:
        login_result = [None]

        def on_login_success(data):
            login_result[0] = data

        login_win = LoginWindow(client, on_login_success)
        login_win.run()

        if login_result[0] is None:
            log("Login returned None — restarting login loop")
            continue

        log(f"Login success: {login_result[0].get('username')}")
        enforcer.apply(login_result[0].get('policies', {}))

        try:
            launcher = LauncherWindow(client, login_result[0], enforcer,
                                      usage_tracker, serial)
            launcher.run()
        except Exception as e:
            log(f"LauncherWindow crashed: {e}", 'error')
            import traceback
            log(traceback.format_exc(), 'error')


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"main() crashed: {e}", 'error')
        import traceback
        log(traceback.format_exc(), 'error')
        input("Press Enter to exit...")