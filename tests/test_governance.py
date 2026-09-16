"""Run: python3 tests/test_governance.py (no network, stdlib only)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.governance import check_write_parents, verify_library_folder, WriteNotAllowed, status


class FakeFiles:
    def __init__(self, tree): self.tree = tree
    def get(self, fileId, fields):
        tree = self.tree
        class R:
            def execute(self_inner): return tree[fileId]
        return R()


class FakeService:
    def __init__(self, tree): self._files = FakeFiles(tree)
    def files(self): return self._files


def expect_raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


check_write_parents(["LIB"], "LIB")
expect_raises(WriteNotAllowed, lambda: check_write_parents(["OTHER"], "LIB"))
expect_raises(WriteNotAllowed, lambda: check_write_parents(["LIB", "OTHER"], "LIB"))
expect_raises(WriteNotAllowed, lambda: check_write_parents([], "LIB"))

live = {"LIB": {"name": "reading", "parents": ["K"]}, "K": {"name": "Knowledge", "parents": ["SB"]}, "SB": {"name": "Second Brain", "parents": ["ROOT"]}, "ROOT": {"name": "My Drive"}}
assert verify_library_folder(FakeService(live), "LIB") == "My Drive/Second Brain/Knowledge/reading"
assert status()["error"] is None

archived = {"LIB": {"name": "Reading Library", "parents": ["A"]}, "A": {"name": "_archive", "parents": ["ROOT"]}, "ROOT": {"name": "My Drive"}}
expect_raises(RuntimeError, lambda: verify_library_folder(FakeService(archived), "LIB"))
assert "archive" in status()["error"]
print("governance tests: 8 checks passed")
