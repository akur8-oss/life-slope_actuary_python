import os
from pathlib import Path
from Shared.keys import api_key as key, api_secret as secret

# Substitute Real API credentials here
api_key = key
api_secret = secret

# Scratch/working folder for intermediate solver files.
# Override per-machine with the SLOPE_WORKING_DIR env var; otherwise default
# to a folder under the user's home directory (works on Windows and macOS).
solver_folder = Path(
    os.environ.get("SLOPE_WORKING_DIR", Path.home() / "Slope API" / "SBA Solver")
)

# Versioned input template that ships in the repo alongside this script,
# resolved relative to this file so it works on any OS without manual copying.
sba_scenario_generator = Path(__file__).parent / "SBA Scenario Generator.xlsx"

# Usage Notes
# The sba_template_name specified in this setting MUST have the following:
# 1. A single portfolio named 'Inforce Portfolio' (this can be changed by changing the projection update template below)
# 2. The proper reinvestment strategy must be set up on the specified template
# 3. The asset scaling variable needs to be set on the Projection Portfolio
# 4. The Portfolio Parameters Table that is used should include Initial Asset Scaling Type of '4' in the data table
# 5. The Company Properties Table should be set to
#       Capital Method 'None',
#       Distribute Earnings: False
#       Tax Rate: 0
sba_template_name = "SBA Solver Template"                    # The name of the projection template to be used within the solver
starting_asset_table_name = "Initial Asset Scaling"          # The name of the starting assets table
epl_table_name = "EPL Inputs"                                # The name of the EPL Input Table
virtual_folder_name = "SBA Solver"                           # The name of the virtual folder in SLOPE to store projections in Slope

solver_final_asset_tolerance = 100000        # tolerance for remaining assets for the BEL solve - Higher tolerances will converge faster
solver_max_iterations = 4                   # The maximum number of attempts to solve for BEL. If max iterations is exceeded, the last closes guess outside the tolerance will be used
next_guess_range = 0.20                     # Check a 20% weighted average range around the last guess
