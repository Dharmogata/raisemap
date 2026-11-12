# raisemap

A static exception map for Python: which exceptions can escape each function,
where they come from, and where the docstring disagrees.

Python does not declare exceptions, so "what can this call raise?" is answered
by reading every function it calls, and every function those call. `raisemap`
does that reading. It follows explicit `raise` statements through the project's
call graph, subtracts what `try`/`except` catches, and compares the result with
the `Raises` section of each docstring.

```console
$ raisemap map examples/orders
store.py:12  store.read_orders
    FileNotFoundError  store.py:14
    JSONDecodeError    store.py:15
store.py:18  store.find_order
    FileNotFoundError  store.py:14 via store.read_orders()
    JSONDecodeError    store.py:15 via store.read_orders()
    OrderNotFound      store.py:29
store.py:32  store.order_total
    FileNotFoundError  store.py:14 via store.find_order()
    JSONDecodeError    store.py:15 via store.find_order()

3 of 3 function(s) shown.
```

`order_total` wraps its call in `except OrderError`, so `OrderNotFound` — a
subclass — does not escape it, while the two I/O errors from two calls down do.

```console
$ raisemap check examples/orders
store.py:12  RM001  store.read_orders  raises FileNotFoundError (store.py:14) but the docstring does not document it
store.py:12  RM001  store.read_orders  raises JSONDecodeError (store.py:15) but the docstring does not document it
store.py:18  RM001  store.find_order  raises FileNotFoundError (store.py:14 via store.read_orders()) but the docstring does not document it
store.py:18  RM001  store.find_order  raises JSONDecodeError (store.py:15 via store.read_orders()) but the docstring does not document it
store.py:32  RM001  store.order_total  raises FileNotFoundError (store.py:14 via store.find_order()) but the docstring does not document it
store.py:32  RM001  store.order_total  raises JSONDecodeError (store.py:15 via store.find_order()) but the docstring does not document it
store.py:32  RM002  store.order_total  documents PermissionError but no raising path was found
6 undocumented, 1 documented but not found, across 3 function(s).
```

Both are real output from [`examples/orders`](examples/orders).

## Install

```bash
pip install git+https://github.com/Dharmogata/raisemap
```

Python 3.10 or newer. No dependencies.

## Usage

```bash
raisemap map [PATH]                 # exceptions escaping each function
raisemap map src --all              # include functions where none was found
raisemap map src --include-private
raisemap map src --format json
raisemap check [PATH]               # compare against docstrings; exit 1 on RM001
raisemap check src --strict         # also fail on RM002
```

`check` only judges functions that have a docstring. Sphinx (`:raises X:`),
Google (`Raises:`) and NumPy (`Raises` / `------`) styles are read. Documenting
a base class covers its subclasses, so `ValueError` covers `JSONDecodeError`.

## What the analysis sees

1. **Explicit raises** — `raise X(...)`, `raise X`, and bare `raise` or
   `raise error` inside a handler, which restore what the handler caught.
2. **Project calls** — functions, methods through `self`/`cls` (including
   inherited ones), constructors, and names reached through absolute or relative
   imports. Their exceptions propagate to the caller; the whole project is
   solved to a fixed point, so recursion and long chains are handled.
3. **Known standard-library calls** — a small table: `open`, `json.load(s)`,
   `tomllib.load(s)`, `subprocess.check_call/check_output`,
   `urllib.request.urlopen`, `datetime.strptime/fromisoformat`, and a few more.
4. **Handlers** — `try`/`except` with tuples, bare `except`, handler ordering,
   `else` and `finally`, and `contextlib.suppress`. Matching uses the real class
   hierarchy: builtins, known standard-library exceptions, and classes defined
   in the project.

## What it does not see

This is a lower bound, not a proof. An empty result means "nothing found", never
"cannot raise".

- **Calls on objects of unknown type** (`client.send()`), and anything from
  third-party libraries. Each function reports how many calls were unresolved,
  and `RM002` says so when that count is not zero.
- **Implicit exceptions** from operators and builtins: `d[key]`, `x / y`,
  `int(text)`, attribute access. In the example above, `order["id"]` can raise
  `KeyError` and is not reported.
- **Dynamic raises** such as `raise self.error_class(...)`. They are counted,
  not guessed.
- Decorators, context managers other than `suppress`, generators, `__exit__`
  swallowing, and exceptions raised in callbacks.
- Nested functions and nested classes are not analysed as separate units.

## Roadmap

- Opt-in implicit exceptions (`--implicit`) for subscripts, division and
  conversions, with local guards recognised.
- Type-informed resolution: annotations and simple local inference, so
  `client: Client` makes `client.send()` resolvable.
- Stub-style exception tables for popular libraries (`requests`, `httpx`,
  `sqlalchemy`), contributed as data files.
- `raisemap why FUNCTION EXCEPTION`: print the full propagation chain.
- Docstring fix-ups: generate the missing `Raises` entries in the file's style.
- Call-graph export (DOT/JSON) and SARIF output.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

MIT licensed.
