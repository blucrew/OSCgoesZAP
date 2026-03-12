"""ogb_restim_bridge.py — OscGoesBrrr → ReStim T-Code bridge (v2).

OGB sends avatar contact intensity as an OSC float (0.0–1.0) to a local port.
This script receives those OSC signals and forwards them to ReStim's WebSocket
T-Code server — no Intiface required.

Refinements over v1:
  • Multi-address max  — monitors several OGB OSC paths, uses the highest value
  • Dead zone          — filters noise below DEAD_ZONE threshold
  • Intensity curve    — square-root gamma (more natural feel at low intensities)
  • Beta tiers         — L1 steps to LIGHT (8099) or ACTIVE (5000) by intensity
  • Alpha oscillation  — L2 oscillates at a rate/amplitude that scales with intensity

Setup:
  1. OGB → Settings: osc.port=9001
  2. Run: python ogb_restim_bridge.py  [--debug] [--no-alpha] [--gamma 0.5]
  3. ReStim websocket server must be active at localhost:12346

T-Code channels:
  L0  — volume   (intensity after curve → 0000–9999)
  L1  — beta     (LIGHT=8099 at low intensity, ACTIVE=5000 at high, OFF=9999 idle)
  L2  — alpha    (sinusoidal oscillation; rate & amplitude scale with intensity)

Install deps:  pip install python-osc aiohttp
"""

import asyncio
import logging
import argparse
import math
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer
import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ── Config ───────────────────────────────────────────────────────────────────

OSC_IP     = "127.0.0.1"
OSC_PORT   = 9001
RESTIM_URL = "ws://localhost:12346"

# All OGB addresses to monitor — bridge takes the max across all of them
OSC_ADDRESSES = [
    "/avatar/parameters/OGB/Orf/Fleshlight/PenSelfNewRoot",
    "/avatar/parameters/OGB/Pen/Shaft/PenSelf",
    "/avatar/parameters/OGB/Pen/Shaft/TouchSelf",
]

# Intensity processing
DEAD_ZONE       = 0.02   # raw values below this → treated as zero (noise filter)
INTENSITY_GAMMA = 0.5    # curve exponent: 0.5 = sqrt (more responsive at low end)
                          # 1.0 = linear, 2.0 = squared (sluggish at low end)

# Beta (L1) tiers
BETA_OFF    = 9999   # parked — outside feel range
BETA_LIGHT  = 8099   # right threshold — lighter sensation (low intensity)
BETA_ACTIVE = 5000   # centre — most intense (high intensity)
BETA_THRESH = 0.35   # effective intensity above which ACTIVE tier kicks in

# Alpha (L2) oscillation
ALPHA_CENTER  = 0.5   # neutral alpha position (0.0–1.0)
ALPHA_MIN_HZ  = 0.3   # oscillation rate at minimum intensity
ALPHA_MAX_HZ  = 1.5   # oscillation rate at maximum intensity
ALPHA_MIN_AMP = 0.20  # oscillation amplitude at minimum intensity
ALPHA_MAX_AMP = 0.45  # oscillation amplitude at maximum intensity

INTERVAL_MS  = 100    # T-Code transition time (ms) for L0/L1 updates
IDLE_TIMEOUT = 0.5    # seconds without OSC before parking everything


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tcode_val(v: float) -> str:
    """Unipolar 0.0–1.0 → zero-padded 4-digit T-Code string (0000–9999)."""
    return str(int(max(0.0, min(1.0, v)) * 9999)).zfill(4)


def _apply_curve(raw: float) -> float:
    """Apply dead zone then gamma curve. Returns effective intensity 0.0–1.0."""
    if raw < DEAD_ZONE:
        return 0.0
    # Remap [DEAD_ZONE, 1.0] → [0.0, 1.0] so we start cleanly from zero
    remapped = (raw - DEAD_ZONE) / (1.0 - DEAD_ZONE)
    return remapped ** INTENSITY_GAMMA


def _beta_tier(effective: float) -> int:
    """Map effective intensity to the appropriate beta position."""
    if effective <= 0.0:
        return BETA_OFF
    return BETA_ACTIVE if effective >= BETA_THRESH else BETA_LIGHT


# ── Bridge ───────────────────────────────────────────────────────────────────

class OGBRestimBridge:
    def __init__(self, no_alpha: bool = False):
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        # Per-address raw intensities; we take the max
        self._addr_intensity: dict[str, float] = {a: 0.0 for a in OSC_ADDRESSES}
        self._effective: float = 0.0
        self._last_osc_time: float = 0.0
        self._current_beta: int = BETA_OFF
        self._no_alpha = no_alpha
        self._alpha_phase: float = 0.0
        self._alpha_parked: bool = True
        self._loop = None

    # ── ReStim connection ───────────────────────────────────────────────────

    async def connect_restim(self) -> bool:
        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(RESTIM_URL, heartbeat=30)
            logging.info("Connected to ReStim at %s", RESTIM_URL)
            return True
        except Exception as e:
            logging.error("Could not connect to ReStim: %s", e)
            return False

    async def send_tcode(self, cmd: str):
        if self._ws is None or self._ws.closed:
            logging.warning("ReStim disconnected — reconnecting…")
            await self.connect_restim()
            if self._ws is None:
                return
        try:
            await self._ws.send_str(cmd)
            logging.debug(">> %s", cmd)
        except Exception as e:
            logging.error("Send error: %s", e)

    # ── OSC callbacks ───────────────────────────────────────────────────────

    def on_ogb_intensity(self, address: str, *args):
        """Called by python-osc for each monitored OSC address."""
        if not args:
            return
        self._addr_intensity[address] = float(args[0])
        self._last_osc_time = self._loop.time()

        effective = _apply_curve(max(self._addr_intensity.values()))
        self._effective = effective

        asyncio.run_coroutine_threadsafe(
            self._update_volume_and_beta(effective), self._loop
        )

    async def _update_volume_and_beta(self, effective: float):
        """Send L0 volume. Send L1 beta only when the tier changes."""
        desired_beta = _beta_tier(effective)
        if desired_beta != self._current_beta:
            transition = 500 if desired_beta == BETA_OFF else 200
            await self.send_tcode(f"L1{desired_beta:04d}I{transition}")
            self._current_beta = desired_beta

        await self.send_tcode(f"L0{_tcode_val(effective)}I{INTERVAL_MS}")

    def _default_osc_handler(self, address: str, *args):
        if args and isinstance(args[0], float):
            logging.debug("OSC (unmatched): %s = %s", address, args)

    # ── Alpha oscillator ────────────────────────────────────────────────────

    async def _alpha_oscillator(self):
        """Background task — sinusoidally oscillates L2 (alpha).
        Rate and amplitude both scale with effective intensity.
        """
        if self._no_alpha:
            return

        dt = 0.05  # 50 ms tick → ~20 Hz update rate
        while True:
            eff = self._effective
            if eff < 0.01:
                if not self._alpha_parked:
                    await self.send_tcode(f"L2{_tcode_val(ALPHA_CENTER)}I500")
                    self._alpha_parked = True
                self._alpha_phase = 0.0
            else:
                self._alpha_parked = False
                hz  = ALPHA_MIN_HZ  + (ALPHA_MAX_HZ  - ALPHA_MIN_HZ)  * eff
                amp = ALPHA_MIN_AMP + (ALPHA_MAX_AMP - ALPHA_MIN_AMP) * eff
                pos = ALPHA_CENTER + amp * math.sin(2 * math.pi * self._alpha_phase)
                self._alpha_phase = (self._alpha_phase + hz * dt) % 1.0
                await self.send_tcode(f"L2{_tcode_val(pos)}I{int(dt * 1000)}")

            await asyncio.sleep(dt)

    # ── Idle watchdog ───────────────────────────────────────────────────────

    async def _idle_watchdog(self):
        """Park everything if no OSC received for IDLE_TIMEOUT seconds."""
        while True:
            await asyncio.sleep(0.1)
            if self._current_beta != BETA_OFF and self._loop is not None:
                elapsed = self._loop.time() - self._last_osc_time
                if elapsed > IDLE_TIMEOUT:
                    logging.info("No OSC signal — parking to off")
                    for k in self._addr_intensity:
                        self._addr_intensity[k] = 0.0
                    self._effective = 0.0
                    await self.send_tcode(
                        f"L1{BETA_OFF:04d}I500 L0{_tcode_val(0.0)}I500"
                    )
                    self._current_beta = BETA_OFF

    # ── Main ────────────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        if not await self.connect_restim():
            logging.error(
                "Could not connect to ReStim — is the websocket server enabled on %s?",
                RESTIM_URL
            )
            return

        # Startup: park everything
        await self.send_tcode(
            f"L1{BETA_OFF:04d}I0 L0{_tcode_val(0.0)}I0 L2{_tcode_val(ALPHA_CENTER)}I0"
        )

        dispatcher = Dispatcher()
        for addr in OSC_ADDRESSES:
            dispatcher.map(addr, self.on_ogb_intensity)
        dispatcher.set_default_handler(self._default_osc_handler)

        server = AsyncIOOSCUDPServer((OSC_IP, OSC_PORT), dispatcher, self._loop)
        transport, protocol = await server.create_serve_endpoint()

        logging.info("Listening for OGB OSC on %s:%d", OSC_IP, OSC_PORT)
        logging.info("Watching %d OSC addresses:", len(OSC_ADDRESSES))
        for a in OSC_ADDRESSES:
            logging.info("  %s", a)
        logging.info(
            "Curve: gamma=%.2f  dead_zone=%.2f  beta_thresh=%.2f  alpha=%s",
            INTENSITY_GAMMA, DEAD_ZONE, BETA_THRESH,
            "off" if self._no_alpha else f"{ALPHA_MIN_HZ}–{ALPHA_MAX_HZ} Hz"
        )
        logging.info("Bridge running — touch your avatar contacts in VRChat!")

        try:
            await asyncio.gather(
                self._idle_watchdog(),
                self._alpha_oscillator(),
            )
        finally:
            transport.close()
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OGB → ReStim T-Code bridge v2")
    parser.add_argument("--osc-port",    type=int,   default=OSC_PORT,
                        help=f"OSC listen port (default {OSC_PORT})")
    parser.add_argument("--restim-url",  type=str,   default=RESTIM_URL,
                        help=f"ReStim WS URL (default {RESTIM_URL})")
    parser.add_argument("--gamma",       type=float, default=INTENSITY_GAMMA,
                        help=f"Intensity curve exponent (default {INTENSITY_GAMMA}; 0.5=sqrt, 1.0=linear)")
    parser.add_argument("--dead-zone",   type=float, default=DEAD_ZONE,
                        help=f"Noise dead zone threshold (default {DEAD_ZONE})")
    parser.add_argument("--beta-thresh", type=float, default=BETA_THRESH,
                        help=f"Effective intensity to switch to ACTIVE beta (default {BETA_THRESH})")
    parser.add_argument("--no-alpha",    action="store_true",
                        help="Disable L2 alpha oscillation")
    parser.add_argument("--debug",       action="store_true",
                        help="Log all unmatched OSC messages")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    OSC_PORT        = args.osc_port
    RESTIM_URL      = args.restim_url
    INTENSITY_GAMMA = args.gamma
    DEAD_ZONE       = args.dead_zone
    BETA_THRESH     = args.beta_thresh

    bridge = OGBRestimBridge(no_alpha=args.no_alpha)
    asyncio.run(bridge.run())
