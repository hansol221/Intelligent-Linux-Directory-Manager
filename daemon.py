"""
Download monitoring daemon.

This service watches the Downloads directory, classifies files with a trained
model, moves them into category directories, and records actions in SQLite.
"""

import time
import os
import pwd
import sqlite3
import shutil
import magic
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ================== 1. Configuration ==================
RETENTION_MINUTES = 5 

EXTENSION_RULES = {
    '.heic': 'Pictures', '.jpg': 'Pictures', '.jpeg': 'Pictures', '.png': 'Pictures', '.gif': 'Pictures',
    '.avi': 'Videos', '.mp4': 'Videos', '.mkv': 'Videos', '.mov': 'Videos',
    '.mp3': 'Music', '.wav': 'Music',
    '.pdf': 'Documents', '.docx': 'Documents', '.txt': 'Documents',
    '.zip': 'Downloads', '.gz': 'Downloads'
}

def get_real_user_info():
    try:
        real_user = os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name
        user_home = os.path.expanduser(f"~{real_user}")
        return real_user, user_home
    except Exception:
        return os.getlogin(), os.path.expanduser("~")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REAL_USER, USER_HOME = get_real_user_info()
DOWNLOADS_DIR = os.path.join(USER_HOME, "Downloads")
DB_PATH = os.path.join(PROJECT_DIR, "file_tracker.db")

logging.basicConfig(
    filename=f"{PROJECT_DIR}/download_daemon.log", 
    level=logging.INFO, 
    format='%(asctime)s - %(message)s'
)

# ================== 2. Utilities ==================

def notify_user(filename):
    """Send a desktop notification to the real user.

    When running as a root systemd service, we try three approaches in order
    and stop at the first one that succeeds:

    1. sudo -u with explicit DISPLAY, DBUS, and XDG_RUNTIME_DIR env vars.
    2. systemd-run inside the user's own session (requires lingering enabled).
    3. su -c as a last resort.
    """
    uid = pwd.getpwnam(REAL_USER).pw_uid
    message = f"'{filename}' moved to TemporaryFile_Trash folder."
    dbus_addr = f"unix:path=/run/user/{uid}/bus"
    xdg_dir   = f"/run/user/{uid}"

    strategies = [
        # 1. sudo -u with all required GUI env vars explicitly set
        [
            "sudo", "-u", REAL_USER,
            "env",
            "DISPLAY=:0",
            f"DBUS_SESSION_BUS_ADDRESS={dbus_addr}",
            f"XDG_RUNTIME_DIR={xdg_dir}",
            "notify-send", "--urgency=normal", "File Moved", message
        ],
        # 2. systemd-run inside the user's session
        [
            "systemd-run",
            f"--uid={REAL_USER}",
            f"--setenv=DISPLAY=:0",
            f"--setenv=DBUS_SESSION_BUS_ADDRESS={dbus_addr}",
            f"--setenv=XDG_RUNTIME_DIR={xdg_dir}",
            "--pipe", "--wait", "--quiet",
            "notify-send", "--urgency=normal", "File Moved", message
        ],
        # 3. su fallback
        [
            "su", "-", REAL_USER, "-c",
            f"DISPLAY=:0 "
            f"DBUS_SESSION_BUS_ADDRESS={dbus_addr} "
            f"XDG_RUNTIME_DIR={xdg_dir} "
            f"notify-send --urgency=normal 'File Moved' '{message}'"
        ],
    ]

    for cmd in strategies:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            if result.returncode == 0:
                logging.info(f"notify_user succeeded: {' '.join(cmd[:3])}")
                return
            logging.warning(
                f"notify_user strategy failed (rc={result.returncode}): "
                f"{' '.join(cmd[:4])} | stderr: {result.stderr.decode().strip()}"
            )
        except Exception as e:
            logging.warning(f"notify_user strategy error: {e}")

    logging.error("notify_user: all strategies failed")

def safe_dest(directory: str, filename: str) -> str:
    """
    Return a collision-free destination path.
    If a file with the same name already exists, appends a timestamp:
    stem_YYYYMMDD_HHMMSS.ext
    Truncates the stem to 200 chars to stay within the OS 255-char filename limit.
    """
    dest = os.path.join(directory, filename)
    if not os.path.exists(dest):
        return dest
    p      = Path(filename)
    suffix = p.suffix
    stem   = p.stem[:200]   # safety margin for the 255-char OS limit
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(directory, f"{stem}_{ts}{suffix}")

def is_file_finished(filepath):
    try:
        if os.path.getsize(filepath) == 0: return False
        size1 = os.path.getsize(filepath)
        time.sleep(0.5)
        size2 = os.path.getsize(filepath)
        return size1 == size2
    except OSError: return False

def has_been_opened(fpath):
    """
    Return True if the file has been opened at least once after being tracked.

    Strategy:
      - At distribution time, we snapshot the file's atime as `initial_atime`.
      - If the current atime has advanced beyond that baseline by more than
        2 seconds (filesystem noise tolerance), the user opened the file.
      - Falls back to comparing atime vs mtime when no DB baseline exists.
    """
    try:
        current_atime = os.path.getatime(fpath)

        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT initial_atime FROM tracked_files WHERE path = ?", (fpath,)
            ).fetchone()

        if row and row[0] is not None:
            baseline_atime = row[0]
        else:
            baseline_atime = os.path.getmtime(fpath)

        # 2-second tolerance to ignore filesystem/OS noise
        return current_atime > baseline_atime + 2

    except Exception:
        return False

# ================== 3. Core Trash Logic ==================

def cleanup_expired_files():
    """Move files that have NEVER been opened after RETENTION_MINUTES to TemporaryFile_Trash."""
    limit_time = datetime.now() - timedelta(minutes=RETENTION_MINUTES)
    limit_timestamp = limit_time.timestamp()

    trash_dir = os.path.join(USER_HOME, "TemporaryFile_Trash")  # renamed from Archive_Trash
    if not os.path.exists(trash_dir):
        os.makedirs(trash_dir, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        tracked = conn.execute("SELECT id, path FROM tracked_files").fetchall()

        for fid, fpath in tracked:
            try:
                if os.path.exists(fpath):
                    # Only act on files older than the retention window
                    if os.path.getmtime(fpath) < limit_timestamp:

                        # ── Guard: skip files the user has opened ──────────
                        if has_been_opened(fpath):
                            logging.info(f"SKIPPED (opened by user): {os.path.basename(fpath)}")
                            # Remove from tracking – no need to keep checking
                            conn.execute("DELETE FROM tracked_files WHERE id = ?", (fid,))
                            continue
                        # ──────────────────────────────────────────────────

                        filename  = os.path.basename(fpath)
                        dest_path = safe_dest(trash_dir, filename)

                        shutil.move(fpath, dest_path)
                        notify_user(filename)
                        logging.info(f"MOVED TO TemporaryFile_Trash: {filename}")
                        conn.execute("DELETE FROM tracked_files WHERE id = ?", (fid,))
                else:
                    conn.execute("DELETE FROM tracked_files WHERE id = ?", (fid,))
            except Exception as e:
                logging.error(f"Error processing {fpath}: {e}")

# ================== 4. Distribution Logic ==================

class DownloadHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory: self.handle_event(event.src_path)
    def on_moved(self, event):
        if not event.is_directory: self.handle_event(event.dest_path)

    def handle_event(self, src_path):
        filepath = Path(src_path)
        if filepath.suffix.lower() in {'.part', '.crdownload', '.tmp'}: return
        for _ in range(10):
            if is_file_finished(str(filepath)):
                self.process_file(filepath)
                break
            time.sleep(1)

    def process_file(self, filepath):
        try:
            filename = filepath.name
            ext = filepath.suffix.lower()
            category = EXTENSION_RULES.get(ext)
            
            if not category:
                mime = magic.from_file(str(filepath), mime=True)
                if "video" in mime: category = "Videos"
                elif "audio" in mime: category = "Music"
                elif "image" in mime: category = "Pictures"
                else: category = "Downloads"

            target_dir = os.path.join(USER_HOME, category)
            os.makedirs(target_dir, exist_ok=True)
            dest_path = safe_dest(target_dir, filename)
            
            shutil.move(str(filepath), dest_path)

            # Snapshot atime right after move – used as baseline for open-detection
            initial_atime = os.path.getatime(dest_path)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO tracked_files (path, date, initial_atime) VALUES (?, ?, ?)",
                    (dest_path, datetime.now(), initial_atime)
                )
            logging.info(f"DISTRIBUTED: {filename} -> {target_dir}")
        except Exception as e:
            logging.error(f"Error distributing {filepath}: {e}")

def main():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_files (
                id            INTEGER PRIMARY KEY,
                path          TEXT,
                date          TIMESTAMP,
                initial_atime REAL        -- atime snapshot taken right after download
            )
        """)
        # Migrate existing DB rows that don't have initial_atime yet
        try:
            conn.execute("ALTER TABLE tracked_files ADD COLUMN initial_atime REAL")
        except sqlite3.OperationalError:
            pass  # Column already exists – safe to ignore

    observer = Observer()
    observer.schedule(DownloadHandler(), DOWNLOADS_DIR, recursive=False)
    observer.start()
    logging.info(f"Daemon monitoring {DOWNLOADS_DIR} for user {REAL_USER}")

    try:
        while True:
            cleanup_expired_files()
            time.sleep(60)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
