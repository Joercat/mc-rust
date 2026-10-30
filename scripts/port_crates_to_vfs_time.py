#!/usr/bin/env python3
"""
Rewrites `std::fs`, `std::env::current_dir()`, `Path::{exists,is_dir,is_file,canonicalize}`,
and `std::time::{Instant,SystemTime,UNIX_EPOCH,SystemTimeError}` in active Horizon crates
to use `horizon_core::vfs` and `horizon_core::time`.
"""
import os
import re

ACTIVE_CRATES = [
    "horizon-core",
    "horizon-threads",
    "horizon-hdr",
    "horizon-dualsense",
    "horizon-world",
    "horizon-client-core",
    "horizon-client-input",
    "horizon-client-render",
    "horizon-command",
    "horizon-items",
    "horizon-server-files",
    "horizon-assets",
    "horizon-protocol",
    "horizon-theme",
    "horizon-telemetry",
    "horizon-friends",
    "horizon-mods",
    "horizon-shaders",
    "horizon-shader-api",
    "horizon-release-sign",
    "horizon-client",
]

TIME_REDIRECT = {"Instant", "SystemTime", "UNIX_EPOCH", "SystemTimeError"}


def ensure_horizon_core_dep(crate_name: str):
    if crate_name == "horizon-core":
        return
    toml_path = f"crates/{crate_name}/Cargo.toml"
    txt = open(toml_path).read()
    if re.search(r"(?m)^horizon-core\s*=", txt):
        return
    if "[dependencies]" in txt:
        txt = txt.replace(
            "[dependencies]\n",
            '[dependencies]\nhorizon-core = { path = "../horizon-core" }\n',
            1,
        )
    else:
        txt += '\n[dependencies]\nhorizon-core = { path = "../horizon-core" }\n'
    open(toml_path, "w").write(txt)


def split_top_comma(s: str):
    parts = []
    depth = 0
    cur = []
    for ch in s:
        if ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            item = "".join(cur).strip()
            if item:
                parts.append(item)
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def expand_use_tree(prefix: str, expr: str):
    expr = expr.strip()
    if "{" not in expr:
        if expr == "self":
            return [prefix.rstrip(":")]
        return [f"{prefix}{expr}" if prefix else expr]
    brace_idx = expr.index("{")
    assert expr.endswith("}"), f"Unbalanced use tree: {expr}"
    sub_prefix = expr[:brace_idx].strip()
    full_prefix = f"{prefix}{sub_prefix}" if prefix else sub_prefix
    inner = expr[brace_idx + 1 : -1]
    out = []
    for item in split_top_comma(inner):
        out.extend(expand_use_tree(full_prefix, item))
    return out


def rewrite_std_use(match: re.Match, core_prefix: str) -> str:
    vis_and_use = match.group(1)  # e.g. "use " or "pub(crate) use "
    body = match.group(2)  # after `std::` up to `;`
    # Skip `std::os::unix::...`
    if body.lstrip().startswith("os::"):
        return match.group(0)
    # Check if fs or target time items appear at all
    if not re.search(r"\b(fs|Instant|SystemTime|UNIX_EPOCH|SystemTimeError)\b", body):
        return match.group(0)

    leaves = expand_use_tree("std::", body)
    kept_std = []
    extra_uses = []
    for leaf in leaves:
        if leaf == "std::fs":
            extra_uses.append(f"{vis_and_use}{core_prefix}::vfs as fs;")
        elif leaf.startswith("std::fs::"):
            rest = leaf[len("std::fs::") :]
            extra_uses.append(f"{vis_and_use}{core_prefix}::vfs::{rest};")
        elif leaf.startswith("std::time::"):
            rest = leaf[len("std::time::") :]
            base_ident = rest.split(" as ")[0].strip()
            if base_ident in TIME_REDIRECT:
                extra_uses.append(f"{vis_and_use}{core_prefix}::time::{rest};")
            else:
                kept_std.append(leaf)
        else:
            kept_std.append(leaf)

    stmts = []
    if kept_std:
        short = [x[len("std::") :] for x in kept_std]
        if len(short) == 1:
            stmts.append(f"{vis_and_use}std::{short[0]};")
        else:
            stmts.append(f"{vis_and_use}std::{{{', '.join(short)}}};")
    stmts.extend(extra_uses)
    return "\n".join(stmts)


def find_inner_attr_end(code: str) -> int:
    """Return index right after leading comments and `#![...]` inner attributes."""
    lines = code.splitlines(keepends=True)
    pos = 0
    in_block_comment = False
    for line in lines:
        s = line.strip()
        if in_block_comment:
            pos += len(line)
            if "*/" in s:
                in_block_comment = False
            continue
        if s.startswith("/*"):
            pos += len(line)
            if "*/" not in s:
                in_block_comment = True
            continue
        if not s or s.startswith("//") or s.startswith("#!["):
            pos += len(line)
            continue
        break
    return pos


def transform_code(code: str, core_prefix: str) -> tuple[str, bool]:
    orig = code

    # 1. Rewrite `use std::...;` statements
    use_pat = re.compile(
        r"((?:pub(?:\([^)]*\))?\s+)?use\s+)std::([^;]+);",
        re.DOTALL,
    )
    code = use_pat.sub(lambda m: rewrite_std_use(m, core_prefix), code)

    # 2. Rewrite fully-qualified paths
    code = re.sub(r"\bstd::fs::", f"{core_prefix}::vfs::", code)
    code = re.sub(r"\bstd::env::current_dir\(\)", f"{core_prefix}::vfs::current_dir()", code)
    code = re.sub(r"\benv::current_dir\(\)", f"{core_prefix}::vfs::current_dir()", code)
    for tname in TIME_REDIRECT:
        code = re.sub(rf"\bstd::time::{tname}\b", f"{core_prefix}::time::{tname}", code)
        code = re.sub(rf"(?<!::)\btime::{tname}\b", f"{core_prefix}::time::{tname}", code)

    # 3. Rewrite zero-arg `.exists()`, `.is_dir()`, `.is_file()`, `.canonicalize()`
    path_ext_used = False
    for method, repl in [
        ("exists", "vfs_exists"),
        ("is_dir", "vfs_is_dir"),
        ("is_file", "vfs_is_file"),
        ("canonicalize", "vfs_canonicalize"),
    ]:
        new_code, n = re.subn(rf"\.{method}\(\)", f".{repl}()", code)
        if n > 0:
            path_ext_used = True
            code = new_code

    if path_ext_used:
        trait_use = (
            f"#[allow(unused_imports)]\n"
            f"use {core_prefix}::vfs::{{VfsPathExt as _, VfsTypeExt as _}};\n"
        )
        if f"::vfs::{{VfsPathExt as _, VfsTypeExt as _}}" not in code:
            insert_pos = find_inner_attr_end(code)
            code = code[:insert_pos] + trait_use + code[insert_pos:]

    return code, (code != orig)


def main():
    modified_files = 0
    modified_crates = set()
    inline_test_pat = re.compile(r"\n#\[cfg\(test\)\]\s*(?:pub\s+)?mod\s+[a-zA-Z0-9_]+\s*\{")
    for crate in ACTIVE_CRATES:
        src_dir = f"crates/{crate}/src"
        if not os.path.isdir(src_dir):
            continue
        core_prefix = "crate" if crate == "horizon-core" else "horizon_core"
        for dp, _, fns in os.walk(src_dir):
            if "/bin/" in dp:
                continue
            for fn in sorted(fns):
                if not fn.endswith(".rs") or fn.endswith("_tests.rs") or fn == "tests.rs":
                    continue
                if crate == "horizon-core" and fn in ("vfs.rs", "time.rs"):
                    continue
                fpath = os.path.join(dp, fn)
                full = open(fpath).read()
                m = inline_test_pat.search(full)
                if m:
                    head, tail = full[: m.start()], full[m.start() :]
                else:
                    head, tail = full, ""
                new_head, changed = transform_code(head, core_prefix)
                if "ZipArchive" in new_head or "zip::" in new_head:
                    new_head = new_head.replace("entry.vfs_is_dir()", "entry.is_dir()")
                if new_head + tail != full:
                    open(fpath, "w").write(new_head + tail)
                    modified_files += 1
                    modified_crates.add(crate)

    for crate in sorted(modified_crates):
        ensure_horizon_core_dep(crate)

    print(f"Updated {modified_files} files across {len(modified_crates)} crates: {sorted(modified_crates)}")


if __name__ == "__main__":
    main()
