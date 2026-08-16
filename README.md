# autocad-mcp

`autocad-mcp` is a local, stdio-only MCP server that drives a **running**
AutoCAD 2026 session through Autodesk's documented ActiveX/COM API. It follows
the same model as its sibling
[`solidworks-mcp`](https://github.com/limuzi013/solidworks-mcp):

- It attaches to AutoCAD already running on the Windows desktop; it never starts
  the application.
- It exposes scoped tools, not an arbitrary AutoCAD command execution endpoint.
- COM calls are serialised, because AutoCAD's automation session is single-threaded.
- All MCP geometry input and output coordinates are in **millimetres**, and all
  angles are in **degrees**.
- Written DWG/DXF files are restricted to `ACAD_MCP_OUTPUT_ROOT`.

<!-- mcp-name: io.github.limuzi013/autocad-mcp -->

## Units and angles

AutoCAD stores model geometry as numbers plus a drawing-unit setting. The server
reads `INSUNITS` and converts millimetre inputs to that setting (for example,
1,000 mm becomes 1 drawing unit when `INSUNITS=metres`). Unitless drawings are
passed through unchanged and flagged in `get_active_drawing_info`.

Angles cross the MCP boundary as **degrees measured counter-clockwise from the
positive X axis**, and are converted to the radians AutoCAD's ActiveX API
expects. Values that are ratios or multipliers rather than measurements — an
ellipse's radius ratio, a block insertion's `scale` — are unitless and are *not*
converted: a block's own geometry is already in drawing units, so `scale: 1`
inserts it at actual size whatever `INSUNITS` says.

## Requirements

- Windows with **full AutoCAD 2026** installed (not AutoCAD LT)
- AutoCAD open with an active drawing before calling a tool
- Python 3.10+

## Install

```powershell
git clone https://github.com/limuzi013/autocad-mcp.git
cd autocad-mcp
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install .
```

That puts an `autocad-mcp` command in the environment, which is what the MCP
host runs. `uv` works too, if you prefer it:

```powershell
uv tool install --from git+https://github.com/limuzi013/autocad-mcp autocad-mcp
```

### Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "command": "C:\\path\\to\\.venv\\Scripts\\autocad-mcp.exe"
    }
  }
}
```

### Codex CLI (`~/.codex/config.toml`)

```toml
[mcp_servers.autocad-mcp]
command = 'C:\path\to\.venv\Scripts\autocad-mcp.exe'
```

Use single-quoted TOML strings so backslashes survive. Restart the MCP host after
editing its configuration.

An MCP host can also be pointed straight at a checkout, with nothing installed
but the dependencies — `server.py` at the repository root exists for exactly
that:

```toml
[mcp_servers.autocad-mcp]
command = 'C:\path\to\.venv\Scripts\python.exe'
args = ['C:\path\to\autocad-mcp\server.py']
```

`solidworks-mcp` needs the same three packages, so a single virtual
environment can serve both servers.

## Tools

| Area | Tools |
| --- | --- |
| Connection | `autocad_status`, `list_open_drawings`, `get_active_drawing_info` |
| Documents | `create_new_drawing`, `open_drawing`, `save_active_drawing`, `save_drawing_as` |
| Drawing state | `list_layers`, `create_layer`, `set_current_layer`, `set_layer_properties`, `list_blocks`, `list_layouts`, `query_entities`, `set_entity_layer` |
| Geometry | `draw_line`, `draw_circle`, `draw_arc`, `draw_ellipse`, `draw_polyline`, `draw_point`, `draw_text`, `insert_block`, `add_linear_dimension` |
| Editing | `erase_entity`, `move_entity`, `copy_entity` |
| View and feedback | `zoom_extents`, `capture_screenshot` |

Every ActiveX signature these tools call was read from the installed
`acax25enu.tlb` by type-library reflection rather than from documentation, and
the flagged-method list is checked against it (see *Method flagging* below).

### Inspection

`query_entities` returns stable AutoCAD handles; use one in `set_entity_layer`,
`erase_entity`, `move_entity`, or `copy_entity` to target a particular object
without relying on UI selection.

Walking a space is expensive — each entity costs an `Item()` call plus a
property read per reported field, and every one of those is a cross-process COM
round trip — so the scan stops after `max_scan` entities (default 5,000). The
reply always says what actually happened:

- `total_in_space` — how many entities the space holds.
- `scanned` — how many were actually examined.
- `truncated` — true when `scanned < total_in_space`, i.e. the scan stopped
  short of the end. A filter that rejected entities after a *full* walk is not
  truncation, and does not set this.
- `stopped_by` — `"limit"`, `"max_scan"`, or null when the scan ran to the end.
  Raising the other ceiling would not change the result.

`list_layouts` reports the Model tab and each paper-space tab in tab order, with
the plot device and the paper size converted to millimetres. A layout whose plot
device is missing cannot report a size; that layout comes back with a null
`width_mm` rather than failing the whole listing. A layout measured in pixels
has no millimetre equivalent, so `paper_units` is always reported alongside.

Note that `space: "paper"` always means the *active* paper space. `list_layouts`
shows which layout that currently is, but this server does not switch layouts.

### Geometry

A `layer` passed to a drawing tool is checked before the entity is created, so
a bad layer name cannot leave new geometry sitting on the current layer while
the call reports a failure. `insert_block` checks the block name the same way.

`draw_polyline` creates a *lightweight* polyline, which is planar by
construction. Give every vertex the same z and it becomes the polyline's
elevation; a varying z is refused rather than silently flattened onto z=0.

`draw_arc` sweeps counter-clockwise from `start_angle_deg` to `end_angle_deg`
and reports the resulting `sweep_deg`. Equal angles — and a 360° difference,
which is the same thing — describe no arc at all and are refused; use
`draw_circle` for a full circle.

`draw_ellipse` takes **semi-axes**: the distance from the centre to the end of
each axis, not the full width and height. `minor_axis_mm` may not exceed
`major_axis_mm`, because AutoCAD's underlying `RadiusRatio` is minor/major and
cannot exceed 1; swap the two and add 90 to `rotation_deg` for the same ellipse.

`insert_block` inserts a reference to a block definition that already exists in
the drawing (`list_blocks` lists them). Layout blocks such as `*Model_Space` are
refused, because they are the spaces themselves. Scaling is uniform.

`add_linear_dimension` measures between two points. `aligned` measures the true
distance; `horizontal` and `vertical` measure the X and Y components; `rotated`
measures along `rotation_deg`, which is required for — and rejected outside of —
that orientation. The reply carries `measurement_mm`, the value AutoCAD itself
computed, converted back to millimetres.

`draw_point` creates a point entity. AutoCAD renders points using the drawing's
`PDMODE`/`PDSIZE`, so with the default `PDMODE` of 0 a point is a single dot.

### Editing

`erase_entity`, `move_entity`, and `copy_entity` address one entity by handle.
`erase_entity` reads the entity's description before deleting it, so the reply
can say what was removed. `move_entity` takes a from/to pair — pass `[0,0]` and
`[dx,dy]` to move by a displacement — and reports the displacement back in
millimetres. `copy_entity` places the copy in the same space as the original,
on top of it unless a `displacement_mm` is given.

Deletion is a normal AutoCAD edit and remains undoable in AutoCAD itself, but
this server cannot reverse it.

`set_layer_properties` changes colour, lock, freeze, and visibility on an
existing layer, and requires at least one of them so that a call cannot report
success having done nothing. `visible` is AutoCAD's `LayerOn` (hidden but still
regenerated); `frozen` is the stronger setting. Freezing the *current* layer is
refused up front with an explanation, because AutoCAD rejects it with a COM
error that names neither the layer nor the reason.

`capture_screenshot` grabs the desktop region occupied by the AutoCAD window,
so the window must be restored and unobscured. The reply carries
`was_foreground: false` when something may have been covering it, and
`width_px` downscales the image.

## Performance notes

Two costs dominate, and both are cross-process COM round trips.

**Method flagging.** pywin32's `_FlagAsMethod` resolves each name with its own
`GetIDsOfNames` call. `get_space` used to flag four names on every call —
including `query_entities`, which calls none of them. It now flags only the one
method the caller is about to use, one name at a time so that an unrecognised
name costs only itself rather than abandoning the rest of the list. Flagging is
only ever applied to methods whose parameters are all `[in]`: it replaces the
type-library-derived mapping with a bare dispid, which would discard the
parameter descriptions a method with `[out]` parameters needs to return them.
That is why `IAcadLayout.GetPaperSize` is deliberately *not* flagged.
`tools/check_flagged_methods.py` checks both properties — that every flagged
name exists at all, and that none of them take `[out]` parameters — against the
installed type library, and exits non-zero when either fails.

**Space lookup is not cached, on purpose.** `doc.ModelSpace` is re-read on every
call. A cached wrapper outlives what it points at: the user can close or switch
the drawing from the AutoCAD UI between any two MCP calls, and a stale wrapper
would then add geometry to the wrong drawing — or a dead one — while still
reporting success. Nothing cheap identifies a document safely enough to key such
a cache, either: names are reused by successive unsaved drawings (a new
`Drawing1.dwg` after the first is closed), and a released COM pointer can be
reused at the same address. Validating an entry would cost the round trip the
cache was meant to save. A slow correct answer beats a fast wrong one when the
wrong one silently edits the user's other drawing.

## Safety and current scope

- `save_drawing_as` accepts only a relative `.dwg` / `.dxf` path and rejects
  paths outside `ACAD_MCP_OUTPUT_ROOT`; it also refuses overwrites by default.
  A `.dxf` target is written with the DXF file type passed explicitly, because
  `Document.SaveAs` otherwise saves the current DWG format under whatever name
  it is handed — producing a file named `.dxf` that no DXF reader accepts.
- `open_drawing` only opens existing `.dwg` / `.dxf` files.
- There is intentionally no `run_command` / `SendCommand` tool. Such a tool
  would let an MCP client execute arbitrary LISP, scripts, or shell-adjacent
  commands inside AutoCAD.
- `erase_entity` deletes drawing content. It is scoped to a single handle the
  caller must already have obtained, and cannot delete layers, blocks, or
  layouts.
- This version targets 2D entities, dimensions, block insertion, layer/block/
  layout inspection, document lifecycle, and screenshot feedback. Hatches,
  plotting, layout switching, xref attachment, and 3D solids can be added as
  separate, tested tools.

## Tests

The suite fakes the COM surface, so it runs on any machine — AutoCAD does not
have to be installed, let alone running:

```powershell
cd C:\path\to\autocad-mcp
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

The unit-conversion tests carry their own table of millimetres-per-unit written
from the physical definitions, so a wrong factor in `acad_core` cannot agree
with a wrong expectation in the test.

What the offline suite does and does not establish: it checks argument
validation, millimetre/degree conversion, the exact values marshalled into each
COM call, that a rejected argument leaves the drawing untouched, and how many
round trips each call pays for. It cannot establish that AutoCAD accepts those
calls. Signatures, parameter order, and enum values are verified by reflecting
the installed `acax25enu.tlb`; behaviour that only a live session can show — how
`GetPaperSize` marshals its `[out]` pair in practice, whether a layer is locked,
whether a dimension style renders as expected — is not covered by these tests.

## Live verification

Exercised on 2026-08-15 against a running AutoCAD 2026 (25.1s, Chinese UI) in a
drawing created from the local `acadiso.dwt`. Each check compared what the tool
reported against what the drawing actually held, read back through COM
independently of the tool that wrote it — a tool reporting success proves
nothing on its own.

- Millimetre conversion under a non-default `INSUNITS`: with the drawing set to
  decimetres, a 1,000 mm line measured 10 drawing units.
- `draw_arc` takes degrees and reaches AutoCAD in radians: a 0°–90° arc came
  back with `StartAngle` 0 and `EndAngle` π/2.
- `draw_ellipse` semi-axes: 60 × 30 produced `RadiusRatio` 0.5 and a 60-unit
  major axis.
- `add_linear_dimension` reports millimetres, `rotated` included — 30° across a
  100 mm span measured 86.60254.
- `copy_entity` and `move_entity` land where the reply says they do.
- `insert_block` works with the trailing optional `Password` argument omitted.
- `save_drawing_as` writes a genuine DXF: the file opens `0\r\nSECTION`, not a
  DWG signature.
- `capture_screenshot` returns a PNG instead of raising.
- Validation refuses zero-sweep arcs, inverted ellipse axes, a zero radius,
  `rotation_deg` outside `rotated`, unknown handles, and unknown blocks.

One defect surfaced and was fixed here: an unavailable plot device does not
always raise from `GetPaperSize`, it can also return 0 × 0, which used to be
reported as a zero-width sheet instead of an unknown one.

## Type-library tools

Every ActiveX signature this server calls was read from the installed type
library rather than from documentation or memory. The same tools are how you
check the next one:

```powershell
python tools\acax_probe.py iface IAcadModelSpace   # one interface's members and parameter flags
python tools\acax_probe.py find AddArc             # which interfaces expose a name
python tools\acax_probe.py enum AcSaveAsType       # an enum's real values
python tools\check_flagged_methods.py              # gate: every flagged method is all-[in]
```

`ACAD_MCP_TYPELIB` overrides the library location; otherwise the newest
`acax*.tlb` under `C:\Program Files\Common Files\Autodesk Shared` is used,
whatever its language suffix.

## Quick smoke check

With AutoCAD open, send:

```json
{"name":"autocad_status","arguments":{}}
```

Then a minimal drawing flow is:

```jsonc
create_new_drawing {}
create_layer {"name":"MCP-GEOMETRY", "color_aci":4, "make_current":true}
draw_polyline {"vertices_mm":[[0,0],[100,0],[100,60],[0,60]], "closed":true}
draw_circle {"center_mm":[50,30], "radius_mm":12}
draw_arc {"center_mm":[50,30], "radius_mm":20, "start_angle_deg":0, "end_angle_deg":180}
draw_ellipse {"center_mm":[50,30], "major_axis_mm":40, "minor_axis_mm":15, "rotation_deg":30}
draw_point {"point_mm":[50,30]}
draw_text {"text":"AutoCAD MCP", "insertion_point_mm":[10,70], "height_mm":5}
add_linear_dimension {"start_mm":[0,0], "end_mm":[100,0], "dimension_line_point_mm":[50,-15],
                      "orientation":"horizontal"}
zoom_extents {}
capture_screenshot {}
save_drawing_as {"path":"examples\\first-mcp-drawing.dwg"}
```

Then exercise inspection and editing against the handles that came back:

```jsonc
query_entities {"object_name":"AcDbCircle"}        // -> handles, scanned, stopped_by
list_layouts {}
set_layer_properties {"name":"MCP-GEOMETRY", "color_aci":1, "locked":true}
copy_entity {"handle":"<handle>", "displacement_mm":[150,0]}
move_entity {"handle":"<copy handle>", "from_mm":[0,0], "to_mm":[0,80]}
erase_entity {"handle":"<copy handle>"}
```

---

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 JIALE LIU.

Use it, change it, ship it in a commercial product; that is all allowed. What
the license does require, if you redistribute this or anything derived from it,
is that you keep the copyright notices, pass on a copy of the [NOTICE](NOTICE)
file, and state which files you changed.
