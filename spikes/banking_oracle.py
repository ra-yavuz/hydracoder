#!/usr/bin/env python3
"""Hidden oracle for spike 4: a COUPLED multi-file task. The worker must build
three files that depend on each other:

  bank_account.py : class BankAccount(owner, balance=0) with deposit(amount),
                    withdraw(amount) (raises ValueError on overdraft),
                    and a balance attribute.
  bank.py         : class Bank() that imports BankAccount; open_account(owner)
                    -> BankAccount, total_assets() -> sum of all balances,
                    transfer(from_owner, to_owner, amount).
  bankcli.py      : exposes a function run(args: list[str]) -> str that drives
                    the Bank: supports ["open","alice"], ["deposit","alice","100"],
                    ["balance","alice"], ["transfer","alice","bob","30"].

The test below exercises CROSS-FILE integration: bank.py must use BankAccount's
real interface, and bankcli.py must use Bank's real interface. If any file
drifts from the others' signatures, integration fails even if each file is
syntactically valid on its own. That is exactly what we are testing."""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).parent


def _load(modname, filename):
    path = HERE / filename
    if not path.exists():
        return None, f"missing file {filename}"
    # ensure sibling imports (bank.py importing bank_account) resolve
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, f"import error: {type(e).__name__}: {e}"
    return mod, None


def main():
    results = []

    def check(desc, cond):
        results.append((desc, bool(cond)))

    # Layer 1: BankAccount
    ba_mod, err = _load("bank_account", "bank_account.py")
    if err:
        print("FATAL:", err); _summary(results); return 1
    BankAccount = getattr(ba_mod, "BankAccount", None)
    check("BankAccount class exists", BankAccount is not None)
    if BankAccount:
        try:
            a = BankAccount("alice")
            a.deposit(100)
            check("deposit updates balance", a.balance == 100)
            a.withdraw(30)
            check("withdraw updates balance", a.balance == 70)
            overdrew = False
            try:
                a.withdraw(1000)
            except ValueError:
                overdrew = True
            check("overdraft raises ValueError", overdrew)
        except Exception as e:
            check(f"BankAccount usable ({e})", False)

    # Layer 2: Bank uses BankAccount (CROSS-FILE)
    bank_mod, err = _load("bank", "bank.py")
    if err:
        check(f"bank.py imports cleanly ({err})", False); _summary(results); return 1
    Bank = getattr(bank_mod, "Bank", None)
    check("Bank class exists", Bank is not None)
    if Bank:
        try:
            bk = Bank()
            acc = bk.open_account("alice")
            check("Bank.open_account returns a BankAccount", acc.__class__.__name__ == "BankAccount")
            acc.deposit(200)
            bk.open_account("bob")
            check("Bank.total_assets sums balances", bk.total_assets() == 200)
            bk.transfer("alice", "bob", 50)
            check("Bank.transfer moves money (alice=150)", _balance(bk, "alice") == 150)
            check("Bank.transfer moves money (bob=50)", _balance(bk, "bob") == 50)
        except Exception as e:
            check(f"Bank cross-file integration ({type(e).__name__}: {e})", False)

    # Layer 3: CLI uses Bank (CROSS-FILE)
    cli_mod, err = _load("bankcli", "bankcli.py")
    if err:
        check(f"bankcli.py imports cleanly ({err})", False); _summary(results); return 1
    run = getattr(cli_mod, "run", None)
    check("bankcli.run exists", run is not None)
    if run:
        try:
            run(["open", "alice"])
            run(["deposit", "alice", "100"])
            out = run(["balance", "alice"])
            check("CLI balance reflects deposit (contains '100')", "100" in str(out))
        except Exception as e:
            check(f"CLI cross-file integration ({type(e).__name__}: {e})", False)

    return _summary(results)


def _balance(bk, owner):
    # tolerate different internal storage; prefer a public accessor if present
    for attr in ("get_balance", "balance_of"):
        if hasattr(bk, attr):
            return getattr(bk, attr)(owner)
    # fall back to scanning common containers
    for cont in ("accounts", "_accounts"):
        d = getattr(bk, cont, None)
        if isinstance(d, dict) and owner in d:
            acc = d[owner]
            return getattr(acc, "balance", None)
    return None


def _summary(results):
    print("=== SPIKE 4 ORACLE (coupled multi-file) ===")
    npass = sum(1 for _, ok in results if ok)
    for desc, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
    print(f"TOTAL: {npass}/{len(results)} checks pass")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
