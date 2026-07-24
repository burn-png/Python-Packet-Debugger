import sys
import os
import ctypes
import threading
import traceback
import time

from PyQt6 import QtWidgets, QtGui, QtCore
import pydivert
import keyboard
import psutil

log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_log.txt")

def log(msg):
    with open(log_path, "a") as f:
        f.write(str(msg) + "\n")

DEFAULT_FILTER = "false"
DEFAULT_MODE_NAME = "NONE"

# ---------- Self-elevate ----------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def run_as_admin():
    if is_admin():
        return
    script = os.path.abspath(sys.argv[0])
    params = f'"{script}"'
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit(0)

run_as_admin()

# ---------- Shared state ----------
blocking = False
current_filter = DEFAULT_FILTER
current_mode_name = DEFAULT_MODE_NAME
direction_mode = "outbound"
app_filter_name = ""
app_ports = set()
lock = threading.Lock()
restart_event = threading.Event()

current_handle = None
handle_lock = threading.Lock()

blocked_count = 0
passed_count = 0
in_flight_delays = 0

delay_enabled = False
delay_ms = 100

block_hotkey = "q"
delay_hotkey = "`"

# ---------- App port tracker ----------
def app_port_tracker():
    global app_ports
    while True:
        name = app_filter_name
        if name:
            ports = set()
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if proc.info['name'] and proc.info['name'].lower() == name.lower():
                        for c in proc.connections(kind='inet'):
                            if c.laddr:
                                ports.add(c.laddr.port)
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
            app_ports = ports
        else:
            app_ports = set()
        time.sleep(1)

# ---------- Delayed send worker ----------
def send_after_delay(w, packet, delay_seconds):
    global in_flight_delays
    time.sleep(delay_seconds)
    try:
        w.send(packet)
    except Exception as e:
        log(f"Delayed send failed (handle likely closed): {e}")
    finally:
        with lock:
            in_flight_delays -= 1

# ---------- TX OFF overlay ----------
class Overlay(QtWidgets.QWidget):
    toggle_signal = QtCore.pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self.mode_name = current_mode_name
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint |
            QtCore.Qt.WindowType.WindowStaysOnTopHint |
            QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        # Occupies top strip: y 20 to 110
        self.setGeometry(0, 20, screen.width(), 90)
        self.visible_state = False
        self.toggle_signal.connect(self.set_state)

    def set_state(self, on, mode_name):
        self.visible_state = on
        self.mode_name = mode_name
        self.update()
        self.show() if on else self.hide()

    def paintEvent(self, event):
        if not self.visible_state:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        f1 = QtGui.QFont("Consolas", 28, QtGui.QFont.Weight.Bold)
        painter.setFont(f1)
        painter.setPen(QtGui.QColor("red"))
        painter.drawText(QtCore.QRect(rect.x(), rect.y(), rect.width(), 50),
                          QtCore.Qt.AlignmentFlag.AlignCenter, "TX OFF")
        f2 = QtGui.QFont("Consolas", 14, QtGui.QFont.Weight.Bold)
        painter.setFont(f2)
        painter.setPen(QtGui.QColor("#ff8888"))
        painter.drawText(QtCore.QRect(rect.x(), rect.y() + 48, rect.width(), 30),
                          QtCore.Qt.AlignmentFlag.AlignCenter, f"MODE: {self.mode_name}")


# ---------- DELAY overlay (separate window, sits below TX OFF) ----------
class DelayOverlay(QtWidgets.QWidget):
    toggle_signal = QtCore.pyqtSignal(bool, int)

    def __init__(self):
        super().__init__()
        self.ms_value = delay_ms
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint |
            QtCore.Qt.WindowType.WindowStaysOnTopHint |
            QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        # Positioned below the TX OFF overlay's strip (which ends around y=110)
        # so the two never occupy the same pixels, even if both are visible at once.
        self.setGeometry(0, 120, screen.width(), 70)
        self.visible_state = False
        self.toggle_signal.connect(self.set_state)

    def set_state(self, on, ms_value):
        self.visible_state = on
        self.ms_value = ms_value
        self.update()
        self.show() if on else self.hide()

    def update_ms(self, ms_value):
        self.ms_value = ms_value
        if self.visible_state:
            self.update()

    def paintEvent(self, event):
        if not self.visible_state:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        font = QtGui.QFont("Consolas", 22, QtGui.QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QtGui.QColor("#ffaa00"))
        painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, f"DELAY {self.ms_value} ms")


# ---------- Debug panel ----------
class DebugPanel(QtWidgets.QWidget):
    def __init__(self, overlay, delay_overlay):
        super().__init__()
        self.overlay = overlay
        self.delay_overlay = delay_overlay
        self.setWindowTitle("Packet Filter Debug")
        self.setWindowFlags(QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.resize(300, 720)

        self.awaiting_rebind_for = None

        layout = QtWidgets.QVBoxLayout()

        # ---- Hotkeys section ----
        layout.addWidget(QtWidgets.QLabel("<b>Hotkeys</b>"))

        block_row = QtWidgets.QHBoxLayout()
        block_row.addWidget(QtWidgets.QLabel("Block toggle:"))
        self.block_key_label = QtWidgets.QLabel(block_hotkey.upper())
        self.block_key_label.setStyleSheet("font-weight: bold; color: #cc0000;")
        block_row.addWidget(self.block_key_label)
        self.block_rebind_btn = QtWidgets.QPushButton("Edit")
        self.block_rebind_btn.clicked.connect(lambda: self.start_rebind("block"))
        block_row.addWidget(self.block_rebind_btn)
        layout.addLayout(block_row)

        delay_row = QtWidgets.QHBoxLayout()
        delay_row.addWidget(QtWidgets.QLabel("Delay toggle:"))
        self.delay_key_label = QtWidgets.QLabel(delay_hotkey.upper())
        self.delay_key_label.setStyleSheet("font-weight: bold; color: #cc7700;")
        delay_row.addWidget(self.delay_key_label)
        self.delay_rebind_btn = QtWidgets.QPushButton("Edit")
        self.delay_rebind_btn.clicked.connect(lambda: self.start_rebind("delay"))
        delay_row.addWidget(self.delay_rebind_btn)
        layout.addLayout(delay_row)

        self.rebind_status = QtWidgets.QLabel("")
        self.rebind_status.setStyleSheet("color: #0077cc;")
        layout.addWidget(self.rebind_status)

        # ---- Direction ----
        layout.addWidget(QtWidgets.QLabel("<b>Direction</b>"))
        self.dir_group = QtWidgets.QButtonGroup(self)
        self.rb_out = QtWidgets.QRadioButton("Outbound only")
        self.rb_in = QtWidgets.QRadioButton("Inbound only")
        self.rb_both = QtWidgets.QRadioButton("Both")
        self.rb_out.setChecked(True)
        for rb in (self.rb_out, self.rb_in, self.rb_both):
            self.dir_group.addButton(rb)
            layout.addWidget(rb)

        layout.addWidget(QtWidgets.QLabel("<b>Protocols</b>"))
        self.cb_tcp = QtWidgets.QCheckBox("TCP")
        self.cb_udp = QtWidgets.QCheckBox("UDP")
        self.cb_icmp = QtWidgets.QCheckBox("ICMP")
        for cb in (self.cb_tcp, self.cb_udp, self.cb_icmp):
            layout.addWidget(cb)

        layout.addWidget(QtWidgets.QLabel("<b>Common ports (TCP)</b>"))
        self.cb_http = QtWidgets.QCheckBox("HTTP (80)")
        self.cb_https = QtWidgets.QCheckBox("HTTPS (443)")
        for cb in (self.cb_http, self.cb_https):
            layout.addWidget(cb)

        self.cb_dns = QtWidgets.QCheckBox("DNS (UDP 53)")
        layout.addWidget(self.cb_dns)

        layout.addWidget(QtWidgets.QLabel("<b>Custom port (optional)</b>"))
        self.port_input = QtWidgets.QLineEdit()
        self.port_input.setPlaceholderText("e.g. 8080")
        layout.addWidget(self.port_input)

        self.select_all_cb = QtWidgets.QCheckBox("ALL TRAFFIC (overrides protocols above)")
        layout.addWidget(self.select_all_cb)

        layout.addWidget(QtWidgets.QLabel("<b>Limit to app (optional)</b>"))
        self.app_input = QtWidgets.QLineEdit()
        self.app_input.setPlaceholderText("e.g. chrome.exe")
        layout.addWidget(self.app_input)

        self.apply_btn = QtWidgets.QPushButton("Apply Filter")
        self.apply_btn.clicked.connect(self.apply_filter)
        layout.addWidget(self.apply_btn)

        self.status_label = QtWidgets.QLabel(f"Active: {DEFAULT_MODE_NAME}")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.counter_label = QtWidgets.QLabel("Blocked: 0   Passed: 0")
        layout.addWidget(self.counter_label)

        # ---- Delay section ----
        layout.addWidget(QtWidgets.QLabel("<b>Delay Traffic (hold packets before sending)</b>"))

        d_row = QtWidgets.QHBoxLayout()
        d_row.addWidget(QtWidgets.QLabel("Delay (ms):"))
        self.delay_input = QtWidgets.QSpinBox()
        self.delay_input.setRange(0, 30000)
        self.delay_input.setSingleStep(50)
        self.delay_input.setValue(100)
        self.delay_input.valueChanged.connect(self.update_delay_ms)
        d_row.addWidget(self.delay_input)
        layout.addLayout(d_row)

        self.delay_status_label = QtWidgets.QLabel("Delay: OFF")
        layout.addWidget(self.delay_status_label)

        self.inflight_label = QtWidgets.QLabel("In-flight delayed packets: 0")
        layout.addWidget(self.inflight_label)

        layout.addStretch()
        self.setLayout(layout)

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_counters)
        self.timer.start(300)

    # ---- Rebind flow ----
    def start_rebind(self, which):
        self.awaiting_rebind_for = which
        self.rebind_status.setText(f"Press any key to bind '{which}' toggle...")
        threading.Thread(target=self._capture_key, args=(which,), daemon=True).start()

    def _capture_key(self, which):
        try:
            event = keyboard.read_event(suppress=False)
            while event.event_type != "down":
                event = keyboard.read_event(suppress=False)
            new_key = event.name
        except Exception:
            log("Key capture failed:")
            log(traceback.format_exc())
            return

        QtCore.QMetaObject.invokeMethod(
            self, "_finish_rebind", QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, which), QtCore.Q_ARG(str, new_key)
        )

    @QtCore.pyqtSlot(str, str)
    def _finish_rebind(self, which, new_key):
        global block_hotkey, delay_hotkey
        if which == "block":
            block_hotkey = new_key
            self.block_key_label.setText(new_key.upper())
        else:
            delay_hotkey = new_key
            self.delay_key_label.setText(new_key.upper())

        rebind_hotkeys()
        self.rebind_status.setText(f"'{which}' toggle bound to: {new_key.upper()}")
        self.awaiting_rebind_for = None
        log(f"Hotkey rebind: {which} -> {new_key}")

    def update_delay_ms(self, value):
        global delay_ms
        with lock:
            delay_ms = value
        log(f"Packet delay set to {value}ms")
        self.delay_overlay.update_ms(value)

    def update_counters(self):
        self.counter_label.setText(f"Blocked: {blocked_count}   Passed: {passed_count}")
        self.inflight_label.setText(f"In-flight delayed packets: {in_flight_delays}")
        with lock:
            enabled = delay_enabled
        if enabled:
            self.delay_status_label.setText(f"Delay: ON ({delay_ms}ms)")
            self.delay_status_label.setStyleSheet("color: orange")
        else:
            self.delay_status_label.setText("Delay: OFF")
            self.delay_status_label.setStyleSheet("")

    def apply_filter(self):
        global current_filter, current_mode_name, direction_mode, app_filter_name

        if self.select_all_cb.isChecked():
            new_filter, new_name = "true", "ALL"
        else:
            conditions, labels = [], []
            if self.cb_tcp.isChecked(): conditions.append("tcp"); labels.append("TCP")
            if self.cb_udp.isChecked(): conditions.append("udp"); labels.append("UDP")
            if self.cb_icmp.isChecked(): conditions.append("icmp"); labels.append("ICMP")
            if self.cb_http.isChecked(): conditions.append("tcp.DstPort == 80"); labels.append("HTTP")
            if self.cb_https.isChecked(): conditions.append("tcp.DstPort == 443"); labels.append("HTTPS")
            if self.cb_dns.isChecked(): conditions.append("udp.DstPort == 53"); labels.append("DNS")
            port_text = self.port_input.text().strip()
            if port_text.isdigit():
                conditions.append(f"tcp.DstPort == {port_text} or udp.DstPort == {port_text}")
                labels.append(f"PORT:{port_text}")

            if not conditions:
                new_filter, new_name = "false", "NONE"
            else:
                new_filter = " or ".join(f"({c})" for c in conditions)
                new_name = "+".join(labels)

        with lock:
            current_filter = new_filter
            current_mode_name = new_name
            direction_mode = "outbound" if self.rb_out.isChecked() else \
                              "inbound" if self.rb_in.isChecked() else "both"
            app_filter_name = self.app_input.text().strip()

        label_suffix = f" | DIR:{direction_mode.upper()}"
        if app_filter_name:
            label_suffix += f" | APP:{app_filter_name}"

        self.status_label.setText(f"Active: {new_name}{label_suffix}")
        self.overlay.mode_name = new_name + label_suffix
        self.overlay.update()

        log(f"Filter updated -> {new_name} : {new_filter} | dir={direction_mode} | app={app_filter_name}")

        with handle_lock:
            if current_handle is not None:
                try:
                    current_handle.close()
                    log("Forced handle close to apply new filter")
                except Exception:
                    log("Handle close during switch (expected, ignoring):")
                    log(traceback.format_exc())

        restart_event.set()


def toggle_block(overlay):
    global blocking
    with lock:
        blocking = not blocking
        state = blocking
        mode_name = current_mode_name
    overlay.toggle_signal.emit(state, mode_name)
    log(f"Block toggled: {'ON' if state else 'OFF'}  (mode: {mode_name})")


def toggle_delay(delay_overlay):
    global delay_enabled
    with lock:
        delay_enabled = not delay_enabled
        state = delay_enabled
        ms_value = delay_ms
    delay_overlay.toggle_signal.emit(state, ms_value)
    log(f"Delay toggled: {'ON' if state else 'OFF'}  ({ms_value}ms)")


# ---------- Hotkey (re)binding ----------
_block_hook = None
_delay_hook = None

def rebind_hotkeys():
    global _block_hook, _delay_hook
    if _block_hook is not None:
        try:
            keyboard.unhook(_block_hook)
        except Exception:
            pass
    if _delay_hook is not None:
        try:
            keyboard.unhook(_delay_hook)
        except Exception:
            pass

    _block_hook = keyboard.on_press_key(block_hotkey, lambda e: toggle_block(overlay_ref[0]))
    _delay_hook = keyboard.on_press_key(delay_hotkey, lambda e: toggle_delay(delay_overlay_ref[0]))
    log(f"Hotkeys active -> block: {block_hotkey} | delay: {delay_hotkey}")


overlay_ref = [None]
delay_overlay_ref = [None]


def packet_loop():
    global current_filter, blocked_count, passed_count, current_handle, in_flight_delays
    while True:
        restart_event.clear()
        with lock:
            filt = current_filter
        log(f"Opening WinDivert with filter: {filt}")

        w = None
        try:
            w = pydivert.WinDivert(filt)
            w.open()
            with handle_lock:
                current_handle = w
            log("Handle opened successfully")

            while not restart_event.is_set():
                try:
                    packet = w.recv()
                except Exception as e:
                    log(f"recv() ended ({e}) — reopening")
                    break

                with lock:
                    block_now = blocking
                    d_mode = direction_mode
                    app_name = app_filter_name
                    d_enabled = delay_enabled
                    d_ms = delay_ms

                should_block = False
                if block_now:
                    direction_ok = (
                        (d_mode == "outbound" and packet.is_outbound) or
                        (d_mode == "inbound" and not packet.is_outbound) or
                        (d_mode == "both")
                    )
                    if direction_ok:
                        if app_name:
                            port = packet.src_port if packet.is_outbound else packet.dst_port
                            if port in app_ports:
                                should_block = True
                        else:
                            should_block = True

                if should_block:
                    blocked_count += 1
                    continue

                passed_count += 1

                direction_matches_delay = (
                    (d_mode == "outbound" and packet.is_outbound) or
                    (d_mode == "inbound" and not packet.is_outbound) or
                    (d_mode == "both")
                )

                if d_enabled and d_ms > 0 and direction_matches_delay:
                    with lock:
                        in_flight_delays += 1
                    threading.Thread(
                        target=send_after_delay,
                        args=(w, packet, d_ms / 1000.0),
                        daemon=True
                    ).start()
                else:
                    try:
                        w.send(packet)
                    except Exception as e:
                        log(f"send() failed ({e}) — reopening")
                        break

        except Exception:
            log("packet_loop error (will retry):")
            log(traceback.format_exc())
        finally:
            with handle_lock:
                current_handle = None
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass

        time.sleep(0.1)


def main():
    global current_filter, current_mode_name
    current_filter = DEFAULT_FILTER
    current_mode_name = DEFAULT_MODE_NAME

    log(f"=== Script started, default filter: {current_mode_name} ({current_filter}) ===")
    app = QtWidgets.QApplication(sys.argv)

    overlay = Overlay()
    delay_overlay = DelayOverlay()
    overlay_ref[0] = overlay
    delay_overlay_ref[0] = delay_overlay

    panel = DebugPanel(overlay, delay_overlay)
    panel.show()

    rebind_hotkeys()

    threading.Thread(target=packet_loop, daemon=True).start()
    threading.Thread(target=app_port_tracker, daemon=True).start()

    log("Entering app.exec()")
    sys.exit(app.exec())

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("TOP-LEVEL EXCEPTION:")
        log(traceback.format_exc())
