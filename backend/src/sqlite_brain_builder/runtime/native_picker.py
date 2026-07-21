from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


class NativePickerError(RuntimeError):
    pass


def _initial_directory(value: str | Path | None) -> str | None:
    if value:
        path = Path(value).expanduser()
        if path.is_file():
            path = path.parent
        if path.is_dir():
            return str(path.resolve())
    return None


def _tk_dialog() -> tuple[object, object]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise NativePickerError("NATIVE_WINDOWS_PICKER_UNAVAILABLE") from exc
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    return root, filedialog


def choose_directory(*, title: str, initial_directory: str | Path | None = None) -> list[Path]:
    root, filedialog = _tk_dialog()
    try:
        value = filedialog.askdirectory(
            parent=root,
            title=title,
            initialdir=_initial_directory(initial_directory),
            mustexist=True,
        )
    finally:
        root.destroy()
    if not value:
        return []
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise NativePickerError("NATIVE_PICKER_DIRECTORY_INVALID")
    return [path]


def choose_files(
    *,
    title: str,
    extensions: Iterable[str],
    multiple: bool,
    initial_directory: str | Path | None = None,
    filter_description: str | None = None,
) -> list[Path]:
    allowed = sorted({str(value).casefold() for value in extensions if str(value).startswith(".")})
    if not allowed:
        raise NativePickerError("NATIVE_PICKER_EXTENSION_ALLOWLIST_REQUIRED")
    patterns = " ".join(f"*{extension}" for extension in allowed)
    description = str(filter_description or "Allowed files").strip() or "Allowed files"
    filetypes = [(f"{description} ({patterns})", patterns)]
    filetypes.extend(
        (f"{extension.removeprefix('.').upper()} files (*{extension})", f"*{extension}")
        for extension in allowed
    )
    root, filedialog = _tk_dialog()
    try:
        options = {
            "parent": root,
            "title": title,
            "initialdir": _initial_directory(initial_directory),
            # No catch-all row is supplied.  Windows Explorer therefore shows
            # the lane's complete allowlist plus one explicit row per allowed
            # type in its file-type dropdown.
            "filetypes": filetypes,
        }
        values = filedialog.askopenfilenames(**options) if multiple else [filedialog.askopenfilename(**options)]
    finally:
        root.destroy()
    paths = [Path(value).expanduser().resolve() for value in values if value]
    for path in paths:
        if not path.is_file() or path.suffix.casefold() not in allowed:
            raise NativePickerError(f"NATIVE_PICKER_FILE_TYPE_INVALID:{path.suffix.casefold() or '<none>'}")
    deduplicated: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            deduplicated.append(path)
    return deduplicated


__all__ = ["NativePickerError", "choose_directory", "choose_files"]
