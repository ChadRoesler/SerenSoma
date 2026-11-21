# The catalogue

The base appendage files Soma ships with: parts that someone has wired up, measured and written
down, so the next robot can import them instead of starting from nothing. One file per part,
named `<maker>-<part>.appendage.yaml`. The format is in `firmware/docs/appendages.md`, under
"Appendage files".

| File | Part | Kind |
|---|---|---|
| `m5-flashlight-u152.appendage.yaml` | M5 Unit FlashLight (U152), stock | `pwm` |
| `m5-module-fan-v11.appendage.yaml` | M5 Module Fan v1.1 (M013-V11) | `i2c` |

To add one: make the part work on your robot, export its file from the Appendages tab, and send it
in. A file says what it was measured with, so the next person knows how far to trust it.

These are drafts. Nothing reads them yet.
