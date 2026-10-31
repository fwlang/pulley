"""Release versions and version constraints.

Versions are dotted numbers with an optional pre-release suffix: ``1``, ``1.2``, ``1.2.3``,
``26.7``, ``2.0.0-beta.1``. Missing components count as zero, so ``1.2`` equals ``1.2.0``.
Pre-releases sort before their release. Versions that are not numeric (``nightly``) only
match constraints that name them exactly.

Constraints:

====================  ======================================
``*``, ``latest``     any release
``1.2.3``, ``=1.2.3`` exactly that version
``^1.2.3``            ``>=1.2.3, <2.0.0`` (``^0.2`` is ``>=0.2, <0.3``)
``~1.2.3``            ``>=1.2.3, <1.3.0`` (``~1`` is ``>=1, <2``)
``1.2.*``             ``>=1.2, <1.3``
``>=1.0, <2``         every comparison must hold (``>``, ``>=``, ``<``, ``<=``, ``!=``)
====================  ======================================

Pre-releases match only when a comparison in the constraint itself names a pre-release
with the same numbers, as in npm and Cargo.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+]?([0-9A-Za-z][0-9A-Za-z.-]*))?$")


@functools.total_ordering
@dataclass(frozen=True)
class Version:
    text: str
    numbers: tuple[int, ...] = ()
    pre: tuple = ()
    numeric: bool = True

    @staticmethod
    def parse(text: str) -> "Version":
        text = str(text).strip()
        match = _VERSION.match(text)
        if not match:
            return Version(text, (), (), False)
        numbers = tuple(int(n) for n in match.group(1).split("."))
        while len(numbers) > 1 and numbers[-1] == 0:
            numbers = numbers[:-1]
        pre: tuple = ()
        if match.group(2):
            pre = tuple(int(p) if p.isdigit() else p for p in re.split(r"[.-]", match.group(2)))
        return Version(text, numbers, pre, True)

    @property
    def prerelease(self) -> bool:
        return bool(self.pre)

    def _key(self):
        # Releases sort after their pre-releases; pre-release parts compare numbers before text.
        pre_key = (1,) if not self.pre else (0,) + tuple(
            (0, p, "") if isinstance(p, int) else (1, 0, p) for p in self.pre
        )
        return (self.numeric, self.numbers, pre_key, self.text if not self.numeric else "")

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def component(self, index: int) -> int:
        return self.numbers[index] if index < len(self.numbers) else 0

    def __str__(self) -> str:
        return self.text


class ConstraintError(ValueError):
    pass


@dataclass(frozen=True)
class Comparison:
    op: str
    version: Version

    def holds(self, v: Version) -> bool:
        if not v.numeric or not self.version.numeric:
            return self.op in ("=", "==") and v.text == self.version.text
        return {
            "=": v == self.version,
            "==": v == self.version,
            "!=": v != self.version,
            ">": v > self.version,
            ">=": v >= self.version,
            "<": v < self.version,
            "<=": v <= self.version,
        }[self.op]


class Constraint:
    """A set of comparisons that a version must all satisfy."""

    def __init__(self, text: str = "*"):
        self.text = (text or "*").strip()
        self.comparisons: list[Comparison] = []
        if self.text in ("*", "latest", ""):
            return
        normalized = re.sub(r"(>=|<=|==|!=|>|<|=)\s+", r"\1", self.text)
        for part in re.split(r"[,\s]+(?=[<>=!^~\d*v])", normalized):
            part = part.strip().rstrip(",").strip()
            if part:
                self.comparisons.extend(self._parse(part))

    def _parse(self, part: str) -> list[Comparison]:
        if part in ("*", "latest"):
            return []
        if part[0] in "^~":
            base = Version.parse(part[1:])
            if not base.numeric:
                raise ConstraintError(f"invalid version constraint '{self.text}'")
            given = len(part[1:].lstrip("v").split("-")[0].split("."))
            if part[0] == "^":
                # The first non-zero component (of those written) may not change.
                index = 0
                while index < given - 1 and base.component(index) == 0:
                    index += 1
            else:
                index = 0 if given == 1 else 1
            upper = list(base.numbers[: index + 1]) + [0] * (index + 1 - len(base.numbers))
            upper[index] += 1
            return [Comparison(">=", base), Comparison("<", Version.parse(".".join(map(str, upper))))]
        if part.endswith(".*") or part.endswith(".x"):
            prefix = part[:-2]
            base = Version.parse(prefix)
            if not base.numeric:
                raise ConstraintError(f"invalid version constraint '{self.text}'")
            count = len(prefix.split("."))
            upper = [base.component(i) for i in range(count)]
            upper[-1] += 1
            return [Comparison(">=", base), Comparison("<", Version.parse(".".join(map(str, upper))))]
        match = re.match(r"^(>=|<=|==|!=|=|>|<)?\s*(.+)$", part)
        if not match:
            raise ConstraintError(f"invalid version constraint '{self.text}'")
        op, value = match.group(1) or "=", match.group(2).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
            raise ConstraintError(f"invalid version constraint '{self.text}'")
        version = Version.parse(value)
        if not version.numeric and op not in ("=", "=="):
            raise ConstraintError(f"invalid version constraint '{self.text}'")
        return [Comparison(op, version)]

    @property
    def any(self) -> bool:
        return not self.comparisons

    def allows(self, version: Version | str) -> bool:
        v = version if isinstance(version, Version) else Version.parse(version)
        if v.prerelease and not any(
            c.version.prerelease and c.version.numbers == v.numbers for c in self.comparisons
        ):
            return False
        return all(c.holds(v) for c in self.comparisons)

    def exact(self) -> str | None:
        """The single version this constraint names, if it is an exact requirement."""
        if len(self.comparisons) == 1 and self.comparisons[0].op in ("=", "=="):
            return self.comparisons[0].version.text
        return None

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"Constraint({self.text!r})"


def best(versions, constraints: list[Constraint]) -> str | None:
    """The highest version allowed by every constraint."""
    allowed = [Version.parse(v) for v in versions]
    allowed = [v for v in allowed if all(c.allows(v) for c in constraints)]
    return str(max(allowed)) if allowed else None


def caret(version: str) -> str:
    """The default constraint written for a newly added dependency."""
    v = Version.parse(version)
    return f"^{version}" if v.numeric and not v.prerelease else f"={version}"
