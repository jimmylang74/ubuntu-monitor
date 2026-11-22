from __future__ import annotations

import logging
from pathlib import Path

from app.cli import main
from app.config import ServerConfig
from app.main import configure_logging


def test_rotating_file_logging_writes_file(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "power-monitor.log"
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    try:
        configure_logging(
            ServerConfig(log_file=str(log_path), log_level="info", log_max_bytes=4096)
        )
        logging.getLogger("app.test").info("hello file logging")
        for handler in root.handlers:
            handler.flush()
        assert log_path.is_file()
        assert "hello file logging" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        for handler in old_handlers:
            root.addHandler(handler)
        root.setLevel(old_level)


def test_check_command_prints_effective_config(capsys) -> None:
    assert main(["check"]) == 0
    output = capsys.readouterr().out
    assert "configuration OK" in output
    assert "database:" in output
    assert "retention:" in output
    assert "external meter:" in output
