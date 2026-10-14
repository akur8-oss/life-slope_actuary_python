# Add parent folder to allow import of shared modules
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import json
import os
import logging
import sba_solver
from run_config import load_run_config
from Shared.sigma_report import SigmaReportParams

# Run the SBA solver for a run file. Run inputs (base projection, time points, initial guess) are set in the run file,
# not here - see runs/example.toml
#   uv run python main.py                       (uses runs/run.toml)
#   uv run python main.py runs/<your run>.toml


def setup_logging(logging_level: str):
    # Setup Logger
    log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging_level)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)

def get_reports_data() -> dict[str, SigmaReportParams]:
    # Load reports.json from the local directory
    current_dir = os.path.dirname(__file__)
    reports_file_path = os.path.join(current_dir, 'reports.json')

    with open(reports_file_path, 'r') as file:
        reports_json = json.load(file)

    # Convert each value in reports_data to a SigmaReportParams object
    reports_data = {key: SigmaReportParams.from_dict(value) for key, value in reports_json.items()}

    return reports_data

def run(run_file: str | None = None):
    # Load and check the run file before anything is sent to SLOPE
    config = load_run_config(run_file)
    setup_logging(config.log_level)
    config.log_summary()

    reports = get_reports_data()
    solver = sba_solver.SbaSolver(config.base_projection_id, reports, config.solver)

    solver.calculate_bel(config.time_points, config.initial_guesses, config.use_base_scenarios_at_time_zero)
    solver.print_results()
    solver.save_results(config)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
