# Appendages

How SerenChan grows things that aren't part of the stock StackChan: side flashlights, a fan, buttons,
the LLM module as a second pair of ears. This is a design written from reading the code and the parts'
datasheets, 16 Nov 2026. None of it is built yet, and nothing here has touched hardware. Every pin
number below is checked against this firmware's own code, but not yet against a real board with the
proto modules stacked on it.

## The short version

The firmware is the base: voice, servos, the neon rails, touch, the screen. An **appendage** is
anything plugged in on top of it. There are two kinds, sorted by what it would take to support them:

| | **Described** | **Compiled** |
|---|---|---|
| What it is | Wiring plus behaviour the firmware already has built in | A driver with its own protocol |
| How it gets on | A line in `soma.yaml` on the card, or sent from the Appendages tab | Ticked in the Firmware tab, built, installed by OTA |
| Changing it | Reboot | Rebuild |
| Examples | flashlights, fan, buttons, the switch | the LLM module (JSON over UART) |

Why not real drivers loaded from the SD card? An ESP32 can't load code off a card the way a PC loads a
DLL. Espressif's `elf_loader` gets close: it runs an ELF from PSRAM on the S3. But every module would
have to be linked against this exact firmware's symbols, and a bad one takes the whole robot down with
it. Most appendages don't need that anyway. A flashlight is a pulse train and a PWM line. A fan is one
PWM line. A button is a debounced input. If the firmware carries those **primitives**, the card only
has to say how they're wired.

## The pins

### What the firmware already uses (from the code, 16 Nov 2026)

| GPIO | Used by | Where in the code |
|---|---|---|
| 0 | I2S MCLK, and the boot button | `hal/board/config.h` |
| 3, 35, 36, 37 | LCD (CS, D/C, SCK, MOSI); 35 is also SD MISO | `hal/board/stackchan.cc`, `hal/hal_sdcard.cpp` |
| 4 | SD card CS | `hal/hal_sdcard.cpp` |
| 6, 7 | Servo bus (SCS, UART1 at 1 Mbaud, TX 6, RX 7) | `hal/hal_servo.cpp:356` |
| 11, 12 | Internal I2C: codecs, AW9523, BMI270, Si12T touch, and the base's PY32 (which drives the neon rails) | `hal/board/config.h`, `hal/hal_io_expander.cpp` |
| 13, 14, 33, 34 | I2S audio | `hal/board/config.h` |
| 15, 16, 38–42, 45–48 | Camera | `hal/board/config.h` |
| 19, 20 | USB (serial/JTAG) | the chip |
| 43, 44 | UART0, the main console (`CONFIG_ESP_CONSOLE_UART_DEFAULT`) | `sdkconfig` |
| 26–32 | Flash and PSRAM | the module |

### What's left

| GPIO | On the CoreS3 | Notes |
|---|---|---|
| 1, 2 | Port A (red) | External I2C by convention, but nothing in this firmware uses it |
| 8, 9 | Port B (black) | |
| 17, 18 | Port C (blue) | **Probably taken by the LLM module**, see below |
| 5, 10 | The M-Bus only | Reachable through a proto module |

That leaves **eight pins** for everything. Check them against M5's CoreS3 M-Bus pinout before soldering.
The table above is the CoreS3 as I remember it plus the elimination from code; the code side is sure,
the M-Bus side isn't.

### What the planned parts want

| Part | Pins | Notes |
|---|---|---|
| LLM module | 2 (UART) | On the CoreS3 the module's UART lands on the Port C position, G17/G18 (as M5's examples have it: RX 18, TX 17). If that holds, **a flashlight can't go in Port C**. |
| Flashlight ×2, stock | 1 each (EN) | |
| Flashlight ×2, with the dimming mod | 2 each (EN + FLASH) | See the flashlight section |
| Dual Button | 2 | Port B fits |
| Switch | 1 | |
| Fan | 1 (PWM) | Through a transistor or MOSFET, never straight from the pin |

Stock: 2 + 2 + 2 + 1 + 1 = **8**, exactly the budget, with nothing spare.
With the mod: **10**, two over. Ways out, best first:

1. **Share one FLASH line between both lights.** They dim together but still switch on and off
   separately. That makes 9.
2. **Put the slow things on I2C.** Make Port A an I2C bus with an expander (an M5 PbHub, or a cheap
   PCA9554 on a proto board). Buttons, the switch and the fan don't need microsecond timing, so they
   go there, and the direct GPIOs are kept for the flashlights' pulse trains and the UART. The
   flashlight library says outright that its pulse train can't go through PbHub. This also leaves
   room for whatever comes after the fan.
3. Leave the switch for later.

Doing 1 and 2 together makes room for everything, with some to spare.

### Power

The 5 V rail is shared by the servos (spiky), the LLM module (around 1.5 W, and it runs warm), and the
lights. A flashlight in torch mode is about 214 mA, so two make about 430 mA. A full-power flash is
about 584 mA each for up to 220 ms. Two flashing together while running on battery is how the
CoreS3 browns out and reboots mid-sentence; the flashlight's own README warns about exactly this. The
registry refuses a double full flash unless USB power is present (the AXP2101 knows).

### Stack height

The LLM module and each proto module sit on the M-Bus between the CoreS3 and the StackChan base. Measure
the stack before anything gets printed; the shell may not close.

## The primitives (what the firmware carries)

| Primitive | Does | Notes |
|---|---|---|
| `out` | A pin high or low | Starts low at boot, always |
| `pwm` | LEDC duty, 0 to 100% | Frequency per appendage |
| `pulses` | N rising edges with microsecond timing, interrupts off | For the AW3641E's 1-wire protocol |
| `button` | Debounced input to press, release and hold events | Interrupt-driven on a GPIO, polled through an expander |
| `uart` | Line-framed JSON both ways | For compiled appendages |

Each kind of appendage (`flashlight`, `fan`, `button`, `dual_button`, `switch`) is a small class built
from these primitives and compiled in once. A **described** appendage is one of those kinds, wired to
pins by a line of config.

## Describing one: `soma.yaml`

`soma.yaml` is deliberately not full YAML: `key: value` lines, one level of nesting, lines at most 192
characters (`hal/soma_config.cpp`). So an appendage is one line, the kind first and its wiring after:

```yaml
appendages:
  light_left:  flashlight en=8 flash=5 side=left
  light_right: flashlight en=9 flash=5 side=right
  fan:         fan pwm=10 follows=ears.temp on_at=60 full_at=75
  buttons:     dual_button a=1 b=2 on_a=interrupt on_b=mute
```

Pin numbers in that sketch are placeholders until the audit above has been checked on the board.

The appendage list is kept in NVS like the rest of the card, and the card is deleted after boot, the
same as today. `POST /api/config` takes the same lines, so the Appendages tab can change the list
without touching the card. Changes take effect at the next boot. Hot-swapping is a non-goal; it's
the wrong risk for a robot that runs on its own.

`appendages:` has to be added to `is_section` in `soma_config.cpp`. The line limit allows about six
`key=value` pairs, which is plenty.

### The registry's rules

- **A pin belongs to one appendage.** Two claiming one GPIO means *both* are refused, and `/api/status`
  says which pin and who claimed it. Pins the base uses (the table above) are always refused.
- **Everything starts off.** Every output pin goes low before anything else runs, so a
  crash-and-reboot never leaves a light latched on.
- **The registry owns the update loop.** The AW3641E's timed off (flash and timed torch) only happens
  when `update()` runs, so the registry calls it every tick, on a task of its own.
- **A refused appendage is not an error for the robot.** It boots, and the appendage shows
  `ok: false` with the reason.

## The flashlight (M5 Unit FlashLight, U152)

From the AW3641E datasheet (`WrensRoom/stackChanDocs/FLASHLEDDRIVER_2019-07-01.pdf`) and M5's driver
(`m5stack/M5Unit-FlashLight`):

- Grove Port C style: red 5 V, black GND, **yellow EN**, **white not connected**. 4.6 g, 32×24×8 mm.
- **Flash or torch is a hardware switch** (S1) that ties the chip's FLASH pin to 5 V or GND. Software
  can't read it.
- **Flash mode:** brightness is the number of rising edges on EN (1 = 100%, 8 = 30%), with 4 µs high
  and low, after EN has been low for at least 500 µs. Up to about 584 mA. The library clamps a flash
  at 220 ms, and M5 says to wait 8 to 15 s between flashes for heat.
- **Torch mode:** EN high is on, at a fixed ~214 mA. **Stock, there is no dimming.**

### The mod: dimming and mode switching from software

The datasheet's torch mode *does* dim: hold EN high, wait at least 2 ms, then PWM the **FLASH** pin. The
current is `I_flash·D + I_torch·(1−D)`. The unit wires FLASH to the switch instead. Set the switch to
torch (FLASH to GND), cut or lift that connection, and run FLASH to the **white Grove wire**. Then:

- FLASH low, EN high: torch at 214 mA.
- PWM on FLASH at duty D: more than torch, up toward flash current.
- FLASH high plus a pulse train on EN: a real flash, chosen from software.

Logic high is 1.3 V, so 3.3 V GPIO drives both pins fine. **Cap the duty at about 50%**: the chip's
continuous rating is 0.4 A, and 584·0.5 + 214·0.5 ≈ 400 mA. The registry enforces the cap, not the
caller. Check the board's schematic first: the switch may also pull FLASH through something we'd be
fighting.

Torch current never goes *below* 214 mA this way, so this dims *between* torch and the cap, not down
to a glow. Getting under torch might work by chopping EN slowly (low for more than 500 µs each cycle,
so the chip never reads the edges as a pulse count), but the chip soft-starts every time EN rises, so
it may flicker or come out uneven. That's a bench test, not a promise.

### What the flashlight kind offers

`on`, `off`, `level 0..100` (the mod only: 0 is plain torch), `flash level ms`, `fade to over ms`
(the mod only), and `sweep`: a gesture, the head turning with the beam on.

## The LLM module as ears (compiled)

The module (AX630C) runs wake word, speech-to-text and VAD on board. Its English speech-to-text is
`sherpa-ncnn-streaming-zipformer-20M` (`sys.lsmode`), which is small: fast, and streams partial
results, but likely less accurate than the cluster's.

The plan is not to replace the cluster's transcription but to add to it:

- **On the module:** the wake word, live partial text, and above all **end of speech**. The voice tab's
  first timer, "quiet that ends a turn", is the biggest wait in every reply, and a model that knows
  words beats a silence timer.
- **On the cluster:** the transcript that counts, as now.

Measure both on Chad's voice before deciding anything more. Open question: the module has its own
microphone. Do the ears listen through it, or does the CoreS3 forward audio?

The module also reports its chip temperature (`sys.hwinfo`), which is what `fan … follows=ears.temp`
reads. Appendages can read each other's telemetry through the registry by name.

UART at 115200 8N1, JSON per line. It's a compiled appendage because it has a protocol, a session
lifecycle (`setup`, `work`, `exit`) and streaming results. The primitives can't express that.

## The robot's API

| Route | Does |
|---|---|
| `GET /api/appendages` | Every configured appendage: `name`, `kind`, `pins`, `ok`, `error`, `state` (on, level, last press…), `telemetry` |
| `POST /api/appendages/<name>` | `{"action": "...", ...}`, the kind's own actions; unknown ones are refused with the list of known ones |
| `POST /api/appendages/<name>/test` | That kind's self-test (below); the answer says what to look for, because a robot can't see its own light |

MCP gets **one** tool, `appendage {name, action, args}`, and the Workbench gets one,
`soma_appendage`, that passes through. Adding a fan never needs a new tool anywhere.

### The self-tests (the Diagnostics pane)

| Kind | Test |
|---|---|
| flashlight | Torch on 1 s, each flash level once at 8 s spacing (or one level, for impatient people), a fade if modded. You confirm by eye. |
| fan | Ramp 0 to 100 and back; reports the duty, and RPM if the fan has a tach wire and a pin for it |
| button / dual_button / switch | Live: each press, release and hold, with timestamps, until you stop |
| ears | `sys.ping`, `sys.hwinfo` (load, memory, temperature), `sys.lsmode`, then a 5 s listen with what it heard |

## seren-soma

- **The Appendages tab:** the list (ticked = in the robot's appendage list), and for the one selected,
  its **data** (kind, pins, power, state) and its **diagnostics** (the self-test, live). Until the
  firmware reports appendages, the tab shows the catalogue and the plan, and says plainly that the
  robot doesn't have them yet.
- **The Diagnostics tab:** what used to be four tabs (Touch, Servos, Lights, Voice) is now one, with
  those four inside it. These are the base body, not appendages.
- **The Firmware tab:** the build side (later). The tab already stages a build of your own
  (`firmware.py`, `stage_bytes`) and offers it at the next check-in. What's missing is the build itself:
  a runner that takes the ticked compiled appendages, sets their `CONFIG_SOMA_APPENDAGE_*`, builds,
  and hands the image to `stage_bytes`. It could be ESP-IDF on a box at home or a GitHub Actions
  workflow; that's still to decide.

## Order of work

1. ~~This doc, and the pin audit in it.~~
2. ~~The tabs in seren-soma: Diagnostics gathered into one, and Appendages showing the catalogue and plan.~~
3. When the flashlights arrive: check the pin audit on the board, and test one light stock from a
   GPIO by hand.
4. Firmware: the registry, the primitives, `GET /api/appendages`, and the flashlight kind.
   Then the tab's data pane goes live.
5. The mod, on one light first.
6. When the proto boards arrive: the stack, the fan, the buttons, and the expander if needed.
7. The ears: measure first, then build.
8. The build runner for the Firmware tab.

## Open questions

- The LLM module's UART pins on the CoreS3: G17/G18 for sure, or switchable?
- Where the ears listen from: the module's microphone or the CoreS3's.
- Which switch unit, and what is it for?
- The fan: 5 V two-wire, or does it have a tach?
- The build runner: at home or GitHub Actions.
