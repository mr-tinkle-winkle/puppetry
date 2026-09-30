"""
Turns a stream of input events (as the Input Visualizer sees them) into the
macro code that would reproduce them.

The visualizer only records tuples on the hot path (no formatting, so the
CPS tester stays honest); `render()` runs on a timer and rebuilds the text
from the most recent events.

Events are (t, kind, a, b):
    kd / ku  -- a = "KEY_A" / "BTN_LEFT"
    wheel    -- a = notches (+ = up)
    move     -- a = dx, b = dy (pixels)

With `combine` on, a key pressed and released on its own becomes
`tap(KEY_A)`, several keys pressed then released in reverse order become
`combo(...)`, a run of wheel notches becomes one `wheel(n)`; anything that
doesn't fit those shapes (a key held while the mouse moves, keys released
out of order, a key still down) stays as exact `kd(...)` / `ku(...)`.
Bursts of mouse movement always become one `move_mouse(dx, dy, ...)`.
"""
from __future__ import annotations

MOVE_GAP = 0.15        # idle seconds that end a mouse-movement burst
WHEEL_GAP = 0.25       # ... a run of wheel notches
MIN_WAIT = 0.005       # smaller gaps aren't worth a wait() line
DEFAULT_HOLD = 0.1     # tap()/combo() default -- omitted when it matches
COMBO_SPREAD = 0.3     # max seconds from first to last press of a combo
MAX_EVENTS = 800


def fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s or "0"


class InputLog:
    def __init__(self):
        self.events: list = []
        self.revision = 0

    def clear(self) -> None:
        self.events.clear()
        self.revision += 1

    def add(self, t: float, kind: str, a=None, b=None) -> None:
        ev = self.events
        if kind == "move" and ev and ev[-1][1] == "move" and t - ev[-1][0] <= MOVE_GAP:
            # a 1000Hz mouse would flood the log: a burst is stored as its
            # first event plus ONE running-total "tail" (5th field set), which
            # render() treats as a continuation of the burst whatever its age
            last = ev[-1]
            if len(last) == 5:
                ev[-1] = (t, "move", last[2] + a, last[3] + b, True)
            else:
                ev.append((t, "move", a, b, True))
            self.revision += 1
            return
        ev.append((t, kind, a, b))
        if len(ev) > MAX_EVENTS * 1.25:
            del ev[:len(ev) - MAX_EVENTS]
        self.revision += 1

    # -- rendering ---------------------------------------------------------
    def render(self, combine: bool = True, waits: bool = True) -> str:
        items = self._items(combine)
        items.sort(key=lambda it: (it[0], it[1]))
        out: list = []
        prev_end = None
        for start, end, lines in items:
            if waits and prev_end is not None and start - prev_end >= MIN_WAIT:
                out.append(f"wait({fmt(start - prev_end)})")
            out.extend(lines)
            prev_end = max(end, prev_end) if prev_end is not None else end
        return "\n".join(out)

    def _items(self, combine: bool) -> list:
        """[(start, end, [lines])] in no particular order."""
        events = self.events
        # forget releases of keys we never saw go down (recording began mid-press)
        down: set = set()
        clean: list = []
        for ev in events:
            if ev[1] == "kd":
                down.add(ev[2])
            elif ev[1] == "ku":
                if ev[2] not in down:
                    continue
                down.discard(ev[2])
            clean.append(ev)

        items: list = []
        moves = [e for e in clean if e[1] == "move"]
        wheels = [e for e in clean if e[1] == "wheel"]
        keys = [e for e in clean if e[1] in ("kd", "ku")]

        # mouse movement bursts
        burst: list = []

        def flush_move():
            if not burst:
                return
            dx = sum(e[2] for e in burst)
            dy = sum(e[3] for e in burst)
            t0, t1 = burst[0][0], burst[-1][0]
            if dx or dy:
                dur = max(t1 - t0, 0.01)
                items.append((t0, t1, [f"move_mouse({dx}, {dy}, time_={fmt(dur)}, easing=\"linear\")"]))
            burst.clear()

        for e in moves:
            if burst and len(e) < 5 and e[0] - burst[-1][0] > MOVE_GAP:
                flush_move()
            burst.append(e)
        flush_move()

        # wheel
        run: list = []

        def flush_wheel():
            if not run:
                return
            n = sum(e[2] for e in run)
            if n:
                items.append((run[0][0], run[-1][0], [f"wheel({n})"]))
            run.clear()

        for e in wheels:
            if combine:
                if run and (e[0] - run[-1][0] > WHEEL_GAP or (e[2] > 0) != (run[-1][2] > 0)):
                    flush_wheel()
                run.append(e)
            else:
                items.append((e[0], e[0], [f"wheel({e[2]})"]))
        flush_wheel()

        # keys
        span_events = [e[0] for e in moves + wheels]

        def raw(group):
            for t, kind, name, _ in group:
                items.append((t, t, [f"{kind}({name})"]))

        if not combine:
            raw(keys)
            return items

        group: list = []
        held: list = []          # press order of the keys currently down

        def close_group():
            nonlocal group, held
            if not group:
                return
            presses = [e for e in group if e[1] == "kd"]
            releases = [e for e in group if e[1] == "ku"]
            t0, t1 = group[0][0], group[-1][0]
            busy = any(t0 < t < t1 for t in span_events)
            names = [e[2] for e in presses]
            if held or busy or len(releases) != len(presses):
                raw(group)
            elif len(presses) == 1:
                hold = releases[0][0] - presses[0][0]
                arg = "" if abs(hold - DEFAULT_HOLD) < 0.005 else f", time_={fmt(hold)}"
                items.append((t0, t1, [f"tap({names[0]}{arg})"]))
            elif ([e[2] for e in releases] == names[::-1]
                  and presses[-1][0] - presses[0][0] <= COMBO_SPREAD):
                hold = releases[0][0] - presses[-1][0]
                arg = "" if abs(hold - DEFAULT_HOLD) < 0.005 else f", time_={fmt(hold)}"
                items.append((t0, t1, [f"combo({', '.join(names)}{arg})"]))
            else:
                raw(group)
            group = []

        for e in keys:
            if e[1] == "kd":
                if e[2] in held:
                    continue
                held.append(e[2])
            else:
                if e[2] in held:
                    held.remove(e[2])
            group.append(e)
            if not held:
                close_group()
        # still-down keys: exact presses (held stays non-empty, so raw)
        close_group()
        return items
