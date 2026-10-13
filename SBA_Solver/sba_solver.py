import datetime
from dateutil.parser import parse
from dataclasses import dataclass
import pandas as pd
import logging
import openpyxl
from openpyxl.utils.dataframe import dataframe_to_rows
import settings
from Shared.sigma_report import SigmaReport, SigmaReportParams
from Shared.slope_api import SlopeApi
import shutil
import sys
import time
from pathlib import Path
import xlwings as xw


class SbaSolver:
    final_bel = []
    liability_cashflows_table_id = 0

    solver_folder = ""
    slope_file_path = ""
    __asset_market_value = 0
    __base_projection = {}
    __last_cf_time = 0
    __max_iterations = settings.solver_max_iterations
    __tolerance = settings.solver_final_asset_tolerance

    # Columns the solver writes to each table it uploads to SLOPE - checked against the table structures before the solve starts
    EPL_COLUMNS = ["Date", "Cash Flow", "Liability ID", "Scenario Number"]
    STARTING_ASSET_COLUMNS = ["Scaling Target", "Scenario #", "Scaling Factor"]
    # Columns returned by the Liability Cash Flows report. Time Index is only used to find the last cash flow time
    EPL_REPORT_COLUMNS = ["Time Index", "Date", "Cash Flow"]

    @dataclass
    class TimeSolveParams:
        time_index: int
        use_epl: bool = True
        generate_asset_files: bool = True
        generate_scenario_file: bool = True
        initial_guess: float | None = None   # Starting assets to bracket the first guesses around. Derived if None

    def __init__(self, projection_id: int, reports: dict[str, SigmaReportParams]):
        self.reports = reports
        self.base_projection_id = projection_id
        self.api = SlopeApi()
        self.api.authorize(settings.api_key, settings.api_secret)
        self.solver_folder = settings.solver_folder / str(projection_id)
        self.solver_folder.mkdir(parents=True, exist_ok=True)
        self.slope_file_path = f"SBA Solver/{projection_id}"
        self.max_error = "Unknown"
        pd.options.display.float_format = '{:,.2f}'.format

        # Get the base projection that was run
        self.__base_projection = self.api.get_projection_details(self.base_projection_id)
        if self.__base_projection.get("status") not in ["Completed", "CompletedWithErrors"]:
            logging.info("Main Projection must be completed before the solver can be run")
            raise Exception(f"Projection ID {projection_id} has not completed running.")

        # Get the model this was run on
        self.model_id = self.__base_projection.get("model").get("id")

        # Get the Table IDs of the inputs needed from this model
        table_structures = self.api.list_table_structures(self.model_id)

        self.starting_assets_table_id = next((table.get("id") for table in table_structures if table.get("name") == settings.starting_asset_table_name),None)
        self.epl_table_id = next((table.get("id") for table in table_structures if table.get("name") == settings.epl_table_name), None)

        # Check the model is set up for the solver before any data is retrieved or uploaded
        self.__validate_setup(table_structures)
        self.starting_assets_table_columns = self.api.get_table_structure_columns(self.starting_assets_table_id)

        # The solver projections use the base projection's (single) portfolio
        self.portfolio_name = self.__base_projection.get("portfolios")[0].get("name")

        # The first EPL-based solver run, built from the base projection. Every later EPL-based run is copied from it
        self.first_solver_projection_id = None

    # Check the tables and EPL product the solver uses are set up on the model. All problems are reported together so
    # they can be fixed in one pass
    def __validate_setup(self, table_structures: list[dict]):
        # The EPL product is checked first, since the EPL Inputs table is only uploaded if the base doesn't already use EPL
        errors = self.__check_epl_product()
        errors += self.__check_table_structures(table_structures)
        if errors:
            raise ValueError("SBA solver setup check failed. Check the names below in settings.py and sba_solver.py "
                             "against the model:\n  " + "\n  ".join(errors))
        logging.info("SBA solver setup check passed")

    # Check that each table structure the solver uploads data to exists on the model and has the columns the solver
    # writes. Returns a list of problems found
    def __check_table_structures(self, table_structures: list[dict]) -> list[str]:
        structure_ids = {table.get("name"): table.get("id") for table in table_structures}
        base_tables = {table["tableStructureName"]: table for table in self.__base_projection.get("dataTables", [])}
        errors = []

        expected_columns = {settings.starting_asset_table_name: SbaSolver.STARTING_ASSET_COLUMNS}
        if not self.base_uses_epl:
            expected_columns[settings.epl_table_name] = SbaSolver.EPL_COLUMNS
        self.__shared_table_ids = {}
        for structure_name, overrides in settings.solver_table_overrides.items():
            expected_columns[structure_name] = list(overrides)
            # Shared solver tables are reused across solves if they already exist on the model
            shared_name = settings.shared_solver_tables.get(structure_name)
            if shared_name:
                self.__shared_table_ids[structure_name] = next(
                    (table["id"] for table in self.api.list_model_data_tables(self.model_id, structure_name)
                     if table["name"] == shared_name and table.get("isLatestVersion", True)), None)
                if self.__shared_table_ids[structure_name] is not None:
                    continue
            # Otherwise override tables are copied from the table set on the base projection
            if base_tables.get(structure_name, {}).get("dataTableId") is None:
                errors.append(f"Base projection {self.base_projection_id} has no data table set for table structure '{structure_name}'")

        for structure_name, columns in expected_columns.items():
            structure_id = structure_ids.get(structure_name)
            if structure_id is None:
                errors.append(f"Model {self.model_id} has no table structure named '{structure_name}'")
                continue
            structure_columns = {column["name"] for column in self.api.get_table_structure_columns(structure_id)}
            missing = [column for column in columns if column not in structure_columns]
            if missing:
                errors.append(f"Table structure '{structure_name}' is missing column(s) {missing}. "
                              f"It has columns {sorted(structure_columns)}")

        return errors

    # Check the EPL product exists on the model. If the EPL model point file has not been created yet, also check the
    # product has the model point field that links to the EPL Inputs table. Saves the product, its model point fields
    # and any existing model point file for setting up the first solver projection. Returns a list of problems found
    def __check_epl_product(self) -> list[str]:
        self.base_uses_epl = False
        products = self.api.list_model_products(self.model_id)
        self.epl_product = next((product for product in products if product["name"] == settings.epl_product_name), None)
        if self.epl_product is None:
            liability_products = sorted(product["name"] for product in products if product.get("type") == "Liability")
            return [f"Model {self.model_id} has no product named '{settings.epl_product_name}' (settings.epl_product_name). "
                    f"Its liability products are {liability_products}"]

        errors = []
        if self.epl_product.get("type") != "Liability":
            errors.append(f"Product '{settings.epl_product_name}' is a {self.epl_product.get('type')} product - it must be a Liability product")

        # If the base projection already uses the EPL product, the solver runs keep its EPL Inputs table and model point
        # file, so neither needs to be created
        self.base_uses_epl = self.__projection_uses_epl(self.base_projection_id)
        if self.base_uses_epl:
            logging.info(f"Base projection already uses '{settings.epl_product_name}' - its EPL Inputs and model point file will be used")
            self.epl_mpf_file_id = None
            return errors

        # The model point fields only matter if the EPL model point file has to be created
        self.epl_mpf_file_id = self.__find_epl_model_point_file()
        if self.epl_mpf_file_id is not None:
            logging.info(f"Using existing EPL model point file '{settings.epl_mpf_path}'")
            return errors

        self.epl_model_point_fields = self.api.get_product_model_point_fields(self.epl_product["id"])
        field_names = [field["fieldName"] for field in self.epl_model_point_fields]
        if settings.epl_liability_id_field not in field_names:
            errors.append(f"EPL model point file '{settings.epl_mpf_path}' does not exist yet and product "
                          f"'{settings.epl_product_name}' has no model point field '{settings.epl_liability_id_field}' to "
                          f"create it with (settings.epl_liability_id_field). It has fields {sorted(field_names)}")

        return errors

    # Returns True if any portfolio on the projection includes the EPL product
    def __projection_uses_epl(self, projection_id: int) -> bool:
        return any(product["productId"] == self.epl_product["id"]
                   for portfolio in self.api.get_projection_portfolios(projection_id)
                   for product in self.api.get_portfolio_products(portfolio["id"]))

    # Returns the File ID of the EPL model point file if it already exists in the SLOPE File Manager, otherwise None
    def __find_epl_model_point_file(self) -> int | None:
        folder, _, filename = settings.epl_mpf_path.rpartition("/")
        return next((file["fileId"] for file in self.api.list_files([folder])
                     if file["fileKey"] in (settings.epl_mpf_path, filename)), None)

    # Returns the initial guess for a time point. initial_guesses is either a single value used for every time point,
    # or a dict of values keyed by time point. Returns None if no guess was provided, so the solver derives one
    @staticmethod
    def __get_initial_guess(initial_guesses: float | dict[int, float] | None, time_index: int) -> float | None:
        if isinstance(initial_guesses, dict):
            return initial_guesses.get(time_index)
        return initial_guesses

    def calculate_bel_at_zero(self, initial_guess: float | None = None):
        logging.info(f"Starting SBA Solver for Time 0")

        # Get Liability Cash Flows and Market Values from Base projection
        logging.info(f"Getting Liability Cash Flows from base Projection ID {self.base_projection_id}")

        base_report_params = {"Projection-ID": f"{self.base_projection_id}",
                              "Scenario-ID": '1'}

        self.__create_liability_cash_flows(base_report_params)

        solve_params = SbaSolver.TimeSolveParams(0, use_epl=False, generate_asset_files=False, generate_scenario_file=False,
                                                 initial_guess=initial_guess)
        result = self.__solve_at_time(solve_params)
 
        self.final_bel.append({"Time": 0, "BEL": result["bel"], "ProjectionId": result["projectionId"],
                               "Scenario": result["scenario"]})

    # initial_guesses is optional: a single starting-asset guess used for every time point, or a dict of guesses keyed by
    # time point. Any time point without a guess derives its own from the market value of liabilities
    def calculate_bel(self, time_indexes:list[int], initial_guesses: float | dict[int, float] | None = None):
        if len(time_indexes) == 0:
            logging.info("No time indexes specified for BEL solver routine.")
            return []

        logging.info(f"Starting SBA Solver for the following time points:")
        logging.info(time_indexes)

        # Get Liability Cash Flows and Market Values from Base projection
        logging.info(f"Getting Liability Cash Flows from base Projection ID {self.base_projection_id}")

        base_report_params = {"Projection-ID": f"{self.base_projection_id}",
                              "Scenario-ID": '1'}

        self.__create_liability_cash_flows(base_report_params)

        for time_idx in time_indexes:
            solve_params = SbaSolver.TimeSolveParams(time_idx, initial_guess=self.__get_initial_guess(initial_guesses, time_idx))
            result = self.__solve_at_time(solve_params)
            self.final_bel.append({"Time": time_idx, "BEL": result["bel"], "ProjectionId": result["projectionId"], "Scenario": result["scenario"]})

        # except Exception as e:
        #    logging.info(e)
        #    logging.info(traceback.format_exc())
        #    logging.info("SBA Solver Failed - BEL could not be calculated")

    def print_results(self):
        print("Results:")
        for result in self.final_bel:
            print(f"Time {result['Time']}: Projection({result['ProjectionId']}) Scenario({result['Scenario']}) BEL: {result['BEL']}")

    def __create_asset_mpfs(self, time_index, report_params):
        products_report = SigmaReport(self.api, self.reports["Asset Products"])
        products_report.retrieve(report_params)
        products = products_report.get_data()
        self.__asset_market_value = products['Market Value at Pivot Time'].sum()

        if time_index == 0:
            # For time 0, we can just use the existing asset MPFs from the base projection
            base_products = self.__base_projection.get("portfolios")[0].get("products")

            # Create a list of asset objects from the base projection products
            assets = []
            for product in base_products:
                if product.get("productType") == "Asset":
                    assets.append({"productName": product.get("name"),
                               "modelPointFile": product.get("modelPointFile")})

            return assets

        assets = []
        report = SigmaReport(self.api, self.reports["Asset MPF"])
        for product in products['Product Name']:
            report_params["Product-Name"] = product

            # Download MPF as of the point in time
            logging.info(f"Creating Asset MPF for {product}")
            report.retrieve(report_params)
            
            mpf_file_name = product + "_" + str(time_index) + ".csv"

            # Save it to file manager
            file_id = self.api.upload_file(report.get_filename(), f"{self.slope_file_path}/{mpf_file_name}")
            assets.append({"productName": product,
                           "modelPointFile": {"fileId": file_id}})

        return assets

    def __create_liability_cash_flows(self, report_params):
        # Get Liability cash flows from SLOPE
        report = SigmaReport(self.api, self.reports["Liability Cash Flows"])
        report.retrieve(report_params)
        epl_data = report.get_data()

        # Check the report returned the expected columns - any other column would be silently dropped on upload
        if sorted(epl_data.columns) != sorted(SbaSolver.EPL_REPORT_COLUMNS):
            raise ValueError(f"Liability Cash Flows report returned columns {list(epl_data.columns)}. Expected {SbaSolver.EPL_REPORT_COLUMNS}")

        # Find time of last cash flow - we must explicitly convert this from a numpy.int64 type to an int here, otherwise it will fail to be serialized in json later
        self.__last_cf_time = int(epl_data["Time Index"].max())

        # The base projection's own EPL Inputs table is used by the solver runs, so no new table is needed
        if self.base_uses_epl:
            logging.info(f"Base projection already uses '{settings.epl_product_name}' - keeping its EPL Inputs table")
            return

        # Add an empty data row at the bottom to catch any missing months if the cash flows are spare and populate with 0 cash flows
        epl_data.loc[epl_data.index.max() + 1] = ["", "", 0]

        # Add required index columns. Columns written here must match SbaSolver.EPL_COLUMNS
        epl_data["Liability ID"] = "BEL"
        epl_data["Scenario Number"] = ""
        epl_data = epl_data.drop(columns=["Time Index"])

        # Add additional columns required by the table structure
        epl_table_columns = self.api.get_table_structure_columns(self.epl_table_id)
        for column in epl_table_columns:
            column_name = column["name"]
            if column_name not in epl_data.columns:
                epl_data[column_name] = 0

        # Send the EPL cash flows to SLOPE directly as row data
        logging.info("Create EPL table for liability cash flows for BEL runs")
        rows = SlopeApi.dataframe_to_rows(epl_data, epl_table_columns)
        self.liability_cashflows_table_id = self.api.upsert_data_table_rows(
            self.model_id, self.epl_table_id, settings.epl_table_name, f"{self.base_projection_id} Cash Flows", rows)

    # Set up a copy of the base projection as the first EPL-based solver run. This replaces a manually maintained
    # projection template: the solver run settings are applied, the solver table overrides attached, and its liability
    # products replaced by a single Externally Projected Liability product. Every later solver run is copied from it
    def __setup_first_solver_projection(self, projection_id: int):
        logging.info(f"Setting up projection {projection_id} as the first SBA solver projection")
        data_tables = self.__create_solver_override_tables()
        if not self.base_uses_epl:
            data_tables.append({"tableStructureName": settings.epl_table_name,
                                "dataTableId": self.liability_cashflows_table_id})
        self.api.update_projection(projection_id, {
            "virtualFolders": [settings.virtual_folder_name],
            # Remove any scenario filter on the base projection so every guess runs all scenarios
            "scenarioSubset": "",
            "dataTables": data_tables
        })

        self.__replace_liabilities_with_epl(projection_id)
        self.first_solver_projection_id = projection_id

    # For each table in settings.solver_table_overrides, returns the projection dataTables entry of a table with the
    # requested column values on every row. Tables in settings.shared_solver_tables are created once and reused across
    # every solve. Other tables are copied from the base projection's table and saved per base projection
    def __create_solver_override_tables(self) -> list[dict]:
        base_tables = {table["tableStructureName"]: table for table in self.__base_projection.get("dataTables", [])}
        entries = []
        for structure_name, overrides in settings.solver_table_overrides.items():
            shared_name = settings.shared_solver_tables.get(structure_name)
            shared_table_id = self.__shared_table_ids.get(structure_name)
            if shared_table_id is not None:
                table_id = self.__check_shared_solver_table(structure_name, shared_name, shared_table_id, overrides)
                entries.append({"tableStructureName": structure_name, "dataTableId": table_id})
                continue

            base_table = base_tables.get(structure_name)
            if base_table is None or base_table.get("dataTableId") is None:
                raise ValueError(f"Base projection {self.base_projection_id} has no data table set for table structure '{structure_name}'")

            table = self.api.get_data_table_raw(base_table["dataTableId"])
            rows, _ = self.__apply_overrides(structure_name, table, overrides)

            table_name = shared_name or f"{self.base_projection_id} SBA {structure_name}"
            logging.info(f"Create '{table_name}' table for solver runs with overrides: {overrides}")
            table_id = self.api.upsert_data_table_rows(self.model_id, base_table["tableStructureId"], structure_name,
                                                       table_name, rows)
            if shared_name:
                self.__shared_table_ids[structure_name] = table_id
            entries.append({"tableStructureName": structure_name, "dataTableId": table_id})

        return entries

    # Check the overridden column values on an existing shared solver table. It is used as is if they are all set
    # correctly, otherwise its rows are updated with the correct values. Returns the Data Table ID to use
    def __check_shared_solver_table(self, structure_name: str, table_name: str, table_id: int, overrides: dict) -> int:
        table = self.api.get_data_table_raw(table_id)
        rows, changed = self.__apply_overrides(structure_name, table, overrides)
        if not changed:
            logging.info(f"Using existing '{table_name}' table for solver runs")
            return table_id

        logging.warning(f"Existing '{table_name}' table does not have the expected values {overrides} - updating it")
        table_id = self.api.update_data_table_rows(table_id, rows)
        self.__shared_table_ids[structure_name] = table_id
        return table_id

    # Returns a copy of the raw table rows with the override values set on every row, and whether any value changed
    @staticmethod
    def __apply_overrides(structure_name: str, table: dict, overrides: dict) -> tuple[list[list[str]], bool]:
        # Find the position of each column in the row data - MortalityTable columns take up 2 positions
        positions = {}
        position = 0
        for column in sorted(table["columns"], key=lambda col: col["order"]):
            positions[column["name"]] = position
            position += 2 if column["dataType"] == "MortalityTable" else 1

        missing = [name for name in overrides if name not in positions]
        if missing:
            raise ValueError(f"Table structure '{structure_name}' does not have column(s) {missing}. Check settings.solver_table_overrides")

        rows = [list(row) for row in table["rows"]]
        changed = False
        for row in rows:
            for column_name, value in overrides.items():
                if not SbaSolver.__same_value(row[positions[column_name]], value):
                    row[positions[column_name]] = value
                    changed = True
        return rows, changed

    # Compare table values as SLOPE may return them, e.g. "0.0" for "0" or "False" for "false"
    @staticmethod
    def __same_value(actual, expected) -> bool:
        actual, expected = str(actual if actual is not None else "").strip(), str(expected).strip()
        if actual.lower() == expected.lower():
            return True
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False

    # Remove all liability products from the projection and replace them with a single Externally Projected Liability
    # product. If the projection already uses the EPL product, it is kept as is
    def __replace_liabilities_with_epl(self, projection_id: int):
        epl_product = self.epl_product
        portfolios = self.api.get_projection_portfolios(projection_id)
        epl_portfolio_id = None
        has_epl = False
        for portfolio in portfolios:
            for product in self.api.get_portfolio_products(portfolio["id"]):
                if product["type"] != "Liability":
                    continue
                if epl_portfolio_id is None:
                    epl_portfolio_id = portfolio["id"]
                if product["productId"] == epl_product["id"] and not has_epl:
                    has_epl = True
                    continue
                logging.info(f"Removing liability product '{product['name']}' from solver projection")
                self.api.remove_product_from_portfolio(product["id"])

        if has_epl:
            logging.info(f"Base projection already uses '{settings.epl_product_name}' - keeping it")
            return

        logging.info(f"Adding '{settings.epl_product_name}' product to solver projection")
        self.api.add_product_to_portfolio(epl_portfolio_id or portfolios[0]["id"], epl_product["id"],
                                          settings.epl_product_name, self.__get_epl_model_point_file())

    # Returns the file reference for the EPL model point file. The file is the same for every run, so reuse it if it
    # already exists in the SLOPE File Manager (found by the setup check), otherwise create and upload it
    def __get_epl_model_point_file(self) -> dict:
        if self.epl_mpf_file_id is not None:
            return {"fileId": self.epl_mpf_file_id}

        logging.info(f"Creating EPL model point file '{settings.epl_mpf_path}'")
        # A single model point that links to the "BEL" rows of the EPL Inputs table. Date fields (e.g. Issue Date) are
        # set to the base projection's start date, and other required fields use their default value
        start_date = parse(self.__base_projection["startDate"]).strftime("%m/%d/%Y")
        model_point = {}
        for field in self.epl_model_point_fields:
            if field["fieldName"] == settings.epl_liability_id_field:
                model_point[field["fieldName"]] = "BEL"
            elif field["dataType"] == "Date":
                model_point[field["fieldName"]] = start_date
            elif field["isRequired"]:
                model_point[field["fieldName"]] = field.get("defaultValue") or ""
                if not model_point[field["fieldName"]]:
                    logging.warning(f"Required EPL model point field '{field['fieldName']}' has no default value - left blank")

        mpf_file = self.solver_folder / settings.epl_mpf_path.rpartition("/")[2]
        pd.DataFrame([model_point]).to_csv(mpf_file, index=False)
        self.epl_mpf_file_id = self.api.upload_file(str(mpf_file), settings.epl_mpf_path)
        return {"fileId": self.epl_mpf_file_id}

    def __create_scenario_file(self, time_index, valuation_date, report_params):
        logging.info(f"Get starting spot curve at time {time_index}")
        # Get Scenario Spot Rates as of the Pivot Point
        scenario_report = SigmaReport(self.api, self.reports["Scenario Data"])
        scenario_report.retrieve(report_params)
        spot_curve = scenario_report.get_data()

        scenario_file = self.solver_folder / f"sba_scenarios_time_{time_index}.xlsx"
        logging.info(f"Creating new SBA scenario file at '{scenario_file}'")
        scenario_generator = openpyxl.load_workbook(filename=settings.sba_scenario_generator, read_only=False)
        sheet = scenario_generator["Input"]

        # Write pivot point scenario values to scenario generator to create SBA scenarios
        rows = dataframe_to_rows(spot_curve, index=False, header=False)
        for r_idx, row in enumerate(rows, 1):
            for c_idx, df_value in enumerate(row, 1):
                # if c_idx > 1:
                sheet.cell(row=r_idx + 4, column=c_idx + 1, value=df_value)

        # Sandboxed Excel on macOS shows a 'Grant File Access' prompt for every new file outside its container,
        # so on macOS the workbook is recalculated inside Excel's container and then moved to the solver folder
        excel_file = scenario_file
        excel_container = Path.home() / "Library" / "Containers" / "com.microsoft.Excel" / "Data"
        if sys.platform == "darwin" and excel_container.is_dir():
            excel_file = excel_container / "SBA Solver" / scenario_file.name
            excel_file.parent.mkdir(parents=True, exist_ok=True)

        scenario_generator.save(excel_file)
        scenario_generator.close()

        # openpyxl is great for updating data, but it doesn't recalculate formulas, which is kind of important for this step
        # Open, recalculate, save, and close the workbook using Excel so it is re-saved with formulas updated
        # xlwings drives the installed MS Excel on both macOS (AppleScript) and Windows (COM), so this is cross-platform
        # Requires MS Excel to be installed. If Excel hangs, it is usually a stuck open dialog -- close all Excel windows and rerun
        excel = xw.App(visible=False)
        try:
            # xlwings (appscript on macOS) needs an absolute path string
            workbook = excel.books.open(str(excel_file.resolve()))
            excel.calculate()
            workbook.save()
            workbook.close()
        finally:
            excel.quit()

        if excel_file != scenario_file:
            shutil.move(excel_file, scenario_file)

        # Upload SBA Scenarios to SLOPE
        logging.info(f"Load scenario file to slope at '{self.slope_file_path}/Time-{time_index} SBA Scenarios.xlsx'")
        scenario_params = {"modelId": self.model_id,
                           "name": f"Proj-{self.base_projection_id}-Time-{time_index}-SBA",
                           "startDate": valuation_date.isoformat(),
                           "yieldCurveRateType": "SpotRate",
                           "filePath": f"{self.slope_file_path}/Time-{time_index} SBA Scenarios.xlsx",
                           "excelSheetName": "Scenarios"
                           }
        scenario_table_id = self.api.create_or_update_scenario_table(scenario_file, scenario_params)
        return scenario_table_id

    def __get_solver_results(self, projection_id, guesses) -> tuple[int, float]:
        # Wait for projection to finish
        while self.api.is_projection_running(projection_id):
            status = self.api.get_projection_status(projection_id)
            logging.info(f"Waiting for Projection ID {projection_id} to finish. Current status: {status}")
            time.sleep(30)  # Check once every 30 seconds if it is done

        # Download Results
        report = SigmaReport(self.api, self.reports["Solver Results"])
        report.retrieve({"Projection-ID": f"{projection_id}"})
        sba_result = report.get_data()

        if len(sba_result) < 9:
            # Data is not loaded to Snowflake yet, so wait and try again up to 5 times
            for attempt in range(5):
                time.sleep(20)  # Wait 20 seconds before retrying
                report.retrieve({"Projection-ID": f"{projection_id}"})
                sba_result =report.get_data()
                if (len(sba_result) >= 9):
                    break

        result = {
            "bel": 0,
            "tolerance": 0,
            "projectionId": projection_id,
            "scenario": 0,
        }
        # Save results in array for solver
        for idx, row in sba_result.iterrows():
            scenario = int(row['Scenario Number'])
            start = row['Starting Assets']
            end = row['Ending Assets']

            if abs(end) > result["tolerance"]:
                result["tolerance"] = abs(end)

            if start > result["bel"]:
                result["bel"] = start
                result["scenario"] = scenario

            if scenario not in guesses:
                guesses[scenario] = []
            guesses[scenario].append({"value": start, "result": end})

        return result


    def __solve_at_time(self, params: TimeSolveParams) -> dict:
        if params.time_index > self.__last_cf_time:
            logging.info("Time is after last liability cash flow time. BEL is 0.")
            return {"bel": 0,
                    "tolerance": 0,
                    "projectionId": 0,
                    "scenario": 0
                    }

        logging.info(f"Solving for BEL at time {params.time_index}:")

        pivot_point_report_params = {"Projection-ID": f"{self.base_projection_id}",
                                     "Scenario-ID": '1',
                                     "Pivot-Time-Index": f"{params.time_index}"}

        # Get Market Value of Liabilities at this pivot point
        logging.info(f"Get starting market value liabilities at time {params.time_index}.")
        report = SigmaReport(self.api, self.reports["Market Value Liability"])
        report.retrieve(pivot_point_report_params)
        mvl_data = report.get_data()
        valuation_date = datetime.datetime.fromisoformat(mvl_data["Date"].iloc[0])

        market_value_liabilities = mvl_data["Market Value"].iloc[0]
        logging.info(f"Valuation Date: {valuation_date}")
        logging.info(f"Market Value Liabilities: {market_value_liabilities}")

        # Projection Parameters expected below
        solver_projection_parameters = {
            "startDate": valuation_date.isoformat(),
            "periodInMonths": self.__last_cf_time - params.time_index + 1,
            "virtualFolders": [settings.virtual_folder_name]
        }

        # Runs without EPL are copied straight from the base projection, so remove any scenario filter on each of them.
        # EPL runs are copied from the first solver projection, which already has it removed
        if not params.use_epl:
            solver_projection_parameters["scenarioSubset"] = ""

        # When the base projection already uses EPL, its EPL Inputs table is kept
        if params.use_epl and not self.base_uses_epl:
            solver_projection_parameters["dataTables"] = [{
                "tableStructureName": settings.epl_table_name,
                "dataTableId": self.liability_cashflows_table_id
            }]

        if params.generate_scenario_file:
            # Create Scenario File for this pivot point
            logging.info(f"Create Scenario file at time {params.time_index}")
            solver_projection_parameters["scenarioTableId"] = self.__create_scenario_file(params.time_index, valuation_date, pivot_point_report_params)

        if params.generate_asset_files:
            # Create Asset MPFs at Pivot Time Index
            logging.info(f"Get starting asset model points at time {params.time_index}")
            assets = self.__create_asset_mpfs(params.time_index, pivot_point_report_params)
            solver_projection_parameters["portfolios"] = [{
                "portfolioName": self.portfolio_name,
                "products": assets
            }]

        # Start Initial Guesses for solver - use the initial guess if one was provided, otherwise derive it
        if params.initial_guess is not None:
            starting_guess = params.initial_guess
            logging.info(f"Using provided initial guess of {starting_guess:,.2f}")
        else:
            starting_guess = market_value_liabilities if market_value_liabilities > 0 else self.__asset_market_value
            if starting_guess <= 0:
                starting_guess = 10000000  # If both MVL and Asset MV are 0 or negative, just start at 10 million
            logging.info(f"Using derived initial guess of {starting_guess:,.2f}")
        solver_projections = []
        guess_num = 1

        # Low - 90% of starting guess
        starting_assets = [starting_guess * 0.99] * 10
        solver_projections.append(self.__start_run(starting_assets, params.time_index, solver_projection_parameters, guess_num, params.use_epl))
        guess_num += 1
        # Mid - 99.5% of starting guess
        starting_assets = [starting_guess * 0.995] * 10
        solver_projections.append(self.__start_run(starting_assets, params.time_index, solver_projection_parameters, guess_num, params.use_epl))
        guess_num += 1
        # High - 110% of MVL
        starting_assets = [starting_guess * 1.01] * 10
        solver_projections.append(self.__start_run(starting_assets, params.time_index, solver_projection_parameters, guess_num, params.use_epl))
        guess_num += 1

        # Iteration
        best_guess = {
            "bel": market_value_liabilities,
            "tolerance": market_value_liabilities,
            "projectionId": 0,
            "scenario": 0
        }

        guesses = {}
        for i in range(1, 9):
            guesses[i] = []

        for i in range(self.__max_iterations):
            for projection_id in solver_projections:
                this_guess = self.__get_solver_results(projection_id, guesses)
                # if within tolerance, stop here
                if this_guess["tolerance"] <= self.__tolerance:
                    return this_guess
                if this_guess["tolerance"] < best_guess["tolerance"]:
                    best_guess = this_guess


            # if outside tolerance and not at max iterations, create next set of guesses and iterate again
            if i < self.__max_iterations - 1:
                solver_projections = []
                for guess in self.__solve_next_guess(guesses):
                    solver_projections.append(self.__start_run(guess, params.time_index, solver_projection_parameters, guess_num, params.use_epl))
                    guess_num += 1

        self.__calculate_max_error(guesses, best_guess['scenario'])
        logging.info(f"No projection within tolerance after {self.__max_iterations}.")
        logging.info(f"Best guess of {best_guess['bel']} with final difference of {best_guess['tolerance']}.")
        logging.info(f"Maximum Potential Error in BEL: {self.max_error}")
        return best_guess

    def __calculate_max_error(self, prior_results: dict[int, float], scenario: int):
        guesses = prior_results[scenario]

        # Find the 2 closest points to 0
        guesses.sort(key=lambda x: x['result'])
        index_low = next((i for i, x in reversed(list(enumerate(guesses))) if x['result'] < 0), None)
        index_high = next((i for i, x in enumerate(guesses) if x['result'] >= 0), None)
        

        if index_low is None or index_high is None:
            self.max_error = "Unknown"
            return
        
        self.max_error = str(abs(guesses[index_high]['value'] - guesses[index_low]['value'])/2)

    def __start_run(self, starting_assets: list[float], time_index, projection_params, guess_num: int, use_epl: bool) -> int:
        # Write new starting asset values to a table
        df = pd.DataFrame(starting_assets)
        # Columns written here must match SbaSolver.STARTING_ASSET_COLUMNS
        df = df.rename(columns={df.columns[0]: "Scaling Target"})
        df['Scenario #'] = range(len(df))
        df['Scaling Factor'] = None
        logging.info(f"Starting run for BEL solve:")
        logging.info(df)

        # Send starting asset values to SLOPE directly as row data
        starting_assets_table_id = self.api.upsert_data_table_rows(
            self.model_id, self.starting_assets_table_id, settings.starting_asset_table_name,
            f"{self.base_projection_id}-{time_index} SBA Solver Guess {guess_num}",
            SlopeApi.dataframe_to_rows(df, self.starting_assets_table_columns))

        # Create a new projection from the requested source
        projection_name = f"SBA Solver Projection-{self.base_projection_id} Time-{time_index} Guess {guess_num}"
        if use_epl and self.first_solver_projection_id is not None:
            # Copy the first solver projection, which already has the EPL product and solver tables set up
            logging.info(f"Creating new copy of first solver projection {self.first_solver_projection_id}")
            projection_id = self.api.copy_projection(self.first_solver_projection_id, projection_name, False)
        else:
            logging.info(f"Creating new copy of projection {self.base_projection_id}")
            projection_id = self.api.copy_projection(self.base_projection_id, projection_name, False)
            if use_epl:
                self.__setup_first_solver_projection(projection_id)

        self.api.update_projection(projection_id, projection_params)

        # Set starting assets table
        self.api.update_projection_table(projection_id, settings.starting_asset_table_name, starting_assets_table_id)

        # Start the projection
        logging.info(f"Starting projection ID {projection_id}")
        self.api.run_projection(projection_id)
        return projection_id

    def __solve_next_guess(self, prior_results: dict[int, float]):
        max_scenario = max(prior_results.keys())
        low = [0] * (max_scenario+1)
        mid = [0] * (max_scenario+1)
        high = [0] * (max_scenario+1)
        for scenario, guesses in prior_results.items():
            size = len(guesses)
            # Perform an iterative solve from the closest two points for each scenario
            if len(guesses) < 2:
                print("Must have at least 2 values to perform interpolation")
                return 0

            # Find the 2 closest points to 0
            guesses.sort(key=lambda x: x['result'])
            index_low = next((i for i, x in reversed(list(enumerate(guesses))) if x['result'] < 0), None)
            index_high = next((i for i, x in enumerate(guesses) if x['result'] >= 0), None)

            if index_low is None:
                # There are no points <0, so find the next lowest positive point closest to 0
                index_low = index_high
                index_high = next((i for i, x in enumerate(guesses) if x['result'] > guesses[index_low]['result']), None)
            elif index_high is None:
                # There are not any points >0, so find the next highest point negative value closest to 0
                index_high = index_low
                index_low = next((i for i, x in reversed(list(enumerate(guesses))) if x['result'] < guesses[index_high]['result']), None)

            # Secant method to calculate best guess of starting asset that will result in 0 at end
            solved_guess = guesses[index_high]['value'] \
                - guesses[index_high]['result'] * (guesses[index_high]['value'] - guesses[index_low]['value']) \
                / (guesses[index_high]['result'] - guesses[index_low]['result'])

            # Calculate a range of guesses around the best guess to try using a weighted average within the interval
            low[scenario] = solved_guess * (1 - settings.next_guess_range) + guesses[index_low]['value'] * settings.next_guess_range
            mid[scenario] = solved_guess
            high[scenario] = solved_guess * (1 - settings.next_guess_range) + guesses[index_high]['value'] * settings.next_guess_range

        # Returns the best guess plus a range around that best guess within the interval
        return [low, mid, high]


