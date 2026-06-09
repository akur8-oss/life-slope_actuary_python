import os
from dataclasses import dataclass
from pathlib import Path
from guess_iteration import GuessIteration
from Shared.sigma_report import SigmaReportParams

# Scratch/working folder for intermediate solver files.
# Override per-machine with the VM20_WORKING_DIR env var; otherwise default
# to a folder under the user's home directory (works on Windows and macOS).
_DEFAULT_VM20_DIR = Path(
    os.environ.get("VM20_WORKING_DIR", Path.home() / "Slope API" / "VM20")
)

@dataclass
class VM20Params:
    api_key: str
    api_secret: str
    # Determines sample size for scenarios for intermediate guess runs - Use greater of 5 or 10% of scenarios
    scenario_sample_size: float
    min_scenarios: int
    max_iterations: int
    pbr_projection_template_name: str
    reports: dict[str, SigmaReportParams] = None
    working_directory: Path = _DEFAULT_VM20_DIR
    projection_virtual_folder = "VM-20 Solver"
    epl_table_structure_name: str = "EPL Inputs"
    starting_assets_table_structure_name: str = "Initial Asset Scaling"

@dataclass
class VM20RestartParams:
    starting_assets: float = None
    sample_scenarios: list[int] = None
    epl_table_id: int = None
    initial_guesses: GuessIteration = None