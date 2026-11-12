"""Tests for raisemap escape analysis, docstring parsing and the command line."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from raisemap.analysis import analyse_sources, module_name
from raisemap.cli import main
from raisemap.docstrings import documented_raises
from raisemap.hierarchy import ancestors, is_caught_by


def analyse(**modules: str):
    sources = {
        f"{name.replace('__', '/')}.py": textwrap.dedent(source) for name, source in modules.items()
    }
    project = analyse_sources(sources)
    return project, {report.name: report for report in project.reports()}


def raised(source: str, function: str = "m.f") -> list[str]:
    _, reports = analyse(m=source)
    return list(reports[function].raises)


class TestHierarchy:
    def test_builtin_chain(self):
        assert {"FileNotFoundError", "OSError", "Exception"} <= ancestors("FileNotFoundError", {})

    def test_known_standard_library_exception(self):
        assert "ValueError" in ancestors("JSONDecodeError", {})

    def test_project_class_chain(self):
        bases = {"AppError": ("RuntimeError",), "NotFound": ("AppError", "LookupError")}
        found = ancestors("NotFound", bases)
        assert {"AppError", "RuntimeError", "LookupError", "Exception"} <= found

    def test_unknown_class_is_assumed_to_be_an_exception(self):
        assert ancestors("VendorError", {}) == {"VendorError", "Exception", "BaseException"}

    def test_cyclic_bases_terminate(self):
        assert ancestors("A", {"A": ("B",), "B": ("A",)}) == {"A", "B"}

    def test_catching(self):
        assert is_caught_by("KeyError", frozenset({"LookupError"}), {})
        assert not is_caught_by("KeyError", frozenset({"ValueError"}), {})
        assert is_caught_by("KeyboardInterrupt", None, {})
        assert is_caught_by("KeyboardInterrupt", frozenset({"BaseException"}), {})
        assert not is_caught_by("KeyboardInterrupt", frozenset({"Exception"}), {})


class TestDirectRaises:
    def test_raise_call_and_bare_class(self):
        source = """\
            def f(x):
                if x:
                    raise ValueError("bad")
                raise errors.NotFound
        """
        assert raised(source) == ["NotFound", "ValueError"]

    def test_nested_definitions_are_not_executed(self):
        source = """\
            def f():
                def inner():
                    raise ValueError
                class K:
                    raise TypeError
                return lambda: (_ for _ in ()).throw(KeyError)
        """
        assert raised(source) == []

    def test_dynamic_raise_is_counted_not_guessed(self):
        _, reports = analyse(m="def f(error):\n    raise error\n")
        assert reports["m.f"].raises == {} and reports["m.f"].dynamic_raises == 1

    def test_origin_points_at_the_raise(self):
        _, reports = analyse(m="def f():\n\n    raise ValueError\n")
        assert reports["m.f"].raises["ValueError"].render() == "m.py:3"


class TestTryExcept:
    def test_handler_removes_what_it_catches(self):
        source = """\
            def f():
                try:
                    raise KeyError("k")
                except LookupError:
                    return None
        """
        assert raised(source) == []

    def test_unrelated_handler_lets_it_through(self):
        source = """\
            def f():
                try:
                    raise KeyError("k")
                except ValueError:
                    pass
        """
        assert raised(source) == ["KeyError"]

    def test_tuple_and_bare_except(self):
        source = """\
            def f():
                try:
                    raise KeyError
                except (ValueError, KeyError):
                    pass
                try:
                    raise SystemExit
                except:
                    pass
        """
        assert raised(source) == []

    def test_bare_reraise_restores_the_caught_exception(self):
        source = """\
            def f():
                try:
                    raise KeyError
                except LookupError:
                    log()
                    raise
        """
        assert raised(source) == ["KeyError"]

    def test_reraise_by_name(self):
        source = """\
            def f():
                try:
                    raise KeyError
                except KeyError as error:
                    raise error
        """
        assert raised(source) == ["KeyError"]

    def test_translation(self):
        source = """\
            def f():
                try:
                    raise KeyError
                except KeyError as error:
                    raise AppError("missing") from error
        """
        assert raised(source) == ["AppError"]

    def test_reraise_of_invisible_exception_uses_the_handler_type(self):
        source = """\
            def f(client):
                try:
                    client.send()
                except ConnectionError:
                    raise
        """
        assert raised(source) == ["ConnectionError"]

    def test_else_and_finally_are_not_protected(self):
        source = """\
            def f():
                try:
                    pass
                except Exception:
                    pass
                else:
                    raise ValueError
                finally:
                    raise TypeError
        """
        assert raised(source) == ["TypeError", "ValueError"]

    def test_handler_ordering(self):
        source = """\
            def f():
                try:
                    raise FileNotFoundError
                except FileNotFoundError:
                    pass
                except OSError:
                    raise RuntimeError
        """
        assert raised(source) == []

    def test_project_exception_hierarchy(self):
        source = """\
            class AppError(Exception): ...
            class NotFound(AppError): ...

            def f():
                try:
                    raise NotFound
                except AppError:
                    pass
        """
        assert raised(source) == []

    def test_contextlib_suppress(self):
        source = """\
            from contextlib import suppress

            def f():
                with suppress(FileNotFoundError):
                    raise FileNotFoundError
                with suppress(KeyError):
                    raise ValueError
        """
        assert raised(source) == ["ValueError"]


class TestPropagation:
    def test_through_local_calls(self):
        source = """\
            def f():
                return g()

            def g():
                return h()

            def h():
                raise ValueError
        """
        _, reports = analyse(m=source)
        assert reports["m.f"].raises["ValueError"].render() == "m.py:8 via m.g()"

    def test_caller_can_catch_what_the_callee_raises(self):
        source = """\
            def f():
                try:
                    g()
                except ValueError:
                    pass

            def g():
                raise ValueError
        """
        assert raised(source) == []

    def test_recursion_terminates(self):
        source = """\
            def f(n):
                if n < 0:
                    raise ValueError
                return g(n)

            def g(n):
                return f(n - 1)
        """
        _, reports = analyse(m=source)
        assert list(reports["m.f"].raises) == list(reports["m.g"].raises) == ["ValueError"]

    def test_methods_through_self_and_inheritance(self):
        source = """\
            class Base:
                def check(self):
                    raise PermissionError

            class Child(Base):
                def run(self):
                    self.check()
                    self.unknown()
        """
        _, reports = analyse(m=source)
        assert list(reports["m.Child.run"].raises) == ["PermissionError"]
        assert reports["m.Child.run"].unresolved_calls == 1

    def test_constructor(self):
        source = """\
            class Thing:
                def __init__(self, x):
                    if x < 0:
                        raise ValueError

            class Plain: ...

            def f():
                Plain()
                return Thing(1)
        """
        _, reports = analyse(m=source)
        assert list(reports["m.f"].raises) == ["ValueError"]
        assert reports["m.f"].unresolved_calls == 0

    def test_across_modules(self):
        project, reports = analyse(
            pkg____init__="",
            pkg__store="""\
                def load(key):
                    raise KeyError(key)
            """,
            pkg__service="""\
                from . import store
                from .store import load as fetch
                import pkg.store

                def a():
                    return store.load(1)

                def b():
                    return fetch(1)

                def c():
                    return pkg.store.load(1)
            """,
        )
        assert project.errors == []
        for name in ("pkg.service.a", "pkg.service.b", "pkg.service.c"):
            assert (
                reports[name].raises["KeyError"].render() == "pkg/store.py:2 via pkg.store.load()"
            )


class TestKnownCalls:
    def test_standard_library_table(self):
        source = """\
            import json
            from datetime import datetime
            from json import loads as parse

            def f(path, text):
                with open(path) as handle:
                    json.load(handle)
                parse(text)
                datetime.strptime(text, "%Y")
                len(text)
        """
        _, reports = analyse(m=source)
        assert list(reports["m.f"].raises) == ["FileNotFoundError", "JSONDecodeError", "ValueError"]
        assert reports["m.f"].unresolved_calls == 0

    def test_base_class_handler_catches_known_call(self):
        source = """\
            import json

            def f(text):
                try:
                    return json.loads(text)
                except ValueError:
                    return None
        """
        assert raised(source) == []

    def test_local_definition_shadows_the_table(self):
        assert raised("def open(path): ...\ndef f():\n    open('x')\n") == []


class TestDocstrings:
    def test_no_docstring_is_distinct_from_no_raises(self):
        assert documented_raises(None) is None
        assert documented_raises("Summary only.") == frozenset()

    def test_sphinx(self):
        doc = (
            "Do it.\n\n:param x: thing\n:raises ValueError: bad\n"
            ":raise pkg.errors.NotFound, KeyError: x"
        )
        assert documented_raises(doc) == {"ValueError", "NotFound", "KeyError"}

    def test_google(self):
        doc = textwrap.dedent(
            """\
            Do it.

            Args:
                x: thing

            Raises:
                ValueError: if bad
                    continuation line: not an entry
                errors.NotFound: if missing

            Returns:
                TypeError: this is not a raise
            """
        )
        assert documented_raises(doc) == {"ValueError", "NotFound"}

    def test_numpy(self):
        doc = textwrap.dedent(
            """\
            Do it.

            Raises
            ------
            ValueError
                If bad.
            errors.NotFound
                If missing.

            Returns
            -------
            TypeError
                Not a raise.
            """
        )
        assert documented_raises(doc) == {"ValueError", "NotFound"}

    def test_prose_mentioning_raises_is_not_a_section(self):
        assert documented_raises("This function\nraises\nnothing useful.") == frozenset()


CHECK_SOURCE = '''\
import json


def documented(text):
    """Parse.

    Raises
    ------
    ValueError
        If the text is not JSON.
    """
    return json.loads(text)


def undocumented(text):
    """Parse without saying how it fails."""
    return json.loads(text)


def stale(client):
    """Send.

    :raises TimeoutError: never, as far as the analysis can tell
    """
    return client.send()


def no_docstring():
    raise KeyError


def _private():
    """Hidden."""
    raise KeyError
'''


class TestCheck:
    def test_base_class_documentation_covers_subclass(self):
        project, reports = analyse(m=CHECK_SOURCE)
        assert project.undocumented(reports["m.documented"]) == []
        assert project.stale(reports["m.documented"]) == []

    def test_undocumented_and_stale(self):
        project, reports = analyse(m=CHECK_SOURCE)
        assert project.undocumented(reports["m.undocumented"]) == ["JSONDecodeError"]
        assert project.stale(reports["m.stale"]) == ["TimeoutError"]
        assert project.undocumented(reports["m.no_docstring"]) == []
        assert project.stale(reports["m.no_docstring"]) == []


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text(CHECK_SOURCE, encoding="utf-8")
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "latin.py").write_bytes(b"x = '\xe9'\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text("def test():\n    raise ValueError\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "v.py").write_text("def v():\n    raise ValueError\n")
    return tmp_path


class TestCli:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("pkg/mod.py", ("pkg.mod", False)),
            ("src/pkg/__init__.py", ("pkg", True)),
            ("tests/test_x.py", None),
            ("pkg/test_x.py", None),
        ],
    )
    def test_module_name(self, path, expected):
        assert module_name(path) == expected

    def test_map_text(self, tree, capsys):
        assert main(["map", str(tree)]) == 0
        captured = capsys.readouterr()
        assert "m.py:4  m.documented" in captured.out
        assert "JSONDecodeError  m.py:12" in captured.out
        assert "m.stale" not in captured.out and "_private" not in captured.out
        assert "3 of 4 function(s) shown." in captured.out
        assert "not parsed: bad.py:1" in captured.err

    def test_map_all_and_private(self, tree, capsys):
        assert main(["map", str(tree), "--all", "--include-private"]) == 0
        output = capsys.readouterr().out
        assert "m.stale  [1 unresolved call(s)]" in output
        assert "(no escaping exception found)" in output
        assert "m._private" in output

    def test_map_json(self, tree, capsys):
        assert main(["map", str(tree), "--format", "json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [item["function"] for item in payload] == [
            "m.documented",
            "m.undocumented",
            "m.no_docstring",
        ]
        assert payload[0]["raises"]["JSONDecodeError"] == {"site": "m.py:12", "via": None}

    def test_check_text(self, tree, capsys):
        assert main(["check", str(tree)]) == 1
        output = capsys.readouterr().out
        assert "RM001  m.undocumented  raises JSONDecodeError (m.py:17)" in output
        assert "RM002  m.stale  documents TimeoutError" in output
        assert "1 call(s) could not be resolved" in output
        assert "1 undocumented, 1 documented but not found, across 4 function(s)." in output

    def test_check_json_and_strict(self, tmp_path, capsys):
        (tmp_path / "m.py").write_text(
            'def f():\n    """Doc.\n\n    :raises KeyError: no\n    """\n', encoding="utf-8"
        )
        assert main(["check", str(tmp_path / "m.py"), "--format", "json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [item["rule"] for item in payload] == ["RM002"]
        assert "could not be resolved" not in payload[0]["message"]
        assert main(["check", str(tmp_path), "--strict"]) == 1
        capsys.readouterr()

    def test_missing_path(self, tmp_path, capsys):
        assert main(["map", str(tmp_path / "absent")]) == 2
        assert "No such file" in capsys.readouterr().err
