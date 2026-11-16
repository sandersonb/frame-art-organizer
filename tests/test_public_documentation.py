"""Public setup examples must not contain identifying deployment details."""
import ipaddress
from pathlib import Path
import re
import tomllib

import pytest


ROOT = Path(__file__).parents[1]
PUBLIC_FILES = sorted({
    *ROOT.glob('*.md'),
    *(ROOT / 'docs').rglob('*.md'),
    ROOT / 'config.example.toml',
    *(ROOT / 'deploy').glob('*.service'),
})
PRIVATE_V4 = tuple(ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
    '169.254.0.0/16', '100.64.0.0/10',
))
IPV4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
IPV6 = re.compile(r'(?i)(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]*(?![\w:])')
MAC = re.compile(r'(?i)\b[0-9a-f]{2}(?P<sep>[:-])(?:[0-9a-f]{2}(?P=sep)){4}[0-9a-f]{2}\b')
HOME_PATH = re.compile(r'/(?:Users|home)/[^/\s`<>]+')


@pytest.mark.parametrize('path', PUBLIC_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_public_examples_do_not_contain_private_network_addresses(path):
    text = path.read_text()
    for candidate in IPV4.findall(text):
        try:
            address = ipaddress.IPv4Address(candidate)
        except ipaddress.AddressValueError:
            continue
        assert not any(address in network for network in PRIVATE_V4), (
            f'{path.name}: use a reserved example hostname instead of a private address'
        )
    for candidate in IPV6.findall(text):
        try:
            address = ipaddress.IPv6Address(candidate)
        except ipaddress.AddressValueError:
            continue
        allowed = (address.is_loopback or address.is_unspecified
                   or address in ipaddress.ip_network('2001:db8::/32'))
        assert allowed or not address.is_private, f'{path.name}: omit local IPv6 addresses'


@pytest.mark.parametrize('path', PUBLIC_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_public_examples_do_not_contain_literal_mac_addresses_or_user_home_paths(path):
    text = path.read_text()
    assert not MAC.search(text), f'{path.name}: omit device addresses from public examples'
    assert not HOME_PATH.search(text), f'{path.name}: use a generic installation-path placeholder'


def test_example_config_uses_a_reserved_hostname():
    cfg = tomllib.loads((ROOT / 'config.example.toml').read_text())
    assert cfg['frame']['host'].endswith('.example')


def test_systemd_unit_has_generic_account_and_explicit_placeholder_paths():
    text = (ROOT / 'deploy/frame-art-organizer.service').read_text()
    assert 'User=frame-art-organizer\n' in text
    assert 'WorkingDirectory=/path/to/frame-art-organizer\n' in text
    assert '--config /path/to/frame-art-organizer/config.toml ' in text


def test_deployment_config_tokens_and_captures_are_ignored():
    patterns = set((ROOT / '.gitignore').read_text().splitlines())
    assert {'config.toml', '.token', '.token*', '*.log', '*.jsonl', 'backups/'} <= patterns
