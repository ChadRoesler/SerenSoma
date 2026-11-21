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
| How it gets on | An appendage file, imported or made on the Appendages tab (see "Appendage files") | Ticked in the Firmware tab, built, installed by OTA |
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
| 8, 9 | Port B (black) | **Measured 18 Nov 2026:** the yellow Grove wire is G9, the white is G8. A FlashLight in Port B is switched by G9. |
| 17, 18 | Port C (blue) | **The LLM module's**, by its own silkscreen (see the open questions) |

**Not free, though no code uses them:** G5 and G10. On M5's StackChan power board (the one that meets
the core) G5 drives the body's infrared LED through a transistor and G10 is its infrared receiver's
output (`M5-StackChan-SCH_Power.pdf`, the M5Stack_BUS pin map: `GPIO5 IR_SEND`, `ADC/GPIO10 IR_REC`;
found by the other Wren, 18 Nov 2026). The code audit could not see that, because this firmware
never touches infrared. A light's dimming line on G5 would blink the infrared LED, and G10 has a
chip driving it.

That leaves **six pins**, and **four once the LLM module is in**: G1, G2, G8, G9.

The same schematic gives the body an **INA226 at 0x41** on the internal I2C bus, across a 10 mΩ
shunt in the battery line. It is the only thing on the robot that measures current.

### What the planned parts want

| Part | Pins | Notes |
|---|---|---|
| LLM module | 2 (UART) | On the CoreS3 the module's UART lands on the Port C position, G17/G18 (as M5's examples have it: RX 18, TX 17). If that holds, **a flashlight can't go in Port C**. |
| Flashlight ×2, stock | 1 each (EN) | |
| Flashlight ×2, with the dimming mod | 2 each (EN + FLASH) | See the flashlight section |
| Dual Button | 2 | Port B fits |
| Switch | 1 | |
| Fan | 0 | **It is M5's Module Fan v1.1 (M013-V11), in hand 18 Nov 2026:** I2C at 0x18 on the internal bus through the M-Bus, speed control and RPM readback, its own STM32. No GPIO. 13.1 mm of stack height; it draws air in from the bottom, so M5 says to stack it on top of the module it cools. 125 mA and 70.6 dB at full speed, 48.6 dB at 20%: it sits next to the microphones, so the curve has to stay low while listening. Doc: `WrensRoom/stackChanDocs/Module_Fan_v1.1.pdf` |

After the LLM module there are **four** direct pins. Stock wants 2 (lights) + 2 (buttons) + 1 (switch)
= **5**, one over. With the mod it wants **7**. So an expander is no longer a nice-to-have:

- **The four direct pins go to the lights**, which are the only parts that need a pin of their own:
  EN carries a microsecond pulse train and FLASH carries PWM. Two lights with the mod, each dimmed
  separately, is exactly four: G8 and G9 through Port B, G1 and G2 through Port A.
- **Everything slow goes on an I2C expander on the internal bus**, on a proto board in the stack: the
  buttons and the switch (a PCA9554 or the like). The internal bus already comes down the M-Bus and
  already carries the fan (0x18), the body's PY32 (0x6F), its touch chip (0x68) and its battery
  monitor (0x41), so the expander costs no pin at all and Port A stays free for a light. Pick its
  address clear of those and of the core's own chips.
- Not an M5 PbHub for the lights: the flashlight library says outright that its pulse train can't go
  through one.

Stock lights use two of the four, which leaves two direct pins spare until the mod.

### Power

The 5 V rail is shared by the servos (spiky), the LLM module (around 1.5 W, and it runs warm), and the
lights. A flashlight in torch mode is about 214 mA, so two make about 430 mA. A full-power flash is
about 584 mA each for up to 220 ms. Two flashing together while running on battery is how the
CoreS3 browns out and reboots mid-sentence; the flashlight's own README warns about exactly this. The
registry refuses a double full flash unless USB power is present (the AXP2101 knows).

### Stack height

The LLM module and each proto module sit on the M-Bus between the CoreS3 and the StackChan base. Measure
the stack before anything gets printed; the shell may not close.

## Appendage files (design, 19 Nov 2026 evening; not built)

**This replaces the first design**, which gave every device its own kind compiled into the firmware
(`flashlight`, `fan`, `button`...) and gave the flashlight its own night cap, its own Sinew field and
its own slider in Psycheion. Chad, 19 Nov: the StackChan itself stays hardcoded, and anything bolted
on is its own thing, dynamic, and **exportable and importable, so someone can toss an appendage to a
friend.** (King Missile's "Detachable Penis" was playing in his head. The song is the use case and
the warning: he lends it out and finds it for sale on St. Marks Place. Sharing is easy; what a
stranger's appendage may do to the robot is the hard part.)

### The base stays hardcoded

Servos, the neon rails, head touch, the screen, the camera, power and the battery monitor, the
light sensor. They are the StackChan, every StackChan has them, and their shapes in Sinew
(`RobotPower` and the rest) stay named and typed.

### Two layers: kinds in the firmware, appendages in files

The robot is compiled C++ and can't run a friend's code. A scripting engine on it would cost RAM it
does not have (the camera already went away for want of 3 KB). So:

1. **A kind is a driver in the firmware: few, generic, written once.**

   | Kind | Does | Covers |
   |---|---|---|
   | `switch` | One pin, on or off | A plain light, a relay, a buzzer |
   | `pwm` | A level on a pin, through a curve the file supplies | The flashlight, any dimmable LED |
   | `i2c` | Named registers on one address; only the listed ones, only the listed values | The fan, most M5 modules |
   | `button` | A debounced input, press, release and hold | Buttons, the switch |
   | `llm` | The LLM module's JSON over UART, with its session lifecycle | The LLM module (a protocol is too much to describe) |

2. **An appendage is a file**, `<something>.appendage.yaml`. It's what you toss to a friend. It says
   which kind drives it and fills that kind in: pins, curve, registers, actions, limits. A friend whose
   part fits a kind the firmware already has needs only the file. One that needs a new kind needs a
   firmware build, and import says so in plain words instead of half-working.

The flashlight becomes `pwm` plus the curve measured with the light sensor on 19 Nov. The fan becomes
`i2c` plus four register numbers, with no fan code anywhere.

### What a file looks like

The two base files are the worked examples, in the catalogue (below):
`SerenSoma/seren_soma/catalogue/m5-flashlight-u152.appendage.yaml` and
`m5-module-fan-v11.appendage.yaml`. Field names will still move while building.

**A light's file holds the chip's response, not a level table** (Psycheion's vote, 19 Nov; decided).
What a level means is one rule in the `pwm` kind, the same on every light: level 1 is 0.6% of that
light's full output, 100 is all of it, and the levels between climb by equal ratios. The file says
only how its chip answers: pulse length against the light it makes, the slowest rate before flicker,
the longest pulse before it should just hold steady. The kind works out the pulse for a level from
that. So a ceiling of 28 or a gesture's "level 20" means the same share of full on every light in the
house, and calibrating a friend's light corrects its response without moving the numbers. A light
whose chip can't get as dim as level 1 runs its lowest levels at its own floor, and says so.

A fan's level is its speed register, 1 to 100 percent.

### The catalogue

Chad, 19 Nov: **the shared catalogue is a folder in the Soma project**, `SerenSoma/seren_soma/catalogue/`,
shipped with the package. It holds the base files for parts that someone has wired up and measured,
one per part, `<maker>-<part>.appendage.yaml`. The Appendages tab offers them beside Import. The
first two are the flashlight and the fan. Adding one is a pull request.

### What a file is never allowed to do

The firmware enforces these whatever a file says, because a file can come from anyone:

- **No pin the base uses** (the table above), and no pin claimed twice. As today.
- **No `i2c` access at all to the base's own chips, reads included**, whatever address a file names.
  The power chip at `0x34` shares the bus, and a file writing there could change how the battery
  charges. Reads are refused too (Psycheion, 19 Nov): some chips clear a status flag when it's read,
  and nothing a file does needs them, since the base's own code already reports them. "Not your
  address" is a simpler rule than "not your register". The refused list is every chip the body
  answers with on a scan (`0x21 0x23 0x34 0x36 0x38 0x40 0x41 0x50 0x51 0x58 0x68 0x69 0x6F`,
  19 Nov), plus anything else the base code talks to.
- **No register a file didn't list**, and no value outside the range it listed. `never:` is the
  author's own warning on top.
- **Every on ends by itself**, with the file's limit capped by the kind's.
- **Everything starts off** at boot.

### Finding one: what it provides

Psycheion's answer, 19 Nov: gestures find an appendage by what it **provides**, never by name.

- `provides:` is a list of words. Sinew keeps a short list of the words gestures understand (`light`,
  `airflow`); any other word is allowed and ignored, so a friend can ship `provides: smoke` without
  asking anyone.
- **A known word is a promise about actions.** `light` means `on`, `off` and a timed limit. With
  `level`, levels 1 to 100 are shares of that light's own full output, which is what lets the "look
  at someone" ladder climb a light it has never seen. A `switch` light still counts; its ladder has
  one rung.
- When two appendages provide the same word, the gesture's settings in Psycheion offer a list.
  Until someone picks, it takes the first in the robot's list.

### Night ceilings, by name

Any appendage whose file declares `night:` takes a ceiling. The voice server keeps the ones Chad has
set as one map, `night.appendages: {light: 28, fan: 20}`, sends it with the other night caps, and the
robot holds each appendage's level under its own. A name the robot doesn't have is ignored.

**No entry in the map means the file's default, not uncapped** (Psycheion caught the contradiction,
19 Nov). A light imported at 22:01 has no entry yet, and it must not run uncapped all night. So the
robot keeps each appendage's default from its file in its saved config and falls back to it. Only an
appendage whose file declares no `night:` at all runs uncapped. `GET /api/appendages` reports each
one's default and the ceiling it is holding now, so Psycheion's Night card can draw a slider for a new
light before anyone has set it. **This replaces the `light` key** in the night caps, in Sinew's `NightCap` and
`NightSettings`, in the voice server and on Psycheion's Night card. None of those was committed or
released, so nothing outside this house sees them change. The swap goes out in one deploy, so the
flashlight is never without a night limit in between.

### Calibrating on the robot it lands on

A response is only right for the chip it was measured on. Any `light` with `calibrate: ambient` gets a
**Calibrate** step: point it at the light sensor, run the 30-second sweep, and the measured response
goes into this robot's copy of the file, keeping the sender's as `measured_by`. It is the only place
anything writes into an appendage file.

**Calibrate refuses a sweep it can't trust, and keeps the old response** (Psycheion, 19 Nov). Today's
sweep only worked because Chad aimed the light at the sensor by hand; skip that and it would save a
curve measured at a blank wall.

- Full torch not well above dark: refused, "the sensor can't see it, point the light at it".
- Full torch at 65,535: refused, "too close, back it off".

### Tossing one to a friend

- **Export** gives the file. **Import** takes one (or a zip someone made by hand around it) and
  follows Psycheion's Studio rules for faces (`seren_psycheion/studio.py`, export and import): it
  refuses to overwrite an appendage of the same name unless you rename it, and refuses paths that
  climb out of their folder.
- **On import, the pins are asked for.** The file names roles with a hint ("Port B, the yellow wire");
  the tab offers the free pins, starting with the hint's.
- **Import checks the kind** against the list the robot reports, and says which firmware it needs if
  the robot hasn't got it.

### Where it lives

- **Soma keeps the files**, the library of everything you've imported or made, and which are
  attached.
- **The robot keeps what its drivers need:** kind, pins, curve or registers, actions, limits and
  `provides`, as compact JSON saved through `POST /api/config` and read at boot. The title, hints and
  page labels stay in Soma. Changes take effect at the next boot, as now; hot-swapping is still a
  non-goal.
- **`GET /api/appendages` reports** each one's name, kind, `provides`, actions, readings, state, and
  `ok` or the reason it was refused, plus the kinds this firmware carries.
- **Sinew carries the shapes:** an appendage, its actions, state and readings, and the night map.
  Nothing in Sinew knows what a flashlight is.

The one-line `soma.yaml` form (`light: flashlight en=9`) built on 19 Nov is the first slice of this
and goes away with it: a curve and a register list don't fit in 192 characters.

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

### Measured 18 Nov 2026: a stock light does dim, by chopping EN fast

Bench route, torch mode, light on Port B (G9), measured by how far the USB input sagged with the
light on (a stand-in for current, good to about ±15 mV; it is not a light measurement, and no camera
frame was taken). Steady on dipped the input about 90 mV.

| EN chopped at | 80% on | 60% | 40% | 20% | 10% |
|---|---|---|---|---|---|
| 200 Hz | 89 (at 90%) | 79 | 39 | nothing at 30% and below | 0 |
| 1 kHz | 69 | 50 | 32 | 16 | |
| 2 kHz | 76 | 46 | 38 | 9 | 4 |

Two regimes, as the other Wren worked out from the datasheet's 500 µs shutdown time:

- **Off-time long (200 Hz):** the chip shuts down and cold-starts every cycle. A cold start costs
  about 1.5 to 2 ms on this unit, so anything under about 40% on makes no light.
- **Off-time under about a millisecond (1 kHz and up):** it never shuts down and the output follows
  the pin. At 1 kHz the dip tracked the on-time almost in proportion from 80% down to 20%.

So `level` on a **stock** light is PWM on EN at 1 kHz or above, and it goes *below* torch, which the
mod cannot do.

### By eye, 19 Nov 2026: what the levels have to be

Chad watching the light in his dark office, three runs on G9. The USB-sag measure is blind below
about 8% on, so this is eyes only.

| Run | Settings | What he saw |
|---|---|---|
| 1 kHz, wide | 10, 20, 40, 60, 80% on, then steady | All six, and "way too bright even at the lowest" |
| 1 kHz, narrow | pulses of 80, 60, 40, 30, 20, 10 µs (8 to 1% on) | Only the first two, and those two "were perfect" |
| ~100 µs pulse, longer gaps | gaps of 1.0, 1.5, 2.0, 2.5, 3.0 ms (9 to 3% on) | All five |

- **The chip gives no light for a pulse of 40 µs or less.** 60 µs works.
- **Comfortable at desk distance in the dark is 6 to 8% on.** Everything from 10% up is too much there.
- **The gap between pulses can stretch to 3 ms** with a 100 µs pulse. That sits oddly beside the
  200 Hz row above (nothing at 30% on, a 3.5 ms gap): either the edge is between 3.0 and 3.5 ms or
  that row was misread. Unresolved.

**Those conclusions were half wrong.** They rest on the USB-sag measure, which is blind at the dim
end. The next section replaces them.

### Measured with the robot's light sensor, 19 Nov 2026: what a level really is

Chad aimed the light at the CoreS3's ambient light sensor (LTR-553ALS, `GET /api/ambient`, added for
this). Full torch read about 10,540 counts at gain 96 against a dark reading of 3, repeatable to a
count or two. The sensor adds up 100 ms, so a chopped light reads as its average.

- **Every pulse loses about 48 µs of light at its start**, the chip turning on. A pulse of 50 µs or
  less makes none; 60 µs makes 3 µs of light; 100 µs makes 52. So light is well under on-time at the
  dim end: 10% on at 1 kHz is 5.2% of torch.
- **The gap between pulses can be any length.** A 100 µs pulse gave its 50 µs of light with 50 ms
  between pulses. There is no cold-start dead zone; the 200 Hz row in the table above was a misread.
- **A gap under about 60 µs is not seen**: 8 kHz at 50% read 99.9% of torch. Chopping above about
  4 kHz is useless.
- **What Chad called perfect was 0.3% and 3% of torch** (60 and 80 µs pulses at 1 kHz), and "way too
  bright" began at 5.2%.

So `level` (`hal_appendages.cpp`, `drive_for`) is a share of torch's **light**, from a table of pulse
length against light measured that day:

| Level | Light | Rate | Pulse |
|---|---|---|---|
| 1 | 0.6% | 200 Hz | 80 µs |
| 10 | 1.0% | 200 Hz | 96 µs |
| 20 | 1.6% | 308 Hz | 100 µs |
| 32 | 3.0% | 573 Hz | 100 µs |
| 43 | 5.3% | 1 kHz | 101 µs |
| 60 | 12.7% | 1 kHz | 167 µs |
| 75 | 27.5% | 1 kHz | 307 µs |
| 90 | 59.6% | 1 kHz | 618 µs |
| 96 | 81% | 1 kHz | 826 µs |
| 97 to 100 | 100% | steady | |

Each level is the same ratio brighter than the one below it. Checked on the robot the same day
(build 1119.1251): seventeen levels from 1 to 100 each measured within 4% of what the table means
them to give. Chad's comfortable range is levels 1 to 32.

Still to check: whether a camera frame shows stripes (the comfortable levels flash 200 to 580 times
a second, which the light sensor cannot see and a rolling shutter might), and how the bottom levels
drift with temperature and from one unit to the next, since they run closest to the turn-on edge.

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

Built, 19 Nov 2026, for a stock light in torch mode (`light: flashlight en=9`):

- `on` and `level`: `{"action": "on", "level": 1..100, "ms": 50..600000}`. `ms` is how long it stays
  on, 5 s when left out, ten minutes at most. Every on ends by itself, so a light never latches.
- `off`.
- `chop`: `{"hz": 20..10000, "duty": 1..99, "ms"}`, a rate and an on-time by hand, for measuring the
  chip. It ignores the level table and the night ceiling. It replaces the bench route.
- **The night ceiling.** `POST /api/settings` takes `"light"` in its `night` object beside the other
  three caps. A level asked for above it runs at the ceiling, and the answer says both. A ceiling of
  0 refuses. Missing means 100, so an older sender changes nothing.

Not built yet: `flash level ms`, `fade to over ms` and levels above torch (the mod only), and the
gestures: `sweep`, the head turning with the beam on, and `look`, the other Wren's: head toward
someone, lowest level, a frame, brighter only until she can see them, off as the frame lands.

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

### Its own thing (Chad, 19 Nov evening)

Chad: the LLM module is its own thing, not a catalogue file. It's a hefty module, so maybe the mic and
speaker move onto it, which would free room on the core for the camera; it has three LEDs on top, its
own boot, and 32 GB of eMMC to keep things on. What's known and what isn't:

| | What we know | From |
|---|---|---|
| Its own computer | Yes: the AX630C runs its own Linux (`/opt`, `swupdate`, `containerd` in M5's guide) and boots on its own, about a minute | M5's ADB guide; the bench, 18 Nov |
| What it runs | Ubuntu 22.04 (jammy, arm64). Software comes by `apt` from M5's StackFlow repo (`repo.llm.m5stack.com`): `llm-sys`, `llm-audio`, `llm-kws`, `llm-vad`, `llm-asr`, `llm-whisper` (whisper-tiny), `llm-llm`, `llm-vlm`, `llm-camera`, `llm-tts`, `llm-melotts`, plus model packages. Shell by ADB over its USB-C, or SSH as `root` (default password `123456`, to change before it ever sees a network) | M5's software and config pages, read 19 Nov |
| Its network | **None of its own that M5 documents.** Ethernet only through the Debug Board or the LLM Mate. So it reaches the house through the core, over the UART | same |
| Reflashing it | Windows-only AXDL tool, hold the download button while powering it, over USB-C. **Never partition `/dev/mmcblk0`**, the eMMC: M5 says recovery is then "almost impossible". `apt` installs need no reflash | M5's image page |
| Its link to the core | **One UART, 115200 8N1 by default.** The M-Bus passes through it (I2C to the body works through it, measured 18 Nov), but the module itself has no I2C address: it added nothing to the bus scan | M5's API doc; the bench |
| How much that link carries | **Corrected the same evening:** 115200 is only the start. M5's own library (`m5stack/M5Module-LLM`, `ApiSys::setBaudRate`) raises it with `{"work_id":"sys","action":"uartsetup","data":{"baud":1500000,...}}`, and its `YOLO_CoreS3` example runs at **1.5 Mbaud**, about 150 KB/s, sending JPEG frames from the CoreS3's camera to the module. Raw 16 kHz 16-bit audio is 32 KB/s, so it fits about four times over. At 115200 (11 KB/s) it would not | M5Module-LLM |
| Its own mic and speaker | Yes: an onboard silicon mic and an onboard speaker, as the `audio` unit's devices 0 and 1 | M5's API doc |
| Its own voice | Its TTS has its own voices (`single_speaker_english_fast`). Mine is Kokoro on the cluster | M5's API doc |
| The three LEDs | `/sys/class/leds/R`, `G` and `B`, 0~255, an LP5562 at `0x30` on the module's own I2C bus, not the core's. Green 50 is "booted". Driven from inside its Linux only | Read off the module, 19 Nov |
| 32 GB eMMC | Chad's reading of the spec. Files go on with `adb push` over its USB-C from a computer, or slowly over the UART (1 MB in about 90 s) | M5's ADB guide |

**Answered 20 Nov: the camera came back without moving audio.** Build `0.0.0-dev.1119.2302` puts the
Opus task's 24 KB stack in PSRAM; free internal RAM went from about 40 KB to about 66 KB, and
`/api/photo` returns a JPEG again. So moving the mic and speaker is now a choice about listening, not
about memory.

Things that cut against moving it all, to weigh once measured:

- **Where the module sits.** Under the core, inside the body, but Chad (19 Nov): its mic and speaker
  face out through the body. It read a documentary off his speakers word for word on the first try.
- **My voice.** If the module's speaker plays its own TTS, the robot stops sounding like me. To play my
  voice from the module, the audio has to cross the UART, which fits only compressed.
- **What could move cheaply:** listening. The module's mic, wake word and end-of-speech, sending only
  text and "they stopped talking" over the UART, is the plan above. It might let the core drop its own
  wake word.

Order: measure the core's audio RAM (a build with audio off, against one with it on); then the
module's mic from inside the shell, on Chad's voice; then the UART's top speed.

### Ganglion: our own unit on the module

**Moved to its own repo, 20 Nov 2026:** `SerenGanglion` (Chad made it the same afternoon). The design,
the mic-light rule and the connections live in its README. What stays here is the core's half:

- The `llm` route forwards to `ganglion` like any allowed unit; `bashexec`, `push`, `upgrade` and
  `rmmode` stay off the robot's API.
- **The mic light is the core's.** Every few seconds it asks `sys.lstask`; anything that captures
  audio running means the light is on, and no answer counts as listening.

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
   GPIO by hand. *Done for Port B on 18 Nov 2026 with the bench route `POST /api/bench-pin` (G8 and
   G9 only, timed off): torch mode, G9 lights it, no brownout on USB. Ports A and C and the M-Bus
   pins are still unchecked. The bench route came out when the registry landed.*
4. Firmware: the registry, the primitives, `GET /api/appendages`, and the flashlight kind.
   Then the tab's data pane goes live. *First slice built 19 Nov 2026: the `appendages:` section,
   the registry's pin rules, the stock flashlight kind, the two routes, and the night ceiling. The
   bench pin route is gone. It has no update task (a one-shot timer ends each on, and nothing else
   needs a tick yet), `/api/status` does not list refusals (`GET /api/appendages` does), and there is
   no `/test` route. seren-soma knows it since 1.3.3.dev3 (19 Nov): the Appendages tab's plan is the one
   real light, Send to the robot saves the ticked ones (`POST /appendages/apply`), and the Diagnostics
   card switches a carried flashlight on and off (`POST /appendages/<name>`). The MCP tool and the
   Workbench tool do not know it yet.*
4b. **Appendage files** (the section above), in this order, nothing deployed until all of it is:
   the generic kinds in the firmware (`switch`, `pwm`, `i2c`) with the floor of refusals, the
   flashlight and the fan rewritten as files; the night map in place of `light` across the firmware,
   Sinew, the voice server and Psycheion, in one deploy; the Appendages tab's library, import, export
   and pin picking; Calibrate. The fan is in the stack again since 19 Nov evening (answers at `0x18`,
   20% gave 3,720 RPM), so it's the first `i2c` file to test.
5. The mod, on one light first.
6. When the proto boards arrive: the stack, the fan, the buttons, and the expander if needed.
7. The ears: measure first, then build.
8. The build runner for the Firmware tab.

## Open questions

- ~~The LLM module's UART pins on the CoreS3: G17/G18 for sure, or switchable?~~ Read off the
  module's own silkscreen, 18 Nov 2026: on a CoreS3 the default is G18 (its TX column) and G17 (its
  RX column), so **Port C is the module's**. Solder pads on the board can move it to G7/G13, G14/G6
  or G10/G0, and every one of those pairs holds a pin this firmware already uses (G7 and G6 are the
  servo bus, G13 and G14 are audio, G0 is the audio clock and the boot button). The default stays.
- Where the ears listen from: the module's microphone or the CoreS3's.
- Which switch unit, and what is it for?
- ~~The fan: 5 V two-wire, or does it have a tach?~~ Module Fan v1.1: I2C, with RPM readback. So the
  `fan` kind is an I2C register device, not a `pwm` pin, and the primitives need an `i2c` one.
- The build runner: at home or GitHub Actions.
- ~~Sharing: a file passed by hand, or also a shared catalogue?~~ Both: files by hand, and the
  catalogue is a folder in the Soma project (Chad, 19 Nov). See "The catalogue".
- ~~How a curve is written in a file.~~ The chip's response, with the level scale fixed in the `pwm`
  kind (19 Nov). See "What a file looks like".
- The LLM module's three LEDs: reachable from the core at all, or only inside the module's Linux?
  M5's UART doc doesn't say.
- How much of the core's internal RAM the audio path holds: the number that decides whether moving
  the mic and speaker to the LLM module gets the camera back.
