"""ogb_restim_bridge_gui.py — OscGoesBrrr → ReStim T-Code bridge with GUI.

Install:  pip install python-osc aiohttp
Run:      python ogb_restim_bridge_gui.py
"""

import asyncio
import json
import math
import queue
import threading
import tkinter as tk
from tkinter import ttk, simpledialog
from dataclasses import dataclass, field, asdict, fields as dc_fields
from pathlib import Path
from typing import Optional

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer
import aiohttp


# ──────────────────────────────────────────────────────────────────────────────
# Config dataclasses
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "ogb_restim_config.json"

DEFAULT_ADDRESSES = [
    "/avatar/parameters/OGB/Orf/Fleshlight/PenSelfNewRoot",
    "/avatar/parameters/OGB/Pen/Shaft/PenSelf",
    "/avatar/parameters/OGB/Pen/Shaft/TouchSelf",
]


@dataclass
class AddressCfg:
    address: str
    enabled: bool = True
    use_global: bool = True
    l0: bool = True
    l1: bool = True
    l2: bool = True
    pulse: bool = False


@dataclass
class AppConfig:
    restim_url: str = "ws://localhost:12346"
    osc_port: int = 9001
    idle_timeout: float = 0.5
    dead_zone: float = 0.02
    gamma: float = 0.5
    # Global output modes
    l0: bool = True
    l1: bool = True
    l2: bool = True
    pulse: bool = False
    # L1 beta
    beta_thresh: float = 0.35
    beta_light: int = 8099
    beta_active: int = 5000
    beta_off: int = 9999
    # L2 alpha oscillation
    alpha_min_hz: float = 0.3
    alpha_max_hz: float = 1.5
    alpha_min_amp: float = 0.20
    alpha_max_amp: float = 0.45
    # Pulse
    pulse_min_hz: float = 0.5
    pulse_max_hz: float = 3.0
    pulse_min_depth: float = 0.3
    pulse_max_depth: float = 1.0
    # T-Code axis names (must match ReStim Preferences → Funscript/T-Code)
    axis_volume: str = "L0"
    axis_beta:   str = "L1"
    axis_alpha:  str = "L2"
    # Output floor: min T-Code value sent when intensity > 0 (0 = off, e.g. 1000 = 10%)
    tcode_floor: int = 0
    # Loop tick for alpha/pulse generators (ms)
    send_interval_ms: int = 50
    # Addresses managed separately
    addresses: list = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "AppConfig":
        raw = d.pop("addresses", [])
        valid = {f.name for f in dc_fields(cls)}
        cfg = cls(**{k: v for k, v in d.items() if k in valid})
        valid_a = {f.name for f in dc_fields(AddressCfg)}
        cfg.addresses = []
        for a in raw:
            try:
                cfg.addresses.append(AddressCfg(**{k: v for k, v in a.items() if k in valid_a}))
            except Exception:
                pass
        return cfg

    def save(self):
        try:
            CONFIG_FILE.write_text(json.dumps(self.to_json(), indent=2))
        except Exception:
            pass

    @classmethod
    def load(cls) -> "AppConfig":
        if CONFIG_FILE.exists():
            try:
                return cls.from_json(json.loads(CONFIG_FILE.read_text()))
            except Exception:
                pass
        cfg = cls()
        cfg.addresses = [AddressCfg(a) for a in DEFAULT_ADDRESSES]
        cfg.save()   # write defaults immediately so a crash doesn't lose first-run state
        return cfg


# ──────────────────────────────────────────────────────────────────────────────
# T-Code helpers
# ──────────────────────────────────────────────────────────────────────────────

def _tv(v: float) -> str:
    """float 0..1 → 4-digit T-Code string."""
    return str(int(max(0.0, min(1.0, v)) * 9999)).zfill(4)


def _tv_floor(v: float, floor_val: int) -> str:
    """float 0..1 → 4-digit T-Code string with minimum floor when v > 0."""
    if v <= 0.0:
        return "0000"
    return str(max(floor_val, min(9999, int(v * 9999)))).zfill(4)


def _curve(raw: float, dz: float, gamma: float) -> float:
    """Dead zone + gamma curve → effective intensity 0..1."""
    if raw < dz:
        return 0.0
    return ((raw - dz) / (1.0 - dz)) ** gamma


# ──────────────────────────────────────────────────────────────────────────────
# Bridge engine  (runs in a background asyncio thread)
# ──────────────────────────────────────────────────────────────────────────────

class BridgeEngine:
    def __init__(self, cfg: AppConfig, shared: dict, log_q: queue.Queue):
        self._cfg = cfg
        self._shared = shared      # {addr: float}  +  {"__disc__/avatar/...": float}
        self._log_q = log_q
        self._ws = None
        self._session = None
        self._current_beta = cfg.beta_off
        self._alpha_phase = 0.0
        self._alpha_parked = True
        self._pulse_phase = 0.0
        # Per-mode effective intensities, recomputed on every OSC hit
        self._mode_eff: dict[str, float] = {"l0": 0.0, "l1": 0.0, "l2": 0.0, "pulse": 0.0}
        self._last_osc = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_ev: Optional[asyncio.Event] = None

    # ── Logging / connection ─────────────────────────────────────────────────

    def _log(self, msg: str):
        self._log_q.put_nowait(msg)

    async def _connect(self) -> bool:
        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(self._cfg.restim_url, heartbeat=30)
            self._log(f"Connected → {self._cfg.restim_url}")
            return True
        except Exception as e:
            self._log(f"Connect failed: {e}")
            return False

    async def _send(self, cmd: str):
        if self._ws is None or self._ws.closed:
            await self._connect()
            if self._ws is None:
                return
        try:
            await self._ws.send_str(cmd)
        except Exception as e:
            self._log(f"Send error: {e}")
            self._ws = None

    # ── Intensity computation ────────────────────────────────────────────────

    def _calc_mode_eff(self) -> dict[str, float]:
        """Max effective intensity per output mode across all enabled addresses."""
        cfg = self._cfg
        out = {"l0": 0.0, "l1": 0.0, "l2": 0.0, "pulse": 0.0}
        for ac in cfg.addresses:
            if not ac.enabled:
                continue
            eff = _curve(self._shared.get(ac.address, 0.0), cfg.dead_zone, cfg.gamma)
            if eff == 0.0:
                continue
            modes = (
                {"l0": ac.l0, "l1": ac.l1, "l2": ac.l2, "pulse": ac.pulse}
                if not ac.use_global else
                {"l0": cfg.l0, "l1": cfg.l1, "l2": cfg.l2, "pulse": cfg.pulse}
            )
            for m, active in modes.items():
                if active:
                    out[m] = max(out[m], eff)
        return out

    # ── OSC callbacks ────────────────────────────────────────────────────────

    def osc_cb(self, address: str, *args):
        if not args:
            return
        self._shared[address] = float(args[0])
        self._last_osc = self._loop.time()
        self._mode_eff = self._calc_mode_eff()
        asyncio.run_coroutine_threadsafe(self._update(), self._loop)

    def default_cb(self, address: str, *args):
        """Catch all unregistered OSC — used for address discovery."""
        if args and isinstance(args[0], float):
            disc_key = f"__disc__{address}"
            if disc_key not in self._shared:
                self._shared[disc_key] = float(args[0])
                self._log(f"Discovered: {address}")

    # ── Output update ────────────────────────────────────────────────────────

    async def _update(self):
        cfg, me = self._cfg, self._mode_eff
        parts = []

        # Volume axis — skipped when pulse loop is driving it
        if not cfg.pulse:
            v0 = _tv_floor(me["l0"], cfg.tcode_floor)
            parts.append(f"{cfg.axis_volume}{v0}I100")
            self._shared["__live__l0"] = me["l0"]

        # Beta axis tier
        eff_l1 = me["l1"]
        desired = cfg.beta_off
        if eff_l1 > 0.0:
            desired = cfg.beta_active if eff_l1 >= cfg.beta_thresh else cfg.beta_light
        if desired != self._current_beta:
            t = 500 if desired == cfg.beta_off else 200
            parts.append(f"{cfg.axis_beta}{desired:04d}I{t}")
            self._current_beta = desired
        self._shared["__live__l1"] = self._current_beta / 9999.0

        if parts:
            await self._send(" ".join(parts))

    # ── Background loops ─────────────────────────────────────────────────────

    async def _alpha_loop(self):
        while not self._stop_ev.is_set():
            cfg = self._cfg
            dt = cfg.send_interval_ms / 1000.0
            eff = self._mode_eff["l2"] if cfg.l2 else 0.0
            if eff < 0.01:
                if not self._alpha_parked:
                    await self._send(f"{cfg.axis_alpha}{_tv(0.5)}I500")
                    self._alpha_parked = True
                self._alpha_phase = 0.0
                self._shared["__live__l2"] = 0.0
            else:
                self._alpha_parked = False
                hz  = cfg.alpha_min_hz  + (cfg.alpha_max_hz  - cfg.alpha_min_hz)  * eff
                amp = cfg.alpha_min_amp + (cfg.alpha_max_amp - cfg.alpha_min_amp) * eff
                pos = 0.5 + amp * math.sin(2 * math.pi * self._alpha_phase)
                self._alpha_phase = (self._alpha_phase + hz * dt) % 1.0
                await self._send(f"{cfg.axis_alpha}{_tv(pos)}I{int(dt * 1000)}")
                self._shared["__live__l2"] = eff
            await asyncio.sleep(dt)

    async def _pulse_loop(self):
        """Drives volume axis with a triangle-wave pulse when pulse mode is active."""
        while not self._stop_ev.is_set():
            cfg = self._cfg
            dt = cfg.send_interval_ms / 1000.0
            eff = self._mode_eff["pulse"] if cfg.pulse else 0.0
            if eff > 0.01:
                hz    = cfg.pulse_min_hz    + (cfg.pulse_max_hz    - cfg.pulse_min_hz)    * eff
                depth = cfg.pulse_min_depth + (cfg.pulse_max_depth - cfg.pulse_min_depth) * eff
                wave  = 1.0 - abs(2.0 * (self._pulse_phase % 1.0) - 1.0)  # triangle 0→1→0
                val   = eff * ((1.0 - depth) + depth * wave)
                tv    = _tv_floor(val, cfg.tcode_floor)
                await self._send(f"{cfg.axis_volume}{tv}I{int(dt * 1000)}")
                self._shared["__live__l0"] = val
                self._pulse_phase = (self._pulse_phase + hz * dt) % 1.0
            else:
                if self._pulse_phase != 0.0:
                    self._pulse_phase = 0.0
            await asyncio.sleep(dt)

    async def _watchdog(self):
        """Park everything if no OSC received for idle_timeout seconds."""
        while not self._stop_ev.is_set():
            await asyncio.sleep(0.1)
            if self._current_beta != self._cfg.beta_off:
                elapsed = self._loop.time() - self._last_osc
                if elapsed > self._cfg.idle_timeout:
                    self._log("Idle — parking")
                    for k in [k for k in self._shared if not k.startswith("__")]:
                        self._shared[k] = 0.0
                    self._mode_eff = {"l0": 0.0, "l1": 0.0, "l2": 0.0, "pulse": 0.0}
                    cfg = self._cfg
                    await self._send(
                        f"{cfg.axis_beta}{cfg.beta_off:04d}I500 "
                        f"{cfg.axis_volume}0000I500"
                    )
                    self._current_beta = self._cfg.beta_off
                    self._shared["__live__l0"] = 0.0
                    self._shared["__live__l1"] = cfg.beta_off / 9999.0
                    self._shared["__live__l2"] = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run_async(self):
        self._loop = asyncio.get_event_loop()
        self._stop_ev = asyncio.Event()

        if not await self._connect():
            return

        cfg = self._cfg
        await self._send(
            f"{cfg.axis_beta}{cfg.beta_off:04d}I0 "
            f"{cfg.axis_volume}0000I0 "
            f"{cfg.axis_alpha}{_tv(0.5)}I0"
        )

        dispatcher = Dispatcher()
        for ac in self._cfg.addresses:
            dispatcher.map(ac.address, self.osc_cb)
        dispatcher.set_default_handler(self.default_cb)

        server = AsyncIOOSCUDPServer(
            ("127.0.0.1", self._cfg.osc_port), dispatcher, self._loop
        )
        transport, _ = await server.create_serve_endpoint()
        n = len(self._cfg.addresses)
        self._log(f"OSC listening on port {self._cfg.osc_port} — watching {n} address(es)")

        try:
            await asyncio.gather(
                self._watchdog(),
                self._alpha_loop(),
                self._pulse_loop(),
            )
        finally:
            transport.close()
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()
            self._log("Bridge stopped.")

    def start(self):
        threading.Thread(
            target=lambda: asyncio.run(self._run_async()),
            daemon=True
        ).start()

    def stop(self):
        if self._stop_ev and self._loop:
            self._loop.call_soon_threadsafe(self._stop_ev.set)


# ──────────────────────────────────────────────────────────────────────────────
# Custom widgets
# ──────────────────────────────────────────────────────────────────────────────

class IntensityBar(tk.Canvas):
    W, H = 90, 11

    def __init__(self, parent, **kw):
        super().__init__(parent, width=self.W, height=self.H,
                         bg="#222", highlightthickness=1,
                         highlightbackground="#444", **kw)
        self._rect = self.create_rectangle(0, 0, 0, self.H, fill="#00bb44", outline="")

    def set(self, v: float):
        w = int(max(0.0, min(1.0, v)) * self.W)
        self.coords(self._rect, 0, 0, w, self.H)
        if v < 0.5:
            r, g = int(v * 2 * 220), 187
        else:
            r, g = 220, int((1.0 - (v - 0.5) * 2) * 187)
        self.itemconfig(self._rect, fill=f"#{r:02x}{g:02x}10")


# ──────────────────────────────────────────────────────────────────────────────
# Main GUI
# ──────────────────────────────────────────────────────────────────────────────

class BridgeGUI:
    def __init__(self):
        self.cfg = AppConfig.load()
        self._shared: dict = {}
        self._log_q: queue.Queue = queue.Queue()
        self._engine: Optional[BridgeEngine] = None
        self._running = False
        self._addr_row_widgets: list[dict] = []

        self.root = tk.Tk()
        self.root.title("OGB → ReStim Bridge")
        self.root.minsize(560, 540)

        try:
            ttk.Style().theme_use("clam")
        except Exception:
            pass

        self._build_ui()
        self.root.after(150, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        tab1 = ttk.Frame(nb, padding=8)
        nb.add(tab1, text="  Bridge  ")
        self._build_connection(tab1)
        self._build_live_bar(tab1)
        ttk.Separator(tab1, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)
        self._build_addresses(tab1)

        tab2 = ttk.Frame(nb, padding=8)
        nb.add(tab2, text="  Settings  ")
        self._build_settings(tab2)

        self._build_log(self.root)

    # ── Connection bar ────────────────────────────────────────────────────────

    def _build_connection(self, parent):
        f = ttk.Frame(parent)
        f.pack(fill=tk.X)

        self._dot_canvas = tk.Canvas(f, width=14, height=14,
                                     bg=self.root.cget("bg"),
                                     highlightthickness=0)
        self._dot_canvas.pack(side=tk.LEFT, padx=(0, 4))
        self._dot = self._dot_canvas.create_oval(2, 2, 12, 12,
                                                  fill="#bb3333", outline="")
        self._status_lbl = ttk.Label(f, text="Stopped", width=12)
        self._status_lbl.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(f, text="ReStim:").pack(side=tk.LEFT)
        self._url_var = tk.StringVar(value=self.cfg.restim_url)
        ttk.Entry(f, textvariable=self._url_var, width=22).pack(side=tk.LEFT, padx=4)

        ttk.Label(f, text="OSC:").pack(side=tk.LEFT, padx=(8, 0))
        self._port_var = tk.IntVar(value=self.cfg.osc_port)
        ttk.Spinbox(f, textvariable=self._port_var,
                    from_=1024, to=65535, width=6).pack(side=tk.LEFT, padx=4)

        self._start_btn = ttk.Button(f, text="Start", command=self._toggle_bridge, width=8)
        self._start_btn.pack(side=tk.RIGHT)

    # ── Live output bar ───────────────────────────────────────────────────────

    def _build_live_bar(self, parent):
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, pady=(4, 0))

        ttk.Label(f, text="Live →", font=("TkDefaultFont", 7),
                  foreground="#888").pack(side=tk.LEFT, padx=(2, 6))

        # Volume
        ttk.Label(f, text="Vol", font=("TkDefaultFont", 7),
                  foreground="#aaa").pack(side=tk.LEFT)
        self._live_vol_bar = IntensityBar(f)
        self._live_vol_bar.pack(side=tk.LEFT, padx=(2, 2))
        self._live_vol_lbl = ttk.Label(f, text=" 0%", width=5,
                                        font=("TkFixedFont", 7))
        self._live_vol_lbl.pack(side=tk.LEFT, padx=(0, 10))

        # Beta
        ttk.Label(f, text="β", font=("TkDefaultFont", 7),
                  foreground="#aaa").pack(side=tk.LEFT)
        self._live_beta_lbl = ttk.Label(f, text="5000", width=5,
                                         font=("TkFixedFont", 7))
        self._live_beta_lbl.pack(side=tk.LEFT, padx=(2, 2))
        ttk.Label(f, text="(C)", font=("TkDefaultFont", 7),
                  foreground="#888").pack(side=tk.LEFT, padx=(0, 10))
        self._live_beta_side = ttk.Label(f, text="Centre", width=7,
                                          font=("TkDefaultFont", 7))
        self._live_beta_side.pack(side=tk.LEFT, padx=(0, 10))

        # Alpha
        ttk.Label(f, text="α", font=("TkDefaultFont", 7),
                  foreground="#aaa").pack(side=tk.LEFT)
        self._live_alpha_bar = IntensityBar(f)
        self._live_alpha_bar.pack(side=tk.LEFT, padx=(2, 2))
        self._live_alpha_lbl = ttk.Label(f, text=" 0%", width=5,
                                          font=("TkFixedFont", 7))
        self._live_alpha_lbl.pack(side=tk.LEFT)

    # ── Address list ──────────────────────────────────────────────────────────

    def _build_addresses(self, parent):
        hdr = ttk.Frame(parent)
        hdr.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(hdr, text="OSC Addresses",
                  font=("TkDefaultFont", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(hdr, text="+ Add", command=self._add_address,
                   width=7).pack(side=tk.RIGHT)

        # Scrollable canvas so expanded inline panels don't overflow
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)

        self._addr_canvas = tk.Canvas(wrap, highlightthickness=0, height=200)
        _asb = ttk.Scrollbar(wrap, orient=tk.VERTICAL,
                             command=self._addr_canvas.yview)
        self._addr_canvas.configure(yscrollcommand=_asb.set)
        _asb.pack(side=tk.RIGHT, fill=tk.Y)
        self._addr_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._addr_rows_frame = ttk.Frame(self._addr_canvas)
        self._addr_win = self._addr_canvas.create_window(
            (0, 0), window=self._addr_rows_frame, anchor="nw")

        self._addr_rows_frame.bind(
            "<Configure>",
            lambda e: self._addr_canvas.configure(
                scrollregion=self._addr_canvas.bbox("all")))
        self._addr_canvas.bind(
            "<Configure>",
            lambda e: self._addr_canvas.itemconfig(
                self._addr_win, width=e.width))

        # Route mouse-wheel to the address canvas when hovered
        def _wheel_addr(e):
            self._addr_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._addr_canvas.bind("<Enter>",
            lambda e: self._addr_canvas.bind_all("<MouseWheel>", _wheel_addr))
        self._addr_canvas.bind("<Leave>",
            lambda e: self._addr_canvas.unbind_all("<MouseWheel>"))

        # Discovered section (shown dynamically below the canvas)
        self._disc_frame = ttk.LabelFrame(parent,
                                          text="Discovered — not yet active",
                                          padding=4)
        self._rebuild_addr_list()

    def _rebuild_addr_list(self):
        for w in self._addr_rows_frame.winfo_children():
            w.destroy()
        self._addr_row_widgets.clear()
        for i, ac in enumerate(self.cfg.addresses):
            self._make_addr_row(i, ac)

    def _make_addr_row(self, idx: int, ac: AddressCfg):
        """Each address gets a header row + a collapsible inline-settings panel."""
        container = ttk.Frame(self._addr_rows_frame)
        container.pack(fill=tk.X, pady=1)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ttk.Frame(container)
        hdr.pack(fill=tk.X)

        en_var = tk.BooleanVar(value=ac.enabled)
        ttk.Checkbutton(hdr, variable=en_var,
                        command=lambda: setattr(ac, "enabled", en_var.get())
                        ).pack(side=tk.LEFT)

        short = ac.address.replace("/avatar/parameters/", "…/")
        ttk.Label(hdr, text=short, width=34, anchor="w",
                  font=("TkFixedFont", 8)).pack(side=tk.LEFT, padx=(2, 4))

        bar = IntensityBar(hdr)
        bar.pack(side=tk.LEFT, padx=(0, 4))

        # Expand / collapse toggle
        _open = [False]
        _panel_ref: list[Optional[tk.Frame]] = [None]

        def _toggle(c=container, a=ac):
            if _open[0]:
                if _panel_ref[0]:
                    _panel_ref[0].pack_forget()
                expand_btn.config(text="▶")
                _open[0] = False
            else:
                _build_inline(c, a)
                expand_btn.config(text="▼")
                _open[0] = True

        expand_btn = ttk.Button(hdr, text="▶", width=2, command=_toggle)
        expand_btn.pack(side=tk.LEFT, padx=2)

        def _delete(i=idx):
            self.cfg.addresses.pop(i)
            self._rebuild_addr_list()
        ttk.Button(hdr, text="×", width=3, command=_delete).pack(side=tk.LEFT)

        # ── Inline settings panel (built once, toggled) ───────────────────────
        def _build_inline(c, a):
            # Re-show if already built
            if _panel_ref[0] is not None:
                _panel_ref[0].pack(fill=tk.X)
                return

            pnl = ttk.Frame(c, padding=(26, 2, 2, 4))
            pnl.pack(fill=tk.X)
            _panel_ref[0] = pnl

            use_g = tk.BooleanVar(value=a.use_global)
            mode_cbs: list[ttk.Checkbutton] = []

            def _refresh_state():
                st = "disabled" if a.use_global else "normal"
                for cb in mode_cbs:
                    cb.config(state=st)

            def _tog_global():
                a.use_global = use_g.get()
                _refresh_state()

            ttk.Checkbutton(pnl, text="Use global", variable=use_g,
                            command=_tog_global).pack(side=tk.LEFT, padx=(0, 8))

            ttk.Separator(pnl, orient=tk.VERTICAL).pack(
                side=tk.LEFT, fill=tk.Y, padx=(0, 6), pady=2)

            for lbl, attr in [
                ("L0 Vol",   "l0"),
                ("L1 Beta",  "l1"),
                ("L2 Alpha", "l2"),
                ("Pulse",    "pulse"),
            ]:
                v = tk.BooleanVar(value=getattr(a, attr))
                def _cb(var=v, at=attr, obj=a):
                    setattr(obj, at, var.get())
                cb = ttk.Checkbutton(pnl, text=lbl, variable=v, command=_cb)
                cb.pack(side=tk.LEFT, padx=5)
                mode_cbs.append(cb)

            _refresh_state()

        self._addr_row_widgets.append({"ac": ac, "bar": bar, "en_var": en_var})

    def _add_address(self):
        addr = simpledialog.askstring(
            "Add Address",
            "Enter OSC address:\n(e.g. /avatar/parameters/OGB/Pen/Shaft/PenSelf)\n\n"
            "Tip: start the bridge to auto-discover active OGB addresses.",
            parent=self.root,
        )
        if addr and addr.strip():
            self.cfg.addresses.append(AddressCfg(addr.strip()))
            self._rebuild_addr_list()

    # ── Settings tab ──────────────────────────────────────────────────────────

    def _build_settings(self, parent):
        self._settings_canvas = tk.Canvas(parent, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient=tk.VERTICAL,
                           command=self._settings_canvas.yview)
        self._settings_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._settings_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = ttk.Frame(self._settings_canvas)
        win = self._settings_canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",
            lambda e: self._settings_canvas.configure(
                scrollregion=self._settings_canvas.bbox("all")))
        self._settings_canvas.bind("<Configure>",
            lambda e: self._settings_canvas.itemconfig(win, width=e.width))

        def _wheel_cfg(e):
            self._settings_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._settings_canvas.bind("<Enter>",
            lambda e: self._settings_canvas.bind_all("<MouseWheel>", _wheel_cfg))
        self._settings_canvas.bind("<Leave>",
            lambda e: self._settings_canvas.unbind_all("<MouseWheel>"))

        self._populate_settings(inner)

    def _section(self, parent, row: int, text: str):
        sf = ttk.Frame(parent)
        sf.grid(row=row, column=0, columnspan=6, sticky="ew", pady=(14, 3), padx=4)
        ttk.Label(sf, text=text,
                  font=("TkDefaultFont", 9, "bold")).pack(side=tk.LEFT)
        ttk.Separator(sf, orient=tk.HORIZONTAL).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

    def _fine_slider(self, parent, row: int, col: int, label: str,
                     from_: float, to: float, cfg_attr: str,
                     fmt: str = ".2f", length: int = 95,
                     int_val: bool = False, step: float = None):
        """Slider with ‹ › fine-step buttons, bound to self.cfg.{cfg_attr}.

        Grid layout (matches old _sl):
          col   → label
          col+1 → [‹][Scale][›] sub-frame
          col+2 → value readout
        """
        if step is None:
            rng = to - from_
            step = max(1.0, round(rng / 100)) if int_val else rng / 100

        ttk.Label(parent, text=label).grid(
            row=row, column=col, sticky="w", padx=(4, 2), pady=2)

        init = getattr(self.cfg, cfg_attr)
        var = tk.DoubleVar(value=float(init))
        disp = int(init) if int_val else init
        val_lbl = ttk.Label(parent, text=f"{disp:{fmt}}", width=6)
        val_lbl.grid(row=row, column=col + 2, sticky="w")

        def _set(v):
            fv = float(v)
            val = int(round(fv)) if int_val else fv
            val = max(from_, min(to, val))
            setattr(self.cfg, cfg_attr, val)
            val_lbl.config(text=f"{val:{fmt}}")

        def _nudge(delta: float):
            nv = var.get() + delta * step
            nv = max(from_, min(to, nv))
            var.set(nv)
            _set(nv)

        sf = ttk.Frame(parent)
        sf.grid(row=row, column=col + 1, padx=1, pady=1, sticky="w")
        ttk.Button(sf, text="‹", width=2,
                   command=lambda: _nudge(-1)).pack(side=tk.LEFT)
        ttk.Scale(sf, from_=from_, to=to, variable=var,
                  command=_set, length=length).pack(side=tk.LEFT)
        ttk.Button(sf, text="›", width=2,
                   command=lambda: _nudge(1)).pack(side=tk.LEFT)

        return var

    def _chk(self, parent, row: int, col: int, label: str, cfg_attr: str):
        var = tk.BooleanVar(value=getattr(self.cfg, cfg_attr))
        def _cb():
            setattr(self.cfg, cfg_attr, var.get())
        ttk.Checkbutton(parent, text=label, variable=var,
                        command=_cb).grid(row=row, column=col,
                                          sticky="w", padx=6, pady=2)
        return var

    def _populate_settings(self, f):
        r = 0

        # ── Intensity curve ──────────────────────────────────────────────────
        self._section(f, r, "Intensity Curve"); r += 1
        self._fine_slider(f, r, 0, "Dead zone",     0.0,  0.2,  "dead_zone",
                          step=0.005); r += 1
        self._fine_slider(f, r, 0, "Gamma (curve)", 0.2,  2.0,  "gamma",
                          step=0.05)
        ttk.Label(f, text="← sqrt · linear · squared →",
                  font=("TkDefaultFont", 7), foreground="#888").grid(
            row=r, column=3, columnspan=3, sticky="w", padx=4)
        r += 1
        self._fine_slider(f, r, 0, "Min floor (T-code)", 0, 5000, "tcode_floor",
                          fmt="d", int_val=True, step=50)
        ttk.Label(f, text="min output when active  (0 = off, 1000 ≈ 10%)",
                  font=("TkDefaultFont", 7), foreground="#888").grid(
            row=r, column=3, columnspan=3, sticky="w", padx=4)
        r += 1

        # ── Global modes ─────────────────────────────────────────────────────
        self._section(f, r, "Global Output Modes"); r += 1
        mf = ttk.Frame(f)
        mf.grid(row=r, column=0, columnspan=6, sticky="w", padx=4); r += 1
        self._chk(mf, 0, 0, "L0 Volume",  "l0")
        self._chk(mf, 0, 1, "L1 Beta",    "l1")
        self._chk(mf, 0, 2, "L2 Alpha",   "l2")
        self._chk(mf, 0, 3, "Pulse (L0)", "pulse")

        # ── L1 beta ──────────────────────────────────────────────────────────
        # L1 value = left/right balance: 0 = full Left, 5000 = Centre, 9999 = full Right
        self._section(f, r, "L1 Beta  (0 = Left ◄─── 5000 = Centre ───► 9999 = Right)"); r += 1
        self._fine_slider(f, r, 0, "Switch threshold", 0.0, 1.0, "beta_thresh",
                          step=0.01); r += 1

        self._fine_slider(f, r, 0, "Light pos",  0, 9999, "beta_light",
                          fmt="d", int_val=True, step=50)
        ttk.Label(f, text="gentle touch",
                  font=("TkDefaultFont", 7), foreground="#888").grid(
            row=r, column=3, sticky="w", padx=(2, 4))

        self._fine_slider(f, r, 4, "Active pos", 0, 9999, "beta_active",
                          fmt="d", int_val=True, step=50)
        ttk.Label(f, text="full intensity",
                  font=("TkDefaultFont", 7), foreground="#888").grid(
            row=r, column=7, sticky="w", padx=(2, 4))
        r += 1

        # ── L2 alpha ─────────────────────────────────────────────────────────
        self._section(f, r, "L2 Alpha Oscillation"); r += 1
        self._fine_slider(f, r, 0, "Min Hz",  0.05, 6.0, "alpha_min_hz", step=0.05)
        self._fine_slider(f, r, 4, "Max Hz",  0.05, 6.0, "alpha_max_hz", step=0.05)
        r += 1
        self._fine_slider(f, r, 0, "Min amp", 0.0,  0.5, "alpha_min_amp", step=0.01)
        self._fine_slider(f, r, 4, "Max amp", 0.0,  0.5, "alpha_max_amp", step=0.01)
        r += 1

        # ── Pulse ────────────────────────────────────────────────────────────
        self._section(f, r, "Pulse Mode  (triangle wave on L0)"); r += 1
        self._fine_slider(f, r, 0, "Min Hz",    0.1, 10.0, "pulse_min_hz",   step=0.1)
        self._fine_slider(f, r, 4, "Max Hz",    0.1, 10.0, "pulse_max_hz",   step=0.1)
        r += 1
        self._fine_slider(f, r, 0, "Min depth", 0.0, 1.0,  "pulse_min_depth", step=0.01)
        self._fine_slider(f, r, 4, "Max depth", 0.0, 1.0,  "pulse_max_depth", step=0.01)
        r += 1

        # ── T-Code axis names ────────────────────────────────────────────────
        self._section(f, r, "T-Code Axis Names"); r += 1

        axes_info = ttk.Frame(f)
        axes_info.grid(row=r, column=0, columnspan=6, sticky="ew", padx=4, pady=(0, 4))
        ttk.Label(axes_info,
                  text="Must match ReStim  Tools → Preferences → Funscript/T-Code",
                  font=("TkDefaultFont", 7), foreground="#888").pack(anchor="w")
        r += 1

        for lbl, attr, default_hint in [
            ("Volume axis", "axis_volume", "default: L0  → set ReStim 'Volume' to this"),
            ("Beta axis",   "axis_beta",   "default: L1  → ReStim 'Beta' (already L1)"),
            ("Alpha axis",  "axis_alpha",  "default: L2  → change ReStim 'Alpha' from L0 to L2"),
        ]:
            ttk.Label(f, text=lbl).grid(row=r, column=0, sticky="w", padx=(4, 2), pady=2)
            var = tk.StringVar(value=getattr(self.cfg, attr))
            ent = ttk.Entry(f, textvariable=var, width=6)
            ent.grid(row=r, column=1, sticky="w", padx=2)
            ttk.Label(f, text=default_hint, font=("TkDefaultFont", 7),
                      foreground="#777").grid(row=r, column=2, columnspan=4,
                                              sticky="w", padx=4)
            def _bind_axis(v=var, a=attr):
                val = v.get().strip().upper()
                if val:
                    setattr(self.cfg, a, val)
            var.trace_add("write", lambda *_, v=var, a=attr: _bind_axis(v, a))
            r += 1

        # ── ReStim setup guide ───────────────────────────────────────────────
        self._section(f, r, "ReStim Setup Guide"); r += 1
        guide_frame = ttk.Frame(f, padding=(4, 2, 4, 6))
        guide_frame.grid(row=r, column=0, columnspan=6, sticky="ew")
        guide_text = (
            "In ReStim:  Tools → Preferences → Funscript / T-Code\n"
            "\n"
            "  Set each axis to match the values above:\n"
            "    Volume  →  L0   (empty by default — must be set)\n"
            "    Beta    →  L1   (already L1 by default — no change needed)\n"
            "    Alpha   →  L2   (default is L0 — change this to L2)\n"
            "\n"
            "  Also set Limit Min / Limit Max to match your device's range.\n"
            "  For FOC-Stim: Volume 0→1,  Beta -1→1,  Alpha -1→1"
        )
        ttk.Label(guide_frame, text=guide_text, font=("TkFixedFont", 7),
                  foreground="#bbbbbb", justify=tk.LEFT,
                  background="#1a1a1a", relief=tk.FLAT,
                  padding=(6, 4)).pack(fill=tk.X)
        r += 1

        # ── Timing ───────────────────────────────────────────────────────────
        self._section(f, r, "Timing"); r += 1
        self._fine_slider(f, r, 0, "Idle timeout (s)", 0.1, 10.0,
                          "idle_timeout", step=0.1); r += 1
        self._fine_slider(f, r, 0, "Send interval (ms)", 10, 200,
                          "send_interval_ms", fmt="d", int_val=True, step=5)
        ttk.Label(f, text="alpha/pulse loop tick  (lower = smoother, higher = lighter CPU)",
                  font=("TkDefaultFont", 7), foreground="#888").grid(
            row=r, column=3, columnspan=3, sticky="w", padx=4)
        r += 1

    # ── Log panel ─────────────────────────────────────────────────────────────

    def _build_log(self, parent):
        lf = ttk.LabelFrame(parent, text="Log", padding=(4, 2))
        lf.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._log_text = tk.Text(lf, height=4, state=tk.DISABLED,
                                  font=("TkFixedFont", 8),
                                  bg="#181818", fg="#cccccc",
                                  relief=tk.FLAT, wrap=tk.WORD)
        self._log_text.pack(fill=tk.X)

    def _append_log(self, msg: str):
        t = self._log_text
        t.configure(state=tk.NORMAL)
        t.insert(tk.END, msg + "\n")
        lines = int(t.index(tk.END).split(".")[0])
        if lines > 202:
            t.delete("1.0", f"{lines - 200}.0")
        t.see(tk.END)
        t.configure(state=tk.DISABLED)

    # ── Bridge control ────────────────────────────────────────────────────────

    def _toggle_bridge(self):
        if self._running:
            if self._engine:
                self._engine.stop()
            self._running = False
            self._start_btn.config(text="Start")
            self._dot_canvas.itemconfig(self._dot, fill="#bb3333")
            self._status_lbl.config(text="Stopped")
        else:
            self.cfg.restim_url = self._url_var.get().strip()
            self.cfg.osc_port   = self._port_var.get()
            self._shared.clear()
            self._engine = BridgeEngine(self.cfg, self._shared, self._log_q)
            self._engine.start()
            self._running = True
            self._start_btn.config(text="Stop")
            self._dot_canvas.itemconfig(self._dot, fill="#aaaa00")
            self._status_lbl.config(text="Connecting…")

    # ── Poll loop (runs on tkinter main thread) ───────────────────────────────

    def _poll(self):
        # Drain log queue and update status indicator
        try:
            while True:
                msg = self._log_q.get_nowait()
                self._append_log(msg)
                low = msg.lower()
                if "connected →" in low:
                    self._dot_canvas.itemconfig(self._dot, fill="#33cc55")
                    self._status_lbl.config(text="Connected")
                elif "failed" in low or "error" in low:
                    self._dot_canvas.itemconfig(self._dot, fill="#cc4444")
                    self._status_lbl.config(text="Error")
                elif "stopped" in low and self._running:
                    self._dot_canvas.itemconfig(self._dot, fill="#bb3333")
                    self._status_lbl.config(text="Stopped")
        except queue.Empty:
            pass

        # Update per-address intensity bars
        for info in self._addr_row_widgets:
            v = self._shared.get(info["ac"].address, 0.0)
            info["bar"].set(v)

        # Update live output bar
        l0 = self._shared.get("__live__l0", 0.0)
        l1 = self._shared.get("__live__l1", self.cfg.beta_off / 9999.0)
        l2 = self._shared.get("__live__l2", 0.0)
        self._live_vol_bar.set(l0)
        self._live_vol_lbl.config(text=f"{int(l0 * 100):2d}%")
        beta_raw = int(l1 * 9999)
        if beta_raw < 4800:
            side = "◄ Left"
        elif beta_raw > 5200:
            side = "Right ►"
        else:
            side = "Centre"
        self._live_beta_lbl.config(text=str(beta_raw))
        self._live_beta_side.config(text=side)
        self._live_alpha_bar.set(l2)
        self._live_alpha_lbl.config(text=f"{int(l2 * 100):2d}%")

        # Show discovered addresses not yet in the active list
        known = {ac.address for ac in self.cfg.addresses}
        new_disc = {
            k[8:]: v   # strip "__disc__"
            for k, v in self._shared.items()
            if k.startswith("__disc__") and k[8:] not in known
        }
        if new_disc:
            self._disc_frame.pack(fill=tk.X, pady=(6, 0))
            for w in self._disc_frame.winfo_children():
                w.destroy()
            for addr in sorted(new_disc):
                row = ttk.Frame(self._disc_frame)
                row.pack(fill=tk.X, pady=1)
                short = addr.replace("/avatar/parameters/", "…/")
                ttk.Label(row, text=short, font=("TkFixedFont", 8),
                          width=42, anchor="w").pack(side=tk.LEFT)
                def _add(a=addr):
                    self.cfg.addresses.append(AddressCfg(a))
                    del self._shared[f"__disc__{a}"]
                    self._rebuild_addr_list()
                ttk.Button(row, text="+ Add", width=6,
                           command=_add).pack(side=tk.RIGHT)
        else:
            self._disc_frame.pack_forget()

        self.root.after(150, self._poll)

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._running and self._engine:
            self._engine.stop()
        self.cfg.save()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    BridgeGUI().run()
