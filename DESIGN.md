# Design direction — PetrichorLog

Committed, and consumed by the whole product (the CLI reads it from the `THEME`
dict at the top of `petrichorlog.py`, and the docs use the same wording).

| Token | Value | Where it is used |
| --- | --- | --- |
| Accent (CSS) | `oklch(0.58 0.12 198)` | registered in `THEME["accent_oklch"]` |
| Accent (non-CSS) | `#008f94` | `THEME["accent_hex"]` — for any tool that cannot parse `oklch()` (charts, canvas, native widgets, terminal themes) |
| Surface / mode | terminal dark | `THEME["surface"]` — a terminal is the only honest surface for a CLI |
| Type | monospace | `THEME["type"]` — the terminal's system monospace; nothing is downloaded or bundled |
| Rule / separator | `─` | `THEME["rule"]` — every table and panel is framed with the same box-drawing rule |
| Bar | `█` | `THEME["bar"]` — the season visualiser's single mark |

## What this means in practice

* **Archetype: cli.** One binary, help-first output, everything reachable from the
  argument parser. There is no HTML, no browser surface and no server.
* **Style family: calm-precise.** Short labels, fixed columns, no decoration. Every
  value is aligned: `id / date / time / rain / intensity / minutes` in `log list`,
  `month / rain mm / bar / events / intensity / moisture` in `log season`.
* **Density: compact.** One event per line in lists, one month per line in the
  season table; detail views use one `key   value` line per field.
* **Signature: aligned columns, one accent for status, no colour-only signal.**
  PetrichorLog never emits ANSI colour (the plan excludes colour output), so
  nothing depends on a palette being visible: status is carried by words
  (`overridden`, `as suggested`), by the presence of a bar, and by the sign of the
  delta (`+20 percent`). The accent is available for host tooling through
  `THEME["accent_hex"]` (e.g. colourising the `intensity` column in a terminal
  theme, or a chart of the same data) without the CLI depending on it.
* **Shape language: monospace, no chrome; box-drawing separators.** Tables are
  framed with `─` rules of matching width, never with ASCII `+---+` art or blank
  padding, and the same rule is reused for every surface.
* **Layout: aligned terminal columns, help-first output.** A bare `petrichorlog`
  prints the banner, the intensity scale and the full usage block before anything
  else; every subcommand has its own `--help` with worked examples.
* **No second palette, no theme picker, no remote assets.** The CLI loads no fonts,
  no icons and no images — it renders with the terminal's own monospace face.
