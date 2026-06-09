#!/usr/bin/env python3
"""Hidden acceptance oracle for spike 3 mathutils functions. Imports whatever
the worker(s) wrote and checks correctness. Not shown to the models."""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).parent


def _load(modname, filename):
    path = HERE / filename
    if not path.exists():
        return None, f"missing file {filename}"
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, f"import error: {e}"
    return mod, None


CHECKS = {
    "is_prime": ("is_prime.py", "is_prime", [
        (2, True), (3, True), (4, False), (1, False), (17, True), (15, False), (97, True),
    ]),
    "gcd": ("gcd.py", "gcd", [
        ((12, 8), 4), ((17, 5), 1), ((100, 10), 10), ((0, 5), 5),
    ]),
    "fibonacci": ("fibonacci.py", "fibonacci", [
        (0, 0), (1, 1), (2, 1), (3, 2), (7, 13), (10, 55),
    ]),
    "factorial": ("factorial.py", "factorial", [
        (0, 1), (1, 1), (5, 120), (6, 720),
    ]),
    "roman_numeral": ("roman_numeral.py", "roman_numeral", [
        (1, "I"), (4, "IV"), (9, "IX"), (40, "XL"), (90, "XC"), (2024, "MMXXIV"), (3888, "MMMDCCCLXXXVIII"),
    ]),
}


def main():
    total_pass = 0
    total = 0
    summary = {}
    for fn, (filename, attr, cases) in CHECKS.items():
        mod, err = _load(fn, filename)
        if err:
            summary[fn] = f"LOAD FAIL: {err}"
            total += len(cases)
            continue
        f = getattr(mod, attr, None)
        if f is None:
            summary[fn] = f"no function {attr} in {filename}"
            total += len(cases)
            continue
        ok = 0
        bad = []
        for args, exp in cases:
            total += 1
            a = args if isinstance(args, tuple) else (args,)
            try:
                got = f(*a)
            except Exception as e:
                bad.append(f"{args}->EXC {e}")
                continue
            if got == exp:
                ok += 1
                total_pass += 1
            else:
                bad.append(f"{args}->{got!r} (want {exp!r})")
        summary[fn] = f"{ok}/{len(cases)} pass" + (f"  fails: {bad}" if bad else "")
    print("=== ORACLE RESULTS ===")
    for fn, res in summary.items():
        print(f"  {fn}: {res}")
    print(f"TOTAL: {total_pass}/{total} cases pass")
    return 0 if total_pass == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
