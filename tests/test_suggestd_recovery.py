"""Suggestion pipe failures are contained and their subprocesses are reaped."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from zimtty.app import ZimTTY
from tests.test_security_ui import MemoryZim


def process(reader, returncode=None):
    return SimpleNamespace(
        stdout=reader, returncode=returncode,
        kill=Mock(), wait=AsyncMock(return_value=0),
    )


def app_for(proc):
    app = ZimTTY(MemoryZim(), None)
    app._sugg_proc = proc
    app._sugg_ready = True
    app._search_active = (7, "query", app._search_epoch)
    app._current_search = Mock(return_value=True)
    app._show_results = Mock()
    app._dispatch_query = Mock()
    return app


@pytest.mark.parametrize("failure", ["oversized", "io"])
async def test_failed_suggestion_read_cleans_up_and_allows_next_query(failure):
    reader = asyncio.StreamReader(limit=64)
    if failure == "oversized":
        reply = {"q": "query", "results": [["path", "title" * 40]]}
        reader.feed_data(json.dumps(reply).encode() + b"\n")
    else:
        reader.set_exception(OSError("suggestion pipe failed"))
    proc = process(reader)
    app = app_for(proc)

    await ZimTTY._read_suggestd(app)

    assert app._sugg_proc is None
    proc.kill.assert_called_once_with()
    proc.wait.assert_awaited_once_with()
    app._show_results.assert_not_called()
    ZimTTY._send_query(app, "next query")
    assert app._search_pending == "next query"
    assert not hasattr(app, "_suggest_inprocess")


async def test_valid_results_are_delivered_and_exited_process_is_reaped():
    reader = asyncio.StreamReader()
    reader.feed_data(b'bad json\n{"id":7,"q":"query","results":[["path","title"]]}\n')
    reader.feed_eof()
    proc = process(reader, returncode=0)
    app = app_for(proc)

    await ZimTTY._read_suggestd(app)

    app._show_results.assert_called_once_with("query", [("path", "title")])
    assert app._sugg_proc is None
    proc.kill.assert_not_called()
    proc.wait.assert_awaited_once_with()


async def test_malformed_result_shapes_are_skipped_without_losing_valid_results():
    reader = asyncio.StreamReader()
    messages = [None, 1, [], {"results": []},
                {"id": 7, "q": "query", "results": [[1, 2]]},
                {"id": 7, "q": "query", "results": ["private"]},
                {"id": 7, "q": "query", "results": [["path", "title"]]}]
    for message in messages:
        reader.feed_data(json.dumps(message).encode() + b"\n")
    reader.feed_eof()
    app = app_for(process(reader, returncode=0))

    await ZimTTY._read_suggestd(app)

    app._show_results.assert_called_once_with("query", [("path", "title")])
    assert "private" not in app.diagnostics.render()


async def test_cancelling_reader_still_cleans_up_its_process():
    proc = process(asyncio.StreamReader())
    app = app_for(proc)
    task = asyncio.create_task(ZimTTY._read_suggestd(app))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert app._sugg_proc is None
    proc.kill.assert_called_once_with()
    proc.wait.assert_awaited_once_with()


async def test_old_reader_does_not_clear_replacement_process():
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"id":7,"q":"query","results":[]}\n')
    reader.feed_eof()
    old = process(reader)
    new = process(asyncio.StreamReader())
    app = app_for(old)
    app._show_results.side_effect = lambda *_: setattr(app, "_sugg_proc", new)

    await ZimTTY._read_suggestd(app)

    assert app._sugg_proc is new
    old.kill.assert_called_once_with()
    old.wait.assert_awaited_once_with()
    new.kill.assert_not_called()


@pytest.mark.parametrize("cleanup", [ZimTTY._read_suggestd, ZimTTY.on_unmount])
async def test_process_exiting_during_cleanup_is_still_reaped(cleanup):
    reader = asyncio.StreamReader()
    reader.feed_eof()
    proc = process(reader)
    proc.kill.side_effect = ProcessLookupError
    app = app_for(proc)

    await cleanup(app)

    assert app._sugg_proc is None
    proc.wait.assert_awaited_once_with()


@pytest.mark.parametrize("returncode", [None, 0])
async def test_unmount_reaps_running_and_exited_processes(returncode):
    proc = process(asyncio.StreamReader(), returncode)
    app = app_for(proc)

    await ZimTTY.on_unmount(app)

    assert app._sugg_proc is None
    assert proc.kill.call_count == (returncode is None)
    proc.wait.assert_awaited_once_with()
