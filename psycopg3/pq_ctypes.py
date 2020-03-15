#!/usr/bin/env python3
"""
libpq Python wrapper using ctypes bindings.

Clients shouldn't use this module directly, unless for testing: they should use
the `pq` module instead, which is in charge of choosing the best
implementation.
"""

# Copyright (C) 2020 The Psycopg Team

from collections import namedtuple
from ctypes import c_char_p, pointer

from .pq_enums import (
    ConnStatus,
    PollingStatus,
    ExecStatus,
    TransactionStatus,
    Ping,
)
from .pq_encodings import py_codecs
from . import _pq_ctypes as impl


class PQerror(Exception):
    pass


class PGconn:
    __slots__ = ("pgconn_ptr",)

    def __init__(self, pgconn_ptr):
        self.pgconn_ptr = pgconn_ptr

    def __del__(self):
        self.finish()

    @classmethod
    def connect(cls, conninfo):
        if isinstance(conninfo, str):
            conninfo = conninfo.encode("utf8")
        if not isinstance(conninfo, bytes):
            raise TypeError(f"bytes expected, got {conninfo!r} instead")

        pgconn_ptr = impl.PQconnectdb(conninfo)
        return cls(pgconn_ptr)

    @classmethod
    def connect_start(cls, conninfo):
        if isinstance(conninfo, str):
            conninfo = conninfo.encode("utf8")
        if not isinstance(conninfo, bytes):
            raise TypeError(f"bytes expected, got {conninfo!r} instead")

        pgconn_ptr = impl.PQconnectStart(conninfo)
        return cls(pgconn_ptr)

    def connect_poll(self):
        rv = impl.PQconnectPoll(self.pgconn_ptr)
        return PollingStatus(rv)

    def finish(self):
        self.pgconn_ptr, p = None, self.pgconn_ptr
        if p is not None:
            impl.PQfinish(p)

    @property
    def info(self):
        opts = impl.PQconninfo(self.pgconn_ptr)
        if not opts:
            raise MemoryError("couldn't allocate connection info")
        try:
            return Conninfo._options_from_array(opts)
        finally:
            impl.PQconninfoFree(opts)

    def reset(self):
        impl.PQreset(self.pgconn_ptr)

    def reset_start(self):
        rv = impl.PQresetStart(self.pgconn_ptr)
        if rv == 0:
            raise PQerror("couldn't reset connection")

    def reset_poll(self):
        rv = impl.PQresetPoll(self.pgconn_ptr)
        return PollingStatus(rv)

    @classmethod
    def ping(self, conninfo):
        if isinstance(conninfo, str):
            conninfo = conninfo.encode("utf8")
        if not isinstance(conninfo, bytes):
            raise TypeError(f"bytes expected, got {conninfo!r} instead")

        rv = impl.PQping(conninfo)
        return Ping(rv)

    @property
    def db(self):
        return self._decode(impl.PQdb(self.pgconn_ptr))

    @property
    def user(self):
        return self._decode(impl.PQuser(self.pgconn_ptr))

    @property
    def password(self):
        return self._decode(impl.PQpass(self.pgconn_ptr))

    @property
    def host(self):
        return self._decode(impl.PQhost(self.pgconn_ptr))

    @property
    def hostaddr(self):
        return self._decode(impl.PQhostaddr(self.pgconn_ptr))

    @property
    def port(self):
        return self._decode(impl.PQport(self.pgconn_ptr))

    @property
    def tty(self):
        return self._decode(impl.PQtty(self.pgconn_ptr))

    @property
    def options(self):
        return self._decode(impl.PQoptions(self.pgconn_ptr))

    @property
    def status(self):
        rv = impl.PQstatus(self.pgconn_ptr)
        return ConnStatus(rv)

    @property
    def transaction_status(self):
        rv = impl.PQtransactionStatus(self.pgconn_ptr)
        return TransactionStatus(rv)

    def parameter_status(self, name):
        rv = impl.PQparameterStatus(
            self.pgconn_ptr, self._encode(name, "utf8")
        )
        return self._decode(rv, "utf8")

    @property
    def protocol_version(self):
        return impl.PQprotocolVersion(self.pgconn_ptr)

    @property
    def server_version(self):
        return impl.PQserverVersion(self.pgconn_ptr)

    @property
    def error_message(self):
        return self._decode(impl.PQerrorMessage(self.pgconn_ptr))

    @property
    def socket(self):
        return impl.PQsocket(self.pgconn_ptr)

    @property
    def backend_pid(self):
        return impl.PQbackendPID(self.pgconn_ptr)

    @property
    def needs_password(self):
        return bool(impl.PQconnectionNeedsPassword(self.pgconn_ptr))

    @property
    def used_password(self):
        return bool(impl.PQconnectionUsedPassword(self.pgconn_ptr))

    @property
    def ssl_in_use(self):
        return bool(impl.PQsslInUse(self.pgconn_ptr))

    def exec_(self, command):
        rv = impl.PQexec(self.pgconn_ptr, self._encode(command))
        if rv is None:
            raise MemoryError("couldn't allocate PGresult")
        return PGresult(rv)

    def _encode(self, s, py_enc=None):
        if isinstance(s, bytes):
            return s
        elif isinstance(s, str):
            if py_enc is None:
                pg_enc = self.parameter_status("client_encoding")
                py_enc = py_codecs[pg_enc]
                if py_enc is None:
                    raise PQerror(
                        f"PostgreSQL encoding {pg_enc} doesn't have a Python codec."
                        f" Please use bytes instead of str"
                    )
            return s.encode(py_enc)
        else:
            raise TypeError(f"expected bytes or str, got {s!r} instead")

    def _decode(self, b, py_enc=None):
        if b is None:
            return None

        if py_enc is None:
            pg_enc = self.parameter_status("client_encoding")
            py_enc = py_codecs[pg_enc]

        if py_enc is not None:
            return b.decode(py_enc)
        else:
            # pretty much a punt, but this is only for communication, no data
            return b.decode("utf8", "replace")


class PGresult:
    __slots__ = ("pgresult_ptr",)

    def __init__(self, pgresult_ptr):
        self.pgresult_ptr = pgresult_ptr

    def __del__(self):
        self.clear()

    def clear(self):
        self.pgresult_ptr, p = None, self.pgresult_ptr
        if p is not None:
            impl.PQclear(p)

    @property
    def status(self):
        rv = impl.PQresultStatus(self.pgresult_ptr)
        return ExecStatus(rv)


ConninfoOption = namedtuple(
    "ConninfoOption", "keyword envvar compiled val label dispatcher dispsize"
)


class Conninfo:
    @classmethod
    def get_defaults(cls):
        opts = impl.PQconndefaults()
        if not opts:
            raise MemoryError("couldn't allocate connection defaults")
        try:
            return cls._options_from_array(opts)
        finally:
            impl.PQconninfoFree(opts)

    @classmethod
    def parse(cls, conninfo):
        if isinstance(conninfo, str):
            conninfo = conninfo.encode("utf8")
        if not isinstance(conninfo, bytes):
            raise TypeError(f"bytes expected, got {conninfo!r} instead")

        errmsg = c_char_p()
        rv = impl.PQconninfoParse(conninfo, pointer(errmsg))
        if not rv:
            if not errmsg:
                raise MemoryError("couldn't allocate on conninfo parse")
            else:
                exc = PQerror(errmsg.value.decode("utf8", "replace"))
                impl.PQfreemem(errmsg)
                raise exc

        try:
            return cls._options_from_array(rv)
        finally:
            impl.PQconninfoFree(rv)

    @classmethod
    def _options_from_array(cls, opts):
        def gets(opt, kw):
            rv = getattr(opt, kw)
            if rv is not None:
                rv = rv.decode("utf8", "replace")
            return rv

        rv = []
        skws = "keyword envvar compiled val label dispatcher".split()
        for opt in opts:
            if not opt.keyword:
                break
            d = {kw: gets(opt, kw) for kw in skws}
            d["dispsize"] = opt.dispsize
            rv.append(ConninfoOption(**d))

        return rv