"""Configuration lookup must not depend on editable vs site-packages installation."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from frame_art_organizer import cli, config


def write_config(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.toml"
    path.write_text('[frame]\nhost="192.0.2.10"\ntoken_file="pairing.token"\n'
                    '[paths]\ndatabase="state/state.db"\n')
    return path


def test_default_config_uses_invocation_directory_not_import_directory(tmp_path, monkeypatch):
    # config was already imported above, before changing working directory.
    path = write_config(tmp_path / "appliance")
    monkeypatch.chdir(path.parent)
    assert config.load()["frame"]["host"] == "192.0.2.10"
    args = cli.build_parser().parse_args(["info"])
    fc, _ = cli._client(args)  # Construction only: no sockets or workers.
    assert fc.cfg.token_file == str(path.parent / "pairing.token")
    assert fc._process is None


def test_explicit_config_resolves_storage_and_token_relative_to_config(tmp_path, monkeypatch):
    path = write_config(tmp_path / "appliance")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    args = cli.build_parser().parse_args(["--config", str(path), "info"])
    fc, cfg = cli._client(args)
    assert fc.cfg.token_file == str(path.parent / "pairing.token")
    assert config.paths(cfg, path.parent)["database"] == path.parent / "state/state.db"
    assert fc._process is None


@pytest.mark.parametrize("explicit", [False, True])
def test_noneditable_site_packages_layout_finds_external_config(tmp_path, explicit):
    site = tmp_path / "venv/lib/python/site-packages"
    # Import the real application from a non-editable layout in a fresh process.
    shutil.copytree(Path(config.__file__).parent, site / "frame_art_organizer",
                    ignore=shutil.ignore_patterns("__pycache__"))
    path = write_config(tmp_path / "appliance")
    cwd = path.parent
    if explicit:
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from frame_art_organizer import cli, config
assert Path(config.__file__).is_relative_to(Path(sys.argv[1]))
args = cli.build_parser().parse_args(sys.argv[2:])
fc, cfg = cli._client(args)
assert cfg['frame']['host'] == '192.0.2.10'
assert Path(fc.cfg.token_file) == Path(args.config).resolve().parent / 'pairing.token'
assert fc._process is None
print('installed configuration lookup passed')
"""
    args = ["--config", str(path)] if explicit else []
    result = subprocess.run([sys.executable, "-c", script, str(site), *args, "info"],
                            cwd=cwd, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "installed configuration lookup passed" in result.stdout
