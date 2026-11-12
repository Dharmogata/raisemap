"""Read the exceptions a docstring says a function raises."""

from __future__ import annotations

import re

_SPHINX = re.compile(r"^\s*:raises?\s+([^:]+):", re.MULTILINE)
_NAME = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)")


def _simple(name: str) -> str:
    return name.strip().rsplit(".", 1)[-1]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def documented_raises(docstring: str | None) -> frozenset[str] | None:
    """Return the exception names a docstring documents.

    Sphinx (``:raises X:``), Google (``Raises:``) and NumPy (``Raises`` with an
    underline) styles are recognised.

    Parameters
    ----------
    docstring : str or None
        The docstring, as returned by ``ast.get_docstring``.

    Returns
    -------
    frozenset of str or None
        Simple class names. ``None`` when there is no docstring at all, which
        is different from a docstring that documents no exceptions.
    """
    if docstring is None:
        return None
    names: set[str] = set()
    for match in _SPHINX.finditer(docstring):
        names.update(_simple(part) for part in match.group(1).split(",") if part.strip())

    lines = docstring.splitlines()
    index = 0
    while index < len(lines):
        header = lines[index].strip()
        if header.rstrip(":").lower() != "raises":
            index += 1
            continue
        section_indent = _indent(lines[index])
        index += 1
        numpy_style = index < len(lines) and set(lines[index].strip()) == {"-"}
        if numpy_style:
            index += 1
        elif not header.endswith(":"):
            continue
        entry_indent: int | None = None
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if not stripped:
                index += 1
                continue
            indent = _indent(line)
            if numpy_style:
                if indent < section_indent or (
                    indent == section_indent
                    and index + 1 < len(lines)
                    and set(lines[index + 1].strip()) == {"-"}
                ):
                    break
                if indent == section_indent:
                    match = _NAME.match(stripped)
                    if match:
                        names.add(_simple(match.group(1)))
            else:
                if indent <= section_indent:
                    break
                if entry_indent is None:
                    entry_indent = indent
                if indent == entry_indent:
                    match = _NAME.match(stripped)
                    if match and stripped[match.end() :].lstrip().startswith(":"):
                        names.add(_simple(match.group(1)))
            index += 1
    return frozenset(names)
