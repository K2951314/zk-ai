"""Write-through for web-console config edits: keep the YAML files authoritative.

The console (``/admin/models``, ``/admin/aliases``, ``/admin/providers/{id}/limits``)
mutates the live config *and* rewrites the source YAML here, so what you see in
``config/*.yaml`` is exactly what the gateway runs - deletions really delete, and
a reload/restart can never resurrect an entry the operator removed.

ruamel.yaml round-trip mode is used for adds/updates so comments, ordering and
formatting of untouched entries survive:

* an existing entry (same ``id``/``name``) is updated **in place** - its leading
  and inline comments stay; nested blocks (deployments / rule lists) are replaced;
* a missing entry is appended at the end of the list.

Deletes are done as a raw **text splice** instead: a removed entry's leading
comment header is reattached by ruamel to whatever shifts into its slot, and the
``# -----`` separators are byte-identical across entries, so removing the stale
header via the parsed tree risks a neighbour's comments. Splicing the exact line
range (header + body) is deterministic and leaves every other line untouched.

The previous file content is kept as ``<name>.bak`` (single rolling backup) and
every write is atomic (temp file in the same directory + replace). If the file
cannot be written (read-only disk, external editor lock...) the caller keeps the
DB override as fallback and surfaces the warning in the API response.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from app.core.logging import get_logger

logger = get_logger("core.config_writer")

_MAX_DOC_SIZE = 2_000_000  # config files are tiny; refuse anything pathological


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 100
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_document(path: Path) -> CommentedMap:
    """Parse a config file (or return an empty doc for a missing one)."""
    if not path.exists():
        return CommentedMap()
    size = path.stat().st_size
    if size > _MAX_DOC_SIZE:  # pragma: no cover - guard against pathological input
        raise OSError(f"refusing to edit suspiciously large config file: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return CommentedMap()
    doc = _yaml().load(text)
    if doc is None:
        return CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise ValueError(f"{path}: top level must be a mapping")
    return doc


def atomic_write(path: Path, document: CommentedMap) -> None:
    """Persist *document* to *path* atomically, keeping one rolling ``.bak``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        _yaml().dump(document, fh)
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists():
        bak = path.with_name(path.name + ".bak")
        try:
            if bak.exists():
                bak.unlink()
            os.replace(path, bak)
        except OSError as exc:  # rolling backup is best-effort; don't block the write
            logger.warning("could not rotate backup for %s: %s", path, exc)
    os.replace(tmp, path)
    logger.info("config file updated: %s", path)


def _list_of(doc: CommentedMap, key: str) -> CommentedSeq:
    value = doc.get(key)
    if isinstance(value, CommentedSeq):
        return value
    if value is None:
        value = CommentedSeq()
        doc[key] = value
    return value


def _find(entries: CommentedSeq, key: str, name: str) -> int:
    for index, entry in enumerate(entries):
        if isinstance(entry, CommentedMap) and str(entry.get(key)) == name:
            return index
    return -1


def _plain(data: Any) -> Any:
    """Deep-convert model dumps into plain ruamel-friendly containers."""
    if isinstance(data, dict):
        mapping = CommentedMap()
        for k, v in data.items():
            mapping[k] = _plain(v)
        return mapping
    if isinstance(data, (list, tuple)):
        sequence = CommentedSeq()
        for item in data:
            sequence.append(_plain(item))
        return sequence
    return data


def _sync_entry(entry: CommentedMap, payload: dict[str, Any]) -> None:
    """Update an existing mapping in place; keys absent from payload are dropped.

    Only leaf containers (capabilities dicts) keep their identity so their inline
    comments survive; everything structural is re-stamped.
    """
    for key in [k for k in entry if k not in payload]:
        del entry[key]
    for key, value in payload.items():
        if isinstance(value, dict) and isinstance(entry.get(key), CommentedMap):
            merged = entry[key]
            for stale in [k for k in merged if k not in value]:
                del merged[stale]
            for k, v in value.items():
                merged[k] = v
        elif entry.get(key) != value:
            entry[key] = _plain(value)


def upsert_list_entry(path: Path, section: str, key: str, name: str, payload: dict) -> None:
    """Create-or-update one dict entry inside ``doc[section]`` (list of maps)."""
    doc = load_document(path)
    entries = _list_of(doc, section)
    index = _find(entries, key, name)
    if index >= 0:
        _sync_entry(entries[index], payload)
    else:
        entries.append(_plain(payload))
    atomic_write(path, doc)


def delete_list_entry(path: Path, section: str, key: str, name: str) -> bool:
    """Remove one list entry (and its leading comment header) from a YAML file.

    Comment cleanup via ruamel's own reattachment is unreliable here: the header
    block above an entry can be reattached to whatever element shifts into its
    slot, and the ``# -----`` separator lines are byte-identical across entries,
    so neither identity nor text matching can single out the right lines without
    risking a neighbor's header. We therefore splice the *raw text* directly:

    * locate the entry's first line (``  - <key>: <name>``) inside ``section:``;
    * extend upwards over the contiguous blank/comment header block that belongs
      to this entry (the model description the operator cares about);
    * extend downwards to the entry's last content line, keeping any trailing
      blank + next-entry header comment untouched;
    * delete that exact line range and write the file back.

    Returns True when something was removed. Other entries - and all their
    comments and blank-line spacing - are byte-preserved.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    section_start = _section_line(lines, section)
    if section_start is None:
        return False
    base_indent, item_start = _find_entry_line(lines, section_start, key, name)
    if item_start is None:
        return False
    header_start = _walk_back_header(lines, item_start, floor=section_start + 1)
    block_end = _entry_block_end(lines, item_start, base_indent)
    delete_end = _trim_trailing_header(lines, block_end, item_start + 1)
    del lines[header_start:delete_end]
    _atomic_write_text(path, "".join(lines))
    return True


def _section_line(lines: list[str], section: str) -> int | None:
    for index, line in enumerate(lines):
        if line.rstrip() == f"{section}:":
            return index
    return None


def _find_entry_line(lines: list[str], section_start: int, key: str, name: str):
    """Return ``(base_indent, item_line_index)`` for ``- <key>: <name>`` in section."""
    import re

    pattern = re.compile(rf"^(\s*)-\s+{re.escape(key)}:\s*{re.escape(name)}\s*(#.*)?$")
    for index in range(section_start + 1, len(lines)):
        if _is_top_level_key(lines[index]):
            break  # a real top-level key ends the section (col-0 comments do not)
        match = pattern.match(lines[index])
        if match:
            return len(match.group(1)), index
    return (None, None)


def _is_top_level_key(line: str) -> bool:
    """A line that starts a top-level mapping key: column 0, not a comment/blank."""
    return bool(line[:1]) and line[0] not in {" ", "\t", "\n", "\r", "#"} and bool(line.strip())


def _walk_back_header(lines: list[str], item_index: int, floor: int) -> int:
    """First line of the blank+comment block immediately above ``item_index``."""
    start = item_index
    while start > floor:
        prev = lines[start - 1].strip()
        if prev == "" or prev.startswith("#"):
            start -= 1
        else:
            break
    return start


def _entry_block_end(lines: list[str], item_index: int, base_indent: int) -> int:
    """Line index just past the entry's last content line (before trailing comment)."""
    index = item_index + 1
    while index < len(lines):
        line = lines[index]
        if _is_top_level_key(line):  # a new top-level key ends everything
            break
        if _is_sibling_marker(line, base_indent):  # a sibling entry begins here
            break
        index += 1
    return index


def _is_sibling_marker(line: str, base_indent: int) -> bool:
    if len(line) <= base_indent:
        return False
    return line[:base_indent] == " " * base_indent and line[base_indent : base_indent + 2] == "- "


def _trim_trailing_header(lines: list[str], end_index: int, floor: int) -> int:
    """Pull ``end_index`` back over trailing blank + comment lines so the next
    entry's header (and blank padding) is preserved; never cut below ``floor``
    (which guarantees the deleted entry's own first line is removed)."""
    cut = end_index
    while cut > floor:
        stripped = lines[cut - 1].strip()
        if stripped == "" or stripped.startswith("#"):
            cut -= 1
        else:
            break
    return cut


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists():
        bak = path.with_name(path.name + ".bak")
        try:
            if bak.exists():
                bak.unlink()
            os.replace(path, bak)
        except OSError as exc:
            logger.warning("could not rotate backup for %s: %s", path, exc)
    os.replace(tmp, path)
    logger.info("config file updated: %s", path)


def sync_provider_rate_limits(path: Path, provider_id: str, rules: list[dict]) -> None:
    """Write ``options.rate_limits`` for one provider entry in providers.yaml.

    ``rules == []`` removes the key entirely (cleaner than an empty list).
    """
    doc = load_document(path)
    providers = _list_of(doc, "providers")
    index = _find(providers, "id", provider_id)
    if index < 0:
        raise KeyError(f"provider '{provider_id}' not found in {path.name}")
    entry = providers[index]
    options = entry.get("options")
    if not isinstance(options, CommentedMap):
        options = CommentedMap()
        entry["options"] = options
    if rules:
        options["rate_limits"] = _plain(rules)
    else:
        if "rate_limits" in options:
            del options["rate_limits"]
        if not options:
            del entry["options"]
    atomic_write(path, doc)


__all__ = [
    "atomic_write",
    "delete_list_entry",
    "load_document",
    "sync_provider_rate_limits",
    "upsert_list_entry",
]
