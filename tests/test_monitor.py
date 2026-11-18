import io
import json
import signal
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from monitor import run_and_alert
from tests.factories import order, write_order_file

TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "/repo/pipeline/run.py", line 63, in <module>',
    "    main()",
    '  File "/repo/pipeline/run.py", line 36, in load_file',
    '    (o["order_id"], o["customer_id"], o["order_total"], o["currency"], o["created_at"]),',
    "                                      ~^^^^^^^^^^^^^^^",
    "KeyError: 'order_total'",
]
SIGNATURE = "KeyError: 'order_total'"
ISSUE_URL = "https://github.com/sudar-path/auto-sdlc/issues/7"


class FakeProc:
    def __init__(self, lines: Iterable[str], returncode: int, wait_timeouts: int = 0) -> None:
        self.stderr = lines
        self.returncode = returncode
        self.wait_timeouts = wait_timeouts
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_timeouts:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired("pipeline", timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class FakeGh:
    def __init__(self, issues: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.issues = issues or []
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        if args[:3] == ["gh", "issue", "list"]:
            return json.dumps(self.issues)
        if args[:3] == ["gh", "issue", "create"]:
            return ISSUE_URL + "\n"
        return ""


def install_popen(monkeypatch: pytest.MonkeyPatch, proc: FakeProc) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(args: list[str], **kwargs: Any) -> FakeProc:
        calls.append((args, kwargs))
        return proc

    monkeypatch.setattr("monitor.run_and_alert.subprocess.Popen", fake_popen)
    return calls


def interrupted_stderr() -> Iterator[str]:
    yield "partial line\n"
    raise KeyboardInterrupt


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(run_and_alert, "run_gh", fake)
    return fake


@pytest.fixture
def failing_pipeline(monkeypatch: pytest.MonkeyPatch, landing: Path) -> None:
    monkeypatch.setattr(run_and_alert, "LANDING", str(landing))
    monkeypatch.delenv("ALERTS", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    install_popen(monkeypatch, FakeProc([line + "\n" for line in TRACEBACK], returncode=1))


# Signature parsing


def test_parse_signature_takes_exception_line_after_caret_markers() -> None:
    assert run_and_alert.parse_signature(TRACEBACK, 1) == SIGNATURE


def test_parse_signature_ignores_trailing_blank_lines() -> None:
    assert run_and_alert.parse_signature([*TRACEBACK, "", "   "], 1) == SIGNATURE


def test_parse_signature_uses_plain_error_message() -> None:
    assert run_and_alert.parse_signature(["fatal: landing is not writable"], 1) == "fatal: landing is not writable"


@pytest.mark.parametrize("lines", [[], ["", "    indented only"]])
def test_parse_signature_falls_back_to_exit_code(lines: list[str]) -> None:
    assert run_and_alert.parse_signature(lines, 3) == "pipeline exited with code 3"


def test_parse_signature_truncates_long_messages() -> None:
    signature = run_and_alert.parse_signature(["ValueError: " + "x" * 500], 1)

    assert len(signature) == run_and_alert.MAX_SIGNATURE
    assert signature.startswith("ValueError: xxx")


# Evidence building


def test_sample_record_missing_landing_dir(tmp_path: Path) -> None:
    missing = tmp_path / "nope"

    assert run_and_alert.sample_record(str(missing)) == "landing directory not found: %s" % missing


def test_sample_record_empty_landing_dir(landing: Path) -> None:
    (landing / ".orders_x.json.tmp").write_text("[]")

    assert run_and_alert.sample_record(str(landing)) == "no orders_*.json files in %s" % landing


def test_sample_record_invalid_json(landing: Path) -> None:
    (landing / "orders_a_0001.json").write_text("{not json")

    result = run_and_alert.sample_record(str(landing))

    assert result.startswith("orders_a_0001.json could not be read as JSON: ")


@pytest.mark.parametrize("content", ["[]", '{"order_id": "o1"}'])
def test_sample_record_without_records(landing: Path, content: str) -> None:
    (landing / "orders_a_0001.json").write_text(content)

    expected = "orders_a_0001.json does not contain a non-empty list of records"
    assert run_and_alert.sample_record(str(landing)) == expected


def test_sample_record_uses_first_record_of_oldest_file(landing: Path) -> None:
    write_order_file(landing, "orders_b_0002.json", [order("newer", 1.0)])
    write_order_file(landing, "orders_a_0001.json", [order("first", 2.5), order("second", 3.0)])

    result = run_and_alert.sample_record(str(landing))

    name, fenced = result.split("\n", 1)
    assert name == "orders_a_0001.json"
    assert fenced.startswith("```json\n")
    assert fenced.endswith("\n```")
    assert json.loads(fenced.removeprefix("```json\n").removesuffix("\n```")) == order("first", 2.5)


def test_build_alert_includes_signature_trace_and_sample(landing: Path) -> None:
    write_order_file(landing, "orders_a_0001.json", [order("o1", 9.5)])

    alert = run_and_alert.build_alert(1, TRACEBACK, str(landing))

    assert alert.signature == SIGNATURE
    assert alert.title == "Incident: KeyError: 'order_total'"
    assert "exited with code 1" in alert.body
    assert "```text\n" + "\n".join(TRACEBACK) + "\n```" in alert.body
    assert '"order_id": "o1"' in alert.body


# Running the pipeline


def test_stream_stderr_echoes_everything_and_keeps_last_lines() -> None:
    lines = ["line %d\n" % i for i in range(50)]
    sink = io.StringIO()

    tail = run_and_alert.stream_stderr(lines, sink)

    assert sink.getvalue() == "".join(lines)
    assert tail == ["line %d" % i for i in range(10, 50)]


def test_run_pipeline_passes_stdout_through_and_captures_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = install_popen(monkeypatch, FakeProc(["boom\n"], returncode=1))

    assert run_and_alert.run_pipeline(["python", "pipeline/run.py"]) == (1, ["boom"])

    args, kwargs = calls[0]
    assert args == ["python", "pipeline/run.py"]
    assert kwargs["stderr"] is subprocess.PIPE
    assert "stdout" not in kwargs
    assert "shell" not in kwargs
    assert capsys.readouterr().err == "boom\n"


def test_run_pipeline_ctrl_c_terminates_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(interrupted_stderr(), returncode=0)
    install_popen(monkeypatch, proc)

    assert run_and_alert.run_pipeline(["x"]) == (-signal.SIGINT, [])
    assert proc.terminated
    assert not proc.killed


def test_run_pipeline_ctrl_c_kills_child_that_ignores_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(interrupted_stderr(), returncode=0, wait_timeouts=1)
    install_popen(monkeypatch, proc)

    run_and_alert.run_pipeline(["x"])

    assert proc.terminated
    assert proc.killed


@pytest.mark.parametrize(("code", "status"), [(1, 1), (0, 0), (-9, 137)])
def test_exit_status(code: int, status: int) -> None:
    assert run_and_alert.exit_status(code) == status


# Dedupe and issue creation


def test_find_open_incident_lists_open_incident_issues() -> None:
    gh = FakeGh([{"number": 4, "title": "Incident: ValueError: x"}, {"number": 5, "title": "Incident: %s" % SIGNATURE}])

    assert run_and_alert.find_open_incident(SIGNATURE, gh) == 5
    assert gh.calls == [
        ["gh", "issue", "list", "--label", "incident", "--state", "open", "--json", "number,title", "--limit", "100"]
    ]


def test_send_alert_dedupe_hit_creates_nothing(capsys: pytest.CaptureFixture[str], landing: Path) -> None:
    gh = FakeGh([{"number": 5, "title": "Incident: %s (seen twice)" % SIGNATURE}])

    run_and_alert.send_alert(run_and_alert.build_alert(1, TRACEBACK, str(landing)), gh)

    assert [call[:3] for call in gh.calls] == [["gh", "issue", "list"]]
    assert capsys.readouterr().out == "alert skipped: issue #5 is already open for %s\n" % SIGNATURE


def test_send_alert_dedupe_miss_creates_label_and_issue(capsys: pytest.CaptureFixture[str], landing: Path) -> None:
    gh = FakeGh([{"number": 4, "title": "Incident: ValueError: other"}])
    alert = run_and_alert.build_alert(1, TRACEBACK, str(landing))

    run_and_alert.send_alert(alert, gh)

    assert gh.calls[1:] == [
        ["gh", "label", "create", "incident", "--force"],
        ["gh", "issue", "create", "--title", alert.title, "--label", "incident", "--body", alert.body],
    ]
    assert capsys.readouterr().out == "opened %s\n" % ISSUE_URL


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 401\n"), "returned non-zero exit status 1.: HTTP 401"),
        (FileNotFoundError("gh not found"), "alert failed: gh not found"),
    ],
)
def test_send_alert_reports_gh_failure(
    capsys: pytest.CaptureFixture[str], landing: Path, error: Exception, message: str
) -> None:
    run_and_alert.send_alert(run_and_alert.build_alert(1, TRACEBACK, str(landing)), FakeGh(error=error))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_run_gh_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="[]\n", stderr="")

    monkeypatch.setattr("monitor.run_and_alert.subprocess.run", fake_run)

    assert run_and_alert.run_gh(["gh", "issue", "list"]) == "[]\n"
    assert calls == [(["gh", "issue", "list"], {"capture_output": True, "text": True, "check": True})]


# Entry point


def test_main_runs_pipeline_with_current_interpreter(monkeypatch: pytest.MonkeyPatch, gh: FakeGh) -> None:
    calls = install_popen(monkeypatch, FakeProc([], returncode=0))

    assert run_and_alert.main() == 0
    assert calls[0][0] == [sys.executable, run_and_alert.PIPELINE]
    assert gh.calls == []


@pytest.mark.usefixtures("failing_pipeline")
def test_main_opens_issue_and_exits_with_pipeline_code(gh: FakeGh, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_and_alert.main() == 1

    assert gh.calls[-1][:5] == ["gh", "issue", "create", "--title", "Incident: %s" % SIGNATURE]
    captured = capsys.readouterr()
    assert captured.err == "\n".join(TRACEBACK) + "\n"
    assert captured.out == "opened %s\n" % ISSUE_URL


@pytest.mark.usefixtures("failing_pipeline")
@pytest.mark.parametrize("value", ["off", "OFF"])
def test_main_alerts_off(
    monkeypatch: pytest.MonkeyPatch, gh: FakeGh, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    monkeypatch.setenv("ALERTS", value)
    monkeypatch.setenv("DRY_RUN", "1")

    assert run_and_alert.main() == 1
    assert gh.calls == []
    assert capsys.readouterr().out == "alerting disabled\n"


@pytest.mark.usefixtures("failing_pipeline")
def test_main_dry_run_prints_issue_without_gh(
    monkeypatch: pytest.MonkeyPatch, gh: FakeGh, capsys: pytest.CaptureFixture[str], landing: Path
) -> None:
    monkeypatch.setenv("ALERTS", "on")
    monkeypatch.setenv("DRY_RUN", "1")
    write_order_file(landing, "orders_a_0001.json", [order("o1", 9.5)])

    assert run_and_alert.main() == 1
    assert gh.calls == []
    out = capsys.readouterr().out
    expected = run_and_alert.build_alert(1, TRACEBACK, str(landing))
    assert out == "%s\n%s\n" % (expected.title, expected.body)


def test_main_ctrl_c_is_quiet(monkeypatch: pytest.MonkeyPatch, gh: FakeGh, capsys: pytest.CaptureFixture[str]) -> None:
    proc = FakeProc(interrupted_stderr(), returncode=0)
    install_popen(monkeypatch, proc)

    assert run_and_alert.main() == 0
    assert proc.terminated
    assert gh.calls == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("code", [-signal.SIGINT, 128 + signal.SIGINT])
def test_main_sigint_exit_code_is_quiet(
    monkeypatch: pytest.MonkeyPatch, gh: FakeGh, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    install_popen(monkeypatch, FakeProc(["Traceback ...\n", "KeyboardInterrupt\n"], returncode=code))

    assert run_and_alert.main() == 0
    assert gh.calls == []
    assert capsys.readouterr().out == ""
