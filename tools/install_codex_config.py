"""Append the reviewed read-only MCP configuration without altering other settings.

Default is preview. --install writes ~/.codex/config.toml and a sibling backup.
"""
import argparse
import os
import shutil
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    addition = (root/"config"/"codex-mcp.toml").read_text(encoding="utf-8")
    expected = tomllib.loads(addition)["mcp_servers"]["autocad_2014"]
    destination = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))/"config.toml"
    original = destination.read_text(encoding="utf-8") if destination.exists() else ""
    data = tomllib.loads(original)
    if "autocad_2014" in data.get("mcp_servers", {}):
        if data["mcp_servers"]["autocad_2014"] == expected:
            print("Read-only AutoCAD MCP configuration already matches")
            return
        raise RuntimeError("autocad_2014 already exists with different settings; no changes made")
    combined = original.rstrip()+"\n\n"+addition
    parsed = tomllib.loads(combined)
    assert parsed["mcp_servers"]["autocad_2014"] == expected
    assert {k:v for k,v in parsed.get("mcp_servers", {}).items() if k != "autocad_2014"} == data.get("mcp_servers", {})
    if not args.install:
        print(f"Validated append-only configuration for {destination}")
        print("Server: autocad_2014; ReadOnly=1; expected AutoCAD=19.1; timeout=1800 s")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        backup = destination.with_name(destination.name+".autocad-backup-"+datetime.now().strftime("%Y%m%d%H%M%S"))
        shutil.copy2(destination, backup)
    fd, temporary = tempfile.mkstemp(prefix="autocad-mcp-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(combined)
        # Avoid silently replacing a simultaneous user edit.
        if destination.exists() and destination.read_text(encoding="utf-8") != original:
            raise RuntimeError("Codex config changed during installation; no replacement made")
        os.replace(temporary, destination)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    print(f"Installed read-only AutoCAD MCP entry in {destination}")


if __name__ == "__main__":
    main()
