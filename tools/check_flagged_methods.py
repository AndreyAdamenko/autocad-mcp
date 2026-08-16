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

"""Check every method acad_core flags against the installed type library.

``_FlagAsMethod`` replaces the type-library-derived mapping for a name with a
bare dispid, which discards the parameter description along with it.  For a
method whose parameters are all ``[in]`` that is exactly what we want -- it
stops pywin32 resolving an argument-taking member as a zero-argument property.
For a method with ``[out]`` parameters it is destructive: the output parameters
stop coming back.  ``IAcadLayout.GetPaperSize`` has two of them, and flagging it
was caught here rather than in production.

So this asserts two things about every flagged name:

1. it exists in the type library at all (a typo silently flags nothing), and
2. none of its parameters are ``[out]``.

    python tools\\check_flagged_methods.py

Exits non-zero when something is wrong, so it can gate a change.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acax_probe import find_library  # noqa: E402  (path set above)

import pythoncom  # noqa: E402

PARAMFLAG_OUT = 0x02
SOURCE = Path(__file__).resolve().parents[1] / "autocad_mcp" / "acad_core.py"


def flagged_names(source: str) -> set[str]:
    """Every literal method name the module hands to flag_methods.

    Two call shapes reach it: ``flag_methods(obj, "Name")`` directly, and
    ``get_space(doc, space, "Name")``, which forwards its trailing arguments.
    Both are read a line at a time, and only PascalCase literals are taken --
    that drops the ``"model"`` / ``"paper"`` space argument without this having
    to know parameter positions.

    Reading source text means a name assembled at runtime would go unseen.
    Every call site passes a literal today, which is what makes this worth
    running; if that stops being true, this check quietly narrows.
    """
    names: set[str] = set()
    for call in re.finditer(r"(?:flag_methods|get_space)\([^\n]*", source):
        names.update(re.findall(r'"([A-Z]\w+)"', call.group(0)))
    return names


def describe(names: set[str]) -> dict[str, set[tuple[str, int, tuple[int, ...]]]]:
    library = pythoncom.LoadTypeLib(str(find_library()))
    found: dict[str, set[tuple[str, int, tuple[int, ...]]]] = {}
    for index in range(library.GetTypeInfoCount()):
        interface = library.GetDocumentation(index)[0]
        type_info = library.GetTypeInfo(index)
        attr = type_info.GetTypeAttr()
        for position in range(attr.cFuncs):
            func_desc = type_info.GetFuncDesc(position)
            name = type_info.GetNames(func_desc.memid)[0]
            if name not in names:
                continue
            outs = tuple(i for i, arg in enumerate(func_desc.args) if arg[1] & PARAMFLAG_OUT)
            found.setdefault(name, set()).add((interface, len(func_desc.args), outs))
    return found


def main() -> int:
    names = flagged_names(SOURCE.read_text(encoding="utf-8"))
    if not names:
        print("No flagged names found in acad_core.py -- has flag_methods been renamed?")
        return 1
    found = describe(names)

    print(f"{'name':<22} {'interface':<24} {'args':>4}  out-params")
    problems: list[str] = []
    for name in sorted(names):
        entries = found.get(name)
        if not entries:
            print(f"{name:<22} NOT PRESENT IN THE TYPE LIBRARY")
            problems.append(f"{name}: not in the type library")
            continue
        for interface, count, outs in sorted(entries):
            note = "   <== HAS [out] PARAMS, MUST NOT BE FLAGGED" if outs else ""
            print(f"{name:<22} {interface:<24} {count:>4}  {outs or '-'}{note}")
            if outs:
                problems.append(f"{name}: [out] parameters on {interface}")

    if problems:
        print("\nPROBLEMS:")
        for problem in sorted(set(problems)):
            print(f"  - {problem}")
        return 1
    print(f"\nOK - all {len(names)} flagged methods exist and take only [in] parameters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
