import requests
import datetime
import logging
import time
import threading
import hashlib
from dateutil.parser import parse
import pandas as pd
from requests.adapters import HTTPAdapter
from urllib3 import Retry


# Retry policy for every SLOPE API call, logging each retry so intermittent failures are visible in the run log
class _LoggedRetry(Retry):
    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        reason = f"HTTP {response.status}" if response is not None and response.status else repr(error)
        new_retry = super().increment(method, url, response, error, _pool, _stacktrace)
        logging.warning(f"SLOPE API {method} {url} failed ({reason}) - retry {len(new_retry.history)} of {SlopeApi.max_retries}")
        return new_retry


# Adds a default timeout to every request, so a hung connection fails and is retried instead of waiting forever
class _TimeoutHTTPAdapter(HTTPAdapter):
    def __init__(self, timeout, *args, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


class SlopeApi:
    api_url = "https://api.slopesoftware.com/api"
    app_url = "https://app.slopesoftware.com"
    max_data_table_rows = 100000  # API limit for row data table endpoints (also limited to 10MB per request)
    # Retries for intermittent failures: connection errors, timeouts and these HTTP statuses, on every method
    max_retries = 5
    retry_backoff_factor = 2           # Wait 2, 4, 8, 16, 32 seconds between retries (or the server's Retry-After)
    retry_statuses = [429, 500, 502, 503, 504]
    # (connect, read) timeouts in seconds. SLOPE API calls time out at 30 seconds on the server, so the read timeout is
    # just above that to let the server's 504 arrive first. File uploads/downloads go to pre-signed storage URLs instead
    # and can take longer for large files
    request_timeout = (10, 35)
    file_transfer_timeout = (10, 300)
    __expires: datetime.datetime
    __refresh_token = ""
    __lock = threading.Lock()

    def __init__(self):
        self.session = self.__create_session(SlopeApi.request_timeout)
        self.session.headers.update({"Content-type": "application/json"})
        # Session without the SLOPE auth headers, for token refresh and the pre-signed file upload/download URLs
        self.__external_session = self.__create_session(SlopeApi.file_transfer_timeout)
        # Session for calls that must not be repeated if SLOPE may have received them (starting a projection). It only
        # retries connection failures, where the request never reached SLOPE. Used with the main session's auth headers
        self.__no_resend_session = self.__create_session(SlopeApi.request_timeout, resend=False)

    # Returns a session that retries intermittent failures on every method, including POST and PATCH (e.g. report
    # generation), and times out hung requests. Once the retries are used up, the last response is returned so the
    # error is logged and raised by __check_response as before
    # With resend=False, only connection failures are retried - not timeouts, dropped connections or error statuses,
    # where SLOPE may already have acted on the request
    @staticmethod
    def __create_session(timeout: tuple[float, float], resend: bool = True) -> requests.Session:
        if resend:
            retry = _LoggedRetry(total=SlopeApi.max_retries,
                                 backoff_factor=SlopeApi.retry_backoff_factor,
                                 status_forcelist=SlopeApi.retry_statuses,
                                 allowed_methods=None,
                                 raise_on_status=False)
        else:
            retry = _LoggedRetry(total=SlopeApi.max_retries, connect=SlopeApi.max_retries, read=0, status=0, other=0,
                                 backoff_factor=SlopeApi.retry_backoff_factor,
                                 allowed_methods=None,
                                 raise_on_status=False)
        session = requests.Session()
        adapter = _TimeoutHTTPAdapter(timeout, max_retries=retry)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return session

    @staticmethod
    def __check_response(response):

        if not response.ok:
            logging.error(f"API call did not succeed. Response {response}")
            logging.error(f"Headers: {response.headers}")
            logging.error(f"Response Content:")
            content_type = response.headers.get('Content-Type')
            if content_type is not None and 'json' in content_type:
                logging.error(response.json())
            else:
                logging.error(response.content)
            response.raise_for_status()

        logging.debug(f"API Response: {response}")

    # Authenticate the API using API Key and API Secret
    def authorize(self, key: str, secret: str):
        with self.__lock:
            auth_params = {
                "apiKey": key,
                "apiSecretKey": secret
            }
            logging.debug("Authorizing SLOPE API")
            response = self.session.post(f"{self.api_url}/v1/Authorize", json=auth_params)
            self.__check_response(response)
            access_token = response.json()["accessToken"]
            self.session.headers.update({"Authorization": f"Bearer {access_token}"})
            self.__refresh_token = response.json()["refreshToken"]
            self.__expires = parse(response.json()["expires"])

    # Refresh the API authentication session
    def refresh(self):
        logging.debug("Refreshing API auth token")
        refresh_params = {
            "refreshToken": self.__refresh_token
        }
        response = self.__external_session.post(f"{self.api_url}/v1/Authorize/Refresh", json=refresh_params, headers={"Content-Type": "application/json"})
        self.__check_response(response)
        access_token = response.json()["accessToken"]
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})
        self.__refresh_token = response.json()["refreshToken"]
        self.__expires = parse(response.json()["expires"])

    # Returns an integer representing the number of seconds until the current API session key expires
    def __expires_in_seconds(self) -> float:
        # How many seconds until the current token expires
        return (self.__expires - datetime.datetime.now(datetime.timezone.utc)).total_seconds()

    def __keep_alive(self):
        # Check this one thread at a time so multiple refreshes don't get triggered. Race conditions on authorization calls = Bad.
        with self.__lock:
            # The token expires after 10 minutes. Refresh it if we have less than 5 minutes left
            if self.__expires_in_seconds() < 300:
                self.refresh()

    # Upload a file from local machine to the SLOPE file manager
    def upload_file(self, filename: str, slope_path: str) -> int:
        self.__keep_alive()
        slope_file_params = {"filePath": slope_path}
        response = self.session.post(f"{self.api_url}/v1/Files/GetUploadUrl", json=slope_file_params)
        upload_url = response.json()["uploadUrl"]

        logging.debug(f"Uploading file '{filename}' to '{slope_path}'.")
        # Calculate the MD5 digest of the file - We will pass this AWS to verify integrity of file upload process
        md5_digest = self.__file_md5_digest(filename)
        # Note - Do not use session here - this is a direct call to s3 and does not use the Slope session auth
        # The file is read into memory so a retry re-sends the whole file
        with open(filename, "rb") as file:
            response = self.__external_session.put(upload_url, data=file.read())
        self.__check_response(response)

        response = self.session.post(f"{self.api_url}/v1/Files/SaveUpload", json=slope_file_params)
        self.__check_response(response)
        return response.json()["fileId"]

    @staticmethod
    def __file_md5_digest(filename: str):
        with open(filename, "rb") as f:
            file_hash = hashlib.md5()
            while chunk := f.read(8192):
                file_hash.update(chunk)
        return file_hash.digest()

    # Make a Copy of an existing projection
    def copy_projection(self, projection_id: int, name: str, update_tables: bool = True) -> int:
        self.__keep_alive()
        params = {"projectionName": name, "setTablesToLatestVersion": update_tables}
        logging.debug(f"Copy projection with ID of {projection_id} to new projection named '{name}'.")
        response = self.session.post(f"{self.api_url}/v1/Projections/{projection_id}/Copy", json=params)
        self.__check_response(response)
        return response.json()["id"]
    
    # Take a file from the local machine, upload it to SLOPE and create a data table from it
    def create_data_table(self, filename: str, slope_table_params) -> int:
        self.__keep_alive()
        self.upload_file(filename, slope_table_params["filePath"])
        logging.debug(f"Creating Data Table with parameters: {slope_table_params}")
        response = self.session.post(f"{self.api_url}/v1/DataTables", json=slope_table_params)
        self.__check_response(response)
        return response.json()["id"]

    # Take a file from the local machine, upload it to SLOPE
    # If the requested data table does not already exist, create it from this file
    # If it does already exist, update it from this file
    def create_or_update_data_table(self, filename: str, slope_table_params) -> int:
        self.__keep_alive()
        self.upload_file(filename, slope_table_params["filePath"])
        response = self.session.post(f"{self.api_url}/v1/DataTables/0", json=slope_table_params)
        if response.ok:
            logging.debug(f"Created new Data Table with parameters: {slope_table_params}")
            return response.json()["id"]
        if response.status_code != 409:
            self.__check_response(response)
            return None

        logging.debug(f"Updating Data Table with parameters: {slope_table_params}")
        response = self.session.patch(f"{self.api_url}/v1/DataTables", json=slope_table_params)
        self.__check_response(response)
        return response.json()["id"]
    
    # Take a file from the local machine, upload it to SLOPE and create a decrement table from it
    def create_decrement_table(self, filename: str, slope_table_params) -> int:
        self.__keep_alive()
        self.upload_file(filename, slope_table_params["filePath"])
        logging.debug(f"Creating Decrement Table with parameters: {slope_table_params}")
        # Check if table already exists and rename to new name
        response = self.session.post(f"{self.api_url}/v1/DecrementTables", json=slope_table_params)
        self.__check_response(response)
        return response.json()["id"]

    # Create a decrement table from a file that already exists in the SLOPE File Manager
    def create_only_decrement_table(self, slope_table_params) -> int:
        self.__keep_alive()
        logging.debug(f"Creating Decrement Table with parameters: {slope_table_params}")
        # Check if table already exists and rename to new name
        response = self.session.post(f"{self.api_url}/v1/DecrementTables", json=slope_table_params)
        self.__check_response(response)
        return response.json()["id"]
    
    # Create a new projection from an existing projection template
    def create_projection_from_template(self, template_id: int, name: str) -> int:
        self.__keep_alive()
        params = {"templateId": template_id, "name": name}
        logging.debug(f"Creating projection from Template {template_id} with name: {name}")
        response = self.session.post(f"{self.api_url}/v1/Projections", json=params)
        self.__check_response(response)
        return response.json()["id"]
    
     # Take a file from the local machine, upload it to SLOPE and create a scenario table from it
    def create_scenario_table(self, filename: str, slope_scenario_table_params) -> int:
        self.__keep_alive()
        self.upload_file(filename, slope_scenario_table_params["filePath"])
        logging.debug(f"Creating scenario table with parameters: {slope_scenario_table_params}")
        response = self.session.post(f"{self.api_url}/v1/ScenarioTables", json=slope_scenario_table_params)
        self.__check_response(response)
        return response.json()["id"]
    
     # Take a file from the local machine, upload it to SLOPE
    # If the requested data table does not already exist, create it from this file
    # If it does already exist, update it from this file
    def create_or_update_scenario_table(self, filename: str, slope_scenario_table_params) -> int:
        self.__keep_alive()
        self.upload_file(filename, slope_scenario_table_params["filePath"])
        response = self.session.post(f"{self.api_url}/v1/ScenarioTables", json=slope_scenario_table_params)
        if response.ok:
            logging.debug(f"Created new Scenario Table with parameters: {slope_scenario_table_params}")
            return response.json()["id"]
        if response.status_code != 409:
            self.__check_response(response)
            return None

        response = self.session.get(f"{self.api_url}/v1/Models/{slope_scenario_table_params['modelId']}/ScenarioTables")
        self.__check_response(response)
        # Get the list of scenario tables for the model
        scenario_tables = response.json()["items"]
        scenario_table_id = next(item for item in scenario_tables if item["name"] == slope_scenario_table_params["name"])["id"]
        logging.debug(f"Updating Scenario Table with parameters: {slope_scenario_table_params}")
        response = self.session.patch(f"{self.api_url}/v1/ScenarioTables/{scenario_table_id}", json=slope_scenario_table_params)
        self.__check_response(response)
        return response.json()["id"]
    
    def generate_workbook_report(self, workbook_id: str, element_id: str, format_type: str, parameters: dict, row_limit: int = None, offset: int = None) -> dict:
        self.__keep_alive()
        report_params = {
            "reportFormat": format_type,
            "elementId": element_id,
        }
        if parameters:
            report_params["parameters"] = parameters
        if row_limit:
            report_params["rowLimit"] = row_limit
        if offset:
            report_params["offset"] = offset
            
        logging.debug(f"Generating workbook report {workbook_id} with parameters: {report_params}")
        response = self.session.post(f"{self.api_url}/v1/Reports/Workbooks/{workbook_id}/Generate", json=report_params)
        self.__check_response(response)
        return response.json()

    def get_workbook_report_status(self, generation_id: str) -> dict:
        self.__keep_alive()
        response = self.session.get(f"{self.api_url}/v1/Reports/Workbooks/Status/{generation_id}")
        self.__check_response(response)
        return response.json()
    
    # Download results from a single element in a single workbook
    def download_report(self, workbook_id: str, element_id: str, filename: str, format_type: str, parameters: dict, row_limit=None, offset=None, timeout=900):
        self.__keep_alive()
        report_response = self.generate_workbook_report(
            workbook_id=workbook_id,
            element_id=element_id,
            format_type=format_type,
            parameters=parameters,
            row_limit=row_limit,
            offset=offset
        )
        generation_id = report_response["generationId"]
        logging.debug(f"Report generation started with ID: {generation_id}")

        start_time = time.time()
        while True:
            status_response = self.get_workbook_report_status(generation_id)
            if status_response["status"] == "Completed":
                download_url = status_response["downloadUrl"]
                break
            elif status_response["status"] == "Failed":
                raise Exception(f"Report generation failed: {status_response.get('message', 'Unknown error')}")
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Report generation did not complete within {timeout} seconds.")
            time.sleep(5)
        logging.debug(f"Downloading report from {download_url}")
        file_response = self.__external_session.get(download_url)
        self.__check_response(file_response)
        
        logging.debug(f"Saving as '{filename}'.")
        file = open(filename, "wb")
        file.write(file_response.content)
        file.close()

    def download_and_load_report(self, workbook_id: str, element_id: str, filename: str, format_type: str, parameters: dict) -> pd.DataFrame:
        self.download_report(workbook_id, element_id, filename, format_type, parameters)
        data = None
        match format_type:
            case "Csv":
                data = pd.read_csv(filename, dtype='str')
            case "Excel":
                data = pd.read_excel(filename)
            case _:
                logging.error("Invalid report type requested.")
        return data

    # Download the contents of a data table with given Data Table ID
    # Returns an pandas DataFrame object with the contents of the table
    def get_data_table_by_id(self, data_table_id: int) -> pd.DataFrame:
        self.__keep_alive()
        logging.debug(f"Retrieving contents of data table with ID '{data_table_id}'")
        endpoint_url = f"{self.api_url}/v1/DataTables/Data?DataTableId={data_table_id}"
        return self.__get_data_table(endpoint_url)

    # Download the contents of a data table with given Data Table Name, Version, and Table Structure ID
    # Returns an pandas DataFrame object with the contents of the table
    def get_data_table_by_name(self, table_name: str, table_structure_id: int, version: int = None) -> pd.DataFrame:
        self.__keep_alive()
        version_name = version or "latest"
        logging.debug(f"Retrieving contents of data table with Name '{table_name}' Version '{version_name}' of Table Structure ID '{table_structure_id}'")
        endpoint_url = f"{self.api_url}/v1/DataTables/Data?Name={table_name}&TableStructureId={table_structure_id}"
        if version is not None:
            endpoint_url += f"&Version={version}"

        return self.__get_data_table(endpoint_url)

    # Internal function for getting Data Table contents - Handles pagination of the data contents
    def __get_data_table(self, url: str) -> pd.DataFrame:
        response = self.session.get(url)
        self.__check_response(response)
        json = response.json()
        if 'rows' not in json:
            logging.error("Data Table Files not implemented yet. Empty Data Returns")
            return pd.DataFrame()

        #data_table = {key:json[key] for key in json.keys() - {'rows', 'offset'}}
        table = SlopeApi.__parse_data_table_json(json)

        # Check if we got the whole table or if we hit the row limit
        # If row limit was hit, then offset will be not' 'None' (and contain an integer value)
        # Keep looping until we get the whole table
        while json['offset']:
            logging.debug(f"Retrieving more data from table '{json['name']}' ID '{json['id']}' starting at row {json['offset']}")
            response = self.session.get(url + f"&Offset={json['offset']}")
            self.__check_response(response)
            json = response.json()
            table = pd.concat([table, SlopeApi.__parse_data_table_json(json)])

        #data_table["data"] = table

        return table

    # Internal Method for converting data table contents into pandas DataFrame and setting data properties correctly
    @staticmethod
    def __parse_data_table_json(json) -> pd.DataFrame:
        columns = []
        index = []
        # Get the column names
        for col in json['columns']:
            columns.append(col['name'])
            if col['isIndex']:
                index.append(col['name'])
        df = pd.DataFrame.from_records(data=json['rows'], columns=columns)
        # Convert each column to the correct data type
        for col in json['columns']:
            if col['dataType'] == 'Integer' or col['dataType'] == 'Decimal':
                df[col['name']] = pd.to_numeric(df[col['name']])
            elif col['dataType'] == 'Boolean':
                df[col['name']] = df[col['name']].astype(bool)
            else:
                df[col['name']] = df[col['name']].astype(str)

        df.set_index(index)
        return df

    # Returns all of the properties set on a given Projection
    def get_projection_details(self, projection_id: int, fields: list[str] = None):
        self.__keep_alive()
        response = ""
        if fields is None:
            response = self.session.get(f"{self.api_url}/v1/Projections/{projection_id}")
        else :
            csv_fields = ','.join(fields)
            response = self.session.get(f"{self.api_url}/v1/Projections/{projection_id}?fields={csv_fields}")
        self.__check_response(response)
        return response.json()

    # Gets the run status of a projection
    def get_projection_status(self, projection_id) -> str:
        return self.get_projection_details(projection_id, ["status"])["status"]
    
    def get_table_structure_columns(self, table_structure_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Table Structure columns for ID {table_structure_id}")
        response = self.session.get(f"{self.api_url}/v1/TableStructures/{table_structure_id}/Columns")
        self.__check_response(response)
        return response.json()["items"]
    
    
    # Check if a projection is still running
    # True - The projection is still running
    # False - The projection has completed (possibly unsuccessfully)
    def is_projection_running(self, projection_id) -> bool:
        self.__keep_alive()
        response = self.session.get(f"{self.api_url}/v1/Projections/{projection_id}")
        if response.ok:
            return response.json()["isRunning"]
        else:
            return False
        
    # Returns a list of all data tables that exist on a given Model ID
    def list_data_tables(self, model_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Data Table listing from model {model_id}")
        response = self.session.get(f"{self.api_url}/v1/DataTables/List?ModelId={model_id}")
        self.__check_response(response)
        return response.json()

    # Returns a list of all data tables that exist on a given Model ID with specified Table Structure Name
    def list_data_tables_by_structure_name(self, model_id: int, table_structure_name: str) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Data Table listing from model {model_id} with Table Structure name '{table_structure_name}'")
        response = self.session.get(f"{self.api_url}/v1/DataTables/List?ModelId={model_id}&TableStructureName={table_structure_name}")
        self.__check_response(response)
        return response.json()

    # Returns a list of all data tables that exist on a given Table Structure ID
    def list_data_tables_by_structure_id(self, table_structure_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Data Table listing from Table Structure {table_structure_id}")
        response = self.session.get(
            f"{self.api_url}/v1/DataTables/List?TableStructureId={table_structure_id}")
        self.__check_response(response)
        return response.json()

    # Returns a list of all decrement tables that exist on a given Model ID
    def list_decrement_tables(self, model_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Data Table listing from model {model_id}")
        response = self.session.get(f"{self.api_url}/v1/DecrementTables/List?ModelId={model_id}")
        self.__check_response(response)
        return response.json()

    def list_projection_templates(self, model_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Projection Templates from model {model_id}")
        url = f"{self.api_url}/v1/Models/{model_id}/ProjectionTemplates"
        response = self.session.get(url)
        self.__check_response(response)
        json = response.json()
        templates = json["items"]
    
        # Check if we got the entire list or if we hit the row limit
        # If row limit was hit, then offset will be not' 'None' (and contain an integer value)
        # Keep looping until we get the whole table
        while 'offset' in json and json['offset']:
            logging.debug(f"Retrieving more templates for model {model_id} starting at row {json['offset']}")
            response = self.session.get(url + f"&Offset={json['offset']}")
            self.__check_response(response)
            json = response.json()
            templates = templates + json["items"]

        return templates    
    
    # Returns a list of all table structures that exist on a given Model ID
    def list_table_structures(self, model_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving all Table Structures from mode {model_id}")
        response = self.session.get(f"{self.api_url}/v1/TableStructures/List/{model_id}")
        self.__check_response(response)
        return response.json()

    # Run a projection
    def run_projection(self, projection_id):
        self.__keep_alive()
        # Not retried once sent - a repeated start of a projection that already started would fail
        response = self.__no_resend_session.post(f"{self.api_url}/v1/Projections/{projection_id}/run",
                                                 headers=self.session.headers)
        if not response.ok:
            raise Exception(f"Failed to start projection with id: {projection_id}", response.text)
        
    # Link to a projection's details in the SLOPE app
    @staticmethod
    def projection_url(projection_id: int) -> str:
        return f"{SlopeApi.app_url}/ModelResults/FinancialProjection/DetailsTabView/{projection_id}"

    # Update values and properties on a projection
    def update_projection(self, projection_id, properties):
        self.__keep_alive()
        logging.debug(f"Updating Projection ID {projection_id} with parameters: {properties}")
        response = self.session.patch(f"{self.api_url}/v1/Projections/{projection_id}", json=properties)
        self.__check_response(response)

    # Update the Model Point file on a projection
    def update_projection_mpf(self, projection_id, portfolio_name, product_name, model_point_file_id):
        self.__keep_alive()
        projection_update_parameters = {
            "portfolios": [{
                "portfolioName": portfolio_name,
                "products": [{
                    "productName": product_name,
                    "modelPointFile": {
                        "fileId": model_point_file_id
                    }
                }]
            }]
        }
        logging.debug(f"Updating Projection ID {projection_id} with parameters: {projection_update_parameters}")
        response = self.session.patch(f"{self.api_url}/v1/Projections/{projection_id}", json=projection_update_parameters)
        self.__check_response(response)

    # Update the data table being used on a projection
    def update_projection_table(self, projection_id, table_name, data_table_id):
        self.__keep_alive()
        projection_update_parameters = {
            "dataTables": [{
                "tableStructureName": table_name,
                "dataTableId": data_table_id
            }]
        }
        logging.debug(f"Updating Projection ID {projection_id} with parameters: {projection_update_parameters}")
        response = self.session.patch(f"{self.api_url}/v1/Projections/{projection_id}", json=projection_update_parameters)
        self.__check_response(response)

    # Wait until a projection has completed running. Periodically check for updates until is is finished.
    def wait_for_completion(self, projection_id):
        while self.is_projection_running(projection_id):
            status = self.get_projection_status(projection_id)
            logging.info(f"Waiting for Projection ID {projection_id} to finish. Current status: {status}")
            time.sleep(15)  # Check once every 15 seconds if it is done

    # Internal function for retrieving every page of a paginated 'items'/'offset' endpoint
    def __get_all_pages(self, url: str, params: dict = None) -> list[dict]:
        params = dict(params or {})
        items = []
        while True:
            response = self.session.get(url, params=params)
            self.__check_response(response)
            json = response.json()
            items += json["items"]
            if not json.get("offset"):
                return items
            params["Offset"] = json["offset"]

    # Returns a list of all products defined on a given Model ID
    def list_model_products(self, model_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Products from model {model_id}")
        return self.__get_all_pages(f"{self.api_url}/v1/Models/{model_id}/Products")

    # Returns a list of all data tables on a given Model ID, optionally filtered to a Table Structure name
    def list_model_data_tables(self, model_id: int, table_structure_name: str = None) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Data Tables from model {model_id} with Table Structure name '{table_structure_name}'")
        params = {"TableStructureName": table_structure_name} if table_structure_name else None
        return self.__get_all_pages(f"{self.api_url}/v1/Models/{model_id}/DataTables", params)

    # Returns the model point fields defined on a given (model) Product ID
    def get_product_model_point_fields(self, product_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Model Point Fields for product {product_id}")
        return self.__get_all_pages(f"{self.api_url}/v1/Products/{product_id}/ModelPointFields")

    # Returns the portfolios configured on a projection
    def get_projection_portfolios(self, projection_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Portfolios for projection {projection_id}")
        return self.__get_all_pages(f"{self.api_url}/v1/Projections/{projection_id}/Portfolios")

    # Returns the products configured on a projection's portfolio (portfolio ID is the ID on the projection)
    def get_portfolio_products(self, portfolio_id: int) -> list[dict]:
        self.__keep_alive()
        logging.debug(f"Retrieving Products for projection portfolio {portfolio_id}")
        return self.__get_all_pages(f"{self.api_url}/v1/Projections/Portfolios/{portfolio_id}/Products")

    # Add a model product to a projection's portfolio. Returns the ID of the product on the projection
    def add_product_to_portfolio(self, portfolio_id: int, product_id: int, name: str, model_point_file: dict = None) -> int:
        self.__keep_alive()
        params = {"productId": product_id, "name": name}
        if model_point_file is not None:
            params["modelPointFile"] = model_point_file
        logging.debug(f"Adding product to projection portfolio {portfolio_id} with parameters: {params}")
        response = self.session.post(f"{self.api_url}/v1/Projections/Portfolios/{portfolio_id}/Products", json=params)
        self.__check_response(response)
        return response.json()["id"]

    # Remove a product from a projection's portfolio (product ID is the ID on the projection)
    def remove_product_from_portfolio(self, product_id: int):
        self.__keep_alive()
        logging.debug(f"Removing product {product_id} from projection portfolio")
        response = self.session.delete(f"{self.api_url}/v1/Projections/Portfolios/Products/{product_id}")
        self.__check_response(response)

    # Returns the latest version of files in the SLOPE File Manager, optionally filtered to a list of folders
    def list_files(self, folders: list[str] = None) -> list[dict]:
        self.__keep_alive()
        params = {"Folders": ",".join(folders)} if folders else None
        logging.debug(f"Retrieving file listing for folders: {folders}")
        return self.__get_all_pages(f"{self.api_url}/v1/Files/GetFiles", params)

    # Download the contents of a data table exactly as the API returns it (handles pagination)
    # Returns {"columns": [...], "rows": [[...], ...]} with rows as positional string values, which can be passed back
    # unchanged to create_data_table_from_rows / update_data_table_rows
    def get_data_table_raw(self, data_table_id: int) -> dict:
        self.__keep_alive()
        logging.debug(f"Retrieving raw contents of data table with ID '{data_table_id}'")
        url = f"{self.api_url}/v1/DataTables/Data"
        params = {"DataTableId": data_table_id}
        response = self.session.get(url, params=params)
        self.__check_response(response)
        json = response.json()
        if 'rows' not in json:
            raise ValueError(f"Data Table {data_table_id} is file only - row data is not available")
        table = {"columns": json["columns"], "rows": json["rows"]}
        while json.get("offset"):
            params["Offset"] = json["offset"]
            response = self.session.get(url, params=params)
            self.__check_response(response)
            json = response.json()
            table["rows"] += json["rows"]
        return table

    # Create a new data table directly from row data (no file upload). Returns the new Data Table ID
    def create_data_table_from_rows(self, table_structure_id: int, name: str, rows: list[list[str]]) -> int:
        self.__keep_alive()
        self.__check_row_limit(rows)
        logging.debug(f"Creating Data Table '{name}' on Table Structure {table_structure_id} with {len(rows)} rows")
        params = {"tableStructureId": table_structure_id, "name": name, "rows": rows}
        response = self.session.post(f"{self.api_url}/v2/DataTables", json=params)
        self.__check_response(response)
        return response.json()["id"]

    # Overwrite the rows of an existing data table directly from row data (no file upload)
    # Returns the Data Table ID, which will differ from the one passed in if SLOPE versioned the table
    def update_data_table_rows(self, data_table_id: int, rows: list[list[str]]) -> int:
        self.__keep_alive()
        self.__check_row_limit(rows)
        logging.debug(f"Updating Data Table {data_table_id} with {len(rows)} rows")
        response = self.session.put(f"{self.api_url}/v1/DataTables/{data_table_id}/Data", json={"rows": rows})
        self.__check_response(response)
        return response.json()["id"]

    # If a data table with this name already exists on the model for the table structure, overwrite its rows
    # Otherwise create a new data table from the rows. Returns the Data Table ID
    def upsert_data_table_rows(self, model_id: int, table_structure_id: int, table_structure_name: str, name: str, rows: list[list[str]]) -> int:
        existing = self.list_model_data_tables(model_id, table_structure_name)
        data_table_id = next((table["id"] for table in existing
                              if table["name"] == name and table.get("isLatestVersion", True)), None)
        if data_table_id is None:
            return self.create_data_table_from_rows(table_structure_id, name, rows)
        return self.update_data_table_rows(data_table_id, rows)

    @staticmethod
    def __check_row_limit(rows: list):
        if len(rows) == 0:
            raise ValueError("Data table row data must contain at least 1 row")
        if len(rows) > SlopeApi.max_data_table_rows:
            raise ValueError(f"Data table row data has {len(rows)} rows - the API limit is {SlopeApi.max_data_table_rows}. "
                             "Use the file-based table upload for larger tables.")

    # Convert a DataFrame into positional row data for the data table row endpoints
    # columns is the result of get_table_structure_columns. Values are ordered with index columns first, then data
    # columns, each by sortOrder. Columns missing from the DataFrame and NaN/None values are passed as empty strings
    @staticmethod
    def dataframe_to_rows(df: pd.DataFrame, columns: list[dict]) -> list[list[str]]:
        ordered = sorted(columns, key=lambda col: (not col["isIndex"], col["sortOrder"]))
        for col in ordered:
            if col["dataType"] == "MortalityTable":
                raise ValueError(f"Column '{col['name']}' is a MortalityTable column, which is not supported by dataframe_to_rows")

        def to_str(value, data_type: str) -> str:
            if value is None or (not isinstance(value, str) and pd.isna(value)):
                return ""
            if data_type == "Boolean":
                return "true" if str(value).lower() in ("true", "1", "1.0") else "false"
            if data_type == "Integer" and isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)

        rows = []
        for _, record in df.iterrows():
            rows.append([to_str(record[col["name"]], col["dataType"]) if col["name"] in df.columns else ""
                         for col in ordered])
        return rows
