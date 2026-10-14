import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
import settings

# Run files live here. Only example.toml is committed - copy it to create your own run file
RUNS_FOLDER = Path(__file__).parent / "runs"
DEFAULT_RUN_FILE = RUNS_FOLDER / "run.toml"
EXAMPLE_RUN_FILE = RUNS_FOLDER / "example.toml"

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


# Solver tuning. Defaults come from settings.py and can be overridden per run in the [solver] section of a run file
@dataclass(frozen=True)
class SolverTuning:
    tolerance: float = settings.solver_final_asset_tolerance
    max_iterations: int = settings.solver_max_iterations
    provided_guess_brackets: list[float] = field(default_factory=lambda: list(settings.provided_guess_brackets))
    derived_guess_brackets: list[float] = field(default_factory=lambda: list(settings.derived_guess_brackets))


# The inputs for one SBA solver run, loaded from a run file
@dataclass(frozen=True)
class RunConfig:
    path: Path
    base_projection_id: int
    time_points: list[int]
    # Starting-asset guess by time point. Time points not listed derive their guess from the market value of liabilities
    initial_guesses: dict[int, float]
    description: str = ""
    log_level: str = "INFO"
    # Time 0 keeps the base projection's scenario table instead of building and uploading a new scenario file
    use_base_scenarios_at_time_zero: bool = False
    solver: SolverTuning = field(default_factory=SolverTuning)

    def log_summary(self):
        logging.info(f"Run file: {self.path}")
        if self.description:
            logging.info(f"Description: {self.description}")
        logging.info(f"Base projection: {self.base_projection_id}")
        logging.info(f"Time points: {self.time_points}")
        logging.info(f"Initial guesses: {self.initial_guesses or 'derived from market value of liabilities'}")
        if 0 in self.time_points:
            logging.info("Time 0 scenarios: " + ("base projection's scenario table" if self.use_base_scenarios_at_time_zero
                                                 else "new scenario file"))
        logging.info(f"Solver tuning: {self.solver}")


_TOP_LEVEL_KEYS = {"description", "base_projection_id", "time_points", "initial_guess", "logging", "time_zero", "solver"}
_LOGGING_KEYS = {"level"}
_TIME_ZERO_KEYS = {"use_base_scenarios"}
_SOLVER_KEYS = {"tolerance", "max_iterations", "provided_guess_brackets", "derived_guess_brackets"}


def load_run_config(path: str | Path | None = None) -> RunConfig:
    """Load and validate a run file. Every problem found is reported together in one error, before any API call."""
    path = Path(path) if path else DEFAULT_RUN_FILE
    if not path.is_file():
        raise FileNotFoundError(f"Run file '{path}' not found. Copy '{EXAMPLE_RUN_FILE}' to create one, e.g. "
                                f"'{DEFAULT_RUN_FILE}', then edit it")
    with open(path, "rb") as file:
        try:
            data = tomllib.load(file)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"Run file '{path}' is not valid TOML: {e}") from e

    errors = []
    _check_keys(data, _TOP_LEVEL_KEYS, "", errors)

    base_projection_id = data.get("base_projection_id")
    if not _is_int(base_projection_id) or base_projection_id <= 0:
        errors.append(f"base_projection_id must be a positive whole number, got {base_projection_id!r}")

    time_points = data.get("time_points")
    if not isinstance(time_points, list) or not time_points or not all(_is_int(t) and t >= 0 for t in time_points):
        errors.append(f"time_points must be a list of whole numbers >= 0 (months), e.g. [0, 12], got {time_points!r}")
        time_points = []
    elif len(set(time_points)) != len(time_points):
        errors.append(f"time_points has duplicates: {time_points}")

    initial_guesses = _parse_initial_guess(data.get("initial_guess"), time_points, errors)

    logging_section = data.get("logging", {})
    log_level = "INFO"
    if _check_section(logging_section, "logging", _LOGGING_KEYS, errors):
        log_level = str(logging_section.get("level", "INFO")).upper()
        if log_level not in LOG_LEVELS:
            errors.append(f"logging.level must be one of {LOG_LEVELS}, got {logging_section.get('level')!r}")

    time_zero_section = data.get("time_zero", {})
    use_base_scenarios = False
    if _check_section(time_zero_section, "time_zero", _TIME_ZERO_KEYS, errors):
        use_base_scenarios = time_zero_section.get("use_base_scenarios", False)
        if not isinstance(use_base_scenarios, bool):
            errors.append(f"time_zero.use_base_scenarios must be true or false, got {use_base_scenarios!r}")

    solver = _parse_solver(data.get("solver", {}), errors)

    description = data.get("description", "")
    if not isinstance(description, str):
        errors.append(f"description must be text, got {description!r}")

    if errors:
        raise ValueError(f"Run file '{path}' has problems:\n  " + "\n  ".join(errors))

    return RunConfig(path=path.resolve(), base_projection_id=base_projection_id, time_points=time_points,
                     initial_guesses=initial_guesses, description=description, log_level=log_level,
                     use_base_scenarios_at_time_zero=use_base_scenarios, solver=solver)


# initial_guess is either a single number used for every time point, or a table with an optional "all" value and
# per-time-point values keyed by time point (as text, e.g. "12" = 1450000000), which override "all"
def _parse_initial_guess(value, time_points: list[int], errors: list[str]) -> dict[int, float]:
    if value is None:
        return {}
    if _is_number(value):
        if value <= 0:
            errors.append(f"initial_guess must be positive, got {value!r}")
            return {}
        return {t: float(value) for t in time_points}
    if not isinstance(value, dict):
        errors.append(f"initial_guess must be a number or a table, got {value!r}")
        return {}

    default = value.get("all")
    guesses = {}
    for key, guess in value.items():
        if key == "all":
            continue
        if not key.isdigit():
            errors.append(f"initial_guess key {key!r} must be 'all' or a time point, e.g. \"12\"")
        elif int(key) not in time_points:
            errors.append(f"initial_guess has a guess for time point {key}, which is not in time_points {time_points}")
        else:
            guesses[int(key)] = guess
    if default is not None:
        for t in time_points:
            guesses.setdefault(t, default)

    for t, guess in list(guesses.items()):
        if not _is_number(guess) or guess <= 0:
            errors.append(f"initial_guess for time point {t} must be a positive number, got {guess!r}")
            del guesses[t]
        else:
            guesses[t] = float(guess)
    return guesses


def _parse_solver(section, errors: list[str]) -> SolverTuning:
    defaults = SolverTuning()
    if not _check_section(section, "solver", _SOLVER_KEYS, errors):
        return defaults

    tolerance = section.get("tolerance", defaults.tolerance)
    if not _is_number(tolerance) or tolerance <= 0:
        errors.append(f"solver.tolerance must be a positive number, got {tolerance!r}")

    max_iterations = section.get("max_iterations", defaults.max_iterations)
    if not _is_int(max_iterations) or max_iterations < 1:
        errors.append(f"solver.max_iterations must be a whole number >= 1, got {max_iterations!r}")

    brackets = {}
    for name in ("provided_guess_brackets", "derived_guess_brackets"):
        brackets[name] = section.get(name, getattr(defaults, name))
        if (not isinstance(brackets[name], list) or len(brackets[name]) != 3
                or not all(_is_number(b) and b > 0 for b in brackets[name])):
            errors.append(f"solver.{name} must be a list of 3 positive multiples of the guess, e.g. [0.98, 1.00, 1.02], "
                          f"got {brackets[name]!r}")

    return SolverTuning(tolerance=tolerance, max_iterations=max_iterations, **brackets)


def _check_section(section, name: str, allowed: set[str], errors: list[str]) -> bool:
    if not isinstance(section, dict):
        errors.append(f"[{name}] must be a section (table), got {section!r}")
        return False
    _check_keys(section, allowed, f"{name}.", errors)
    return True


# Report unknown keys, which are usually typos (e.g. time_point instead of time_points)
def _check_keys(data: dict, allowed: set[str], prefix: str, errors: list[str]):
    unknown = sorted(set(data) - allowed)
    if unknown:
        errors.append(f"Unknown setting(s) {[prefix + key for key in unknown]}. Expected one of {sorted(prefix + key for key in allowed)}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
