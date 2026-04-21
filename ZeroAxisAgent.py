#!/usr/bin/env python
# ZeroAxis Windows Agent – Kiosk + Multi‑User RBAC

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.request

# ========== Configuration ==========
SERVER_URL = "https://zeroaxis.live"
DEVICE_SERIAL = None
# ==================================

# Setup logging
LOG_FILE = r"C:\ProgramData\ZeroAxis\agent.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

def log(msg, level='info'):
    getattr(logging, level)(msg)
    print(msg)

def get_serial():
    """Read the device serial (MachineGuid) from registry."""
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        if guid:
            return guid.strip()
    except Exception as e:
        log(f"Failed to read serial: {e}", 'error')
    return None

# ========== Windows API helpers ==========
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

def block_keys():
    """Block Win key, Alt+Tab, Ctrl+Alt+Del, etc. using low-level keyboard hook."""
    # This requires a separate DLL or ctypes hook.
    # For simplicity, we'll use group policies / registry to disable most.
    # Full key blocking would require a global hook (more complex).
    # We'll implement a simpler approach: disable via registry.
    try:
        # Disable Win keys via Group Policy (requires reboot)
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer")
        winreg.SetValueEx(key, "NoWinKeys", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
        # Disable Task Manager
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Policies\System")
        winreg.SetValueEx(key, "DisableTaskMgr", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
        # Disable Sticky Keys, Filter Keys, etc.
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Control Panel\Accessibility\StickyKeys")
        winreg.SetValueEx(key, "Flags", 0, winreg.REG_SZ, "506")
        winreg.CloseKey(key)
        log("Key restrictions applied via registry")
    except Exception as e:
        log(f"Failed to set key restrictions: {e}", 'error')

def set_shell(agent_path):
    """Set this agent as the Windows shell."""
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon",
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "Shell", 0, winreg.REG_SZ, agent_path)
        winreg.CloseKey(key)
        log(f"Shell set to {agent_path}")
    except Exception as e:
        log(f"Failed to set shell: {e}", 'error')

def lock_workstation():
    """Lock the workstation (requires user32.dll)."""
    user32.LockWorkStation()

def shutdown_system(reboot=False):
    """Shutdown or reboot the system."""
    if reboot:
        os.system("shutdown /r /t 10 /c \"ZeroAxis will reboot in 10 seconds.\"")
    else:
        os.system("shutdown /s /t 10 /c \"ZeroAxis will shut down in 10 seconds.\"")

# ========== Policy enforcement ==========
class PolicyEnforcer:
    def __init__(self):
        self.allowed_apps = []   # list of app names or paths
        self.blocked_apps = []
        self.kiosk_mode = False
        self.screen_time_limit = 0   # minutes per day
        self.curfew_start = None
        self.curfew_end = None
        self.internet_filter = "none"
        self.allowed_domains = []
        self.user_group_id = None
        self.allowed_browser = ""
        self.allowed_document_viewer = ""
        self.session_start = datetime.now()
        self.today_usage = 0

    def apply_policies(self, policies: dict):
        """Update internal policy state from server response."""
        self.allowed_apps = policies.get('allowed_apps', [])
        self.blocked_apps = policies.get('blocked_apps', [])
        self.kiosk_mode = policies.get('kiosk_mode', False)
        self.screen_time_limit = policies.get('screen_time_limit_mins', 0)
        self.curfew_start = policies.get('curfew_start')
        self.curfew_end = policies.get('curfew_end')
        self.internet_filter = policies.get('internet_filter', 'none')
        self.allowed_domains = policies.get('allowed_domains', [])
        self.allowed_browser = policies.get('allowed_browser', '')
        self.allowed_document_viewer = policies.get('allowed_document_viewer', '')
        # Reset daily usage if new day
        today_date = datetime.now().date()
        if not hasattr(self, '_last_date') or self._last_date != today_date:
            self.today_usage = 0
            self._last_date = today_date
        log(f"Policies updated: allowed={self.allowed_apps}, limit={self.screen_time_limit}")

    def is_curfew_active(self) -> bool:
        """Check if current time is within curfew (outside allowed hours)."""
        if not self.curfew_start or not self.curfew_end:
            return False
        now = datetime.now().time()
        start = datetime.strptime(self.curfew_start, '%H:%M').time()
        end = datetime.strptime(self.curfew_end, '%H:%M').time()
        # Curfew is the period when device should be locked (e.g., 22:00 to 06:00)
        # If start < end, curfew is overnight; else it's within same day.
        if start < end:
            return now < start or now > end
        else:
            return start <= now <= end

    def check_screen_time(self) -> bool:
        """Return True if screen time limit exceeded."""
        if self.screen_time_limit <= 0:
            return False
        return self.today_usage >= self.screen_time_limit

    def launch_app(self, app_name_or_path):
        """Launch an allowed application."""
        try:
            # If it's a simple name like "chrome", try to find its path
            # For simplicity, assume it's a full path or a registered command.
            # We'll use shell execute.
            subprocess.Popen(app_name_or_path, shell=True)
            log(f"Launched app: {app_name_or_path}")
        except Exception as e:
            log(f"Failed to launch {app_name_or_path}: {e}", 'error')

# ========== HTTP client ==========
class ZeroAxisClient:
    def __init__(self, server_url, device_serial):
        self.server = server_url
        self.serial = device_serial
        self.session = requests.Session()

    def login(self, username, pin):
        """Authenticate end user and get policies."""
        url = f"{self.server}/api/enduser/login"
        payload = {
            "device_serial": self.serial,
            "username": username,
            "pin": pin
        }
        try:
            resp = self.session.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('success'):
                    return data
                else:
                    log(f"Login failed: {data.get('error')}")
                    return None
            else:
                log(f"Login HTTP {resp.status_code}: {resp.text}")
                return None
        except Exception as e:
            log(f"Login request error: {e}", 'error')
            return None

    def logout(self):
        """Notify server of logout."""
        url = f"{self.server}/api/enduser/logout"
        payload = {"device_serial": self.serial}
        try:
            self.session.post(url, json=payload, timeout=5)
        except:
            pass

    def sync_policy(self):
        """Fetch current policy for the active user (based on active session)."""
        url = f"{self.server}/api/device/policy/{self.serial}"
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"Policy sync error: {e}", 'error')
        return None

# ========== UI Components ==========
class LoginWindow:
    def __init__(self, client: ZeroAxisClient, on_login_success):
        self.client = client
        self.on_success = on_login_success
        self.root = tk.Tk()
        self.root.title("ZeroAxis Login")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#1a1a2e")
        self.create_widgets()

    def create_widgets(self):
        frame = tk.Frame(self.root, bg="#1a1a2e")
        frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(frame, text="ZeroAxis Device", font=("Arial", 24, "bold"),
                 fg="white", bg="#1a1a2e").pack(pady=10)

        tk.Label(frame, text="Username", fg="white", bg="#1a1a2e").pack(pady=(20,0))
        self.username_entry = tk.Entry(frame, width=30, font=("Arial", 14))
        self.username_entry.pack(pady=5)

        tk.Label(frame, text="PIN", fg="white", bg="#1a1a2e").pack(pady=(10,0))
        self.pin_entry = tk.Entry(frame, width=30, font=("Arial", 14), show="*")
        self.pin_entry.pack(pady=5)

        self.login_btn = tk.Button(frame, text="Login", command=self.do_login,
                                   bg="#206bc4", fg="white", font=("Arial", 12),
                                   width=20)
        self.login_btn.pack(pady=20)

        self.status_label = tk.Label(frame, text="", fg="red", bg="#1a1a2e")
        self.status_label.pack()

        self.username_entry.bind("<Return>", lambda e: self.do_login())
        self.pin_entry.bind("<Return>", lambda e: self.do_login())

    def do_login(self):
        username = self.username_entry.get().strip()
        pin = self.pin_entry.get().strip()
        if not username:
            self.status_label.config(text="Username required")
            return
        self.login_btn.config(state="disabled", text="Logging in...")
        self.status_label.config(text="")
        self.root.update()

        def auth():
            result = self.client.login(username, pin)
            self.root.after(0, lambda: self._login_callback(result))

        threading.Thread(target=auth, daemon=True).start()

    def _login_callback(self, result):
        self.login_btn.config(state="normal", text="Login")
        if result and result.get('success'):
            self.root.destroy()
            self.on_success(result)
        else:
            self.status_label.config(text="Invalid username or PIN")

    def run(self):
        self.root.mainloop()

class LauncherApp:
    def __init__(self, client: ZeroAxisClient, user_data: dict, enforcer: PolicyEnforcer):
        self.client = client
        self.user_data = user_data
        self.enforcer = enforcer
        self.root = tk.Tk()
        self.root.title("ZeroAxis Launcher")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#1a1a2e")
        self.running_apps = {}   # store subprocess objects
        self.create_ui()
        self.start_policy_sync()
        self.start_screen_time_tracking()

    def create_ui(self):
        # Top bar
        top_frame = tk.Frame(self.root, bg="#206bc4", height=40)
        top_frame.pack(fill="x")
        tk.Label(top_frame, text=f"Welcome, {self.user_data.get('username')}",
                 fg="white", bg="#206bc4", font=("Arial", 12)).pack(side="left", padx=10)
        logout_btn = tk.Button(top_frame, text="Logout", command=self.logout,
                               bg="#dc3545", fg="white", font=("Arial", 10))
        logout_btn.pack(side="right", padx=10)

        # Buttons for Browser and Documents
        button_frame = tk.Frame(self.root, bg="#1a1a2e")
        button_frame.pack(fill="x", padx=10, pady=5)
        browser_btn = tk.Button(button_frame, text="Browser", command=self.open_browser,
                                bg="#2E86AB", fg="white", font=("Arial", 10))
        browser_btn.pack(side="left", padx=5, expand=True, fill="x")
        docs_btn = tk.Button(button_frame, text="Documents", command=self.open_documents,
                             bg="#2E86AB", fg="white", font=("Arial", 10))
        docs_btn.pack(side="left", padx=5, expand=True, fill="x")

        # Main area: grid of app icons
        self.apps_frame = tk.Frame(self.root, bg="#1a1a2e")
        self.apps_frame.pack(fill="both", expand=True, padx=20, pady=20)
        self.refresh_apps()

        # Status bar
        self.status_var = tk.StringVar()
        status_bar = tk.Label(self.root, textvariable=self.status_var,
                              bg="#333", fg="white", anchor="w")
        status_bar.pack(side="bottom", fill="x")

    def refresh_apps(self):
        # Clear existing widgets
        for widget in self.apps_frame.winfo_children():
            widget.destroy()
        # Create buttons for each allowed app
        apps = self.enforcer.allowed_apps
        if not apps:
            tk.Label(self.apps_frame, text="No apps allowed. Contact administrator.",
                     fg="white", bg="#1a1a2e", font=("Arial", 14)).pack()
            return
        # Arrange in a grid (max 4 columns)
        row, col = 0, 0
        for app in apps:
            btn = tk.Button(self.apps_frame, text=app, command=lambda a=app: self.launch_app(a),
                            bg="#2563eb", fg="white", font=("Arial", 12), width=20, height=2)
            btn.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")
            col += 1
            if col >= 4:
                col = 0
                row += 1
        # Configure grid weights
        for i in range(4):
            self.apps_frame.columnconfigure(i, weight=1)
        for i in range(row+1):
            self.apps_frame.rowconfigure(i, weight=1)

    def launch_app(self, app_name):
        # Check screen time and curfew before launching
        if self.enforcer.check_screen_time():
            self.show_message("Screen time limit reached. Device will lock.")
            self.lock_and_logout()
            return
        if self.enforcer.is_curfew_active():
            self.show_message("Curfew active. Device locked.")
            self.lock_and_logout()
            return
        self.enforcer.launch_app(app_name)
        # Note: we don't track running apps for switching (simplified).
        # A full solution would have a taskbar with running apps.

    def start_policy_sync(self):
        def sync_loop():
            while True:
                time.sleep(900)  # 15 minutes
                try:
                    pol = self.client.sync_policy()
                    if pol and 'policy' in pol:
                        self.enforcer.apply_policies(pol['policy'])
                        self.root.after(0, self.refresh_apps)
                except:
                    pass
        threading.Thread(target=sync_loop, daemon=True).start()

    def start_screen_time_tracking(self):
        def track_loop():
            while True:
                time.sleep(60)  # update every minute
                self.enforcer.today_usage += 1
                if self.enforcer.check_screen_time():
                    self.root.after(0, lambda: self.lock_and_logout())
                # Update status bar
                if self.enforcer.screen_time_limit > 0:
                    remaining = max(0, self.enforcer.screen_time_limit - self.enforcer.today_usage)
                    self.root.after(0, lambda: self.status_var.set(f"Screen time remaining: {remaining} min"))
        threading.Thread(target=track_loop, daemon=True).start()

    def lock_and_logout(self):
        self.show_message("Policy violation. Logging out and locking device.")
        self.logout()
        lock_workstation()
        # Exit the launcher to return to login (the login screen will restart)
        self.root.quit()
        sys.exit(0)

    def logout(self):
        self.client.logout()
        # Kill all launched apps
        for proc in self.running_apps.values():
            try:
                proc.terminate()
            except:
                pass
        self.running_apps.clear()

    def show_message(self, msg):
        messagebox.showwarning("ZeroAxis", msg)

    def open_browser(self):
        browser = self.enforcer.allowed_browser
        if browser:
            try:
                subprocess.Popen([browser])
            except Exception as e:
                messagebox.showerror("Error", f"Failed to launch browser: {e}")
        else:
            messagebox.showwarning("No Browser", "No browser app configured for this mode.")

    def open_documents(self):
        viewer = self.enforcer.allowed_document_viewer
        if viewer:
            try:
                subprocess.Popen([viewer])
            except Exception as e:
                messagebox.showerror("Error", f"Failed to launch document viewer: {e}")
        else:
            messagebox.showwarning("No Viewer", "No document viewer configured for this mode.")

    def run(self):
        self.root.mainloop()

# ========== Main entry point ==========
def main():
    # Ensure script runs as shell (no console)
    # If compiled with PyInstaller --noconsole, this is fine.
    DEVICE_SERIAL = get_serial()
    if not DEVICE_SERIAL:
        log("Could not determine device serial", 'error')
        sys.exit(1)

    # Set this executable as shell (if not already)
    exe_path = sys.executable if getattr(sys, 'frozen', False) else __file__
    set_shell(exe_path)

    # Apply key restrictions
    block_keys()

    client = ZeroAxisClient(SERVER_URL, DEVICE_SERIAL)
    enforcer = PolicyEnforcer()

    # Main loop: show login until success, then launcher
    while True:
        login_win = LoginWindow(client, lambda user_data: start_launcher(client, user_data, enforcer))
        login_win.run()
        # After login window closes, launcher will run; if launcher exits, loop restarts.

def start_launcher(client, user_data, enforcer):
    # Apply policies from login response
    policies = user_data.get('policies', {})
    enforcer.apply_policies(policies)
    launcher = LauncherApp(client, user_data, enforcer)
    launcher.run()

if __name__ == "__main__":
    main()