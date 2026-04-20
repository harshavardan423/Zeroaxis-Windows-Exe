import tkinter as tk
from tkinter import ttk, messagebox
import requests
import subprocess
import sys
import os
import tempfile
import time
import logging
import winreg
import ctypes
import uuid

# ==================== CONFIGURATION ====================
SERVER = "https://zeroaxis.live"
MESH_AGENT_URL = "https://zeroaxis.live/mesh/meshagents?id=4&meshid=lo4dBoYli%40zTegCG5VsnliUmMCcVH6ckdunm%40K%24vIQEatq%40yTsq1uBSb8ERoaJWU&installflags=0"
LOG_FILE = r"C:\Users\Public\ZeroAxis\bootstrap.log"
# ======================================================

# Setup logging
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def log(msg, level='info'):
    getattr(logging, level)(msg)
    print(msg)

def is_admin():
    """Check if running as administrator."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def get_serial():
    """Get stable MachineGuid from registry."""
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        if guid and guid.upper() not in ('UNKNOWN', 'NONE', 'NULL', ''):
            return guid
    except Exception as e:
        log(f"Registry read failed: {e}", 'error')
    
    # Fallback: WMIC
    try:
        result = subprocess.run(
            ["wmic", "bios", "get", "serialnumber"],
            capture_output=True, text=True, timeout=5
        )
        serial = result.stdout.strip().split("\n")[-1].strip()
        if serial and serial.upper() not in ('UNKNOWN', 'TO BE FILLED BY O.E.M.', 'NONE', ''):
            return serial
    except Exception as e:
        log(f"WMIC fallback failed: {e}", 'error')
    
    # Last resort
    log("Using generated UUID as serial", 'warning')
    return str(uuid.uuid4())

def check_enrolled(serial):
    """Check if device is already enrolled."""
    try:
        r = requests.get(f"{SERVER}/api/devices/check/{serial}", timeout=10)
        if r.status_code == 200:
            return r.json().get('healthy', False)
        return False
    except Exception as e:
        log(f"Enrollment check failed: {e}", 'error')
        return False

def wait_for_online(serial, status_label=None, timeout=120):
    """Poll Flask until device shows online."""
    start = time.time()
    session = requests.Session()
    while time.time() - start < timeout:
        try:
            r = session.get(f"{SERVER}/api/devices/status/{serial}", timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get('status') == 'online':
                    return True
        except:
            pass
        if status_label:
            elapsed = int(time.time() - start)
            status_label.config(text=f"Waiting for device to come online... ({elapsed}s)")
            status_label.update()
        time.sleep(5)
    return False

def fetch_groups():
    """Fetch district/block/school hierarchy."""
    try:
        r = requests.get(f"{SERVER}/api/groups", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"Failed to fetch groups: {e}", 'error')
        return []

def register_device(serial, name, district_id, block_id, school_id):
    """Register device with Flask."""
    try:
        payload = {
            "serial": serial,
            "name": name,
            "platform": "windows",
            "district_id": district_id,
            "block_id": block_id,
            "school_id": school_id
        }
        r = requests.post(f"{SERVER}/api/devices/register", json=payload, timeout=10)
        return r.status_code in (200, 201)
    except Exception as e:
        log(f"Registration failed: {e}", 'error')
        return False

def download_and_install_agent(status_label=None):
    """Download MeshAgent with retries and install."""
    agent_path = os.path.join(tempfile.gettempdir(), "MeshAgent.exe")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if status_label:
                status_label.config(text=f"Downloading agent (attempt {attempt+1})...")
                status_label.update()
            log(f"Downloading MeshAgent from {MESH_AGENT_URL}")
            r = requests.get(MESH_AGENT_URL, timeout=120, stream=True, verify=False)
            r.raise_for_status()
            with open(agent_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            break
        except Exception as e:
            log(f"Download attempt {attempt+1} failed: {e}", 'error')
            if attempt == max_retries - 1:
                return False
            time.sleep(5)
    
    if status_label:
        status_label.config(text="Installing agent...")
        status_label.update()
    
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", agent_path, "-fullinstall", None, 1)
        if ret <= 32:
            raise Exception(f"ShellExecute failed with code {ret}")
        log("MeshAgent installation initiated")
        return True
    except Exception as e:
        log(f"Installation failed: {e}", 'error')
        return False

def wait_for_meshagent_service(status_label=None, timeout=60):
    """Wait for Mesh Agent service to be in running state."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = subprocess.run(
                ["sc", "query", "Mesh Agent"],
                capture_output=True, text=True, timeout=5
            )
            if "RUNNING" in result.stdout:
                log("Mesh Agent service is running")
                return True
        except:
            pass
        if status_label:
            elapsed = int(time.time() - start)
            status_label.config(text=f"Waiting for Mesh Agent service... ({elapsed}s)")
            status_label.update()
        time.sleep(3)
    log("Mesh Agent service did not start within timeout", 'warning')
    return False

def get_id_from_name(groups, district_name, block_name, school_name):
    district = next((d for d in groups if d["name"] == district_name), None)
    if not district:
        return None, None, None
    block = next((b for b in district.get("blocks", []) if b["name"] == block_name), None)
    if not block:
        return district["id"], None, None
    school = next((s for s in block.get("schools", []) if s["name"] == school_name), None)
    school_id = school["id"] if school else None
    return district["id"], block["id"], school_id

def show_prompt(serial, groups):
    root = tk.Tk()
    root.title("Zeroaxis Device Setup")
    root.geometry("450x480")
    root.resizable(False, False)
    root.configure(bg="#1a1a2e")

    header = tk.Frame(root, bg="#206bc4", pady=15)
    header.pack(fill="x")
    tk.Label(header, text="Zeroaxis MDM", font=("Arial", 16, "bold"), bg="#206bc4", fg="white").pack()
    tk.Label(header, text="Device Enrollment", font=("Arial", 10), bg="#206bc4", fg="#cce0ff").pack()

    body = tk.Frame(root, bg="#1a1a2e", padx=30, pady=20)
    body.pack(fill="both", expand=True)

    tk.Label(body, text=f"Serial: {serial}", bg="#1a1a2e", fg="#888", font=("Arial", 9)).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0,10))

    tk.Label(body, text="Device Name:", bg="#1a1a2e", fg="white").grid(row=1, column=0, sticky="w", pady=5)
    name_var = tk.StringVar(value=f"WIN-{serial[-6:]}")
    tk.Entry(body, textvariable=name_var, width=25).grid(row=1, column=1, sticky="w", pady=5)

    tk.Label(body, text="District:", bg="#1a1a2e", fg="white").grid(row=2, column=0, sticky="w", pady=5)
    district_var = tk.StringVar()
    district_cb = ttk.Combobox(body, textvariable=district_var, width=23, state="readonly")
    district_cb["values"] = [d["name"] for d in groups]
    district_cb.grid(row=2, column=1, sticky="w", pady=5)

    tk.Label(body, text="Block:", bg="#1a1a2e", fg="white").grid(row=3, column=0, sticky="w", pady=5)
    block_var = tk.StringVar()
    block_cb = ttk.Combobox(body, textvariable=block_var, width=23, state="readonly")
    block_cb.grid(row=3, column=1, sticky="w", pady=5)

    tk.Label(body, text="School:", bg="#1a1a2e", fg="white").grid(row=4, column=0, sticky="w", pady=5)
    school_var = tk.StringVar()
    school_cb = ttk.Combobox(body, textvariable=school_var, width=23, state="readonly")
    school_cb.grid(row=4, column=1, sticky="w", pady=5)

    status_label = tk.Label(body, text="", bg="#1a1a2e", fg="#aaa", font=("Arial", 9))
    status_label.grid(row=6, column=0, columnspan=2, pady=(0, 5))

    result = {"confirmed": False}

    def on_district_change(event):
        selected = district_var.get()
        district = next((d for d in groups if d["name"] == selected), None)
        if district:
            block_cb["values"] = [b["name"] for b in district.get("blocks", [])]
            block_var.set("")
            school_cb["values"] = []
            school_var.set("")

    def on_block_change(event):
        selected_district = district_var.get()
        selected_block = block_var.get()
        district = next((d for d in groups if d["name"] == selected_district), None)
        if district:
            block = next((b for b in district.get("blocks", []) if b["name"] == selected_block), None)
            if block:
                school_cb["values"] = [s["name"] for s in block.get("schools", [])]
                school_var.set("")

    district_cb.bind("<<ComboboxSelected>>", on_district_change)
    block_cb.bind("<<ComboboxSelected>>", on_block_change)

    def on_confirm():
        if not district_var.get() or not block_var.get():
            messagebox.showerror("Error", "Please select a district and block.")
            return

        result["confirmed"] = True
        result["name"] = name_var.get()
        result["district"] = district_var.get()
        result["block"] = block_var.get()
        result["school"] = school_var.get()
        btn.config(state="disabled", text="Enrolling...")
        status_label.config(text="Registering device...")
        root.update()

        district_id, block_id, school_id = get_id_from_name(
            groups, result["district"], result["block"], result["school"]
        )

        success = register_device(
            serial=serial,
            name=result["name"],
            district_id=district_id,
            block_id=block_id,
            school_id=school_id
        )

        if not success:
            messagebox.showerror("Error", "Failed to register device. Check server connection.")
            btn.config(state="normal", text="Confirm & Enroll")
            status_label.config(text="")
            return

        ok = download_and_install_agent(status_label)
        if not ok:
            messagebox.showerror("Error", "Agent install failed. Device registered but agent not installed.")
            root.destroy()
            return

        status_label.config(text="Waiting for Mesh Agent service...")
        root.update()
        if not wait_for_meshagent_service(status_label):
            messagebox.showwarning("Warning", "Mesh Agent service did not start in time. It may start later.")
        else:
            time.sleep(5)

        status_label.config(text="Connecting to management server...")
        root.update()
        online = wait_for_online(serial, status_label, timeout=120)

        if online:
            status_label.config(text="Installing Windows agent...")
            root.update()
            install_windows_agent()
            status_label.config(text="Enrolled successfully!")
            messagebox.showinfo("Success", "Device enrolled and Windows agent installed. The device will reboot to activate kiosk mode.")
            # Optional: reboot to apply shell change
            import subprocess
            subprocess.run(["shutdown", "/r", "/t", "10", "/c", "ZeroAxis setup complete. Rebooting in 10 seconds..."])
        else:
            messagebox.showwarning("Partial Success", "Agent installed but device hasn't come online yet. It may take a few more minutes.")
        root.destroy()

    btn = tk.Button(body, text="Confirm & Enroll", command=on_confirm,
                    bg="#206bc4", fg="white", font=("Arial", 11, "bold"),
                    relief="flat", padx=20, pady=8, cursor="hand2")
    btn.grid(row=5, column=0, columnspan=2, pady=15)

    root.mainloop()
    return result

def install_windows_agent():
    """Download the ZeroAxis Windows Agent EXE and set it as the shell."""
    agent_url = "https://zeroaxis.live/downloads/ZeroAxisAgent.exe"   # <-- change to your actual URL
    agent_dir = r"C:\Program Files\ZeroAxis"
    agent_exe = os.path.join(agent_dir, "ZeroAxisAgent.exe")

    try:
        if not os.path.exists(agent_dir):
            os.makedirs(agent_dir)
            log(f"Created directory {agent_dir}")

        # Download the agent
        log(f"Downloading Windows agent from {agent_url}")
        import urllib.request
        urllib.request.urlretrieve(agent_url, agent_exe)
        log(f"Agent downloaded to {agent_exe}")

        # Set as Windows shell (replaces explorer.exe)
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon",
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "Shell", 0, winreg.REG_SZ, agent_exe)
        winreg.CloseKey(key)
        log("Agent set as Windows shell (will start instead of Explorer)")

        # Disable Task Manager
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                   r"Software\Microsoft\Windows\CurrentVersion\Policies\System")
            winreg.SetValueEx(key, "DisableTaskMgr", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(key)
        except:
            pass

        return True
    except Exception as e:
        log(f"Failed to install Windows agent: {e}", 'error')
        return False

def main():
    if not is_admin():
        messagebox.showerror("Error", "Please run this program as Administrator.")
        sys.exit(1)
    
    log("Starting ZeroAxis Bootstrap")
    serial = get_serial()
    log(f"Device serial: {serial}")

    if check_enrolled(serial):
        log("Device already enrolled, installing agent...")
        download_and_install_agent()
        sys.exit(0)

    groups = fetch_groups()
    if not groups:
        messagebox.showerror("Error", "Cannot reach server. Check your internet connection.")
        sys.exit(1)

    show_prompt(serial, groups)

if __name__ == "__main__":
    main()