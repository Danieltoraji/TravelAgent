"""Route ordering: transport-edge access, nearest-neighbour and 2-opt.

Transport distance is converted to estimated public-transport time and is
deducted from ``daily_travel_time`` together with visits and opening waits.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algorithoms._common import Location, Spot, _spot_key
from transport.providers import TravelTimeMatrix


def _spot_edge(first: Spot, second: Spot, matrix: TravelTimeMatrix):
    return matrix.get(_spot_key(first), _spot_key(second))


def _spot_distance(first: Spot, second: Spot, matrix: TravelTimeMatrix) -> float:
    return _spot_edge(first, second, matrix).distance_km


def _spot_transport_minutes(first: Spot, second: Spot, matrix: TravelTimeMatrix) -> int:
    return _spot_edge(first, second, matrix).transport_minutes


def _route_distance(
    route: Sequence[Spot], start_location: Optional[Location], matrix: TravelTimeMatrix
) -> float:
    if not route:
        return 0.0
    total = 0.0
    for previous, current in zip(route, route[1:]):
        total += _spot_distance(previous, current, matrix)
    return total


def _nearest_neighbor(
    spots: Sequence[Spot], start: Spot, matrix: TravelTimeMatrix
) -> List[Spot]:
    remaining = list(spots)
    ordered: List[Spot] = []
    current = start
    while remaining:
        next_spot = min(
            remaining,
            key=lambda spot: (
                _spot_distance(current, spot, matrix),
                _spot_key(spot),
            ),
        )
        remaining.remove(next_spot)
        ordered.append(next_spot)
        current = next_spot
    return ordered


def _two_opt(
    route: Sequence[Spot], start_location: Optional[Location], matrix: TravelTimeMatrix
) -> List[Spot]:
    """Improve an open route; the final attraction need not return to the start."""
    best = list(route)
    best_distance = _route_distance(best, start_location, matrix)
    improved = True
    while improved:
        improved = False
        for left in range(len(best) - 1):
            for right in range(left + 1, len(best)):
                candidate = best[:left] + list(reversed(best[left : right + 1])) + best[right + 1 :]
                distance = _route_distance(candidate, start_location, matrix)
                if distance + 1e-9 < best_distance:
                    best = candidate
                    best_distance = distance
                    improved = True
    return best


def _order_spots(
    spots: Sequence[Spot], start_location: Optional[Location], matrix: TravelTimeMatrix
) -> List[Spot]:
    if len(spots) <= 1:
        return list(spots)
    # Without a hotel/start point, try every attraction as the first stop and
    # retain the shortest open route.
    candidates = []
    for first in spots:
        rest = [spot for spot in spots if _spot_key(spot) != _spot_key(first)]
        route = [first, *_nearest_neighbor(rest, first, matrix)]
        route = _two_opt(route, None, matrix)
        candidates.append(route)
    return min(candidates, key=lambda route: _route_distance(route, None, matrix))
