import math
from dataclasses import dataclass
from enum import Enum


# Where each scenario's solve stands. Only ACTIVE scenarios are solved. The others are still run, at a guess with enough assets, so the
# final projection has every scenario, but their results are not used
class ScenarioStatus(Enum):
    ACTIVE = "Active"              # Still needs solving
    CONVERGED = "Converged"        # Solved to tolerance - see Tolerances for the two ways a scenario can be solved
    NON_BINDING = "Non-binding"    # Can't set the BEL, because another scenario needs more starting assets


# The two tolerances for solving a scenario. A scenario is solved when EITHER is met. Both are fractions, e.g. 0.000001 is
# 0.0001%
@dataclass(frozen=True)
class Tolerances:
    # A run is solved when its ending assets are within this fraction of its starting assets
    ending_assets_pct: float
    # A scenario is solved when its starting assets are known to within this fraction of the BEL: the lowest starting assets
    # with enough assets (positive ending assets) are within this fraction of the highest starting assets without enough
    # (negative ending assets). The BEL is then guaranteed to be within that range
    bel_pct: float


# Each point is {"value": starting assets, "result": ending assets, ...}. Ending assets are assumed to rise with starting
# assets, so a scenario's required starting assets are above any starting value with negative ending assets and below
# any starting value with positive ending assets


def within_tolerance(point: dict, tolerances: Tolerances) -> bool:
    return abs(point["result"]) <= tolerances.ending_assets_pct * abs(point["value"])


# The point within the ending assets tolerance closest to 0 ending assets, or None if the scenario has no such run
def converged_point(points: list[dict], tolerances: Tolerances) -> dict | None:
    within = [point for point in points if within_tolerance(point, tolerances)]
    return min(within, key=lambda point: abs(point["result"])) if within else None


# The lowest starting assets known to be enough (not negative ending assets) for a scenario, or None if none was run
def sufficient_point(points: list[dict]) -> dict | None:
    enough = [point for point in points if point["result"] >= 0]
    return min(enough, key=lambda point: point["value"]) if enough else None


# The highest starting assets known to be too low (negative ending assets) in any scenario. The BEL is above it, as the BEL
# is the highest starting assets any scenario needs. None if no run had negative ending assets
def lower_bound(guesses: dict[int, list[dict]]) -> float | None:
    return max((point["value"] for points in guesses.values() for point in points if point["result"] < 0), default=None)


# True if the sufficient point is within the BEL tolerance of the lower bound
def bel_tolerance_met(sufficient: dict | None, lower: float | None, tolerances: Tolerances) -> bool:
    return (sufficient is not None and lower is not None
            and sufficient["value"] - lower <= tolerances.bel_pct * sufficient["value"])


# Update the status of every ACTIVE scenario from all results so far. status is changed in place and the scenarios
# that changed are returned. The BEL is the highest required starting assets of all scenarios, so:
# - a scenario with a run within the ending assets tolerance is CONVERGED
# - a scenario with positive (out of tolerance) ending assets at starting assets v needs less than v. If another scenario
#   has a run at v or higher that is within tolerance or has negative ending assets, that scenario needs at least as
#   much as v, so this one can't be the binding scenario and is NON_BINDING. That other scenario can be in any status
# - a scenario whose lowest sufficient starting assets are within the BEL tolerance of the lower bound is CONVERGED
def update_statuses(guesses: dict[int, list[dict]], status: dict[int, ScenarioStatus], tolerances: Tolerances) -> dict[int, ScenarioStatus]:
    changed = {}
    lower = lower_bound(guesses)
    for scenario in [s for s, current in status.items() if current == ScenarioStatus.ACTIVE]:
        points = guesses.get(scenario, [])
        if converged_point(points, tolerances) is not None:
            changed[scenario] = ScenarioStatus.CONVERGED
            continue

        # The lowest positive starting assets is the tightest upper limit on what this scenario needs
        upper = min((point["value"] for point in points if point["result"] > 0 and not within_tolerance(point, tolerances)),
                    default=None)
        if upper is not None and any(point["value"] >= upper and (point["result"] < 0 or within_tolerance(point, tolerances))
                                     for other, other_points in guesses.items() if other != scenario
                                     for point in other_points):
            changed[scenario] = ScenarioStatus.NON_BINDING
        elif bel_tolerance_met(sufficient_point(points), lower, tolerances):
            changed[scenario] = ScenarioStatus.CONVERGED

    status.update(changed)
    return changed


# The point a CONVERGED scenario is solved at: its run within the ending assets tolerance if it has one, otherwise its lowest
# sufficient run
def solved_point(points: list[dict], tolerances: Tolerances) -> dict | None:
    return converged_point(points, tolerances) or sufficient_point(points)


# The converged scenario needing the most starting assets, as (scenario, point), or None if no scenario has converged
def binding_converged(guesses: dict[int, list[dict]], status: dict[int, ScenarioStatus],
                      tolerances: Tolerances) -> tuple[int, dict] | None:
    converged = {scenario: solved_point(guesses[scenario], tolerances)
                 for scenario, current in status.items() if current == ScenarioStatus.CONVERGED}
    if not converged:
        return None
    scenario = max(converged, key=lambda s: converged[s]["value"])
    return scenario, converged[scenario]


# The range the BEL is guaranteed to lie in, as (lower, upper) starting assets, or None if it isn't known yet. The lower end
# is the highest starting assets without enough assets in any scenario. The upper end is the highest, across the scenarios
# that can set the BEL, of each one's lowest starting assets with enough assets
def bel_range(guesses: dict[int, list[dict]], status: dict[int, ScenarioStatus]) -> tuple[float, float] | None:
    lower = lower_bound(guesses)
    uppers = [sufficient_point(guesses.get(scenario, [])) for scenario, current in status.items()
              if current != ScenarioStatus.NON_BINDING]
    if lower is None or not uppers or any(point is None for point in uppers):
        return None
    return lower, max(max(point["value"] for point in uppers), lower)


# How many times the gap between the secant and inverse quadratic estimates the 3 guesses of a round are spread apart
SPREAD_FACTOR = 1.5


# Inverse quadratic interpolation through 3 results: the starting assets estimated to give 0 ending assets. The 3 results
# must have different ending assets
def inverse_quadratic(p0: dict, p1: dict, p2: dict) -> float:
    x0, x1, x2 = p0["value"], p1["value"], p2["value"]
    f0, f1, f2 = p0["result"], p1["result"], p2["result"]
    return (x0 * f1 * f2 / ((f0 - f1) * (f0 - f2))
            + x1 * f0 * f2 / ((f1 - f0) * (f1 - f2))
            + x2 * f0 * f1 / ((f2 - f0) * (f2 - f1)))


# Work out the next 3 guesses (low, mid, high starting assets) for a scenario from all of its results so far. Returns the
# scenario's solver state: runs, best point, bracket (lower/upper, None if not bracketed), estimate, method and guesses
#
# The mid guess is the best estimate of the starting assets that give 0 ending assets, from the 3 results closest to 0
# ending assets (on either side). Ending assets are curved near the answer, so a straight line through 2 points always
# misses it. The gap between the secant estimate and the inverse quadratic estimate shows how curved the results are, so
# it sets how far the low and high guesses are placed either side of the mid guess. This keeps the 3 guesses tight enough
# to be accurate, while wide enough to straddle the answer. When the answer is bracketed, the guesses stay inside the bracket
def next_scenario_guesses(points: list[dict], tolerances: Tolerances) -> dict:
    below = [point for point in points if point["result"] < 0]
    above = [point for point in points if point["result"] >= 0]
    ordered = sorted(points, key=lambda point: abs(point["result"]))
    best = ordered[0]
    state = {"runs": len(points), "best": best, "lower": None, "upper": None}

    if below and above:
        # Bracketed - the answer lies between the closest results either side of 0
        lower, upper = sorted((max(below, key=lambda point: point["result"])["value"],
                               min(above, key=lambda point: point["result"])["value"]))
        state.update(lower=lower, upper=upper)
    else:
        lower = upper = None

    second = next((point for point in ordered[1:] if point["value"] != best["value"]), None)
    slope = (best["result"] - second["result"]) / (best["value"] - second["value"]) if second else 0
    if slope <= 0:
        # Ending assets don't increase with starting assets, so there is no usable slope
        if lower is not None:
            estimate, spread, method = (lower + upper) / 2, (upper - lower) / 4, "bisection"
        else:
            # Step 1% towards 0 ending assets
            step = (1 if best["result"] < 0 else -1) * abs(best["value"]) * 0.01
            estimate, spread, method = best["value"] + step, abs(step) / 4, "step"
    else:
        estimate = best["value"] - best["result"] / slope
        uncertainty, method = 0.0, "secant"
        third = next((point for point in ordered if point is not best and point is not second
                      and point["value"] not in (best["value"], second["value"])
                      and point["result"] not in (best["result"], second["result"])), None)
        if third is not None:
            quadratic = inverse_quadratic(best, second, third)
            if math.isfinite(quadratic):
                uncertainty, estimate, method = abs(quadratic - estimate), quadratic, "inverse quadratic"
        # Space the guesses at least the starting assets equivalent of the tighter tolerance apart (half the BEL tolerance
        # lets a low and high guess close the bracket on the answer)
        minimum_spread = min(tolerances.ending_assets_pct * abs(best["value"]) / slope,
                             tolerances.bel_pct * abs(best["value"]) / 2)
        spread = max(SPREAD_FACTOR * uncertainty, minimum_spread)
        if lower is not None:
            if not lower < estimate < upper:
                estimate, method = (lower + upper) / 2, "bisection"
            spread = min(spread, (upper - lower) / 3)

    state.update(estimate=estimate, method=method, guesses=[estimate - spread, estimate, estimate + spread])
    return state


# The starting assets to run a scenario that is no longer being solved at: its run with the smallest ending assets that are not
# negative, so the scenario always has enough assets. If it has no such run (it converged within the ending assets tolerance
# from below), its next high guess, which is above its estimate of the starting assets that give 0 ending assets
def reporting_guess(points: list[dict], tolerances: Tolerances) -> float:
    enough = [point for point in points if point["result"] >= 0]
    if enough:
        return min(enough, key=lambda point: point["result"])["value"]
    return next_scenario_guesses(points, tolerances)["guesses"][2]


# Returns the next round of guesses as [low, mid, high] lists of starting assets indexed by scenario number, plus the solver
# state of each scenario still to solve. Every scenario is run, so each list has a value for every scenario. A scenario still
# to solve gets its next low, mid and high guesses. Any other scenario (converged or non-binding) is run at its reporting
# guess in all three projections. Its results are not used. Rows with no scenario (the table has at least min_rows) keep the
# default guess, which is ignored
def next_round_guesses(prior_results: dict[int, list[dict]], status: dict[int, ScenarioStatus], tolerances: Tolerances,
                       default_guess: float, min_rows: int) -> tuple[list[list[float]], dict[int, dict]]:
    size = max(min_rows, max(prior_results, default=-1) + 1)
    next_guesses = [[default_guess] * size for _ in range(3)]
    states = {}
    for scenario, points in prior_results.items():
        if status.get(scenario) != ScenarioStatus.ACTIVE:
            guess = reporting_guess(points, tolerances)
            for guesses in next_guesses:
                guesses[scenario] = guess
            continue
        states[scenario] = next_scenario_guesses(points, tolerances)
        for k in range(3):
            next_guesses[k][scenario] = states[scenario]["guesses"][k]
    return next_guesses, states
