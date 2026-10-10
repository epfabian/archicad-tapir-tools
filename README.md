# Archicad Tapir Tools

A small collection of Python scripts for Archicad, built on the [Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation) add-on, to speed up repetitive drafting tasks.

## Scripts

| Script | Description |
|---|---|
| [`embankment.py`](scripts/embankment.py) | Draws embankment hatching between two selected edges: alternating full- and half-length strokes at a chosen (or last-used) spacing, with optional mitered corners. Works with lines, polylines, arcs and circles. All strokes are created as one group on an `Embankment Hatch` layer and can be undone in one step. |

## Requirements

- Archicad 29
- [Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation) add-on
- [uv](https://docs.astral.sh/uv/) (used by Tapir to run Python scripts)

## Installation

1. Install Tapir and restart Archicad.
2. Copy the scripts from the `scripts` folder to `Documents\Tapir\custom-scripts`. Tapir lists them automatically in its palette.

## Usage

**Embankment hatch:** select the top and bottom edge, run `embankment.py` from the Tapir palette, enter the spacing, click **Next**, then click the top edge. Esc cancels.

The scripts follow Archicad's UI language (German or English).

## License

Released under the [MIT License](LICENSE).

## Author

**Fabian Epple**
