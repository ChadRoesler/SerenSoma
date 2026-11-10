
## Build

### Fetch Dependencies

```bash
python3 ./fetch_repos.py
```

### Tool Chains

[ESP-IDF v6.1](https://docs.espressif.com/projects/esp-idf/en/v6.1/esp32s3/index.html)

### Build

```bash
idf.py build
```

### Host-side tests

The motion coordinate helpers can be tested without ESP-IDF hardware:

```bash
cmake -S tests -B build-host-tests
cmake --build build-host-tests
ctest --test-dir build-host-tests --output-on-failure
```

### Flash

```bash
idf.py flash
```

## Per-robot settings: `soma.yaml`

Wi-Fi, the voice server's address, the control token and the tailnet key come
from `soma.yaml` on the SD card, read once at boot, saved to NVS and deleted.
See [`soma.yaml.sample`](soma.yaml.sample). The matching `CONFIG_*` values in
`sdkconfig.defaults.local` still work as the build's defaults, for your own
builds; a release build leaves them blank. Parsing lives in
[`main/hal/soma_config.cpp`](main/hal/soma_config.cpp).

## Releases

A `fw-v*` tag (`fw-v0.1.0`) builds in the ESP-IDF 6.1 container from a clean
checkout, fails if the build carries a token, tailnet key or check-in address,
and attaches two images to a GitHub Release: `-full.bin` (one file at `0x0`,
for a new robot, erases saved settings) and `-update.zip` (the parts and
`flash_args`, keeps them). See
[`.github/workflows/firmware-release.yml`](../.github/workflows/firmware-release.yml).
The package's `v*` tags and the firmware's `fw-v*` tags never build each other.

## Customization

### Custom faces

Draw your own face as layered PNGs and install it with one command -- no C++.
The firmware composes all 21 assistant emotions from a handful of eyelid shapes
and mouth curves, and animates them with the existing blink, lip sync, breathing
and gaze systems. Falls back to the stock face if none is installed.

```bash
seren-psycheion face init my_face        # starter face.yaml (pip install seren-psycheion)
seren-psycheion face preview my_face     # render all 21 emotions, no hardware
seren-psycheion face build my_face       # convert, into my_face/build
idf.py build && idf.py flash
```

`preview` composites your artwork exactly the way the device layers it, so the
draw-check-redraw loop takes a second instead of a reflash.

Full guide: [docs/custom-faces.md](docs/custom-faces.md)
