"""Guard: no key literal reaches the Valkey client without going through ``valkey_key``/``valkey_pattern``.

A key built as a plain string (or an f-string) and handed to the client lands in the shared,
un-namespaced keyspace. The check is syntactic: for every call to a Valkey client method (or
``cache_get``), the key argument must be a call to ``valkey_key`` / ``valkey_pattern`` (or a
``*.snapshot_key`` / ``*.user_count_key`` builder), a name bound to anything but a string literal in
the same function (key-builder functions with ``cache_key`` in their name are checked to return a
helper call), or a function parameter (the caller is checked where it builds the key).
"""

import ast
from pathlib import Path

import pytest


API = Path(__file__).resolve().parent.parent / "api"

# Client methods whose first positional argument is a key (or, for scan/scan_iter, the ``match`` kwarg).
KEY_METHODS = frozenset(
    {
        "get",
        "set",
        "setex",
        "setnx",
        "getdel",
        "delete",
        "unlink",
        "exists",
        "incr",
        "decr",
        "incrby",
        "expire",
        "pexpire",
        "ttl",
        "pttl",
        "mget",
        "hget",
        "hset",
        "sadd",
        "srem",
        "smembers",
        "lpush",
        "rpush",
        "zadd",
        "type",
    }
)
SCAN_METHODS = frozenset({"scan", "scan_iter", "keys"})
KEY_BUILDERS = frozenset({"valkey_key", "valkey_pattern", "snapshot_key", "user_count_key", "_marker_key"})
# Modules that are not clients of the shared Valkey, or that take keys as parameters by design.
SKIP = frozenset({"valkey_keys.py"})


def _is_client(node: ast.expr) -> bool:
    """True for ``valkey``, ``_valkey``, ``self._valkey`` and similar receivers."""
    name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""
    return "valkey" in name.lower()


def _builder_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
    return name in KEY_BUILDERS


def _is_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)))


def _violations(path: Path, root: Path = API.parent) -> list[str]:
    tree = ast.parse(path.read_text())
    found: list[str] = []

    def check(call: ast.Call, key: ast.expr, scope: dict[str, ast.expr]) -> None:
        bad = False
        if _is_literal(key):
            bad = True
        elif isinstance(key, ast.Name) and key.id in scope:
            bad = _is_literal(scope[key.id])
        if bad:
            found.append(f"{path.relative_to(root)}:{call.lineno} passes an unprefixed key to the Valkey client")

    def walk(scope_node: ast.AST) -> None:
        scope: dict[str, ast.expr] = {}
        for node in ast.walk(scope_node):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                scope[node.targets[0].id] = node.value
        for node in ast.walk(scope_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and _is_client(func.value):
                if func.attr in KEY_METHODS and node.args:
                    check(node, node.args[0], scope)
                elif func.attr in SCAN_METHODS:
                    for kw in node.keywords:
                        if kw.arg == "match":
                            check(node, kw.value, scope)
                    if func.attr == "keys" and node.args:
                        check(node, node.args[0], scope)
            elif isinstance(func, ast.Name) and func.id == "cache_get" and len(node.args) >= 2:
                check(node, node.args[1], scope)

    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            walk(fn)
            if "cache_key" in fn.name:
                found.extend(
                    f"{path.relative_to(root)}:{ret.lineno} key builder {fn.name} returns an unprefixed key"
                    for ret in ast.walk(fn)
                    if isinstance(ret, ast.Return) and ret.value is not None and _is_literal(ret.value)
                )
    walk(tree)
    return sorted(set(found))


def test_no_valkey_call_passes_an_unprefixed_key() -> None:
    problems = [v for path in sorted(API.rglob("*.py")) if path.name not in SKIP for v in _violations(path)]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "source",
    [
        'await valkey.get(f"revoked:jti:{jti}")',
        'await self._valkey.setex("password_changed:u", 60, "1")',
        'key = f"nlq:{digest}"\nawait valkey.set(key, "v")',
        'await valkey.scan(cursor=0, match="recommend:*")',
        'await cache_get(valkey, f"search:{d}", cache="x")',
    ],
)
def test_the_guard_flags_unprefixed_literals(tmp_path: Path, source: str) -> None:
    probe = tmp_path / "probe.py"
    body = source.replace("\n", "\n    ")
    probe.write_text(f"async def f(valkey, jti=None, digest=None, d=None):\n    {body}\n")
    assert _violations(probe, tmp_path), source


def test_the_guard_accepts_helper_built_keys(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "async def f(valkey, jti, user_id):\n"
        '    key = valkey_key(f"x:{jti}")\n'
        "    await valkey.set(key, 1)\n"
        '    await valkey.get(valkey_key(f"y:{jti}"))\n'
        '    await valkey.scan(cursor=0, match=valkey_pattern("z:*"))\n'
    )
    assert _violations(probe, tmp_path) == []
