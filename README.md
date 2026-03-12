# OSCgoesZAP ⚡

**VRChat proximity → ReStim e-stim bridge**

Connects [OscGoesBrrr](https://github.com/OscToys/OscGoesBrrr) avatar contact data to [ReStim](https://github.com/diglet48/restim) via T-Code over WebSocket — so physical contact in VRChat drives real e-stim output with no Intiface required.

---

## How it works

```
VRChat avatar contacts
        │
        ▼  OSC float (0.0 – 1.0)
 OscGoesBrrr (port 9001)
        │
        ▼
  OSCgoesZAP bridge
  ┌─────────────────────────────────────┐
  │  dead zone filter                   │
  │  gamma curve (feel shaping)         │
  │  ┌─────────────────────────────┐    │
  │  │ L0  volume  (raw intensity) │    │
  │  │ L1  beta    (left/right pos)│    │  T-Code over WS
  │  │ L2  alpha   (oscillation)   │────┼──────────────────► ReStim
  │  │ Pulse mode  (triangle wave) │    │
  │  └─────────────────────────────┘    │
  └─────────────────────────────────────┘
```

### T-Code channels

| Channel | Role | Behaviour |
|---------|------|-----------|
| **L0** | Volume / intensity | Scales directly with proximity (0 → 9999) |
| **L1** | Beta position (left ↔ right) | Steps: `OFF=9999` → `Light=8099` → `Active=5000` based on intensity threshold |
| **L2** | Alpha oscillation | Sinusoidal sweep; rate and amplitude both scale with intensity |
| **Pulse** | Triangle wave on L0 | Optional mode — replaces static L0 with a pulsing wave |

---

## Requirements

- **Python 3.10+**
- **VRChat** with an OGB-compatible avatar
- **[OscGoesBrrr](https://github.com/OscToys/OscGoesBrrr)** — OSC contact output enabled, port 9001
- **[ReStim](https://github.com/diglet48/restim)** — WebSocket T-Code server enabled (default `ws://localhost:12346`)
- A compatible e-stim device connected to ReStim

---

## Installation

```bash
git clone https://github.com/blucrew/OSCgoesZAP.git
cd OSCgoesZAP
pip install -r requirements.txt
```

---

## Usage

### GUI (recommended)

```bash
python ogb_restim_bridge_gui.py
```

- Set your **ReStim URL** and **OSC port**, hit **Start**
- The bridge auto-discovers active OGB addresses while running — click **+ Add** in the *Discovered* panel to register them
- Click **▶** next to any address to expand its inline settings and choose which output modes it drives
- All parameters update live; config saves to `ogb_restim_config.json` on close

### CLI (headless / scripting)

```bash
python ogb_restim_bridge.py
```

Optional flags:

```
--osc-port 9001          OSC listen port (default 9001)
--restim-url ws://...    ReStim WebSocket URL (default ws://localhost:12346)
--gamma 0.5              Intensity curve exponent (0.5=sqrt, 1.0=linear, 2.0=squared)
--dead-zone 0.02         Noise floor — values below this are treated as zero
--beta-thresh 0.35       Intensity at which beta switches to Active tier
--no-alpha               Disable L2 alpha oscillation
--debug                  Log all unmatched OSC messages
```

---

## OGB address reference

OscGoesBrrr sends contact values on paths like:

```
/avatar/parameters/OGB/Pen/Shaft/PenSelf
/avatar/parameters/OGB/Pen/Shaft/TouchSelf
/avatar/parameters/OGB/Orf/Fleshlight/PenSelfNewRoot
```

The exact paths depend on your avatar's OGB setup. Run the GUI with the bridge active and the *Discovered* panel will show every address OGB is transmitting — add the ones you want to respond to.

---

## Output modes explained

| Mode | What it does | Good for |
|------|-------------|----------|
| **L0 Volume** | Intensity follows proximity directly | General sensation scaling |
| **L1 Beta** | Snaps to a left/right position based on intensity tier | Targeting a specific feel location |
| **L2 Alpha** | Oscillates left↔right at a rate that scales with intensity | Dynamic, alive sensation |
| **Pulse** | Replaces L0 with a triangle wave (rate + depth scale with intensity) | Rhythmic pulsing feel |

Modes can be combined. Per-address overrides let different contact zones drive different combinations — e.g. shaft contacts drive L0+L2, tip contact drives Pulse.

---

## Configuration

Settings are saved automatically to `ogb_restim_config.json` in the same directory.
Delete the file to reset to defaults.

Key parameters (all adjustable in the GUI Settings tab):

| Parameter | Default | Notes |
|-----------|---------|-------|
| Dead zone | 0.02 | Filters OSC noise at idle |
| Gamma | 0.5 | 0.5 = sqrt curve (snappier at low end) |
| Beta light pos | 8099 | L1 position for gentle touch (right of centre) |
| Beta active pos | 5000 | L1 position for full intensity (centre) |
| Beta threshold | 0.35 | Intensity at which Active tier kicks in |
| Alpha min/max Hz | 0.3 / 1.5 | Oscillation rate range |
| Alpha min/max amp | 0.20 / 0.45 | Oscillation depth range |
| Idle timeout | 0.5 s | Seconds before output parks to zero |

---

## Safety

- Output is gated by a dead zone — idle avatar produces zero signal
- An idle watchdog parks all channels if OSC stops arriving (avatar unloaded, VRChat closed, etc.)
- Start at low ReStim power and increase gradually while testing

---

## Dependencies

- [`python-osc`](https://pypi.org/project/python-osc/) — OSC server
- [`aiohttp`](https://pypi.org/project/aiohttp/) — WebSocket client

---

## Acknowledgements

- **[OscGoesBrrr](https://github.com/OscToys/OscGoesBrrr)** by [Senky Dragon](https://github.com/OscToys) — VRChat avatar OSC contact system that feeds proximity data into this bridge. Licensed [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
- **[ReStim](https://github.com/diglet48/restim)** by [diglet48](https://github.com/diglet48) — e-stim control software that receives T-Code commands from this bridge. Licensed [MIT](https://opensource.org/licenses/MIT).

OSCgoesZAP does not include or modify any code from either project. It communicates with OscGoesBrrr via the OSC protocol and with ReStim via its WebSocket T-Code interface.

---

## License

MIT — see [LICENSE](LICENSE)

OSCgoesZAP is an independent interoperability tool and not a derivative work of either dependency.
