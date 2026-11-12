"""Compute which exceptions can escape each function of a project.

The analysis is deliberately bounded. It sees:

* explicit ``raise`` statements;
* calls it can resolve to a function, method or class defined in the project,
  whose exceptions propagate to the caller;
* a table of standard-library calls with a well-known failure mode;
* ``try``/``except`` and ``contextlib.suppress``, which remove what they catch.

It does not see exceptions from calls on objects of unknown type, from
operators, or from third-party libraries. Each function reports how many calls
were left unresolved, so an empty result is never mistaken for a proof.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Mapping
from dataclasses import dataclass, field

from raisemap.docstrings import documented_raises
from raisemap.hierarchy import ancestors, is_caught_by

KNOWN_CALLS: dict[str, tuple[str, ...]] = {
    "open": ("FileNotFoundError",),
    "json.loads": ("JSONDecodeError",),
    "json.load": ("JSONDecodeError",),
    "tomllib.loads": ("TOMLDecodeError",),
    "tomllib.load": ("TOMLDecodeError",),
    "subprocess.check_call": ("CalledProcessError",),
    "subprocess.check_output": ("CalledProcessError",),
    "urllib.request.urlopen": ("URLError",),
    "os.remove": ("FileNotFoundError",),
    "os.unlink": ("FileNotFoundError",),
    "os.listdir": ("FileNotFoundError",),
    "os.stat": ("FileNotFoundError",),
    "importlib.import_module": ("ModuleNotFoundError",),
    "datetime.datetime.strptime": ("ValueError",),
    "datetime.datetime.fromisoformat": ("ValueError",),
    "datetime.date.fromisoformat": ("ValueError",),
    "uuid.UUID": ("ValueError",),
    "ipaddress.ip_address": ("ValueError",),
    "decimal.Decimal": ("InvalidOperation",),
}

MAXIMUM_PASSES = 100
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_TRY_NODES = (ast.Try, getattr(ast, "TryStar", ast.Try))


@dataclass(frozen=True)
class Origin:
    """Where an exception that escapes a function comes from.

    Parameters
    ----------
    site : str
        ``path:line`` of the ``raise`` statement or of the known call.
    via : str or None
        Dotted name of the project function it propagated through, when the
        exception was not raised in the reporting function itself.
    """

    site: str
    via: str | None = None

    def render(self) -> str:
        """Return a one-line description."""
        return f"{self.site} via {self.via}()" if self.via else self.site


@dataclass
class _Function:
    name: str
    module: str
    owner: str | None
    path: str
    node: ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class _Module:
    name: str
    path: str
    package: str
    imports: dict[str, str] = field(default_factory=dict)
    functions: dict[str, str] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)


@dataclass
class _Class:
    name: str
    module: str
    bases: tuple[ast.expr, ...]
    methods: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FunctionReport:
    """Analysis result for one function.

    Parameters
    ----------
    name : str
        Dotted name including module and class.
    path : str
        Source file.
    line : int
        One-based definition line.
    raises : dict of str to Origin
        Exceptions that can escape, by simple class name.
    documented : frozenset of str or None
        Exceptions named in the docstring; ``None`` without a docstring.
    unresolved_calls : int
        Calls whose target could not be determined.
    dynamic_raises : int
        ``raise`` statements whose exception class could not be determined.
    """

    name: str
    path: str
    line: int
    raises: dict[str, Origin]
    documented: frozenset[str] | None
    unresolved_calls: int
    dynamic_raises: int

    @property
    def is_public(self) -> bool:
        """Whether no component of the name after the module is private."""
        short = self.name.rsplit(".", 1)[-1]
        return short == "__init__" or not short.startswith("_")


def module_name(path: str) -> tuple[str, bool] | None:
    """Return ``(dotted_name, is_package)`` for a path, or ``None`` for tests."""
    parts = path[: -len(".py")].split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    is_package = bool(parts) and parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    for part in parts:
        if part in {"tests", "test", "conftest"} or part.startswith("test_"):
            return None
    return ".".join(parts), is_package


def _simple_name(node: ast.AST | None) -> str | None:
    """Return the last component of a name or attribute chain."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _looks_like_class(name: str) -> bool:
    return name[:1].isupper()


class Project:
    """A set of modules analysed together.

    Parameters
    ----------
    sources : Mapping of str to str
        POSIX-style relative ``.py`` paths and their contents.

    Attributes
    ----------
    errors : list of str
        Files that could not be parsed.
    """

    def __init__(self, sources: Mapping[str, str]) -> None:
        self.errors: list[str] = []
        self._modules: dict[str, _Module] = {}
        self._functions: dict[str, _Function] = {}
        self._classes: dict[str, _Class] = {}
        self._bases: dict[str, tuple[str, ...]] = {}
        self._summaries: dict[str, dict[str, Origin]] = {}
        self._unresolved: dict[str, int] = {}
        self._dynamic: dict[str, int] = {}
        for path in sorted(sources):
            named = module_name(path)
            if named is None:
                continue
            try:
                tree = ast.parse(sources[path])
            except SyntaxError as error:
                self.errors.append(f"{path}:{error.lineno}: {error.msg}")
                continue
            self._index(tree, path, *named)
        self._solve()

    # -- indexing ----------------------------------------------------------

    def _index(self, tree: ast.Module, path: str, name: str, is_package: bool) -> None:
        package = name if is_package else name.rpartition(".")[0]
        module = _Module(name, path, package)
        self._modules[name] = module
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        module.imports[alias.asname] = alias.name
                    else:
                        root = alias.name.split(".")[0]
                        module.imports[root] = root
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    parts = parts[: max(len(parts) - (node.level - 1), 0)]
                    base = ".".join([*parts, *([node.module] if node.module else [])])
                for alias in node.names:
                    target = f"{base}.{alias.name}" if base else alias.name
                    module.imports[alias.asname or alias.name] = target
        for statement in tree.body:
            if isinstance(statement, _FUNCTION_NODES):
                qualified = f"{name}.{statement.name}" if name else statement.name
                module.functions[statement.name] = qualified
                self._functions[qualified] = _Function(qualified, name, None, path, statement)
            elif isinstance(statement, ast.ClassDef):
                qualified = f"{name}.{statement.name}" if name else statement.name
                module.classes[statement.name] = qualified
                record = _Class(qualified, name, tuple(statement.bases))
                self._classes[qualified] = record
                self._bases[statement.name] = tuple(
                    base for base in map(_simple_name, statement.bases) if base
                )
                for member in statement.body:
                    if isinstance(member, _FUNCTION_NODES):
                        method = f"{qualified}.{member.name}"
                        record.methods[member.name] = method
                        self._functions[method] = _Function(method, name, qualified, path, member)

    # -- resolution --------------------------------------------------------

    def _dotted(self, node: ast.AST, module: _Module) -> str | None:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        if node.id in module.imports:
            root = module.imports[node.id]
        elif node.id in module.functions:
            root = module.functions[node.id]
        elif node.id in module.classes:
            root = module.classes[node.id]
        else:
            return None
        return ".".join([root, *reversed(parts)])

    def _method(
        self, class_name: str, method: str, seen: frozenset[str] = frozenset()
    ) -> str | None:
        record = self._classes.get(class_name)
        if record is None or class_name in seen:
            return None
        if method in record.methods:
            return record.methods[method]
        module = self._modules[record.module]
        for base in record.bases:
            target = self._dotted(base, module)
            if target:
                found = self._method(target, method, seen | {class_name})
                if found:
                    return found
        return None

    def _resolve(self, call: ast.Call, function: _Function) -> tuple[str, object] | None:
        """Return ``("function", name)``, ``("known", names)`` or ``("none", None)``.

        ``None`` means the target is unknown.
        """
        module = self._modules[function.module]
        target = call.func
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
            and function.owner
        ):
            method = self._method(function.owner, target.attr)
            return ("function", method) if method else None
        dotted = self._dotted(target, module)
        if dotted is None:
            if isinstance(target, ast.Name):
                if target.id in KNOWN_CALLS:
                    return "known", KNOWN_CALLS[target.id]
                if hasattr(builtins, target.id):
                    return "none", None
            return None
        if dotted in self._functions:
            return "function", dotted
        if dotted in self._classes:
            constructor = self._method(dotted, "__init__")
            return ("function", constructor) if constructor else ("none", None)
        if dotted in KNOWN_CALLS:
            return "known", KNOWN_CALLS[dotted]
        return None

    # -- escape analysis ---------------------------------------------------

    def _handler_types(self, handler: ast.ExceptHandler) -> frozenset[str] | None:
        """Return the class names a handler catches; ``None`` for a bare except."""
        if handler.type is None:
            return None
        nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        return frozenset(name for name in map(_simple_name, nodes) if name)

    def _escapes(
        self,
        node: ast.AST,
        function: _Function,
        handling: dict[str, Origin],
        bound: frozenset[str],
        counters: dict[str, int],
    ) -> dict[str, Origin]:
        if isinstance(node, _SCOPE_NODES):
            return {}
        result: dict[str, Origin] = {}

        def merge(found: Mapping[str, Origin]) -> None:
            for name, origin in found.items():
                result.setdefault(name, origin)

        def descend(children: list[ast.AST] | list[ast.stmt] | list[ast.expr]) -> dict[str, Origin]:
            collected: dict[str, Origin] = {}
            for child in children:
                for name, origin in self._escapes(
                    child, function, handling, bound, counters
                ).items():
                    collected.setdefault(name, origin)
            return collected

        if isinstance(node, _TRY_NODES):
            opaque_before = counters["unresolved"] + counters["dynamic"]
            remaining = descend(node.body)
            body_is_opaque = counters["unresolved"] + counters["dynamic"] > opaque_before
            for handler in node.handlers:
                types = self._handler_types(handler)
                caught = {
                    name: origin
                    for name, origin in remaining.items()
                    if is_caught_by(name, types, self._bases)
                }
                for name in caught:
                    del remaining[name]
                if not caught and not body_is_opaque:
                    continue  # nothing in the guarded body can reach this handler
                # A handler can re-raise what it names even when the guarded
                # body's raises are invisible to this analysis.
                if not caught and types:
                    site = f"{function.path}:{handler.lineno}"
                    caught = {name: Origin(site) for name in types}
                inner_bound = bound | {handler.name} if handler.name else bound
                for statement in handler.body:
                    merge(self._escapes(statement, function, caught, inner_bound, counters))
            merge(remaining)
            merge(descend(node.orelse))
            merge(descend(node.finalbody))
            return result

        if isinstance(node, ast.Raise):
            site = f"{function.path}:{node.lineno}"
            name = _simple_name(node.exc)
            if node.exc is None or (isinstance(node.exc, ast.Name) and node.exc.id in bound):
                merge(handling)
            elif name and (_looks_like_class(name) or name in self._bases):
                result[name] = Origin(site)
            else:
                counters["dynamic"] += 1
            if isinstance(node.exc, ast.Call):
                merge(descend([*node.exc.args, *(k.value for k in node.exc.keywords)]))
            return result

        if isinstance(node, (ast.With, ast.AsyncWith)):
            suppressed: set[str] = set()
            for item in node.items:
                merge(self._escapes(item.context_expr, function, handling, bound, counters))
                expression = item.context_expr
                if isinstance(expression, ast.Call) and _simple_name(expression) == "suppress":
                    suppressed.update(n for n in map(_simple_name, expression.args) if n)
            body = descend(node.body)
            if suppressed:
                types = frozenset(suppressed)
                body = {
                    name: origin
                    for name, origin in body.items()
                    if not is_caught_by(name, types, self._bases)
                }
            merge(body)
            return result

        if isinstance(node, ast.Call):
            resolved = self._resolve(node, function)
            site = f"{function.path}:{node.lineno}"
            if resolved is None:
                counters["unresolved"] += 1
            elif resolved[0] == "known":
                for name in resolved[1]:  # type: ignore[union-attr]
                    result.setdefault(name, Origin(site))
            elif resolved[0] == "function":
                callee = str(resolved[1])
                for name, origin in self._summaries.get(callee, {}).items():
                    result.setdefault(name, Origin(origin.site, callee))
        merge(descend(list(ast.iter_child_nodes(node))))
        return result

    def _solve(self) -> None:
        """Iterate to a fixed point so exceptions propagate through call chains."""
        self._summaries = {name: {} for name in self._functions}
        for _ in range(MAXIMUM_PASSES):
            changed = False
            for name, function in self._functions.items():
                counters = {"unresolved": 0, "dynamic": 0}
                summary: dict[str, Origin] = {}
                for statement in function.node.body:
                    for exception, origin in self._escapes(
                        statement, function, {}, frozenset(), counters
                    ).items():
                        summary.setdefault(exception, origin)
                self._unresolved[name] = counters["unresolved"]
                self._dynamic[name] = counters["dynamic"]
                if summary.keys() != self._summaries[name].keys():
                    changed = True
                self._summaries[name] = summary
            if not changed:
                return

    # -- results -----------------------------------------------------------

    def reports(self) -> list[FunctionReport]:
        """Return one report per function, in file and line order."""
        reports = [
            FunctionReport(
                name=name,
                path=function.path,
                line=function.node.lineno,
                raises=dict(sorted(self._summaries[name].items())),
                documented=documented_raises(ast.get_docstring(function.node)),
                unresolved_calls=self._unresolved.get(name, 0),
                dynamic_raises=self._dynamic.get(name, 0),
            )
            for name, function in self._functions.items()
        ]
        reports.sort(key=lambda report: (report.path, report.line))
        return reports

    def undocumented(self, report: FunctionReport) -> list[str]:
        """Return exceptions that escape but that the docstring does not cover.

        Documenting a base class covers its subclasses.
        """
        if report.documented is None:
            return []
        return [
            name for name in report.raises if not ancestors(name, self._bases) & report.documented
        ]

    def stale(self, report: FunctionReport) -> list[str]:
        """Return documented exceptions for which no raising path was found."""
        if report.documented is None:
            return []
        return sorted(
            documented
            for documented in report.documented
            if not any(documented in ancestors(name, self._bases) for name in report.raises)
        )


def analyse_sources(sources: Mapping[str, str]) -> Project:
    """Analyse a mapping of relative path to source text.

    Parameters
    ----------
    sources : Mapping of str to str
        POSIX-style relative ``.py`` paths and their contents.

    Returns
    -------
    Project
        The solved project; call :meth:`Project.reports` for results.
    """
    return Project(sources)
