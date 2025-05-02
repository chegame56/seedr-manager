import os
import sys
import json
import time
import shutil
import logging
import requests
import subprocess
import re
from datetime import datetime
from torrentool.api import Torrent
from seedrcc import Login, Seedr
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QProgressBar, QListWidget, QFileDialog, QStackedWidget,
    QMessageBox, QMenu
)
from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QBrush

# ---- Constants & Logging ----
SETTINGS_FILE = "settings.json"
HISTORY_FILE = "history.json"
DEFAULTS = {
    "username": "", "password": "",
    "download_dir": "", "idm_path": "", "torrent_folder": "",
    "max_retry_attempts": 3, "stalled_threshold": 400
}
TIME_LIMIT = 10000  # seconds per torrent

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('seedr_processor.log', encoding='utf-8')]
)

# ---- Helper Functions ----
def show_error(parent, title, message):
    QMessageBox.critical(parent, title, message)

def show_warning(parent, title, message):
    QMessageBox.warning(parent, title, message)

def show_info(parent, title, message):
    QMessageBox.information(parent, title, message)

def get_free_space(path):
    try:
        return shutil.disk_usage(path).free
    except Exception as e:
        logging.error(f"Failed to get free space: {e}")
        return 0

# ---- Persistence Helpers ----
def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
                # Ensure all default keys exist
                settings = DEFAULTS.copy()
                settings.update(loaded_settings)
                return settings
        return DEFAULTS.copy()
    except Exception as e:
        logging.error(f"Failed to load settings: {e}")
        show_error(None, "Settings Error", f"Failed to load settings: {str(e)}\nUsing defaults.")
        return DEFAULTS.copy()

def save_settings(cfg):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=4)
        logging.debug(f"Settings saved: {cfg}")
    except Exception as e:
        logging.error(f"Failed to save settings: {e}")
        show_error(None, "Settings Error", f"Failed to save settings: {str(e)}")

def load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []
    except Exception as e:
        logging.error(f"Failed to load history: {e}")
        show_error(None, "History Error", f"Failed to load history: {str(e)}")
        return []

def save_history(hist):
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(hist, f, indent=4)
        logging.debug(f"History saved, {len(hist)} records")
    except Exception as e:
        logging.error(f"Failed to save history: {e}")
        show_error(None, "History Error", f"Failed to save history: {str(e)}")

# ---- Network Functions ----
def fetch_torrent_progress(url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        r.raise_for_status()
        txt = r.text
        if txt.startswith('?'):
            m = re.search(r'\((.*?)\)$', txt)
            data = json.loads(m.group(1)) if m else {}
        else:
            data = r.json()
        p = data.get('progress') or data.get('stats', {}).get('progress') or 0
        return min(int(p), 101)
    except Exception as e:
        logging.debug(f"Progress error: {e}")
        return 0

def fetch_trackers(urls):
    all_trackers = set()
    for url in urls:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                all_trackers.update(line.strip() for line in r.text.splitlines() if line.strip())
        except Exception as e:
            logging.error(f"Tracker fetch error for {url}: {e}")
    return list(all_trackers)

def create_magnet_link(info_hash, name=None, trackers=None):
    link = f"magnet:?xt=urn:btih:{info_hash}"
    if name:
        link += f"&dn={name}"
    if trackers:
        for tracker in trackers:
            link += f"&tr={tracker}"
    return link

def find_video_files_and_get_links(account, log_file, idm_path, download_dir):
    video_links = []
    try:
        contents = account.listContents()
        for fd in contents.get('folders', []):
            files = account.listContents(folderId=fd['id']).get('files', [])
            for f in files:
                nm = f['name']
                if nm.lower().endswith(('.mp4', '.mkv', '.avi')):
                    fid = f.get('folder_file_id') or f.get('id')
                    resp = account.fetchFile(fileId=fid)
                    url = resp.get('url') if isinstance(resp, dict) else None
                    if url:
                        with open(log_file, 'a', encoding='utf-8') as log:
                            log.write(f"{datetime.now()} - {nm}: {url}\n")
                        video_links.append((nm, url))

                        # Launch IDM with retry mechanism
                        max_retries = 3
                        retry_count = 0
                        success = False

                        while retry_count < max_retries and not success:
                            try:
                                subprocess.Popen([idm_path, "/d", url, "/p", download_dir, "/f", nm], shell=False)
                                tgt = os.path.join(download_dir, nm)
                                # Wait for file to appear with timeout
                                for _ in range(30):
                                    if os.path.isfile(tgt):
                                        success = True
                                        break
                                    logging.debug(f"Waiting for {nm} to appear in {download_dir}...")
                                    time.sleep(2)

                                if not success:
                                    retry_count += 1
                                    delay = 2 ** retry_count  # Exponential backoff: 2s, 4s, 8s
                                    logging.warning(f"IDM download not confirmed for {nm}, retrying in {delay}s (attempt {retry_count}/{max_retries})")
                                    time.sleep(delay)
                            except Exception as e:
                                retry_count += 1
                                logging.error(f"Failed to launch IDM (attempt {retry_count}/{max_retries}): {e}")
                                if retry_count < max_retries:
                                    time.sleep(retry_count * 2)  # Progressive delay

            try:
                account.deleteFolder(folderId=fd['id'])
            except Exception as e:
                logging.error(f"Failed to delete folder {fd['id']}: {e}")
    except Exception as e:
        logging.error(f"Error finding video files: {e}")
    return video_links

# ---- Worker with History Signal ----
class ProcessorWorker(QObject):
    progress_update = pyqtSignal(str, int, str)  # Name, Progress, Status
    record_update = pyqtSignal(object)
    error_occurred = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, cfg, torrent_name=None, info_hash=None):
        super().__init__()
        self.cfg = cfg
        self._stop = False
        self.single = (torrent_name is not None)
        self.redo_name = torrent_name
        self.redo_hash = info_hash
        self.retry_count = 0
        self.max_retries = cfg.get("max_retry_attempts", 3)
        self.stalled_threshold = cfg.get("stalled_threshold", 400)

    def stop(self):
        self._stop = True

    def clear_seedr_problematic_torrent(self, account, torrent_id=None):
        try:
            if torrent_id:
                account.deleteTorrent(torrent_id)
                logging.info(f"Deleted problematic torrent: {torrent_id}")
            else:
                # Find and delete the first active torrent
                contents = account.listContents()
                for t in contents.get('torrents', []):
                    account.deleteTorrent(t['id'])
                    logging.info(f"Deleted torrent: {t['id']}")
                    break
            return True
        except Exception as e:
            logging.error(f"Failed to clear problematic torrent: {e}")
            return False

    def run(self):
        retry_attempt = 0
        while retry_attempt <= self.max_retries:
            if self._stop:
                break

            try:
                login = Login(self.cfg["username"], self.cfg["password"])
                resp = login.authorize()
                if 'access_token' not in resp:
                    self.error_occurred.emit("Login Failed", "Invalid credentials or server error")
                    self.finished.emit()
                    return

                account = Seedr(token=login.token)

                # Try to clear any existing torrents if this is a retry
                if retry_attempt > 0:
                    self.clear_seedr_problematic_torrent(account)

                trackers = fetch_trackers([
                    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all.txt",
                    "https://raw.githubusercontent.com/XIU2/TrackersListCollection/refs/heads/master/all.txt",
                    "https://raw.githubusercontent.com/Tunglies/TrackersList/refs/heads/main/all.txt",
                    "https://raw.githubusercontent.com/FlawlessCasual17/UltimateBTTrackersList/refs/heads/master/ultimate_trackers.txt"
                ])

                torrents = []
                if self.single:
                    class Dummy:
                        def __init__(s, name, info_hash):
                            s.name = name
                            s.info_hash = info_hash
                    torrents = [Dummy(self.redo_name, self.redo_hash)]
                else:
                    tf = self.cfg["torrent_folder"]
                    try:
                        for f in os.listdir(tf):
                            if f.endswith(".torrent"):
                                torrents.append(os.path.join(tf, f))
                    except Exception as e:
                        self.error_occurred.emit("Folder Error", f"Could not list torrent folder: {e}")
                        self.finished.emit()
                        return

                log_file = os.path.join(self.cfg["torrent_folder"], "seedr_video_links.txt")
                try:
                    with open(log_file, 'w', encoding='utf-8') as log:
                        log.write(f"Links - {datetime.now()}\n{'-'*60}\n")
                except Exception as e:
                    self.error_occurred.emit("File Error", f"Could not write log file: {e}")
                    self.finished.emit()
                    return

                for entry in torrents:
                    if self._stop:
                        break

                    if self.single:
                        fname, info_hash = self.redo_name, self.redo_hash
                        title = self.redo_name
                    else:
                        fname = os.path.basename(entry)
                        try:
                            t = Torrent.from_file(entry)
                            info_hash, title = t.info_hash, t.name or fname.replace('.torrent', '')
                            os.remove(entry)
                        except Exception as e:
                            self.error_occurred.emit("Torrent Error", f"Failed to parse or remove {fname}: {e}")
                            continue

                    magnet = create_magnet_link(info_hash, title, trackers)
                    try:
                        result = account.addTorrent(magnet)
                        logging.info(f"addTorrent response: {result}")
                    except Exception as e:
                        self.error_occurred.emit("Seedr Error", f"Failed to add torrent: {e}")
                        continue

                    status = "Failed"
                    if result.get('result') is not True:
                        logging.error(f"addTorrent error: {result}")
                    else:
                        start = time.time()
                        p = 0
                        count = 0
                        try:
                            contents = account.listContents()
                            torrent_id = contents['torrents'][0]['id']
                            pu = contents['torrents'][0]['progress_url']
                        except (KeyError, IndexError) as e:
                            logging.error(f"progress_url fetch error: {e}")
                            pu = ""
                            torrent_id = None

                        while time.time() - start < TIME_LIMIT and p < 101 and not self._stop:
                            previous_progress = p
                            try:
                                p = fetch_torrent_progress(pu) if pu else 0
                            except Exception as e:
                                logging.error(f"progress_url progress error: {e}")
                                p = previous_progress

                            if p == previous_progress:
                                count += 1
                            else:
                                count = 0

                            # Update status in UI
                            status_text = "Active"
                            if count > self.stalled_threshold:
                                status_text = "Stalled"
                                p = 101  # Force exit from loop

                            self.progress_update.emit(title, p, status_text)
                            time.sleep(3)

                        links = find_video_files_and_get_links(
                            account, log_file,
                            self.cfg["idm_path"], self.cfg["download_dir"]
                        )
                        status = "Success" if links else "Failed"

                    rec = {
                        "file_name": fname,
                        "title": title,
                        "hash": info_hash,
                        "date": datetime.now().isoformat(sep=' ', timespec='seconds'),
                        "status": status
                    }
                    self.record_update.emit(rec)

                # If we get here without errors, break the retry loop
                break

            except Exception as e:
                logging.error(f"Processing error: {e}")
                retry_attempt += 1
                if retry_attempt <= self.max_retries:
                    delay = 2 ** retry_attempt
                    logging.info(f"Retrying operation in {delay}s (attempt {retry_attempt}/{self.max_retries})...")
                    time.sleep(delay)
                else:
                    self.error_occurred.emit("Processing Error",
                                          f"Failed after {self.max_retries} attempts: {str(e)}")

        self.finished.emit()
        logging.debug("Processor finished")

# ---- Main GUI Application ----
class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Seedr Manager by RUCHIRA")
        self.resize(850, 600)
        self.settings = load_settings()
        self.history = load_history()
        self.thread, self.worker = None, None
        self.active_progress = {}

        self.stack = QStackedWidget()
        self.home_page()
        self.account_page()
        self.settings_page()
        self.history_page()
        self.create_sidebar()

        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self.update_progress_bars)

        self.statusBar().showMessage("Ready Ver. 0.1.0")

        if self.settings["username"] and self.settings["password"]:
            self.do_login()
            self.refresh_existing()
            self.progress_timer.start(3000)

    def create_sidebar(self):
        side = QWidget()
        v = QVBoxLayout(side)
        for txt, i in [("Home", 0), ("Account", 1), ("Settings", 2), ("History", 3)]:
            b = QPushButton(txt)
            b.clicked.connect(lambda _, x=i: self.stack.setCurrentIndex(x))
            v.addWidget(b)
        v.addStretch()
        c = QWidget()
        h = QHBoxLayout(c)
        h.addWidget(side)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(c)

    # ---- Home ----
    def home_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        h0 = QHBoxLayout()
        self.space_lbl = QLabel("Space: N/A")
        btn_clear = QPushButton("Clear Storage")
        btn_clear.clicked.connect(self.clear_storage)
        h0.addWidget(self.space_lbl)
        h0.addStretch()
        h0.addWidget(btn_clear)
        v.addLayout(h0)
        hc = QHBoxLayout()
        for txt, fn in [("Start", self.start), ("Stop", self.stop),
                       ("Refresh", self.refresh_existing), ("Exit", self.close)]:
            b = QPushButton(txt)
            b.clicked.connect(fn)
            hc.addWidget(b)
        v.addLayout(hc)
        self.tbl = QTableWidget(0, 3)  # Added status column
        self.tbl.setHorizontalHeaderLabels(["Torrent", "Progress", "Status"])
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self.torrent_context_menu)
        v.addWidget(self.tbl)
        v.addWidget(QLabel("Video Files:"))
        self.video_list = QListWidget()
        self.video_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.video_list.customContextMenuRequested.connect(self.video_context_menu)
        self.video_list.itemClicked.connect(self.download_video)
        v.addWidget(self.video_list)
        self.stack.addWidget(w)

    def refresh_existing(self):
        if not hasattr(self, 'account'):
            return
        try:
            ud = self.account.listContents()
            used, tot = ud.get('space_used', 0) / (1024 * 1024), ud.get('space_max', 1) / (1024 * 1024)
            self.space_lbl.setText(f"Space: {used:.1f} MB / {tot:.1f} MB")

            active_rows = []
            completed_rows = []

            self.tbl.setRowCount(0)
            self.active_progress.clear()

            # Process torrents - active ones first
            for t in ud.get('torrents', []):
                name = t.get('torrent_name', '')
                url = t.get('progress_url', '')
                p = fetch_torrent_progress(url) if url else 0
                status = "Active" if p < 101 else "Complete"

                row_data = (name, p, status, url, t.get('id'))
                if status == "Active":
                    active_rows.append(row_data)
                else:
                    completed_rows.append(row_data)

            # Add rows to table, active first
            for name, p, status, url, torrent_id in active_rows + completed_rows:
                row = self.tbl.rowCount()
                self.tbl.insertRow(row)
                self.tbl.setItem(row, 0, QTableWidgetItem(name))
                self.tbl.setItem(row, 2, QTableWidgetItem(status))

                pb = QProgressBar()
                pb.setValue(p)

                # Color-code based on status
                if status == "Active":
                    pb.setStyleSheet("QProgressBar::chunk { background-color: #4080FF; }")
                    # Highlight active row
                    for col in range(3):
                        item = self.tbl.item(row, col)
                        if item:
                            item.setBackground(QBrush(QColor("#E8F0FF")))
                elif status == "Stalled":
                    pb.setStyleSheet("QProgressBar::chunk { background-color: #FF8040; }")
                else:  # Completed
                    pb.setStyleSheet("QProgressBar::chunk { background-color: #40C040; }")

                self.tbl.setCellWidget(row, 1, pb)

                # Track progress URLs of active torrents
                if status == "Active" and url:
                    self.active_progress[name] = (url, torrent_id)

            # List video files
            self.video_list.clear()
            for fd in ud.get('folders', []):
                for f in self.account.listContents(folderId=fd['id']).get('files', []):
                    if f['name'].lower().endswith(('.mp4', '.mkv', '.avi')):
                        self.video_list.addItem(f['name'])

        except Exception as e:
            logging.error(f"Refresh error: {e}")
            show_error(self, "Refresh Error", f"Failed to refresh: {e}")

    def update_progress_bars(self):
        to_remove = []
        for name, (url, torrent_id) in self.active_progress.items():
            p = fetch_torrent_progress(url)
            status = "Active"

            # Check for rows with this name
            for row in range(self.tbl.rowCount()):
                if self.tbl.item(row, 0) and self.tbl.item(row, 0).text() == name:
                    # Update progress
                    self.tbl.cellWidget(row, 1).setValue(p)

                    # Check for stalled downloads
                    prev_status = self.tbl.item(row, 2).text()
                    if prev_status == "Stalled" or p >= 101:
                        status = "Complete" if p >= 101 else "Stalled"
                        self.tbl.item(row, 2).setText(status)

                        # Update progress bar color
                        if status == "Stalled":
                            self.tbl.cellWidget(row, 1).setStyleSheet(
                                "QProgressBar::chunk { background-color: #FF8040; }")
                        elif status == "Complete":
                            self.tbl.cellWidget(row, 1).setStyleSheet(
                                "QProgressBar::chunk { background-color: #40C040; }")
                    break

            if status != "Active":
                to_remove.append(name)

        for name in to_remove:
            del self.active_progress[name]

    def closeEvent(self, event):
        if hasattr(self, 'progress_timer'):
            self.progress_timer.stop()
        super().closeEvent(event)

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.progress_timer.stop()

    def clear_storage(self):
        if not hasattr(self, 'account'):
            return
        ans = QMessageBox.question(
            self, "Confirm Delete",
            "Delete ALL items from Seedr storage? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if ans == QMessageBox.Yes:
            try:
                ud = self.account.listContents()
                for t in ud.get('torrents', []):
                    self.account.deleteTorrent(t['id'])
                for fd in ud.get('folders', []):
                    self.account.deleteFolder(folderId=fd['id'])
                logging.debug("Storage cleared")
                self.refresh_existing()
            except Exception as e:
                logging.error(f"Clear storage error: {e}")
                show_error(self, "Clear Storage Error", f"Failed to clear storage: {e}")

    def torrent_context_menu(self, pos):
        row = self.tbl.rowAt(pos.y())
        if row < 0:
            return

        # Only show delete option for active torrents
        status_item = self.tbl.item(row, 2)
        if not status_item or status_item.text() != "Active":
            return

        torrent_name = self.tbl.item(row, 0).text()

        menu = QMenu(self)
        del_act = menu.addAction("Delete from Seedr")
        if menu.exec_(self.tbl.mapToGlobal(pos)) == del_act:
            self.delete_torrent(torrent_name)

    def delete_torrent(self, name):
        if not hasattr(self, 'account'):
            return

        ans = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete the torrent '{name}' from Seedr? Download will be cancelled.",
            QMessageBox.Yes | QMessageBox.No
        )

        if ans != QMessageBox.Yes:
            return

        try:
            # Find the torrent ID
            torrent_id = None
            if name in self.active_progress:
                url, torrent_id = self.active_progress[name]

            if not torrent_id:
                # Try to find it in the current list
                contents = self.account.listContents()
                for t in contents.get('torrents', []):
                    if t.get('torrent_name') == name:
                        torrent_id = t['id']
                        break

            if torrent_id:
                self.account.deleteTorrent(torrent_id)
                logging.info(f"Deleted torrent '{name}' (ID: {torrent_id})")

                # Add failed entry to history
                rec = {
                    "file_name": name,
                    "title": name,
                    "hash": "",  # May not have hash if deleted mid-download
                    "date": datetime.now().isoformat(sep=' ', timespec='seconds'),
                    "status": "Failed (Deleted)"
                }
                self.history.append(rec)
                save_history(self.history)
                self.reload_history()

                self.refresh_existing()
            else:
                show_warning(self, "Torrent Not Found", f"Could not find torrent '{name}' to delete")
        except Exception as e:
            logging.error(f"Error deleting torrent '{name}': {e}")
            show_error(self, "Delete Error", f"Failed to delete torrent: {e}")

    # ---- Account Page ----
    def account_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Username:"))
        self.user = QLineEdit(self.settings["username"])
        v.addWidget(self.user)
        v.addWidget(QLabel("Password:"))
        self.pw = QLineEdit(self.settings["password"])
        self.pw.setEchoMode(QLineEdit.Password)
        v.addWidget(self.pw)
        b = QPushButton("Login")
        b.clicked.connect(self.do_login)
        v.addWidget(b)
        v.addStretch()
        self.stack.addWidget(w)

    def do_login(self):
        u, p = self.user.text().strip(), self.pw.text().strip()
        if not u or not p:
            show_warning(self, "Login Error", "Please enter both username and password.")
            return
        try:
            lg = Login(u, p)
            r = lg.authorize()
            if r and 'access_token' in r:
                logging.debug("Login success")
                self.account = Seedr(token=lg.token)
                self.settings["username"], self.settings["password"] = u, p
                save_settings(self.settings)
                self.refresh_existing()
                show_info(self, "Success", "Successfully logged in!")
            else:
                show_error(self, "Login Failed", "Invalid credentials or server error.")
        except Exception as e:
            logging.error(f"Login error: {e}")
            show_error(self, "Login Error", f"Failed to login: {e}")

    # ---- Settings Page ----
    def settings_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.settings_edits = {}

        # Standard path settings
        for label, key in [("Download Dir", "download_dir"),
                         ("IDM Path", "idm_path"),
                         ("Torrent Folder", "torrent_folder")]:
            v.addWidget(QLabel(label))
            le = QLineEdit(self.settings.get(key, ""))
            v.addWidget(le)
            btn = QPushButton("Browse")
            btn.clicked.connect(lambda _, e=le, k=key: self.browse(e, k))
            v.addWidget(btn)
            self.settings_edits[key] = le

        # Advanced settings
        v.addWidget(QLabel("Advanced Settings"))

        # Retry attempts
        v.addWidget(QLabel("Max Retry Attempts:"))
        retry_edit = QLineEdit(str(self.settings.get("max_retry_attempts", 3)))
        v.addWidget(retry_edit)
        self.settings_edits["max_retry_attempts"] = retry_edit

        # Stalled threshold
        v.addWidget(QLabel("Stalled Download Threshold (cycles):"))
        stalled_edit = QLineEdit(str(self.settings.get("stalled_threshold", 400)))
        v.addWidget(stalled_edit)
        self.settings_edits["stalled_threshold"] = stalled_edit

        b = QPushButton("Save")
        b.clicked.connect(self.save_settings_ui)
        v.addWidget(b)
        v.addStretch()
        self.stack.addWidget(w)

    def browse(self, lineedit, key):
        if key in ["download_dir", "torrent_folder"]:
            d = QFileDialog.getExistingDirectory(self, "Select " + key)
        else:  # idm_path
            d, _ = QFileDialog.getOpenFileName(self, "Select IDM Executable",
                                           filter="Executable Files (*.exe);;All Files (*)")
        if d:
            lineedit.setText(d)
            self.settings[key] = d

    def save_settings_ui(self):
        try:
            # Process string paths
            for key in ["download_dir", "idm_path", "torrent_folder"]:
                self.settings[key] = self.settings_edits[key].text()

            # Process numeric values with validation
            for key in ["max_retry_attempts", "stalled_threshold"]:
                try:
                    value = int(self.settings_edits[key].text())
                    if key == "max_retry_attempts" and value < 0:
                        value = 3  # Default if invalid
                    elif key == "stalled_threshold" and value < 10:
                        value = 400  # Default if invalid
                    self.settings[key] = value
                except ValueError:
                    # Keep existing value if invalid
                    pass

            save_settings(self.settings)
            show_info(self, "Settings", "Settings saved successfully.")
        except Exception as e:
            logging.error(f"Settings save error: {e}")
            show_error(self, "Settings Error", f"Error saving settings: {e}")

    # ---- History Page ----
    def history_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.hist_tbl = QTableWidget(0, 5)
        self.hist_tbl.setHorizontalHeaderLabels(["File", "Title", "Hash", "Date", "Status"])
        self.hist_tbl.horizontalHeader().setStretchLastSection(True)
        self.hist_tbl.setContextMenuPolicy(Qt.CustomContextMenu)
        self.hist_tbl.customContextMenuRequested.connect(self.history_context_menu)
        v.addWidget(self.hist_tbl)
        self.reload_history()
        self.stack.addWidget(w)

    def reload_history(self):
        self.hist_tbl.setRowCount(0)
        for rec in self.history:
            r = self.hist_tbl.rowCount()
            self.hist_tbl.insertRow(r)
            for c, key in enumerate(["file_name", "title", "hash", "date", "status"]):
                item = QTableWidgetItem(rec.get(key, ""))

                # Color-code status
                if c == 4:  # Status column
                    status = rec.get("status", "")
                    if status == "Success":
                        item.setBackground(QBrush(QColor("#E0FFE0")))  # Light green
                    elif status == "Failed" or status.startswith("Failed"):
                        item.setBackground(QBrush(QColor("#FFE0E0")))  # Light red

                self.hist_tbl.setItem(r, c, item)

    def history_context_menu(self, pos):
        row = self.hist_tbl.rowAt(pos.y())
        if row < 0:
            return
        menu = QMenu(self)
        act = menu.addAction("Download Again")
        if menu.exec_(self.hist_tbl.mapToGlobal(pos)) == act:
            self.redownload(self.history[row])

    def redownload(self, rec):
        if self.thread and self.thread.isRunning():
            show_warning(self, "Already Running", "Please wait for the current process to complete.")
            return
        self.thread = QThread()
        self.worker = ProcessorWorker(self.settings, rec["file_name"], rec["hash"])
        self.worker.moveToThread(self.thread)
        self.worker.progress_update.connect(self.update_progress)
        self.worker.record_update.connect(self.add_history_record)
        self.worker.error_occurred.connect(lambda title, msg: show_error(self, title, msg))
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

    # ---- Controls ----
    def start(self):
        if self.thread and self.thread.isRunning():
            show_info(self, "Processing", "Already processing torrents")
            return
        for key in ["download_dir", "idm_path", "torrent_folder"]:
            if not self.settings.get(key):
                show_warning(self, "Configuration", f"Please configure {key} in Settings")
                self.stack.setCurrentIndex(2)
                return
        if not hasattr(self, 'account'):
            show_warning(self, "Login Required", "Please login first")
            self.stack.setCurrentIndex(1)
            return
        self.thread = QThread()
        self.worker = ProcessorWorker(self.settings)
        self.worker.moveToThread(self.thread)
        self.worker.progress_update.connect(self.update_progress)
        self.worker.record_update.connect(self.add_history_record)
        self.worker.error_occurred.connect(lambda title, msg: show_error(self, title, msg))
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

    def update_progress(self, name, pct, status):
        # First check if this torrent is already in the table
        for row in range(self.tbl.rowCount()):
            if self.tbl.item(row, 0) and self.tbl.item(row, 0).text() == name:
                # Update existing row
                self.tbl.cellWidget(row, 1).setValue(pct)
                self.tbl.setItem(row, 2, QTableWidgetItem(status))

                # Update progress bar style based on status
                if status == "Active":
                    self.tbl.cellWidget(row, 1).setStyleSheet(
                        "QProgressBar::chunk { background-color: #4080FF; }")
                elif status == "Stalled":
                    self.tbl.cellWidget(row, 1).setStyleSheet(
                        "QProgressBar::chunk { background-color: #FF8040; }")
                else:  # Completed
                    self.tbl.cellWidget(row, 1).setStyleSheet(
                        "QProgressBar::chunk { background-color: #40C040; }")

                # Move active torrents to top
                if status == "Active" and row > 0:
                    self.tbl.insertRow(0)
                    for col in range(3):
                        if col == 1:  # Progress bar column
                            pb = self.tbl.cellWidget(row + 1, col)
                            self.tbl.setCellWidget(0, col, pb)
                        else:
                            item = self.tbl.takeItem(row + 1, col)
                            self.tbl.setItem(0, col, item)
                    self.tbl.removeRow(row + 1)

                return

        # If not found, create a new row at the top if active, bottom if not
        insert_row = 0 if status == "Active" else self.tbl.rowCount()
        self.tbl.insertRow(insert_row)
        self.tbl.setItem(insert_row, 0, QTableWidgetItem(name))
        self.tbl.setItem(insert_row, 2, QTableWidgetItem(status))

        pb = QProgressBar()
        pb.setRange(0, 101)
        pb.setValue(pct)

        # Set color based on status
        if status == "Active":
            pb.setStyleSheet("QProgressBar::chunk { background-color: #4080FF; }")
            # Highlight active row
            for col in range(3):
                item = self.tbl.item(insert_row, col)
                if item:
                    item.setBackground(QBrush(QColor("#E8F0FF")))
        elif status == "Stalled":
            pb.setStyleSheet("QProgressBar::chunk { background-color: #FF8040; }")
        else:  # Completed or other
            pb.setStyleSheet("QProgressBar::chunk { background-color: #40C040; }")

        self.tbl.setCellWidget(insert_row, 1, pb)

    def video_context_menu(self, pos):
        item = self.video_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        dl_act = menu.addAction("Download with IDM")
        del_act = menu.addAction("Delete from Seedr")
        action = menu.exec_(self.video_list.mapToGlobal(pos))

        if action == dl_act:
            self.download_video(item)
        elif action == del_act:
            self.delete_video(item)

    def delete_video(self, item):
        name = item.text()
        if not hasattr(self, 'account'):
            logging.error("Not logged in – cannot delete video")
            return
        try:
            for fd in self.account.listContents().get('folders', []):
                for f in self.account.listContents(folderId=fd['id']).get('files', []):
                    if f['name'] == name:
                        fid = f.get('folder_file_id') or f.get('id')
                        self.account.deleteFile(fileId=fid)
                        logging.debug(f"Deleted video '{name}' (id={fid})")
                        self.refresh_existing()
                        return
            logging.error(f"Video '{name}' not found for deletion")
        except Exception as e:
            logging.error(f"Error deleting '{name}': {e}")
            show_error(self, "Delete Error", f"Failed to delete video: {e}")

    def download_video(self, item):
        name = item.text()
        if not hasattr(self, 'account'):
            logging.error("Not logged in – cannot download")
            return
        try:
            for fd in self.account.listContents().get('folders', []):
                for f in self.account.listContents(folderId=fd['id']).get('files', []):
                    if f['name'] == name:
                        fid = f.get('folder_file_id') or f.get('id')
                        resp = self.account.fetchFile(fileId=fid)
                        url = resp.get('url')
                        if not url:
                            raise Exception("No URL returned")
                        logging.debug(f"Downloading {name} via IDM: {url}")
                        try:
                            # Enhanced IDM command with file name parameter
                            subprocess.Popen([
                                self.settings['idm_path'],
                                "/d", url,
                                "/p", self.settings['download_dir'],
                                "/f", name
                            ], shell=False)
                            show_info(self, "Download Started",
                                    f"Download started for '{name}'\nCheck IDM for progress.")
                        except Exception as e:
                            show_error(self, "IDM Error", f"Failed to launch IDM: {e}")
                        return
            logging.error(f"Video '{name}' not found for download")
        except Exception as e:
            logging.error(f"Download error for '{name}': {e}")
            show_error(self, "Download Error", f"Failed to download video: {e}")

    def add_history_record(self, rec):
        self.history.append(rec)
        save_history(self.history)
        self.reload_history()

# ---- Launch ----
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = App()
    win.show()
    sys.exit(app.exec_())