"""Finds and runs Poppler's pdftotext, so the engine does not depend on the PATH.

Where pdftotext is looked for, first match wins:
  1. the environment variable POPPLER_PATH (the folder that contains pdftotext)
  2. poppler_path in config/settings.yml
  3. the PATH
A copy bundled with Git for Windows (...\\Git\\mingw64\\bin) is skipped when another one exists,
because its older build can lay out text differently from what the templates were tested with.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import yaml

EXE = "pdftotext.exe" if os.name == "nt" else "pdftotext"
SETTINGS_FILE = "settings.yml"


class PopplerError(RuntimeError):
    """pdftotext cannot be found or cannot read a PDF; the message says what to do."""


@dataclass(frozen=True)
class Pdftotext:
    path: str
    version: str
    source: str            # where it was found: POPPLER_PATH, settings.yml or PATH


_FOUND: Pdftotext | None = None


def _version(exe: str) -> str | None:
    try:
        proc = subprocess.run([exe, "-v"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"pdftotext version ([\d.]+)", (proc.stdout + proc.stderr).decode("utf-8", "replace"))
    return match.group(1) if match else None


def _in_folder(folder: str) -> str | None:
    exe = Path(folder.strip().strip('"')) / EXE
    return str(exe) if exe.is_file() else None


def _on_path() -> list[str]:
    found = []
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        exe = _in_folder(folder) if folder else None
        if exe and exe not in found:
            found.append(exe)
    return found


def _is_git_copy(exe: str) -> bool:
    return re.search(r"[\\/]git[\\/]", exe, re.IGNORECASE) is not None


def configure(config_dir: Path | None = None) -> Pdftotext:
    """Locate pdftotext once (call at start-up); raises PopplerError with instructions."""
    global _FOUND
    tried: list[str] = []
    candidates: list[tuple[str, str]] = []

    env = os.environ.get("POPPLER_PATH")
    if env:
        exe = _in_folder(env)
        tried.append(f"POPPLER_PATH={env}" + ("" if exe else "  (no pdftotext there)"))
        if exe:
            candidates.append((exe, "POPPLER_PATH"))

    settings = Path(config_dir) / SETTINGS_FILE if config_dir else None
    if settings and settings.is_file():
        data = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
        folder = data.get("poppler_path")
        if folder:
            exe = _in_folder(str(folder))
            tried.append(f"{settings.name}: poppler_path={folder}" + ("" if exe else "  (no pdftotext there)"))
            if exe:
                candidates.append((exe, settings.name))

    on_path = _on_path()
    tried.append("PATH: " + (", ".join(on_path) if on_path else "no pdftotext"))
    preferred = [e for e in on_path if not _is_git_copy(e)] or on_path
    candidates += [(e, "PATH") for e in preferred]

    for exe, source in candidates:
        version = _version(exe)
        if version:
            _FOUND = Pdftotext(exe, version, source)
            return _FOUND
        tried.append(f"{exe}: does not run")
    raise PopplerError(
        "pdftotext (Poppler) was not found.\n  Looked in:\n    - " + "\n    - ".join(tried) +
        "\n  Fix: set poppler_path in config\\settings.yml to the folder that contains pdftotext.exe,"
        "\n       for example  poppler_path: C:\\Tools\\poppler\\Library\\bin")


def pdftotext() -> Pdftotext:
    return _FOUND or configure(None)


# Results of pdftotext, kept while the program runs: the automatic template checks read the
# same stored samples once per template and per check (regression, stress test, overlap), and a
# PDF that is read again gives the same text. One run of pdftotext per file and option set is
# enough; on Windows starting it costs about as much as reading the file. A file that changed
# (other size or modification time) is read again. At most _CACHE_BYTES of text are kept.
_CACHE: "OrderedDict[tuple, tuple[str, str, str | None]]" = OrderedDict()
_CACHE_BYTES = 256 * 2**20
_cached_bytes = 0


def _key(tool: str, args: list[str], pdf: Path) -> tuple | None:
    try:
        stat = Path(pdf).stat()
        return (tool, tuple(args), str(Path(pdf).resolve()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


def run(args: list[str], pdf: Path, warnings: list[str] | None = None) -> str:
    """Run pdftotext with these options on one PDF and return its text output (UTF-8).
    Messages Poppler prints on a file it could still read (e.g. "Syntax Error ...", a sign of a
    damaged file) are appended to `warnings` when a list is given."""
    global _cached_bytes
    pdf = Path(pdf)
    tool = pdftotext()
    key = _key(tool.path, args, pdf)
    if key is not None and key in _CACHE:
        _CACHE.move_to_end(key)
        out, err, error = _CACHE[key]
    else:
        try:
            proc = subprocess.run([tool.path, *args, str(pdf), "-"], capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise PopplerError(f"{pdf.name}: pdftotext took longer than 5 minutes") from None
        err = proc.stderr.decode("utf-8", "replace")
        out = proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""
        error = _explain(err) if proc.returncode != 0 else None
        if key is not None:
            _CACHE[key] = (out, err, error)
            _cached_bytes += len(out) + len(err)
            while _cached_bytes > _CACHE_BYTES and len(_CACHE) > 1:
                _, (o, e, _x) = _CACHE.popitem(last=False)
                _cached_bytes -= len(o) + len(e)
    if error is not None:
        raise PopplerError(f"{pdf.name}: {error}")
    if warnings is not None:
        warnings.extend(line.strip() for line in err.splitlines() if line.strip())
    return out


def tool_path(name: str) -> str | None:
    """Another Poppler program (pdfimages, pdfinfo) from the same folder as pdftotext."""
    folder = Path(pdftotext().path).parent
    exe = folder / (name + (".exe" if os.name == "nt" else ""))
    return str(exe) if exe.is_file() else shutil.which(name)


def _explain(stderr: str) -> str:
    text = stderr.strip()
    low = text.lower()
    if "password" in low:
        return "the PDF is password-protected; save an unprotected copy and process that"
    if "couldn't open" in low or "no such file" in low:
        return "the file could not be opened (moved, locked by another program, or no permission)"
    if "may not be a pdf" in low or "xref" in low or "syntax error" in low or "couldn't find trailer" in low:
        return "the file is damaged or is not a real PDF (" + text.splitlines()[0] + ")"
    return "pdftotext failed: " + (text.splitlines()[0] if text else "no message")
