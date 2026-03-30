import time
import os
import pwd
import sqlite3
import shutil
import magic
import logging
from datetime import datetime, timedelta
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ================== 1. Configuration & Retention ==================

# [MODIFIED: Hansol] Set retention to 5 mins for testing; easily scalable to 3 days (4320 mins)
RETENTION_MINUTES = 5 

# [ADDED: Hansol] Strict Extension Rules: 
# This acts as a 'Hard Rule' to prioritize extensions over AI/MIME predictions.
# Prevents errors like 'include_audio.avi' being classified as Music.
EXTENSION_RULES = {
    '.heic': 'Pictures', '.jpg': 'Pictures', '.jpeg': 'Pictures', '.png': 'Pictures', '.gif': 'Pictures',
    '.avi': 'Videos', '.mp4': 'Videos', '.mkv': 'Videos', '.mov': 'Videos',
    '.mp3': 'Music', '.wav': 'Music',
    '.pdf': 'Documents', '.docx': 'Documents', '.txt': 'Documents',
    '.zip': 'Downloads', '.gz': 'Downloads'
}

def get_real_user_info():
    """Find the home directory of the actual user (hansol221)."""
    try:
        script_stat = os.stat(__file__)
        user_info = pwd.getpwuid(script_stat.st_uid)
        return user_info.pw_name, user_info.pw_dir
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

# ================== 2. Automatic Cleanup Logic ==================

def init_db():
    """Initialize database to track file movement times."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS tracked_files (id INTEGER PRIMARY KEY, path TEXT, date TIMESTAMP)")

def cleanup_expired_files():
    """Deletes files from target folders after the retention period expires."""
    limit_time = datetime.now() - timedelta(minutes=RETENTION_MINUTES)
    
    with sqlite3.connect(DB_PATH) as conn:
        expired = conn.execute("SELECT id, path FROM tracked_files WHERE date < ?", (limit_time,)).fetchall()
        for fid, fpath in expired:
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
                    logging.info(f"CLEANUP: Deleted expired file {fpath}")
                conn.execute("DELETE FROM tracked_files WHERE id = ?", (fid,))
            except Exception as e:
                logging.error(f"Cleanup Error for {fpath}: {e}")

# ================== 3. File Processing Handler ==================

# [ADDED: Hansol] Integrity Check: 
# Checks if the file is fully written by comparing sizes over a 0.5s interval.
# This prevents moving 0-byte or corrupted files during active downloads.
def is_file_finished(filepath):
    try:
        if os.path.getsize(filepath) == 0: return False
        size1 = os.path.getsize(filepath)
        time.sleep(0.5)
        size2 = os.path.getsize(filepath)
        return size1 == size2
    except OSError: return False

class DownloadHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory: self.handle_event(event.src_path)

    # [ADDED: Hansol] Handle Move/Rename Events: 
    # Browsers often rename temp files (.crdownload) to final names upon completion.
    def on_moved(self, event):
        if not event.is_directory: self.handle_event(event.dest_path)

    def handle_event(self, src_path):
        filepath = Path(src_path)
        if filepath.suffix.lower() in {'.part', '.crdownload', '.tmp'}: return

        # [MODIFIED: Hansol] Stability Retry Loop: 
        # Waits for the file to be 'finished' before processing to avoid integrity errors.
        for _ in range(10):
            if is_file_finished(str(filepath)):
                self.process_file(filepath)
                break
            time.sleep(1)

    def process_file(self, filepath):
        try:
            filename = filepath.name
            ext = filepath.suffix.lower()
            
            # [MODIFIED: Hansol] Priority 1: Check Extension Rules First (Hard Rule)
            # This fixes the issue where keywords like 'audio' in filenames confused the AI.
            category = EXTENSION_RULES.get(ext)
            
            # [MODIFIED: Hansol] Priority 2: Fallback to MIME Type/AI if no extension rule exists
            if not category:
                mime = magic.from_file(str(filepath), mime=True)
                if "video" in mime: category = "Videos"
                elif "audio" in mime: category = "Music"
                elif "image" in mime: category = "Pictures"
                else: category = "Downloads"

            # 3. Determine Final Path
            target_dir = os.path.join(USER_HOME, category)
            # [MODIFIED: Hansol] Auto-create folder if missing for better script independence.
            if not os.path.exists(target_dir): os.makedirs(target_dir)

            dest_path = os.path.join(target_dir, filename)
            
            # [ADDED: Hansol] Collision Handling: Prevents overwriting existing files.
            if os.path.exists(dest_path):
                dest_path = os.path.join(target_dir, f"{int(time.time())}_{filename}")
            
            shutil.move(str(filepath), dest_path)
            
            # 4. Log to DB for later cleanup
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("INSERT INTO tracked_files (path, date) VALUES (?, ?)", (dest_path, datetime.now()))
            
            logging.info(f"DISTRIBUTED: {filename} -> {target_dir}")
        except Exception as e:
            logging.error(f"Move error: {e}")

# ================== 4. Main Execution ==================

def main():
    init_db()
    observer = Observer()
    observer.schedule(DownloadHandler(), DOWNLOADS_DIR, recursive=False)
    observer.start()
    logging.info(f"Daemon Started. Auto-cleanup interval: {RETENTION_MINUTES} mins.")
    
    try:
        while True:
            # [MODIFIED: Hansol] Regular Cleanup: Checks for expired files every minute.
            cleanup_expired_files()
            time.sleep(60) 
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
