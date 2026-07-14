"""
test_evaluator.py — unit tests for the Evaluator base loop (Phase 5 step 1).

Verifies the shared loop mechanics with fake hooks: scope iteration, skip, multi-signal
(bake-off) evaluate, on_signal accounting, per-item/per-signal error isolation, after_cycle.

Run: python -m tests.test_evaluator
"""
import sys
from execution.evaluator import Evaluator


class _Fake(Evaluator):
    def __init__(self, items, evals, skips=(), raise_on=None, on_signal_false=()):
        super().__init__("fake")
        self._items = items
        self._evals = evals            # {item: [signals]}
        self._skips = set(skips)
        self._raise_on = raise_on      # item that raises in evaluate()
        self._false = set(on_signal_false)
        self.seen = []                 # signals passed to on_signal
        self.after_called_with = None

    def scope(self, now):
        return self._items

    def skip(self, item, now):
        return item in self._skips

    def evaluate(self, item, now):
        if item == self._raise_on:
            raise ValueError("boom")
        return self._evals.get(item, [])

    def on_signal(self, signal, now):
        self.seen.append(signal)
        return signal not in self._false

    def after_cycle(self, acted, now):
        self.after_called_with = list(acted)


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}]  {name}")
    return cond


def main():
    ok = True

    # 1. basic: 3 items, one signal each → 3 acted
    e = _Fake(["A", "B", "C"], {"A": ["a"], "B": ["b"], "C": ["c"]})
    acted = e.evaluate_once()
    ok &= _check("basic 3-item cycle acts on 3 signals", acted == ["a", "b", "c"])
    ok &= _check("cycle_count increments", e._cycle_count == 1)
    ok &= _check("after_cycle gets acted list", e.after_called_with == ["a", "b", "c"])

    # 2. skip: B skipped
    e = _Fake(["A", "B", "C"], {"A": ["a"], "B": ["b"], "C": ["c"]}, skips=["B"])
    ok &= _check("skipped item not evaluated", e.evaluate_once() == ["a", "c"])

    # 3. bake-off: one item yields multiple signals
    e = _Fake(["A"], {"A": ["a1", "a2", "a3"]})
    ok &= _check("multi-signal (bake-off) item acts on all", e.evaluate_once() == ["a1", "a2", "a3"])

    # 4. on_signal False → not counted as acted
    e = _Fake(["A"], {"A": ["a1", "a2"]}, on_signal_false=["a2"])
    ok &= _check("on_signal False excluded from acted", e.evaluate_once() == ["a1"])
    ok &= _check("on_signal still called for rejected", "a2" in e.seen)

    # 5. error isolation: B raises in evaluate() but A and C still processed
    e = _Fake(["A", "B", "C"], {"A": ["a"], "C": ["c"]}, raise_on="B")
    ok &= _check("evaluate() error on one item doesn't abort cycle", e.evaluate_once() == ["a", "c"])

    # 6. empty/None evaluate → no signals, no crash
    e = _Fake(["A"], {"A": []})
    ok &= _check("empty evaluate yields nothing", e.evaluate_once() == [])

    print("\n" + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
