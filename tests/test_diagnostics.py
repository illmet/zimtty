"""Diagnostics must remain useful without exporting any reader content."""
import io
import json
import os
import subprocess
import sys

import pytest

import zimtty.app as app_module
from zimtty.diagnostics import DiagnosticTrace, Event
from tests.test_security_ui import MemoryApp, MemoryZim


SECRET = "PRIVATE query/title/path [red]content[/red]"


def test_trace_is_bounded_and_cannot_accept_freeform_messages():
    trace = DiagnosticTrace()
    for _ in range(100):
        trace.record(Event.SEARCH_QUERY_FAILED, RuntimeError(SECRET))
    with pytest.raises(TypeError):
        trace.record(SECRET)
    assert len(trace.render().splitlines()) == 32
    assert SECRET not in trace.render()
    trace.clear()
    assert trace.render() == ""


async def test_article_failure_records_location_without_exception_text(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.omatheme, "COLORS", tmp_path / "no-theme")
    app = MemoryApp(MemoryZim({"private": OSError(SECRET)}), None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.open("private")
        report = app.diagnostics.render()
        assert "article_read_failed app:" in report
        assert SECRET not in report
        assert "private" not in report
        assert str(tmp_path) not in report


@pytest.mark.parametrize("rich_error", [False, True])
async def test_textual_fatal_output_never_renders_exception_or_locals(rich_error, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.omatheme, "COLORS", tmp_path / "no-theme")

    class PrivateError(Exception):
        if rich_error:
            def __rich__(self):
                return SECRET

    class CrashingApp(MemoryApp):
        def on_mount(self):
            private_local = SECRET
            raise PrivateError(private_local)

    app = CrashingApp(MemoryZim(), None)
    # run_test must still raise the original error so regressions cannot hide.
    with pytest.raises(PrivateError):
        async with app.run_test():
            pass
    output = capsys.readouterr()
    assert "internal error" in output.err
    assert SECRET not in output.err + output.out + app.diagnostics.render()
    assert "private_local" not in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("diagnostics", [False, True])
def test_cli_startup_failure_is_private_and_nonzero(diagnostics, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["zimtty", *(["--diagnostics"] if diagnostics else []), SECRET])

    def fail(_):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(app_module, "find_zim", fail)
    with pytest.raises(SystemExit) as result:
        app_module.main()
    assert result.value.code == 1
    output = capsys.readouterr()
    assert SECRET not in output.err + output.out
    assert ("startup_failed app:" in output.err) == diagnostics


def test_invalid_cli_option_is_not_echoed(monkeypatch, capsys):
    option = "--PRIVATE-query-title-path"
    monkeypatch.setattr(sys, "argv", ["zimtty", option])
    monkeypatch.setattr(app_module, "find_zim", lambda _: pytest.fail("invalid option reached startup"))
    with pytest.raises(SystemExit) as result:
        app_module.main()
    assert result.value.code == 2
    assert option not in capsys.readouterr().err


@pytest.mark.parametrize("textual_first", [False, True])
@pytest.mark.parametrize("crash", [False, True])
def test_real_cli_ignores_verbose_logging_and_emits_only_private_trace(tmp_path, textual_first, crash):
    log_path = tmp_path / "framework.log"
    env = {**os.environ, "TEXTUAL_LOG": str(log_path), "TEXTUAL_DEBUG": "1",
           "TEXTUAL": "debug,devtools", "TEXTUAL_SCREENSHOT": "1",
           "TEXTUAL_SCREENSHOT_LOCATION": str(tmp_path)}
    program = f'''
{('import textual.constants' if textual_first else '')}
import sys
from types import SimpleNamespace
import zimtty.app as reader
from textual.widgets import Input

class Archive:
    name = "PRIVATE archive name"
    z = SimpleNamespace(article_count=0)

class Reader(reader.ZimTTY):
    async def _start_suggestd(self):
        pass
    def _send_query(self, query):
        pass
    def on_mount(self):
        super().on_mount()
        self.query_one(Input).value = "PRIVATE typed query"
        self.set_timer(0.1, self.finish)
    def finish(self):
        private_local = "PRIVATE content"
        if {crash!r}:
            raise RuntimeError(private_local)
        self.exit()
    def run(self):
        assert self.devtools is None
        return super().run(headless=True, size=(100, 30))

reader.Zim = lambda path: Archive()
reader.find_zim = lambda path: "PRIVATE archive path"
reader.ZimTTY = Reader
sys.argv = ["zimtty", "--diagnostics"]
reader.main()
'''
    result = subprocess.run([sys.executable, "-c", program], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == (1 if crash else 0), result.stderr
    assert not log_path.exists()
    assert not list(tmp_path.glob("*.svg"))
    assert not result.stdout
    assert "PRIVATE" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "session_started" in result.stderr
    assert "session_stopped" in result.stderr
    assert ("internal_error" in result.stderr) == crash


def test_search_helper_does_not_serialize_exception_messages(monkeypatch, capsys):
    from zimtty import suggestd, zimdoc

    class BrokenZim:
        def suggest(self, *args):
            raise ValueError(SECRET)

    monkeypatch.setattr(zimdoc, "Zim", lambda _: BrokenZim())
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id": 1, "q": "search"}\n'))
    suggestd.serve("unused")
    output = capsys.readouterr()
    assert not output.err
    assert SECRET not in output.out
    response = json.loads(output.out.splitlines()[-1])
    assert response["error"] == "search_failed"
    assert response["results"] == []
