"""Which class a file on disk belongs to, from a list of roots.

Set HERALD_MEMORY_SOURCES to a JSON file: {"roots": [{"path": "...", "class": "public|team|main-only"}]}.
Not set: an installation without a private layer, everything is visible. Set, but the file is missing or unreadable, or a
path is under no listed root: hidden. Deleting the file must not open everything.

The match is on whole path components (/a does not cover /ab); nested roots: the longest wins; equal roots: the more
closed class wins. This guards against mistakes, not a process that sets the variable itself.

Known limit: roots must be written in canonical form; a spelling such as \\\\localhost\\C$\\... does not match and gets the
same fate as a typo in a root.
"""
import hashlib
import json
import os

ENV = "HERALD_MEMORY_SOURCES"
CLASSES = ("public", "team", "main-only")       # from open to closed
PUBLIC_LABELS = frozenset({"public", "internal"})
HIDDEN = "main-only"
UNREADABLE = "unreadable"
UNLABELLED = object()       # no label at all; a null or a non-text label is a label nobody can read, so it closes


def _key(path):
    real = os.path.realpath(path)       # real: a junction must not change a file's class
    if real.startswith("\\\\?\\UNC\\"):
        real = "\\\\" + real[8:]
    elif real.startswith("\\\\?\\"):
        real = real[4:]                 # realpath keeps this prefix; a root written with it must still match
    return os.path.normcase(real).rstrip(os.sep)


class Sources:
    def __init__(self, roots):
        self.roots = roots          # None: not configured; otherwise a list of (key, class)

    def class_of(self, path):
        """public, team or main-only; a path under no root (or a broken list) is main-only."""
        if self.roots is None:
            return "team"
        key, best = _key(path), None
        for root, name in self.roots:
            if key == root or key.startswith(root + os.sep):
                rank = (len(root), CLASSES.index(name))
                if best is None or rank > best[0]:
                    best = (rank, name)
        return best[1] if best else HIDDEN

    def named_main_only(self, path):
        """True only when a listed root says so. Unlike class_of, a broken list or an unlisted path says nothing here."""
        return self.class_of(path) == HIDDEN and self._listed(path)      # no list: team; broken list: not listed

    def _listed(self, path):
        key = _key(path)
        return any(key == root or key.startswith(root + os.sep) for root, _ in self.roots)

    def scope_of(self, path, sensitivity=UNLABELLED):
        """worker or main: the closed one of what the root says and what the record says."""
        if self.class_of(path) == HIDDEN:
            return "main"
        return "worker" if sensitivity is UNLABELLED or is_public(sensitivity) else "main"

    def fingerprint(self):
        """Changes whenever the list does, so an index built under an older list can be told apart."""
        return hashlib.sha256(repr(None if self.roots is None else sorted(self.roots)).encode("utf-8", "replace")).hexdigest()


def line_labels(line):
    """Every label one transcript line carries, at any depth and in any case. A line that cannot be parsed but mentions the
    word counts as one label nobody can read: a torn private line must close its file like a whole one."""
    try:
        stack = [json.loads(line.lstrip("\ufeff"))]
    except (ValueError, RecursionError):
        return [UNREADABLE] if "sensitivity" in line.casefold() else []
    found = []
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if isinstance(key, str) and key.casefold() == "sensitivity":
                    found.append(value)
                stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return found


def is_public(label):
    return isinstance(label, str) and label in PUBLIC_LABELS


def load():
    path = os.environ.get(ENV)
    if not path:
        return Sources(None)
    try:
        with open(path, encoding="utf-8-sig") as handle:        # Windows editors save a byte-order mark
            data = json.load(handle)
        roots = []
        for entry in data["roots"]:
            if entry["class"] not in CLASSES or not isinstance(entry["path"], str) or not os.path.isabs(entry["path"]) or (
                    os.name == "nt" and not os.path.splitdrive(entry["path"])[0]):
                raise ValueError        # a relative or empty root would mean whatever the current folder happens to be
            roots.append((_key(entry["path"]), entry["class"]))
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return Sources([])          # configured but broken: nothing is under a root, so everything is hidden
    return Sources(roots)
