from __future__ import annotations

import io
import time
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
from typing import Optional, List

try:
    import pyvisa
    PYVISA_AVAILABLE = True
except ImportError:
    PYVISA_AVAILABLE = False

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# MODEL: สื่อสารกับเครื่องมือจริงผ่าน SCPI/VISA

class InstrumentError(Exception):
    """เกิดข้อผิดพลาดระหว่างสื่อสารกับเครื่องมือ"""


class RigolInstrument:

    IDN_QUERY = "*IDN?"
    DEFAULT_TIMEOUT_MS = 5000
    CAPTURE_TIMEOUT_MS = 30000  

    def __init__(self):
        self._rm: Optional["pyvisa.ResourceManager"] = None
        self._inst = None
        self.resource_name: Optional[str] = None
        self.idn: Optional[str] = None

    # deverlop resource / connection
    @property
    def is_connected(self) -> bool:
        return self._inst is not None

    def list_resources(self) -> List[str]:
        """คืนรายชื่อ VISA resource ที่มองเห็น (เช่น USB, LAN, GPIB)"""
        if not PYVISA_AVAILABLE:
            return []
        if self._rm is None:
            self._rm = pyvisa.ResourceManager()
        try:
            return list(self._rm.list_resources())
        except Exception as exc:
            raise InstrumentError(f"ไม่สามารถค้นหา resource ได้: {exc}") from exc

    def connect(self, resource_name: str, timeout_ms: Optional[int] = None) -> str:
        """เชื่อมต่อกับเครื่องตาม resource string ที่ระบุ แล้วคืนค่า *IDN? กลับมา"""
        if not PYVISA_AVAILABLE:
            raise InstrumentError("ยังไม่ได้ติดตั้ง pyvisa (pip install pyvisa pyvisa-py)")
        if self._rm is None:
            self._rm = pyvisa.ResourceManager()
        try:
            self._inst = self._rm.open_resource(resource_name)
            self._inst.timeout = timeout_ms or self.DEFAULT_TIMEOUT_MS
            self._inst.read_termination = "\n"
            self._inst.write_termination = "\n"
            try:
                self._inst.chunk_size = 1024 * 1024  # 1 MB
            except Exception:
                pass  
            self.resource_name = resource_name
            self.idn = self._inst.query(self.IDN_QUERY).strip()
            return self.idn
        except Exception as exc:
            self._inst = None
            raise InstrumentError(f"เชื่อมต่อไม่สำเร็จ: {exc}") from exc

    def disconnect(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            finally:
                self._inst = None
                self.resource_name = None
                self.idn = None

    # SCPI helper 
    def _require_connection(self):
        if not self.is_connected:
            raise InstrumentError("ยังไม่ได้เชื่อมต่อกับเครื่องมือ")

    def write(self, command: str) -> None:
        """ส่งคำสั่ง SCPI ที่ไม่ต้องรอผลลัพธ์ตอบกลับ"""
        self._require_connection()
        try:
            self._inst.write(command)
        except Exception as exc:
            raise InstrumentError(f"ส่งคำสั่งล้มเหลว ({command}): {exc}") from exc

    def query(self, command: str) -> str:
        """ส่งคำสั่ง SCPI ที่ต้องรอผลลัพธ์ตอบกลับ (ลงท้ายด้วย ?)"""
        self._require_connection()
        try:
            return self._inst.query(command).strip()
        except Exception as exc:
            raise InstrumentError(f"สอบถามล้มเหลว ({command}): {exc}") from exc

    def send(self, command: str) -> str:
        
        command = command.strip()
        if not command:
            return ""
        if command.endswith("?"):
            return self.query(command)
        self.write(command)
        return ""

    # Oscilloscope Control Methods  
    def capture_screenshot_png(self) -> bytes:
        """ดึงภาพหน้าจอ (IEEE-488.2 binary block): '#'+เลขหลัก N+ความยาว N หลัก+ข้อมูล"""
        self._require_connection()
        original_timeout = self._inst.timeout
        try:
            self._inst.timeout = self.CAPTURE_TIMEOUT_MS
            self._inst.write(":DISP:DATA? ON,PNG")

            header = self._inst.read_bytes(2)
            if header[0:1] != b"#":
                raise InstrumentError(f"Header ไม่ถูกต้อง: {header!r}")
            num_digits = int(header[1:2])
            if num_digits == 0:
                raise InstrumentError("Indefinite length block ไม่รองรับ")

            length = int(self._inst.read_bytes(num_digits).decode())
            image_data = self._inst.read_bytes(length)
            try:
                self._inst.read_bytes(1)  
            except Exception:
                pass

            if not (image_data.startswith(b"\x89PNG") or image_data.startswith(b"BM")):
                raise InstrumentError(f"Unknown image format: {image_data[:16]!r}")
            return image_data
        except InstrumentError:
            raise
        except Exception as exc:
            raise InstrumentError(f"ดึงภาพหน้าจอไม่สำเร็จ: {exc}") from exc
        finally:
            self._inst.timeout = original_timeout
            
    # Channel Status Control
    def set_channel_status(self, channel: int, enable: bool) -> None:
        status_str = "ON" if enable else "OFF"
        self.write(f":CHANnel{channel}:DISPlay {status_str}")

    def get_channel_status(self, channel: int) -> bool:
        res = self.query(f":CHANnel{channel}:DISPlay?")
        return res == "1"
    
    # ---------- Volt/Div Control function ----------
    def set_volt_scale(self, channel: int, scale_val: float) -> None:
        """ตั้งค่า Volt/Div ของ Channel ที่เลือก (หน่วยเป็น Volt เช่น 1.0 = 1V, 0.05 = 50mV)"""
        self.write(f":CHANnel{channel}:SCALe {scale_val}")

    def get_volt_scale(self, channel: int) -> float:
        """ดึงค่า Volt/Div ของ Channel ที่เลือก"""
        res = self.query(f":CHANnel{channel}:SCALe?")
        return float(res)

    # ---------- Time/Div Control function  ----------
    def set_time_scale(self, scale_val: float) -> None:
        """ตั้งค่า Time/Div ของเครื่อง (หน่วยเป็นวินาที เช่น 0.001 = 1ms)"""
        self.write(f":TIMebase:MAIN:SCALe {scale_val}")

    def get_time_scale(self) -> float:
        """ดึงค่า Time/Div ปัจจุบันจากเครื่อง"""
        res = self.query(f":TIMebase:MAIN:SCALe?")
        return float(res)
            

class SimulatedInstrument(RigolInstrument):
    
    def __init__(self):
        super().__init__()
        self._sim_channels = {1: True, 2: False, 3: False, 4: False}
        
        self._sim_volt_scales = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}
        self._sim_time_scale = 0.0005  # 500 us

    def list_resources(self) -> List[str]:
        return ["SIM::MSO1104::INSTR"]

    def connect(self, resource_name: str, timeout_ms: Optional[int] = None) -> str:
        time.sleep(0.3)
        self.resource_name = resource_name
        self.idn = "RIGOL TECHNOLOGIES,MSO1104,SIMULATED,00.01.03 (Simulation Mode)"
        self._inst = "SIMULATED"
        return self.idn

    def disconnect(self) -> None:
        self._inst = None
        self.resource_name = None
        self.idn = None

    def write(self, command: str) -> None:
        self._require_connection()

    def query(self, command: str) -> str:
        self._require_connection()
        if command == self.IDN_QUERY:
            return self.idn
        return f"<simulated response for '{command}'>"

    def capture_screenshot_png(self) -> bytes:
        self._require_connection()
        if not PIL_AVAILABLE:
            raise InstrumentError("ต้องติดตั้ง Pillow เพื่อสร้างภาพจำลอง (pip install pillow)")
        img = Image.new("RGB", (400, 240), color=(20, 20, 30))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    
    
    def set_channel_status(self, channel: int, enable: bool) -> None:
        self._require_connection()
        self._sim_channels[channel] = enable

    def get_channel_status(self, channel: int) -> bool:
        self._require_connection()
        return self._sim_channels.get(channel, False)
    
    def set_volt_scale(self, channel: int, scale_val: float) -> None:
        self._require_connection()
        self._sim_volt_scales[channel] = scale_val

    def get_volt_scale(self, channel: int) -> float:
        self._require_connection()
        return self._sim_volt_scales.get(channel, 1.0)

    def set_time_scale(self, scale_val: float) -> None:
        self._require_connection()
        self._sim_time_scale = scale_val

    def get_time_scale(self) -> float:
        self._require_connection()
        return self._sim_time_scale


# VIEW LAYER (UI Layout & Custom Tkinter Frames)

class ConnectionFrame(ttk.LabelFrame):
    """ส่วนเชื่อมต่อเครื่องมือ: เลือก resource, ปุ่ม Connect/Disconnect"""

    def __init__(self, parent, on_refresh, on_connect, on_disconnect):
        super().__init__(parent, text="Instrument Connection", padding=10)
        self.on_refresh = on_refresh
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self.resource_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Status: Disconnected")

        ttk.Label(self, text="VISA Resource:").grid(row=0, column=0, sticky="w")
        self.resource_combo = ttk.Combobox(self, textvariable=self.resource_var, width=40)
        self.resource_combo.grid(row=0, column=1, padx=5, sticky="we")

        ttk.Button(self, text="Refresh", command=self._refresh).grid(row=0, column=2, padx=3)
        self.connect_btn = ttk.Button(self, text="Connect", command=self._connect)
        self.connect_btn.grid(row=0, column=3, padx=3)
        self.disconnect_btn = ttk.Button( self, text="Disconnect", command=self._disconnect, state="disabled" )
        self.disconnect_btn.grid(row=0, column=4, padx=3)

        ttk.Label(self, textvariable=self.status_var, foreground="#a00").grid( row=1, column=0, columnspan=5, sticky="w", pady=(8, 0) )

        self.columnconfigure(1, weight=1)

    def _refresh(self):
        resources = self.on_refresh()
        self.resource_combo["values"] = resources
        if resources and not self.resource_var.get():
            self.resource_var.set(resources[0])

    def _connect(self):
        self.on_connect(self.resource_var.get())

    def _disconnect(self):
        self.on_disconnect()

    def set_connected_state(self, connected: bool, idn: str = ""):
        if connected:
            self.status_var.set(f"Status: Connected -> {idn}")
            self.connect_btn.config(state="disabled")
            self.disconnect_btn.config(state="normal")
            self.resource_combo.config(state="disabled")
        else:
            self.status_var.set("Status: Disconnected")
            self.connect_btn.config(state="normal")
            self.disconnect_btn.config(state="disabled")
            self.resource_combo.config(state="normal")


class CommandFrame(ttk.LabelFrame):
    """ส่วนส่งคำสั่ง SCPI แบบอิสระ"""

    def __init__(self, parent, on_send):
        super().__init__(parent, text="SCPI Command", padding=10)
        self.on_send = on_send

        self.command_var = tk.StringVar(value="*IDN?")
        entry = ttk.Entry(self, textvariable=self.command_var, width=50)
        entry.grid(row=0, column=0, padx=5, sticky="we")
        entry.bind("<Return>", lambda e: self._send())

        ttk.Button(self, text="Send", command=self._send).grid(row=0, column=1, padx=5)

        # ปุ่มลัดสำหรับคำสั่งที่ใช้บ่อย
        shortcuts = ttk.Frame(self)
        shortcuts.grid(row=1, column=0, columnspan=2, pady=(8, 0), sticky="w")
        for label, cmd in [
            ("*IDN?", "*IDN?"),
            ("Run", ":RUN"),
            ("Stop", ":STOP"),
            ("Auto Scale", ":AUToscale"),
            ("Single", ":SINGle"),
        ]:
            ttk.Button(
                shortcuts, text=label, width=10,
                command=lambda c=cmd: self._send(c)
            ).pack(side="left", padx=2)

        self.columnconfigure(0, weight=1)

    def _send(self, forced_command: Optional[str] = None):
        command = forced_command if forced_command is not None else self.command_var.get()
        self.on_send(command)
        
# Frame สำหรับควบคุม Channel ทั้ง 4
class ChannelControlFrame(ttk.LabelFrame):

    def __init__(self, parent, on_toggle):
        super().__init__(parent, text="Channel View Control", padding=10)
        self.on_toggle = on_toggle

        self.ch_colors = {
            1: "#D4AF37", # Gold/Yellow
            2: "#00A8E8", # Sky Blue
            3: "#DE3163", # Pink/Magenta
            4: "#2E8B57"  # Sea Green
        }
        
        self.check_vars = {}
        self.check_buttons = {}

        # สร้างปุ่มเปิด-ปิด 4 ช่องสัญญาณ
        for ch in range(1, 5):
            ch_frame = ttk.Frame(self)
            ch_frame.pack(side="top", fill="x", anchor="w", pady=4, padx=5)

            # Label ชื่อ Channel
            lbl = tk.Label(ch_frame, text=f"CH {ch}", fg=self.ch_colors[ch], font=("Arial", 10, "bold"), width=5, anchor="w")
            lbl.pack(side="left", padx=2)

            # Checkbutton สำหรับเปิด/ปิด
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(
                ch_frame, 
                text="OFF", 
                variable=var, 
                command=lambda c=ch: self._on_cb_click(c)
            )
            cb.pack(side="left")
            
            self.check_vars[ch] = var
            self.check_buttons[ch] = cb

    def _on_cb_click(self, ch: int):
        is_on = self.check_vars[ch].get()
        self.check_buttons[ch].config(text="ON" if is_on else "OFF")
        self.on_toggle(ch, is_on)

    def update_ui_state(self, ch: int, is_on: bool):
        """ยอมให้ Controller สั่งอัปเดตสถานะปุ่มจากภายนอกได้"""
        self.check_vars[ch].set(is_on)
        self.check_buttons[ch].config(text="ON" if is_on else "OFF")
        

# Frame สำหรับควบคุม Volt/Div และ Time/Div
class ScaleControlFrame(ttk.LabelFrame):
    """หน้าต่างสำหรับตั้งค่าและควบคุม Volt/Div และ Time/Div"""

    def __init__(self, parent, on_volt_change, on_time_change):
        super().__init__(parent, text="Scale Control", padding=10)
        self.on_volt_change = on_volt_change
        self.on_time_change = on_time_change

        # ค่ามาตรฐานช่วงสัญญาณของสโคป Rigol
        self.volt_presets = [
            (0.001, "1 mV"), (0.002, "2 mV"), (0.005, "5 mV"),
            (0.01, "10 mV"), (0.02, "20 mV"), (0.05, "50 mV"),
            (0.1, "100 mV"), (0.2, "200 mV"), (0.5, "500 mV"),
            (1.0, "1 V"), (2.0, "2 V"), (5.0, "5 V"), (10.0, "10 V")
        ]
        self.time_presets = [
            (5e-9, "5 ns"), (10e-9, "10 ns"), (20e-9, "20 ns"), (50e-9, "50 ns"),
            (100e-9, "100 ns"), (200e-9, "200 ns"), (500e-9, "500 ns"),
            (1e-6, "1 us"), (2e-6, "2 us"), (5e-6, "5 us"), (10e-6, "10 us"),
            (20e-6, "20 us"), (50e-6, "50 us"), (100e-6, "100 us"), (200e-6, "200 us"),
            (500e-6, "500 us"), (1e-3, "1 ms"), (2e-3, "2 ms"), (5e-3, "5 ms"),
            (10e-3, "10 ms"), (20e-3, "20 ms"), (50e-3, "50 ms"), (100e-3, "100 ms"),
            (200e-3, "200 ms"), (500e-3, "500 ms"), (1.0, "1 s"), (2.0, "2 s"),
            (5.0, "5 s"), (10.0, "10 s"), (20.0, "20 s"), (50.0, "50 s")
        ]

        # --- ส่วนที่ 1: Vertical (Volt/Div) ---
        v_frame = ttk.LabelFrame(self, text="Vertical Scale (Volt/Div)", padding=5)
        v_frame.pack(side="top", fill="x", pady=4)

        # # --- Channel Selection ---
        ttk.Label(v_frame, text="Channel:").grid(row=0, column=0, sticky="w", padx=2, pady=2)
        self.ch_select = ttk.Combobox(v_frame, values=["CH1", "CH2", "CH3", "CH4"], width=6, state="readonly")
        self.ch_select.set("CH1")
        self.ch_select.grid(row=0, column=1, columnspan=3, sticky="ew", padx=2, pady=2)
        self.ch_select.bind("<<ComboboxSelected>>", self._on_channel_select)

        # Vertical Scale adjustment controls
        ttk.Label(v_frame, text="Scale:").grid(row=1, column=0, sticky="w", padx=2, pady=2)
        self.btn_volt_dec = ttk.Button(v_frame, text="-", width=3, command=lambda: self._step("volt", -1))
        self.btn_volt_dec.grid(row=1, column=1, padx=2, pady=2)
        
        self.volt_var = tk.StringVar()
        self.volt_combo = ttk.Combobox(v_frame, textvariable=self.volt_var, 
                                       values=[item[1] for item in self.volt_presets], 
                                       width=8, state="readonly")
        self.volt_combo.grid(row=1, column=2, padx=2, pady=2)
        self.volt_combo.bind("<<ComboboxSelected>>", lambda e: self._on_combo_select("volt"))
        
        self.btn_volt_inc = ttk.Button(v_frame, text="+", width=3, command=lambda: self._step("volt", 1))
        self.btn_volt_inc.grid(row=1, column=3, padx=2, pady=2)

        # --- ส่วนที่ 2: Horizontal (Time/Div) ---
        h_frame = ttk.LabelFrame(self, text="Horizontal Scale (Time/Div)", padding=5)
        h_frame.pack(side="top", fill="x", pady=4)

        # Horizontal Scale adjustment controls
        ttk.Label(h_frame, text="Scale:").grid(row=0, column=0, sticky="w", padx=2, pady=2)
        self.btn_time_dec = ttk.Button(h_frame, text="-", width=3, command=lambda: self._step("time", -1))
        self.btn_time_dec.grid(row=0, column=1, padx=2, pady=2)

        self.time_var = tk.StringVar()
        self.time_combo = ttk.Combobox(h_frame, textvariable=self.time_var, 
                                       values=[item[1] for item in self.time_presets], 
                                       width=8, state="readonly")
        self.time_combo.grid(row=0, column=2, padx=2, pady=2)
        self.time_combo.bind("<<ComboboxSelected>>", lambda e: self._on_combo_select("time"))

        self.btn_time_inc = ttk.Button(h_frame, text="+", width=3, command=lambda: self._step("time", 1))
        self.btn_time_inc.grid(row=0, column=3, padx=2, pady=2)

        self._axes = {
            "volt": {"presets": self.volt_presets, "var": self.volt_var},
            "time": {"presets": self.time_presets, "var": self.time_var},
        }

    def _get_closest_index(self, val: float, presets: list) -> int:
        return min(range(len(presets)), key=lambda i: abs(presets[i][0] - val))

    def set_volt_ui(self, scale_val: float):
        idx = self._get_closest_index(scale_val, self.volt_presets)
        self.volt_var.set(self.volt_presets[idx][1])

    def set_time_ui(self, scale_val: float):
        idx = self._get_closest_index(scale_val, self.time_presets)
        self.time_var.set(self.time_presets[idx][1])

    def _on_channel_select(self, event=None):
        ch = int(self.ch_select.get()[-1])
        self.on_volt_change(ch, None, is_channel_switch=True)

    def _on_combo_select(self, axis: str):
        """axis = 'volt' หรือ 'time' — ใช้แทน _on_volt_combo_select/_on_time_combo_select เดิม"""
        presets, var = self._axes[axis]["presets"], self._axes[axis]["var"]
        val = next(v for v, s in presets if s == var.get())
        if axis == "volt":
            ch = int(self.ch_select.get()[-1])
            self.on_volt_change(ch, val)
        else:
            self.on_time_change(val)

    def _step(self, axis: str, step: int):
        """axis = 'volt' หรือ 'time' — ใช้แทน _step_volt/_step_time เดิม"""
        presets, var = self._axes[axis]["presets"], self._axes[axis]["var"]
        try:
            current_idx = next(i for i, (v, s) in enumerate(presets) if s == var.get())
        except StopIteration:
            return
        new_idx = max(0, min(len(presets) - 1, current_idx + step))
        new_val, new_str = presets[new_idx]
        var.set(new_str)
        if axis == "volt":
            ch = int(self.ch_select.get()[-1])
            self.on_volt_change(ch, new_val)
        else:
            self.on_time_change(new_val)


# จับภาพหน้าจอออสซิลโลสโคปและแสดงผล
class CaptureFrame(ttk.LabelFrame):

    def __init__(self, parent, on_capture, on_save):
        super().__init__(parent, text="Oscilloscope Screen", padding=10)
        self.on_capture = on_capture
        self.on_save = on_save
        self._current_image = None  

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x")
        self.capture_btn = ttk.Button(btn_row, text="Capture", command=self._capture)
        self.capture_btn.pack(side="left")
        self.save_btn = ttk.Button(
            btn_row, text="Save Image...", command=self._save, state="disabled"
        )
        self.save_btn.pack(side="left", padx=5)

        self.image_container = tk.Frame(self, width=450, height=315, bg="#202020")
        self.image_container.pack_propagate(False)  
        self.image_container.pack(pady=10, anchor="w")

        self.image_label = tk.Label(
            self.image_container, bg="#202020", fg="white", text="No image"
        )
        self.image_label.pack(fill="both", expand=True)

    def _capture(self):
        self.on_capture()

    def _save(self):
        self.on_save()

    def set_capturing_state(self, capturing: bool):
        if capturing:
            self.capture_btn.config(state="disabled", text="Capturing...")
            self.image_label.config(text="กำลังดึงภาพจากเครื่อง กรุณารอสักครู่...", image="")    
        else:
            self.capture_btn.config(state="normal", text="Capture")

    def show_image(self, png_bytes: bytes):
        if not PIL_AVAILABLE:
            self.image_label.config(text="[Pillow ไม่ได้ติดตั้ง จึงแสดงภาพไม่ได้]", image="")
            return
        
        img = Image.open(io.BytesIO(png_bytes))
        
        try:
            img = img.resize((450, 315), Image.Resampling.LANCZOS)
        except AttributeError:
            img = img.resize((450, 315), Image.ANTIALIAS)
            
        self._current_image = ImageTk.PhotoImage(img)
        self.image_label.config(image=self._current_image, text="")
        self.save_btn.config(state="normal")

class LogFrame(ttk.LabelFrame):
    """ส่วนแสดง log / response จากเครื่องมือ"""

    def __init__(self, parent):
        super().__init__(parent, text="Response Log", padding=10)
        self.response_box = scrolledtext.ScrolledText(self, width=70, height=1)
        self.response_box.pack(fill="both", expand=True)

    def log(self, message: str, tag: str = "info"):
        timestamp = time.strftime("%H:%M:%S")
        prefix = {"info": "", "error": "[ERROR] ", "cmd": ">>> "}.get(tag, "")
        self.response_box.insert(tk.END, f"[{timestamp}] {prefix}{message}\n")
        self.response_box.see(tk.END)


# CONTROLLER LAYER 
class OscilloscopeApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Rigol MSO1104 Oscilloscope GUI")
        self.geometry("1024x768")
        self.minsize(880, 700)

        self.instrument: RigolInstrument = (
            RigolInstrument() if PYVISA_AVAILABLE else SimulatedInstrument()
        )
        self._last_capture: Optional[bytes] = None

        self._build_layout()

        if not PYVISA_AVAILABLE:
            self.log_frame.log(
                "ไม่พบไลบรารี pyvisa จึงเริ่มโปรแกรมใน Simulation Mode "
                "(pip install pyvisa pyvisa-py เพื่อใช้งานกับเครื่องจริง)",
                tag="error",
            )

    def _build_layout(self):
        self.connection_frame = ConnectionFrame(
            self,
            on_refresh=self._handle_refresh,
            on_connect=self._handle_connect,
            on_disconnect=self._handle_disconnect,
        )
        self.connection_frame.pack(fill="x", padx=10, pady=(10, 5))

        self.command_frame = CommandFrame(self, on_send=self._handle_send)
        self.command_frame.pack(fill="x", padx=10, pady=5)

        # กล่อง Capture / Channel / Scale 
        middle_container = ttk.Frame(self)
        middle_container.pack(fill="x", padx=10, pady=5)

        self.capture_frame = CaptureFrame(
            middle_container, on_capture=self._handle_capture, on_save=self._handle_save_image
        )
        self.capture_frame.pack(side="left", fill="y", padx=(0, 10))

        self.channel_frame = ChannelControlFrame(
            middle_container, on_toggle=self._handle_channel_toggle
        )
        self.channel_frame.pack(side="left", fill="y", padx=(0, 10))

        self.scale_frame = ScaleControlFrame(
            middle_container, 
            on_volt_change=self._handle_volt_change, 
            on_time_change=self._handle_time_change
        )
        self.scale_frame.pack(side="left", fill="y")

        self.log_frame = LogFrame(self)
        self.log_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))


    # Event handlers: Connection
    def _handle_refresh(self) -> List[str]:
        try:
            resources = self.instrument.list_resources()
            self.log_frame.log(f"พบ resource ทั้งหมด {len(resources)} รายการ")
            return resources
        except InstrumentError as exc:
            self.log_frame.log(str(exc), tag="error")
            return []

    def _handle_connect(self, resource_name: str):
        if not resource_name:
            messagebox.showwarning("ไม่ได้เลือก Resource", "กรุณาเลือกหรือพิมพ์ VISA resource ก่อน")
            return
        try:
            idn = self.instrument.connect(resource_name)
            self.connection_frame.set_connected_state(True, idn)
            self.log_frame.log(f"เชื่อมต่อสำเร็จ: {idn}")
            
            # ดึงสถานะ Channel 1-4 มาแสดง
            for ch in range(1, 5):
                try:
                    is_on = self.instrument.get_channel_status(ch)
                    self.channel_frame.update_ui_state(ch, is_on)
                except Exception:
                    pass

            # ดึงค่า Volt/Div และ Time/Div ปัจจุบันจากเครื่องมาแสดง
            try:
                ch = int(self.scale_frame.ch_select.get()[-1])
                self.scale_frame.set_volt_ui(self.instrument.get_volt_scale(ch))
                self.scale_frame.set_time_ui(self.instrument.get_time_scale())
            except Exception as exc:
                self.log_frame.log(f"ดึงค่าสเกลเริ่มต้นล้มเหลว: {exc}", tag="error")

        except InstrumentError as exc:
            self.connection_frame.set_connected_state(False)
            self.log_frame.log(str(exc), tag="error")
            messagebox.showerror("เชื่อมต่อไม่สำเร็จ", str(exc))
            
            
    def _handle_disconnect(self):
        self.instrument.disconnect()
        self.connection_frame.set_connected_state(False)
        self.log_frame.log("ตัดการเชื่อมต่อแล้ว")

    # Event handlers: SCPI command
    def _handle_send(self, command: str):
        if not self.instrument.is_connected:
            messagebox.showwarning("ยังไม่ได้เชื่อมต่อ", "กรุณาเชื่อมต่อเครื่องมือก่อนส่งคำสั่ง")
            return
        self.log_frame.log(command, tag="cmd")
        try:
            result = self.instrument.send(command)
            if result:
                self.log_frame.log(result)
        except InstrumentError as exc:
            self.log_frame.log(str(exc), tag="error")

    # Event handlers: Capture
    def _handle_capture(self):
        if not self.instrument.is_connected:
            messagebox.showwarning("ยังไม่ได้เชื่อมต่อ", "กรุณาเชื่อมต่อเครื่องมือก่อนจับภาพหน้าจอ")
            return

        self.log_frame.log("กำลังจับภาพหน้าจอ... (อาจใช้เวลาสักครู่ กรุณารอ)")
        self.capture_frame.set_capturing_state(True)

        def worker():
            try:
                png_bytes = self.instrument.capture_screenshot_png()
                self.after(0, lambda: self._on_capture_success(png_bytes))
            except InstrumentError as exc:
                self.after(0, lambda: self._on_capture_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_capture_success(self, png_bytes: bytes):
        self._last_capture = png_bytes
        self.capture_frame.set_capturing_state(False)
        self.capture_frame.show_image(png_bytes)
        self.log_frame.log(f"จับภาพสำเร็จ ({len(png_bytes):,} bytes)")

    def _on_capture_error(self, exc: InstrumentError):
        self.capture_frame.set_capturing_state(False)
        self.log_frame.log(str(exc), tag="error")
        messagebox.showerror("จับภาพไม่สำเร็จ", str(exc))

    def _handle_save_image(self):
        if not self._last_capture:
            return

        # ตรวจสอบชนิดของไฟล์
        if self._last_capture.startswith(b"\x89PNG"):
            ext = ".png"
            filetypes = [("PNG Image", "*.png")]

        elif self._last_capture.startswith(b"BM"):
            ext = ".bmp"
            filetypes = [("Bitmap Image", "*.bmp")]

        else:
            ext = ".bin"
            filetypes = [("Binary File", "*.bin")]

        # ให้ผู้ใช้เลือกตำแหน่งบันทึก
        path = filedialog.asksaveasfilename(
            defaultextension=ext,
            filetypes=filetypes,
            initialfile=f"oscilloscope_capture{ext}"
        )

        if not path:
            return

        # บันทึกไฟล์
        with open(path, "wb") as f:
            f.write(self._last_capture)

        self.log_frame.log(f"บันทึกภาพไปที่: {path}")
        
    def on_close(self):
        try:
            if self.instrument.is_connected: 
                self.instrument.disconnect()
        except Exception:
            pass
        self.destroy()
        
    # Event handler: คุม Channel
    def _handle_channel_toggle(self, channel: int, is_on: bool):
        if not self.instrument.is_connected:
            messagebox.showwarning("ยังไม่ได้เชื่อมต่อ", "กรุณาเชื่อมต่อเครื่องมือก่อนตั้งค่า Channel")
            self.channel_frame.update_ui_state(channel, not is_on) 
            return

        action = "Enable" if is_on else "Disable"
        self.log_frame.log(f"Channel {channel} {action} ", tag="cmd")
        
        try:
            self.instrument.set_channel_status(channel, is_on)
        except InstrumentError as exc:
            self.log_frame.log(str(exc), tag="error")
            messagebox.showerror("ตั้งค่าผิดพลาด", str(exc))
            self.channel_frame.update_ui_state(channel, not is_on) 

    # Event handler: คุมขนาดสเกล Volt / Time
    def _handle_volt_change(self, channel: int, value: Optional[float], is_channel_switch: bool = False):
        if not self.instrument.is_connected:
            messagebox.showwarning("ยังไม่ได้เชื่อมต่อ", "กรุณาเชื่อมต่อเครื่องมือก่อนตั้งค่า Scale")
            return

        if is_channel_switch:
            try:
                volt_val = self.instrument.get_volt_scale(channel)
                self.scale_frame.set_volt_ui(volt_val)
            except InstrumentError as exc:
                self.log_frame.log(str(exc), tag="error")
            return

        if value is not None:
            try:
                self.instrument.set_volt_scale(channel, value)
                self.log_frame.log(f"ตั้งค่า Channel {channel} Scale ไปที่ {value} V/div", tag="cmd")
            except InstrumentError as exc:
                self.log_frame.log(str(exc), tag="error")
                messagebox.showerror("ตั้งค่าผิดพลาด", str(exc))

    def _handle_time_change(self, value: float):
        if not self.instrument.is_connected:
            messagebox.showwarning("ยังไม่ได้เชื่อมต่อ", "กรุณาเชื่อมต่อเครื่องมือก่อนตั้งค่า Scale")
            return

        try:
            self.instrument.set_time_scale(value)
            self.log_frame.log(f"ตั้งค่า Timebase Scale ไปที่ {value} s/div", tag="cmd")
        except InstrumentError as exc:
            self.log_frame.log(str(exc), tag="error")
            messagebox.showerror("ตั้งค่าผิดพลาด", str(exc))


if __name__ == "__main__":
    app = OscilloscopeApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
