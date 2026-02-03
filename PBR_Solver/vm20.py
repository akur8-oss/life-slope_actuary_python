import csv
import logging
import math
import os
import time
from guess_iteration import GuessIteration
from Shared.sigma_report import SigmaReport
from Shared.slope_api import SlopeApi
from vm20_params import VM20Params, VM20RestartParams

class VM20:
    base_projection_details: dict = None
    params: VM20Params = None
    restart_params: VM20RestartParams = None

    model_id: int = None
    
    asset_collar_tolerance: float = 0.02
    slope_file_path: str
    working_directory: str

    __epl_table_id: int = None
    __epl_table_structure_id: int = None
    __pbr_projection_template_id: int = None
    __starting_assets_table_structure_id: int = None
    __solver_steps: list[dict] = None

    def __init__(self, params: VM20Params):
        self.api = SlopeApi()
        if params.api_key and params.api_secret:
            self.api.authorize(params.api_key, params.api_secret)
        
        self.params = params

    # Solves for the value of starting assets such that the assets are within 2% of the final reserve value
    # Returns a tuple of (assets, projection_id)
    def solve_asset_collar(self, sr_projection_id, restart: VM20RestartParams = None) -> tuple[float, int]:
        if restart is None:
            self.restart_params = VM20RestartParams()
        else:
            self.restart_params = restart

        try:
            # projection_id should have been run with assets = NPR as starting point
            logging.info(f"Asset Collar Solver: Starting with Projection ID ({sr_projection_id})")
            self.__get_ids(sr_projection_id)

            # Set up working directory paths locally and in SLOPE
            self.slope_file_path = f'PBR Solver/Projection-{sr_projection_id}'
            self.working_directory = f'{self.params.working_directory}\\Projection-{sr_projection_id}'
            if not os.path.exists(self.working_directory):
                os.makedirs(self.working_directory)

            # projection_id should have been run with assets = NPR as starting point
            if self.restart_params is not None and self.restart_params.starting_assets is not None:
                starting_assets = self.restart_params.starting_assets
                logging.info(f"Asset Collar Solver: Restarting with provided starting assets: {starting_assets}")
            else:
                logging.info("Asset Collar Solver: Getting starting assets.")
                starting_assets = self.__get_starting_assets(sr_projection_id)
            self.restart_params.starting_assets = starting_assets

            if self.restart_params.initial_guesses is not None:
                # Use the last guess as the starting point
                logging.info(f"Asset Collar Solver: Restarting with provided initial guesses: {self.restart_params.initial_guesses}")
                guess = self.restart_params.initial_guesses
            else:
                # Calculate Scenario Reserves for base projection
                logging.info("Asset Collar Solver: Checking Starting Run Tolerance.")
                stochastic_reserve = self.__get_stochastic_reserve(sr_projection_id, full_scenario_set=True)
                diff = stochastic_reserve - starting_assets
                if abs(diff) <= self.asset_collar_tolerance * starting_assets:
                    # Initial Run is within tolerance - we are done
                    return starting_assets, sr_projection_id
                guess = GuessIteration(
                    prior_guess=starting_assets,
                    prior_result=diff,
                    current_guess=stochastic_reserve
                )

            
            # Select Sample of Scenarios
            if self.restart_params is not None and self.restart_params.sample_scenarios is not None:
                sample_scenarios = self.restart_params.sample_scenarios
                logging.info(f"Asset Collar Solver: Restarting with provided sample scenarios: {sample_scenarios}")
            else:
                # Identify Worst 30% Scenarios
                logging.info("Asset Collar Solver: Identifying CTE(70) scenarios.")
                cte_scenarios = self.__get_cte_scenarios(sr_projection_id)

                logging.info("Asset Collar Solver: Selecting sample scenarios.")
                sample_scenarios = self.__get_sample_scenarios(cte_scenarios)
            self.restart_params.sample_scenarios = sample_scenarios

            # Get Liability Cash Flows for Sample Scenarios
            if self.restart_params is not None and self.restart_params.epl_table_id is not None:
                self.__epl_table_id = self.restart_params.epl_table_id
                logging.info(f"Asset Collar Solver: Restarting with provided EPL table ID: {self.__epl_table_id}")
            else:
                logging.info("Asset Collar Solver: Getting EPL Cash Flows for sample scenarios.")
                self.__epl_table_id = self.__get_liability_cashflows(sr_projection_id, sample_scenarios)
            self.restart_params.epl_table_id = self.__epl_table_id  

            # Solve for starting Assets on Sample Scenarios
            solver_assets = self.__solve_starting_assets(sr_projection_id, guess, sample_scenarios)

            # Run Stochastic Again and check
            logging.info("Asset Collar Solver: Running Full Stochastic Scenario Set with original liabilities to verify tolerance.")
            stochastic_projection_id = self.__run_stochastic_set(sr_projection_id, f"Projection {sr_projection_id} VM-20 Solver Final", solver_assets)
            self.api.wait_for_completion(stochastic_projection_id)
            stochastic_reserve = self.__get_stochastic_reserve(stochastic_projection_id, full_scenario_set=True)
            diff = stochastic_reserve - solver_assets
            diff_pct = "{:.2%}".format(diff/stochastic_reserve)
            self.__solver_steps.append({"Iteration": "Final Full Stochastic Run",
                                        "Guess": solver_assets,
                                        "Difference": diff,
                                        "DifferencePct": diff_pct})
            if abs(diff) <= self.asset_collar_tolerance * solver_assets:
                logging.info(f"Asset Collar Solver: Tolerance met with full stochastic set.")
            else:
                logging.info(f"Asset Collar Solver: Tolerance not met with full rerun.")

            for step in self.__solver_steps:
                logging.info(f"Iteration: {step['Iteration']}, Guess: {step['Guess']:,.2f}, Difference: {step['Difference']:,.2f}, DifferencePct: {step['DifferencePct']}")

            return solver_assets, stochastic_projection_id
            
        except Exception as e:
            logging.error(f"Error in Asset Collar Solver: {e}")
            logging.info("Restart parameters:")
            logging.info(f"starting_assets={self.restart_params.starting_assets or 'None'}")
            logging.info(f"sample_scenarios={self.restart_params.sample_scenarios or 'None'}")
            logging.info(f"epl_table_id={self.restart_params.epl_table_id or 'None'}")
            if self.restart_params.initial_guesses is None:
                logging.info("initial_guesses=None")
            else:
                logging.info(f"initial_guesses={self.restart_params.initial_guesses}")
            raise
    
    def __create_starting_asset_table(self, starting_assets, guess_num: int):
        
        filename = f"{self.working_directory}\\Starting_Assets.csv"
        projection_id = self.base_projection_details['id']

        # Create a data table for the starting asset value
        logging.debug(f"Creating Starting Assets table for guess number {guess_num} with value {starting_assets}")
        # This assumes the layout of the Initial Asset Scaling table has not been changed
        data = [
            ["Portfolio Name","Scenario #", "asset Scaling Method", "Scaling Factor", "Scaling Target Basis", "Scaling Target"],
            ['', '', 'Use Asset Scaling Amount/Factor', '', 'US STAT Reported Value', starting_assets]
        ]

        with open(filename, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerows(data)

        # Upload starting asset values to SLOPE
        table_params = {"tableStructureId": self.__starting_assets_table_structure_id,
                        "name": f"Projection {projection_id} VM-20 Solver",
                        "filePath": f"{self.slope_file_path}/Starting_Assets.csv",
                        "isFileOnly": False,
                        "delimiter": ","}
        starting_assets_table_id = self.api.create_or_update_data_table(filename, table_params)

        return starting_assets_table_id
    
    def __get_cte_scenarios(self, projection_id: int) -> list[int]:
        # Download SR by Scenario
        report = SigmaReport(self.api, self.params.reports.get("Scenario Reserves"))
        report.retrieve({"Projection-ID": str(projection_id)})
        
        scenario_values = report.get_data()
        if scenario_values.empty:
            raise ValueError("No scenarios found in the report.")
        
        # Sort in descending order by SR value
        scenario_values.sort_values(by='Scenario Reserve', ascending=False, inplace=True)

        # Get Worst 30% Scenarios
        num_scenarios = len(scenario_values)
        num_worst_scenarios = max(self.params.min_scenarios, math.ceil(num_scenarios * 0.30))
        worst_scenarios = scenario_values.head(num_worst_scenarios)['Scenario Number'].tolist()
        return worst_scenarios

    def __get_ids(self, projection_id: int) -> None:
        self.base_projection_details = self.api.get_projection_details(projection_id)
        self.model_id = self.base_projection_details.get('model').get('id')

        templates = self.api.list_projection_templates(self.model_id)
        self.__pbr_projection_template_id = next((template['id'] for template in templates if template['name'] == self.params.pbr_projection_template_name), None)

        table_structures = self.api.list_table_structures(self.model_id)
        self.__epl_table_structure_id = next((table['id'] for table in table_structures if table['name'] == self.params.epl_table_structure_name), None)
        self.__starting_assets_table_structure_id = next((table['id'] for table in table_structures if table['name'] == self.params.starting_assets_table_structure_name), None)  

    def __get_liability_cashflows(self, projection_id: int, sample_scenarios: list[int]) -> str:
        report = SigmaReport(self.api, self.params.reports.get("Liability Cash Flows"))
        report.retrieve({"Projection-ID": str(projection_id), "Scenario": ",".join(map(str, sample_scenarios))})

        # Read cash flows into a dataframe
        epl_data = report.get_data()

        epl_table_columns = self.api.get_table_structure_columns(self.__epl_table_structure_id)

        # Add an empty data row at the bottom to catch any missing months if the cash flows are sparse and populate with 0 cash flows
        # Note; This assumes there are 3 index columns in the EPL table structure - 
        # In the futre we should be smarter and check the number of index columns and add the correct number of empty columns
        index_columns = 3
        epl_data.loc[epl_data.index.max() + 1] = [""] * index_columns + [0] * (len(epl_data.columns)-index_columns)

        # Add required index columns
        epl_data["Liability ID"] = "PBR"

        # Add additional columns required by the table structure
        
        for column in epl_table_columns:
            column_name = column["name"]
            if column_name not in epl_data.columns:
                epl_data[column_name] = 0

        # Write final updated EPL file to a csv file
        epl_data.to_csv(report.get_filename(), index=False)

        # Upload the EPL cash flows to SLOPE
        logging.info("Create EPL table for liability cash flows for VM-20 Solver runs")
        epl_table_id = self.api.create_or_update_data_table(report.get_filename(), {
            "tableStructureId": self.__epl_table_structure_id,
            "name": f"{projection_id} Cash Flows",
            "filePath": f"{self.slope_file_path}/Liability Cash Flows.csv",
            "delimiter": ","
        })

        return epl_table_id
 
    def __get_starting_assets(self, projection_id: int) -> float:
        report = SigmaReport(self.api, self.params.reports.get("Starting Assets"))
        report.retrieve({"Projection-ID": str(projection_id)})
        starting_assets_df = report.get_data()

        if starting_assets_df.empty:
            # Snowflake may have not yet loaded with final results, wait and retry a few times to see
            for attempt in range(5):
                time.sleep(10)  # Wait 10 seconds before retrying
                report.retrieve({"Projection-ID": str(projection_id)})
                starting_assets_df = report.get_data()
                if not starting_assets_df.empty:
                    break

        if starting_assets_df.empty:
            raise ValueError("No starting assets found in the report.")

        return starting_assets_df['Starting Assets'].iloc[0]

    def __get_sample_scenarios(self, scenarios: list[int]) -> list[int]:
        # Calculate sample size
        num_scenarios = len(scenarios)
        num_samples = max(self.params.min_scenarios, math.ceil(num_scenarios * self.params.scenario_sample_size))

        # Select samples evenly spread across the sorted list of scenarios
        if num_samples >= num_scenarios:
            return scenarios
        sample_indices = [i * (num_scenarios - 1)// (num_samples-1) for i in range(num_samples)]
        sample_scenarios = [scenarios[i] for i in sample_indices]

        return sample_scenarios

    def __get_stochastic_reserve(self, projection_id: int, full_scenario_set: bool) -> float:
        # Download Scenario Reserves report
        report = SigmaReport(self.api, self.params.reports.get("Scenario Reserves"))
        report.retrieve({"Projection-ID": str(projection_id)})
        
        scenario_values = report.get_data()
        if scenario_values.empty:
            # Snowflake may have not yet loaded with final results, wait and retry a few times to see
            for attempt in range(5):
                time.sleep(10)  # Wait 10 seconds before retrying
                report.retrieve({"Projection-ID": f"{projection_id}"})
                scenario_values = report.get_data()
                if not scenario_values.empty:
                    break

        if scenario_values.empty:    
            # If still empty after retries, raise an error
            raise ValueError("No scenarios found in the report.")
        
        if full_scenario_set:
            # Calculate top 30% of scenarios if full set is used
            scenario_values.sort_values(by='Scenario Reserve', ascending=False, inplace=True)
            top_30_percent_index = math.ceil(len(scenario_values) * 0.30)
            scenario_values = scenario_values.head(top_30_percent_index)

        # Calculate average scenario reserve
        avg_scenario_reserve = scenario_values['Scenario Reserve'].mean()
        return avg_scenario_reserve
    
    def __run_stochastic_set(self, copy_from_projection_id: int, name: str, starting_assets: float, scenarios: list[int] = None) -> int:
        if scenarios is None:
            scenarioList = ''
        else:
            scenarioList = ",".join(map(str, scenarios))
        
        projection_id = self.api.copy_projection(copy_from_projection_id, name, False)
        starting_asset_table_id = self.__create_starting_asset_table(starting_assets, 0)
        self.api.update_projection(projection_id, {
            "scenarioSubset": scenarioList,
            "dataTables": [
                {
                    "tableStructureName": self.params.starting_assets_table_structure_name,
                    "dataTableId": starting_asset_table_id
                }
            ]
        })
        self.api.run_projection(projection_id)
        return projection_id

    def __solve_starting_assets(self, sr_projection_id: int, starting_guess: GuessIteration, scenarios_to_run: list[int]) -> float:
        last_diff = starting_guess.prior_result
        last_guess = starting_guess.prior_guess
        last_diff_pct = "{:.2%}".format(last_diff/last_guess)
        current_guess = starting_guess.current_guess

        projection_params = {
                "startDate": self.base_projection_details['startDate'],
                "periodInMonths": self.base_projection_details['periodInMonths'],
                "scenarioTableId": self.base_projection_details['scenarioTableId'],
                "scenarioSubset": ",".join(map(str, scenarios_to_run)),
                "virtualFolders": [self.params.projection_virtual_folder]
            }

        try:
            self.__solver_steps = [{"Iteration": 0,
                             "Guess": last_guess,
                             "Difference": last_diff,
                             "DifferencePct": last_diff_pct}]
            for i in range(self.params.max_iterations):
                projection_id = self.api.create_projection_from_template(self.__pbr_projection_template_id, f"VM-20 Asset Collar Solver - Projection {sr_projection_id} Iteration {i+1}")
                # Update the projection params with the guess assets
                starting_asset_table_id = self.__create_starting_asset_table(current_guess, i+1)
                # Build Solver Template
                projection_params['dataTables'] = [
                    {
                        "tableStructureName": self.params.epl_table_structure_name,
                        "dataTableId": self.__epl_table_id
                    },
                    {
                        "tableStructureName": self.params.starting_assets_table_structure_name,
                        "dataTableId": starting_asset_table_id
                    }
                ]

                self.api.update_projection(projection_id, projection_params)
                # Run the projection
                self.api.run_projection(projection_id)
                # Wait for the projection to complete
                self.api.wait_for_completion(projection_id)

                # Check that starting assets in projection match the guess
                starting_asset_check = self.__get_starting_assets(projection_id)
                if abs(starting_asset_check - current_guess) > 1.0:
                    logging.error(f"Error: Starting assets in projection ({starting_asset_check}) do not match guess ({current_guess})")
                    logging.error("This may indicate an issue with the Starting Assets data table upload or projection setup." \
                    " Please verify the projection configuration.")
                    raise ValueError(f"Starting assets mismatch: expected {current_guess}, got {starting_asset_check}")
                
                stochastic_reserve = self.__get_stochastic_reserve(projection_id, full_scenario_set=False)
                current_diff = stochastic_reserve - current_guess
                current_diff_pct = "{:.2%}".format(current_diff/current_guess)

                self.__solver_steps.append({"Iteration": i+1,
                                     "Guess": current_guess,
                                     "Difference": current_diff,
                                     "DifferencePct": current_diff_pct})

                if abs(current_diff) <= self.asset_collar_tolerance * current_guess:
                    # If the difference is within the tolerance, return the current guess
                    logging.info(f"Asset Collar Solver: Tolerance met with guess {current_guess:,.2f} and difference {current_diff:,.2f} ({current_diff_pct}).")
                    return current_guess
                
                #Calculate next guess using secant method
                next_guess = current_guess - current_diff * (current_guess - last_guess) / (current_diff - last_diff)
                logging.info(f"Asset Collar Solver: Iteration {i+1}, Current Guess: {current_guess:,.2f}, Current Difference: {current_diff:,.2f} ({current_diff_pct}), Last Guess: {last_guess:,.2f}, Last Difference: {last_diff:,.2f} ({last_diff_pct}), Next Guess: {next_guess:,.2f}")
                last_guess, last_diff = current_guess, current_diff
                last_diff_pct = current_diff_pct
                current_guess = next_guess


        except Exception as e:
            self.restart_params.initial_guesses = GuessIteration(
                prior_guess=last_guess,
                prior_result=last_diff,
                current_guess=current_guess,
                iteration=i
            )
            raise e
            
        logging.warning("Asset Collar Solver: Maximum iterations reached without convergence.")
        current_diff_pct = "{:.2%}".format(current_diff/current_guess)
        last_diff_pct = "{:.2%}".format(last_diff/last_guess)
        logging.warning(f"Final guess: {current_guess}, Current Difference: {current_diff} ({current_diff_pct}),  Last Guess: {last_guess}, Last Difference: {last_diff} ({last_diff_pct})")
        return current_guess
    