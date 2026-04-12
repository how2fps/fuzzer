#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib._bootstrap_external as bootstrap_external
import types
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader, PKG_ITEM_PYZ
from PyInstaller.loader.pyimod01_archive import (
    ZlibArchiveReader,
    PYZ_ITEM_MODULE,
    PYZ_ITEM_PKG,
    PYZ_ITEM_NSPKG,
)


DEFAULT_BINARIES: dict[str, Path] = {
    "cidrize-runner": Path("targets/cidrize-runner/bin/linux-cidrize-runner"),
    "ipv4-parser": Path("targets/IPv4-IPv6-parser/bin/mac-ipv4-parser"),
    "ipv6-parser": Path("targets/IPv4-IPv6-parser/bin/mac-ipv6-parser"),
}


def _find_pyz_entry(archive: CArchiveReader) -> str:
    for name, entry in archive.toc.items():
        if entry[-1] == PKG_ITEM_PYZ:
            return name
    raise RuntimeError("no PYZ archive found in executable")


def _module_output_path(module_name: str, typecode: int, out_dir: Path) -> Path:
    parts = module_name.split(".")
    if typecode == PYZ_ITEM_PKG:
        return out_dir / Path(*parts) / "__init__.pyc"
    return out_dir / Path(*parts).with_suffix(".pyc")


def _write_pyc(code_obj: types.CodeType, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    pyc_bytes = bootstrap_external._code_to_timestamp_pyc(
        code_obj, mtime=0, source_size=0
    )
    dest.write_bytes(pyc_bytes)


def extract_pyc_from_binary(binary: Path, out_dir: Path) -> tuple[int, int]:
    archive = CArchiveReader(str(binary))
    pyz_name = _find_pyz_entry(archive)
    pyz = archive.open_embedded_archive(pyz_name)
    if not isinstance(pyz, ZlibArchiveReader):
        raise RuntimeError("embedded archive is not a PYZ archive")

    modules_written = 0
    skipped = 0
    for name, (typecode, _offset, _length) in pyz.toc.items():
        if typecode == PYZ_ITEM_NSPKG:
            skipped += 1
            continue
        if typecode not in (PYZ_ITEM_MODULE, PYZ_ITEM_PKG):
            skipped += 1
            continue
        code_obj = pyz.extract(name, raw=False)
        if not isinstance(code_obj, types.CodeType):
            skipped += 1
            continue
        dest = _module_output_path(name, typecode, out_dir)
        _write_pyc(code_obj, dest)
        modules_written += 1

    return modules_written, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract .pyc modules from PyInstaller executables."
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("blackbox_disam/extracted_pyc"),
        help="Root output directory for extracted pyc trees.",
    )
    parser.add_argument(
        "--binary",
        action="append",
        nargs=2,
        metavar=("LABEL", "PATH"),
        help="Override default binary mapping. Can be repeated.",
    )
    args = parser.parse_args()

    if args.binary:
        binaries = {label: Path(path) for label, path in args.binary}
    else:
        binaries = DEFAULT_BINARIES

    out_root: Path = args.out_root
    out_root.mkdir(parents=True, exist_ok=True)

    for label, binary in binaries.items():
        if not binary.is_file():
            raise SystemExit(f"Missing binary: {binary}")
        out_dir = out_root / label / "pyc"
        modules_written, skipped = extract_pyc_from_binary(binary, out_dir)
        print(
            f"{label}: wrote {modules_written} modules to {out_dir} "
            f"(skipped {skipped})"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
