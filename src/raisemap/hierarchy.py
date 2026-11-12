"""Exception class hierarchy across builtins, the standard library and a project."""

from __future__ import annotations

import builtins
from collections.abc import Mapping

# Standard-library exceptions that are not builtins, mapped to their base.
KNOWN_BASES: dict[str, str] = {
    "JSONDecodeError": "ValueError",
    "TOMLDecodeError": "ValueError",
    "SubprocessError": "Exception",
    "CalledProcessError": "SubprocessError",
    "TimeoutExpired": "SubprocessError",
    "URLError": "OSError",
    "HTTPError": "URLError",
    "InvalidOperation": "ArithmeticError",
    "AddressValueError": "ValueError",
}

_CATCH_ALL = frozenset({"BaseException"})


def ancestors(name: str, project_bases: Mapping[str, tuple[str, ...]]) -> frozenset[str]:
    """Return ``name`` and every class it is known to derive from.

    Parameters
    ----------
    name : str
        Simple class name, without a module prefix.
    project_bases : Mapping of str to tuple of str
        Base class names of classes defined in the analysed project.

    Returns
    -------
    frozenset of str
        Names an ``except`` clause could use to catch ``name``. A class that is
        neither a builtin, a known standard-library exception nor defined in the
        project is assumed to derive from ``Exception``.
    """
    seen: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in project_bases:
            stack.extend(project_bases[current])
        elif current in KNOWN_BASES:
            stack.append(KNOWN_BASES[current])
        else:
            builtin = getattr(builtins, current, None)
            if isinstance(builtin, type) and issubclass(builtin, BaseException):
                seen.update(cls.__name__ for cls in builtin.__mro__ if cls is not object)
            else:
                seen.update({"Exception", "BaseException"})
    return frozenset(seen)


def is_caught_by(
    raised: str, handler_types: frozenset[str] | None, project_bases: Mapping[str, tuple[str, ...]]
) -> bool:
    """Return whether an ``except`` clause naming ``handler_types`` catches ``raised``.

    ``None`` stands for a bare ``except:``.
    """
    if handler_types is None or handler_types & _CATCH_ALL:
        return True
    return bool(ancestors(raised, project_bases) & handler_types)
