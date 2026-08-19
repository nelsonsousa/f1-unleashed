"""Auto-select for the dashboard view (card wfMzaSwh) — server-computed recommendation of the two
drivers most worth watching, re-picked continuously as the session evolves.

Emits: dashAutoSelect  = [num1, num2]  (<=2; [] when none)  — deduped on the pair.

Rules (SME):
  practice   : drivers closest to finishing their PUSH lap (highest track %); none if nobody pushing.
  quali Q1/Q2: DEFAULT (>= AT_RISK_WINDOW_S left in the part, or remaining time unknown) — every
               push-lap driver, closest to finishing first (by track %). Inside AT_RISK_WINDOW_S,
               narrows to at-risk-on-push (by track %) then the rest-on-push (by track %). At-risk =
               positional (Q1 = P13-P20, Q2 = P7-P15 — drop zone + 4 above the cutoff). (card 0pEopiCB)
  quali Q3   : DEFAULT (>= AT_RISK_WINDOW_S left, or unknown) — every push-lap driver, closest to
               finishing first (by track %), same as Q1/Q2's default. Inside AT_RISK_WINDOW_S,
               narrows to {predicted top-5 ∪ current top-5} by track %, then all others by track %.
               (card 0pEopiCB)
  race       : frontmost STRONG overtaking pair (trailing Int < 0.5s); else frontmost SOFT (< 1.0s);
               else P1 & P2. Pair emitted [ahead, behind].

Time-remaining source (card 0pEopiCB): `ExtrapolatedClock.Remaining` — verified against captured
qualifying sessions to reset to the full part duration (e.g. "00:18:00" for Q1) at the start of
each qualifying part, so it's already scoped per-part; no extra bookkeeping needed here.

Tie-break (open point in card 0pEopiCB, resolved as a judgment call): when two push-lap drivers
are tied on track %, the one with the better (numerically lower) `predictedPos` sorts first;
drivers with no prediction yet sort after those that have one. Chosen because `predictedPos` is
the only other per-driver "how close to a result" signal already flowing into this processor, so
it needs no new topic — a driver-number string tiebreak was rejected as arbitrary and would have
reintroduced exactly the kind of "some other field decides who wins the tie" opacity WB-1's
Kuryh6y4 fix (test_dashautoselect_pick_q3_nondeterminism.py) already removed for Q3.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.processing.processors.base import Processor

HOLD_MS = 5000   # buffer a changed pick this long (session time) before emitting it
AT_RISK_WINDOW_S = 300   # switch to the at-risk/predicted-top-5 narrowing inside this much time left


def _remaining_s(s: Any) -> Optional[float]:
    """ExtrapolatedClock.Remaining ("HH:MM:SS" or "MM:SS") → seconds left in the
    current qualifying part. None for anything unparseable."""
    if not isinstance(s, str) or ":" not in s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
    except (ValueError, IndexError):
        return None
    return None


def _int_ms(s: Any) -> Optional[int]:
    """Interval string ("+0.834") → ms; None for lapped/non-numeric."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or "L" in s.upper():
        return None
    body = s.lstrip("+-").strip()
    if not body:
        return None
    try:
        if ":" in body:
            mm, rest = body.split(":", 1)
            return int(round((int(mm) * 60 + float(rest)) * 1000))
        return int(round(float(body) * 1000))
    except (ValueError, IndexError):
        return None


class DashboardAutoSelectProcessor(Processor):
    def __init__(self, bus, session_type: str):
        super().__init__(bus, session_type)
        self._st = session_type
        self._dp: dict[str, float] = {}          # track % (0-100), continuous
        self._push: dict[str, bool] = {}         # current lap classified PUSH
        self._pos: dict[str, int] = {}           # standings position
        self._pred: dict[str, Optional[int]] = {}  # predicted position (quali)
        self._int_ms: dict[str, Optional[int]] = {}  # interval to car ahead (race)
        self._part: Optional[int] = None
        self._remaining_s: Optional[float] = None   # seconds left in the current quali part (ExtrapolatedClock)
        self._started = False                    # session has gone green/lights-out (SessionStatus=Started)
        self._finished: dict[str, bool] = {}     # num -> has taken the chequered flag
        self._last: Optional[list] = None        # last emitted (currently shown) pair
        self._change_since: Optional[float] = None  # session ms a switch became pending (hold before it)

    def subscribe(self) -> None:
        self._bus.on("position", self._on_position)
        self._bus.on("standings", self._on_standings)
        self._bus.on("qualifyingPart", self._on_part)
        self._bus.on("sessionInfo", self._on_session_info)
        self._bus.on("ExtrapolatedClock", self._on_clock)
        self._bus.on("*", self._wild)

    # ── inputs ──────────────────────────────────────────────────────────────
    def _on_session_info(self, data: Any, clock_time: datetime) -> None:
        # Don't auto-select before the session goes green/lights-out. (SME 2026-07-15)
        if isinstance(data, dict) and data.get("sessionStatus") == "Started" and not self._started:
            self._started = True
            self._recompute(clock_time)

    def _wild(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverLapClassification:"):
            if isinstance(data, dict) and data.get("type") is not None:
                self._push[topic.split(":", 1)[1]] = data.get("type") == "PUSH"
                self._recompute(clock_time)
        elif topic.startswith("lapPrediction:"):
            if isinstance(data, dict):
                self._pred[topic.split(":", 1)[1]] = data.get("predictedPos")
                self._recompute(clock_time)
        elif topic.startswith("driverInt:"):
            if isinstance(data, dict):
                self._int_ms[topic.split(":", 1)[1]] = _int_ms(data.get("interval"))
                self._recompute(clock_time)
        elif topic.startswith("driverStatus:"):
            if isinstance(data, str):   # a checkered driver is excluded from new picks
                self._finished[topic.split(":", 1)[1]] = data == "CHECKERED"
                self._recompute(clock_time)

    def _on_position(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        changed = False
        for num, coords in data.items():
            if isinstance(coords, list) and len(coords) >= 3 and isinstance(coords[2], (int, float)):
                self._dp[str(num)] = float(coords[2])
                changed = True
        if changed:
            self._recompute(clock_time)

    def _on_standings(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for d in data.get("drivers", []):
            if isinstance(d, dict) and "num" in d and "position" in d:
                self._pos[str(d["num"])] = d["position"]
        self._recompute(clock_time)

    def _on_part(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, int):
            self._part = data
            self._recompute(clock_time)

    def _on_clock(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict) and "Remaining" in data:
            self._remaining_s = _remaining_s(data.get("Remaining"))
            self._recompute(clock_time)

    # ── pickers ─────────────────────────────────────────────────────────────
    def _at_risk_window(self) -> bool:
        """True inside AT_RISK_WINDOW_S of the current quali part. Unknown remaining
        time (no ExtrapolatedClock yet) is treated as "not yet at risk" — the same
        too-early-focus bias the bug report (card B02ySqwU/0pEopiCB) complains about,
        so the safe default is to stay on the plain push-lap-focus ordering."""
        return self._remaining_s is not None and self._remaining_s < AT_RISK_WINDOW_S

    def _push_by_dp(self) -> list:
        """Push-lap candidates, closest to finishing first (highest track %). Ties on
        track % are broken by the better (lower) predicted position — see the module
        docstring's tie-break note; drivers with no prediction sort after those that
        have one."""
        candidates = [n for n in self._dp if self._push.get(n) and not self._finished.get(n)]

        def key(n: str):
            pred = self._pred.get(n)
            pred_key = pred if isinstance(pred, int) else float("inf")
            return (-self._dp[n], pred_key)

        return sorted(candidates, key=key)

    def _pick_practice(self) -> list:
        return self._push_by_dp()[:2]

    def _pick_q12(self, part: int) -> list:
        push = self._push_by_dp()
        if not self._at_risk_window():
            # Default (card 0pEopiCB): every push-lap driver, closest to finishing first.
            return push[:2]
        zone = set(range(13, 21) if part == 1 else range(7, 16))   # Q1 P13-20 / Q2 P7-15
        at_risk = [n for n in push if self._pos.get(n) in zone]
        rest = [n for n in push if self._pos.get(n) not in zone]
        return (at_risk + rest)[:2]

    def _pick_q3(self) -> list:
        if not self._at_risk_window():
            # Default (card 0pEopiCB): "all drivers at start" — scoped to push-lap
            # candidates, same pool as Q1/Q2's default and practice's rule (a driver
            # in the pits or on an out lap isn't an actionable "focus" pick for a
            # flying lap). Documented judgment call — the card says "all drivers"
            # without specifying whether that includes non-push-lap drivers.
            return self._push_by_dp()[:2]
        fin = self._finished
        top5 = {n for n, p in self._pos.items() if isinstance(p, int) and p <= 5 and not fin.get(n)}
        top5 |= {n for n, p in self._pred.items() if isinstance(p, int) and p <= 5 and not fin.get(n)}
        by_dp = lambda ns: sorted(ns, key=lambda n: self._dp.get(n, -1.0), reverse=True)
        prime = by_dp([n for n in sorted(top5) if n in self._dp])
        rest = by_dp([n for n in self._dp if n not in top5 and not fin.get(n)])
        return (prime + rest)[:2]

    def _pick_race(self) -> list:
        # Finished (chequered) drivers are not candidates.
        by_pos = {p: n for n, p in self._pos.items()
                  if isinstance(p, int) and not self._finished.get(n)}
        if not by_pos:
            return []
        maxp = max(by_pos)

        def pairs(threshold_ms: int) -> list:
            out = []
            for p in range(1, maxp):
                front, behind = by_pos.get(p), by_pos.get(p + 1)
                if front is None or behind is None:
                    continue
                iv = self._int_ms.get(behind)          # trailing car's interval to the car ahead
                if iv is not None and iv < threshold_ms:
                    out.append((p, front, behind))
            return out

        chosen = pairs(500) or pairs(1000)             # strong (<0.5s) else soft (<1.0s)
        if chosen:
            chosen.sort(key=lambda x: x[0])            # frontmost = closest to the fight for P1
            return [chosen[0][1], chosen[0][2]]
        return [by_pos[p] for p in sorted(by_pos)[:2]]   # fallback: two frontmost (alive) — P1 & P2

    # ── output ──────────────────────────────────────────────────────────────
    def _recompute(self, clock_time: datetime) -> None:
        if not self._started:
            return   # don't auto-select before the session starts (SME)
        st = self._st
        if st == "practice":
            pair = self._pick_practice()
        elif st == "qualifying":
            pair = self._pick_q3() if (self._part or 1) >= 3 else self._pick_q12(self._part or 1)
        elif st in ("race", "sprint"):
            pair = self._pick_race()
        else:
            pair = []
        pair = pair[:2]
        # Minimum-hold (SME): when the pick changes, keep showing the CURRENT pair for HOLD_MS of
        # session time — so the user can read the just-completed lap time + classification — then
        # switch to the LATEST pick. A reversal within the window cancels the pending switch. The
        # first selection (nothing shown yet) applies immediately.
        now_ms = clock_time.timestamp() * 1000.0
        if pair == self._last:
            self._change_since = None
            return
        if not self._last:
            self._last = list(pair)
            self._change_since = None
            self._bus.emit("dashAutoSelect", list(pair), clock_time)
            return
        if self._change_since is None:
            self._change_since = now_ms
        elif now_ms - self._change_since >= HOLD_MS:
            self._last = list(pair)
            self._change_since = None
            self._bus.emit("dashAutoSelect", list(pair), clock_time)
