import ast
import json
import logging
import pathlib
import re

import pytest
import structlog
from pydantic import ValidationError
from structlog.contextvars import bound_contextvars, clear_contextvars

from autobrr_remove.config import LoggingConfig
from autobrr_remove.logging import setup_logging

LOG_METHODS = {"critical", "debug", "error", "exception", "info", "warning"}


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level

    yield

    root.handlers = original_handlers
    root.setLevel(original_level)
    structlog.reset_defaults()
    clear_contextvars()


def test_logfmt_is_default_and_uses_wi1_bot_fields(capsys):
    setup_logging(LoggingConfig(level="DEBUG"))

    structlog.stdlib.get_logger("autobrr_remove").info("processed torrent", removed=True)

    line = capsys.readouterr().out.strip()
    assert re.match(
        (
            r'^ts="\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}" '
            r"level=INFO logger=autobrr_remove "
            r"src=test_logfmt_is_default_and_uses_wi1_bot_fields:\d+ "
            r'msg="processed torrent" removed=true$'
        ),
        line,
    )


def test_json_output_includes_and_restores_contextvars(capsys):
    setup_logging(LoggingConfig(format="json"))
    logger = structlog.stdlib.get_logger("autobrr_remove")

    with bound_contextvars(job="remove_stopped", torrent="1234567890abcdef"):
        logger.warning("removing torrent", delete_files=True)
    logger.info("context restored")

    contextual, restored = [
        json.loads(line) for line in capsys.readouterr().out.strip().splitlines()
    ]
    assert contextual == {
        "delete_files": True,
        "torrent": "1234567890abcdef",
        "job": "remove_stopped",
        "level": "WARNING",
        "logger": "autobrr_remove",
        "src": contextual["src"],
        "ts": contextual["ts"],
        "msg": "removing torrent",
    }
    assert re.match(r"test_json_output_includes_and_restores_contextvars:\d+", contextual["src"])
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", contextual["ts"])
    assert "job" not in restored
    assert "torrent" not in restored


def test_stdlib_logs_use_the_same_processors(capsys):
    setup_logging(LoggingConfig(format="json"))

    logging.getLogger("autobrr_remove.worker").warning("stdlib warning")

    event = json.loads(capsys.readouterr().out)
    assert event["level"] == "WARNING"
    assert event["logger"] == "autobrr_remove.worker"
    assert event["msg"] == "stdlib warning"
    assert re.match(r"test_stdlib_logs_use_the_same_processors:\d+", event["src"])


def test_file_handler_uses_selected_format(tmp_path):
    log_file = tmp_path / "autobrr-remove.log"
    setup_logging(LoggingConfig(format="json", file=log_file))

    structlog.stdlib.get_logger("autobrr_remove").info("written to file")
    for handler in logging.getLogger().handlers:
        handler.flush()

    event = json.loads(log_file.read_text())
    assert event["msg"] == "written to file"
    assert event["logger"] == "autobrr_remove"


def test_logging_format_rejects_unknown_values():
    with pytest.raises(ValidationError):
        LoggingConfig.model_validate({"format": "console"})


def test_application_log_messages_are_static():
    package_dir = pathlib.Path(__file__).parents[1] / "autobrr_remove"
    dynamic_messages = []

    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "log"
                and node.func.attr in LOG_METHODS
                and (
                    not node.args
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                )
            ):
                dynamic_messages.append(f"{path.name}:{node.lineno}")

    assert dynamic_messages == []
