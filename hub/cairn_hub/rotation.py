"""Which token a project's lead moves to after its token hits a usage limit. Pure: the caller reads and writes."""
from datetime import datetime


def limited_until(limits, label, now):
    """When the token's limit ends, or None when it has none at `now` (an entry that has ended counts as none)."""
    raw = (limits.get(label) or {}).get("until")
    until = datetime.fromisoformat(raw) if raw else None
    return until if until and until > now else None


def choose(pool, active, limits, now):
    """("switch", label): the first token after `active` in pool order, wrapping, `active` last, that is not limited.
    ("wait", label, until): every one is limited; the one whose limit ends first (ties: pool order).
    None: the pool is empty. Only pool tokens are chosen, so an `active` the node no longer runs is left out."""
    i = pool.index(active) if active in pool else len(pool)
    order = pool[i + 1:] + pool[:i + 1]
    if not order:
        return None
    ends = {label: limited_until(limits, label, now) for label in order}
    free = next((label for label in order if ends[label] is None), None)
    if free is not None:
        return ("switch", free)
    label = min(order, key=lambda l: (ends[l], pool.index(l)))
    return ("wait", label, ends[label])
