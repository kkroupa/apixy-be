from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Annotated, Any, Dict, Final, Literal, Mapping, Optional, Type, Union

import aiohttp
import async_timeout
import databases
import jmespath
import motor.motor_asyncio
from pydantic import AnyUrl, BaseModel, Field, HttpUrl, validator

from apixy.cache import redis_cache
from apixy.entities.shared import ForbidExtraModel, OmitFieldsConfig

from .validators import validate_nonzero_length

logger = logging.getLogger(__name__)


class DataSourceFetchError(Exception):
    """
    Unified exception for datasource fetch method to raise in case of fetching failure.
    To be caught by Project.fetch_data()
    """


class DataSource(ForbidExtraModel):
    """
    An interface for fetching data from a remote source

    :param name: datasource name for displaying/caching purposes
    :param url: URI (http(s), database etc)
    :param jsonpath: JMESPath (https://jmespath.org/) query string
    :param timeout: a float timeout value (in seconds)
    :param cache_expire: an int value for cache expiration time (in seconds).
                         Does not expire if 0.
    """

    class Config:
        orm_mode = True

    id: Optional[int]
    type: str
    name: str
    url: AnyUrl
    jsonpath: str
    timeout: Optional[float] = Field(60, gt=0.0)
    cache_expire: Optional[int] = Field(None, ge=0)

    @validator("jsonpath")
    @classmethod
    def validate_json_path(cls, value: str) -> str:
        """Validator for jmespath strings"""
        try:
            jmespath.compile(value)
            return value
        except jmespath.exceptions.ParseError as exception:
            raise ValueError("Invalid JsonPath") from exception

    @abstractmethod
    async def fetch_data(self) -> Any:
        """
        Fetches data as defined by the instance's attributes

        :raises asyncio.exceptions.TimeoutError: on timeout
        :raises DataSourceFetchError: on inability to fetch data or some another
                                      fetch-related problem
        """


class HTTPDataSource(DataSource):
    """A datasource that fetches data from an external API."""

    url: HttpUrl
    method: Literal["GET", "POST", "PUT", "DELETE"]
    body: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, Any]] = None
    type: Annotated[str, Field(regex=r"^http$")] = "http"

    _body_headers_not_empty = validator("body", "headers", allow_reuse=True)(
        validate_nonzero_length
    )

    @redis_cache
    async def fetch_data(self) -> Any:
        async with async_timeout.timeout(self.timeout):
            try:
                async with aiohttp.request(
                    method=self.method,
                    url=self.url,
                    json=self.body,
                    headers=self.headers,
                ) as response:
                    data = await response.json()
            except aiohttp.ClientError as error:
                logger.exception(error)
                raise DataSourceFetchError from error

        return jmespath.compile(self.jsonpath).search(data)


class MongoDBDataSource(DataSource):
    """A datasource that fetches data from a MongoDB collection."""

    database: str
    collection: str
    query: Dict[str, Any] = {}
    type: Annotated[str, Field(regex=r"^mongo$")] = "mongo"

    @redis_cache
    async def fetch_data(self) -> Any:
        client = motor.motor_asyncio.AsyncIOMotorClient(self.url)
        async with async_timeout.timeout(self.timeout):
            cursor = client[self.database][self.collection].find(
                self.query, {"_id": False}
            )
            try:
                data = await cursor.to_list(None)
            finally:
                await cursor.close()

        return jmespath.compile(self.jsonpath).search(data)


class SQLDataSource(DataSource):
    """A datasource that fetches data from SQL database."""

    query: str
    type: Annotated[str, Field(regex=r"^sql$")] = "sql"

    # TODO: sql validator (allow only select)

    @redis_cache
    async def fetch_data(self) -> Any:
        async with async_timeout.timeout(self.timeout):
            try:
                async with databases.Database(self.url) as database:
                    rows = await database.fetch_all(query=self.query)
            except RuntimeError as error:
                logger.exception(error)
                raise DataSourceFetchError from error

        return jmespath.compile(self.jsonpath).search([dict(row) for row in rows])


class HTTPDataSourceInput(HTTPDataSource):
    class Config(OmitFieldsConfig, DataSource.Config):
        omit_fields = ("id",)


class MongoDBDataSourceInput(MongoDBDataSource):
    class Config(OmitFieldsConfig, DataSource.Config):
        omit_fields = ("id",)


class SQLDataSourceInput(SQLDataSource):
    class Config(OmitFieldsConfig, DataSource.Config):
        omit_fields = ("id",)


class DataSourceUnion(BaseModel):
    __root__: Union[HTTPDataSource, MongoDBDataSource, SQLDataSource]


DataSourceInput = Union[HTTPDataSourceInput, MongoDBDataSourceInput, SQLDataSourceInput]

DATA_SOURCES: Final[Mapping[str, Type[DataSource]]] = {
    "http": HTTPDataSource,
    "mongo": MongoDBDataSource,
    "sql": SQLDataSource,
}
