"""Run the pipeline and open a GitHub incident issue when it fails.

Environment:
    ALERTS=on|off  (default on) - when off, a failure only prints "alerting disabled".
    DRY_RUN=1      - print the issue title and body instead of calling ``gh``.
"""

import glob
import json
import os
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import IO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(ROOT, "pipeline", "run.py")
LANDING = os.path.join(ROOT, "data", "landing")
ORDER_FILE_PATTERN = "orders_*.json"
TAIL_LINES = 40
LABEL = "incident"
# GitHub caps issue titles at 256 characters; truncating keeps dedupe matching the same prefix.
MAX_SIGNATURE = 200
STOP_TIMEOUT = 5

GhRunner = Callable[[list[str]], str]


@dataclass(frozen=True)
class Alert:
    signature: str
    title: str
    body: str


def is_interrupt(returncode: int) -> bool:
    """True for a child killed by SIGINT (negative code) or exiting with the shell's 128+SIGINT."""
    return returncode in (-signal.SIGINT, 128 + signal.SIGINT)


def exit_status(returncode: int) -> int:
    """Map a Popen return code to a process exit status (signal deaths become 128+signum)."""
    return 128 - returncode if returncode < 0 else returncode


def stream_stderr(stream: Iterable[str], sink: IO[str], limit: int = TAIL_LINES) -> list[str]:
    """Echo each line to ``sink`` as it arrives and return the last ``limit`` lines without newlines."""
    tail: deque[str] = deque(maxlen=limit)
    for line in stream:
        sink.write(line)
        sink.flush()
        tail.append(line.rstrip("\n"))
    return list(tail)


def stop_child(proc: "subprocess.Popen[str]", timeout: float = STOP_TIMEOUT) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_pipeline(cmd: Sequence[str]) -> tuple[int, list[str]]:
    """Run ``cmd`` with stdout inherited and stderr echoed. Returns (return code, last stderr lines).

    Ctrl-C terminates the child and is reported as a SIGINT return code.
    """
    proc = subprocess.Popen(list(cmd), stderr=subprocess.PIPE, text=True, errors="replace", bufsize=1)
    try:
        tail = stream_stderr(proc.stderr or [], sys.stderr)
        return proc.wait(), tail
    except KeyboardInterrupt:
        stop_child(proc)
        return -signal.SIGINT, []


def parse_signature(lines: Sequence[str], returncode: int) -> str:
    """Return the exception line (type and message) that ends a traceback.

    Traceback frames, source lines, and ``^^^`` markers are indented, so the signature is the last
    non-empty line that starts in column 0.
    """
    for line in reversed(lines):
        if line.strip() and not line[0].isspace():
            return line.strip()[:MAX_SIGNATURE]
    return "pipeline exited with code %d" % returncode


def sample_record(landing: str) -> str:
    """Describe the first record of the oldest order file in ``landing``. Never raises."""
    if not os.path.isdir(landing):
        return "landing directory not found: %s" % landing
    paths = sorted(glob.glob(os.path.join(landing, ORDER_FILE_PATTERN)))
    if not paths:
        return "no %s files in %s" % (ORDER_FILE_PATTERN, landing)
    name = os.path.basename(paths[0])
    try:
        with open(paths[0]) as f:
            orders = json.load(f)
    except (OSError, ValueError) as e:
        return "%s could not be read as JSON: %s" % (name, e)
    if not isinstance(orders, list) or not orders:
        return "%s does not contain a non-empty list of records" % name
    return "%s\n```json\n%s\n```" % (name, json.dumps(orders[0], indent=2, sort_keys=True))


def build_alert(returncode: int, stderr_tail: Sequence[str], landing: str) -> Alert:
    signature = parse_signature(stderr_tail, returncode)
    body = "\n".join(
        [
            "The orders pipeline exited with code %d." % returncode,
            "",
            "## Signature",
            "",
            "`%s`" % signature,
            "",
            "## Last %d stderr lines" % TAIL_LINES,
            "",
            "```text",
            *stderr_tail,
            "```",
            "",
            "## First record of the oldest landing file",
            "",
            sample_record(landing),
        ]
    )
    return Alert(signature=signature, title="Incident: %s" % signature, body=body)


def run_gh(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout


def find_open_incident(signature: str, gh: GhRunner) -> int | None:
    """Return the number of an open incident issue whose title contains ``signature``, if any."""
    out = gh(["gh", "issue", "list", "--label", LABEL, "--state", "open", "--json", "number,title", "--limit", "100"])
    for issue in json.loads(out):
        if signature in issue["title"]:
            return int(issue["number"])
    return None


def create_issue(alert: Alert, gh: GhRunner) -> str:
    gh(["gh", "label", "create", LABEL, "--force"])
    return gh(["gh", "issue", "create", "--title", alert.title, "--label", LABEL, "--body", alert.body]).strip()


def send_alert(alert: Alert, gh: GhRunner) -> None:
    try:
        existing = find_open_incident(alert.signature, gh)
        if existing is not None:
            print("alert skipped: issue #%d is already open for %s" % (existing, alert.signature), flush=True)
            return
        print("opened %s" % create_issue(alert, gh), flush=True)
    except subprocess.CalledProcessError as e:
        print("alert failed: %s: %s" % (e, (e.stderr or "").strip()), file=sys.stderr, flush=True)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print("alert failed: %s" % e, file=sys.stderr, flush=True)


def handle_failure(returncode: int, stderr_tail: Sequence[str], landing: str, gh: GhRunner) -> None:
    if os.environ.get("ALERTS", "on").strip().lower() == "off":
        print("alerting disabled", flush=True)
        return
    alert = build_alert(returncode, stderr_tail, landing)
    if os.environ.get("DRY_RUN", "").strip() == "1":
        print(alert.title, flush=True)
        print(alert.body, flush=True)
        return
    send_alert(alert, gh)


def main() -> int:
    returncode, stderr_tail = run_pipeline([sys.executable, PIPELINE])
    if returncode == 0 or is_interrupt(returncode):
        return 0
    handle_failure(returncode, stderr_tail, LANDING, run_gh)
    return exit_status(returncode)


if __name__ == "__main__":
    sys.exit(main())
