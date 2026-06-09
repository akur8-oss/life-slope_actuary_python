import datetime
from dataclasses import dataclass
import pandas as pd
import logging
import openpyxl
from openpyxl.utils.dataframe import dataframe_to_rows
import settings
from Shared.sigma_report import SigmaReport, SigmaReportParams
from Shared.slope_api import SlopeApi
import time
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

    @dataclass
    class TimeSolveParams:
        time_index: int
        use_epl: bool = True
        generate_asset_files: bool = True
        generate_scenario_file: bool = True

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

        # Get the Projection Template ID for the SBA Solver
        projection_templates = self.api.list_projection_templates(self.model_id)
        self.epl_projection_template_id = next((template.get("id") for template in projection_templates if template.get("name") == settings.sba_template_name), None)

    def calculate_bel_at_zero(self):
        logging.info(f"Starting SBA Solver for Time 0")

        # Get Liability Cash Flows and Market Values from Base projection
        logging.info(f"Getting Liability Cash Flows from base Projection ID {self.base_projection_id}")

        base_report_params = {"Projection-ID": f"{self.base_projection_id}",
                              "Scenario-ID": '1'}

        self.__create_liability_cash_flows(base_report_params)

        solve_params = SbaSolver.TimeSolveParams(0, use_epl=False, generate_asset_files=False, generate_scenario_file=False)
        result = self.__solve_at_time(solve_params)
 
        self.final_bel.append({"Time": 0, "BEL": result["bel"], "ProjectionId": result["projectionId"],
                               "Scenario": result["scenario"]})

    def calculate_bel(self, time_indexes:list[int]):
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
            solve_params = SbaSolver.TimeSolveParams(time_idx)
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

        # Find time of last cash flow - we must explicitly convert this from a numpy.int64 type to an int here, otherwise it will fail to be serialized in json later
        self.__last_cf_time = int(epl_data["Time Index"].max())

        # Add an empty data row at the bottom to catch any missing months if the cash flows are spare and populate with 0 cash flows
        epl_data.loc[epl_data.index.max() + 1] = ["", "", 0]

        # Add required index columns
        epl_data["Liability ID"] = "BEL"
        epl_data["Scenario Number"] = ""

        # Add additional columns required by the table structure
        epl_table_columns = self.api.get_table_structure_columns(self.epl_table_id)
        for column in epl_table_columns:
            column_name = column["name"]
            if column_name not in epl_data.columns:
                epl_data[column_name] = 0

        # Write final updated EPL file to a csv file
        epl_data.to_csv(report.get_filename(), index=False)

        # Upload the EPL cash flows to SLOPE
        logging.info("Create EPL table for liability cash flows for BEL runs")
        self.liability_cashflows_table_id = self.api.create_or_update_data_table(report.get_filename(), {
            "tableStructureId": self.epl_table_id,
            "name": f"{self.base_projection_id} Cash Flows",
            "filePath": f"{self.slope_file_path}/Liability Cash Flows.csv",
            "delimiter": ","
        })


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

        scenario_generator.save(scenario_file)
        scenario_generator.close()

        # openpyxl is great for updating data, but it doesn't recalculate formulas, which is kind of important for this step
        # Open, recalculate, save, and close the workbook using Excel so it is re-saved with formulas updated
        # xlwings drives the installed MS Excel on both macOS (AppleScript) and Windows (COM), so this is cross-platform
        # Requires MS Excel to be installed. If Excel hangs, it is usually a stuck open dialog -- close all Excel windows and rerun
        excel = xw.App(visible=False)
        try:
            # xlwings (appscript on macOS) needs an absolute path string
            workbook = excel.books.open(str(scenario_file.resolve()))
            excel.calculate()
            workbook.save()
            workbook.close()
        finally:
            excel.quit()

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
            "outputAllResults": False,
            "dataTables": [{
                "tableStructureName": "EPL Inputs",
                "dataTableId": self.liability_cashflows_table_id
            }],
            "virtualFolders": [settings.virtual_folder_name]
        }

        if params.use_epl:
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
                "portfolioName": "Inforce Portfolio",
                "products": assets
            }]

        # Start Initial Guesses for solver
        starting_guess = market_value_liabilities if market_value_liabilities > 0 else self.__asset_market_value
        if starting_guess <= 0:
            starting_guess = 10000000  # If both MVL and Asset MV are 0 or negative, just start at 10 million
        solver_projections = []
        guess_num = 1

        # Low - 90% of starting guess
        starting_assets = [starting_guess * 0.95] * 10
        solver_projections.append(self.__start_run(starting_assets, params.time_index, solver_projection_parameters, guess_num, params.use_epl))
        guess_num += 1
        # Mid - 99.5% of starting guess
        starting_assets = [starting_guess * 0.995] * 10
        solver_projections.append(self.__start_run(starting_assets, params.time_index, solver_projection_parameters, guess_num, params.use_epl))
        guess_num += 1
        # High - 110% of MVL
        starting_assets = [starting_guess * 1.1] * 10
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
        df = df.rename(columns={df.columns[0]: "Scaling Target"})
        df['Scenario #'] = range(len(df))
        df['Scaling Factor'] = None
        df['Asset Scaling Method'] = "Use Asset Scaling Amount/Factor"
        df['Scaling Target Basis'] = "Market Value"
        df['Portfolio Name'] = None
        starting_assets_file = self.solver_folder / "sba_assets.csv"
        df.to_csv(starting_assets_file, index=False)
        logging.info(f"Starting run for BEL solve:")
        logging.info(df)

        # Upload starting asset values to SLOPE
        table_params = {"tableStructureId": self.starting_assets_table_id,
                        "name": f"{self.base_projection_id}-{time_index} SBA Solver Guess {guess_num}",
                        "filePath": f"{self.slope_file_path}/sba_assets.csv",
                        "isFileOnly": False,
                        "delimiter": ","}
        starting_assets_table_id = self.api.create_or_update_data_table(starting_assets_file, table_params)

        # Create a new projection from the requested source
        if use_epl:
            logging.info(f"Creating projection from Template ID {self.epl_projection_template_id}")
            projection_id = self.api.create_projection_from_template(self.epl_projection_template_id, f"SBA Solver Projection-{self.base_projection_id} Time-{time_index}")
        else:
            logging.info(f"Creating new copy of projection {self.base_projection_id}")
            projection_id = self.api.copy_projection(self.base_projection_id, f"SBA Solver Projection-{self.base_projection_id} Time-{time_index}", False)

        self.api.update_projection(projection_id, projection_params)

        # Set starting assets table
        self.api.update_projection_table(projection_id, "Initial Asset Scaling", starting_assets_table_id)

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


