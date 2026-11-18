import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from responder import alert_to_pr

Result = subprocess.CompletedProcess[str]
Response = Result | Exception | Callable[[list[str]], Result]

PY = sys.executable
BASE_SHA = "a" * 40
BRANCH = "fix/incident-7"
TITLE = "Incident: KeyError: 'order_total'"
BODY = "## Signature\n\n`KeyError: 'order_total'`\n\nsample record has total_amount"
RCA = "# Upstream renamed order_total to total_amount\n\n## Timeline\n\nfirst bad file at 16:02"
PR_URL = "https://github.com/sudar-path/auto-sdlc/pull/8"
PORCELAIN = " M pipeline/run.py\n?? tests/test_schema_drift.py\n"


def done(stdout: str = "", returncode: int = 0, stderr: str = "") -> Result:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def issue_json(state: str = "OPEN", labels: Sequence[str] = ("incident", "bug")) -> str:
    return json.dumps(
        {"number": 7, "title": TITLE, "body": BODY, "state": state, "labels": [{"name": name} for name in labels]}
    )


def add_worktree(args: list[str]) -> Result:
    os.makedirs(args[5])
    return done()


class FakeSubprocess:
    """Stands in for subprocess.run. The first rule whose prefix matches the command answers it."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.rules: list[tuple[tuple[str, ...], Response]] = [
            (("gh", "issue", "view"), done(issue_json())),
            (("git", "rev-parse", "--verify", "--quiet", "refs/heads/" + BRANCH), done(returncode=1)),
            (("git", "rev-parse", "--verify", "--quiet"), done(BASE_SHA + "\n")),
            (("git", "rev-parse", "HEAD"), done(BASE_SHA + "\n")),
            (("git", "worktree", "add"), add_worktree),
            (("droid", "exec", "--auto"), done("Fixed the loader; all checks pass.")),
            (("droid", "exec"), done(RCA + "\n")),
            (("git", "status", "--porcelain"), done(PORCELAIN)),
            (("git", "ls-tree"), done("tests\n")),
            ((PY, "-m"), done("ok\n")),
            (("gh", "pr", "create"), done("Creating pull request...\n" + PR_URL + "\n")),
            (("git",), done()),
            (("gh",), done()),
        ]

    def on(self, prefix: Sequence[str], response: Response) -> None:
        self.rules.insert(0, (tuple(prefix), response))

    def __call__(self, args: list[str], **kwargs: Any) -> Result:
        self.calls.append((list(args), kwargs))
        for prefix, response in self.rules:
            if tuple(args[: len(prefix)]) == prefix:
                if isinstance(response, subprocess.CompletedProcess):
                    return response
                if isinstance(response, Exception):
                    raise response
                return response(args)
        raise AssertionError("unexpected command: %s" % args)

    def commands(self) -> list[list[str]]:
        return [args for args, _ in self.calls]

    def ran(self, *prefix: str) -> bool:
        return any(tuple(args[: len(prefix)]) == prefix for args in self.commands())

    def find(self, *prefix: str) -> list[str]:
        return next(args for args in self.commands() if tuple(args[: len(prefix)]) == prefix)

    def option(self, prefix: Sequence[str], flag: str) -> str:
        args = self.find(*prefix)
        return args[args.index(flag) + 1]


class Env:
    def __init__(self, fake: FakeSubprocess, repo: Path, ws: alert_to_pr.Workspace) -> None:
        self.fake = fake
        self.repo = repo
        self.ws = ws
        self.mkdtemp_calls = 0


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = alert_to_pr.Workspace(str(tmp_path / "tmp" / "incident-test"))
    state = Env(FakeSubprocess(), repo, ws)

    def fake_mkdtemp(prefix: str = "") -> str:
        state.mkdtemp_calls += 1
        os.makedirs(ws.root)
        return ws.root

    monkeypatch.setattr("responder.alert_to_pr.subprocess.run", state.fake)
    monkeypatch.setattr("responder.alert_to_pr.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("responder.alert_to_pr.tempfile.mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(alert_to_pr, "ROOT", str(repo))
    monkeypatch.setenv("FAULT", "schema_drift")
    return state


def assert_nothing_published(fake: FakeSubprocess) -> None:
    for prefix in (("git", "add"), ("git", "commit"), ("git", "push"), ("gh", "pr"), ("git", "worktree", "remove")):
        assert not fake.ran(*prefix), prefix


def issue_comment(fake: FakeSubprocess) -> str:
    return fake.option(("gh", "issue", "comment", "7"), "--body")


# Happy path


def test_happy_path_runs_every_step_in_order(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert alert_to_pr.main(["7"]) == 0

    assert [args[:3] for args in env.fake.commands()] == [
        ["gh", "issue", "view"],
        ["git", "rev-parse", "--verify"],
        ["git", "rev-parse", "--verify"],
        ["git", "worktree", "add"],
        ["droid", "exec", "--file"],
        ["droid", "exec", "--auto"],
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain"],
        ["git", "ls-tree", "--name-only"],
        [PY, "-m", "ruff"],
        [PY, "-m", "ruff"],
        [PY, "-m", "mypy"],
        [PY, "-m", "pytest"],
        ["git", "add", "pipeline"],
        ["git", "commit", "-m"],
        ["git", "push", "-u"],
        ["gh", "pr", "create"],
        ["gh", "issue", "comment"],
        ["git", "worktree", "remove"],
    ]
    assert "opened %s" % PR_URL in capsys.readouterr().out
    assert not os.path.exists(env.ws.root)


def test_happy_path_exact_git_and_gh_commands(env: Env) -> None:
    assert alert_to_pr.main(["7"]) == 0

    commands = env.fake.commands()
    assert commands[0] == ["gh", "issue", "view", "7", "--json", "number,title,body,labels,state"]
    assert ["git", "worktree", "add", "-b", BRANCH, env.ws.worktree, BASE_SHA] in commands
    assert ["git", "rev-parse", "--verify", "--quiet", "main^{commit}"] in commands
    assert ["git", "add", "pipeline", "tests"] in commands
    assert ["git", "commit", "-m", "Fix incident #7: %s" % TITLE] in commands
    assert ["git", "push", "-u", "origin", BRANCH] in commands
    assert env.fake.find("gh", "pr", "create")[:7] == ["gh", "pr", "create", "--base", "main", "--head", BRANCH]
    assert [PY, "-m", "ruff", "format", "--check", "."] in commands
    assert [PY, "-m", "pytest", "--cov"] in commands
    assert PR_URL in issue_comment(env.fake)
    assert "human must review and merge" in issue_comment(env.fake)


def test_every_subprocess_call_is_safe(env: Env) -> None:
    assert alert_to_pr.main(["7"]) == 0

    for args, kwargs in env.fake.calls:
        assert "shell" not in kwargs
        assert kwargs["timeout"] == (600 if args[0] == "droid" else 120)
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert "FAULT" not in kwargs["env"]
        in_repo = args[:3] in (["gh", "issue", "view"], ["git", "rev-parse", "--verify"], ["git", "worktree", "add"])
        in_repo = in_repo or args[:3] == ["git", "worktree", "remove"]
        assert kwargs["cwd"] == (str(env.repo) if in_repo else env.ws.worktree), args


def test_droid_runs_use_prompt_files_outside_the_worktree(env: Env) -> None:
    assert alert_to_pr.main(["7", "--keep-worktree", "--model", "claude-opus-5-5"]) == 0

    triage = env.fake.find("droid", "exec", "--file")
    fix = env.fake.find("droid", "exec", "--auto")
    assert triage == [
        "droid", "exec", "--file", env.ws.triage_prompt, "--cwd", env.ws.worktree, "--model", "claude-opus-5-5"
    ]  # fmt: skip
    assert fix == [
        "droid", "exec", "--auto", "medium", "--file", env.ws.fix_prompt, "--cwd", env.ws.worktree,
        "--model", "claude-opus-5-5",
    ]  # fmt: skip
    for prompt in (env.ws.triage_prompt, env.ws.fix_prompt):
        assert not alert_to_pr.is_inside(prompt, env.ws.worktree)


def test_prompts_carry_evidence_rca_and_rules(env: Env) -> None:
    assert alert_to_pr.main(["7", "--keep-worktree"]) == 0

    triage = Path(env.ws.triage_prompt).read_text()
    fix = Path(env.ws.fix_prompt).read_text()
    for prompt in (triage, fix):
        assert TITLE in prompt
        assert BODY in prompt
        assert "Do not read or modify anything under upstream/" in prompt
    for section in ("Timeline", "Root cause", "Blast radius", "Proposed fix", "Missing evidence"):
        assert "## %s" % section in triage
    assert "Work only from the evidence below and the code in this checkout" in triage
    assert "Do not edit, create, or delete any files" in triage
    assert RCA in fix
    assert "regression test under tests/ that fails before your fix and passes after it" in fix
    assert "Do not run git commit or git push" in fix
    assert "must still accept the old field name" in fix
    assert "ruff check ., ruff format --check ., mypy" in fix
    assert "CI or config files" in fix
    assert Path(env.ws.rca).read_text() == RCA + "\n"


def test_pr_body_follows_template(env: Env) -> None:
    assert alert_to_pr.main(["7"]) == 0

    body = env.fake.option(("gh", "pr", "create"), "--body")
    sections = ["## Summary", "## Changes", "## Testing", "## Checklist", "## Root cause analysis"]
    assert [body.index(s) for s in sections] == sorted(body.index(s) for s in sections)
    assert "## Summary\n\nUpstream renamed order_total to total_amount\n\nFixes #7" in body
    assert "- `pipeline/run.py`\n- `tests/test_schema_drift.py`" in body
    assert "- `pytest --cov`: passed" in body
    assert "- [x] No changes under `upstream/`" in body
    assert RCA in body
    assert body.endswith(alert_to_pr.HUMAN_REVIEW)


def test_keep_worktree_skips_removal(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert alert_to_pr.main(["7", "--keep-worktree"]) == 0

    assert not env.fake.ran("git", "worktree", "remove")
    assert os.path.isdir(env.ws.worktree)
    assert "worktree kept at %s" % env.ws.worktree in capsys.readouterr().out


def test_worktree_removal_failure_is_a_warning(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("git", "worktree", "remove"), done(returncode=128, stderr="fatal: locked"))

    assert alert_to_pr.main(["7"]) == 0
    assert "warning: could not remove worktree" in capsys.readouterr().err
    assert os.path.isdir(env.ws.worktree)


# Refusing the issue


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (issue_json(state="CLOSED"), "issue #7 is CLOSED; only open incident issues are handled"),
        (issue_json(labels=["bug"]), 'issue #7 is not labelled "incident"'),
        (issue_json(labels=[]), 'issue #7 is not labelled "incident"'),
        ("not json", "could not parse gh issue view output"),
    ],
)
def test_refuses_unsuitable_issue(env: Env, capsys: pytest.CaptureFixture[str], raw: str, message: str) -> None:
    env.fake.on(("gh", "issue", "view"), done(raw))

    assert alert_to_pr.main(["7"]) == 1
    assert env.fake.commands() == [["gh", "issue", "view", "7", "--json", "number,title,body,labels,state"]]
    assert message in capsys.readouterr().err
    assert env.mkdtemp_calls == 0


def test_gh_issue_view_failure(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("gh", "issue", "view"), done(returncode=1, stderr="GraphQL: Could not resolve to an issue"))

    assert alert_to_pr.main(["7"]) == 1
    assert "gh issue view failed with exit code 1: GraphQL: Could not resolve" in capsys.readouterr().err


# Preconditions


def test_missing_tool_is_reported(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("responder.alert_to_pr.shutil.which", lambda name: None if name == "droid" else "/bin/x")

    assert alert_to_pr.main(["7"]) == 1
    assert env.fake.calls == []
    assert "required command not found on PATH: droid" in capsys.readouterr().err


def test_command_vanishing_after_preflight(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("gh",), FileNotFoundError("gh"))

    assert alert_to_pr.main(["7"]) == 1
    assert "command not found: gh" in capsys.readouterr().err


def test_existing_branch_is_an_error(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("git", "rev-parse", "--verify", "--quiet", "refs/heads/" + BRANCH), done("b" * 40))

    assert alert_to_pr.main(["7"]) == 1
    assert "branch fix/incident-7 already exists" in capsys.readouterr().err
    assert not env.fake.ran("git", "worktree", "add")


def test_unknown_base_is_an_error(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("git", "rev-parse", "--verify", "--quiet", "nope^{commit}"), done(returncode=1))

    assert alert_to_pr.main(["7", "--base", "nope"]) == 1
    assert "--base nope does not name a commit" in capsys.readouterr().err
    assert env.mkdtemp_calls == 0


def test_worktree_with_untracked_data_is_rejected(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    def add_with_data(args: list[str]) -> Result:
        os.makedirs(os.path.join(args[5], "data"))
        return done()

    env.fake.on(("git", "worktree", "add"), add_with_data)

    assert alert_to_pr.main(["7"]) == 1
    assert "worktree unexpectedly contains data" in capsys.readouterr().err
    assert not env.fake.ran("droid")


def test_temp_dir_inside_repo_is_rejected(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = env.repo / "tmp-incident"
    monkeypatch.setattr("responder.alert_to_pr.tempfile.mkdtemp", lambda prefix="": str(inside))

    assert alert_to_pr.main(["7"]) == 1
    assert "is inside the repository" in capsys.readouterr().err
    assert not env.fake.ran("git", "worktree", "add")


# Droid runs


def test_empty_triage_output_stops_before_fix(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("droid", "exec", "--file"), done(" \n\n"))

    assert alert_to_pr.main(["7"]) == 1
    captured = capsys.readouterr()
    assert "triage run produced no output" in captured.err
    assert "worktree kept at %s" % env.ws.worktree in captured.out
    assert not env.fake.ran("droid", "exec", "--auto")
    assert_nothing_published(env.fake)


def test_triage_failure(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("droid", "exec", "--file"), done(returncode=1, stderr="Authentication failed"))

    assert alert_to_pr.main(["7"]) == 1
    assert "triage run failed with exit code 1: Authentication failed" in capsys.readouterr().err
    assert_nothing_published(env.fake)


def test_triage_timeout(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("droid", "exec", "--file"), subprocess.TimeoutExpired(["droid"], 600))

    assert alert_to_pr.main(["7"]) == 1
    assert "droid exec timed out after 600s" in capsys.readouterr().err


def test_fix_failure_keeps_output(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("droid", "exec", "--auto"), done("partial work", returncode=1, stderr="tool denied"))

    assert alert_to_pr.main(["7"]) == 1
    assert "fix run failed with exit code 1" in capsys.readouterr().err
    assert Path(env.ws.fix_output).read_text() == "partial worktool denied"
    assert not env.fake.ran("git", "status")
    assert_nothing_published(env.fake)


# Scope check


@pytest.mark.parametrize(
    ("porcelain", "problem"),
    [
        (" M pipeline/run.py\n M upstream/generate.py\n", "outside pipeline/ and tests/: upstream/generate.py"),
        ("?? .github/workflows/extra.yml\n", "outside pipeline/ and tests/: .github/workflows/extra.yml"),
        (" M pyproject.toml\n?? tests/test_x.py\n", "outside pipeline/ and tests/: pyproject.toml"),
        ("R  pipeline/run.py -> upstream/run.py\n", "outside pipeline/ and tests/: upstream/run.py"),
        ("", "the fix run changed no files"),
    ],
)
def test_scope_violation_aborts_and_comments(
    env: Env, capsys: pytest.CaptureFixture[str], porcelain: str, problem: str
) -> None:
    env.fake.on(("git", "status", "--porcelain"), done(porcelain))

    assert alert_to_pr.main(["7"]) == 1
    captured = capsys.readouterr()
    assert problem in captured.err
    assert "worktree kept at %s" % env.ws.worktree in captured.out
    assert problem in issue_comment(env.fake)
    assert not env.fake.ran(PY, "-m")
    assert_nothing_published(env.fake)
    assert os.path.isdir(env.ws.worktree)


def test_commit_by_droid_is_a_scope_violation(env: Env) -> None:
    env.fake.on(("git", "rev-parse", "HEAD"), done("c" * 40 + "\n"))

    assert alert_to_pr.main(["7"]) == 1
    assert "created commits of its own" in issue_comment(env.fake)
    assert_nothing_published(env.fake)


def test_comment_failure_is_a_warning(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("git", "status", "--porcelain"), done(" M upstream/generate.py\n"))
    env.fake.on(("gh", "issue", "comment"), done(returncode=1, stderr="HTTP 403"))

    assert alert_to_pr.main(["7"]) == 1
    assert "warning: could not comment on issue #7" in capsys.readouterr().err


# Verification


def test_failing_check_comments_last_30_lines(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    output = "".join("mypy line %d\n" % i for i in range(40))
    env.fake.on((PY, "-m", "mypy"), done(output, returncode=1))

    assert alert_to_pr.main(["7"]) == 1
    assert [args[:3] for args in env.fake.commands() if args[0] == PY] == [
        [PY, "-m", "ruff"],
        [PY, "-m", "ruff"],
        [PY, "-m", "mypy"],
        [PY, "-m", "pytest"],
    ]
    comment = issue_comment(env.fake)
    assert "- `ruff check .`: passed" in comment
    assert "- `mypy`: failed (exit 1)" in comment
    assert "mypy line 10\n" in comment
    assert "mypy line 39" in comment
    assert "mypy line 9\n" not in comment
    captured = capsys.readouterr()
    assert "checks failed: mypy" in captured.err
    assert "worktree kept at %s" % env.ws.worktree in captured.out
    assert_nothing_published(env.fake)


def test_check_timeout_counts_as_failure(env: Env) -> None:
    env.fake.on((PY, "-m", "pytest"), subprocess.TimeoutExpired(["pytest"], 120))

    assert alert_to_pr.main(["7"]) == 1
    comment = issue_comment(env.fake)
    assert "- `pytest --cov`: failed (exit -1)" in comment
    assert "timed out after 120s" in comment
    assert_nothing_published(env.fake)


def test_base_without_tests_is_unverified(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.on(("git", "ls-tree"), done(""))

    assert alert_to_pr.main(["7"]) == 1
    assert env.fake.find("git", "ls-tree") == ["git", "ls-tree", "--name-only", BASE_SHA, "--", "tests"]
    comment = issue_comment(env.fake)
    assert "could not verify it" in comment
    assert "UNVERIFIED" in comment
    assert "no tests/ directory" in comment
    assert "UNVERIFIED" in capsys.readouterr().err
    assert not env.fake.ran(PY, "-m")
    assert_nothing_published(env.fake)


def test_pytest_collecting_nothing_is_unverified(env: Env) -> None:
    env.fake.on((PY, "-m", "pytest"), done("no tests ran in 0.01s", returncode=5))

    assert alert_to_pr.main(["7"]) == 1
    comment = issue_comment(env.fake)
    assert "could not verify it: pytest collected no tests" in comment
    assert_nothing_published(env.fake)


# Non-main base


def test_base_not_main_stops_before_commit(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert alert_to_pr.main(["7", "--base", "experiment"]) == 0

    assert ["git", "rev-parse", "--verify", "--quiet", "experiment^{commit}"] in env.fake.commands()
    assert env.fake.ran(PY, "-m", "pytest")
    assert_nothing_published(env.fake)
    assert not env.fake.ran("gh", "issue", "comment")
    out = capsys.readouterr().out
    assert "--base experiment is not main, so stopping before commit and push (experiments only)" in out
    assert "worktree kept at %s" % env.ws.worktree in out


# Dry run


def test_dry_run_prints_plan_and_writes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert alert_to_pr.main(["7", "--dry-run"]) == 0

    assert [args[:2] for args in env.fake.commands()] == [["gh", "issue"], ["git", "rev-parse"], ["git", "rev-parse"]]
    assert env.mkdtemp_calls == 0
    out = capsys.readouterr().out
    for expected in (
        "would run: git worktree add -b fix/incident-7 ",
        "would run: droid exec --file ",
        "would run: droid exec --auto medium --file ",
        "would run: git status --porcelain",
        "would run: %s -m pytest --cov" % PY,
        "would run: git add pipeline tests",
        "would run: git commit -m 'Fix incident #7: Incident: KeyError: '\"'\"'order_total'\"'\"''",
        "would run: git push -u origin fix/incident-7",
        "would run: gh pr create --base main --head fix/incident-7",
        "would run: gh issue comment 7",
        "--- triage prompt ---",
        "--- fix prompt ---",
        "<RCA from the triage run>",
        BODY,
    ):
        assert expected in out, expected
    assert "schema_drift" not in out


def test_dry_run_with_other_base_and_model(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert alert_to_pr.main(["7", "--dry-run", "--base", "experiment", "--model", "gpt-6.1-sol"]) == 0

    out = capsys.readouterr().out
    assert "--model gpt-6.1-sol" in out
    assert "would run: git push" not in out
    assert "would run: gh pr create" not in out
    assert "would stop before commit and push" in out
    assert env.mkdtemp_calls == 0


# Helpers


def test_changed_paths_handles_renames_and_quotes() -> None:
    porcelain = ' M pipeline/run.py\nR  tests/a.py -> tests/b.py\n?? "tests/odd name.py"\n\n'

    assert alert_to_pr.changed_paths(porcelain) == ["pipeline/run.py", "tests/a.py", "tests/b.py", "tests/odd name.py"]


@pytest.mark.parametrize(
    ("rca", "headline"),
    [
        ("\n\n## Root cause: X\nmore", "Root cause: X"),
        ("plain first line\n", "plain first line"),
        ("\n#\n", "(the RCA has no headline)"),
    ],
)
def test_rca_headline(rca: str, headline: str) -> None:
    assert alert_to_pr.rca_headline(rca) == headline


def test_pr_body_truncates_huge_rca() -> None:
    issue = alert_to_pr.Issue(7, TITLE, BODY)
    body = alert_to_pr.pr_body(issue, "x" * (alert_to_pr.MAX_RCA_CHARS + 10), ["pipeline/run.py"], [])

    assert "(RCA truncated)" in body
    assert "## Summary\n\n%s...\n" % ("x" * alert_to_pr.MAX_HEADLINE_CHARS) in body
    assert len(body) < alert_to_pr.MAX_RCA_CHARS + 3000


def test_publish_without_url_output(env: Env) -> None:
    env.fake.on(("gh", "pr", "create"), done(""))

    assert alert_to_pr.main(["7"]) == 0
    assert "(gh printed no URL)" in issue_comment(env.fake)
