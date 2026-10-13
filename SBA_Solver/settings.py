import os
from pathlib import Path
from Shared.credentials import get_api_credentials

# SLOPE API credentials are read from the git-ignored file at the repo root (see env.example)
api_key, api_secret = get_api_credentials()

# Scratch/working folder for intermediate solver files.
# Override per-machine with the SLOPE_WORKING_DIR env var; otherwise default
# to a folder under the user's home directory (works on Windows and macOS).
solver_folder = Path(
    os.environ.get("SLOPE_WORKING_DIR", Path.home() / "Slope API" / "SBA Solver")
)

# Versioned input template that ships in the repo alongside this script,
# resolved relative to this file so it works on any OS without manual copying.
sba_scenario_generator = Path(__file__).parent / "SBA Scenario Generator.xlsx"

starting_asset_table_name = "Initial Asset Scaling"          # The name of the starting assets table
epl_table_name = "EPL Inputs"                                # The name of the EPL Input Table
virtual_folder_name = "SBA Solver"                           # The name of the virtual folder in SLOPE to store projections in Slope

# Solver projections - the first solver run is built automatically from the base projection, later runs are copied from it
# Liability products on the base projection are replaced by this single Externally Projected Liability product
epl_product_name = "Externally Projected Liability"          # The name of the EPL product on the model
epl_liability_id_field = "ID"                                # EPL model point field set to "BEL" to link to the 'Liability ID' rows of the EPL Inputs table
epl_mpf_path = "SBA Solver/EPL Model Point File.csv"         # File Manager path of the EPL model point file - created on first run, then reused.
                                                             # When created, date fields (e.g. Issue Date) are set to the base projection start date

# Column values overridden on the base projection's tables for the solver runs, keyed by Table Structure name
# A copy of the table used on the base projection is saved with these values on every row and set on the solver projections
solver_table_overrides = {
    "Portfolio Parameters": {
        "Initial Asset Scaling": "Use Asset Scaling Table",
    },
    "Company Properties": {
        "Required Capital Method": "None",
        "Distribute Earnings?": "false",
        "Tax Rate": "0",
    },
}

# Override tables shared by every SBA solve, keyed by Table Structure name, with the data table name to use
# The table is created once from the base projection's table if it doesn't exist, then reused for every solve. If it
# exists, its overridden column values are checked and corrected if needed. Any table not listed here is saved per
# base projection as '<base projection id> SBA <table structure>'
shared_solver_tables = {
    "Company Properties": "SBA Solver Company Properties",
}

solver_final_asset_tolerance = 100000       # tolerance for remaining assets for the BEL solve - Higher tolerances will converge faster
solver_max_iterations = 5                   # The maximum number of attempts to solve for BEL. If max iterations is exceeded, the last closes guess outside the tolerance will be used
next_guess_range = 0.10                     # Check a 10% weighted average range around the last guess
