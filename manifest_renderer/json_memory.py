"""Bounded, in-memory sharing of immutable renderer JSON metadata.

Files and canonical JSON remain unchanged. Only recognized metadata fields
are shared; dialogue text, IDs, review state and receipt records stay distinct.
Deep copies deliberately produce ordinary mutable dicts/lists for editors.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import threading
from collections import OrderedDict
from pathlib import Path


def _immutable(*args, **kwargs):
    raise TypeError("shared JSON metadata is immutable; deepcopy before editing")


class FrozenDict(dict):
    __slots__ = ()
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = __init__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        result = {}
        memo[id(self)] = result
        for key, value in self.items():
            result[copy.deepcopy(key, memo)] = copy.deepcopy(value, memo)
        return result


class FrozenList(list):
    __slots__ = ()
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = __init__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        result = []
        memo[id(self)] = result
        result.extend(copy.deepcopy(value, memo) for value in self)
        return result


def _freeze(value):
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    if isinstance(value, dict):
        result = dict.__new__(FrozenDict)
        dict.update(result, ((key, _freeze(item)) for key, item in value.items()))
        return result
    if isinstance(value, list):
        result = list.__new__(FrozenList)
        list.extend(result, (_freeze(item) for item in value))
        return result
    return value


def recursive_size(value):
    """Count live container/key/value bytes once per object identity."""
    seen = set()
    def size(item):
        identity = id(item)
        if identity in seen:
            return 0
        seen.add(identity)
        total = sys.getsizeof(item)
        if isinstance(item, dict):
            total += sum(size(key) + size(child) for key, child in item.items())
        elif isinstance(item, (list, tuple)):
            total += sum(size(child) for child in item)
        return total
    return size(value)


_COMMON_FIELDS = ("models", "runtime", "qa", "requirements", "project_policy", "render_execution")


class MetadataPool:
    """Own a byte-bounded metadata pool, shared across related JSON reads.

    Accounting conservatively includes each entry's complete recursive graph,
    its fingerprint key and OrderedDict overhead. Oversized values are never
    retained by the pool. There is no cache of canonical strings or snapshots.
    """
    def __init__(self, max_bytes=8 * 1024 * 1024):
        self.max_bytes = max(0, int(max_bytes))
        self._entries = OrderedDict()
        self._entry_bytes = 0
        self._lock = threading.RLock()

    @property
    def bytes(self):
        return self._entry_bytes + sys.getsizeof(self._entries)

    def intern(self, field, value):
        if not isinstance(value, (dict, list)) or not self.max_bytes:
            return value
        # Reject large metadata before allocating its canonical representation.
        if recursive_size(value) > self.max_bytes:
            return value
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        key = (field, hashlib.sha256(encoded).digest())
        del encoded
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached[0] == value:
                self._entries.move_to_end(key)
                return cached[0]
            frozen = _freeze(value)
            entry = (frozen, recursive_size((key, frozen)) + sys.getsizeof((frozen, 0)))
            # Count OrderedDict growth separately; graph sums are conservative
            # when nested metadata or key strings are already shared.
            while self._entries and self.bytes + entry[1] + 128 > self.max_bytes:
                _, removed = self._entries.popitem(last=False)
                self._entry_bytes -= removed[1]
            if self.bytes + entry[1] + 128 > self.max_bytes:
                return value
            replaced = self._entries.pop(key, None)
            if replaced is not None:
                self._entry_bytes -= replaced[1]
            self._entries[key] = entry
            self._entry_bytes += entry[1]
            while self._entries and self.bytes > self.max_bytes:
                _, removed = self._entries.popitem(last=False)
                self._entry_bytes -= removed[1]
            return frozen

    def object_hook(self, obj):
        # Parent signatures keep incidental fields named "qa"/"models" in
        # mutable review settings, text metadata and source evidence untouched.
        if "schema_version" in obj and "manifest_id" in obj and "models" in obj and "runtime" in obj:
            fields = _COMMON_FIELDS
        elif "fingerprint" in obj and "dialog_id" in obj:
            fields = _COMMON_FIELDS if "models" in obj and "runtime" in obj else ("render_execution",)
        elif "binding_version" in obj and "attempt_fingerprint" in obj:
            fields = ("models", "runtime")
        elif "plan_version" in obj and "manifest_sha256" in obj:
            fields = ("project_policy",)
        else:
            fields = ()
        for field in fields:
            if field in obj:
                obj[field] = self.intern(field, obj[field])
        return obj


def read_compact(path, *, pool=None):
    """Parse unchanged JSON, sharing only recognized renderer metadata."""
    pool = MetadataPool() if pool is None else pool
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream, object_hook=pool.object_hook)
