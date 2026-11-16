from contextlib import nullcontext

import pytest

from frame_art_organizer import cli


@pytest.mark.parametrize("value", ["0", "-1", "1.5"])
def test_batch_operation_limit_is_positive_integer(value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["schedule-refresh", "--batch-operations="+value])


@pytest.mark.parametrize("errors", [0, 1])
def test_bounded_cli_reports_paused_batch_and_failure_status(conn, monkeypatch, capsys, errors):
    monkeypatch.setattr(cli, "_library", lambda args: (conn, {}, {}))
    monkeypatch.setattr(cli, "_client", lambda args: (nullcontext(), {}))
    monkeypatch.setattr(cli, "_ensure_device", lambda *a: 1)
    monkeypatch.setattr(cli, "_render_pending", lambda *a: 0)
    def refresh(*a, **kwargs):
        assert kwargs["max_operations"] == 3 and kwargs["max_seconds"] == 30
        return {"desired": 3, "added": 1, "removed": 0, "errors": errors, "harvested": 0,
                "interval": 15, "shuffle": True, "uploads": [], "mattes_used": {},
                "pending_adds": 2, "pending_removals": 3, "complete": False,
                "batch_id": 7, "removals_skipped": bool(errors)}
    monkeypatch.setattr(cli.scheduler, "refresh", refresh)
    assert cli.main(["schedule-refresh", "--batch-operations", "3"]) == (1 if errors else 0)
    output = capsys.readouterr().out
    assert "batch 7 paused" in output and "next refresh resumes this selection" in output


def test_cli_emits_start_and_end_observation_summaries(conn, monkeypatch, caplog):
    import json
    import logging
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(cli, "_library", lambda args: (conn, {}, {}))
    monkeypatch.setattr(cli, "_client", lambda args: (nullcontext(), {}))
    monkeypatch.setattr(cli, "_ensure_device", lambda *a: 1)
    monkeypatch.setattr(cli, "_render_pending", lambda *a: 0)
    def fail(*a, **kw): raise RuntimeError("test failure")
    monkeypatch.setattr(cli.scheduler, "refresh", fail)
    with pytest.raises(RuntimeError): cli.main(["schedule-refresh", "--batch-operations", "3"])
    rows = [json.loads(r.getMessage()[8:]) for r in caplog.records if r.getMessage().startswith("observe ")]
    summaries = [r for r in rows if r["event"] == "observation.summary"]
    assert [r["reason"] for r in summaries] == ["command_start", "command_end"]
    assert all(r["scope"] == "cli.schedule-refresh" for r in summaries)
