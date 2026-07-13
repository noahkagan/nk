from __future__ import annotations

import json
import logging

import pytest

from nk import output


@pytest.fixture(autouse=True)
def fixed_terminal_time(monkeypatch) -> None:
    monkeypatch.setattr(
        output.TerminalFormatter,
        "formatTime",
        lambda self, record, datefmt=None: "12:34:56",
    )


def test_terminal_handler_aligns_structured_workspace_events(capsys) -> None:
    output.configure(("short@node", "long-workspace@node"), verbose=True)

    output.event("CHECKING", "run state", workspace="short@node", verbose=True)
    output.event("UNAVAILABLE", "offline", workspace="long-workspace@node", error=True)

    captured = capsys.readouterr()
    assert captured.out == "12:34:56\tshort@node         \tCHECKING\trun state\n"
    assert captured.err == "12:34:56\tlong-workspace@node\tUNAVAILABLE\toffline\n"


def test_verbose_event_is_hidden_by_default(capsys) -> None:
    output.configure(("author@node",))

    output.event("CHECKING", "run state", workspace="author@node", verbose=True)

    assert capsys.readouterr().out == ""


def test_terminal_handler_preserves_subprocess_line_body(capsys) -> None:
    output.configure(("author@node",))

    output.line("[INFO] bootstrap: preparing", workspace="author@node")

    assert capsys.readouterr().out == (
        "12:34:56\tauthor@node\t[INFO] bootstrap: preparing\n"
    )


def test_cluster_event_has_no_workspace_field(capsys) -> None:
    output.configure(())

    output.event("STARTED", "work", "12 workspaces")

    assert capsys.readouterr().out == "12:34:56\tSTARTED\twork\t12 workspaces\n"


def test_subscriber_receives_structured_record() -> None:
    records = []

    class Subscriber(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    subscriber = Subscriber()
    output.LOGGER.addHandler(subscriber)
    try:
        output.configure(("author@node",))
        output.event(
            "UNAVAILABLE", "offline", workspace="author@node", error=True
        )
    finally:
        output.LOGGER.removeHandler(subscriber)

    record = records[0]
    assert record.event_name == "UNAVAILABLE"
    assert record.workspace == "author@node"
    assert record.getMessage() == "offline"
    assert record.levelno == logging.ERROR


def test_jsonl_handler_records_structured_events(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    output.configure(("author@node",))
    output.log_to(path)
    try:
        output.event("LAUNCHED", "task", workspace="author@node")
    finally:
        output.log_to(None)

    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["level"] == "INFO"
    assert value["workspace"] == "author@node"
    assert value["event"] == "LAUNCHED"
    assert value["message"] == "task"
    assert "time" in value
