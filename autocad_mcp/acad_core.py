# Copyright 2026 JIALE LIU
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared AutoCAD COM helpers and the tool registry for autocad-mcp.

This server deliberately attaches only to an AutoCAD session the user has
already started.  It never starts AutoCAD, loads code into it, or exposes an
arbitrary-command tool.  All geometry at the MCP boundary is expressed in
millimetres; values are converted from/to the drawing's ``INSUNITS`` setting
when it is known.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import math
import os
import threading
from contextlib import contextmanager
from ctypes import wintypes
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import pythoncom
import win32com.client
from win32com.client import VARIANT
from mcp.types import Tool


LOG_PATH = Path(__file__).with_name("server.log")
# Rotating rather than plain: this server is long-lived and every COM failure
# logs a traceback, so an unbounded file grows without anyone watching it.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ],
)
logger = logging.getLogger("autocad-mcp")

# Autodesk documents AutoCAD 2026 as Application.25.1.  The unversioned and
# 25.0-compatible IDs are fallbacks for an installation whose registration was
# repaired or migrated from an earlier release.
PROG_IDS = ("AutoCAD.Application.25.1", "AutoCAD.Application.25", "AutoCAD.Application")

# One drawing unit of each INSUNITS code, expressed in millimetres.  The scale
# factor is derived from this table rather than written directly, because a
# hand-written reciprocal is what previously made six of these wrong: the
# decimetre through gigametre entries had been reciprocated against metres, so
# every one of them was off by exactly 1000x, and a 1 m line came out 1 km long.
# Stated this way each number is checkable against a ruler -- a decimetre is
# 100 mm, an AutoCAD "mil" is one thousandth of an inch, not a micrometre.
MM_PER_DRAWING_UNIT = {
    1: 25.4,           # inches
    2: 304.8,          # feet
    3: 1609344.0,      # miles
    4: 1.0,            # millimetres
    5: 10.0,           # centimetres
    6: 1000.0,         # metres
    7: 1e6,            # kilometres
    8: 2.54e-5,        # microinches
    9: 0.0254,         # mils (thousandths of an inch)
    10: 914.4,         # yards
    11: 1e-7,          # angstroms
    12: 1e-6,          # nanometres
    13: 1e-3,          # microns
    14: 100.0,         # decimetres
    15: 1e4,           # decametres
    16: 1e5,           # hectometres
    17: 1e12,          # gigametres
    18: 1.495978707e14,          # astronomical units
    19: 9.4607304725808e18,      # light years
    20: 3.0856775814913673e19,   # parsecs
}

# AutoCAD's INSUNITS enum -> number of drawing units per millimetre.  Geometry
# in DWG is unitless, so unitless/unknown drawings are passed through 1:1 and
# explicitly reported to the caller instead of silently guessing.
DRAWING_UNITS_PER_MM = {code: 1.0 / mm for code, mm in MM_PER_DRAWING_UNIT.items()}
INSUNITS_NAMES = {
    0: "unitless", 1: "inches", 2: "feet", 3: "miles", 4: "millimetres",
    5: "centimetres", 6: "metres", 7: "kilometres", 8: "microinches",
    9: "mils", 10: "yards", 11: "angstroms", 12: "nanometres",
    13: "microns", 14: "decimetres", 15: "decametres", 16: "hectometres",
    17: "gigametres", 18: "astronomical_units", 19: "light_years", 20: "parsecs",
}

# AcSaveAsType.ac2018_dxf, read from the installed acax25enu.tlb rather than
# copied from a web page: AutoCAD 2018 through 2026 share one DWG format, and
# acNative is ac2018_dwg (64) on this release, so 65 is its DXF counterpart.
AC_2018_DXF = 65

OUTPUT_ROOT = Path(
    os.environ.get("ACAD_MCP_OUTPUT_ROOT") or (Path.home() / "Documents" / "autocad-mcp")
).expanduser().resolve()

TOOLS: list[Tool] = []
HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: Sequence[str] | None = None,
) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Callable[[dict[str, Any]], dict[str, Any]]]:
    """Register one strongly-scoped MCP tool."""

    def decorator(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        TOOLS.append(
            Tool(
                name=name,
                description=description,
                inputSchema={
                    "type": "object",
                    "properties": properties or {},
                    "required": list(required or []),
                    "additionalProperties": False,
                },
            )
        )
        HANDLERS[name] = fn
        return fn

    return decorator


def result(ok: bool, message: str, **data: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": ok, "message": message}
    if data:
        payload["data"] = data
    return payload


def com_error(exc: Exception) -> dict[str, Any]:
    logger.exception("AutoCAD COM failure")
    return result(False, f"AutoCAD COM error: {exc}")


_com_state = threading.local()


@contextmanager
def com_apartment() -> Iterator[None]:
    """Ensure COM is initialised on this thread, once, for the process lifetime.

    This used to pair CoInitialize with CoUninitialize around every single tool
    call.  The last CoUninitialize on a thread tears the COM library back down
    and invalidates every proxy obtained under it, so the pairing forced a fresh
    running-object-table lookup per call and made caching anything impossible.
    A long-lived STA host is meant to initialise once and stay up, which is what
    the adjacent solidworks-mcp server does.
    """

    if not getattr(_com_state, "initialised", False):
        pythoncom.CoInitialize()
        _com_state.initialised = True
    yield


def running_app() -> Any:
    """Return a running AutoCAD instance without ever launching one."""

    errors: list[str] = []
    for prog_id in PROG_IDS:
        try:
            return win32com.client.GetActiveObject(prog_id)
        except Exception as exc:
            errors.append(f"{prog_id}: {exc}")
    # Report what each ProgID actually said.  "AutoCAD is not running" and
    # "AutoCAD is running but its COM registration is broken" need different
    # fixes, and collecting these only to drop them hid exactly that difference.
    detail = "; ".join(errors)
    logger.info("No AutoCAD session found. Attempts: %s", detail)
    raise RuntimeError(
        "No running AutoCAD session was found. Start AutoCAD 2026, open a drawing, "
        f"and call autocad_status again. Connection attempts: {detail}"
    )


def active_document(app: Any) -> Any:
    try:
        return app.ActiveDocument
    except Exception as exc:
        raise RuntimeError("AutoCAD has no active drawing. Open or create a drawing first.") from exc


def optional_value(obj: Any, name: str, fallback: Any = None) -> Any:
    try:
        value = getattr(obj, name)
        return value() if callable(value) and not hasattr(value, "_oleobj_") else value
    except Exception:
        return fallback


def document_info(doc: Any) -> dict[str, Any]:
    try:
        insunits = int(doc.GetVariable("INSUNITS"))
    except Exception:
        insunits = 0
    return {
        "name": str(optional_value(doc, "Name", "")),
        "path": str(optional_value(doc, "FullName", "")),
        "saved": bool(optional_value(doc, "Saved", False)),
        "readonly": bool(optional_value(doc, "ReadOnly", False)),
        "insunits": insunits,
        "insunits_name": INSUNITS_NAMES.get(insunits, f"unknown_{insunits}"),
        "coordinates_contract": "millimetres",
        "unit_warning": (
            "INSUNITS is unitless or unknown; millimetre input is passed through unchanged."
            if insunits not in DRAWING_UNITS_PER_MM
            else None
        ),
    }


def drawing_units_per_mm(doc: Any) -> float:
    try:
        code = int(doc.GetVariable("INSUNITS"))
    except Exception:
        code = 0
    return DRAWING_UNITS_PER_MM.get(code, 1.0)


def number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    # Python's json module parses NaN/Infinity by default, so a client can hand
    # us one.  Passing it on produces geometry AutoCAD cannot represent, and the
    # failure surfaces far from the argument that caused it.
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def positive_number(value: Any, name: str) -> float:
    parsed = number(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def boolean(value: Any, name: str) -> bool:
    # bool(value) would read the string "false" and the number 0.0 as True and
    # False respectively, so a caller's typo would silently set the opposite of
    # what they asked for on a drawing they cannot easily inspect.
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def aci_color(value: Any, name: str = "color_aci") -> int:
    """Validate an AutoCAD Color Index. 0 and 256 are ByBlock/ByLayer, not colours."""

    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
        raise ValueError(f"{name} must be an integer between 1 and 255")
    return value


# Every angle crossing the MCP boundary is degrees; every angle crossing the COM
# boundary is radians.  Each drawing tool reads the degrees with number(), keeps
# them for its reply, and converts with math.radians() at the call itself.


def point_mm(raw: Any, scale: float, name: str) -> tuple[float, float, float]:
    if not isinstance(raw, list) or len(raw) not in (2, 3):
        raise ValueError(f"{name} must be [x_mm, y_mm] or [x_mm, y_mm, z_mm]")
    values = [number(value, f"{name}[{index}]") * scale for index, value in enumerate(raw)]
    return (values[0], values[1], values[2] if len(values) == 3 else 0.0)


def com_point_mm(raw: Any, scale: float, name: str) -> Any:
    """Marshal one point as the double array AutoCAD's ActiveX API requires."""

    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, point_mm(raw, scale, name))


def com_double_array(values: Sequence[float]) -> Any:
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(float(value) for value in values))


def flag_methods(obj: Any, *names: str) -> Any:
    """Force pywin32's late binding to retain argument-taking COM methods.

    ``_FlagAsMethod`` resolves each name with its own ``GetIDsOfNames`` call,
    which is a cross-process round trip, so only ever pass the names a call is
    about to use.  Flagging one name at a time also means an unknown name costs
    only itself: ``_FlagAsMethod(a, b, c)`` abandons b and c when a fails.

    Only use this for methods whose parameters are all [in].  Flagging replaces
    whatever pywin32 built from the type library with a bare dispid that carries
    no parameter descriptions, so a method with [out] parameters would lose the
    information needed to return them.
    """

    for name in names:
        try:
            obj._FlagAsMethod(name)
        except Exception:
            logger.debug("Could not flag AutoCAD method %s", name)
    return obj


def get_space(doc: Any, space: Any = "model", *methods: str) -> Any:
    """Return model or paper space, flagging only the methods the caller needs.

    Deliberately *not* cached.  ``doc.ModelSpace`` is one property access, but a
    cached wrapper outlives the thing it points at: the user can close or switch
    the drawing from the AutoCAD UI between any two MCP calls, and a stale
    wrapper would then add geometry to the wrong drawing -- or a dead one --
    while still reporting success.  Nothing cheap identifies a document safely
    enough to key such a cache: names are reused by successive unsaved drawings
    ("Drawing1.dwg" again after the first is closed), and a raw COM pointer can
    be reused at the same address once the original is released.  Validating a
    cache entry would itself cost the round trip it was meant to save, so the
    property access stays, and only the per-name ``GetIDsOfNames`` calls -- four
    on every single call, whether or not the tool used any of them -- were cut.
    """

    normalized = str(space or "model").lower()
    if normalized == "model":
        target = doc.ModelSpace
    elif normalized == "paper":
        target = doc.PaperSpace
    else:
        raise ValueError("space must be 'model' or 'paper'")
    return flag_methods(target, *methods) if methods else target


def entity_info(
    entity: Any,
    index: int | None = None,
    *,
    object_name: str | None = None,
    layer: str | None = None,
) -> dict[str, Any]:
    """Describe one entity.  Every property read here is a COM round trip.

    ``object_name`` and ``layer`` let a caller that already read those values --
    query_entities reads both to filter on them -- hand them back instead of
    paying for the same two round trips a second time per entity.
    """

    payload: dict[str, Any] = {
        "handle": str(optional_value(entity, "Handle", "")),
        "object_name": str(optional_value(entity, "ObjectName", "")) if object_name is None else object_name,
        "layer": str(optional_value(entity, "Layer", "")) if layer is None else layer,
        "color_aci": optional_value(entity, "Color", None),
        "linetype": str(optional_value(entity, "Linetype", "")),
    }
    if index is not None:
        payload["index"] = index
    return payload


def get_entity_by_handle(doc: Any, handle: Any) -> Any:
    if not isinstance(handle, str) or not handle.strip():
        raise ValueError("handle must be a non-empty AutoCAD entity handle")
    try:
        return doc.HandleToObject(handle.strip())
    except Exception as exc:
        raise RuntimeError(f"No accessible entity exists with handle {handle!r}") from exc


def output_path(relative_path: Any, suffixes: set[str]) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("path must be a non-empty path relative to ACAD_MCP_OUTPUT_ROOT")
    candidate = (OUTPUT_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(OUTPUT_ROOT)
    except ValueError as exc:
        raise ValueError("path must stay under ACAD_MCP_OUTPUT_ROOT") from exc
    if candidate.suffix.lower() not in suffixes:
        accepted = ", ".join(sorted(suffixes))
        raise ValueError(f"path must have one of these extensions: {accepted}")
    return candidate


def discover_template() -> Path | None:
    """Find a local metric AutoCAD template without hard-coding a user locale."""

    configured = os.environ.get("ACAD_MCP_TEMPLATE")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file() and candidate.suffix.lower() == ".dwt":
            return candidate
        raise RuntimeError("ACAD_MCP_TEMPLATE must point to an existing .dwt file")

    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    autodesk_root = local_app_data / "Autodesk"
    if not autodesk_root.is_dir():
        return None

    # The folder contains a localised language component, but template names
    # such as acadiso.dwt are consistent and normally indicate metric units.
    templates = list(autodesk_root.glob("AutoCAD */R*/**/Template"))
    for template_dir in sorted(templates, reverse=True):
        candidate = template_dir / "acadiso.dwt"
        if candidate.is_file():
            return candidate
    for template_dir in sorted(templates, reverse=True):
        candidate = template_dir / "acad.dwt"
        if candidate.is_file():
            return candidate
    for template_dir in sorted(templates, reverse=True):
        choices = sorted(template_dir.glob("*.dwt"))
        if choices:
            return choices[0]
    return None


def _new_entity_result(entity: Any, message: str, **extra: Any) -> dict[str, Any]:
    return result(True, message, entity=entity_info(entity), **extra)


# --------------------------------------------------------------------------
# Session and documents
# --------------------------------------------------------------------------


@tool("autocad_status", "Verify the connection to a running AutoCAD session and report the active drawing.")
def autocad_status(_: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        app = running_app()
        doc = active_document(app)
        return result(
            True,
            "Connected to running AutoCAD.",
            version=str(optional_value(app, "Version", "")),
            caption=str(optional_value(app, "Caption", "")),
            visible=bool(optional_value(app, "Visible", False)),
            active_document=document_info(doc),
        )


@tool("list_open_drawings", "List drawings open in the running AutoCAD session.")
def list_open_drawings(_: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        app = running_app()
        documents = app.Documents
        items = [document_info(documents.Item(index)) for index in range(int(documents.Count))]
        return result(True, f"Found {len(items)} open drawing(s).", drawings=items)


@tool("get_active_drawing_info", "Return metadata and units for the active drawing.")
def get_active_drawing_info(_: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        return result(True, "Active drawing information.", drawing=document_info(active_document(running_app())))


@tool(
    "create_new_drawing",
    "Create and activate a new blank drawing in the running AutoCAD session. Uses a local metric template by default.",
    {"template_path": {"type": "string", "description": "Optional absolute path to an existing .dwt template."}},
)
def create_new_drawing(arguments: dict[str, Any]) -> dict[str, Any]:
    supplied_template = arguments.get("template_path")
    if supplied_template is not None:
        template = Path(str(supplied_template)).expanduser().resolve()
        if not template.is_file() or template.suffix.lower() != ".dwt":
            raise ValueError("template_path must name an existing .dwt file")
    else:
        template = discover_template()
    if template is None:
        raise RuntimeError(
            "No AutoCAD .dwt template was found. Set ACAD_MCP_TEMPLATE or pass template_path explicitly."
        )
    with com_apartment():
        app = running_app()
        doc = app.Documents.Add(str(template))
        return result(True, "Created a new drawing.", drawing=document_info(doc), template=str(template))


@tool(
    "open_drawing",
    "Open an existing local DWG or DXF file in the running AutoCAD session.",
    {"path": {"type": "string", "description": "Absolute local path to an existing .dwg or .dxf file."}},
    ["path"],
)
def open_drawing(arguments: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in {".dwg", ".dxf"}:
        raise ValueError("path must name an existing .dwg or .dxf file")
    with com_apartment():
        doc = running_app().Documents.Open(str(path), False)
        return result(True, "Opened drawing.", drawing=document_info(doc))


@tool("save_active_drawing", "Save the active drawing in place. It must already have a file path.")
def save_active_drawing(_: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        full_name = str(optional_value(doc, "FullName", ""))
        if not full_name:
            raise RuntimeError("The active drawing has not been saved yet; use save_drawing_as.")
        doc.Save()
        return result(True, "Saved active drawing.", drawing=document_info(doc))


@tool(
    "save_drawing_as",
    "Save the active drawing below the configured output root. Existing files are refused unless allow_overwrite is true.",
    {
        "path": {"type": "string", "description": "Relative .dwg or .dxf path below ACAD_MCP_OUTPUT_ROOT."},
        "allow_overwrite": {"type": "boolean", "description": "Allow replacing an existing output file; defaults to false."},
    },
    ["path"],
)
def save_drawing_as(arguments: dict[str, Any]) -> dict[str, Any]:
    target = output_path(arguments["path"], {".dwg", ".dxf"})
    if target.exists() and not bool(arguments.get("allow_overwrite", False)):
        raise RuntimeError(f"Refusing to overwrite {target}; set allow_overwrite to true to replace it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with com_apartment():
        doc = active_document(running_app())
        if target.suffix.lower() == ".dxf":
            # SaveAs falls back to the current DWG format when SaveAsType is
            # omitted, so a .dxf path alone produced DWG content wearing a .dxf
            # extension -- a file no DXF reader would accept.  A .dwg path keeps
            # the default on purpose, so a future release's native format is
            # still picked up without touching this code.
            doc.SaveAs(str(target), AC_2018_DXF)
        else:
            doc.SaveAs(str(target))
        return result(
            True,
            "Saved drawing.",
            path=str(target),
            format="dxf" if target.suffix.lower() == ".dxf" else "dwg",
            drawing=document_info(doc),
        )


# --------------------------------------------------------------------------
# Inspection, layers, and entities
# --------------------------------------------------------------------------


@tool("list_layers", "List layers in the active drawing, including color, lock, freeze, and visibility state.")
def list_layers(_: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        layers = doc.Layers
        items = [layer_state(layers.Item(index)) for index in range(int(layers.Count))]
        return result(True, f"Found {len(items)} layer(s).", layers=items)


@tool(
    "create_layer",
    "Create a layer, or return the existing layer with the same name. ACI color is optional (1-255).",
    {
        "name": {"type": "string"},
        "color_aci": {"type": "integer", "minimum": 1, "maximum": 255},
        "make_current": {"type": "boolean"},
    },
    ["name"],
)
def create_layer(arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments["name"]).strip()
    if not name:
        raise ValueError("name must not be empty")
    # Validated before the layer is created, so a bad colour cannot leave a new
    # layer behind while the call reports a failure.
    color = aci_color(arguments["color_aci"]) if "color_aci" in arguments else None
    with com_apartment():
        doc = active_document(running_app())
        try:
            layer = doc.Layers.Item(name)
            created = False
        except Exception:
            layer = doc.Layers.Add(name)
            created = True
        if color is not None:
            layer.Color = color
        if bool(arguments.get("make_current", False)):
            doc.ActiveLayer = layer
        return result(
            True,
            "Created layer." if created else "Layer already existed.",
            layer={
                "name": str(optional_value(layer, "Name", "")),
                "color_aci": optional_value(layer, "Color", None),
                "current": str(optional_value(doc.ActiveLayer, "Name", "")) == name,
            },
        )


@tool(
    "set_current_layer",
    "Make an existing layer current in the active drawing.",
    {"name": {"type": "string"}},
    ["name"],
)
def set_current_layer(arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments["name"]).strip()
    with com_apartment():
        doc = active_document(running_app())
        try:
            doc.ActiveLayer = doc.Layers.Item(name)
        except Exception as exc:
            raise RuntimeError(f"Layer {name!r} does not exist") from exc
        return result(True, "Set current layer.", name=name)


def layer_state(layer: Any) -> dict[str, Any]:
    return {
        "name": str(optional_value(layer, "Name", "")),
        "color_aci": optional_value(layer, "Color", None),
        "locked": bool(optional_value(layer, "Lock", False)),
        "frozen": bool(optional_value(layer, "Freeze", False)),
        "visible": bool(optional_value(layer, "LayerOn", True)),
        "linetype": str(optional_value(layer, "Linetype", "")),
    }


# IAcadLayer exposes these as Color / Lock / Freeze / LayerOn.  "visible" is
# AutoCAD's LayerOn, which is a different control from Freeze: an off layer is
# still regenerated, a frozen one is not.
LAYER_PROPERTIES = ("color_aci", "locked", "frozen", "visible")


@tool(
    "set_layer_properties",
    "Change colour, lock, freeze, or visibility on an existing layer. Supply at least one property. "
    "'visible' is AutoCAD's LayerOn (off but still regenerated); 'frozen' is the stronger setting.",
    {
        "name": {"type": "string"},
        "color_aci": {"type": "integer", "minimum": 1, "maximum": 255},
        "locked": {"type": "boolean", "description": "Locked layers stay visible but cannot be edited."},
        "frozen": {"type": "boolean", "description": "Frozen layers are hidden and not regenerated."},
        "visible": {"type": "boolean", "description": "False turns the layer off; it stays thawed."},
    },
    ["name"],
)
def set_layer_properties(arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments["name"]).strip()
    if not name:
        raise ValueError("name must not be empty")
    requested = [key for key in LAYER_PROPERTIES if key in arguments]
    if not requested:
        # Without this the call is a no-op that reports success, which reads as
        # "the layer now looks like you asked" when nothing was even attempted.
        raise ValueError("supply at least one of: " + ", ".join(LAYER_PROPERTIES))

    # Every value is parsed before a single one is written, so a rejected
    # argument cannot leave the layer half-updated.
    color = aci_color(arguments["color_aci"]) if "color_aci" in arguments else None
    locked = boolean(arguments["locked"], "locked") if "locked" in arguments else None
    frozen = boolean(arguments["frozen"], "frozen") if "frozen" in arguments else None
    visible = boolean(arguments["visible"], "visible") if "visible" in arguments else None

    with com_apartment():
        doc = active_document(running_app())
        try:
            layer = doc.Layers.Item(name)
        except Exception as exc:
            raise RuntimeError(f"Layer {name!r} does not exist; create it with create_layer first.") from exc
        # AutoCAD matches layer names case-insensitively, so the name that came
        # back from the collection is the canonical one, not necessarily what the
        # caller typed.  Comparing the caller's spelling would miss the guard
        # below whenever the two differ only in case.
        canonical = str(optional_value(layer, "Name", "")) or name
        if frozen:
            # AutoCAD refuses to freeze the current layer, and the COM error it
            # raises names neither the layer nor the reason.  Checking here also
            # means the refusal happens before any other property is written.
            current = str(optional_value(doc.ActiveLayer, "Name", ""))
            if current.casefold() == canonical.casefold():
                raise RuntimeError(
                    f"Layer {canonical!r} is the current layer, and AutoCAD cannot freeze the "
                    "current layer. Make a different layer current with set_current_layer first."
                )
        if color is not None:
            layer.Color = color
        if locked is not None:
            layer.Lock = locked
        if frozen is not None:
            layer.Freeze = frozen
        if visible is not None:
            layer.LayerOn = visible
        return result(True, "Updated layer properties.", layer=layer_state(layer), changed=requested)


# Every entity inspected costs one Item() call plus a property read per field,
# each a cross-process COM round trip, so a filtered query over a drawing with
# tens of thousands of entities can walk the whole space and take minutes.  The
# scan stops after this many entities unless the caller raises it, and says so.
DEFAULT_MAX_SCAN = 5000
MAX_SCAN_CEILING = 200000


@tool(
    "query_entities",
    "List entities in model or paper space. Use returned handles to modify a specific entity. "
    "Scanning stops at max_scan entities; the reply reports how many were actually examined.",
    {
        "space": {"type": "string", "enum": ["model", "paper"], "default": "model"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        "object_name": {"type": "string", "description": "Optional exact AutoCAD COM object name, e.g. AcDbLine."},
        "layer": {"type": "string", "description": "Optional exact layer name."},
        "max_scan": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_SCAN_CEILING,
            "default": DEFAULT_MAX_SCAN,
            "description": "Stop after examining this many entities, even if fewer than limit matched.",
        },
    },
)
def query_entities(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = int(arguments.get("limit", 200))
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    max_scan = int(arguments.get("max_scan", DEFAULT_MAX_SCAN))
    if not 1 <= max_scan <= MAX_SCAN_CEILING:
        raise ValueError(f"max_scan must be between 1 and {MAX_SCAN_CEILING}")
    wanted_object = arguments.get("object_name")
    wanted_layer = arguments.get("layer")
    with com_apartment():
        doc = active_document(running_app())
        # No Add* method is called here, so no name needs flagging.
        collection = get_space(doc, arguments.get("space", "model"))
        total = int(collection.Count)
        items: list[dict[str, Any]] = []
        scanned = 0
        for index in range(min(total, max_scan)):
            scanned = index + 1
            entity = collection.Item(index)
            # Read each property once.  Both are needed for the reply anyway, so
            # filtering on a separate read meant paying for them twice.
            object_name = str(optional_value(entity, "ObjectName", ""))
            if wanted_object and object_name != wanted_object:
                continue
            layer = str(optional_value(entity, "Layer", ""))
            if wanted_layer and layer != wanted_layer:
                continue
            items.append(entity_info(entity, index, object_name=object_name, layer=layer))
            if len(items) >= limit:
                break
        # "Truncated" means the scan stopped short of the end of the space, not
        # that a filter rejected things.  Comparing the result count against the
        # total instead marked every filtered query as truncated, which reads as
        # "there is more to fetch" when the space had already been walked.
        truncated = scanned < total
        return result(
            True,
            f"Returned {len(items)} of {total} entities in the space.",
            entities=items,
            total_in_space=total,
            scanned=scanned,
            truncated=truncated,
            # Which of the two ceilings ended the scan; null when it ran to the
            # end.  Raising the other one would not change anything.
            stopped_by=(("limit" if len(items) >= limit else "max_scan") if truncated else None),
            max_scan=max_scan,
        )


@tool("list_blocks", "List block definitions in the active drawing.")
def list_blocks(_: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        blocks = active_document(running_app()).Blocks
        items = []
        for index in range(int(blocks.Count)):
            block = blocks.Item(index)
            items.append(
                {
                    "name": str(optional_value(block, "Name", "")),
                    "is_layout": bool(optional_value(block, "IsLayout", False)),
                    "is_xref": bool(optional_value(block, "IsXRef", False)),
                    "entity_count": int(optional_value(block, "Count", 0) or 0),
                }
            )
        return result(True, f"Found {len(items)} block definition(s).", blocks=items)


# AcPlotPaperUnits, read from acax25enu.tlb.  A layout reports its paper size in
# whichever of these it is set to, so the number is meaningless without it.
PAPER_UNITS_NAMES = {0: "inches", 1: "millimetres", 2: "pixels"}
MM_PER_PAPER_UNIT = {0: 25.4, 1: 1.0}


def layout_paper_size(layout: Any) -> dict[str, Any]:
    """Report one layout's paper size in millimetres where that is meaningful.

    ``GetPaperSize`` has two [out] parameters and no return value, so pywin32
    hands back a (width, height) pair.  It is deliberately *not* run through
    flag_methods: ``_FlagAsMethod`` replaces the type-library-derived mapping
    with a bare dispid, which drops the parameter descriptions pywin32 needs to
    allocate and return [out] values.  Flagging is for argument-taking methods
    that a dynamic dispatch mistakes for properties, not for this.

    A missing or unconfigured plot device is common -- a template carries a
    .pc3 path from whatever machine authored it -- and it shows up two different
    ways.  The call sometimes raises, and sometimes returns 0 x 0 instead, which
    was observed on a stock acadiso.dwt layout pointing at an AutoCAD 2005
    plotter path.  Both mean "no size available", and both must report null:
    passing the zero straight through would say the sheet is nought millimetres
    wide, which reads as a measurement rather than as an absence.  A layout
    measured in pixels has no millimetre equivalent either, so the units are
    always reported alongside.
    """

    units = optional_value(layout, "PaperUnits", None)
    unit_code = int(units) if isinstance(units, int) and not isinstance(units, bool) else None
    payload: dict[str, Any] = {
        "paper_units": PAPER_UNITS_NAMES.get(unit_code, "unknown") if unit_code is not None else "unknown",
        "width_mm": None,
        "height_mm": None,
    }
    factor = MM_PER_PAPER_UNIT.get(unit_code)
    if factor is None:
        return payload
    try:
        width, height = layout.GetPaperSize()
    except Exception:
        logger.debug("Layout did not report a paper size", exc_info=True)
        return payload
    try:
        width_mm = round(float(width) * factor, 4)
        height_mm = round(float(height) * factor, 4)
    except (TypeError, ValueError):
        logger.debug("Layout reported a non-numeric paper size")
        return payload
    if width_mm <= 0 or height_mm <= 0:
        logger.debug("Layout reported a zero paper size; its plot device is unavailable")
        return payload
    payload["width_mm"] = width_mm
    payload["height_mm"] = height_mm
    return payload


@tool(
    "list_layouts",
    "List the layouts in the active drawing: the Model tab plus each paper-space tab, with plot "
    "device and paper size. Paper sizes are converted to millimetres where the layout reports a "
    "physical unit.",
)
def list_layouts(_: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        active_name = str(optional_value(optional_value(doc, "ActiveLayout", None), "Name", ""))
        layouts = doc.Layouts
        items: list[dict[str, Any]] = []
        for index in range(int(layouts.Count)):
            layout = layouts.Item(index)
            name = str(optional_value(layout, "Name", ""))
            items.append(
                {
                    "name": name,
                    "tab_order": int(optional_value(layout, "TabOrder", 0) or 0),
                    # ModelType is true only for the Model tab; every other entry
                    # is a paper-space layout that query_entities cannot reach,
                    # because "paper" always means the *active* paper space.
                    "is_model_tab": bool(optional_value(layout, "ModelType", False)),
                    "active": bool(name) and name == active_name,
                    "block_name": str(optional_value(optional_value(layout, "Block", None), "Name", "")),
                    "plot_device": str(optional_value(layout, "ConfigName", "")),
                    "canonical_media": str(optional_value(layout, "CanonicalMediaName", "")),
                    "paper": layout_paper_size(layout),
                }
            )
        items.sort(key=lambda item: item["tab_order"])
        return result(True, f"Found {len(items)} layout(s).", layouts=items, active_layout=active_name)


@tool(
    "set_entity_layer",
    "Move one entity identified by its handle to an existing layer.",
    {"handle": {"type": "string"}, "layer": {"type": "string"}},
    ["handle", "layer"],
)
def set_entity_layer(arguments: dict[str, Any]) -> dict[str, Any]:
    layer_name = str(arguments["layer"]).strip()
    with com_apartment():
        doc = active_document(running_app())
        try:
            doc.Layers.Item(layer_name)
        except Exception as exc:
            raise RuntimeError(f"Layer {layer_name!r} does not exist") from exc
        entity = get_entity_by_handle(doc, arguments["handle"])
        entity.Layer = layer_name
        return _new_entity_result(entity, "Updated entity layer.")


# --------------------------------------------------------------------------
# Drawing tools.  Points/lengths are always millimetres at the MCP boundary.
# --------------------------------------------------------------------------


POINT_SCHEMA = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 3,
    "description": "[x_mm, y_mm] or [x_mm, y_mm, z_mm] in millimetres.",
}
SPACE_SCHEMA = {"type": "string", "enum": ["model", "paper"], "default": "model"}
LAYER_SCHEMA = {"type": "string", "description": "Optional existing layer name."}


def resolve_layer(doc: Any, arguments: dict[str, Any]) -> str | None:
    """Validate the requested layer name *before* any geometry is created.

    Assigning a non-existent layer is what raises, and by then the entity is
    already in the drawing sitting on the current layer: the tool would report
    a failure while having changed the drawing anyway.  Checking up front means
    a bad layer name costs nothing, and matches what set_entity_layer does.
    """

    layer = arguments.get("layer")
    if layer is None:
        return None
    if not isinstance(layer, str) or not layer.strip():
        raise ValueError("layer must be a non-empty existing layer name")
    name = layer.strip()
    try:
        doc.Layers.Item(name)
    except Exception as exc:
        raise RuntimeError(f"Layer {name!r} does not exist; create it with create_layer first.") from exc
    return name


def apply_layer(entity: Any, layer_name: str | None) -> None:
    if layer_name:
        entity.Layer = layer_name


@tool(
    "draw_line",
    "Add a line in model or paper space. Coordinates are millimetres.",
    {"start_mm": POINT_SCHEMA, "end_mm": POINT_SCHEMA, "layer": LAYER_SCHEMA, "space": SPACE_SCHEMA},
    ["start_mm", "end_mm"],
)
def draw_line(arguments: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        scale = drawing_units_per_mm(doc)
        layer = resolve_layer(doc, arguments)
        entity = get_space(doc, arguments.get("space"), "AddLine").AddLine(
            com_point_mm(arguments["start_mm"], scale, "start_mm"),
            com_point_mm(arguments["end_mm"], scale, "end_mm"),
        )
        apply_layer(entity, layer)
        return _new_entity_result(entity, "Added line.")


@tool(
    "draw_circle",
    "Add a circle in model or paper space. Center and radius are millimetres.",
    {"center_mm": POINT_SCHEMA, "radius_mm": {"type": "number", "exclusiveMinimum": 0}, "layer": LAYER_SCHEMA, "space": SPACE_SCHEMA},
    ["center_mm", "radius_mm"],
)
def draw_circle(arguments: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        scale = drawing_units_per_mm(doc)
        radius = positive_number(arguments["radius_mm"], "radius_mm")
        layer = resolve_layer(doc, arguments)
        entity = get_space(doc, arguments.get("space"), "AddCircle").AddCircle(
            com_point_mm(arguments["center_mm"], scale, "center_mm"), radius * scale
        )
        apply_layer(entity, layer)
        return _new_entity_result(entity, "Added circle.")


@tool(
    "draw_polyline",
    "Add a lightweight 2D polyline in model or paper space. Vertex coordinates are millimetres.",
    {
        "vertices_mm": {"type": "array", "minItems": 2, "items": POINT_SCHEMA},
        "closed": {"type": "boolean", "default": False},
        "layer": LAYER_SCHEMA,
        "space": SPACE_SCHEMA,
    },
    ["vertices_mm"],
)
def draw_polyline(arguments: dict[str, Any]) -> dict[str, Any]:
    vertices = arguments["vertices_mm"]
    if not isinstance(vertices, list) or len(vertices) < 2:
        raise ValueError("vertices_mm must contain at least two vertices")
    with com_apartment():
        doc = active_document(running_app())
        scale = drawing_units_per_mm(doc)
        layer = resolve_layer(doc, arguments)
        coordinates: list[float] = []
        elevations: set[float] = set()
        for index, vertex in enumerate(vertices):
            point = point_mm(vertex, scale, f"vertices_mm[{index}]")
            coordinates.extend((point[0], point[1]))
            elevations.add(round(point[2], 9))
        # A lightweight polyline is planar by construction: it carries a single
        # Elevation rather than a z per vertex.  Accepting three-element points
        # and then dropping the z put the geometry on the wrong plane without
        # ever saying so, so a constant z becomes the elevation and a varying
        # one is refused instead of quietly flattened.
        if len(elevations) > 1:
            raise ValueError(
                "vertices_mm must share a single z value, because a lightweight polyline is planar. "
                "Give every vertex the same z, or draw separate lines for a non-planar path."
            )
        elevation = elevations.pop() if elevations else 0.0
        entity = get_space(doc, arguments.get("space"), "AddLightWeightPolyline").AddLightWeightPolyline(
            com_double_array(coordinates)
        )
        if elevation:
            entity.Elevation = elevation
        entity.Closed = bool(arguments.get("closed", False))
        apply_layer(entity, layer)
        return _new_entity_result(
            entity,
            "Added lightweight polyline.",
            vertices=len(vertices),
            elevation_mm=round(elevation / scale, 6),
        )


@tool(
    "draw_text",
    "Add single-line text in model or paper space. Point and height are millimetres.",
    {
        "text": {"type": "string", "minLength": 1},
        "insertion_point_mm": POINT_SCHEMA,
        "height_mm": {"type": "number", "exclusiveMinimum": 0},
        "layer": LAYER_SCHEMA,
        "space": SPACE_SCHEMA,
    },
    ["text", "insertion_point_mm", "height_mm"],
)
def draw_text(arguments: dict[str, Any]) -> dict[str, Any]:
    text = arguments["text"]
    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string")
    with com_apartment():
        doc = active_document(running_app())
        scale = drawing_units_per_mm(doc)
        height = positive_number(arguments["height_mm"], "height_mm")
        layer = resolve_layer(doc, arguments)
        entity = get_space(doc, arguments.get("space"), "AddText").AddText(
            text, com_point_mm(arguments["insertion_point_mm"], scale, "insertion_point_mm"), height * scale
        )
        apply_layer(entity, layer)
        return _new_entity_result(entity, "Added text.")


@tool(
    "draw_arc",
    "Add an arc in model or paper space. Centre and radius are millimetres. Angles are degrees "
    "measured counter-clockwise from the positive X axis, and the arc always runs counter-clockwise "
    "from start_angle_deg to end_angle_deg.",
    {
        "center_mm": POINT_SCHEMA,
        "radius_mm": {"type": "number", "exclusiveMinimum": 0},
        "start_angle_deg": {"type": "number", "description": "Degrees CCW from +X."},
        "end_angle_deg": {"type": "number", "description": "Degrees CCW from +X; the arc sweeps CCW to here."},
        "layer": LAYER_SCHEMA,
        "space": SPACE_SCHEMA,
    },
    ["center_mm", "radius_mm", "start_angle_deg", "end_angle_deg"],
)
def draw_arc(arguments: dict[str, Any]) -> dict[str, Any]:
    radius = positive_number(arguments["radius_mm"], "radius_mm")
    start_deg = number(arguments["start_angle_deg"], "start_angle_deg")
    end_deg = number(arguments["end_angle_deg"], "end_angle_deg")
    # AutoCAD sweeps counter-clockwise from start to end, so the useful quantity
    # is the CCW difference.  Equal angles -- and a 360 degree difference, which
    # is the same thing -- describe no arc at all rather than a full circle.
    sweep_deg = (end_deg - start_deg) % 360.0
    if sweep_deg == 0.0:
        raise ValueError(
            "start_angle_deg and end_angle_deg describe a zero-degree sweep. "
            "Use draw_circle for a full circle."
        )
    with com_apartment():
        doc = active_document(running_app())
        units_per_mm = drawing_units_per_mm(doc)
        layer = resolve_layer(doc, arguments)
        entity = get_space(doc, arguments.get("space"), "AddArc").AddArc(
            com_point_mm(arguments["center_mm"], units_per_mm, "center_mm"),
            radius * units_per_mm,
            math.radians(start_deg),
            math.radians(end_deg),
        )
        apply_layer(entity, layer)
        return _new_entity_result(entity, "Added arc.", sweep_deg=round(sweep_deg, 9))


@tool(
    "draw_ellipse",
    "Add a full ellipse in model or paper space. The axis lengths are semi-axes: the millimetre "
    "distance from the centre to the end of each axis, not the full width and height.",
    {
        "center_mm": POINT_SCHEMA,
        "major_axis_mm": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "Semi-major axis: centre to the far end of the long axis, in mm.",
        },
        "minor_axis_mm": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "Semi-minor axis in mm. Must not exceed major_axis_mm.",
        },
        "rotation_deg": {
            "type": "number",
            "default": 0.0,
            "description": "Rotates the major axis counter-clockwise from the positive X axis.",
        },
        "layer": LAYER_SCHEMA,
        "space": SPACE_SCHEMA,
    },
    ["center_mm", "major_axis_mm", "minor_axis_mm"],
)
def draw_ellipse(arguments: dict[str, Any]) -> dict[str, Any]:
    major = positive_number(arguments["major_axis_mm"], "major_axis_mm")
    minor = positive_number(arguments["minor_axis_mm"], "minor_axis_mm")
    if minor > major:
        raise ValueError(
            "minor_axis_mm must not exceed major_axis_mm. AutoCAD's RadiusRatio is minor/major and "
            "cannot exceed 1; swap the two lengths and add 90 to rotation_deg for the same ellipse."
        )
    rotation_deg = number(arguments.get("rotation_deg", 0.0), "rotation_deg")
    with com_apartment():
        doc = active_document(running_app())
        units_per_mm = drawing_units_per_mm(doc)
        layer = resolve_layer(doc, arguments)
        # AddEllipse takes MajorAxis as a *vector from the centre* to the end of
        # the major axis -- not a length and not an absolute point -- so the
        # rotation is carried by the direction of that vector.
        radians = math.radians(rotation_deg)
        major_axis = (
            major * units_per_mm * math.cos(radians),
            major * units_per_mm * math.sin(radians),
            0.0,
        )
        entity = get_space(doc, arguments.get("space"), "AddEllipse").AddEllipse(
            com_point_mm(arguments["center_mm"], units_per_mm, "center_mm"),
            com_double_array(major_axis),
            minor / major,
        )
        apply_layer(entity, layer)
        return _new_entity_result(
            entity,
            "Added ellipse.",
            radius_ratio=round(minor / major, 9),
            rotation_deg=rotation_deg,
        )


@tool(
    "draw_point",
    "Add a point entity in model or paper space. AutoCAD draws points using the drawing's PDMODE "
    "and PDSIZE settings, so with the default PDMODE of 0 a point renders as a single dot.",
    {"point_mm": POINT_SCHEMA, "layer": LAYER_SCHEMA, "space": SPACE_SCHEMA},
    ["point_mm"],
)
def draw_point(arguments: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        units_per_mm = drawing_units_per_mm(doc)
        layer = resolve_layer(doc, arguments)
        entity = get_space(doc, arguments.get("space"), "AddPoint").AddPoint(
            com_point_mm(arguments["point_mm"], units_per_mm, "point_mm")
        )
        apply_layer(entity, layer)
        return _new_entity_result(entity, "Added point.")


@tool(
    "insert_block",
    "Insert a reference to a block definition that already exists in the drawing; use list_blocks "
    "for the available names. The insertion point is millimetres, but scale is a unitless "
    "multiplier applied to the block's own geometry, so 1 means actual size.",
    {
        "block_name": {"type": "string", "description": "Existing block definition name."},
        "insertion_point_mm": POINT_SCHEMA,
        "scale": {"type": "number", "default": 1.0, "description": "Uniform scale multiplier; non-zero."},
        "rotation_deg": {"type": "number", "default": 0.0, "description": "Degrees CCW about the insertion point."},
        "layer": LAYER_SCHEMA,
        "space": SPACE_SCHEMA,
    },
    ["block_name", "insertion_point_mm"],
)
def insert_block(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_name = arguments["block_name"]
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("block_name must be a non-empty existing block definition name")
    block_name = raw_name.strip()
    block_scale = number(arguments.get("scale", 1.0), "scale")
    if block_scale == 0:
        raise ValueError("scale must not be zero")
    rotation_deg = number(arguments.get("rotation_deg", 0.0), "rotation_deg")
    with com_apartment():
        doc = active_document(running_app())
        units_per_mm = drawing_units_per_mm(doc)
        # Checked up front for the same reason as resolve_layer: InsertBlock
        # fails on an unknown name with a COM error that names neither the block
        # nor the problem, and by then the call has already begun.
        try:
            block = doc.Blocks.Item(block_name)
        except Exception as exc:
            raise RuntimeError(
                f"Block {block_name!r} does not exist in this drawing; list_blocks shows the available names."
            ) from exc
        if bool(optional_value(block, "IsLayout", False)):
            raise ValueError(
                f"Block {block_name!r} is a layout block (model or paper space itself), "
                "which cannot be inserted as a block reference."
            )
        layer = resolve_layer(doc, arguments)
        entity = get_space(doc, arguments.get("space"), "InsertBlock").InsertBlock(
            com_point_mm(arguments["insertion_point_mm"], units_per_mm, "insertion_point_mm"),
            block_name,
            block_scale,
            block_scale,
            block_scale,
            math.radians(rotation_deg),
        )
        apply_layer(entity, layer)
        return _new_entity_result(
            entity,
            "Inserted block reference.",
            block_name=block_name,
            scale=block_scale,
            rotation_deg=rotation_deg,
            is_xref=bool(optional_value(block, "IsXRef", False)),
        )


# 'aligned' measures the true distance between the two points; the others fix the
# measurement direction and are all AddDimRotated with a different angle.
DIMENSION_ORIENTATIONS = ("aligned", "horizontal", "vertical", "rotated")
FIXED_DIMENSION_ROTATIONS = {"horizontal": 0.0, "vertical": 90.0}


@tool(
    "add_linear_dimension",
    "Dimension the distance between two points. 'aligned' measures the true distance; 'horizontal' "
    "and 'vertical' measure the X and Y components; 'rotated' measures along rotation_deg. All "
    "points are millimetres, and the measured value is reported back in millimetres.",
    {
        "start_mm": POINT_SCHEMA,
        "end_mm": POINT_SCHEMA,
        "dimension_line_point_mm": {
            **POINT_SCHEMA,
            "description": "Where the dimension line sits, in mm. Offset it from the measured points.",
        },
        "orientation": {"type": "string", "enum": list(DIMENSION_ORIENTATIONS), "default": "aligned"},
        "rotation_deg": {
            "type": "number",
            "description": "Measurement direction in degrees. Required for, and only valid with, orientation 'rotated'.",
        },
        "layer": LAYER_SCHEMA,
        "space": SPACE_SCHEMA,
    },
    ["start_mm", "end_mm", "dimension_line_point_mm"],
)
def add_linear_dimension(arguments: dict[str, Any]) -> dict[str, Any]:
    orientation = str(arguments.get("orientation", "aligned")).lower()
    if orientation not in DIMENSION_ORIENTATIONS:
        raise ValueError("orientation must be one of: " + ", ".join(DIMENSION_ORIENTATIONS))
    if orientation == "rotated":
        if "rotation_deg" not in arguments:
            raise ValueError("rotation_deg is required when orientation is 'rotated'")
        rotation_deg = number(arguments["rotation_deg"], "rotation_deg")
    else:
        if "rotation_deg" in arguments:
            # Silently ignoring it would produce a dimension measured along an
            # axis the caller did not ask for, with no sign anything was dropped.
            raise ValueError("rotation_deg applies only when orientation is 'rotated'")
        rotation_deg = FIXED_DIMENSION_ROTATIONS.get(orientation)

    with com_apartment():
        doc = active_document(running_app())
        units_per_mm = drawing_units_per_mm(doc)
        start = point_mm(arguments["start_mm"], units_per_mm, "start_mm")
        end = point_mm(arguments["end_mm"], units_per_mm, "end_mm")
        if start == end:
            raise ValueError("start_mm and end_mm must be different points")
        location = com_point_mm(
            arguments["dimension_line_point_mm"], units_per_mm, "dimension_line_point_mm"
        )
        layer = resolve_layer(doc, arguments)
        if orientation == "aligned":
            entity = get_space(doc, arguments.get("space"), "AddDimAligned").AddDimAligned(
                com_double_array(start), com_double_array(end), location
            )
        else:
            entity = get_space(doc, arguments.get("space"), "AddDimRotated").AddDimRotated(
                com_double_array(start),
                com_double_array(end),
                location,
                math.radians(rotation_deg),
            )
        apply_layer(entity, layer)
        # Measurement is the value AutoCAD itself computed, in drawing units, so
        # converting it back to millimetres is a check on the whole round trip.
        measured = optional_value(entity, "Measurement", None)
        measured_mm = (
            round(float(measured) / units_per_mm, 6)
            if isinstance(measured, (int, float)) and not isinstance(measured, bool)
            else None
        )
        return _new_entity_result(
            entity,
            "Added linear dimension.",
            orientation=orientation,
            rotation_deg=rotation_deg,
            measurement_mm=measured_mm,
        )


# --------------------------------------------------------------------------
# Editing existing entities, always addressed by their stable AutoCAD handle
# --------------------------------------------------------------------------


@tool(
    "erase_entity",
    "Delete one entity from the active drawing, identified by its handle. The deletion is a normal "
    "AutoCAD edit, so it is undoable in AutoCAD itself, but this server cannot reverse it.",
    {"handle": {"type": "string", "description": "Entity handle from query_entities or a draw tool."}},
    ["handle"],
)
def erase_entity(arguments: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        entity = get_entity_by_handle(doc, arguments["handle"])
        # Described before it is deleted: once Delete returns, the wrapper points
        # at an object that is gone and every property read raises, so the reply
        # could not say what was actually removed.
        info = entity_info(entity)
        flag_methods(entity, "Delete").Delete()
        return result(True, "Deleted entity.", entity=info)


@tool(
    "move_entity",
    "Move one entity by the vector from from_mm to to_mm. To move by a displacement, pass [0,0] "
    "and the displacement itself.",
    {
        "handle": {"type": "string"},
        "from_mm": {**POINT_SCHEMA, "description": "Base point of the move, in mm."},
        "to_mm": {**POINT_SCHEMA, "description": "Destination of the base point, in mm."},
    },
    ["handle", "from_mm", "to_mm"],
)
def move_entity(arguments: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        units_per_mm = drawing_units_per_mm(doc)
        # Points parsed before the handle is resolved: bad coordinates should
        # cost nothing and touch nothing.
        start = point_mm(arguments["from_mm"], units_per_mm, "from_mm")
        end = point_mm(arguments["to_mm"], units_per_mm, "to_mm")
        entity = get_entity_by_handle(doc, arguments["handle"])
        flag_methods(entity, "Move").Move(com_double_array(start), com_double_array(end))
        return _new_entity_result(
            entity,
            "Moved entity.",
            displacement_mm=[round((e - s) / units_per_mm, 6) for s, e in zip(start, end)],
        )


@tool(
    "copy_entity",
    "Copy one entity. The copy is created in the same space as the original and, without a "
    "displacement, sits exactly on top of it.",
    {
        "handle": {"type": "string"},
        "displacement_mm": {
            **POINT_SCHEMA,
            "description": "Optional offset applied to the copy, in mm. Omit to leave it in place.",
        },
    },
    ["handle"],
)
def copy_entity(arguments: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        doc = active_document(running_app())
        units_per_mm = drawing_units_per_mm(doc)
        displacement = (
            point_mm(arguments["displacement_mm"], units_per_mm, "displacement_mm")
            if arguments.get("displacement_mm") is not None
            else None
        )
        entity = get_entity_by_handle(doc, arguments["handle"])
        source = entity_info(entity)
        duplicate = flag_methods(entity, "Copy").Copy()
        if displacement is not None and any(displacement):
            flag_methods(duplicate, "Move").Move(
                com_double_array((0.0, 0.0, 0.0)), com_double_array(displacement)
            )
        return _new_entity_result(duplicate, "Copied entity.", source=source)


@tool("zoom_extents", "Zoom the active AutoCAD view so visible drawing content fits the window.")
def zoom_extents(_: dict[str, Any]) -> dict[str, Any]:
    with com_apartment():
        running_app().ZoomExtents()
        return result(True, "Zoomed to extents.")


@tool(
    "capture_screenshot",
    "Capture the running AutoCAD application window and return it as an MCP image. This reads the "
    "desktop, so the window must be restored and unobscured; the reply says so when it was not.",
    {
        "width_px": {
            "type": "integer",
            "minimum": 200,
            "maximum": 2400,
            "default": 1400,
            "description": "Downscale the capture to this width. Full-resolution captures are large.",
        }
    },
)
def capture_screenshot(arguments: dict[str, Any]) -> dict[str, Any]:
    # Pillow is imported here, not at module scope, so every inspection tool
    # still works on an installation without a usable desktop-capture stack.
    try:
        from PIL import Image, ImageGrab
    except Exception as exc:
        raise RuntimeError(f"Pillow is required for screenshots but could not be imported: {exc}") from exc

    with com_apartment():
        hwnd = int(optional_value(running_app(), "HWND", 0) or 0)
    if not hwnd:
        raise RuntimeError("AutoCAD did not expose a window handle for screenshot capture.")

    user32 = ctypes.windll.user32
    # Declaring the signatures matters on 64-bit Python: left to guess, ctypes
    # marshals the window handle as a 32-bit int and truncates it, and the call
    # then fails with no indication that the handle was the problem.
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND

    window = wintypes.HWND(hwnd)
    if user32.IsIconic(window):
        raise RuntimeError(
            "The AutoCAD window is minimised, so there is nothing on screen to capture. "
            "Restore the window and call this again."
        )
    rect = wintypes.RECT()
    if not user32.GetWindowRect(window, ctypes.byref(rect)):
        raise RuntimeError("Could not read the AutoCAD window bounds.")
    if rect.right <= rect.left or rect.bottom <= rect.top:
        raise RuntimeError("AutoCAD reported an empty window rectangle; the window may be off-screen.")

    try:
        image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    except Exception as exc:
        raise RuntimeError(f"Could not capture the AutoCAD window: {exc}") from exc

    width = int(arguments.get("width_px", 1400))
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)

    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()

    # A desktop grab returns whatever pixels are on screen, so an overlapping
    # window silently becomes "the drawing".  Say when AutoCAD was not in front.
    in_front = int(user32.GetForegroundWindow() or 0) == hwnd
    payload = result(
        True,
        "Captured AutoCAD window."
        if in_front
        else "Captured AutoCAD window, but it was not the foreground window, so another window may cover it.",
        width=image.width,
        height=image.height,
        bytes=len(data),
        was_foreground=in_front,
    )
    payload["_image_png_base64"] = base64.b64encode(data).decode("ascii")
    return payload
