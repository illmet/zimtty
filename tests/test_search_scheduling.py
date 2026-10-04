"""Drive real search UI events against controllable subprocess pipes."""
import asyncio
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Input, OptionList

import zimtty.app as app_module
from zimtty.app import SearchBar, ZimTTY
from tests.test_security_ui import MemoryZim


class Helper:
    """A helper that answers only when the test permits it to finish work."""

    def __init__(self, ready=True):
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.returncode = None
        self.requests = []
        self.sent_at = []
        self.closed = asyncio.Event()
        self.kill = Mock(side_effect=lambda: self.finish(-9))
        self.wait = AsyncMock(side_effect=self._wait)
        if ready:
            self.feed({"ready": True})

    def write(self, data):
        if self.returncode is not None:
            raise BrokenPipeError
        self.requests.append(json.loads(data))
        self.sent_at.append(time.monotonic())

    def feed(self, message):
        self.stdout.feed_data(json.dumps(message).encode() + b"\n")

    def reply(self, index=0, results=None):
        request = self.requests[index]
        self.feed({"id": request["id"], "q": request["q"],
                   "results": results if results is not None else [["good", "Good"]]})

    def finish(self, code=0):
        self.returncode = code
        self.stdout.feed_eof()
        self.closed.set()

    async def _wait(self):
        await self.closed.wait()
        return self.returncode


class Helpers:
    def __init__(self):
        self.processes = []
        self.ready = True
        self.launches = 0
        self.fail_start = False
        self.start_gate = None

    async def start(self, *args, **kwargs):
        self.launches += 1
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.fail_start:
            raise OSError("PRIVATE startup details")
        helper = Helper(self.ready)
        self.processes.append(helper)
        return helper


@pytest.fixture
def helpers(monkeypatch, tmp_path):
    factory = Helpers()
    monkeypatch.setattr(app_module.asyncio, "create_subprocess_exec", factory.start)
    monkeypatch.setattr(app_module.omatheme, "COLORS", tmp_path / "no-theme")
    return factory


def reader():
    zim = MemoryZim()
    zim.suggest = Mock(side_effect=AssertionError("search must stay out of the UI process"))
    return ZimTTY(zim, None)


async def eventually(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("search condition did not become true")
        await asyncio.sleep(0.005)


async def input_text(app, pilot, value):
    app.query_one("#search-input", Input).value = value
    await pilot.pause(0.01)


async def test_queries_wait_for_ready_and_typing_remains_usable(helpers):
    helpers.ready = False
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        helper = helpers.processes[0]
        await input_text(app, pilot, "good")
        await asyncio.sleep(0.25)
        assert helper.requests == []
        assert app.query_one("#search-input", Input).value == "good"
        helper.feed({"ready": True})
        await eventually(lambda: len(helper.requests) == 1)
        assert helper.requests[0]["q"] == "good"
    app.zim.suggest.assert_not_called()


async def test_typing_sends_one_query_after_200ms_of_inactivity(helpers):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        helper = helpers.processes[0]
        inp = app.query_one("#search-input", Input)
        inp.value = "g"
        await asyncio.sleep(0.07)
        assert helper.requests == []
        edited = time.monotonic()
        inp.value = "good"
        await eventually(lambda: helper.requests)
        assert [request["q"] for request in helper.requests] == ["good"]
        assert helper.sent_at[0] - edited >= 0.18


async def test_new_edit_discards_pending_query_before_its_debounce_finishes(helpers):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        helper = helpers.processes[0]
        await input_text(app, pilot, "first")
        await eventually(lambda: helper.requests)
        await input_text(app, pilot, "obsolete")
        await asyncio.sleep(0.23)
        assert [request["q"] for request in helper.requests] == ["first"]
        await input_text(app, pilot, "latest")
        helper.reply(0)
        await asyncio.sleep(0.03)
        assert [request["q"] for request in helper.requests] == ["first"]
        assert app.query_one("#search-results", OptionList).option_count == 0
        await eventually(lambda: len(helper.requests) == 2)
        assert [request["q"] for request in helper.requests] == ["first", "latest"]


@pytest.mark.parametrize("already_active", [False, True])
async def test_repeated_enter_does_not_duplicate_query_and_opens_its_result(helpers, already_active):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        helper = helpers.processes[0]
        await input_text(app, pilot, "good")
        if already_active:
            await eventually(lambda: helper.requests)
        await pilot.press("enter", "enter")
        await eventually(lambda: helper.requests)
        assert len(helper.requests) == 1
        helper.reply()
        await eventually(lambda: app.article is not None)
        assert app.article.path == "good"
        assert not app.query_one(SearchBar).display
        await asyncio.sleep(0.25)
        assert len(helper.requests) == 1


async def test_enter_queues_only_latest_query_behind_active_work(helpers):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        helper = helpers.processes[0]
        await input_text(app, pilot, "first")
        await eventually(lambda: helper.requests)
        await input_text(app, pilot, "other")
        await pilot.press("enter", "enter")
        assert len(helper.requests) == 1
        helper.reply()
        await eventually(lambda: len(helper.requests) == 2)
        assert [request["q"] for request in helper.requests] == ["first", "other"]
        assert app.article is None
        helper.reply(1, [["other", "Other"]])
        await eventually(lambda: app.article is not None)
        assert app.article.path == "other"


async def test_only_matching_integer_id_and_query_can_finish_active_work(helpers):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        helper = helpers.processes[0]
        await input_text(app, pilot, "good")
        await eventually(lambda: helper.requests)
        request = helper.requests[0]
        await input_text(app, pilot, "other")
        await pilot.press("enter")
        for identifier, query in ((None, "good"), (request["id"] + 10, "good"),
                                  (str(request["id"]), "good"), (True, "good"),
                                  (request["id"], "wrong")):
            helper.feed({"id": identifier, "q": query, "results": [["good", "Good"]]})
        await pilot.pause(0.03)
        assert len(helper.requests) == 1
        assert app.article is None
        helper.reply()
        await eventually(lambda: len(helper.requests) == 2)
        # A duplicate old reply must not release the second active request.
        helper.reply()
        await pilot.pause(0.03)
        assert app.article is None
        helper.reply(1, [["other", "Other"]])
        await eventually(lambda: app.article is not None)
        assert app.article.path == "other"


@pytest.mark.parametrize("pending", [False, True])
async def test_closing_search_cancels_debounce_pending_and_late_open(helpers, pending):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        helper = helpers.processes[0]
        if pending:
            await input_text(app, pilot, "first")
            await eventually(lambda: helper.requests)
        await input_text(app, pilot, "good")
        if pending:
            await pilot.press("enter")
        await pilot.press("escape")
        if pending:
            helper.reply()
        await asyncio.sleep(0.25)
        assert not app.query_one(SearchBar).display
        assert app.article is None
        assert len(helper.requests) == int(pending)
        assert app.query_one("#search-results", OptionList).option_count == 0


async def test_reopening_same_query_ignores_the_old_session_reply(helpers):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        helper = helpers.processes[0]
        await input_text(app, pilot, "good")
        await eventually(lambda: helper.requests)
        await pilot.press("enter", "escape", "/")
        await input_text(app, pilot, "good")
        await pilot.press("enter")
        helper.reply()
        await eventually(lambda: len(helper.requests) == 2)
        assert app.article is None
        assert helper.requests[0]["id"] != helper.requests[1]["id"]
        helper.reply(1)
        await eventually(lambda: app.article is not None)
        assert app.article.path == "good"


async def test_pipe_failure_restarts_helper_and_next_query_recovers(helpers):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        broken = helpers.processes[0]
        broken.stdout.set_exception(OSError("PRIVATE broken pipe detail"))
        await eventually(lambda: len(helpers.processes) == 2)
        replacement = helpers.processes[1]
        await input_text(app, pilot, "good")
        await eventually(lambda: replacement.requests)
        replacement.reply()
        await eventually(lambda: app._results_for == "good")
        assert app.query_one("#search-results", OptionList).option_count == 1
        broken.kill.assert_called_once_with()
        broken.wait.assert_awaited()
        assert "PRIVATE" not in app.diagnostics.render()
    app.zim.suggest.assert_not_called()


async def test_start_failures_have_bounded_retries_without_thread_fallback(helpers):
    helpers.fail_start = True
    app = reader()
    async with app.run_test() as pilot:
        await input_text(app, pilot, "good")
        await pilot.press("enter")
        await eventually(lambda: helpers.launches == 3)
        await asyncio.sleep(0.6)
        await input_text(app, pilot, "other")
        await pilot.press("enter")
        await asyncio.sleep(0.25)
        assert helpers.launches == 3
        assert app.query_one("#search-input", Input).value == "other"
        assert app.query_one(SearchBar).display
        assert app.article is None
        assert any("unavailable" in note.message for note in app._notifications)
        assert "PRIVATE" not in app.diagnostics.render()
    app.zim.suggest.assert_not_called()


@pytest.mark.parametrize("during_startup", [False, True])
async def test_concurrent_start_requests_share_one_supervisor(helpers, during_startup):
    if during_startup:
        helpers.start_gate = asyncio.Event()
    else:
        helpers.fail_start = True
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.launches == 1)
        await asyncio.gather(app._start_suggestd(), app._start_suggestd())
        assert helpers.launches == 1
        if during_startup:
            helpers.start_gate.set()
        else:
            helpers.fail_start = False
        await eventually(lambda: helpers.processes)
        assert helpers.launches == (1 if during_startup else 2)
        await input_text(app, pilot, "good")
        await eventually(lambda: helpers.processes[0].requests)
        assert len(helpers.processes) == 1


@pytest.mark.parametrize("edit_during_restart", [False, True])
async def test_active_failure_retries_latest_input_and_only_current_enter_intent(helpers, edit_during_restart):
    app = reader()
    async with app.run_test() as pilot:
        await eventually(lambda: helpers.processes)
        broken = helpers.processes[0]
        await input_text(app, pilot, "good")
        await pilot.press("enter")
        await eventually(lambda: broken.requests)
        broken.stdout.set_exception(OSError("PRIVATE active-query pipe failure"))
        if edit_during_restart:
            await input_text(app, pilot, "other")
        await eventually(lambda: len(helpers.processes) == 2)
        replacement = helpers.processes[1]
        await eventually(lambda: replacement.requests)
        expected = "other" if edit_during_restart else "good"
        assert [request["q"] for request in replacement.requests] == [expected]
        replacement.reply(results=[[expected, expected.title()]])
        if edit_during_restart:
            await eventually(lambda: app._results_for == "other")
            assert app.article is None  # editing cancels the old Enter intent
            await pilot.press("enter")
        await eventually(lambda: app.article is not None)
        assert app.article.path == expected
        broken.kill.assert_called_once_with()
        broken.wait.assert_awaited()
        assert "PRIVATE" not in app.diagnostics.render()
    app.zim.suggest.assert_not_called()
