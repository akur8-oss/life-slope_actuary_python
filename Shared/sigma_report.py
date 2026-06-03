from dataclasses import dataclass
from pathlib import Path
from queue import Queue
import logging
import os
import pandas as pd
from Shared.slope_api import SlopeApi
from typing import Any, Dict
import csv
import uuid

# Base folder for downloaded report files (a "Reports" subfolder is added below).
# Override per-machine with the SLOPE_REPORTS_DIR env var; otherwise default to
# a folder under the user's home directory (works on Windows and macOS).
_DEFAULT_REPORTS_BASE = Path(
    os.environ.get("SLOPE_REPORTS_DIR", Path.home() / "Slope API")
)

@dataclass
class SigmaReportParams:
    workbook_id: str
    element_id: str
    filter_params: Dict[str, str]
    working_directory: Path = _DEFAULT_REPORTS_BASE
    row_batch_size: int = 1000000

    @staticmethod
    def from_dict(obj: Any) -> 'SigmaReportParams':
        _workbook = str(obj.get("workbook"))
        _element = str(obj.get("element"))
        _filters = obj.get("filters")
        _row_batch_size = int(obj.get("row_batch_size", 1000000))
        return SigmaReportParams(_workbook, _element, _filters, row_batch_size=_row_batch_size)

class SigmaReport:
    __filename: Path = None
    __data: pd.DataFrame = None

    def __init__(self, api: SlopeApi, params: SigmaReportParams, filepath: str = None):
        self.api = api
        self.working_directory = Path(params.working_directory) / "Reports"
        self.workbook_id = params.workbook_id
        self.element_id = params.element_id
        self.filters = params.filter_params
        self.row_batch_size = params.row_batch_size

        if filepath is not None:
            self.working_directory = Path(filepath)

        self.working_directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def __combine_csv_segments(segments: list, output_filename: str):
        logging.debug(f"Combining {len(segments)} CSV segments into {output_filename}")
        with open(output_filename, 'w', newline='', encoding='utf-8') as outfile:
            writer = csv.writer(outfile)
            header_written = False
            for segment in segments:
                with open(segment, 'r', encoding='utf-8') as infile:
                    reader = csv.reader(infile)
                    header = next(reader)
                    if not header_written:
                        writer.writerow(header)
                        header_written = True
                    for row in reader:
                        writer.writerow(row)
                os.remove(segment)

    def __get_report_params(self, filter_values: dict):
        report_params = {}
        for key, value in filter_values.items():
            sigma_id = self.filters.get(key)
            if sigma_id is not None:
                report_params[sigma_id] = value

        if "Projection-ID" not in report_params:
            report_params["Projection-ID"] = "0"

        return report_params
    
    def get_data(self) -> pd.DataFrame:
        if self.__data is None:
            if self.__filename is None:
                raise ValueError("Report data has not been retrieved yet. Call retrieve() first.")
            else:
                self.__data = pd.read_csv(self.__filename, parse_dates=True)

        return self.__data
        
    def get_filename(self) -> str:
        if self.__filename is None:
            raise ValueError("Report data has not been retrieved yet. Call retrieve() first.")
        return str(self.__filename)
    
    def retrieve(self, filter_values: dict, filename: str = None):
        num_segments = 0
        offset = 0
        row_count = self.row_batch_size
        report_segments = []

        unique_id = uuid.uuid4().hex

        self.__data = None  # Clear any existing data

        if filename is None:
            self.__filename = self.working_directory / f'{self.workbook_id}_{self.element_id}_{unique_id}.csv'
        else:
            self.__filename = Path(filename)

        report_params = self.__get_report_params(filter_values)

        while row_count >= self.row_batch_size:
            num_segments += 1
            
            # Download the report segment
            logging.debug(f"Downloading report segment {num_segments} for workbook {self.workbook_id}, element {self.element_id}, offset {offset}")
            
            segment_filename = self.working_directory / f'{self.workbook_id}_{self.element_id}_{num_segments}_{unique_id}.csv'
            self.api.download_report(self.workbook_id, self.element_id, segment_filename, "Csv", report_params, row_limit=self.row_batch_size, offset=offset)
            report_segments.append(segment_filename)
            
            # Count how many rows were downloaded to see if we hit the limit
            # TODO: See if API can return the row count in the response
            with open(segment_filename, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                row_count = sum(1 for _ in reader) - 1  # subtract 1 for header

            offset += row_count
        
        if (len(report_segments) > 1):
            self.__combine_csv_segments(report_segments, self.__filename)
        else:
            # If only one segment, just rename it to the final filename
            if os.path.exists(self.__filename):
                os.remove(self.__filename)
            os.rename(report_segments[0], self.__filename)
        
        logging.info(f"Downloaded report '{self.__filename}' contains {row_count} rows.")