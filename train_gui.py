#!/usr/bin/env python3
"""RayHat eğitim simülasyonu: Tkinter komuta merkezi + yerel Unix IPC."""
from __future__ import annotations
import math
import os
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk

SOCKET_PATH = "/tmp/rayhat_threadx.sock"
LOCAL_SOCKET = f"/tmp/rayhat_gui_{os.getpid()}.sock"
MAX_SPEED, YELLOW_SPEED = 120, 40
STATION_LIMIT_DEFAULT = 30
COLORS = {
    "bg": "#09111e", "panel": "#121e2f", "panel2": "#1a2a40",
    "line": "#2b3d55", "text": "#e7eef8", "muted": "#94a8c2",
    "green": "#35d69a", "yellow": "#f3c75b", "red": "#ff5967",
    "blue": "#64aaff", "cyan": "#54d6df", "purple": "#bd9aff",
}
ASPECTS = {0: "KIRMIZI", 1: "SARI", 2: "YEŞİL"}
STATE_KEYS = ("speed_dkph", "limit_dkph", "target_dkph", "emergency", "mode",
              "signal", "block", "station_limit_dkph", "position_cm",
              "stop_requested", "doors_open", "service_brake", "hold_brake",
              "at_station", "station_number")
ERRORS = {
    1: "Hız değeri 0–120 km/sa aralığında olmalı.",
    2: "Acil fren reseti reddedildi: tren durmuş, blok boş ve sinyal kırmızı dışında olmalı.",
    3: "Kapı/istasyon işlemi reddedildi: tren istasyonda değil.",
    4: "İşlem reddedildi: tren hareket ediyor.",
    5: "Kalkış reddedildi: kapılar açık.",
    6: "Kapılar zaten kapalı.",
    7: "Kalkış reddedildi: sinyal veya blok hareket izni vermiyor.",
    8: "İstasyon durağı zaten tamamlanmış.",
    9: "Zaten bir sonraki istasyona duruş isteği var.",
    10: "Aktif bir istasyon duruş isteği bulunmuyor.",
    11: "Sonraki istasyon geçilmiş; duruş isteği uygulanamaz.",
    12: "Kalkış reddedildi: acil veya servis freni devrede.",
}


def fresh_state():
    return {"speed_dkph": 0, "limit_dkph": 800, "target_dkph": 400,
            "emergency": 0, "mode": 0, "signal": 2, "block": 0,
            "station_limit_dkph": 300, "position_cm": 0,
            "stop_requested": 0, "doors_open": 0, "service_brake": 0,
            "hold_brake": 0, "at_station": 0, "station_number": 1}


def speed_kmh(state, field="speed_dkph"):
    return state[field] / 10.0


def aspect_name(state):
    return "KIRMIZI" if state["block"] else ASPECTS.get(state["signal"], "?")


def base_authority(state):
    cap = state["limit_dkph"]
    if state["block"] or state["signal"] == 0:
        return 0
    return min(cap, 400) if state["signal"] == 1 else cap


def station_cap(state, next_station_cm):
    cap = base_authority(state)
    distance = max(0, next_station_cm - state["position_cm"])
    if distance <= 300000:
        cap = min(cap, state["station_limit_dkph"])
    if state["stop_requested"]:
        cap = min(cap, int(math.sqrt(distance * 26.0)))
    return cap


class DemoSimulator:
    """Bağımsız demo backend; IPC modu seçiliyken simülasyon kesinlikle duraklatılır."""
    def __init__(self, events):
        self.lock = threading.Lock()
        self.state = fresh_state()
        self.events = events
        self.active = True
        self.running = True
        self.next_station_cm = 120000
        self.station_position_cm = 0.0
        self.speed_dkph = 0.0
        self.position_cm = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_active(self, active):
        with self.lock:
            self.active = active

    def snapshot(self):
        with self.lock:
            out = dict(self.state)
            out["speed_dkph"] = int(round(self.speed_dkph))
            out["position_cm"] = int(self.position_cm)
            return out

    def command(self, command):
        parts = command.strip().upper().split()
        if not parts:
            return False
        with self.lock:
            s = self.state
            if len(parts) == 2 and parts[0] in ("TARGET", "LIMIT"):
                try:
                    value = int(parts[-1])
                except ValueError:
                    self.events.put(("log", f"Reddedildi — sayı okunamadı: {command}"))
                    return False
                if not 0 <= value <= MAX_SPEED * 10:
                    self.events.put(("log", "Reddedildi — hız değeri 0–120 km/sa aralığında olmalı."))
                    return False
                if parts[0] == "TARGET":
                    s["target_dkph"] = value
                elif parts[0] == "LIMIT":
                    s["limit_dkph"] = value
                else:
                    self.events.put(("log", f"Reddedildi — bilinmeyen komut: {command}"))
                    return False
            elif len(parts) == 3 and parts[:2] == ["STATION", "LIMIT"]:
                try:
                    value = int(parts[2])
                except ValueError:
                    return False
                if not 0 <= value <= MAX_SPEED * 10:
                    self.events.put(("log", "Reddedildi — istasyon limiti 0–120 km/sa aralığında olmalı."))
                    return False
                s["station_limit_dkph"] = value
            elif parts == ["MODE", "AUTO"]:
                s["mode"] = 0
            elif parts == ["MODE", "MANUAL"]:
                s["mode"] = 1
            elif parts[:2] == ["BRAKE", "EMERGENCY"] and len(parts) == 3:
                if parts[2] == "ON":
                    s["emergency"] = 1
                elif parts[2] == "OFF":
                    if self.speed_dkph != 0 or s["block"] or s["signal"] == 0:
                        self.events.put(("log", ERRORS[2]))
                        return False
                    s["emergency"] = 0
                else:
                    return False
            elif parts[:2] == ["BRAKE", "SERVICE"] and len(parts) == 3:
                if parts[2] not in ("ON", "OFF"): return False
                s["service_brake"] = int(parts[2] == "ON")
            elif parts[:2] == ["BRAKE", "HOLD"] and len(parts) == 3:
                if parts[2] not in ("ON", "OFF"): return False
                s["hold_brake"] = int(parts[2] == "ON")
            elif parts == ["SIGNAL", "RED"]:
                s["signal"] = 0
            elif parts == ["SIGNAL", "YELLOW"]:
                s["signal"] = 1
            elif parts == ["SIGNAL", "GREEN"]:
                s["signal"] = 2
            elif parts == ["BLOCK", "OCCUPIED"]:
                s["block"] = 1
            elif parts == ["BLOCK", "CLEAR"]:
                s["block"] = 0
            elif parts == ["STATION", "STOP"]:
                if s["at_station"]:
                    self.events.put(("log", ERRORS[8])); return False
                if s["stop_requested"]:
                    self.events.put(("log", ERRORS[9])); return False
                if self.next_station_cm <= self.position_cm + 100:
                    self.events.put(("log", ERRORS[11])); return False
                s["stop_requested"] = 1
            elif parts == ["STATION", "CANCEL"]:
                if not s["stop_requested"]:
                    self.events.put(("log", ERRORS[10])); return False
                s["stop_requested"] = 0
            elif parts == ["DOORS", "OPEN"]:
                if not s["at_station"]:
                    self.events.put(("log", ERRORS[3])); return False
                if self.speed_dkph != 0:
                    self.events.put(("log", ERRORS[4])); return False
                s["doors_open"] = 1
            elif parts == ["DOORS", "CLOSE"]:
                if not s["doors_open"]:
                    self.events.put(("log", ERRORS[6])); return False
                s["doors_open"] = 0
            elif parts == ["DEPART"]:
                if not s["at_station"]:
                    self.events.put(("log", ERRORS[3])); return False
                if self.speed_dkph != 0:
                    self.events.put(("log", ERRORS[4])); return False
                if s["doors_open"]:
                    self.events.put(("log", ERRORS[5])); return False
                if s["emergency"] or s["service_brake"]:
                    self.events.put(("log", ERRORS[12])); return False
                if s["signal"] == 0 or s["block"]:
                    self.events.put(("log", ERRORS[7])); return False
                s["at_station"] = 0
                s["hold_brake"] = 0
                s["stop_requested"] = 0
                s["station_number"] += 1
                self.next_station_cm += 150000
            elif parts == ["RESET"]:
                self.state.clear(); self.state.update(fresh_state())
                self.next_station_cm = 120000
                self.position_cm = 0.0
                self.speed_dkph = 0.0
                self.station_position_cm = 0.0
            else:
                self.events.put(("log", f"Reddedildi — bilinmeyen komut: {command}"))
                return False
            self.events.put(("log", f"Uygulandı: {command}"))
            return True

    def _run(self):
        last = time.monotonic()
        while self.running:
            time.sleep(0.05)
            now = time.monotonic()
            dt = min(now - last, 0.15)
            last = now
            with self.lock:
                if not self.active:
                    continue
                s = self.state
                s["position_cm"] = int(self.position_cm)
                s["speed_dkph"] = int(round(self.speed_dkph))
                authority = base_authority(s)
                cap = station_cap(s, self.next_station_cm)
                target = min(s["target_dkph"], cap)
                if self.speed_dkph > authority:
                    s["emergency"] = 1
                if s["at_station"]:
                    self.speed_dkph = 0.0
                    self.station_position_cm = float(self.next_station_cm)
                    s["speed_dkph"] = 0
                    s["position_cm"] = int(self.position_cm)
                    continue
                if s["emergency"]:
                    self.speed_dkph = max(0.0, self.speed_dkph - 90.0 * dt)
                elif s["service_brake"] or s["hold_brake"] or self.speed_dkph > target:
                    self.speed_dkph = max(0.0, self.speed_dkph - 36.0 * dt)
                else:
                    accel = 20.0 if s["mode"] == 0 else 10.0
                    self.speed_dkph = min(float(target), self.speed_dkph + accel * dt)
                self.position_cm += (self.speed_dkph / 10.0) * 100000.0 * dt / 3600.0
                self.station_position_cm = float(self.next_station_cm)
                distance = self.next_station_cm - self.position_cm
                if s["stop_requested"] and distance <= max(500.0, self.speed_dkph * 5.0):
                    if self.speed_dkph <= 40.0:
                        self.position_cm = float(self.next_station_cm)
                        self.speed_dkph = 0.0
                        s["at_station"] = 1
                        s["stop_requested"] = 0
                        s["hold_brake"] = 1
                        self.events.put(("log", f"İstasyona varıldı: {s['station_number']} — tren durdu."))
                elif not s["stop_requested"] and self.position_cm >= self.next_station_cm:
                    self.next_station_cm += 150000
                    s["station_number"] += 1
                s["position_cm"] = int(self.position_cm)
                s["speed_dkph"] = int(round(self.speed_dkph))

    def close(self):
        self.running = False


class IPCClient:
    """ThreadX Linux portu ile AF_UNIX datagram IPC; ağ/loopback fallback yoktur."""
    def __init__(self, events, updates):
        self.events, self.updates = events, updates
        self.commands = queue.Queue()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def send(self, command):
        self.commands.put(command)

    def _run(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        local_path = LOCAL_SOCKET
        try:
            if os.path.exists(local_path): os.unlink(local_path)
            sock.bind(local_path)
            sock.settimeout(0.2)
        except OSError as exc:
            self.events.put(("connection", False, f"IPC istemci soketi açılamadı: {exc}"))
            return
        last_good = 0.0
        last_poll = 0.0
        connected = False
        while self.running:
            command = None
            try:
                command = self.commands.get(timeout=0.02)
            except queue.Empty:
                pass
            now = time.monotonic()
            if command is None and now - last_poll < 0.05:
                continue
            last_poll = now
            try:
                sock.sendto((command or "GET").encode("ascii"), SOCKET_PATH)
                data = sock.recv(512)
                parts = data.decode("ascii", errors="replace").strip().split(",")
                if len(parts) != 17 or parts[0] not in ("STATE", "ACK", "ERR"):
                    raise ValueError("ThreadX yanıt biçimi geçersiz")
                result = int(parts[1])
                values = list(map(int, parts[2:]))
                if len(values) != len(STATE_KEYS):
                    raise ValueError("ThreadX durum alanları eksik")
                self.updates.put(dict(zip(STATE_KEYS, values)))
                if parts[0] == "ERR":
                    message = ERRORS.get(result, f"ThreadX komutu reddetti (kod {result}).")
                    self.events.put(("log", f"UYGULANMADI — {command}: {message}"))
                elif command:
                    self.events.put(("log", f"ThreadX onayladı: {command}"))
                if command:
                    self.events.put(("command_result", command, parts[0] != "ERR", result))
                last_good = time.monotonic()
                if not connected:
                    connected = True
                    self.events.put(("connection", True, f"Yerel IPC bağlı: {SOCKET_PATH}"))
            except (OSError, ValueError) as exc:
                if command:
                    self.events.put(("log", f"ONAY ALINAMADI — {command}: IPC yanıt vermedi."))
                    self.events.put(("command_result", command, False, -1))
                if connected and time.monotonic() - last_good > 0.8:
                    connected = False
                    self.events.put(("connection", False, f"BAĞLANTI KURULAMADI — {exc}"))
                elif not connected and time.monotonic() - last_good > 0.8:
                    # Tekrarlayan log spam yerine yalnızca bağlantı durumunu güncelle.
                    self.events.put(("connection", False, f"BAĞLANTI KURULAMADI — {SOCKET_PATH}"))
                    last_good = time.monotonic()
        sock.close()
        try:
            os.unlink(local_path)
        except OSError:
            pass

    def close(self):
        self.running = False


class TrainControlCenter:
    def __init__(self, root):
        self.root = root
        root.title("RayHat | Yerel IPC Tren Simülasyonu")
        root.geometry("1220x840")
        root.minsize(1050, 740)
        root.configure(bg=COLORS["bg"])
        self.events, self.updates = queue.Queue(), queue.Queue()
        self.demo = DemoSimulator(self.events)
        self.ipc = IPCClient(self.events, self.updates)
        self.backend = tk.StringVar(value="demo")
        self.connected = True
        self.ipc_connected = False
        self.remote_state = fresh_state()
        self.state = fresh_state()
        self.dirty = {"target": False, "limit": False, "station": False}
        self.pending_slider_commands = {}
        self.syncing = False
        self.last_speed_sample = None
        self.last_speed_time = time.monotonic()
        self.last_motion_update = self.last_speed_time
        self.filtered_acceleration = 0.0
        self.motion_state = "DURUYOR"
        self.motion_candidate = None
        self.motion_candidate_since = self.last_speed_time
        self.render_position_cm = 0.0
        self.render_position_time = self.last_speed_time
        self.remote_update_seen = False
        self.target_var, self.limit_var, self.station_var = tk.IntVar(value=40), tk.IntVar(value=80), tk.IntVar(value=30)
        self.mode_var = tk.StringVar(value="AUTO")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()

    def _build(self):
        style = ttk.Style(); style.theme_use("clam")
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=("Segoe UI", 21, "bold"))
        style.configure("Sub.TLabel", background=COLORS["bg"], foreground=COLORS["muted"], font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 9, "bold"), padding=(8, 7), background=COLORS["panel2"], foreground=COLORS["text"])
        style.map("TButton", background=[("active", "#2b4360")])
        style.configure("TRadiobutton", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI", 9))
        style.map("TRadiobutton", background=[("active", COLORS["panel"])])
        style.configure("Horizontal.TScale", background=COLORS["panel"], troughcolor=COLORS["line"])

        header = ttk.Frame(self.root, padding=(22, 14, 22, 8)); header.pack(fill="x")
        self.connection_badge = tk.Label(header, text="● DEMO MODU", bg=COLORS["bg"], fg=COLORS["cyan"], font=("Segoe UI", 10, "bold"))
        self.connection_badge.pack(side="left", padx=(0, 16), pady=(8, 0))
        ttk.Label(header, text="RAYHAT", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="  IPC SİNYALİZASYON LABORATUVARI", style="Sub.TLabel").pack(side="left", pady=(9, 0))

        cards = ttk.Frame(self.root, padding=(22, 4, 22, 9)); cards.pack(fill="x")
        self.speed_card = self._card(cards, "TREN HIZI", "— km/sa", COLORS["cyan"])
        self.limit_card = self._card(cards, "HAT / ETKİN LİMİT", "— / —", COLORS["blue"])
        self.accel_card = self._card(cards, "HAREKET", "—", COLORS["green"])
        self.station_card = self._card(cards, "SONRAKİ İSTASYON", "—", COLORS["yellow"])

        main = ttk.Frame(self.root, padding=(22, 5, 22, 7)); main.pack(fill="both", expand=True)
        left = ttk.Frame(main, style="Panel.TFrame", padding=15); left.pack(side="left", fill="both", expand=True, padx=(0, 9))
        right = ttk.Frame(main, style="Panel.TFrame", padding=15, width=375); right.pack(side="right", fill="y", padx=(9, 0)); right.pack_propagate(False)
        ttk.Label(left, text="5 vagonlu tren · hız profili ve güzergâh", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.canvas = tk.Canvas(left, height=340, bg="#0d1928", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(9, 10))
        self.motion_label = ttk.Label(left, text="Hazır", font=("Segoe UI", 10, "bold")); self.motion_label.pack(anchor="w")
        self.hud_label = ttk.Label(left, text="", style="Muted.TLabel"); self.hud_label.pack(anchor="w", pady=(4, 0))

        ttk.Label(right, text="HIZ VE SÜRÜŞ KUMANDALARI", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        mode_row = ttk.Frame(right, style="Panel.TFrame"); mode_row.pack(fill="x", pady=(0, 6))
        ttk.Label(mode_row, text="Mod", width=10).pack(side="left")
        ttk.Radiobutton(mode_row, text="Otomatik", variable=self.mode_var, value="AUTO", command=lambda: self._command("MODE AUTO")).pack(side="left", padx=3)
        ttk.Radiobutton(mode_row, text="Manuel", variable=self.mode_var, value="MANUAL", command=lambda: self._command("MODE MANUAL")).pack(side="left", padx=3)

        self._slider_row(right, "Hedef hız", self.target_var, "target", "TARGET")
        self._slider_row(right, "Hat limiti", self.limit_var, "limit", "LIMIT")
        self._slider_row(right, "İstasyon limiti", self.station_var, "station", "STATION LIMIT")

        ttk.Label(right, text="Sinyal / blok (simülasyon)", style="Muted.TLabel").pack(anchor="w", pady=(5, 3))
        signal_row = ttk.Frame(right, style="Panel.TFrame"); signal_row.pack(fill="x", pady=(0, 4))
        for text, cmd in (("Kırmızı", "SIGNAL RED"), ("Sarı", "SIGNAL YELLOW"), ("Yeşil", "SIGNAL GREEN")):
            ttk.Button(signal_row, text=text, command=lambda c=cmd: self._command(c)).pack(side="left", expand=True, fill="x", padx=2)
        block_row = ttk.Frame(right, style="Panel.TFrame"); block_row.pack(fill="x", pady=(0, 6))
        ttk.Button(block_row, text="Blok dolu", command=lambda: self._command("BLOCK OCCUPIED")).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(block_row, text="Blok boş", command=lambda: self._command("BLOCK CLEAR")).pack(side="left", expand=True, fill="x", padx=2)

        ttk.Label(right, text="İstasyon ve kapılar", style="Muted.TLabel").pack(anchor="w", pady=(2, 3))
        station_row = ttk.Frame(right, style="Panel.TFrame"); station_row.pack(fill="x", pady=(0, 6))
        ttk.Button(station_row, text="İSTASYONDA DUR", command=lambda: self._command("STATION STOP")).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(station_row, text="Duruşu iptal", command=lambda: self._command("STATION CANCEL")).pack(side="left", expand=True, fill="x", padx=2)
        door_row = ttk.Frame(right, style="Panel.TFrame"); door_row.pack(fill="x", pady=(0, 6))
        ttk.Button(door_row, text="Kapıları aç", command=lambda: self._command("DOORS OPEN")).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(door_row, text="Kapıları kapat", command=lambda: self._command("DOORS CLOSE")).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(right, text="Sonraki istasyona kalkış", command=lambda: self._command("DEPART")).pack(fill="x", pady=(0, 7))

        ttk.Label(right, text="Frenler", style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        brake_row = ttk.Frame(right, style="Panel.TFrame"); brake_row.pack(fill="x", pady=(0, 7))
        ttk.Button(brake_row, text="Manuel durak/servis freni", command=self._toggle_service).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(brake_row, text="Tutma freni ON/OFF", command=self._toggle_hold).pack(side="left", expand=True, fill="x", padx=2)
        emergency_row = ttk.Frame(right, style="Panel.TFrame"); emergency_row.pack(fill="x", pady=(0, 7))
        ttk.Button(emergency_row, text="ACİL FREN", command=lambda: self._command("BRAKE EMERGENCY ON")).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(emergency_row, text="Acil reset", command=lambda: self._command("BRAKE EMERGENCY OFF")).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(right, text="Simülasyonu sıfırla", command=lambda: self._command("RESET")).pack(fill="x", pady=(0, 8))

        ttk.Separator(right).pack(fill="x", pady=5)
        ttk.Label(right, text="BAĞLANTI KAYNAĞI", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Radiobutton(right, text="Dahili Python demo", variable=self.backend, value="demo", command=self._backend_changed).pack(anchor="w", pady=(4, 0))
        ttk.Radiobutton(right, text="ThreadX yerel IPC", variable=self.backend, value="ipc", command=self._backend_changed).pack(anchor="w")
        self.backend_label = ttk.Label(right, text="Demo etkin", style="Muted.TLabel", wraplength=340)
        self.backend_label.pack(anchor="w", pady=(3, 6))

        ttk.Label(right, text="OLAY / KOMUT GÜNLÜĞÜ", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(2, 4))
        log_frame = ttk.Frame(right, style="Panel.TFrame"); log_frame.pack(fill="both", expand=True)
        self.log = tk.Listbox(log_frame, bg=COLORS["bg"], fg=COLORS["text"], selectbackground=COLORS["panel2"],
                              relief="flat", font=("Consolas", 8), highlightthickness=0)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set); self.log.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")

        footer = ttk.Frame(self.root, padding=(22, 5, 22, 10)); footer.pack(fill="x")
        ttk.Label(footer, text="Eğitim simülasyonudur; gerçek tren/sinyalizasyon ağına bağlanmaz.", style="Sub.TLabel").pack(side="left")
        ttk.Label(footer, text=f"ThreadX IPC: {SOCKET_PATH}", style="Sub.TLabel").pack(side="right")

    def _card(self, parent, title, value, color):
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 9)); frame.pack(side="left", fill="both", expand=True, padx=4)
        ttk.Label(frame, text=title, style="Muted.TLabel").pack(anchor="w")
        label = ttk.Label(frame, text=value, font=("Segoe UI", 18, "bold"), foreground=color); label.pack(anchor="w", pady=(4, 0))
        return label

    def _slider_row(self, parent, title, variable, key, prefix):
        row = ttk.Frame(parent, style="Panel.TFrame"); row.pack(fill="x", pady=3)
        header = ttk.Frame(row, style="Panel.TFrame"); header.pack(fill="x")
        ttk.Label(header, text=title, font=("Segoe UI", 9, "bold")).pack(side="left")
        value_label = ttk.Label(header, text=f"{variable.get()} km/sa", style="Muted.TLabel"); value_label.pack(side="right")
        body = ttk.Frame(row, style="Panel.TFrame"); body.pack(fill="x", pady=(0, 2))
        scale = ttk.Scale(body, from_=0, to=MAX_SPEED, variable=variable, orient="horizontal")
        scale.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(body, text="Uygula", command=lambda: self._apply_speed(key, prefix, variable)).pack(side="right")
        def changed(v):
            value_label.configure(text=f"{int(float(v))} km/sa")
            if not self.syncing: self.dirty[key] = True
        scale.configure(command=changed)
        scale.bind("<ButtonPress-1>", lambda _event, k=key: self.dirty.__setitem__(k, True))
        setattr(self, f"{key}_value_label", value_label)
        setattr(self, f"{key}_scale", scale)

    def _apply_speed(self, key, prefix, variable):
        value = int(float(variable.get()))
        command = f"{prefix} {value * 10}"
        if self.backend.get() == "demo":
            self.dirty[key] = False
            self._command(command)
            return
        self.dirty[key] = True
        self.pending_slider_commands[command] = (key, value)
        if not self._command(command):
            self.pending_slider_commands.pop(command, None)
            self.dirty[key] = False

    def _command(self, command):
        if self.backend.get() == "demo":
            return self.demo.command(command)
        if not self.connected:
            self._log(f"GÖNDERİLMEDİ — bağlantı yok: {command}")
            return False
        self.ipc.send(command)
        return True

    def _toggle_service(self):
        self._command("BRAKE SERVICE OFF" if self.state["service_brake"] else "BRAKE SERVICE ON")

    def _toggle_hold(self):
        self._command("BRAKE HOLD OFF" if self.state["hold_brake"] else "BRAKE HOLD ON")

    def _backend_changed(self):
        is_demo = self.backend.get() == "demo"
        self.demo.set_active(is_demo)
        self.dirty = {"target": False, "limit": False, "station": False}
        self.pending_slider_commands.clear()
        if is_demo:
            self.connected = True
            self.state = self.demo.snapshot()
            self.connection_badge.configure(text="● DEMO MODU", fg=COLORS["cyan"])
            self.backend_label.configure(text="Dahili Python demo etkin; ThreadX durumuna geçişte bu model duraklatılır.")
            self._log("Dahili Python demo seçildi.")
        else:
            self.connected = self.ipc_connected
            self.state = {key: 0 for key in STATE_KEYS}
            if self.connected:
                self.connection_badge.configure(text="● THREADX IPC BAĞLI", fg=COLORS["green"])
                self.backend_label.configure(text=f"Yerel IPC bağlı: {SOCKET_PATH}")
            else:
                self.connection_badge.configure(text="● BAĞLANTI KURULAMADI", fg=COLORS["red"])
                self.backend_label.configure(text=f"ThreadX Unix IPC bekleniyor: {SOCKET_PATH} — demo simülasyonu duraklatıldı.")
            self._log("ThreadX IPC seçildi; Python demo duraklatıldı. Otomatik fallback yok.")

    def _log(self, message):
        self.log.insert("end", time.strftime("%H:%M:%S  ") + message)
        if self.log.size() > 150: self.log.delete(0)
        self.log.see("end")

    def _draw_route_map(self, c, width, state):
        left, right, y = 34, width - 34, 72
        spacing = max(72.0, (right - left) / 4.0)
        c.create_rectangle(12, 40, width - 12, 101, fill="#101d2b", outline="#30445a")
        c.create_text(22, 48, text="HAT PLANI", fill=COLORS["muted"], anchor="w",
                      font=("Segoe UI", 7, "bold"))
        c.create_line(left, y, right, y, fill="#71849a", width=3)
        station_number = max(1, int(state["station_number"]))
        previous_cm = 0 if station_number == 1 else 120000 + (station_number - 2) * 150000
        next_cm = 120000 + (station_number - 1) * 150000
        segment = max(1, next_cm - previous_cm)
        progress = max(0.0, min(1.0, (state["position_cm"] - previous_cm) / segment))
        for index in range(5):
            x = left + index * spacing
            c.create_line(x, y - 7, x, y + 7, fill="#b6c7d8", width=2)
            if index == 0:
                name = "ÖNCEKİ" if station_number > 1 else "BAŞLANGIÇ"
                c.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#7a8da2", outline="")
            else:
                number = station_number + index - 1
                name = f"DURAK {number:02d}"
                c.create_rectangle(x - 13, y - 15, x + 13, y - 3, fill="#1d435d", outline="#62bad2")
                c.create_line(x - 15, y - 15, x, y - 22, x + 15, y - 15,
                              fill="#8bd3e5", width=2)
                c.create_rectangle(x - 2, y - 10, x + 2, y - 3, fill="#9bd9e7", outline="")
            c.create_text(x, y + 18, text=name, fill=COLORS["text"],
                          font=("Segoe UI", 7, "bold"))
        train_x = left + progress * spacing
        c.create_rectangle(train_x - 11, y - 7, train_x + 11, y + 2,
                           fill="#52b7df", outline="#e4f7ff")
        c.create_rectangle(train_x - 7, y - 5, train_x - 2, y - 2, fill="#d5f5ff", outline="")
        c.create_rectangle(train_x + 1, y - 5, train_x + 6, y - 2, fill="#d5f5ff", outline="")
        c.create_text(train_x, y - 27, text="TREN", fill="#83d7fa",
                      font=("Segoe UI", 7, "bold"))

    def _draw_scene(self, render_state=None):
        c = self.canvas; c.delete("all")
        w, h = max(c.winfo_width(), 600), max(c.winfo_height(), 300)
        s = render_state if render_state is not None else self.state
        x_train = int(w * 0.46); track_y = int(h * 0.67)
        # Tünel/yeraltı istasyonu arka planı.
        c.create_rectangle(0, 0, w, track_y - 35, fill="#0d1928", outline="")
        c.create_arc(18, -70, w - 18, track_y + 30, start=0, extent=180,
                     style="arc", outline="#2b4058", width=8)
        for x in range(36, w, 72):
            c.create_line(x, 38, x, track_y - 30, fill="#182a3e", width=2)
            c.create_oval(x - 2, 47, x + 2, 51, fill="#f2ce88", outline="")
        self._draw_route_map(c, w, s)
        # Hareket eden traversler, tren hızıyla zıt yönde akar.
        offset = (s["position_cm"] // 90) % 34
        for x in range(-34, w + 34, 34):
            xx = x - offset
            c.create_line(xx, track_y - 14, xx, track_y + 23, fill="#35475b", width=4)
        c.create_line(0, track_y - 8, w, track_y - 8, fill="#aeb9c6", width=3)
        c.create_line(0, track_y + 12, w, track_y + 12, fill="#aeb9c6", width=3)
        # İstasyon platformu, yaklaşma mesafesine göre trenin önünde belirir.
        next_station_cm = (120000 + max(0, s["station_number"] - 1) * 150000)
        distance_m = (next_station_cm - s["position_cm"]) / 100.0
        station_x = x_train + max(80, min(w * 0.48, distance_m * 0.10))
        if distance_m < 1800:
            px = station_x
            c.create_rectangle(px - 8, track_y - 88, px + 125, track_y - 17, fill="#1b2e43", outline="#44607c", width=2)
            c.create_line(px - 8, track_y - 88, px + 125, track_y - 88, fill="#77bfd5", width=5)
            for dx in (12, 47, 82):
                c.create_rectangle(px + dx, track_y - 76, px + dx + 19, track_y - 70, fill="#b6e8ee", outline="")
            c.create_text(px + 58, track_y - 51, text=f"İSTASYON {s['station_number']}", fill=COLORS["text"], font=("Segoe UI", 9, "bold"))
            c.create_text(px + 58, track_y - 32, text=f"{max(0, distance_m):.0f} m", fill=COLORS["yellow"], font=("Segoe UI", 9))
        # Blok renk katmanı ve sinyal.
        block_color = COLORS["red"] if s["block"] or s["signal"] == 0 else COLORS["yellow"] if s["signal"] == 1 else COLORS["green"]
        sigx = int(w * 0.88); sy = track_y - 105
        c.create_line(sigx, sy + 20, sigx, track_y - 18, fill="#7b8998", width=4)
        c.create_rectangle(sigx - 13, sy - 33, sigx + 13, sy + 27, fill="#202b3a", outline="#56677b")
        for i, col in enumerate((COLORS["red"], COLORS["yellow"], COLORS["green"])):
            active = (s["signal"] == i and not s["block"])
            c.create_oval(sigx - 8, sy - 27 + i * 17, sigx + 8, sy - 11 + i * 17,
                          fill=col if active else "#273344", outline="")
        c.create_text(sigx, sy - 46, text=aspect_name(s), fill=block_color, font=("Segoe UI", 8, "bold"))
        # Beş bağlı vagon: öndeki kontrol kabini sağda, her vagonda pencereler/kapılar/tekerler.
        car_w, car_h, gap = 58, 42, 5
        total = 5 * car_w + 4 * gap
        left_x = x_train - total + 6
        car_colors = ("#3974a8", "#326a9a", "#2e638f", "#326a9a", "#4386bb")
        for i in range(5):
            x = left_x + i * (car_w + gap)
            y = track_y - car_h - 2
            c.create_rectangle(x, y, x + car_w, track_y - 3, fill=car_colors[i], outline="#9bc8e8", width=1)
            c.create_rectangle(x + 6, y + 6, x + car_w - 6, y + 17, fill="#b7e6f0", outline="#d2f4f7")
            c.create_rectangle(x + car_w // 2 - 4, y + 20, x + car_w // 2 + 4, track_y - 4, fill="#254967", outline="")
            c.create_oval(x + 8, track_y - 8, x + 20, track_y + 4, fill="#101722", outline="#8596a8")
            c.create_oval(x + car_w - 20, track_y - 8, x + car_w - 8, track_y + 4, fill="#101722", outline="#8596a8")
            c.create_line(x + car_w, y + 23, x + car_w + gap, y + 23, fill="#d1dae4", width=3)
        # Kabin farı / kapı durumu.
        if not s["at_station"]:
            c.create_oval(x_train - 4, track_y - 24, x_train + 2, track_y - 17, fill="#fff1b0", outline="")
        if s["doors_open"]:
            for i in range(5):
                x = left_x + i * (car_w + gap) + car_w // 2 - 4
                c.create_rectangle(x - 2, track_y - 27, x + 3, track_y - 3, fill=COLORS["yellow"], outline="")
        # HUD: istasyon talebi, fren ve blok durumları.
        chips = []
        if s["stop_requested"]: chips.append(("İSTASYON DURUŞU AKTİF", COLORS["yellow"]))
        if s["service_brake"]: chips.append(("SERVİS FRENİ", COLORS["blue"]))
        if s["hold_brake"]: chips.append(("TUTMA FRENİ", COLORS["purple"]))
        if s["emergency"]: chips.append(("ACİL FREN", COLORS["red"]))
        if s["doors_open"]: chips.append(("KAPILAR AÇIK", COLORS["yellow"]))
        cx = 12
        for text, color in chips:
            c.create_rectangle(cx, 12, cx + len(text) * 7 + 18, 34, fill="#17263a", outline=color)
            c.create_text(cx + 9, 23, text=text, fill=color, anchor="w", font=("Segoe UI", 8, "bold"))
            cx += len(text) * 7 + 26
        # Hız izi: hızlandıkça arkadaki çizgi ve traversler daha hızlı akar.
        kmh = speed_kmh(s)
        c.create_text(20, h - 19, text=f"MESAFE {s['position_cm']/100:.0f} m    HIZ {kmh:.1f} km/sa    SONRAKİ DURAK {max(0,distance_m):.0f} m",
                      fill=COLORS["muted"], anchor="w", font=("Consolas", 9))

    def _motion_display(self, s, kmh, now):
        if s["emergency"]:
            desired, color = "ACİL FREN", COLORS["red"]
        elif s["service_brake"]:
            desired, color = "SERVİS FRENİ", COLORS["blue"]
        elif s["hold_brake"]:
            desired, color = "TUTMA FRENİ", COLORS["purple"]
        elif s["at_station"]:
            desired, color = "İSTASYONDA DURUYOR", COLORS["yellow"]
        else:
            color = COLORS["green"]
            if kmh < 0.2:
                desired, color = "DURUYOR", COLORS["muted"]
            elif self.filtered_acceleration > 0.65:
                desired = "HIZLANIYOR"
            elif self.filtered_acceleration < -0.65:
                desired, color = "YAVAŞLIYOR", COLORS["yellow"]
            else:
                desired = "SABİT HIZ"

        if desired in ("ACİL FREN", "SERVİS FRENİ", "TUTMA FRENİ", "İSTASYONDA DURUYOR"):
            self.motion_state = desired
            self.motion_candidate = None
        elif desired != self.motion_state:
            if desired != self.motion_candidate:
                self.motion_candidate = desired
                self.motion_candidate_since = now
            dwell = 0.14 if desired in ("HIZLANIYOR", "YAVAŞLIYOR") else 0.38
            if now - self.motion_candidate_since >= dwell:
                self.motion_state = desired
                self.motion_candidate = None
        else:
            self.motion_candidate = None

        if self.motion_state == "HIZLANIYOR":
            return "HIZLANIYOR", COLORS["green"]
        if self.motion_state == "YAVAŞLIYOR":
            return "YAVAŞLIYOR", COLORS["yellow"]
        if self.motion_state == "SABİT HIZ":
            return "SABİT HIZ", COLORS["green"]
        if self.motion_state == "DURUYOR":
            return "DURUYOR", COLORS["muted"]
        return self.motion_state, color

    def _refresh(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "log":
                self._log(event[1])
            elif event[0] == "connection":
                self.ipc_connected = event[1]
                if self.backend.get() == "ipc":
                    self.connected = self.ipc_connected
                    if self.connected:
                        self.connection_badge.configure(text="● THREADX IPC BAĞLI", fg=COLORS["green"])
                        self.backend_label.configure(text=event[2])
                        self._log(event[2])
                    else:
                        self.connection_badge.configure(text="● BAĞLANTI KURULAMADI", fg=COLORS["red"])
                        self.backend_label.configure(text=event[2])
                        self._log(event[2])
            elif event[0] == "command_result":
                pending = self.pending_slider_commands.pop(event[1], None)
                if pending:
                    key, sent_value = pending
                    variable = getattr(self, f"{key}_var")
                    if int(float(variable.get())) == sent_value:
                        self.dirty[key] = False
        got_remote_status = False
        if self.backend.get() == "demo":
            self.state = self.demo.snapshot()
            self.render_position_cm = float(self.state["position_cm"])
            self.render_position_time = time.monotonic()
        else:
            while True:
                try:
                    self.remote_state = self.updates.get_nowait()
                    got_remote_status = True
                except queue.Empty: break
            if self.connected:
                self.state = dict(self.remote_state)
                if got_remote_status:
                    self.render_position_cm = float(self.state["position_cm"])
                    self.render_position_time = time.monotonic()
        s = self.state
        is_live = self.connected or self.backend.get() == "demo"
        sample_time = time.monotonic()
        if is_live:
            current_speed = s["speed_dkph"]
            if self.last_speed_sample is None:
                self.last_speed_sample = current_speed
                self.last_speed_time = sample_time
            elif current_speed != self.last_speed_sample:
                elapsed = max(0.05, sample_time - self.last_speed_time)
                raw_acceleration = max(-12.0, min(12.0, (current_speed - self.last_speed_sample) / 10.0 / elapsed))
                self.filtered_acceleration = 0.42 * raw_acceleration + 0.58 * self.filtered_acceleration
                self.last_speed_sample = current_speed
                self.last_speed_time = sample_time
            elif sample_time - self.last_speed_time > 0.12:
                quiet_dt = min(0.08, max(0.0, sample_time - self.last_motion_update))
                self.filtered_acceleration *= math.exp(-3.0 * quiet_dt)
            self.last_motion_update = sample_time
        else:
            self.last_speed_sample = None
            self.filtered_acceleration = 0.0
        display_state = dict(s)
        if is_live:
            elapsed_render = min(0.12, max(0.0, sample_time - self.render_position_time))
            predicted = self.render_position_cm + speed_kmh(s) * 100000.0 * elapsed_render / 3600.0
            next_station_cm = 120000 + max(0, s["station_number"] - 1) * 150000
            if s["stop_requested"] or s["at_station"]:
                predicted = min(predicted, next_station_cm)
            display_state["position_cm"] = int(predicted)
        for key, state_key in (("target", "target_dkph"), ("limit", "limit_dkph"), ("station", "station_limit_dkph")):
            if not self.dirty[key]:
                var = getattr(self, f"{key}_var")
                desired = round(s[state_key] / 10)
                if int(float(var.get())) != desired:
                    self.syncing = True; var.set(desired); self.syncing = False
                getattr(self, f"{key}_value_label").configure(text=f"{desired} km/sa")
        if self.connected or self.backend.get() == "demo":
            kmh = speed_kmh(s)
            authority = min(base_authority(s), s["station_limit_dkph"] if (120000 + max(0,s["station_number"]-1)*150000 - s["position_cm"]) <= 300000 else 1200)
            self.speed_card.configure(text=f"{kmh:05.1f} km/sa")
            self.limit_card.configure(text=f"{s['limit_dkph']/10:.0f} / {authority/10:.0f} km/sa")
            motion, color = self._motion_display(s, kmh, sample_time)
            self.accel_card.configure(text=motion, foreground=color)
            next_cm = 120000 + max(0, s["station_number"] - 1) * 150000
            distance = max(0, (next_cm - s["position_cm"]) / 100)
            station_text = "İSTASYONDA" if s["at_station"] else f"{distance:.0f} m · #{s['station_number']}"
            self.station_card.configure(text=station_text)
            self.mode_var.set("MANUAL" if s["mode"] else "AUTO")
        else:
            self.speed_card.configure(text="— km/sa")
            self.limit_card.configure(text="— / — km/sa")
            self.accel_card.configure(text="IPC BEKLENİYOR", foreground=COLORS["red"])
            self.station_card.configure(text="—")
        self.motion_label.configure(text=f"Sinyal {aspect_name(s)}  ·  Blok {'DOLU' if s['block'] else 'BOŞ'}  ·  {'Kapılar AÇIK' if s['doors_open'] else 'Kapılar kapalı'}  ·  {'Acil fren etkin' if s['emergency'] else 'Fren sistemi hazır'}")
        station_cm = 120000 + max(0, s["station_number"] - 1) * 150000
        self.hud_label.configure(text=f"Hedef {s['target_dkph']/10:.1f} km/sa  |  Hat {s['limit_dkph']/10:.1f}  |  İstasyon bölgesi {s['station_limit_dkph']/10:.1f}  |  {max(0,(station_cm-display_state['position_cm'])/100):.0f} m")
        self._draw_scene(display_state)
        self.root.after(16, self._refresh)

    def _close(self):
        self.demo.close(); self.ipc.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    TrainControlCenter(root)
    root.mainloop()


if __name__ == "__main__":
    main()
