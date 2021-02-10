"""
psycopg3 named cursor objects (server-side cursors)
"""

# Copyright (C) 2020-2021 The Psycopg Team

import warnings
from types import TracebackType
from typing import Any, AsyncIterator, Generic, List, Iterator, Optional
from typing import Sequence, Type, Tuple, TYPE_CHECKING

from . import sql
from .pq import Format
from .cursor import BaseCursor, execute
from .proto import ConnectionType, Query, Params, PQGen

if TYPE_CHECKING:
    from .connection import BaseConnection  # noqa: F401
    from .connection import Connection, AsyncConnection  # noqa: F401

DEFAULT_ITERSIZE = 100


class NamedCursorHelper(Generic[ConnectionType]):
    __slots__ = ("name", "described")
    """Helper object for common NamedCursor code.

    TODO: this should be a mixin, but couldn't find a way to work it
    correctly with the generic.
    """

    def __init__(self, name: str):
        self.name = name
        self.described = False

    def _declare_gen(
        self,
        cur: BaseCursor[ConnectionType],
        query: Query,
        params: Optional[Params] = None,
    ) -> PQGen[None]:
        """Generator implementing `NamedCursor.execute()`."""
        conn = cur._conn
        yield from cur._start_query(query)
        pgq = cur._convert_query(query, params)
        cur._execute_send(pgq)
        results = yield from execute(conn.pgconn)
        cur._execute_results(results)

        # The above result is an COMMAND_OK. Get the cursor result shape
        yield from self._describe_gen(cur)

    def _describe_gen(self, cur: BaseCursor[ConnectionType]) -> PQGen[None]:
        conn = cur._conn
        conn.pgconn.send_describe_portal(
            self.name.encode(conn.client_encoding)
        )
        results = yield from execute(conn.pgconn)
        cur._execute_results(results)
        self.described = True

    def _close_gen(self, cur: BaseCursor[ConnectionType]) -> PQGen[None]:
        query = sql.SQL("close {}").format(sql.Identifier(self.name))
        yield from cur._conn._exec_command(query)

    def _fetch_gen(
        self, cur: BaseCursor[ConnectionType], num: Optional[int]
    ) -> PQGen[List[Tuple[Any, ...]]]:
        # If we are stealing the cursor, make sure we know its shape
        if not self.described:
            yield from cur._start_query()
            yield from self._describe_gen(cur)

        if num is not None:
            howmuch: sql.Composable = sql.Literal(num)
        else:
            howmuch = sql.SQL("all")

        query = sql.SQL("fetch forward {} from {}").format(
            howmuch, sql.Identifier(self.name)
        )
        res = yield from cur._conn._exec_command(query)

        # TODO: loaders don't need to be refreshed
        cur.pgresult = res
        return cur._tx.load_rows(0, res.ntuples)

    def _scroll_gen(
        self, cur: BaseCursor[ConnectionType], value: int, mode: str
    ) -> PQGen[None]:
        if mode not in ("relative", "absolute"):
            raise ValueError(
                f"bad mode: {mode}. It should be 'relative' or 'absolute'"
            )
        query = sql.SQL("move{} {} from {}").format(
            sql.SQL(" absolute" if mode == "absolute" else ""),
            sql.Literal(value),
            sql.Identifier(self.name),
        )
        yield from cur._conn._exec_command(query)

    def _make_declare_statement(
        self,
        cur: BaseCursor[ConnectionType],
        query: Query,
        scrollable: bool,
        hold: bool,
    ) -> sql.Composable:
        if isinstance(query, bytes):
            query = query.decode(cur._conn.client_encoding)
        if not isinstance(query, sql.Composable):
            query = sql.SQL(query)

        return sql.SQL(
            "declare {name} {scroll} cursor{hold} for {query}"
        ).format(
            name=sql.Identifier(self.name),
            scroll=sql.SQL("scroll" if scrollable else "no scroll"),
            hold=sql.SQL(" with hold" if hold else ""),
            query=query,
        )


class NamedCursor(BaseCursor["Connection"]):
    __module__ = "psycopg3"
    __slots__ = ("_helper", "itersize")

    def __init__(
        self,
        connection: "Connection",
        name: str,
        *,
        format: Format = Format.TEXT,
    ):
        super().__init__(connection, format=format)
        self._helper: NamedCursorHelper["Connection"] = NamedCursorHelper(name)
        self.itersize = DEFAULT_ITERSIZE

    def __del__(self) -> None:
        if not self._closed:
            warnings.warn(
                f"named cursor {self} was deleted while still open."
                f" Please use 'with' or '.close()' to close the cursor properly",
                ResourceWarning,
            )

    def __enter__(self) -> "NamedCursor":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()

    @property
    def name(self) -> str:
        return self._helper.name

    def close(self) -> None:
        """
        Close the current cursor and free associated resources.
        """
        with self._conn.lock:
            self._conn.wait(self._helper._close_gen(self))
        self._close()

    def execute(
        self,
        query: Query,
        params: Optional[Params] = None,
        *,
        scrollable: bool = True,
        hold: bool = False,
    ) -> "NamedCursor":
        """
        Execute a query or command to the database.
        """
        query = self._helper._make_declare_statement(
            self, query, scrollable=scrollable, hold=hold
        )
        with self._conn.lock:
            self._conn.wait(self._helper._declare_gen(self, query, params))
        return self

    def fetchone(self) -> Optional[Sequence[Any]]:
        with self._conn.lock:
            recs = self._conn.wait(self._helper._fetch_gen(self, 1))
        if recs:
            self._pos += 1
            return recs[0]
        else:
            return None

    def fetchmany(self, size: int = 0) -> Sequence[Sequence[Any]]:
        if not size:
            size = self.arraysize
        with self._conn.lock:
            recs = self._conn.wait(self._helper._fetch_gen(self, size))
        self._pos += len(recs)
        return recs

    def fetchall(self) -> Sequence[Sequence[Any]]:
        with self._conn.lock:
            recs = self._conn.wait(self._helper._fetch_gen(self, None))
        self._pos += len(recs)
        return recs

    def __iter__(self) -> Iterator[Sequence[Any]]:
        while True:
            with self._conn.lock:
                recs = self._conn.wait(
                    self._helper._fetch_gen(self, self.itersize)
                )
            for rec in recs:
                self._pos += 1
                yield rec
            if len(recs) < self.itersize:
                break

    def scroll(self, value: int, mode: str = "relative") -> None:
        with self._conn.lock:
            self._conn.wait(self._helper._scroll_gen(self, value, mode))
        # Postgres doesn't have a reliable way to report a cursor out of bound
        if mode == "relative":
            self._pos += value
        else:
            self._pos = value


class AsyncNamedCursor(BaseCursor["AsyncConnection"]):
    __module__ = "psycopg3"
    __slots__ = ("_helper", "itersize")

    def __init__(
        self,
        connection: "AsyncConnection",
        name: str,
        *,
        format: Format = Format.TEXT,
    ):
        super().__init__(connection, format=format)
        self._helper: NamedCursorHelper["AsyncConnection"]
        self._helper = NamedCursorHelper(name)
        self.itersize = DEFAULT_ITERSIZE

    def __del__(self) -> None:
        if not self._closed:
            warnings.warn(
                f"named cursor {self} was deleted while still open."
                f" Please use 'with' or '.close()' to close the cursor properly",
                ResourceWarning,
            )

    async def __aenter__(self) -> "AsyncNamedCursor":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    @property
    def name(self) -> str:
        return self._helper.name

    async def close(self) -> None:
        """
        Close the current cursor and free associated resources.
        """
        async with self._conn.lock:
            await self._conn.wait(self._helper._close_gen(self))
        self._close()

    async def execute(
        self,
        query: Query,
        params: Optional[Params] = None,
        *,
        scrollable: bool = True,
        hold: bool = False,
    ) -> "AsyncNamedCursor":
        """
        Execute a query or command to the database.
        """
        query = self._helper._make_declare_statement(
            self, query, scrollable=scrollable, hold=hold
        )
        async with self._conn.lock:
            await self._conn.wait(
                self._helper._declare_gen(self, query, params)
            )
        return self

    async def fetchone(self) -> Optional[Sequence[Any]]:
        async with self._conn.lock:
            recs = await self._conn.wait(self._helper._fetch_gen(self, 1))
        if recs:
            self._pos += 1
            return recs[0]
        else:
            return None

    async def fetchmany(self, size: int = 0) -> Sequence[Sequence[Any]]:
        if not size:
            size = self.arraysize
        async with self._conn.lock:
            recs = await self._conn.wait(self._helper._fetch_gen(self, size))
        self._pos += len(recs)
        return recs

    async def fetchall(self) -> Sequence[Sequence[Any]]:
        async with self._conn.lock:
            recs = await self._conn.wait(self._helper._fetch_gen(self, None))
        self._pos += len(recs)
        return recs

    async def __aiter__(self) -> AsyncIterator[Sequence[Any]]:
        while True:
            async with self._conn.lock:
                recs = await self._conn.wait(
                    self._helper._fetch_gen(self, self.itersize)
                )
            for rec in recs:
                self._pos += 1
                yield rec
            if len(recs) < self.itersize:
                break

    async def scroll(self, value: int, mode: str = "relative") -> None:
        async with self._conn.lock:
            await self._conn.wait(self._helper._scroll_gen(self, value, mode))
