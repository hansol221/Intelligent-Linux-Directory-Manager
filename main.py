#!/usr/bin/env python3
"""
Main entry point for the daemon.
Installs dependencies and starts the daemon in the background.
"""
import subprocess
import sys
import os
import shutil
from pathlib import Path

# Map package names to their import names
# [UPDATE: Hansol] Added 'joblib' and 'pandas' to support AI model inference in daemon.py.
PACKAGES = {
    "scikit-learn": "sklearn",
    "watchdog": "watchdog",
    "python-magic": "python-magic",
    "joblib": "joblib",
    "pandas": "pandas"
}
SERVICE_NAME = "sorty-daemon"
SERVICE_FILE = f"/etc/systemd/system/{SERVICE_NAME}.service"
VENV_DIR = ".venv"

def create_virtual_env(project_dir):
    venv_path = os.path.join(project_dir, VENV_DIR)
    print(f"Creating virtual env in {venv_path}...")
    try:
        subprocess.run([sys.executable, "-m", "venv", venv_path], check=True, capture_output=True)
        print(f"✓ Virtual environment created")
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed: {e.stderr.decode()}")
        sys.exit(1)
    return venv_path

def check_and_install_dependencies(venv_path):
    pip_path = os.path.join(venv_path, "bin", "pip")
    for package in PACKAGES:
        print(f"Installing {package}...")
        try:
            subprocess.run([pip_path, "install", package], check=True, capture_output=True)
            print(f"✓ {package} installed")
        except subprocess.CalledProcessError as e:
            print(f"✗ Failed to install {package}: {e.stderr.decode()}")
            sys.exit(1)

def write_systemd_service(project_dir, venv_path):
    python_path = os.path.join(venv_path, "bin", "python")
    daemon_path = os.path.join(project_dir, "daemon.py")

    # [UPDATE: Hansol] Clarified service description and set User to 'hansol221' 
    # to ensure the daemon has correct permissions for the specific user's Home/Downloads.
    service_content = f"""[Unit]
Description=AI-Powered File Manager Daemon
After=network.target

[Service]
Type=simple
User=hansol221
WorkingDirectory={project_dir}
ExecStart={python_path} {daemon_path}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    with open(SERVICE_FILE, "w") as f:
        f.write(service_content)
    print(f"✓ Created service file: {SERVICE_FILE}")

def enable_and_start_service():
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True)
        subprocess.run(["systemctl", "start", SERVICE_NAME], check=True)
        print(f"✓ Daemon is now active.")
    except subprocess.CalledProcessError as e:
        print(f"✗ Service failed: {e}")
        sys.exit(1)

def main():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    venv_path = create_virtual_env(project_dir)
    check_and_install_dependencies(venv_path)

    # [MODIFIED: Hansol] Removed 'install_libmagic' and 'cold_start.py' execution.
    # The new daemon.py is now more independent and self-sufficient, 
    # handling directory creation dynamically without needing a pre-scan.

    if (shutil.which("systemctl") is None):
        print("systemd not found")
        sys.exit(1)

    write_systemd_service(project_dir, venv_path)
    enable_and_start_service()

if __name__ == "__main__":
    main()
