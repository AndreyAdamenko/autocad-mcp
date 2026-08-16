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

"""Inspect the locally installed AutoCAD ActiveX type library.

Use this instead of trusting a method signature remembered from documentation
or another AutoCAD release.  Every signature this server calls was read here
first, which is how `AcSaveAsType.ac2018_dxf = 65` and the `[out]` parameters on
`GetPaperSize` were established rather than guessed.

    python tools\\acax_probe.py list                    -> every interface and enum
    python tools\\acax_probe.py iface IAcadModelSpace   -> one interface's members
    python tools\\acax_probe.py find AddArc             -> which interfaces expose a name
    python tools\\acax_probe.py enum AcSaveAsType       -> an enum's members

Set ACAD_MCP_TYPELIB to point at a specific .tlb; otherwise the newest
acax*.tlb under the shared Autodesk folder is used, whatever its language
suffix (acax25enu.tlb, acax25chs.tlb, ...).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pythoncom

SHARED = Path(r"C:\Program Files\Common Files\Autodesk Shared")

VT = {
    0: "VT_EMPTY", 1: "VT_NULL", 2: "VT_I2", 3: "VT_I4", 4: "VT_R4", 5: "VT_R8",
    6: "VT_CY", 7: "VT_DATE", 8: "VT_BSTR", 9: "VT_DISPATCH", 10: "VT_ERROR",
    11: "VT_BOOL", 12: "VT_VARIANT", 13: "VT_UNKNOWN", 14: "VT_DECIMAL",
    16: "VT_I1", 17: "VT_UI1", 18: "VT_UI2", 19: "VT_UI4", 20: "VT_I8",
    21: "VT_UI8", 22: "VT_INT", 23: "VT_UINT", 24: "VT_VOID", 25: "VT_HRESULT",
    26: "VT_PTR", 27: "VT_SAFEARRAY", 28: "VT_CARRAY", 29: "VT_USERDEFINED",
    36: "VT_RECORD",
}

INVOKE_KINDS = {1: "METHOD", 2: "PROPGET", 4: "PROPPUT", 8: "PROPPUTREF"}

# PARAMFLAGS, from oaidl.h.  "out" is the one that matters most here: a method
# with [out] parameters must never be passed to _FlagAsMethod.
PARAM_FLAGS = {0x01: "in", 0x02: "out", 0x04: "lcid", 0x08: "retval", 0x10: "opt", 0x20: "hasdefault"}


def find_library() -> Path:
    override = os.environ.get("ACAD_MCP_TYPELIB")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise SystemExit(f"ACAD_MCP_TYPELIB does not name a file: {candidate}")
        return candidate
    if SHARED.is_dir():
        # Newest release first; the language suffix does not change the API.
        for candidate in sorted(SHARED.glob("acax*.tlb"), reverse=True):
            return candidate
    raise SystemExit(
        f"No acax*.tlb found under {SHARED}. Set ACAD_MCP_TYPELIB to the type library."
    )


def vt_name(vartype: object) -> str:
    if isinstance(vartype, tuple):
        inner = vt_name(vartype[1]) if len(vartype) > 1 else "?"
        return f"{vt_name(vartype[0])}<{inner}>"
    return VT.get(vartype, f"vt({vartype})")


def flag_names(flags: int) -> str:
    return ",".join(name for bit, name in sorted(PARAM_FLAGS.items()) if flags & bit) or "-"


def each_type(library: object):
    for index in range(library.GetTypeInfoCount()):
        yield library.GetDocumentation(index)[0], library.GetTypeInfo(index)


def signature(type_info: object, func_desc: object) -> tuple[str, str, str]:
    names = type_info.GetNames(func_desc.memid)
    parameters = []
    for position, arg in enumerate(func_desc.args):
        name = names[position + 1] if position + 1 < len(names) else f"arg{position}"
        parameters.append(f"{name}:{vt_name(arg[0])}[{flag_names(arg[1])}]")
    kind = INVOKE_KINDS.get(func_desc.invkind, str(func_desc.invkind))
    return kind, names[0], ", ".join(parameters)


def dump_interface(library: object, wanted: str) -> None:
    found = False
    for name, type_info in each_type(library):
        if name.lower() != wanted.lower():
            continue
        found = True
        attr = type_info.GetTypeAttr()
        print(f"=== {name} (funcs={attr.cFuncs} vars={attr.cVars}) ===")
        for index in range(attr.cFuncs):
            func_desc = type_info.GetFuncDesc(index)
            kind, member, parameters = signature(type_info, func_desc)
            returns = vt_name(func_desc.rettype[0] if func_desc.rettype else 24)
            print(f"  {kind:<9} {member:<28} ({parameters}) -> {returns}")
    if not found:
        print(f"NOT FOUND: {wanted}")


def find_member(library: object, needle: str) -> None:
    lowered = needle.lower()
    for name, type_info in each_type(library):
        attr = type_info.GetTypeAttr()
        for index in range(attr.cFuncs):
            func_desc = type_info.GetFuncDesc(index)
            kind, member, parameters = signature(type_info, func_desc)
            if lowered in member.lower():
                print(f"{name:<26} {kind:<9} {member:<26} ({parameters})")


def dump_enum(library: object, wanted: str) -> None:
    for name, type_info in each_type(library):
        attr = type_info.GetTypeAttr()
        if wanted.lower() not in name.lower() or not attr.cVars:
            continue
        print(f"=== {name} ===")
        for index in range(attr.cVars):
            var_desc = type_info.GetVarDesc(index)
            print(f"  {type_info.GetNames(var_desc.memid)[0]:<40} = {var_desc.value}")


def main() -> None:
    library = pythoncom.LoadTypeLib(str(find_library()))
    command = sys.argv[1] if len(sys.argv) > 1 else "list"
    arguments = sys.argv[2:]
    if command == "list":
        for name, _ in each_type(library):
            print(name)
    elif command == "iface":
        for wanted in arguments:
            dump_interface(library, wanted)
    elif command == "find":
        for wanted in arguments:
            find_member(library, wanted)
    elif command == "enum":
        for wanted in arguments:
            dump_enum(library, wanted)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
