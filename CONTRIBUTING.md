# Contributing

## Generic, reproducible documentation

The repository describes a reusable application, not an individual deployment.
Keep README examples, specifications, API notes, scripts, comments, and deployment
templates independent of a particular person's environment.

- Do not publish real hostnames, network addresses, MAC addresses, device UUIDs,
  serial numbers, pairing tokens, usernames, home-directory paths, or private share names.
- Do not record personal hardware ownership, physical installations, library inventories,
  actual photo/content IDs, or dated production troubleshooting/session histories.
  Preserve the general technical finding and the procedure needed to reproduce it instead.
- Use reserved names such as `tv.example` and `organizer.example`, or clearly marked
  placeholders such as `/path/to/frame-art-organizer`, in setup examples.
- Use the RFC 5737 documentation address ranges for synthetic IPv4 test fixtures and
  the RFC 3849 documentation range for synthetic IPv6 fixtures. Loopback and unspecified
  bind addresses are appropriate when demonstrating those specific behaviors.
- Describe relevant software/API versions and platform requirements without tying them
  to a contributor's machines. Keep firmware-specific observations explicitly qualified;
  anonymizing a test does not make its result universal.
- Keep `config.toml`, pairing tokens, private notes, library databases, photos, backups,
  and raw diagnostic captures outside tracked files. Review generated logs and screenshots
  for identifying information before sharing them.
- Leave required copyright and license notices intact.

Use the configurable paths and host settings rather than adding machine-specific defaults.
The example systemd unit is a template: its account and installation paths must be adapted
by whoever installs the application.

## Validation

Install the test dependencies and run the suite from the checkout:

```sh
python3 -m venv .venv
./.venv/bin/pip install -e '.[web,dev]'
./.venv/bin/python -m pytest -q
```

Tests must not contact a real TV by default. Use synthetic metadata and temporary or
in-memory databases. For database portability, validate SQL on an older supported
SQLite runtime as well as a current one; the gallery compatibility tests use only
`unittest` and can run without pytest.

Documentation privacy checks cover public Markdown, the example configuration, and
deployment templates. These checks complement manual review; they cannot identify every
form of personal or deployment-specific information.

Removing details from the current files does not remove earlier Git history. Any history
rewrite needs separate coordination and authorization; do not force-push a cleanup by default.
