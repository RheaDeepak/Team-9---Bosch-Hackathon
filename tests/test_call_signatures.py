"""Static guard against the bug class behind "wait_for_recovery() takes from 3 to 5 positional arguments but 7 were given":
every call to a repo function (module.func(...) or an imported name) must fit that function's definition.
Calls using *args/**kwargs are skipped; methods are not checked, only functions and class constructors."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = {p.stem: p for p in ROOT.glob("*.py")}


def _definitions():
    defs = {}
    for name, path in MODULES.items():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef):
                defs[(name, node.name)] = node.args
            elif isinstance(node, ast.ClassDef):
                init = next((m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)
                if init is not None:
                    a = init.args
                    defs[(name, node.name)] = ast.arguments(posonlyargs=a.posonlyargs, args=a.args[1:], vararg=a.vararg,
                                                            kwonlyargs=a.kwonlyargs, kw_defaults=a.kw_defaults,
                                                            kwarg=a.kwarg, defaults=a.defaults)
    return defs


def _mismatch(args, call):
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return None
    names = [a.arg for a in args.posonlyargs + args.args]
    given = len(call.args)
    if given > len(names) and not args.vararg:
        return f"{given} positional arguments given, at most {len(names)} accepted"
    kws = {k.arg for k in call.keywords}
    kwonly = {a.arg for a in args.kwonlyargs}
    unknown = sorted(k for k in kws if k not in names[given:] and k not in kwonly and not args.kwarg)
    if unknown:
        return f"unexpected keyword argument(s) {unknown}"
    required = names[:len(names) - len(args.defaults)][given:] + [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
    missing = [r for r in required if r not in kws]
    return f"missing required argument(s) {missing}" if missing else None


class CallSignatureTest(unittest.TestCase):
    def test_every_call_to_a_repo_function_fits_its_definition(self):
        defs, problems = _definitions(), []
        for path in [*MODULES.values(), *(ROOT / "tests").glob("*.py")]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {(a.asname or a.name): (n.module, a.name) for n in ast.walk(tree)
                        if isinstance(n, ast.ImportFrom) and n.module in MODULES and n.level == 0 for a in n.names}
            for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
                f = call.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in MODULES:
                    key = (f.value.id, f.attr)
                elif isinstance(f, ast.Name) and f.id in imported:
                    key = imported[f.id]
                else:
                    continue
                if key in defs and (why := _mismatch(defs[key], call)):
                    problems.append(f"{path.name}:{call.lineno}: {key[0]}.{key[1]}(): {why}")
        self.assertEqual(problems, [])

    def test_the_checker_catches_the_original_bug(self):
        stale = ast.parse("def wait_for_recovery(plan, baseline, services, on_sample=None, on_progress=None): pass").body[0].args
        call = ast.parse("guard.wait_for_recovery(plan, baseline, services, emit, on_sample, stop, restart_limits)").body[0].value
        self.assertIn("7 positional arguments given, at most 5", _mismatch(stale, call))
        kw = ast.parse("guard.wait_for_recovery(plan, baseline, services, emit=emit, bogus=1)").body[0].value
        self.assertIn("unexpected keyword", _mismatch(stale, kw))
        fixed = _definitions()[("guard", "wait_for_recovery")]
        self.assertIsNone(_mismatch(fixed, ast.parse("guard.wait_for_recovery(plan, baseline, services, emit=emit, on_progress=f)").body[0].value))


if __name__ == "__main__":
    unittest.main()
