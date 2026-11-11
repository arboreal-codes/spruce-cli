import queue
from pathlib import Path

import pytest

from spruce_cli import dashboard
from spruce_cli.common import Config, UserError
from spruce_cli.ui import _capture


def drain(sink: queue.SimpleQueue[str]) -> list[str]:
    return [sink.get() for _ in range(sink.qsize())]


def test_capture_streams_lines_and_errors() -> None:
    sink: queue.SimpleQueue[str] = queue.SimpleQueue()

    def action() -> None:
        print("Reading tenant sites...")
        print("partial", end="")
        raise UserError("Not logged in")

    _capture(action, sink)
    assert drain(sink) == ["Reading tenant sites...", "partialERROR: Not logged in"]

    _capture(lambda: print("ok"), sink)
    assert drain(sink) == ["ok", "Done."]


def test_signed_in_account_reads_cache_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(None, "default", "", "", "", "browser", tmp_path)
    monkeypatch.setattr(Config, "token_cache", property(lambda self: tmp_path / "cache.json"))
    assert dashboard.signed_in_account(config) is None
    (tmp_path / "cache.json").write_text('{"Account": {"k": {"username": "someone@example.com"}}}')
    assert dashboard.signed_in_account(config) == "someone@example.com"
