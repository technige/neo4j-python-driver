#!/usr/bin/env python
# -*- encoding: utf-8 -*-

# Copyright (c) 2002-2019 "Neo4j,"
# Neo4j Sweden AB [http://neo4j.com]
#
# This file is part of Neo4j.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from collections import deque
from inspect import iscoroutinefunction
from logging import getLogger
from warnings import warn

from neo4j.api import Bookmark, Version
from neo4j.aio.bolt import Bolt, Addressable
from neo4j.aio.bolt.error import BoltError, BoltConnectionLost, BoltTransactionError, BoltFailure
from neo4j.data import Record
from neo4j.packstream import PackStream, Structure


log = getLogger("neo4j")


class IgnoredType:

    def __new__(cls):
        return Ignored

    def __bool__(self):
        return False

    def __repr__(self):
        return "Ignored"


Ignored = object.__new__(IgnoredType)


class Summary:

    def __init__(self, metadata, success):
        self._metadata = metadata
        self._success = bool(success)

    def __bool__(self):
        return self._success

    def __repr__(self):
        return "<{} {}>".format(
            self.__class__.__name__,
            " ".join("{}={!r}".format(k, v) for k, v in sorted(self._metadata.items())))

    @property
    def metadata(self):
        return self._metadata

    @property
    def success(self):
        return self._success


class Response:
    """ Collector for response data, consisting of an optional
    sequence of records and a mandatory summary.
    """

    result = None

    def __init__(self, courier):
        self._courier = courier
        self._records = deque()
        self._summary = None

    def put_record(self, record):
        """ Append a record to the end of the record deque.

        :param record:
        """
        self._records.append(record)

    async def get_record(self):
        """ Fetch and return the next record from the top of the
        record deque.

        :return:
        """

        # R = has records
        # S = has summary
        #
        # R=0, S=0 - fetch, check again
        # R=1, S=0 - pop
        # R=0, S=1 - raise stop
        # R=1, S=1 - pop
        while True:
            try:
                return self._records.popleft()
            except IndexError:
                if self._summary is None:
                    await self._courier.fetch(stop=lambda: bool(self._records))
                else:
                    return None

    def put_summary(self, summary):
        """ Update the stored summary value.

        :param summary:
        """
        self._summary = summary

    async def get_summary(self):
        """ Fetch and return the summary value.

        :return:
        """
        await self._courier.fetch(stop=lambda: self._summary is not None)
        return self._summary


class Result:
    """ The result of a Cypher execution.
    """

    def __init__(self, tx, head, body):
        self._tx = tx
        self._head = head
        self._body = body
        self._head.result = self
        self._body.result = self

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            values = await self._body.get_record()
        except BoltFailure as failure:
            # FAILURE
            await self._tx.fail(failure)
        else:
            # RECORD or end of records
            if values is None:
                raise StopAsyncIteration
            else:
                return Record(zip(await self.fields(), values))

    @property
    def transaction(self):
        return self._tx

    async def get_header(self):
        try:
            header = await self._head.get_summary()
        except BoltFailure as failure:
            # FAILURE
            await self._tx.fail(failure)
        else:
            # SUCCESS or IGNORED
            return header

    async def consume(self):
        try:
            footer = await self._body.get_summary()
        except BoltFailure as failure:
            # FAILURE
            await self._tx.fail(failure)
        else:
            # SUCCESS or IGNORED
            # The return value of this function can be used as a
            # predicate, since SUCCESS will return a Summary that
            # coerces to True, and IGNORED will return Ignored, which
            # coerces to False.
            return footer

    async def fields(self):
        header = await self.get_header()
        return header.metadata.get("fields", ())

    async def single(self):
        """ Obtain the next and only remaining record from this result.

        A warning is generated if more than one record is available but
        the first of these is still returned.

        :return: the next :class:`.Record` or :const:`None` if no
            records remain
        :warn: if more than one record is available
        """
        records = [record async for record in self]
        size = len(records)
        if size == 0:
            return None
        if size != 1:
            warn("Expected a result with a single record, but this result contains %d" % size)
        return records[0]


class Transaction:

    @classmethod
    async def begin(cls, courier, readonly=False, bookmarks=None,
                    timeout=None, metadata=None):
        """ Begin an explicit transaction.
        """
        tx = cls(courier, readonly=readonly, bookmarks=bookmarks, timeout=timeout,
                 metadata=metadata)
        tx._autocommit = False
        courier.write_begin(tx._extras)
        if bookmarks:
            # If bookmarks are passed, BEGIN should sync to the
            # network. This ensures that any failures that occur are
            # raised at an appropriate time, rather than later in the
            # transaction. Conversely, if no bookmarks are passed, it
            # should be fine to sync lazily.
            await courier.send()
            await courier.fetch()
        return tx

    def _add_extra(self, key, coercion=lambda x: x, **values):
        for name, value in values.items():
            if value:
                try:
                    self._extras[key] = coercion(value)
                except TypeError:
                    raise TypeError("Unsupported type for {} {!r}".format(name, value))

    def __init__(self, courier, readonly=False, bookmarks=None, timeout=None, metadata=None):
        """

        :param courier:
        :param readonly: if true, the transaction should be readonly,
            otherwise it should have full read/write access
        :param bookmarks: iterable of bookmarks which must all have
            been seen by the server before this transaction begins
        :param timeout: a transaction execution timeout, passed to the
            database kernel on execution
        :param metadata: application metadata tied to this transaction;
            generally used for audit purposes
        """
        self._courier = courier
        self._autocommit = True
        self._closed = False
        self._failure = None
        self._extras = {}
        self._add_extra("mode", lambda x: "R" if x else None, readonly=readonly)
        self._add_extra("bookmarks", list, bookmarks=bookmarks)
        self._add_extra("tx_timeout", lambda x: int(1000 * x), timeout=timeout)
        self._add_extra("tx_metadata", dict, metadata=metadata)

    @property
    def autocommit(self):
        return self._autocommit

    @property
    def closed(self):
        return self._closed

    @property
    def failure(self):
        return self._failure

    async def run(self, cypher, parameters=None, discard=False):
        self._assert_open()
        head = self._courier.write_run(cypher, dict(parameters or {}),
                                       self._extras if self._autocommit else {})
        if discard:
            body = self._courier.write_discard_all()
        else:
            body = self._courier.write_pull_all()
        if self._autocommit:
            try:
                await self._courier.send()
            finally:
                self._closed = True
        return Result(self, head, body)

    async def evaluate(self, cypher, parameters=None, key=0, default=None):
        """ Run Cypher and return a single value (by default the first
        value) from the first and only record.
        """
        result = await self.run(cypher, parameters)
        record = await result.single()
        return record.value(key, default)

    async def commit(self):
        self._assert_open()
        if self._autocommit:
            raise BoltTransactionError("Cannot explicitly commit an auto-commit "
                                       "transaction", self._courier.remote_address)
        try:
            commit = self._courier.write_commit()
            await self._courier.send()
            await self._courier.fetch()
            summary = await commit.get_summary()
            return Bookmark(summary.metadata.get("bookmark"))
        finally:
            self._closed = True

    async def rollback(self):
        self._assert_open()
        if self._autocommit:
            raise BoltTransactionError("Cannot explicitly rollback an auto-commit "
                                       "transaction", self._courier.remote_address)
        try:
            self._courier.write_rollback()
            await self._courier.send()
            await self._courier.fetch()
        finally:
            self._closed = True

    async def fail(self, failure):
        """ Called internally with a BoltFailure object when a FAILURE
        message is received. This will reset the connection, close the
        transaction and raise the failure exception.

        :param failure:
        :return:
        """
        if not self._failure:
            self._courier.write_reset()
            await self._courier.send()
            await self._courier.fetch()
            self._closed = True
            self._failure = failure
            raise self._failure

    def _assert_open(self):
        if self.closed:
            raise BoltTransactionError("Transaction is already "
                                       "closed", self._courier.remote_address)


class Courier(Addressable, object):

    def __init__(self, reader, writer):
        self.stream = PackStream(reader, writer)
        self.responses = deque()
        Addressable._set_transport(self, writer.transport)

    @property
    def connection_id(self):
        return self.local_address.port_number

    def write_hello(self, extras):
        logged_extras = dict(extras)
        if "credentials" in logged_extras:
            logged_extras["credentials"] = "*******"
        log.debug("[#%04X] C: HELLO %r", self.connection_id, logged_extras)
        return self._write(Structure(b"\x01", extras))

    def write_goodbye(self):
        log.debug("[#%04X] C: GOODBYE", self.connection_id)
        return self._write(Structure(b"\x02"))

    def write_reset(self):
        log.debug("[#%04X] C: RESET", self.connection_id)
        return self._write(Structure(b"\x0F"))

    def write_run(self, cypher, parameters, extras):
        parameters = dict(parameters or {})
        extras = dict(extras or {})
        log.debug("[#%04X] C: RUN %r %r %r", self.connection_id, cypher, parameters, extras)
        return self._write(Structure(b"\x10", cypher, parameters, extras))

    def write_begin(self, extras):
        log.debug("[#%04X] C: BEGIN %r", self.connection_id, extras)
        return self._write(Structure(b"\x11", extras))

    def write_commit(self):
        log.debug("[#%04X] C: COMMIT", self.connection_id)
        return self._write(Structure(b"\x12"))

    def write_rollback(self):
        log.debug("[#%04X] C: ROLLBACK", self.connection_id)
        return self._write(Structure(b"\x13"))

    def write_discard_all(self):
        log.debug("[#%04X] C: DISCARD_ALL", self.connection_id)
        return self._write(Structure(b"\x2F"))

    def write_pull_all(self):
        log.debug("[#%04X] C: PULL_ALL", self.connection_id)
        return self._write(Structure(b"\x3F"))

    def _write(self, message):
        self.stream.write_message(message)
        response = Response(self)
        self.responses.append(response)
        return response

    async def send(self):
        log.debug("[#%04X] C: <SEND>", self.connection_id)
        await self.stream.drain()

    async def fetch(self, stop=lambda: None):
        """ Fetch zero or more messages, stopping when no more pending
        responses need to be populated, when the stop condition
        is fulfilled, or when a failure is encountered (for which an
        exception will be raised).

        :param stop:
        :param result:
        """
        while self.responses and not stop():
            fetched = await self._read()
            if isinstance(fetched, list):
                self.responses[0].put_record(fetched)
            else:
                response = self.responses.popleft()
                response.put_summary(fetched)
                if isinstance(fetched, Summary) and not fetched.success:
                    code = fetched.metadata.get("code")
                    message = fetched.metadata.get("message")
                    raise BoltFailure(message, self.remote_address, code, response)

    async def _read(self):
        message = await self.stream.read_message()
        if not isinstance(message, Structure):
            # TODO: log, signal defunct and close
            raise BoltError("Received illegal message "
                            "type {}".format(type(message)), self.remote_address)
        if message.tag == b"\x70":
            metadata = message.fields[0]
            log.debug("[#%04X] S: SUCCESS %r", self.connection_id, metadata)
            return Summary(metadata, success=True)
        elif message.tag == b"\x71":
            data = message.fields[0]
            log.debug("[#%04X] S: RECORD %r", self.connection_id, data)
            return data
        elif message.tag == b"\x7E":
            log.debug("[#%04X] S: IGNORED", self.connection_id)
            return Ignored
        elif message.tag == b"\x7F":
            metadata = message.fields[0]
            log.debug("[#%04X] S: FAILURE %r", self.connection_id, metadata)
            return Summary(metadata, success=False)
        else:
            # TODO: log, signal defunct and close
            raise BoltError("Received illegal message structure "
                            "tag {}".format(message.tag), self.remote_address)


class Bolt3(Bolt):

    protocol_version = Version(3, 0)

    server_agent = None

    connection_id = None

    def __init__(self, reader, writer):
        self._courier = Courier(reader, writer)
        self._tx = None

    async def __ainit__(self, auth):
        args = {
            "scheme": "none",
            "user_agent": self.default_user_agent(),
        }
        if auth:
            args.update({
                "scheme": "basic",
                "principal": auth[0],  # TODO
                "credentials": auth[1],  # TODO
            })
        response = self._courier.write_hello(args)
        await self._courier.send()
        summary = await response.get_summary()
        if summary.success:
            self.server_agent = summary.metadata.get("server")
            self.connection_id = summary.metadata.get("connection_id")
            # TODO: verify genuine product
        else:
            await super().close()
            code = summary.metadata.get("code")
            message = summary.metadata.get("message")
            raise BoltFailure(message, self.remote_address, code, response)

    async def close(self):
        if self.closed:
            return
        self._courier.write_goodbye()
        try:
            await self._courier.send()
        except BoltConnectionLost:
            pass
        finally:
            await super().close()

    @property
    def ready(self):
        """ If true, this flag indicates that there is no transaction
        in progress, and one may be started.
        """
        return not self._tx or self._tx.closed

    def _assert_ready(self):
        if not self.ready:
            # TODO: add transaction identifier
            raise BoltTransactionError("A transaction is already in progress on "
                                       "this connection", self.remote_address)

    async def run(self, cypher, parameters=None, discard=False, readonly=False,
                  bookmarks=None, timeout=None, metadata=None):
        self._assert_ready()
        self._tx = Transaction(self._courier, readonly=readonly, bookmarks=bookmarks,
                               timeout=timeout, metadata=metadata)
        return await self._tx.run(cypher, parameters, discard=discard)

    async def begin(self, readonly=False, bookmarks=None,
                    timeout=None, metadata=None):
        self._assert_ready()
        self._tx = await Transaction.begin(self._courier, readonly=readonly, bookmarks=bookmarks,
                                           timeout=timeout, metadata=metadata)
        return self._tx

    async def run_tx(self, f, args=None, kwargs=None, readonly=False,
                     bookmarks=None, timeout=None, metadata=None):
        tx = await self.begin(readonly=readonly, bookmarks=bookmarks,
                              timeout=None, metadata=metadata)
        if not iscoroutinefunction(f):
            raise TypeError("Transaction function must be awaitable")
        try:
            value = await f(tx, *(args or ()), **(kwargs or {}))
        except Exception:
            await tx.rollback()
            raise
        else:
            await tx.commit()
            return value
