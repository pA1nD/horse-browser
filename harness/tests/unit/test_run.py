import sys
from io import StringIO
from unittest.mock import patch

from horse_harness import run


def test_stdin_executes_code():
    stdout = StringIO()
    fake_stdin = StringIO("print('hello from stdin')")

    with patch.object(sys, "argv", ["horse-harness"]), \
         patch("horse_harness.run.ensure_daemon"), \
         patch("sys.stdin", fake_stdin), \
         patch("sys.stdout", stdout):
        run.main()

    assert stdout.getvalue().strip() == "hello from stdin"


def test_c_flag_is_rejected():
    with patch.object(sys, "argv", ["horse-harness", "-c", "print('old path')"]), \
         patch("sys.stdin", StringIO("print('ignored')")):
        try:
            run.main()
        except SystemExit as e:
            assert "<<'PY'" in str(e)
        else:
            raise AssertionError("-c should be rejected")


def test_no_args_interactive_stdin_prints_usage():
    fake_stdin = StringIO("")
    fake_stdin.isatty = lambda: True

    with patch.object(sys, "argv", ["horse-harness"]), \
         patch("sys.stdin", fake_stdin):
        try:
            run.main()
        except SystemExit as e:
            assert "<<'PY'" in str(e)
        else:
            raise AssertionError("interactive no-args invocation should exit with usage")


def test_no_args_empty_stdin_prints_usage():
    with patch.object(sys, "argv", ["horse-harness"]), \
         patch("sys.stdin", StringIO("")):
        try:
            run.main()
        except SystemExit as e:
            assert "<<'PY'" in str(e)
        else:
            raise AssertionError("empty stdin should exit with usage")
