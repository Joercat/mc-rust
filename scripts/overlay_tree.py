#!/usr/bin/env python3
"""A full copy of a resource tree that a re-encoder may rewrite in place.

The prepared install a pack is built from is hundreds of megabytes and is the
launcher's own copy: `compress_audio.py` and `compress_textures.py` must not
touch it. They rewrite a few thousand files and `pack_vfs.py` packs the
result, so the scratch tree starts as a link to every original file and each
rewrite replaces the directory entry instead of the bytes behind the link
([`break_link`]). Linking the untouched majority keeps a pack build from
copying the whole tree before it starts.

A file that is rewritten in place would edit the prepared install through the
link, which is why every re-encoder writes a temporary file and renames it.
"""
import os
import shutil


def link_tree(src, dst):
    """Make `dst` hold every file of `src`, linked when the filesystem allows.

    Existing files in `dst` are kept: a resumed run has already rewritten
    them. Returns how many entries were added.
    """
    added = 0
    for root, dirs, files in os.walk(src):
        dirs.sort()
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in sorted(files):
            if ".tmp" in name:
                continue
            target = os.path.join(target_dir, name)
            if os.path.exists(target):
                continue
            source = os.path.join(root, name)
            try:
                os.link(source, target)
            except OSError:
                # Two filesystems, or a filesystem without links: copy instead.
                shutil.copy2(source, target)
            added += 1
    return added


def replace_with(tmp, dst):
    """Move a freshly written file over `dst`, breaking any link it had."""
    os.replace(tmp, dst)
