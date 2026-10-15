"""Read-only COM access with bounded busy retries; no commands or exports."""
from __future__ import annotations

import time
from pathlib import Path

import pythoncom

from .acad_core import flag_methods

BUSY_HRESULTS = {-2147418111, -2147417846}  # RPC_E_CALL_REJECTED / RETRYLATER


def retry(call, timeout=12.0, required_metadata=False):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return call()
        except Exception as exc:
            transient=getattr(exc, "hresult", None) in BUSY_HRESULTS or (required_metadata and isinstance(exc,AttributeError))
            if not transient or time.monotonic() >= deadline:
                raise
            pythoncom.PumpWaitingMessages()
            time.sleep(0.25)


def read(obj, name):
    # These inherited members are required for the known document/entity types.
    # New 2014 documents may publish Count before entity type information.
    return retry(lambda: getattr(obj, name),required_metadata=name in ('ObjectName','Handle','Layer'))


def maybe(obj, name, default=None):
    try:
        return read(obj, name)
    except Exception as exc:
        if getattr(exc, "hresult", None) in BUSY_HRESULTS:
            raise
        return default


def method(obj, name, *args):
    # Only [in] methods. GetBoundingBox with [out] must retain type information.
    flag_methods(obj, name)
    return retry(lambda: getattr(obj, name)(*args))


def ready_document(doc, timeout=12.0):
    """Open can return before AutoCAD publishes collection type information.

    Only known, required document/collection members get this AttributeError
    retry. Optional entity fields must still fail immediately when unsupported.
    """
    deadline=time.monotonic()+timeout
    while True:
        try:
            read(doc,'FullName')
            int(read(read(doc,'Layouts'),'Count'))
            int(read(read(doc,'ModelSpace'),'Count'))
            return doc
        except Exception as exc:
            if not isinstance(exc,AttributeError) and getattr(exc,'hresult',None) not in BUSY_HRESULTS:
                raise
            if time.monotonic()>=deadline:
                raise RuntimeError('AutoCAD document collections are not ready; retry after opening completes') from exc
            pythoncom.PumpWaitingMessages()
            time.sleep(0.25)


def open_source(app, path):
    path = Path(path).expanduser().resolve(strict=True)
    if path.suffix.lower() != ".dwg":
        raise ValueError("source must be an original .dwg")
    docs = read(app, "Documents")
    for i in range(int(read(docs, "Count"))):
        doc = method(docs, "Item", i)
        if str(read(doc, "FullName")).casefold() == str(path).casefold():
            if not bool(read(doc, "ReadOnly")):
                raise RuntimeError("This source is already open writable. Close that document before opening it read-only.")
            return ready_document(doc)
    # Check IsQuiescent before Open; an active command/dialog may reject it.
    state = retry(lambda: app.GetAcadState())
    if not read(state, "IsQuiescent"):
        raise RuntimeError("AutoCAD is busy. Finish the active command or dismiss its dialog, then retry.")
    doc = method(docs, "Open", str(path), True)
    if not bool(read(doc, "ReadOnly")):
        raise RuntimeError("AutoCAD did not honour ReadOnly=True")
    return ready_document(doc)
