from pathlib import Path
import tempfile
import shutil

from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.workspace.workspace_db import init_workspace, create_brain


def _norm(x):
    return str(x).replace("/", "\\").lower()


def test_no_nested_brains_paths():
    assert _norm(brain_output_dir(r"C:\Users\Example\Downloads\_0000", "New Brain")).endswith(r"_0000\new_brain_output")
    assert _norm(brain_output_dir(r"C:\Users\Example\Downloads\_0000\brains", "New Brain")).endswith(r"_0000\new_brain_output")
    assert _norm(brain_output_dir(r"C:\Users\Example\Downloads\_0000\brains\brains", "New Brain")).endswith(r"_0000\new_brain_output")
    assert "brains\\brains" not in _norm(brain_output_dir(r"C:\Users\Example\Downloads\_0000\brains", "New Brain"))


def test_create_brain_uses_direct_child_folder():
    root = Path(tempfile.mkdtemp()) / "_0000"
    try:
        init_workspace(root / "brains")
        brain = create_brain(root / "brains", "New Brain")
        out = Path(brain["output_dir"])
        assert out == root / "new_brain_output"
        assert "brains/brains" not in out.as_posix().lower()
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)
