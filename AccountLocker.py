import sys
import os
import json
import ctypes
import threading
from datetime import datetime, timedelta, timezone, time as dtime
import requests
import logging
from PyQt5 import QtWidgets, QtGui, QtCore

import shutil
import winreg  # only works on Windows
import filecmp

# Logging configuration
log_path = os.path.join(os.getenv('APPDATA', os.getcwd()), 'locker.log')
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s: %(message)s',
    filename=log_path,
    filemode='a'
)
logging.info('Starting Account Locker')

# Paths and defaults
CONFIG_PATH = os.path.join(os.getenv('APPDATA', os.getcwd()), 'account_locker_config.json')
DEFAULT_CONFIG = {
    'password_hash': '',
    'lock_time': '18:00',
    'unlock_time': '06:00',      # ← new default unlock time
    'days': list(range(7)),
    'enabled': True
}

import hashlib 


        
def hash_password(pw):
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()

class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, 'r') as f:
                self.data = json.load(f)
            # inject default unlock_time if missing
            if 'unlock_time' not in self.data:
                self.data['unlock_time'] = DEFAULT_CONFIG['unlock_time']
            logging.debug(f'Loaded config: {self.data}')
        else:
            self.data = DEFAULT_CONFIG.copy()
            logging.debug('Using default config')

    def save(self):
        with open(self.path, 'w') as f:
            json.dump(self.data, f, indent=4)
        logging.debug(f'Saved config: {self.data}')

class SetupDialog(QtWidgets.QDialog):
    def __init__(self, config, first_run=False):
        super().__init__()
        self.config = config
        self.first_run = first_run
        self.local_tz = datetime.now().astimezone().tzinfo
        self.offset = timedelta(0)
        self.latest_google_time = None
        self.setWindowTitle('Account Locker Setup')
        self.init_ui()
        # Initial fetch and start timers
        self.update_google_time()
        self.google_timer = QtCore.QTimer(self)
        self.google_timer.timeout.connect(self.update_google_time)
        self.google_timer.start(60 * 1000)
        self.countdown_timer = QtCore.QTimer(self)
        self.countdown_timer.timeout.connect(self.update_countdown)
        self.countdown_timer.start(1000)

        self.time_edit.timeChanged.connect(self.update_countdown)
        self.unlock_edit.timeChanged.connect(self.update_countdown)
        self.enable_cb.stateChanged.connect(self.update_countdown)
        for cb in self.day_checks:
            cb.stateChanged.connect(self.update_countdown)

    def init_ui(self):
        layout = QtWidgets.QFormLayout(self)
        # Password fields
        self.pw1 = QtWidgets.QLineEdit(); self.pw1.setEchoMode(QtWidgets.QLineEdit.Password)
        self.pw2 = QtWidgets.QLineEdit(); self.pw2.setEchoMode(QtWidgets.QLineEdit.Password)
        if not self.first_run:
            self.pw1.setPlaceholderText('Leave blank to keep current')
        layout.addRow('Password:', self.pw1)
        layout.addRow('Confirm:', self.pw2)
        # Lock time picker
        t = QtCore.QTime.fromString(self.config.data['lock_time'], 'HH:mm')
        self.time_edit = QtWidgets.QTimeEdit(t); self.time_edit.setDisplayFormat('HH:mm')
        layout.addRow('Lock Time:', self.time_edit)
        # Unlock time picker
        u = QtCore.QTime.fromString(self.config.data.get('unlock_time', '06:00'), 'HH:mm')
        self.unlock_edit = QtWidgets.QTimeEdit(u); self.unlock_edit.setDisplayFormat('HH:mm')
        layout.addRow('Unlock Time:', self.unlock_edit)
        # Days checkboxes
        days_widget = QtWidgets.QWidget(); days_layout = QtWidgets.QHBoxLayout(days_widget)
        self.day_checks = []
        names = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
        for i, name in enumerate(names):
            cb = QtWidgets.QCheckBox(name)
            cb.setChecked(i in self.config.data['days'])
            days_layout.addWidget(cb)
            self.day_checks.append(cb)
        layout.addRow('Days:', days_widget)
        # Enable checkbox
        self.enable_cb = QtWidgets.QCheckBox('Enable Schedule')
        self.enable_cb.setChecked(self.config.data['enabled'])
        layout.addRow('Enabled:', self.enable_cb)
        # Google time display
        self.google_time_label = QtWidgets.QLabel('Fetching...')
        layout.addRow('Google Time:', self.google_time_label)
        # Countdown display
        self.time_until_label = QtWidgets.QLabel('Calculating...')
        layout.addRow('Time Until Lock:', self.time_until_label)
        
        # Toggle Info visibility
        self.show_info_cb = QtWidgets.QCheckBox('Show Info')
        self.show_info_cb.setChecked(True)
        self.show_info_cb.stateChanged.connect(self.toggle_info_visibility)
        layout.addRow('', self.show_info_cb)

        info_text = (
            "<b>Info:</b><br>"
            "This app automatically copies itself to your <i>Startup folder</i> so it runs with Windows.<br><br>"
            "Configuration and log files are saved in your AppData Roaming folder:<br>"
            "<ul>"
            "<li><code>account_locker_config.json</code>: Stores schedule settings and password hash</li>"
            "<li><code>locker.log</code>: Contains activity logs (lock attempts, errors, etc.)</li>"
            "</ul>"
            f"Folder path:<br><code>{os.getenv('APPDATA')}</code>"
        )

        self.info_label = QtWidgets.QLabel(info_text)
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(QtCore.Qt.RichText)
        layout.addRow('Note:', self.info_label)

        # Folder buttons (wrapped in a container)
        self.folder_btns_widget = QtWidgets.QWidget()
        folder_btns = QtWidgets.QHBoxLayout(self.folder_btns_widget)
        folder_btns.setContentsMargins(0, 0, 0, 0)

        self.open_startup_btn = QtWidgets.QPushButton('Open Startup Folder')
        self.open_startup_btn.clicked.connect(self.open_startup_folder)

        self.open_appdata_btn = QtWidgets.QPushButton('Open AppData Folder')
        self.open_appdata_btn.clicked.connect(self.open_appdata_folder)

        folder_btns.addWidget(self.open_startup_btn)
        folder_btns.addWidget(self.open_appdata_btn)
        layout.addRow('Folders:', self.folder_btns_widget)


                

        # Buttons
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)
        
        
    def open_startup_folder(self):
        startup_dir = os.path.join(os.getenv('APPDATA'), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(startup_dir))

    def open_appdata_folder(self):
        appdata_dir = os.getenv('APPDATA')
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(appdata_dir))

            
    def toggle_info_visibility(self, state):
        visible = state == QtCore.Qt.Checked
        self.info_label.setVisible(visible)
        self.folder_btns_widget.setVisible(visible)



    def update_google_time(self):
        try:
            r = requests.head('https://www.google.com', timeout=5)
            date_str = r.headers.get('Date')
            if date_str:
                dt_utc = datetime.strptime(
                    date_str, '%a, %d %b %Y %H:%M:%S GMT'
                ).replace(tzinfo=timezone.utc)
                dt_local = dt_utc.astimezone(self.local_tz)
                sys_local = datetime.now(self.local_tz)
                self.offset = dt_local - sys_local
                self.latest_google_time = dt_local
                text = dt_local.strftime('%Y-%m-%d %H:%M:%S')
            else:
                text = 'N/A'
        except Exception as e:
            logging.error(f"Google time fetch error: {e}")
            text = 'Error'
        self.google_time_label.setText(text)
        self.update_countdown()

    def update_countdown(self):
        now = datetime.now(self.local_tz) + self.offset

        lock_str   = self.time_edit.time().toString('HH:mm')
        unlock_str = self.unlock_edit.time().toString('HH:mm')
        lock_h, lock_m     = map(int, lock_str.split(':'))
        unlock_h, unlock_m = map(int, unlock_str.split(':'))
        days = [i for i, cb in enumerate(self.day_checks) if cb.isChecked()]
        enabled = self.enable_cb.isChecked()

        if not enabled or now.weekday() not in days:
            self.time_until_label.setText('N/A')
            return

        now_t = now.time()
        lock_t   = dtime(lock_h, lock_m)
        unlock_t = dtime(unlock_h, unlock_m)

        # Determine next event: next lock if outside window, next unlock if inside
        if lock_t < unlock_t:
            in_window = (lock_t <= now_t < unlock_t)
        else:
            in_window = (now_t >= lock_t) or (now_t < unlock_t)

        # compute next transition datetime
        next_dt = None
        for i in range(7):
            d = (now.weekday() + i) % 7
            if d not in days:
                continue
            base = (now + timedelta(days=i)).replace(
                hour=lock_h if not in_window else unlock_h,
                minute=lock_m if not in_window else unlock_m,
                second=0, microsecond=0
            )
            # if window spans midnight and we're computing unlock for next day
            if in_window and lock_t >= unlock_t and d == now.weekday():
                # if we've passed today's unlock time, schedule next day's unlock
                if now_t >= unlock_t:
                    base += timedelta(days=1)
            if base > now:
                next_dt = base
                break

        if not next_dt:
            self.time_until_label.setText('N/A')
            return

        delta = next_dt - now
        d, rem = delta.days, delta.seconds
        h, rem = divmod(rem, 3600)
        m, s   = divmod(rem, 60)

        disp = f"{d}d {h}h {m}m" if d > 0 else f"{h}h {m}m {s}s"
        self.time_until_label.setText(disp)

    def accept(self):
        pw1, pw2 = self.pw1.text(), self.pw2.text()
        if self.first_run or pw1:
            if not pw1 or pw1 != pw2:
                QtWidgets.QMessageBox.warning(self, 'Error', 'Passwords do not match or are empty')
                return
            self.config.data['password_hash'] = hash_password(pw1)
        self.config.data['enabled']     = self.enable_cb.isChecked()
        self.config.data['lock_time']   = self.time_edit.time().toString('HH:mm')
        self.config.data['unlock_time'] = self.unlock_edit.time().toString('HH:mm')
        self.config.data['days']        = [i for i, cb in enumerate(self.day_checks) if cb.isChecked()]
        self.config.save()
        super().accept()
        
        
def add_to_startup():
    try:
        # Get current executable or script path
        current_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
        
        # Define destination path in startup folder
        startup_dir = os.path.join(os.getenv('APPDATA'), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
        dest_name = "AccountLocker.exe" if current_path.lower().endswith(".exe") else "account_locker_copy.py"
        dest_path = os.path.join(startup_dir, dest_name)

        if not os.path.exists(dest_path) or not filecmp.cmp(current_path, dest_path, shallow=False):
            shutil.copy2(current_path, dest_path)

            logging.info(f'Copied self to startup: {dest_path}')
        else:
            logging.debug('Startup copy already exists')
    except Exception as e:
        logging.error(f"Failed to copy to startup: {e}")

        
class LockerApp(QtWidgets.QSystemTrayIcon):
    def __init__(self, icon, parent=None):
        super().__init__(icon, parent)
        self.app = parent
        self.config = Config()
        add_to_startup()

        self.failed_attempts = 0
        if not os.path.exists(self.config.path):
            dlg = SetupDialog(self.config, first_run=True)
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                sys.exit()
        self.local_tz = datetime.now().astimezone().tzinfo
        logging.debug(f'Local timezone: {self.local_tz}')
        # Tray menu
        self.menu = QtWidgets.QMenu()
        self.menu.addAction('Force Lock Now', self.lock_workstation)
        self.edit = self.menu.addAction('Edit Schedule', self.open_settings)
        self.toggle = self.menu.addAction(
            'Disable Schedule' if self.config.data['enabled'] else 'Enable Schedule',
            self.toggle_schedule
        )
        self.menu.addAction('Exit', self.exit_app)
        self.setContextMenu(self.menu)
        self.setToolTip('Account Locker')
        self.activated.connect(
            lambda r: self.open_settings() if r == QtWidgets.QSystemTrayIcon.Trigger else None
        )
        self.show()
        # Timers
        self.offset = timedelta(0)
        self.sync_time()
        self.sync_timer = QtCore.QTimer(self)
        self.sync_timer.timeout.connect(self.sync_time)
        self.sync_timer.start(3600 * 1000)
        self.check_timer = QtCore.QTimer(self)
        self.check_timer.timeout.connect(self.check_lock)
        self.check_timer.start(60 * 1000)

            
    def sync_time(self):
        def work():
            try:
                r = requests.head('https://www.google.com', timeout=5)
                date_str = r.headers.get('Date')
                if date_str:
                    dt_utc = datetime.strptime(
                        date_str, '%a, %d %b %Y %H:%M:%S GMT'
                    ).replace(tzinfo=timezone.utc)
                    dt_local = dt_utc.astimezone(self.local_tz)
                    sys_local = datetime.now(self.local_tz)
                    self.offset = dt_local - sys_local
                    logging.debug(f'Time offset (google_local - sys_local): {self.offset}')
            except Exception as e:
                logging.error(f'Time sync failed: {e}')
        threading.Thread(target=work, daemon=True).start()

    def current_time(self):
        return datetime.now(self.local_tz) + self.offset

    def check_lock(self):
        now = self.current_time()
        cfg = self.config.data

        if not cfg['enabled'] or now.weekday() not in cfg['days']:
            return

        # parse lock/unlock
        lock_h, lock_m       = map(int, cfg['lock_time'].split(':'))
        unlock_h, unlock_m   = map(int, cfg['unlock_time'].split(':'))
        now_t    = now.time()
        lock_t   = dtime(lock_h, lock_m)
        unlock_t = dtime(unlock_h, unlock_m)

        # determine if we're in the lock‑window
        if lock_t < unlock_t:
            in_window = (lock_t <= now_t < unlock_t)
        else:
            in_window = (now_t >= lock_t) or (now_t < unlock_t)

        if in_window:
            logging.info('Scheduled lock triggered')
            self.lock_workstation()

    def lock_workstation(self):
        logging.info('Locking workstation')
        try:
            ctypes.windll.user32.LockWorkStation()
        except Exception as e:
            logging.error(f'Lock failed: {e}')

    def verify(self):
        pw, ok = QtWidgets.QInputDialog.getText(
            None,
            'Password',
            'Enter password:',
            QtWidgets.QLineEdit.Password
        )
        if not ok:
            return False

        # correct password?
        if hash_password(pw) == self.config.data['password_hash']:
            # reset counter & tooltip
            self.failed_attempts = 0
            self.setToolTip('Account Locker')
            return True

        # on failure
        self.failed_attempts += 1
        logging.warning(f'Password attempt failed ({self.failed_attempts} total)')
        QtWidgets.QMessageBox.warning(
            None,
            'Error',
            f'Incorrect password.\nFailed attempts: {self.failed_attempts}'
        )
        # reflect count in tray tooltip
        self.setToolTip(f'Account Locker (Failed attempts: {self.failed_attempts})')
        return False


    def open_settings(self):
        if self.verify():
            dlg = SetupDialog(self.config)
            dlg.exec_()
            self.toggle.setText(
                'Disable Schedule' if self.config.data['enabled'] else 'Enable Schedule'
            )

    def toggle_schedule(self):
        if self.verify():
            self.config.data['enabled'] = not self.config.data['enabled']
            self.config.save()
            self.toggle.setText(
                'Disable Schedule' if self.config.data['enabled'] else 'Enable Schedule'
            )

    def exit_app(self):
        if self.verify():
            QtWidgets.QApplication.quit()

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')
    icon = (
        QtGui.QIcon(icon_path)
        if os.path.exists(icon_path)
        else app.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
    )
    LockerApp(icon, app)
    sys.exit(app.exec_())
