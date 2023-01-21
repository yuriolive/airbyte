#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#

import datetime
import time
from dataclasses import InitVar, dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Union

import requests
from airbyte_cdk.models import AirbyteMessage, SyncMode, Type
from airbyte_cdk.sources.declarative.auth.declarative_authenticator import DeclarativeAuthenticator
from airbyte_cdk.sources.declarative.auth.token import BasicHttpAuthenticator
from airbyte_cdk.sources.declarative.datetime.min_max_datetime import MinMaxDatetime
from airbyte_cdk.sources.declarative.exceptions import ReadException
from airbyte_cdk.sources.declarative.extractors import RecordSelector
from airbyte_cdk.sources.declarative.extractors.record_extractor import RecordExtractor
from airbyte_cdk.sources.declarative.interpolation.interpolated_string import InterpolatedString
from airbyte_cdk.sources.declarative.requesters.request_option import RequestOption
from airbyte_cdk.sources.declarative.retrievers import SimpleRetriever
from airbyte_cdk.sources.declarative.stream_slicers import DatetimeStreamSlicer, SingleSlice
from airbyte_cdk.sources.declarative.types import Config, Record, StreamSlice, StreamState
from airbyte_cdk.sources.streams.core import Stream, StreamData
from airbyte_cdk.sources.streams.http.requests_native_auth.abstract_token import AbstractHeaderAuthenticator
from dataclasses_jsonschema import JsonSchemaMixin
from isodate import Duration, parse_duration


@dataclass
class ShortLivedTokenAuthenticator(AbstractHeaderAuthenticator, DeclarativeAuthenticator, JsonSchemaMixin):
    """
    https://docs.railz.ai/reference/authentication
    """

    client_id: Union[InterpolatedString, str]
    secret_key: Union[InterpolatedString, str]
    url: Union[InterpolatedString, str]
    config: Config
    options: InitVar[Mapping[str, Any]]
    token_key: Union[InterpolatedString, str] = "access_token"
    lifetime: Union[InterpolatedString, str] = "PT3600S"

    def __post_init__(self, options: Mapping[str, Any]):
        self._client_id = InterpolatedString.create(self.client_id, options=options)
        self._secret_key = InterpolatedString.create(self.secret_key, options=options)
        self._url = InterpolatedString.create(self.url, options=options)
        self._token_key = InterpolatedString.create(self.token_key, options=options)
        self._lifetime = InterpolatedString.create(self.lifetime, options=options)
        self._basic_auth = BasicHttpAuthenticator(
            username=self._client_id,
            password=self._secret_key,
            config=self.config,
            options=options,
        )
        self._session = requests.Session()
        self._token = None
        self._timestamp = None

    @classmethod
    def _parse_timedelta(cls, time_str) -> Union[datetime.timedelta, Duration]:
        """
        :return Parses an ISO 8601 durations into datetime.timedelta or Duration objects.
        """
        if not time_str:
            return datetime.timedelta(0)
        return parse_duration(time_str)

    def check_token(self):
        now = time.time()
        url = self._url.eval(self.config)
        token_key = self._token_key.eval(self.config)
        lifetime = self._parse_timedelta(self._lifetime.eval(self.config))
        if not self._token or now - self._timestamp >= lifetime.seconds:
            response = self._session.get(url, headers=self._basic_auth.get_auth_header())
            response.raise_for_status()
            response_json = response.json()
            if token_key not in response_json:
                raise Exception(f"token_key: '{token_key}' not found in response {url}")
            self._token = response_json[token_key]
            self._timestamp = now

    @property
    def auth_header(self) -> str:
        return "Authorization"

    @property
    def token(self) -> str:
        self.check_token()
        return f"Bearer {self._token}"


@dataclass
class RailzNestedExtractor(RecordExtractor, JsonSchemaMixin):
    config: Config
    options: InitVar[Mapping[str, Any]]
    nested_fields: List[Union[InterpolatedString, str]]
    propagate_fields: List[Union[InterpolatedString, str]]
    prefix_key: Union[InterpolatedString, str] = None

    def __post_init__(self, options: Mapping[str, Any]):
        self._nested_fields = [InterpolatedString.create(nested_field, options=options) for nested_field in self.nested_fields]
        if not self._nested_fields:
            ValueError("nested_fields cannot be empty")
        self._propagate_fields = [InterpolatedString.create(propagate_field, options=options) for propagate_field in self.propagate_fields]
        self._prefix_key = self.prefix_key
        if self.prefix_key:
            self._prefix_key = InterpolatedString.create(self.prefix_key, options=options)

    def extract_records(self, response: requests.Response) -> List[Record]:
        response_json = response.json()
        nested_fields = [f.eval(self.config) for f in self._nested_fields]
        propagate_fields = [f.eval(self.config) for f in self._propagate_fields]
        prefix_key = self._prefix_key
        if self._prefix_key:
            prefix_key = self._prefix_key.eval(self.config)
        records = []
        for record in self._extract_records(response_json, nested_fields, propagate_fields):
            if prefix_key:
                record = {prefix_key: record}
            records.append(record)
        return records

    def _extract_records(self, obj, nested_fields, propagate_fields=None):
        field = nested_fields.pop(0)
        for record in obj[field]:
            for propagate_field in propagate_fields:
                if propagate_field in obj:
                    record[propagate_field] = obj[propagate_field]
            if nested_fields:
                yield from self._extract_records(record, nested_fields[:], propagate_fields)
            else:
                yield record


@dataclass
class RailzAiParentStreamConfig(JsonSchemaMixin):
    options: InitVar[Mapping[str, Any]]
    stream: Stream

    def __post_init__(self, options: Mapping[str, Any]):
        self._options = options


@dataclass
class RailzAiServiceSlicer(SingleSlice):
    config: Config
    parent_stream_config: RailzAiParentStreamConfig

    def __post_init__(self, options: Mapping[str, Any]):
        self._options = options
        self._cursor = {}

    def get_stream_state(self) -> StreamState:
        return self._cursor if self._cursor else {}

    def update_cursor(self, stream_slice: StreamSlice, last_record: Optional[Record] = None):
        if last_record is None:
            self._cursor.update(stream_slice)
            return

        if stream_slice["businessName"] not in self._cursor:
            self._cursor[stream_slice["businessName"]] = set()
        self._cursor[stream_slice["businessName"]].add(stream_slice["serviceName"])

    @staticmethod
    def _check_valid_service(connection):
        return connection["status"] in ["active", "disconnected", "expired"]

    def stream_slices(self, sync_mode: SyncMode, stream_state: StreamState) -> Iterable[StreamSlice]:
        if not self.parent_stream_config:
            yield from []
        else:
            parent_stream = self.parent_stream_config.stream
            for parent_stream_slice in parent_stream.stream_slices(sync_mode=sync_mode, cursor_field=None, stream_state=stream_state):
                empty_parent_slice = True

                for parent_record in parent_stream.read_records(
                    sync_mode=SyncMode.full_refresh, cursor_field=None, stream_slice=parent_stream_slice, stream_state=None
                ):
                    # Skip non-records (eg AirbyteLogMessage)
                    if isinstance(parent_record, AirbyteMessage):
                        if parent_record.type == Type.RECORD:
                            parent_record = parent_record.record.data
                        else:
                            continue

                    for connection in parent_record["connections"]:
                        if self._check_valid_service(connection):
                            empty_parent_slice = False
                            yield {"businessName": parent_record["businessName"], "serviceName": connection["serviceName"]}

                if empty_parent_slice:
                    yield from []

    def get_request_params(
        self,
        stream_state: Optional[StreamState] = None,
        stream_slice: Optional[StreamSlice] = None,
        next_page_token: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        options = {}
        if stream_slice:
            options = {
                "businessName": stream_slice["businessName"],
                "serviceName": stream_slice["serviceName"],
            }
        return options


@dataclass
class RailzAiDatetimeStreamSlicer(DatetimeStreamSlicer):
    cursor_format: str = "%Y-%m-%d"

    def parse_date(self, date: str) -> datetime.datetime:
        return self._parser.parse(date, self.cursor_format, self._timezone)


@dataclass
class RailzAiIncrementalReportsSlicer(RailzAiDatetimeStreamSlicer):
    def update_cursor(self, stream_slice: StreamSlice, last_record: Optional[Record] = None):
        if last_record is None:
            self._cursor = stream_slice.get(self.cursor_field.eval(self.config))
            return

        stream_slice_value_end = stream_slice.get(self.stream_slice_field_end.eval(self.config))
        last_record_value = last_record.get(self.cursor_field.eval(self.config)) if last_record else None
        if self._cursor and last_record_value:
            self._cursor = max(last_record_value, self._cursor)
        elif last_record_value:
            self._cursor = last_record_value
        if self.stream_slice_field_end:
            self._cursor_end = stream_slice_value_end


@dataclass
class RailzAiIncrementalServiceSlicer(RailzAiServiceSlicer):
    base_datetime_stream_slicer_options: Mapping[str, Any]

    def update_cursor(self, stream_slice: StreamSlice, last_record: Optional[Record] = None):
        if last_record is None:
            self._cursor.update(stream_slice)
            return

        if stream_slice["businessName"] not in self._cursor:
            self._cursor[stream_slice["businessName"]] = {}
        if stream_slice["serviceName"] not in self._cursor[stream_slice["businessName"]]:
            self._cursor[stream_slice["businessName"]][stream_slice["serviceName"]] = {}
        service = self._cursor[stream_slice["businessName"]][stream_slice["serviceName"]]

        last_record_cursor_value = last_record[self._datetime_cursor_field]

        if self._datetime_cursor_field not in service or service[self._datetime_cursor_field] < last_record_cursor_value:
            service[self._datetime_cursor_field] = last_record_cursor_value

    def stream_slices(self, sync_mode: SyncMode, stream_state: StreamState) -> Iterable[StreamSlice]:
        for stream_slice in super().stream_slices(sync_mode, stream_state):
            state_cursor_value = (
                stream_state.get(stream_slice["businessName"], {})
                .get(stream_slice["serviceName"], {})
                .get(self._datetime_cursor_field, None)
            )
            if state_cursor_value:
                start = self._datetime_parse_date(state_cursor_value).strftime(
                    self.base_datetime_stream_slicer_options["start_datetime"]["datetime_format"]
                )
                datetime_stream_slicer = self._create_datetime_stream_slicer(
                    {
                        **self.base_datetime_stream_slicer_options,
                        "start_datetime": {
                            **self.base_datetime_stream_slicer_options["start_datetime"],
                            "datetime": start,
                        },
                    }
                )
            else:
                datetime_stream_slicer = self._create_datetime_stream_slicer(self.base_datetime_stream_slicer_options)
            for datetime_stream_slice in datetime_stream_slicer.stream_slices(
                sync_mode, stream_state.get(stream_slice["businessName"], {}).get(stream_slice["serviceName"], {})
            ):
                yield {**stream_slice, **datetime_stream_slice}

    def get_request_params(
        self,
        stream_state: Optional[StreamState] = None,
        stream_slice: Optional[StreamSlice] = None,
        next_page_token: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        options = {}
        if stream_slice:
            start_date = (stream_slice[self._datetime_slice_field_start],)
            end_date = (stream_slice[self._datetime_slice_field_end],)
            if isinstance(start_date, tuple):
                start_date = start_date[0]
            if isinstance(end_date, tuple):
                end_date = end_date[0]

            options = {
                "businessName": stream_slice["businessName"],
                "serviceName": stream_slice["serviceName"],
                self._datetime_cursor_field: f"gte:{start_date};lte:{end_date}",
            }
        return options

    @property
    def _datetime_cursor_field(self):
        datetime_stream_slicer = self._create_datetime_stream_slicer(self.base_datetime_stream_slicer_options)
        return datetime_stream_slicer.cursor_field.eval(self.config)

    @property
    def _datetime_slice_field_start(self):
        datetime_stream_slicer = self._create_datetime_stream_slicer(self.base_datetime_stream_slicer_options)
        return datetime_stream_slicer.stream_slice_field_start.eval(self.config)

    @property
    def _datetime_slice_field_end(self):
        datetime_stream_slicer = self._create_datetime_stream_slicer(self.base_datetime_stream_slicer_options)
        return datetime_stream_slicer.stream_slice_field_end.eval(self.config)

    @property
    def _datetime_options_start_field(self):
        datetime_stream_slicer = self._create_datetime_stream_slicer(self.base_datetime_stream_slicer_options)
        return datetime_stream_slicer.start_time_option.field_name

    @property
    def _datetime_options_end_field(self):
        datetime_stream_slicer = self._create_datetime_stream_slicer(self.base_datetime_stream_slicer_options)
        return datetime_stream_slicer.end_time_option.field_name

    def _datetime_parse_date(self, date):
        datetime_stream_slicer = self._create_datetime_stream_slicer(self.base_datetime_stream_slicer_options)
        return datetime_stream_slicer.parse_date(date)

    def _create_datetime_stream_slicer(self, options):
        args = {
            **options,
            "start_datetime": MinMaxDatetime(**options["start_datetime"], options=self._options),
            "end_datetime": MinMaxDatetime(**options["end_datetime"], options=self._options),
            "start_time_option": RequestOption(**options["start_time_option"], options=self._options),
            "end_time_option": RequestOption(**options["end_time_option"], options=self._options),
            "options": self._options,
            "config": self.config,
        }
        return RailzAiDatetimeStreamSlicer(**args)


@dataclass
class RailzAiIncrementalServiceReportsSlicer(RailzAiIncrementalServiceSlicer):
    def get_request_params(
        self,
        stream_state: Optional[StreamState] = None,
        stream_slice: Optional[StreamSlice] = None,
        next_page_token: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        options = {}
        if stream_slice:
            start_date = (stream_slice[self._datetime_slice_field_start],)
            end_date = (stream_slice[self._datetime_slice_field_end],)
            if isinstance(start_date, tuple):
                start_date = start_date[0]
            if isinstance(end_date, tuple):
                end_date = end_date[0]

            options = {
                "businessName": stream_slice["businessName"],
                "serviceName": stream_slice["serviceName"],
                self._datetime_options_start_field: start_date,
                self._datetime_options_end_field: end_date,
            }
        return options


@dataclass
class RailzAiServiceRetriever(SimpleRetriever):
    def __post_init__(self, options: Mapping[str, Any]):
        super().__post_init__(options)
        self._failed_services = set()

    def read_records(
        self,
        sync_mode: SyncMode,
        cursor_field: Optional[List[str]] = None,
        stream_slice: Optional[StreamSlice] = None,
        stream_state: Optional[StreamState] = None,
    ) -> Iterable[StreamData]:
        if stream_slice["serviceName"] in self._failed_services:
            yield from []
            return

        try:
            yield from super().read_records(sync_mode, cursor_field, stream_slice, stream_state)
        except ReadException as e:
            self._failed_services.add(stream_slice["serviceName"])
            self.logger.warning(e)
            yield from []


@dataclass
class RailzAiReportsSelector(RecordSelector):
    config: Config = field(default_factory=dict)
    meta_fields: Union[List[str], str, None] = None
    _default_meta_fields = ("reportId",)

    def __post_init__(self, options: Mapping[str, Any]):
        super().__post_init__(options)

        if isinstance(self.meta_fields, str):
            self.meta_fields = InterpolatedString(self.meta_fields, default=None, options=options).eval(self.config)
            if not isinstance(self.meta_fields, List):
                self.meta_fields = None

    def select_records(
        self,
        response: requests.Response,
        stream_state: StreamState,
        stream_slice: Optional[StreamSlice] = None,
        next_page_token: Optional[Mapping[str, Any]] = None,
    ) -> List[Record]:
        records = super().select_records(response, stream_state, stream_slice, next_page_token)

        selected_records = []

        for record in records:
            meta_fields = self.meta_fields or self._default_meta_fields
            data_set = record["data"] if isinstance(record["data"], list) else [record["data"]]
            for data_element in data_set:
                selected_records.append(
                    {**data_element, **{_field: value for _field, value in record["meta"].items() if _field in meta_fields}}
                )

        return selected_records


@dataclass
class RailzAiIncrementalReportsSelector(RailzAiReportsSelector):
    _default_meta_fields = ("reportId", "startDate", "endDate")
