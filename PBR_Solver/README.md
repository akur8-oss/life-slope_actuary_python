# PBR Solver Runbook

## 1. Overview
The PBR solver automates VM‑20 asset collar calculations by iteratively running SLOPE projections until projected starting assets converge within a 2% tolerance of the stochastic reserve. It orchestrates report retrieval, scenario sampling, liability cash-flow preparation, and repeated projection runs through the SLOPE API.

## 2. Environment Setup
### Python version
- Requires Python 3.13 or later (per project metadata).【F:PBR_Solver/pyproject.toml†L1-L8】

### Dependencies (pip installs)
Install the libraries used by the solver scripts:
- `requests` (SLOPE API calls)【F:Shared/slope_api.py†L1-L10】
- `pandas` (report/cash-flow data handling)【F:Shared/slope_api.py†L7-L9】【F:Shared/sigma_report.py†L6-L18】
- `python-dateutil` (token expiry parsing)【F:Shared/slope_api.py†L1-L8】
- `urllib3` (retry adapter via `requests`)【F:Shared/slope_api.py†L7-L10】

Example:
```bash
pip install requests pandas python-dateutil urllib3
```

### Quick Start: Run `main.py`
1. **Verify Python version**:
   ```bash
   python --version
   ```
2. **Install dependencies** (if not already installed):
   ```bash
   pip install requests pandas python-dateutil urllib3
   ```
3. **Set credentials** in `Shared/keys.py`:
   - `api_key`
   - `api_secret`
4. **Set inputs** in `PBR_Solver/main.py`:
   - `projection_id = <your projection id>`
   - Optional: adjust `logging_level`
5. **Run from the repo root**:
   ```bash
   python PBR_Solver/main.py
   ```

Notes:
- Working files are written to `c:\Slope API\VM20` by default (see `VM20Params.working_directory`).【F:PBR_Solver/vm20_params.py†L5-L18】

### Folder structure
Key directories/files under `PBR_Solver`:
- `main.py`: Entry point configuring parameters and launching the solver.【F:PBR_Solver/main.py†L14-L74】
- `vm20.py`: Core VM‑20 solver logic and API interactions.【F:PBR_Solver/vm20.py†L11-L360】
- `vm20_params.py`: Dataclasses for runtime and restart parameters.【F:PBR_Solver/vm20_params.py†L5-L24】
- `guess_iteration.py`: Tracks successive asset guesses for secant updates.【F:PBR_Solver/guess_iteration.py†L3-L8】
- `reports.json`: Sigma report IDs and filters used for data pulls.【F:PBR_Solver/reports.json†L1-L26】
- `Shared/`: Common API, report, and credential helpers (`slope_api.py`, `sigma_report.py`, `keys.py`).【F:PBR_Solver/main.py†L6-L10】

## 3. Script Architecture
- **Entry point (`main.py`)**: Sets logging, loads report metadata, assembles `VM20Params`, optional `VM20RestartParams`, and calls `VM20.solve_asset_collar`.【F:PBR_Solver/main.py†L21-L74】
- **Solver engine (`VM20` in `vm20.py`)**: Manages ID discovery, scenario sampling, liability data preparation, iterative secant solving, and stochastic validation. Uses `SlopeApi` and `SigmaReport` helpers for all SLOPE interactions.【F:PBR_Solver/vm20.py†L27-L360】
- **Parameter models (`vm20_params.py`)**: Captures static configuration (API keys, template/table names, iteration limits, working directories) and optional restart checkpoints (starting assets, scenario list, prior guesses, EPL table ID).【F:PBR_Solver/vm20_params.py†L5-L24】
- **Report handling (`sigma_report.py`)**: Downloads Sigma reports, optionally in batches, saves CSVs, and exposes dataframes for solver use.【F:Shared/sigma_report.py†L10-L70】
- **API client (`slope_api.py`)**: Handles authorization, projection lifecycle, table uploads/patching, report downloads, and status polling with retry-aware HTTP sessions.【F:Shared/slope_api.py†L13-L457】

## 4. Input Parameters
### Dynamic inputs (per run)
- `projection_id` in `main.py`: Base projection to solve against.【F:PBR_Solver/main.py†L14-L74】
- `logging_level` in `main.py` controls verbosity to console.【F:PBR_Solver/main.py†L17-L27】
- `VM20Params` values like scenario sample size, min scenarios, max iterations, template name, and table structure names tailor run behaviour.【F:PBR_Solver/main.py†L45-L55】【F:PBR_Solver/vm20_params.py†L5-L24】

### Static inputs (configured once per environment)
- API credentials from `Shared/keys.py` referenced in `VM20Params`.【F:PBR_Solver/main.py†L6-L55】【F:Shared/keys.py†L1-L4】 Ensure they are set to your tenant values.
- `reports.json` workbook/element IDs and filter keys that map to Sigma reports used by the solver (Scenario Reserves, Liability Cash Flows, Starting Assets).【F:PBR_Solver/reports.json†L1-L26】【F:PBR_Solver/main.py†L29-L55】
- Working directory root (`VM20Params.working_directory`) where CSVs are stored locally; defaults to `c:\Slope API\VM20`.【F:PBR_Solver/vm20_params.py†L5-L18】

### One-time configuration inputs
- Projection template name (`pbr_projection_template_name`) matching the VM‑20 solver template in SLOPE.【F:PBR_Solver/main.py†L51-L55】
- Table structure names for EPL inputs and starting asset scaling to align uploaded CSVs with your SLOPE model structures.【F:PBR_Solver/main.py†L51-L55】【F:PBR_Solver/vm20_params.py†L15-L18】

## 5. Execution Flow
1. **Initialize logging and load report metadata** via `setup_logging()` and `parse_reports_json()`.【F:PBR_Solver/main.py†L21-L55】
2. **Create parameter objects** (`VM20Params` plus optional `VM20RestartParams`) including credentials, solver tolerances, and restart hints.【F:PBR_Solver/main.py†L45-L69】【F:PBR_Solver/vm20_params.py†L5-L24】
3. **Start solver**: `VM20.solve_asset_collar(projection_id, restart_params)` logs restart context, gathers template/table IDs, and prepares working directories.【F:PBR_Solver/vm20.py†L36-L118】
4. **Derive starting assets** from the Starting Assets Sigma report unless provided in restart params.【F:PBR_Solver/vm20.py†L53-L78】【F:PBR_Solver/vm20.py†L228-L236】
5. **Sample scenarios**: identify CTE(70) worst scenarios and down-sample to a target count unless a list is provided.【F:PBR_Solver/vm20.py†L81-L248】
6. **Prepare liabilities**: pull batched Liability Cash Flows reports for sampled scenarios, add required columns, and upload as an EPL data table via the API.【F:PBR_Solver/vm20.py†L94-L226】
7. **Iterative solving**: run successive projections with updated starting assets using a secant method until the asset collar tolerance is met or `max_iterations` is reached.【F:PBR_Solver/vm20.py†L293-L360】
8. **Full stochastic validation**: rerun the full scenario set with the solved assets and verify tolerance; return final assets and projection ID.【F:PBR_Solver/vm20.py†L103-L118】

## 6. API Integration Points
- **Authentication**: `SlopeApi.authorize(api_key, api_secret)` establishes session tokens used across all calls.【F:Shared/slope_api.py†L44-L70】
- **Metadata discovery**: `get_projection_details`, `list_projection_templates`, and `list_table_structures` fetch model IDs, template IDs, and table structure IDs for uploads and projection creation.【F:PBR_Solver/vm20.py†L44-L186】【F:Shared/slope_api.py†L310-L406】
- **Report retrieval**: `SigmaReport.retrieve` / `retrieve_batched` download Sigma reports needed for reserves, starting assets, and liabilities.【F:Shared/sigma_report.py†L37-L70】
- **Projection lifecycle**: `create_projection_from_template`, `update_projection`, `run_projection`, and `wait_for_completion` drive iteration runs.【F:PBR_Solver/vm20.py†L285-L345】【F:Shared/slope_api.py†L166-L457】
- **Data table patching/upload**: `create_or_update_data_table` uploads CSVs for starting assets and EPL cash flows to the appropriate table structures (e.g., `"EPL Inputs"`, `"Initial Asset Scaling"`).【F:PBR_Solver/vm20.py†L131-L226】【F:Shared/slope_api.py†L119-L146】

## 7. Error Handling and Logging
- Exceptions inside `solve_asset_collar` are logged with restart context (starting assets, scenarios, EPL table ID, prior guesses) before being re-raised for visibility.【F:PBR_Solver/vm20.py†L119-L129】
- The API client logs failed responses and raises for status when HTTP calls are unsuccessful; detailed headers/body are logged for troubleshooting.【F:Shared/slope_api.py†L28-L143】
- Logging output defaults to the console with timestamped entries; adjust `logging_level` in `main.py` for more or less verbosity.【F:PBR_Solver/main.py†L17-L27】
- Report CSVs and working files are stored under `VM20Params.working_directory` with per-projection subfolders (e.g., `c:\Slope API\VM20\Projection-<id>`).【F:PBR_Solver/vm20.py†L47-L52】【F:Shared/sigma_report.py†L12-L33】

## 8. Maintenance Guidelines
- **API surface changes**: If SLOPE updates endpoints, table structures, or template names, adjust `pbr_projection_template_name`, `epl_table_structure_name`, and `starting_assets_table_structure_name` to the new values and verify column expectations when building EPL uploads.【F:PBR_Solver/vm20.py†L176-L226】【F:PBR_Solver/vm20_params.py†L5-L18】
- **Report updates**: If workbook/element IDs change, update `reports.json` so Sigma report retrieval continues to work.【F:PBR_Solver/reports.json†L1-L26】【F:PBR_Solver/main.py†L29-L55】
- **Logging & debugging**: Increase `logging_level` to `DEBUG` to see API payloads and solver iteration details; check console output and CSVs in the working directory for run artifacts.【F:PBR_Solver/main.py†L17-L27】【F:PBR_Solver/vm20.py†L131-L360】

## 9. Troubleshooting Tips
- **No scenarios or starting assets found**: Ensure the projection ID is valid and Sigma report filters in `reports.json` match your tenant; missing data raises a `ValueError`.【F:PBR_Solver/vm20.py†L158-L277】【F:PBR_Solver/reports.json†L1-L26】
- **Convergence not reached**: Check `max_iterations`, sample size settings, and restart with `initial_guesses` if available to resume secant steps from the last iteration. Logs show guess/difference history.【F:PBR_Solver/vm20.py†L293-L360】【F:PBR_Solver/guess_iteration.py†L3-L8】
- **API errors (401/403/409/etc.)**: Verify API keys in `Shared/keys.py`, confirm the user has access to referenced models/templates/tables, and review logged response bodies from `SlopeApi.__check_response`.【F:Shared/keys.py†L1-L4】【F:Shared/slope_api.py†L28-L143】
- **Table upload shape mismatches**: If EPL or starting asset uploads fail, confirm table structure column names in SLOPE match those expected in `__get_liability_cashflows` and `__create_starting_asset_table`; update names or add columns accordingly.【F:PBR_Solver/vm20.py†L131-L226】
- **Long-running reports**: The API retry logic waits and retries report downloads if generation is slow; rerun with higher logging to watch the retry loop.【F:Shared/slope_api.py†L208-L239】
