"""Shared setup for the probe/experiment scripts.

Reads the Frame's address and token path from the project's own `config.toml`
(gitignored — copy `config.example.toml` to `config.toml` and set `[frame].host`),
so no address or local path is hardcoded in the repo.
"""
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_cfg_path = ROOT / "config.toml"
if not _cfg_path.exists():
    raise SystemExit(
        f"{_cfg_path} not found — copy config.example.toml to config.toml and set [frame].host"
    )
_frame = tomllib.loads(_cfg_path.read_text())["frame"]

HOST = _frame["host"]
TOKEN_FILE = str(ROOT / _frame.get("token_file", ".token"))

# Where experiment scripts drop small state files (ids to clean up, etc.).
SCRATCH = Path(tempfile.gettempdir()) / "frame-art-organizer-scratch"
SCRATCH.mkdir(parents=True, exist_ok=True)
