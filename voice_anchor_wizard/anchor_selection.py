"""Pure, unchanged Wizard duration-strata and bounded composite selection."""
from __future__ import annotations
import itertools

JOIN_MS = 300
TARGET_MIN_MS = 4000
TARGET_MAX_MS = 15000
MAX_COMPOSITE_COMPONENTS = 8

def pick_candidates(candidates: list[dict], limit: int = 18) -> tuple[list[dict], dict]:
    """Pick duration strata, deduplicate, then deterministically fill to the limit."""
    all_items = sorted(candidates, key=lambda c: (-c["duration_ms"], int(c["speech_id"])))
    preferred = [c for c in all_items if c["duration_ms"] >= 5000]
    pool = preferred or all_items
    labels: dict[str, set[str]] = {}
    raw_slots: list[tuple[str, dict]] = []
    if pool:
        longest = pool[:6]
        shortest = sorted(pool, key=lambda c: (c["duration_ms"], int(c["speech_id"])))[:6]
        ordered = sorted(pool, key=lambda c: (c["duration_ms"], int(c["speech_id"])))
        start = max(0, (len(ordered) - 6) // 2)
        middle = ordered[start:start + 6]
        for label, items in (("long", longest), ("middle", middle), ("short", shortest)):
            for item in items:
                raw_slots.append((label, item))
                labels.setdefault(item["speech_id"], set()).add(label)
    chosen: list[dict] = []
    seen: set[str] = set()
    for _, item in raw_slots:
        if item["speech_id"] not in seen:
            chosen.append(item)
            seen.add(item["speech_id"])
    for item in all_items:
        if len(chosen) >= min(limit, len(all_items)):
            break
        if item["speech_id"] not in seen:
            chosen.append(item)
            seen.add(item["speech_id"])
            labels.setdefault(item["speech_id"], set()).add("fill")
    result = [dict(item, buckets=sorted(labels.get(item["speech_id"], {"fill"})),
                   below_five_seconds=item["duration_ms"] < 5000) for item in chosen[:limit]]
    meta = {
        "eligible_count": len(all_items),
        "at_least_five_seconds_count": len(preferred),
        "raw_bucket_slots": len(raw_slots),
        "duplicate_bucket_slots_removed": len(raw_slots) - len({item["speech_id"] for _, item in raw_slots}),
        "shown_count": len(result),
        "fallback_used": len(preferred) == 0 or any(x["below_five_seconds"] for x in result),
    }
    return result, meta


def anchor_duration(components: list[dict]) -> int:
    return sum(c["duration_ms"] for c in components) + JOIN_MS * (len(components) - 1)


def automatic_composite(candidates: list[dict]) -> dict | None:
    best = None
    shortest_durations = sorted(c["duration_ms"] for c in candidates)
    for size in range(2, min(MAX_COMPOSITE_COMPONENTS, len(candidates)) + 1):
        # Even the shortest possible group is too long: larger groups cannot fit.
        if sum(shortest_durations[:size]) + JOIN_MS * (size - 1) > TARGET_MAX_MS:
            break
        for combo in itertools.combinations(candidates, size):
            duration = anchor_duration(list(combo))
            if TARGET_MIN_MS <= duration <= TARGET_MAX_MS:
                score = (abs(duration - 8000), -len({b for c in combo for b in c.get("buckets", [])}),
                         tuple(int(c["speech_id"]) for c in combo))
                if best is None or score < best[0]:
                    best = (score, combo, duration)
    if best is None:
        return None
    return {"kind": "composite", "component_speech_ids": [c["speech_id"] for c in best[1]],
            "duration_ms": best[2], "join_silence_ms": JOIN_MS, "source": "automatic"}
