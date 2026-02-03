# Add parent folder to allow import of shared modules
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from Shared.keys import *
import logging
from guess_iteration import GuessIteration
from Shared.sigma_report import SigmaReportParams
from vm20 import VM20, VM20Params, VM20RestartParams
import os
import json

# Projection ID to Solve
projection_id = 163450  # Replace with your actual projection ID

# Change this to appropriate level for your run
logging_level = logging.INFO


def setup_logging():
    log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging_level)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)

def parse_reports_json() -> dict[str, SigmaReportParams]:
    # Load reports.json from the local directory
    current_dir = os.path.dirname(__file__)
    reports_file_path = os.path.join(current_dir, 'reports.json')

    with open(reports_file_path, 'r') as file:
        reports_json = json.load(file)

    # Convert each value in reports_data to a SigmaReportParams object
    reports_data = {key: SigmaReportParams.from_dict(value) for key, value in reports_json.items()}

    return reports_data

if __name__ == '__main__':
    setup_logging()

    params = VM20Params(
        api_key=api_key,
        api_secret=api_secret,
        scenario_sample_size=0.10,
        min_scenarios=8,
        max_iterations=5,
        pbr_projection_template_name="VM-20 Asset Collar Solver Template",
        epl_table_structure_name="EPL Inputs",
        starting_assets_table_structure_name="Initial Asset Scaling",
        reports = parse_reports_json()
    )

    # Only use restart params if the you want to start midway through the solver routine. Otherwise, don't include this.
    restart_params = None
    # restart_params = VM20RestartParams(
    #    starting_assets=1634355.36,
    #    sample_scenarios=None,
    #    epl_table_id=None,
    #    initial_guesses=None
    # )
    #     sample_scenarios=[56, 147, 50, 67, 64, 65, 41, 110],
    #     epl_table_id=1251682
    # )
    #    initial_guesses=GuessIteration(
    #        prior_guess=66000000.0, 
    #        prior_result=55623470.27275342, 
    #        current_guess=99623470.27275342
    #    )
    #)

    # Run VM-20 Solver Process
    vm20_solver = VM20(params)
    assets, projection_id = vm20_solver.solve_asset_collar(projection_id, restart_params)
    logging.info(f"Final Assets: {assets:,.2f}, Projection ID: https://app.slopesoftware.com/ModelResults/FinancialProjection/DetailsTabView/{projection_id}")
    logging.info(f"VM-20 Solver Process Completed Successfully.")