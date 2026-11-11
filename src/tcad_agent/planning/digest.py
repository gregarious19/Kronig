"""Lossless digest of external result files (JSON, YAML, text).

Nothing is dropped, truncated, or rounded. Large numeric arrays are the only thing moved out of
the digest body: with ``out_dir`` they are written verbatim to side files and the digest shows a
pointer with shape and range. In flat mode every leaf is printed as ``json.path = value`` so other
tools can cite and re-verify exact paths.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

EXTERNALIZE_ABOVE = 24
TEXT_SUFFIXES = {".json", ".yaml", ".yml", ".md", ".txt", ".csv"}


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _fmt(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False) if ("\n" in value or value == "") else value
    return repr(value)


def _numbers(value: Any) -> list[float] | None:
    out: list[float] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            out.append(float(item))
        else:
            return None
    return out


def _shape(value: Any) -> list[int]:
    shape = []
    while isinstance(value, list):
        shape.append(len(value))
        value = value[0] if value else None
    return shape


class _Digest:
    def __init__(self, out_dir: Path | None, source: Path) -> None:
        self.out_dir = out_dir
        self.source = source
        self.lines: list[str] = []
        self.externalized: list[str] = []

    def _pointer(self, value: list[Any], path: str) -> str | None:
        nums = _numbers(value)
        if nums is None or self.out_dir is None or len(nums) <= EXTERNALIZE_ABOVE:
            return None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{self.source.stem}__{path}").strip("_")
        target = self.out_dir / f"{slug}.json"
        target.write_text(json.dumps(value), encoding="utf-8")
        self.externalized.append(f"{path} → {target}")
        return (
            f"<numeric array shape={_shape(value)} n={len(nums)} "
            f"min={min(nums)!r} max={max(nums)!r} → {target}>"
        )

    def flat(self, value: Any, path: str) -> None:
        if _scalar(value):
            self.lines.append(f"{path or '$'} = {_fmt(value)}")
            return
        if isinstance(value, list):
            pointer = self._pointer(value, path)
            if pointer:
                self.lines.append(f"{path} = {pointer}")
                return
            if not value:
                self.lines.append(f"{path} = []")
            for i, item in enumerate(value):
                self.flat(item, f"{path}[{i}]")
            return
        if not value:
            self.lines.append(f"{path or '$'} = {{}}")
        for key, item in value.items():
            self.flat(item, f"{path}.{key}" if path else str(key))

    def nested(self, value: Any, path: str, indent: int, key: str | None) -> None:
        pad = "  " * indent
        label = "" if key is None else f"{pad}- " if key == "" else f"{pad}{key}: "
        if _scalar(value):
            self.lines.append(f"{label}{_fmt(value)}")
            return
        if isinstance(value, list):
            pointer = self._pointer(value, path)
            if pointer:
                self.lines.append(f"{label}{pointer}")
                return
            nums = _numbers(value)
            if nums is not None and len(_shape(value)) <= 2:
                self.lines.append(f"{label}{json.dumps(value)}")
                return
        if key is not None:
            self.lines.append(label.rstrip())
        child = indent + 1 if key is not None else indent
        if isinstance(value, dict):
            for k, v in value.items():
                self.nested(v, f"{path}.{k}" if path else str(k), child, str(k))
        else:
            for i, v in enumerate(value):
                self.nested(v, f"{path}[{i}]", child, "")


def _load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    if path.suffix == ".json":
        return json.loads(text)
    return text


def expand(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in TEXT_SUFFIXES)
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(p)
    return files


def render(paths: Iterable[Path], *, out_dir: Path | None = None, flat: bool = False) -> str:
    """Return the digest of every file under ``paths``; raises on unreadable input."""
    blocks: list[str] = []
    for f in expand(paths):
        data = _load(f)
        lines = [f"## {f.resolve()}"]
        if isinstance(data, str):
            lines.append(data)
        else:
            d = _Digest(out_dir, f)
            if flat:
                d.flat(data, "")
            else:
                d.nested(data, "", 0, None)
            lines += [line for line in d.lines if line.strip()]
            if d.externalized:
                lines += ["", "Externalized verbatim:", *(f"- {e}" for e in d.externalized)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
