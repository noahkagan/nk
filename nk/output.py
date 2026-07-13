"""Structured operational events and their terminal presentation."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path


LOGGER = logging.getLogger("nk.operations")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


class TerminalFormatter(logging.Formatter):
    def __init__(self, reference_width: int) -> None:
        super().__init__()
        self.reference_width = reference_width

    def format(self, record: logging.LogRecord) -> str:
        workspace = getattr(record, "workspace", None)
        event_name = getattr(record, "event_name", None)
        fields = [self.formatTime(record, "%H:%M:%S")]
        if workspace:
            fields.append(workspace.ljust(self.reference_width))
        if event_name:
            fields.append(event_name)
        message = record.getMessage()
        if message:
            fields.append(message)
        return "\t".join(fields)


class TerminalHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stderr if record.levelno >= logging.ERROR else sys.stdout
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


class JsonlHandler(logging.Handler):
    def __init__(self, path: Path) -> None:
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.stream.write(json.dumps({
                "time": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "workspace": getattr(record, "workspace", None),
                "event": getattr(record, "event_name", None),
                "message": record.getMessage(),
            }, separators=(",", ":")) + "\n")
            self.stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            super().close()


def configure(workspaces: Iterable[str], *, verbose: bool = False) -> None:
    references = tuple(workspaces)
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = TerminalHandler()
    handler.setFormatter(
        TerminalFormatter(max(map(len, references), default=0))
    )
    for existing in tuple(LOGGER.handlers):
        if isinstance(existing, TerminalHandler):
            LOGGER.removeHandler(existing)
    LOGGER.addHandler(handler)


def log_to(path: Path | None) -> None:
    for existing in tuple(LOGGER.handlers):
        if isinstance(existing, JsonlHandler):
            LOGGER.removeHandler(existing)
            existing.close()
    if path is None:
        return
    # ponytail: append-only local log; add rotation if this becomes operationally large.
    LOGGER.addHandler(JsonlHandler(path))


def event(
    name: str,
    *details: object,
    workspace: str | None = None,
    error: bool = False,
    verbose: bool = False,
) -> None:
    LOGGER.log(
        logging.ERROR if error else logging.DEBUG if verbose else logging.INFO,
        "\t".join(map(str, details)),
        extra={"event_name": name, "workspace": workspace},
    )


def line(message: str, *, workspace: str | None = None) -> None:
    LOGGER.info(message, extra={"event_name": None, "workspace": workspace})
