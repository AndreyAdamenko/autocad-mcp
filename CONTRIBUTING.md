# Contributing

## What the tests can and cannot tell you

`tests/test_acad_core.py` fakes the COM surface, so it runs on any machine —
AutoCAD does not have to be installed, let alone running. CI runs it on Windows
across Python 3.10–3.13.

What the offline suite establishes: argument validation, millimetre/degree
conversion, the exact values marshalled into each COM call, that a rejected
argument leaves the drawing untouched, and how many round trips each call pays
for.

What it cannot establish: that AutoCAD accepts those calls. Behaviour only a
live session shows — how `GetPaperSize` marshals its `[out]` pair in practice,
whether a layer is locked, whether a dimension style renders as expected — has
to be checked by hand against a running AutoCAD, reading the drawing back
through COM independently of the tool that wrote it. A tool reporting success
proves nothing on its own.

## Check the type library before writing a tool

Every ActiveX signature this server calls was read from the installed type
library rather than from documentation or memory. The same tools are how you
check the next one:

```powershell
python tools\acax_probe.py iface IAcadModelSpace   # members and parameter flags
python tools\acax_probe.py find AddArc             # which interfaces expose a name
python tools\acax_probe.py enum AcSaveAsType       # an enum's real values
python tools\check_flagged_methods.py              # gate: every flagged method is all-[in]
```

`check_flagged_methods.py` exits non-zero if a flagged name does not exist, or
if it takes `[out]` parameters. Flagging replaces the type-library-derived
mapping with a bare dispid, which discards the parameter descriptions an
`[out]` parameter needs to come back — which is why `IAcadLayout.GetPaperSize`
is deliberately not flagged. Run it after touching the flagged list.

## House rules

- **Units at the boundary.** Lengths are millimetres and angles are degrees in
  every MCP argument and every reply. Conversion to drawing units happens once,
  against `INSUNITS`. Ratios and multipliers — an ellipse's radius ratio, a
  block insertion's `scale` — are unitless and must not be converted.
- **Validate before mutating.** A bad layer name must be rejected before the
  entity is created, so a failed call cannot leave new geometry on the wrong
  layer.
- **Do not cache the space.** `doc.ModelSpace` is re-read on every call on
  purpose: the user can close or switch the drawing between any two MCP calls,
  and a stale wrapper would edit the wrong drawing while reporting success.
- **Say what actually happened.** `query_entities` reports `scanned`,
  `truncated`, and `stopped_by` rather than implying a full walk. A layout whose
  plot device is missing reports a null size, not a zero one.

## Why `mcp` is capped below 2.0

`mcp` 2.0 removed the low-level `Server.list_tools()` / `Server.call_tool()`
decorator API this server is built on, and renamed `Tool.inputSchema` to
`input_schema` (keeping the old name only as a serialisation alias). Importing
`autocad_mcp.server` against 2.0 raises
`AttributeError: 'Server' object has no attribute 'list_tools'`, so the
dependency is pinned `<2` until someone ports it.

CI imports the server module as its own step for this reason. The unit tests
only reach `acad_core`, so they stayed green while the entry point was broken —
which is how this was missed the first time.

## Scope

There is intentionally no `run_command` / `SendCommand` tool, and a pull request
adding one will not be merged: it would let an MCP client execute arbitrary
LISP, scripts, or shell-adjacent commands inside the user's AutoCAD session.
The server attaches to a session the user already has open, never starts the
application, and never touches the network.
