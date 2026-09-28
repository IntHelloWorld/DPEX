"""Disk-backed conversion for large Fullchain traces.

The SQLite file is an intermediate representation.  It deliberately keeps the
Java event protocol unchanged while avoiding Python object graphs proportional
to the raw trace size.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import struct
import tempfile
from functools import lru_cache
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Iterable, Iterator

import orjson
import zstandard

from dpex.domain.trace import (
    AGENT_PROTOCOL_VERSION,
    ALLOWED_EVENTS,
    ASSERTION_EVENTS,
    NOISE_PREFIXES,
    SCHEMA_VERSION,
    _descriptor_argument_count,
    _validate_arguments,
    _validate_value_item,
)
from dpex.infrastructure.io import (
    compress_zstd_file,
    read_json,
    write_compact_json,
)


TRACE_STORE_NAME = "trace.sqlite3"
TRACE_STORE_ARCHIVE_NAME = "trace.sqlite3.zst"
FINAL_TRACE_ARCHIVE_SUFFIX = ".zst"
METHOD_SUMMARY_NAME = "method_summary.json"
METHOD_SUMMARY_SCHEMA = "trace-method-summary"
METHOD_SUMMARY_VERSION = 1
SQLITE_CACHE_KIB = 64 * 1024
STREAM_BUFFER_BYTES = 16 * 1024 * 1024
FAST_RAW_THRESHOLD_BYTES = 256 * 1024 * 1024
FAST_MAX_DIRECT_CHILDREN = 65536
FAST_STORE_KIND = "raw-sidecar-v1"
QUERY_STORE_KIND = "query-store-v1"
DEGRADED_STORE_KIND = "degraded-query-store-v1"


_QUERY_SCHEMA = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE query_methods (
    method_key INTEGER PRIMARY KEY,
    method_id TEXT,
    class_name TEXT NOT NULL,
    method TEXT NOT NULL,
    descriptor TEXT NOT NULL,
    UNIQUE(class_name,method,descriptor)
);
CREATE TABLE query_nodes (
    invocation_id INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL,
    is_call INTEGER NOT NULL,
    method_key INTEGER NOT NULL,
    enter_seq INTEGER NOT NULL,
    exit_seq INTEGER NOT NULL,
    exit_type TEXT NOT NULL,
    origin_test_line INTEGER NOT NULL,
    arguments_json TEXT,
    result_json TEXT,
    exception_class TEXT NOT NULL,
    message TEXT NOT NULL,
    subtree_call_count INTEGER,
    structure_hash BLOB,
    structure_json TEXT
);
CREATE TABLE query_edges (
    parent_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    child_id INTEGER NOT NULL,
    PRIMARY KEY(parent_id,ordinal)
);
CREATE TABLE query_repetitions (
    parent_id INTEGER NOT NULL,
    start_ordinal INTEGER NOT NULL,
    pattern_length INTEGER NOT NULL,
    repeat_count INTEGER NOT NULL,
    PRIMARY KEY(parent_id,start_ordinal)
);
"""


_SCHEMA = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE invocations (
    invocation_id INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL,
    class_name TEXT NOT NULL,
    method TEXT NOT NULL,
    descriptor TEXT NOT NULL,
    thread_id INTEGER NOT NULL,
    thread_name TEXT NOT NULL,
    enter_seq INTEGER NOT NULL,
    enter_ns INTEGER NOT NULL,
    origin_test_line INTEGER NOT NULL,
    arguments_json TEXT
);
CREATE TABLE exits (
    invocation_id INTEGER PRIMARY KEY,
    seq INTEGER NOT NULL,
    ts_ns INTEGER NOT NULL,
    exit_type TEXT NOT NULL,
    duration_ns INTEGER NOT NULL,
    exception_class TEXT NOT NULL,
    message TEXT NOT NULL,
    return_value_json TEXT
);
CREATE TABLE excluded (
    invocation_id INTEGER PRIMARY KEY
);
CREATE TABLE calls (
    invocation_id INTEGER PRIMARY KEY
);
CREATE TABLE folded (
    invocation_id INTEGER PRIMARY KEY
);
CREATE TABLE assertions (
    seq INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    thread_id INTEGER NOT NULL,
    source_start_line INTEGER NOT NULL,
    source_end_line INTEGER NOT NULL
);
CREATE TABLE failures (
    seq INTEGER PRIMARY KEY,
    exception_class TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE lifecycle (
    seq INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


_FAST_SCHEMA = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE fast_methods (
    method_key INTEGER PRIMARY KEY,
    class_name TEXT NOT NULL,
    method TEXT NOT NULL,
    descriptor TEXT NOT NULL,
    UNIQUE(class_name,method,descriptor)
);
CREATE TABLE fast_nodes (
    invocation_id INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL,
    is_call INTEGER NOT NULL,
    method_key INTEGER NOT NULL,
    subtree_count INTEGER NOT NULL,
    exit_seq INTEGER NOT NULL,
    exit_type TEXT NOT NULL,
    result_json TEXT,
    children_blob BLOB
);
"""


def _json(value: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=sort_keys, separators=(",", ":")
    )


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    # Every store is a rebuildable intermediate protected by an atomic rename.
    # A rollback journal can be as large as the trace itself during derived-state
    # updates, so durability here only multiplies disk usage without preserving
    # a usable artifact after interruption.
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA page_size=32768")
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
    connection.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_KIB}")
    connection.execute("PRAGMA temp_store=FILE")
    return connection


def _put_metadata(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
        (key, _json(value)),
    )


def _metadata(connection: sqlite3.Connection, key: str) -> Any:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        raise ValueError(f"trace store metadata is missing {key}")
    return json.loads(str(row[0]))


def _metadata_or(
    connection: sqlite3.Connection, key: str, default: Any = None
) -> Any:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key=?", (key,)
    ).fetchone()
    return default if row is None else json.loads(str(row[0]))


def _open_zstd(path: Path):
    source = path.open("rb")
    compressed = zstandard.ZstdDecompressor().stream_reader(source)
    return source, compressed


def _iter_raw_lines(path: Path) -> Iterator[tuple[int, bytes]]:
    """Yield decompressed binary lines while still validating the full frame."""
    source = compressed = buffered = None
    try:
        if path.suffix == ".zst":
            source, compressed = _open_zstd(path)
            buffered = io.BufferedReader(compressed, buffer_size=1024 * 1024)
            binary = buffered
        else:
            source = path.open("rb")
            binary = source
        for line_number, line in enumerate(binary, 1):
            yield line_number, line
    except zstandard.ZstdError as error:
        raise ValueError(
            f"incomplete or corrupt Zstd trace stream: {error}"
        ) from error
    except OSError as error:
        raise ValueError(f"cannot stream trace events {path}: {error}") from error
    finally:
        if buffered is not None and not buffered.closed:
            buffered.close()
        if compressed is not None and not compressed.closed:
            compressed.close()
        if source is not None and not source.closed:
            source.close()


def iter_raw_events(path: Path) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """Yield one strict JSONL event at a time from plain or Zstd input."""
    for line_number, line in _iter_raw_lines(path):
        if not line.strip():
            continue
        try:
            event = orjson.loads(line)
        except orjson.JSONDecodeError as error:
            raise ValueError(
                f"invalid event JSON at line {line_number}: {error}"
            ) from error
        if not isinstance(event, dict):
            raise ValueError(f"event at line {line_number} is not an object")
        yield line_number, len(line), event


def _iter_raw_enter_events(path: Path) -> Iterator[dict[str, Any]]:
    """Second-pass reader that parses ENTER records and skips all other JSON."""
    prefix = b'{"type":"ENTER"'
    for line_number, line in _iter_raw_lines(path):
        if not line.startswith(prefix):
            continue
        try:
            event = orjson.loads(line)
        except orjson.JSONDecodeError as error:
            raise ValueError(
                f"invalid ENTER JSON at line {line_number}: {error}"
            ) from error
        if not isinstance(event, dict):
            raise ValueError(f"ENTER at line {line_number} is not an object")
        yield event


def _insert_invocation(
    connection: sqlite3.Connection, event: dict[str, Any]
) -> None:
    try:
        connection.execute(
            """INSERT INTO invocations(
                   invocation_id,parent_id,class_name,method,descriptor,
                   thread_id,thread_name,enter_seq,enter_ns,origin_test_line,
                   arguments_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(event["invocation_id"]),
                int(event.get("parent_id") or 0),
                str(event.get("class") or ""),
                str(event.get("method") or ""),
                str(event.get("descriptor") or ""),
                int(event.get("thread_id") or 0),
                str(event.get("thread_name") or ""),
                int(event.get("seq") or 0),
                int(event.get("ts_ns") or 0),
                int(event.get("origin_test_line") or 0),
                _json(event["arguments"])
                if isinstance(event.get("arguments"), dict)
                else None,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise ValueError(
            f"duplicate invocation id: {int(event.get('invocation_id') or 0)}"
        ) from error


def _insert_exit(connection: sqlite3.Connection, event: dict[str, Any]) -> None:
    invocation_id = int(event.get("invocation_id") or 0)
    try:
        connection.execute(
            "INSERT INTO exits VALUES (?,?,?,?,?,?,?,?)",
            (
                invocation_id,
                int(event.get("seq") or 0),
                int(event.get("ts_ns") or 0),
                str(event.get("type") or ""),
                int(event.get("duration_ns") or 0),
                str(event.get("exception_class") or ""),
                str(event.get("message") or ""),
                _json(event["return_value"])
                if isinstance(event.get("return_value"), dict)
                else None,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise ValueError(f"duplicate exit: invocation {invocation_id}") from error


def _invocation_row(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(event["invocation_id"]),
        int(event.get("parent_id") or 0),
        str(event.get("class") or ""),
        str(event.get("method") or ""),
        str(event.get("descriptor") or ""),
        int(event.get("thread_id") or 0),
        str(event.get("thread_name") or ""),
        int(event.get("seq") or 0),
        int(event.get("ts_ns") or 0),
        int(event.get("origin_test_line") or 0),
        _json(event["arguments"])
        if isinstance(event.get("arguments"), dict)
        else None,
    )


def _exit_row(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(event.get("invocation_id") or 0),
        int(event.get("seq") or 0),
        int(event.get("ts_ns") or 0),
        str(event.get("type") or ""),
        int(event.get("duration_ns") or 0),
        str(event.get("exception_class") or ""),
        str(event.get("message") or ""),
        _json(event["return_value"])
        if isinstance(event.get("return_value"), dict)
        else None,
    )


def _flush_event_rows(
    connection: sqlite3.Connection,
    invocations: list[tuple[Any, ...]],
    exits: list[tuple[Any, ...]],
    assertions: list[tuple[Any, ...]],
    failures: list[tuple[Any, ...]],
    lifecycle: list[tuple[Any, ...]],
    excluded: list[tuple[int]],
    calls: list[tuple[int]],
) -> None:
    try:
        connection.executemany(
            """INSERT INTO invocations(
                   invocation_id,parent_id,class_name,method,descriptor,
                   thread_id,thread_name,enter_seq,enter_ns,origin_test_line,
                   arguments_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            invocations,
        )
        connection.executemany(
            "INSERT INTO exits VALUES (?,?,?,?,?,?,?,?)", exits
        )
        connection.executemany(
            "INSERT INTO assertions VALUES (?,?,?,?,?,?)", assertions
        )
        connection.executemany(
            "INSERT INTO failures VALUES (?,?,?,?)", failures
        )
        connection.executemany(
            "INSERT INTO lifecycle VALUES (?,?,?)", lifecycle
        )
        connection.executemany(
            "INSERT INTO excluded(invocation_id) VALUES (?)", excluded
        )
        connection.executemany(
            "INSERT INTO calls(invocation_id) VALUES (?)", calls
        )
    except sqlite3.IntegrityError as error:
        raise ValueError(f"duplicate trace event identity: {error}") from error
    invocations.clear()
    exits.clear()
    assertions.clear()
    failures.clear()
    lifecycle.clear()
    excluded.clear()
    calls.clear()


@lru_cache(maxsize=8192)
def _is_noise_method(class_name: str, method: str) -> bool:
    qualified = f"{class_name}.{method}"
    return any(qualified.startswith(prefix) for prefix in NOISE_PREFIXES)


@lru_cache(maxsize=4096)
def _argument_count(descriptor: str) -> int:
    return _descriptor_argument_count(descriptor)


def _validate_capture_event(
    event: dict[str, Any],
    capture: dict[str, Any],
    active_descriptors: dict[int, str],
    *,
    validate_payload: bool = True,
) -> None:
    """Validate captured values once while the raw event is already in memory."""
    enabled = bool(capture["capture_values"])
    event_type = str(event.get("type") or "")
    invocation_id = int(event.get("invocation_id") or 0)
    if event_type == "ENTER":
        descriptor = str(event.get("descriptor") or "")
        arguments = event.get("arguments")
        if enabled != isinstance(arguments, dict):
            raise ValueError(
                f"inconsistent arguments capture: invocation {invocation_id}"
            )
        if enabled and validate_payload:
            _validate_arguments(
                arguments, _argument_count(descriptor), capture
            )
        active_descriptors[invocation_id] = descriptor
        return
    if event_type not in {"RETURN", "THROW"}:
        return
    descriptor = active_descriptors.pop(invocation_id, None)
    if descriptor is None:
        # The relational pass reports the stable protocol error with context.
        return
    returned = event.get("return_value")
    if event_type == "THROW":
        if returned is not None:
            raise ValueError(
                f"THROW invocation has return value: invocation {invocation_id}"
            )
        return
    if enabled != isinstance(returned, dict):
        raise ValueError(
            f"inconsistent return capture: invocation {invocation_id}"
        )
    if enabled and validate_payload:
        validated = _validate_value_item(returned, allow_index=False)
        descriptor_void = descriptor.endswith(")V")
        if (validated["kind"] == "void") != descriptor_void:
            raise ValueError(f"inconsistent void return: invocation {invocation_id}")


def _assertion_intervals(
    connection: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], int]:
    active: dict[int, tuple[Any, ...]] = {}
    occurrences: dict[str, int] = {}
    intervals: list[dict[str, Any]] = []
    unmatched = 0
    for row in connection.execute(
        """SELECT seq,event_type,assertion_id,thread_id,
                  source_start_line,source_end_line
           FROM assertions ORDER BY seq"""
    ):
        seq, event_type, assertion_id, thread_id, start_line, end_line = row
        thread_id = int(thread_id)
        if event_type == "ASSERT_START":
            if thread_id in active:
                unmatched += 1
            active[thread_id] = row
            continue
        start = active.pop(thread_id, None)
        if start is None or str(start[2]) != str(assertion_id):
            unmatched += 1
            continue
        assertion_id = str(assertion_id)
        occurrences[assertion_id] = occurrences.get(assertion_id, 0) + 1
        intervals.append({
            "assertion_id": assertion_id,
            "occurrence": occurrences[assertion_id],
            "outcome": "PASS" if event_type == "ASSERT_PASS" else "FAIL",
            "thread_id": thread_id,
            "source_start_line": int(start[4]),
            "source_end_line": int(start[5]),
            "start_seq": int(start[0]),
            "end_seq": int(seq),
        })
    return intervals, unmatched + len(active)


def _fold_assertions(connection: sqlite3.Connection) -> dict[str, Any]:
    intervals, unmatched = _assertion_intervals(connection)
    folded_count = 0
    for interval in intervals:
        if interval["outcome"] != "PASS":
            continue
        connection.execute("DROP TABLE IF EXISTS temp.fold_candidate")
        connection.execute("DROP TABLE IF EXISTS temp.fold_unsafe")
        connection.execute(
            "CREATE TEMP TABLE fold_candidate(invocation_id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TEMP TABLE fold_unsafe(invocation_id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            """INSERT INTO fold_candidate
               SELECT invocation.invocation_id
               FROM invocations AS invocation
               JOIN calls USING(invocation_id)
               JOIN exits USING(invocation_id)
               LEFT JOIN folded USING(invocation_id)
               WHERE folded.invocation_id IS NULL AND invocation.thread_id=?
                 AND invocation.enter_seq>? AND exits.seq<?
                 AND invocation.origin_test_line BETWEEN ? AND ?""",
            (
                interval["thread_id"], interval["start_seq"],
                interval["end_seq"], interval["source_start_line"],
                interval["source_end_line"],
            ),
        )
        connection.execute(
            """INSERT OR IGNORE INTO fold_unsafe
               SELECT candidate.invocation_id
               FROM fold_candidate AS candidate
               JOIN invocations AS invocation
                 ON invocation.invocation_id=candidate.invocation_id
               JOIN exits ON exits.invocation_id=candidate.invocation_id
               WHERE exits.exit_type!='RETURN'
                  OR EXISTS (
                      SELECT 1 FROM invocations AS child
                      JOIN calls AS child_call
                        ON child_call.invocation_id=child.invocation_id
                      LEFT JOIN folded AS child_folded
                        ON child_folded.invocation_id=child.invocation_id
                      WHERE child.parent_id=candidate.invocation_id
                        AND child_folded.invocation_id IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM fold_candidate AS nested
                            WHERE nested.invocation_id=child.invocation_id
                        )
                  )"""
        )
        while True:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO fold_unsafe
                   SELECT parent.invocation_id
                   FROM fold_candidate AS parent
                   JOIN invocations AS child
                     ON child.parent_id=parent.invocation_id
                   JOIN fold_unsafe AS unsafe
                     ON unsafe.invocation_id=child.invocation_id"""
            )
            if cursor.rowcount == 0:
                break
        cursor = connection.execute(
            """INSERT OR IGNORE INTO folded(invocation_id)
               SELECT candidate.invocation_id
               FROM fold_candidate AS candidate
               WHERE NOT EXISTS (
                   SELECT 1 FROM fold_unsafe AS unsafe
                   WHERE unsafe.invocation_id=candidate.invocation_id
               )"""
        )
        folded_count += max(0, cursor.rowcount)
    original = int(connection.execute(
        "SELECT count(*) FROM calls"
    ).fetchone()[0])
    retained = original - folded_count
    return {
        "original_call_count": original,
        "retained_call_count": retained,
        "folded_call_count": folded_count,
        "assertion_interval_count": len(intervals),
        "successful_assertion_count": sum(
            item["outcome"] == "PASS" for item in intervals
        ),
        "failed_assertion_count": sum(
            item["outcome"] == "FAIL" for item in intervals
        ),
        "unmatched_assertion_event_count": unmatched,
    }


def _preserve_assertions(connection: sqlite3.Connection) -> dict[str, Any]:
    intervals, unmatched = _assertion_intervals(connection)
    original = int(connection.execute("SELECT count(*) FROM calls").fetchone()[0])
    return {
        "original_call_count": original,
        "retained_call_count": original,
        "folded_call_count": 0,
        "assertion_interval_count": len(intervals),
        "successful_assertion_count": sum(
            item["outcome"] == "PASS" for item in intervals
        ),
        "failed_assertion_count": sum(
            item["outcome"] == "FAIL" for item in intervals
        ),
        "unmatched_assertion_event_count": unmatched,
    }


def _prepare_raw_store(
    connection: sqlite3.Connection,
    *,
    capture_config: dict[str, Any],
    project: str,
    test: str,
    test_class: str,
    test_method: str,
    process_exit_code: int | None,
    assertion_instrumentation: dict[str, Any],
    defect_context: dict[str, Any],
    event_count: int,
    original_call_count: int,
    fold_assertions: bool,
) -> None:
    entered = int(connection.execute(
        "SELECT count(*) FROM invocations"
    ).fetchone()[0])
    if entered == 0:
        raise ValueError("fullchain v2 requires at least one ENTER event")
    unknown = [
        str(row[0]) for row in connection.execute(
            "SELECT DISTINCT event_type FROM lifecycle WHERE event_type NOT IN "
            "('TEST_START','TEST_END')"
        )
    ]
    if unknown:
        raise ValueError(f"unsupported event types: {sorted(unknown)}")
    starts = list(connection.execute(
        "SELECT payload_json FROM lifecycle WHERE event_type='TEST_START' ORDER BY seq"
    ))
    ends = list(connection.execute(
        "SELECT payload_json FROM lifecycle WHERE event_type='TEST_END' ORDER BY seq"
    ))
    if not starts:
        raise ValueError("test process did not record TEST_START")
    if not ends:
        raise ValueError("test process did not record TEST_END")
    test_start = json.loads(str(starts[-1][0]))
    test_end = json.loads(str(ends[-1][0]))
    if test_start.get("agent_protocol_version") != AGENT_PROTOCOL_VERSION:
        raise ValueError(
            "requested Fullchain agent v5 but raw trace used another protocol"
        )
    if test_start.get("value_capture") != capture_config:
        raise ValueError(
            "requested value capture configuration does not match agent TEST_START"
        )
    derived_exit_code = 0 if test_end.get("successful") is True else 1
    if process_exit_code is None:
        process_exit_code = derived_exit_code
    elif process_exit_code != derived_exit_code:
        raise ValueError(
            "Java process exit code does not match the recorded TEST_END outcome"
        )

    # Build indexes after bulk ingestion so millions of inserts do not maintain
    # three large B-trees row by row.
    connection.execute("CREATE INDEX invocation_parent ON invocations(parent_id)")
    connection.execute(
        "CREATE INDEX invocation_enter ON invocations(enter_seq, invocation_id)"
    )
    connection.execute(
        "CREATE INDEX invocation_method ON invocations(class_name, method, descriptor)"
    )

    unmatched = connection.execute(
        """SELECT exits.invocation_id FROM exits
           LEFT JOIN invocations USING(invocation_id)
           WHERE invocations.invocation_id IS NULL LIMIT 1"""
    ).fetchall()
    unclosed = connection.execute(
        """SELECT invocation.invocation_id
           FROM invocations AS invocation
           LEFT JOIN exits USING(invocation_id)
           LEFT JOIN excluded USING(invocation_id)
           WHERE exits.invocation_id IS NULL AND excluded.invocation_id IS NULL
           ORDER BY invocation.enter_seq LIMIT 20"""
    ).fetchall()
    failures = [
        json.loads(str(row[0])) for row in connection.execute(
            "SELECT payload_json FROM failures ORDER BY seq"
        )
    ]
    terminal_stack_overflow = (
        test_end.get("successful") is False
        and any(
            item.get("exception_class") == "java.lang.StackOverflowError"
            for item in failures
        )
    )
    if (unmatched or unclosed) and not terminal_stack_overflow:
        if unmatched:
            raise ValueError(
                f"exit without ENTER: invocation {int(unmatched[0][0])}"
            )
        raise ValueError(
            "unclosed invocations: " + str([int(item[0]) for item in unclosed])
        )
    if terminal_stack_overflow and unclosed:
        failure = next(
            item for item in failures
            if item.get("exception_class") == "java.lang.StackOverflowError"
        )
        terminal_seq = int(failure.get("seq") or test_end.get("seq") or 0)
        terminal_ns = int(failure.get("ts_ns") or test_end.get("ts_ns") or 0)
        connection.execute(
            """INSERT INTO exits(
                   invocation_id,seq,ts_ns,exit_type,duration_ns,
                   exception_class,message,return_value_json
               )
               SELECT invocation.invocation_id,?,?, 'THROW',
                      max(0,?-invocation.enter_ns),
                      'java.lang.StackOverflowError',?,NULL
               FROM invocations AS invocation
               LEFT JOIN exits USING(invocation_id)
               LEFT JOIN excluded USING(invocation_id)
               WHERE exits.invocation_id IS NULL
                 AND excluded.invocation_id IS NULL""",
            (
                terminal_seq, terminal_ns, terminal_ns,
                str(failure.get("message") or ""),
            ),
        )

    missing_parent = connection.execute(
        """SELECT child.invocation_id FROM invocations AS child
           LEFT JOIN invocations AS parent ON parent.invocation_id=child.parent_id
           WHERE child.parent_id!=0 AND parent.invocation_id IS NULL LIMIT 1"""
    ).fetchone()
    if missing_parent is not None:
        raise ValueError("missing invocation parent")
    invalid_parent = connection.execute(
        "SELECT invocation_id FROM invocations WHERE parent_id>=invocation_id LIMIT 1"
    ).fetchone()
    if invalid_parent is not None:
        raise ValueError("cyclic or non-monotonic invocation parent pointers")

    filtered_call_count = int(connection.execute(
        "SELECT count(*) FROM calls"
    ).fetchone()[0])
    if filtered_call_count == 0:
        raise ValueError("no calls remain after noise filtering")
    folding = (
        _fold_assertions(connection)
        if fold_assertions else _preserve_assertions(connection)
    )
    _put_metadata(connection, "schema_version", SCHEMA_VERSION)
    _put_metadata(connection, "project", project)
    _put_metadata(connection, "test", test)
    _put_metadata(connection, "test_class", test_class)
    _put_metadata(connection, "test_method", test_method)
    _put_metadata(connection, "process_exit_code", process_exit_code)
    _put_metadata(connection, "test_start", test_start)
    _put_metadata(connection, "test_end", test_end)
    _put_metadata(connection, "test_failures", failures)
    _put_metadata(connection, "capture", capture_config)
    _put_metadata(connection, "assertion_instrumentation", assertion_instrumentation)
    _put_metadata(connection, "defect_context", defect_context)
    _put_metadata(connection, "assertion_folding", folding)
    _put_metadata(
        connection, "assertion_folding_strategy",
        "dynamic-successful-assertion-subtree-folding"
        if fold_assertions else "assertion-folding-disabled",
    )
    _put_metadata(connection, "event_count", event_count)
    _put_metadata(connection, "original_call_count", original_call_count)
    _put_metadata(connection, "filtered_call_count", filtered_call_count)
    _put_metadata(connection, "call_count", folding["retained_call_count"])
    _put_metadata(connection, "source_kind", "raw-jsonl")


def raw_events_to_store(
    raw_path: Path,
    store_path: Path,
    summary_path: Path,
    *,
    project: str,
    test: str,
    test_class: str,
    test_method: str,
    process_exit_code: int | None,
    assertion_instrumentation: dict[str, Any],
    defect_context: dict[str, Any],
    capture_config: dict[str, Any],
    fold_assertions: bool = True,
) -> dict[str, Any]:
    """Ingest a v5 JSONL trace into an atomic SQLite store."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = store_path.with_suffix(store_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    connection = _connect(temporary)
    try:
        connection.executescript(_SCHEMA)
        raw_capture_fingerprint = _sha256_file(raw_path)
        event_count = 0
        buffered_bytes = 0
        invocation_rows: list[tuple[Any, ...]] = []
        exit_rows: list[tuple[Any, ...]] = []
        assertion_rows: list[tuple[Any, ...]] = []
        failure_rows: list[tuple[Any, ...]] = []
        lifecycle_rows: list[tuple[Any, ...]] = []
        excluded_rows: list[tuple[int]] = []
        call_rows: list[tuple[int]] = []
        active_descriptors: dict[int, str] = {}
        active_projection: dict[int, tuple[str, str, bool]] = {}
        original_call_count = 0
        with connection:
            for line_number, encoded_size, event in iter_raw_events(raw_path):
                event_count += 1
                buffered_bytes += encoded_size
                event_type = event.get("type")
                if event_type not in ALLOWED_EVENTS:
                    raise ValueError(
                        f"unsupported event type at line {line_number}: {event_type!r}"
                    )
                _validate_capture_event(
                    event, capture_config, active_descriptors
                )
                if event_type == "ENTER":
                    invocation_rows.append(_invocation_row(event))
                    invocation_id = int(event.get("invocation_id") or 0)
                    parent_id = int(event.get("parent_id") or 0)
                    class_name = str(event.get("class") or "")
                    method = str(event.get("method") or "")
                    parent = active_projection.get(parent_id)
                    excluded = method == "<clinit>" or bool(
                        parent is not None and parent[2]
                    )
                    active_projection[invocation_id] = (
                        class_name, method, excluded
                    )
                    if excluded:
                        excluded_rows.append((invocation_id,))
                    elif parent_id and parent is not None and not parent[2]:
                        original_call_count += 1
                        if not _is_noise_method(
                            parent[0], parent[1]
                        ) and not _is_noise_method(class_name, method):
                            call_rows.append((invocation_id,))
                elif event_type in {"RETURN", "THROW"}:
                    exit_rows.append(_exit_row(event))
                    active_projection.pop(
                        int(event.get("invocation_id") or 0), None
                    )
                elif event_type in ASSERTION_EVENTS:
                    assertion_rows.append((
                        int(event.get("seq") or 0), str(event_type),
                        str(event.get("assertion_id") or ""),
                        int(event.get("thread_id") or 0),
                        int(event.get("source_start_line") or 0),
                        int(event.get("source_end_line") or 0),
                    ))
                elif event_type == "TEST_FAILURE":
                    failure_rows.append((
                        int(event.get("seq") or 0),
                        str(event.get("exception_class") or ""),
                        str(event.get("message") or ""), _json(event),
                    ))
                else:
                    lifecycle_rows.append((
                        int(event.get("seq") or 0), str(event_type), _json(event),
                    ))
                if buffered_bytes >= STREAM_BUFFER_BYTES:
                    _flush_event_rows(
                        connection, invocation_rows, exit_rows, assertion_rows,
                        failure_rows, lifecycle_rows, excluded_rows, call_rows,
                    )
                    connection.commit()
                    buffered_bytes = 0
            _flush_event_rows(
                connection, invocation_rows, exit_rows, assertion_rows,
                failure_rows, lifecycle_rows, excluded_rows, call_rows,
            )
            _prepare_raw_store(
                connection,
                capture_config=capture_config,
                project=project,
                test=test,
                test_class=test_class,
                test_method=test_method,
                process_exit_code=process_exit_code,
                assertion_instrumentation=assertion_instrumentation,
                defect_context=defect_context,
                event_count=event_count,
                original_call_count=original_call_count,
                fold_assertions=fold_assertions,
            )
            _put_metadata(
                connection, "raw_capture_fingerprint", raw_capture_fingerprint
            )
        connection.close()
        temporary.replace(store_path)
        return write_method_summary(store_path, summary_path)
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise


def execution_to_store(
    execution: dict[str, Any],
    store_path: Path,
    summary_path: Path,
    *,
    test: str,
    assertion_folding: dict[str, Any],
    defect_context: dict[str, Any],
) -> dict[str, Any]:
    """Write an already-normalized in-memory execution to the native store.

    This is used by focused fixtures and callers that already own normalized
    data; it is not a reader for the retired compressed-JSON artifact format.
    """
    temporary = store_path.with_suffix(store_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    connection = _connect(temporary)
    try:
        connection.executescript(_SCHEMA)
        call_ids = {int(item["invocation_id"]) for item in execution["calls"]}
        with connection:
            for item in execution["invocations"]:
                _insert_invocation(connection, {
                    "invocation_id": item["invocation_id"],
                    "parent_id": item.get("parent_id"), "class": item["class"],
                    "method": item["method"], "descriptor": item.get("descriptor"),
                    "thread_id": item.get("thread_id"),
                    "thread_name": item.get("thread_name"),
                    "seq": item.get("enter_seq"), "ts_ns": item.get("enter_ns"),
                    "origin_test_line": item.get("origin_test_line"),
                    "arguments": item.get("arguments"),
                })
                connection.execute(
                    "INSERT INTO exits VALUES (?,?,?,?,?,?,?,?)",
                    (
                        int(item["invocation_id"]), int(item.get("exit_seq") or 0),
                        int(item.get("exit_ns") or 0), str(item.get("exit_type") or ""),
                        int(item.get("duration_ns") or 0),
                        str(item.get("exception_class") or ""),
                        str(item.get("message") or ""),
                        _json(item["return_value"])
                        if isinstance(item.get("return_value"), dict) else None,
                    ),
                )
            connection.executemany(
                "INSERT INTO calls VALUES (?)", [(value,) for value in call_ids]
            )
            connection.execute("CREATE INDEX invocation_parent ON invocations(parent_id)")
            connection.execute(
                "CREATE INDEX invocation_enter ON invocations(enter_seq,invocation_id)"
            )
            connection.execute(
                "CREATE INDEX invocation_method ON invocations(class_name,method,descriptor)"
            )
            test_start = execution.get("test_start") or {}
            capture = dict(test_start.get("value_capture") or {
                "capture_values": False, "value_string_edge_chars": 10,
                "value_container_edge_items": 2,
                "value_nested_container_edge_items": 1,
                "value_max_depth": 2, "value_max_arguments": 8,
            })
            for key, value in {
                "schema_version": SCHEMA_VERSION,
                "project": str(execution.get("project") or ""), "test": test,
                "test_class": test.split("::", 1)[0],
                "test_method": test.split("::", 1)[1],
                "process_exit_code": int(execution.get("process_exit_code") or 0),
                "test_start": test_start, "test_end": execution.get("test_end"),
                "test_failures": execution.get("test_failures") or [],
                "capture": capture, "assertion_folding": assertion_folding,
                "defect_context": defect_context, "call_count": len(call_ids),
                "source_kind": "normalized-execution",
            }.items():
                _put_metadata(connection, key, value)
        connection.close()
        temporary.replace(store_path)
        return write_method_summary(store_path, summary_path)
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise


class _FastPathUnsupported(Exception):
    """The raw stream is valid but requires the general SQLite converter."""


def should_use_fast_trace_store(
    raw_path: Path, assertion_instrumentation: dict[str, Any]
) -> bool:
    """Select the sidecar path only where its extra raw pass pays for itself."""
    return (
        raw_path.is_file()
        and raw_path.stat().st_size >= FAST_RAW_THRESHOLD_BYTES
        and assertion_instrumentation.get("configured_ranges") == ""
    )


def is_fast_trace_store(store_path: Path) -> bool:
    if not store_path.is_file():
        return False
    connection = _connect(store_path)
    try:
        return _metadata_or(connection, "storage_kind") == FAST_STORE_KIND
    finally:
        connection.close()


def _children_blob(children: list[tuple[int, int]]) -> bytes | None:
    if not children:
        return None
    result = bytearray(16 * len(children))
    for index, (child_id, subtree_count) in enumerate(children):
        struct.pack_into("<QQ", result, index * 16, child_id, subtree_count)
    return bytes(result)


def _decode_children(blob: bytes | None) -> tuple[list[int], list[int]]:
    children: list[int] = []
    prefix = [0]
    if blob is None:
        return children, prefix
    for child_id, subtree_count in struct.iter_unpack("<QQ", blob):
        children.append(int(child_id))
        prefix.append(prefix[-1] + int(subtree_count))
    return children, prefix


def _flush_fast_nodes(
    connection: sqlite3.Connection, rows: list[tuple[Any, ...]]
) -> None:
    if not rows:
        return
    try:
        connection.executemany(
            "INSERT INTO fast_nodes VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
    except sqlite3.IntegrityError as error:
        raise ValueError(f"duplicate trace event identity: {error}") from error
    rows.clear()


def raw_events_to_fast_store(
    raw_path: Path,
    store_path: Path,
    summary_path: Path,
    *,
    project: str,
    test: str,
    test_class: str,
    test_method: str,
    process_exit_code: int | None,
    assertion_instrumentation: dict[str, Any],
    defect_context: dict[str, Any],
    capture_config: dict[str, Any],
    fold_assertions: bool = True,
) -> dict[str, Any]:
    """Build a compact exit/topology sidecar for a second raw ENTER pass.

    Assertion-bearing or non-monotonic streams transparently fall back to the
    general converter.  Protocol or evidence errors still fail rather than
    being weakened by the optimization.
    """
    if assertion_instrumentation.get("configured_ranges") != "":
        return raw_events_to_store(
            raw_path, store_path, summary_path, project=project, test=test,
            test_class=test_class, test_method=test_method,
            process_exit_code=process_exit_code,
            assertion_instrumentation=assertion_instrumentation,
            defect_context=defect_context, capture_config=capture_config,
            fold_assertions=fold_assertions,
        )
    store_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = store_path.with_suffix(store_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    connection = _connect(temporary)
    try:
        connection.executescript(_FAST_SCHEMA)
        # frame: parent, class, method, excluded, is_call, method_key,
        #        subtree_count, direct_call_children
        active: dict[int, list[Any]] = {}
        active_descriptors: dict[int, str] = {}
        method_keys: dict[tuple[str, str, str], int] = {}
        used_method_keys: set[int] = set()
        call_method_keys: set[int] = set()
        node_rows: list[tuple[Any, ...]] = []
        test_starts: list[dict[str, Any]] = []
        test_ends: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        event_count = entered = call_count = 0
        buffered_bytes = 0
        previous_invocation_id = 0
        with connection:
            for line_number, encoded_size, event in iter_raw_events(raw_path):
                event_count += 1
                buffered_bytes += encoded_size
                event_type = event.get("type")
                if event_type not in ALLOWED_EVENTS:
                    raise ValueError(
                        f"unsupported event type at line {line_number}: {event_type!r}"
                    )
                if event_type in ASSERTION_EVENTS:
                    raise _FastPathUnsupported()
                _validate_capture_event(
                    event, capture_config, active_descriptors
                )
                if event_type == "ENTER":
                    invocation_id = int(event.get("invocation_id") or 0)
                    parent_id = int(event.get("parent_id") or 0)
                    if invocation_id <= previous_invocation_id:
                        raise _FastPathUnsupported()
                    previous_invocation_id = invocation_id
                    parent = active.get(parent_id)
                    if parent_id and parent is None:
                        raise _FastPathUnsupported()
                    class_name = str(event.get("class") or "")
                    method = str(event.get("method") or "")
                    descriptor = str(event.get("descriptor") or "")
                    excluded = method == "<clinit>" or bool(
                        parent is not None and parent[3]
                    )
                    is_call = bool(
                        parent_id and parent is not None and not excluded
                        and not _is_noise_method(parent[1], parent[2])
                        and not _is_noise_method(class_name, method)
                    )
                    key = (class_name, method, descriptor)
                    method_key = method_keys.setdefault(key, len(method_keys) + 1)
                    active[invocation_id] = [
                        parent_id, class_name, method, excluded, is_call,
                        method_key, 1 if is_call else 0, [],
                    ]
                    entered += 1
                    if is_call:
                        call_count += 1
                        call_method_keys.add(method_key)
                elif event_type in {"RETURN", "THROW"}:
                    invocation_id = int(event.get("invocation_id") or 0)
                    frame = active.pop(invocation_id, None)
                    if frame is None:
                        raise ValueError(
                            f"exit without ENTER: invocation {invocation_id}"
                        )
                    if frame[3]:
                        continue
                    subtree_count = int(frame[6])
                    children = frame[7]
                    if frame[4]:
                        parent = active.get(int(frame[0]))
                        if parent is None:
                            raise _FastPathUnsupported()
                        if len(parent[7]) >= FAST_MAX_DIRECT_CHILDREN:
                            raise _FastPathUnsupported()
                        parent[6] += subtree_count
                        parent[7].append((invocation_id, subtree_count))
                    if frame[4] or children:
                        used_method_keys.add(int(frame[5]))
                        result = (
                            [
                                "throw", str(event.get("exception_class") or ""),
                                str(event.get("message") or ""), 0,
                            ]
                            if event_type == "THROW"
                            else _compact_value(event.get("return_value"))
                        )
                        node_rows.append((
                            invocation_id, int(frame[0]), 1 if frame[4] else 0,
                            int(frame[5]), subtree_count,
                            int(event.get("seq") or 0), str(event_type),
                            _json(result) if result is not None else None,
                            _children_blob(children),
                        ))
                elif event_type == "TEST_START":
                    test_starts.append(event)
                elif event_type == "TEST_END":
                    test_ends.append(event)
                elif event_type == "TEST_FAILURE":
                    failures.append(event)
                if buffered_bytes >= STREAM_BUFFER_BYTES:
                    _flush_fast_nodes(connection, node_rows)
                    connection.commit()
                    buffered_bytes = 0
            _flush_fast_nodes(connection, node_rows)
            if entered == 0:
                raise ValueError("fullchain v2 requires at least one ENTER event")
            if not test_starts:
                raise ValueError("test process did not record TEST_START")
            if not test_ends:
                raise ValueError("test process did not record TEST_END")
            test_start = test_starts[-1]
            test_end = test_ends[-1]
            terminal_stack_overflow = (
                test_end.get("successful") is False
                and any(
                    item.get("exception_class") == "java.lang.StackOverflowError"
                    for item in failures
                )
            )
            if active or active_descriptors:
                if terminal_stack_overflow:
                    raise _FastPathUnsupported()
                raise ValueError(
                    "unclosed invocations: "
                    + str(sorted(active)[:20])
                )
            if test_start.get("agent_protocol_version") != AGENT_PROTOCOL_VERSION:
                raise ValueError(
                    "requested Fullchain agent v5 but raw trace used another protocol"
                )
            if test_start.get("value_capture") != capture_config:
                raise ValueError(
                    "requested value capture configuration does not match agent TEST_START"
                )
            derived_exit_code = 0 if test_end.get("successful") is True else 1
            if process_exit_code is None:
                process_exit_code = derived_exit_code
            elif process_exit_code != derived_exit_code:
                raise ValueError(
                    "Java process exit code does not match the recorded TEST_END outcome"
                )
            if call_count == 0:
                raise ValueError("no calls remain after noise filtering")
            connection.executemany(
                "INSERT INTO fast_methods VALUES (?,?,?,?)",
                [
                    (method_key, *key)
                    for key, method_key in method_keys.items()
                    if method_key in used_method_keys
                ],
            )
            connection.execute(
                "CREATE INDEX fast_node_method ON "
                "fast_nodes(method_key,invocation_id) WHERE is_call=1"
            )
            folding = {
                "original_call_count": call_count,
                "retained_call_count": call_count,
                "folded_call_count": 0,
                "assertion_interval_count": 0,
                "successful_assertion_count": 0,
                "failed_assertion_count": 0,
                "unmatched_assertion_event_count": 0,
            }
            for key, value in {
                "schema_version": SCHEMA_VERSION,
                "storage_kind": FAST_STORE_KIND,
                "raw_name": raw_path.name,
                "project": project,
                "test": test,
                "test_class": test_class,
                "test_method": test_method,
                "process_exit_code": process_exit_code,
                "test_start": test_start,
                "test_end": test_end,
                "test_failures": failures,
                "capture": capture_config,
                "assertion_instrumentation": assertion_instrumentation,
                "defect_context": defect_context,
                "assertion_folding": folding,
                "assertion_folding_strategy": (
                    "dynamic-successful-assertion-subtree-folding"
                    if fold_assertions else "assertion-folding-disabled"
                ),
                "event_count": event_count,
                "call_count": call_count,
                "source_kind": FAST_STORE_KIND,
            }.items():
                _put_metadata(connection, key, value)
        connection.close()
        temporary.replace(store_path)
        methods = sorted(
            [list(key) for key, method_key in method_keys.items()
             if method_key in call_method_keys]
        )
        summary = {
            "schema": METHOD_SUMMARY_SCHEMA,
            "schema_version": METHOD_SUMMARY_VERSION,
            "test": test,
            "capture": capture_config,
            "call_count": call_count,
            "methods": methods,
        }
        validate_method_summary(summary)
        write_compact_json(summary_path, summary)
        return summary
    except _FastPathUnsupported:
        connection.close()
        temporary.unlink(missing_ok=True)
        return raw_events_to_store(
            raw_path, store_path, summary_path, project=project, test=test,
            test_class=test_class, test_method=test_method,
            process_exit_code=process_exit_code,
            assertion_instrumentation=assertion_instrumentation,
            defect_context=defect_context, capture_config=capture_config,
            fold_assertions=fold_assertions,
        )
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise


def write_method_summary(store_path: Path, summary_path: Path) -> dict[str, Any]:
    connection = _connect(store_path)
    try:
        methods = [
            [str(row[0]), str(row[1]), str(row[2])]
            for row in connection.execute(
                """SELECT DISTINCT class_name,method,descriptor
                   FROM invocations
                   JOIN calls USING(invocation_id)
                   LEFT JOIN folded USING(invocation_id)
                   WHERE folded.invocation_id IS NULL
                   ORDER BY class_name,method,descriptor"""
            )
        ]
        summary = {
            "schema": METHOD_SUMMARY_SCHEMA,
            "schema_version": METHOD_SUMMARY_VERSION,
            "test": str(_metadata(connection, "test")),
            "capture": _metadata(connection, "capture"),
            "call_count": int(_metadata(connection, "call_count")),
            "methods": methods,
        }
        validate_method_summary(summary)
        write_compact_json(summary_path, summary)
        return summary
    finally:
        connection.close()


def trace_store_assertion_folding_strategy(store_path: Path) -> str:
    """Read the execution-space construction policy without loading the trace."""
    materialized = ensure_trace_store(store_path.parent, "")
    connection = _connect(materialized)
    try:
        return str(_metadata_or(
            connection, "assertion_folding_strategy", "legacy-enabled"
        ))
    finally:
        connection.close()


def validate_method_summary(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "schema_version", "test", "capture", "call_count", "methods"
        }
        or value.get("schema") != METHOD_SUMMARY_SCHEMA
        or value.get("schema_version") != METHOD_SUMMARY_VERSION
        or not isinstance(value.get("test"), str)
        or "::" not in value["test"]
        or not isinstance(value.get("capture"), dict)
        or not isinstance(value.get("call_count"), int)
        or isinstance(value["call_count"], bool)
        or value["call_count"] <= 0
        or not isinstance(value.get("methods"), list)
        or not value["methods"]
    ):
        raise ValueError("invalid trace method summary")
    previous: tuple[str, str, str] | None = None
    for item in value["methods"]:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not all(isinstance(part, str) for part in item)
            or not item[0]
            or not item[1]
        ):
            raise ValueError("invalid summarized trace method")
        key = tuple(item)
        if previous is not None and key <= previous:
            raise ValueError("trace summary methods are not uniquely sorted")
        previous = key
    return value


def archive_trace_store(directory: Path) -> Path:
    """Compress a completed intermediate store and release its disk blocks."""
    store_path = directory / TRACE_STORE_NAME
    archive_path = directory / TRACE_STORE_ARCHIVE_NAME
    if not store_path.is_file():
        if archive_path.is_file():
            return archive_path
        raise ValueError("trace store is missing before archival")
    compress_zstd_file(store_path, archive_path, level=1)
    store_path.unlink()
    return archive_path


def restore_trace_store(directory: Path) -> Path:
    """Restore one archived store atomically for final trace materialization."""
    store_path = directory / TRACE_STORE_NAME
    if store_path.is_file():
        return store_path
    archive_path = directory / TRACE_STORE_ARCHIVE_NAME
    if not archive_path.is_file():
        raise ValueError("compressed trace store is missing")
    temporary = store_path.with_suffix(store_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source = compressed = None
    try:
        source, compressed = _open_zstd(archive_path)
        with temporary.open("wb") as output:
            while True:
                chunk = compressed.read(STREAM_BUFFER_BYTES)
                if not chunk:
                    break
                output.write(chunk)
        compressed.close()
        source.close()
        connection = _connect(temporary)
        try:
            _metadata(connection, "schema_version")
            table = (
                "fast_nodes"
                if _metadata_or(connection, "storage_kind") == FAST_STORE_KIND
                else "invocations"
            )
            connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        finally:
            connection.close()
        temporary.replace(store_path)
        return store_path
    except (OSError, ValueError, sqlite3.Error, zstandard.ZstdError) as error:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"cannot restore compressed trace store: {error}") from error
    finally:
        if compressed is not None and not compressed.closed:
            compressed.close()
        if source is not None and not source.closed:
            source.close()


def read_available_trace_summary(
    directory: Path, work_name: str
) -> dict[str, Any]:
    """Read bounded reuse metadata for a native SQLite conversion store."""
    summary_path = directory / METHOD_SUMMARY_NAME
    if summary_path.is_file() and (directory / TRACE_STORE_NAME).is_file():
        return validate_method_summary(read_json(summary_path))
    raise ValueError("trigger has no native SQLite trace store and method summary")


def ensure_trace_store(directory: Path, work_name: str) -> dict[str, Any]:
    store_path = directory / TRACE_STORE_NAME
    summary_path = directory / METHOD_SUMMARY_NAME
    if store_path.is_file() and summary_path.is_file():
        return validate_method_summary(read_json(summary_path))
    raise ValueError("trigger has no native SQLite trace store and method summary")


def _compact_value(value: Any) -> list[Any] | None:
    if not isinstance(value, dict):
        return None
    omitted_count = int(value.get("omitted_count") or 0)
    if value.get("truncated") is True and omitted_count == 0:
        omitted_count = 1
    return [
        str(value.get("kind") or ""), str(value.get("runtime_type") or ""),
        str(value.get("text") or ""), omitted_count,
    ]


def _compact_arguments(value: Any) -> list[Any] | None:
    if not isinstance(value, dict):
        return None
    return [
        int(value.get("omitted_count") or 0),
        [
            [
                str(item.get("kind") or ""),
                str(item.get("runtime_type") or ""),
                str(item.get("text") or ""),
                1 if item.get("truncated") is True else 0,
            ]
            for item in value.get("items") or []
        ],
    ]


def _prepare_nodes(
    connection: sqlite3.Connection,
    method_ids: dict[tuple[str, str, str], str],
) -> list[list[str]]:
    connection.execute("DROP TABLE IF EXISTS temp.nodes")
    connection.execute(
        """CREATE TEMP TABLE nodes (
               invocation_id INTEGER PRIMARY KEY,
               is_call INTEGER NOT NULL,
               method_id TEXT,
               subtree_count INTEGER NOT NULL DEFAULT 0
           )"""
    )
    connection.execute(
        """INSERT INTO nodes(invocation_id,is_call,subtree_count)
           SELECT calls.invocation_id,1,1 FROM calls
           LEFT JOIN folded USING(invocation_id)
           WHERE folded.invocation_id IS NULL"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO nodes(invocation_id,is_call,subtree_count)
           SELECT invocation.parent_id,0,0
           FROM calls
           JOIN invocations AS invocation USING(invocation_id)
           LEFT JOIN folded USING(invocation_id)
           WHERE folded.invocation_id IS NULL"""
    )
    used_keys = [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """SELECT DISTINCT invocation.class_name,invocation.method,
                              invocation.descriptor
               FROM nodes JOIN invocations AS invocation USING(invocation_id)
               ORDER BY invocation.class_name,invocation.method,invocation.descriptor"""
        )
    ]
    boundary_keys = [key for key in used_keys if key not in method_ids]
    local_ids = dict(method_ids)
    local_ids.update({
        key: f"B{index}" for index, key in enumerate(boundary_keys, 1)
    })
    connection.execute(
        "CREATE TEMP TABLE method_map(class_name,method,descriptor,method_id PRIMARY KEY)"
    )
    connection.executemany(
        "INSERT INTO method_map VALUES (?,?,?,?)",
        [(*key, local_ids[key]) for key in used_keys],
    )
    connection.execute(
        """UPDATE nodes SET method_id=(
               SELECT method_id FROM method_map
               JOIN invocations AS invocation
                 ON invocation.class_name=method_map.class_name
                AND invocation.method=method_map.method
                AND invocation.descriptor=method_map.descriptor
               WHERE invocation.invocation_id=nodes.invocation_id
           )"""
    )

    last_seq: int | None = None
    last_id: int | None = None
    batch_size = 10000
    while True:
        if last_seq is None:
            rows = connection.execute(
                """SELECT node.invocation_id,invocation.parent_id,
                          node.subtree_count,invocation.enter_seq
                   FROM nodes AS node JOIN invocations AS invocation USING(invocation_id)
                   ORDER BY invocation.enter_seq DESC,node.invocation_id DESC LIMIT ?""",
                (batch_size,),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT node.invocation_id,invocation.parent_id,
                          node.subtree_count,invocation.enter_seq
                   FROM nodes AS node JOIN invocations AS invocation USING(invocation_id)
                   WHERE invocation.enter_seq<? OR (
                       invocation.enter_seq=? AND node.invocation_id<?
                   )
                   ORDER BY invocation.enter_seq DESC,node.invocation_id DESC LIMIT ?""",
                (last_seq, last_seq, last_id, batch_size),
            ).fetchall()
        if not rows:
            break
        pending: dict[int, int] = {}
        updates = []
        for invocation_id, parent_id, initial_count, enter_seq in rows:
            count = int(initial_count) + pending.pop(int(invocation_id), 0)
            updates.append((count, int(invocation_id)))
            if int(parent_id):
                pending[int(parent_id)] = pending.get(int(parent_id), 0) + count
            last_seq, last_id = int(enter_seq), int(invocation_id)
        connection.executemany(
            "UPDATE nodes SET subtree_count=? WHERE invocation_id=?", updates
        )
        connection.executemany(
            "UPDATE nodes SET subtree_count=subtree_count+? WHERE invocation_id=?",
            [(count, parent) for parent, count in pending.items()],
        )
    connection.execute("DROP TABLE IF EXISTS temp.node_edges")
    connection.execute(
        """CREATE TEMP TABLE node_edges AS
           SELECT invocation.parent_id,
                  node.invocation_id AS child_id,
                  node.subtree_count AS child_subtree_count
           FROM nodes AS node
           JOIN invocations AS invocation USING(invocation_id)
           WHERE node.is_call=1"""
    )
    connection.execute(
        "CREATE INDEX node_edge_parent ON node_edges(parent_id,child_id)"
    )
    return [
        [local_ids[key], *key] for key in used_keys if key in method_ids
    ] + [
        [local_ids[key], *key] for key in boundary_keys
    ]


def _write_array(
    write: Callable[[str], Any], values: Iterable[Any]
) -> None:
    write("[")
    separator = ""
    for value in values:
        write(separator)
        write(_json(value, sort_keys=isinstance(value, dict)))
        separator = ","
    write("]")


def _write_rows(
    connection: sqlite3.Connection,
    write: Callable[[str], Any],
    is_call: bool,
) -> None:
    query = """SELECT
          parent.invocation_id,parent.method_id,parent.subtree_count,
          invocation.parent_id,invocation.enter_seq,invocation_exit.seq,
          invocation_exit.exit_type,invocation.origin_test_line,
          invocation.arguments_json,invocation_exit.return_value_json,
          invocation_exit.exception_class,invocation_exit.message,
          edge.child_id,edge.child_subtree_count
       FROM nodes AS parent
       JOIN invocations AS invocation USING(invocation_id)
       JOIN exits AS invocation_exit USING(invocation_id)
       LEFT JOIN node_edges AS edge ON edge.parent_id=parent.invocation_id
       WHERE parent.is_call=?
       ORDER BY parent.invocation_id,edge.child_id"""
    cursor = iter(connection.execute(query, (1 if is_call else 0,)))
    pending = next(cursor, None)
    write("[")
    row_separator = ""
    while pending is not None:
        row = pending
        invocation_id = int(row[0])
        write(row_separator)
        row_separator = ","
        head = [
            invocation_id, int(row[3]), str(row[1]), int(row[4]), int(row[5]),
            str(row[6]), int(row[7]),
        ]
        write(_json(head)[:-1] + ",[")
        child_separator = ""
        cumulative = 0
        with tempfile.SpooledTemporaryFile(
            mode="w+", encoding="utf-8", max_size=STREAM_BUFFER_BYTES
        ) as prefix:
            prefix.write("[0")
            while pending is not None and int(pending[0]) == invocation_id:
                child_id = pending[12]
                if child_id is not None:
                    write(child_separator + str(int(child_id)))
                    child_separator = ","
                    cumulative += int(pending[13])
                    prefix.write("," + str(cumulative))
                pending = next(cursor, None)
            prefix.write("]")
            arguments = (
                _compact_arguments(json.loads(str(row[8])))
                if row[8] is not None else None
            )
            result = (
                ["throw", str(row[10]), str(row[11]), 0]
                if str(row[6]) == "THROW"
                else _compact_value(json.loads(str(row[9])))
                if row[9] is not None else None
            )
            write("]")
            write("," + str(int(row[2])))
            write("," + _json(arguments))
            write("," + _json(result) + ",")
            prefix.seek(0)
            while True:
                chunk = prefix.read(1024 * 1024)
                if not chunk:
                    break
                write(chunk)
            write("]")
    write("]")


def _write_method_index(
    connection: sqlite3.Connection, write: Callable[[str], Any]
) -> None:
    cursor = iter(connection.execute(
        """SELECT node.method_id,node.invocation_id
           FROM nodes AS node WHERE node.is_call=1
           ORDER BY CAST(substr(node.method_id,2) AS INTEGER),node.invocation_id"""
    ))
    pending = next(cursor, None)
    write("[")
    group_separator = ""
    while pending is not None:
        method_id = str(pending[0])
        write(group_separator + "[" + _json(method_id) + ",[" )
        group_separator = ","
        id_separator = ""
        while pending is not None and str(pending[0]) == method_id:
            write(id_separator + str(int(pending[1])))
            id_separator = ","
            pending = next(cursor, None)
        write("]]" )
    write("]")


def _fast_method_maps(
    connection: sqlite3.Connection,
    method_ids: dict[tuple[str, str, str], str],
) -> tuple[list[list[str]], dict[int, str]]:
    keyed = [
        (int(row[0]), (str(row[1]), str(row[2]), str(row[3])))
        for row in connection.execute(
            "SELECT method_key,class_name,method,descriptor FROM fast_methods"
        )
    ]
    used_keys = sorted(key for _, key in keyed)
    boundary_keys = [key for key in used_keys if key not in method_ids]
    local_ids = dict(method_ids)
    local_ids.update({
        key: f"B{index}" for index, key in enumerate(boundary_keys, 1)
    })
    key_to_local = {
        method_key: local_ids[key] for method_key, key in keyed
    }
    methods = [
        [local_ids[key], *key] for key in used_keys if key in method_ids
    ] + [
        [local_ids[key], *key] for key in boundary_keys
    ]
    return methods, key_to_local


def _write_fast_method_index(
    connection: sqlite3.Connection,
    write: Callable[[str], Any],
    key_to_local: dict[int, str],
) -> None:
    methods = sorted(
        (
            (int(method_id[1:]), method_key, method_id)
            for method_key, method_id in key_to_local.items()
            if method_id.startswith("M")
        ),
        key=lambda item: item[0],
    )
    write("[")
    separator = ""
    for _, method_key, method_id in methods:
        ids = connection.execute(
            "SELECT invocation_id FROM fast_nodes "
            "WHERE is_call=1 AND method_key=? ORDER BY invocation_id",
            (method_key,),
        )
        first = next(ids, None)
        if first is None:
            continue
        write(separator + "[" + _json(method_id) + ",[")
        separator = ","
        id_separator = ""
        row = first
        while row is not None:
            write(id_separator + str(int(row[0])))
            id_separator = ","
            row = next(ids, None)
        write("]]" )
    write("]")


def _fast_material_writer(
    connection: sqlite3.Connection,
    raw_path: Path,
    write: Callable[[str], Any],
    *,
    project: str,
    test_id: str,
    test: str,
    catalog_fingerprint: str,
    method_ids: dict[tuple[str, str, str], str],
) -> None:
    methods, key_to_local = _fast_method_maps(connection, method_ids)
    folding = _metadata(connection, "assertion_folding")
    capture = _metadata(connection, "capture")
    context = _metadata(connection, "defect_context")
    failures = [
        {
            "exception_class": str(item.get("exception_class") or ""),
            "message": str(item.get("message") or ""),
        }
        for item in _metadata(connection, "test_failures")
    ]
    failure = {
        "process_exit_code": int(_metadata(connection, "process_exit_code")),
        "error_stack": str(context.get("error_stack") or ""),
        "test_output": str(context.get("test_output") or ""),
        "events": failures,
    }
    call_count = int(_metadata(connection, "call_count"))
    nodes = iter(connection.execute(
        """SELECT invocation_id,parent_id,is_call,method_key,subtree_count,
                  exit_seq,exit_type,result_json,children_blob
           FROM fast_nodes ORDER BY invocation_id"""
    ))
    pending = next(nodes, None)
    written_calls = 0
    written_contexts = 0

    write("{")
    write(_json("assertion_folding") + ":" + _json(folding, sort_keys=True))
    write("," + _json("call_count") + ":" + str(call_count))
    write("," + _json("calls") + ":[")
    call_separator = ""
    with tempfile.SpooledTemporaryFile(
        mode="w+", encoding="utf-8", max_size=STREAM_BUFFER_BYTES
    ) as contexts:
        context_separator = ""
        for event in _iter_raw_enter_events(raw_path):
            invocation_id = int(event.get("invocation_id") or 0)
            if pending is None or invocation_id < int(pending[0]):
                continue
            if invocation_id > int(pending[0]):
                raise ValueError(
                    f"fast trace sidecar has no matching ENTER for {int(pending[0])}"
                )
            key = int(pending[3])
            method_id = key_to_local.get(key)
            if method_id is None:
                raise ValueError("fast trace sidecar references an unknown method")
            children, prefix = _decode_children(pending[8])
            arguments = (
                _compact_arguments(event.get("arguments"))
                if isinstance(event.get("arguments"), dict) else None
            )
            result = (
                json.loads(str(pending[7])) if pending[7] is not None else None
            )
            row = [
                invocation_id, int(pending[1]), method_id,
                int(event.get("seq") or 0), int(pending[5]), str(pending[6]),
                int(event.get("origin_test_line") or 0), children,
                int(pending[4]), arguments, result, prefix,
            ]
            serialized = _json(row)
            if int(pending[2]):
                write(call_separator + serialized)
                call_separator = ","
                written_calls += 1
            else:
                contexts.write(context_separator + serialized)
                context_separator = ","
                written_contexts += 1
            pending = next(nodes, None)
        if pending is not None:
            raise ValueError(
                f"fast trace sidecar has no matching ENTER for {int(pending[0])}"
            )
        if written_calls != call_count:
            raise ValueError("fast trace sidecar call count changed during output")
        expected_contexts = int(connection.execute(
            "SELECT count(*) FROM fast_nodes WHERE is_call=0"
        ).fetchone()[0])
        if written_contexts != expected_contexts:
            raise ValueError("fast trace sidecar context count changed during output")
        write("]")
        write("," + _json("capture") + ":" + _json(capture, sort_keys=True))
        write("," + _json("contexts") + ":[")
        contexts.seek(0)
        while True:
            chunk = contexts.read(1024 * 1024)
            if not chunk:
                break
            write(chunk)
        write("]")
    write("," + _json("failure") + ":" + _json(failure, sort_keys=True))
    write("," + _json("method_catalog_fingerprint") + ":" + _json(catalog_fingerprint))
    write("," + _json("method_invocations") + ":")
    _write_fast_method_index(connection, write, key_to_local)
    write("," + _json("methods") + ":" + _json(methods))
    write("," + _json("project") + ":" + _json(project))
    write("," + _json("root_ids") + ":")
    _write_array(
        write,
        (
            int(row[0]) for row in connection.execute(
                "SELECT invocation_id FROM fast_nodes "
                "WHERE is_call=0 AND subtree_count>0 ORDER BY invocation_id"
            )
        ),
    )
    write("," + _json("schema") + ":" + _json("refinement-trace"))
    write("," + _json("schema_version") + ":2")
    write("," + _json("test") + ":" + _json(test))
    write("," + _json("test_id") + ":" + _json(test_id))
    write("}")


def _material_writer(
    connection: sqlite3.Connection,
    write: Callable[[str], Any],
    *,
    project: str,
    test_id: str,
    test: str,
    catalog_fingerprint: str,
    methods: list[list[str]],
) -> None:
    # This is the exact sort_keys=True order used by refinement_trace._fingerprint.
    folding = _metadata(connection, "assertion_folding")
    capture = _metadata(connection, "capture")
    context = _metadata(connection, "defect_context")
    failures = [
        {
            "exception_class": str(item.get("exception_class") or ""),
            "message": str(item.get("message") or ""),
        }
        for item in _metadata(connection, "test_failures")
    ]
    failure = {
        "process_exit_code": int(_metadata(connection, "process_exit_code")),
        "error_stack": str(context.get("error_stack") or ""),
        "test_output": str(context.get("test_output") or ""),
        "events": failures,
    }
    call_count = int(_metadata(connection, "call_count"))
    write("{")
    write(_json("assertion_folding") + ":" + _json(folding, sort_keys=True))
    write("," + _json("call_count") + ":" + str(call_count))
    write("," + _json("calls") + ":")
    _write_rows(connection, write, True)
    write("," + _json("capture") + ":" + _json(capture, sort_keys=True))
    write("," + _json("contexts") + ":")
    _write_rows(connection, write, False)
    write("," + _json("failure") + ":" + _json(failure, sort_keys=True))
    write("," + _json("method_catalog_fingerprint") + ":" + _json(catalog_fingerprint))
    write("," + _json("method_invocations") + ":")
    _write_method_index(connection, write)
    write("," + _json("methods") + ":" + _json(methods))
    write("," + _json("project") + ":" + _json(project))
    write("," + _json("root_ids") + ":")
    _write_array(
        write,
        (
            int(row[0]) for row in connection.execute(
                """SELECT invocation_id FROM nodes
                   WHERE is_call=0 AND subtree_count>0 ORDER BY invocation_id"""
            )
        ),
    )
    write("," + _json("schema") + ":" + _json("refinement-trace"))
    write("," + _json("schema_version") + ":2")
    write("," + _json("test") + ":" + _json(test))
    write("," + _json("test_id") + ":" + _json(test_id))
    write("}")


def write_refinement_trace_from_store(
    store_path: Path,
    target: Path,
    *,
    project: str,
    test_id: str,
    test: str,
    method_ids: dict[tuple[str, str, str], str],
    catalog_fingerprint: str,
) -> str:
    """Stream a compact refinement trace from SQLite and return its fingerprint."""
    connection = _connect(store_path)
    try:
        if str(_metadata(connection, "test")) != test:
            raise ValueError("trace store test does not match suite trigger")
        fast_store = _metadata_or(connection, "storage_kind") == FAST_STORE_KIND
        methods = None if fast_store else _prepare_nodes(connection, method_ids)
        raw_path = None
        if fast_store:
            raw_path = store_path.parent / str(_metadata(connection, "raw_name"))
            if not raw_path.is_file():
                raise ValueError("fast trace store is missing its compressed raw input")
        digest = hashlib.sha256()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with temporary.open("wb") as raw:
                with zstandard.ZstdCompressor(level=1).stream_writer(raw) as compressed:
                    with io.TextIOWrapper(compressed, encoding="utf-8") as output:
                        first = True
                        fingerprint: str | None = None

                        def emit(chunk: str) -> None:
                            nonlocal first, fingerprint
                            digest.update(chunk.encode("utf-8"))
                            if first:
                                if chunk != "{":
                                    raise AssertionError("material must start with object")
                                output.write(chunk)
                                first = False
                                return
                            if chunk == "}":
                                fingerprint = digest.hexdigest()
                                output.write(",\"fingerprint\":" + _json(fingerprint) + "}")
                            else:
                                output.write(chunk)

                        if fast_store:
                            _fast_material_writer(
                                connection,
                                raw_path,
                                emit,
                                project=project,
                                test_id=test_id,
                                test=test,
                                catalog_fingerprint=catalog_fingerprint,
                                method_ids=method_ids,
                            )
                        else:
                            _material_writer(
                                connection,
                                emit,
                                project=project,
                                test_id=test_id,
                                test=test,
                                catalog_fingerprint=catalog_fingerprint,
                                methods=methods,
                            )
                        if fingerprint is None:
                            raise AssertionError("material did not end with an object")
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return fingerprint
    finally:
        connection.close()


def _compressed_sequence(
    values: list[int], *, maximum_pattern: int = 32
) -> tuple[list[int], list[tuple[int, int, int]]]:
    """Losslessly fold adjacent repeated blocks in one ordered child sequence."""
    if len(values) < 2:
        return list(values), []

    # Prefer a whole-sequence period.  This covers very large AAA and ABABAB
    # traces without allocating a second prefix table proportional to the run.
    first = values[0]
    candidates: list[int] = []
    for index in range(1, min(len(values), 4097)):
        if values[index] == first and len(values) % index == 0:
            candidates.append(index)
    for period in candidates:
        if all(value == values[index % period] for index, value in enumerate(values)):
            repeat = len(values) // period
            if repeat > 1:
                return list(values[:period]), [(0, period, repeat)]

    result: list[int] = []
    groups: list[tuple[int, int, int]] = []
    index = 0
    while index < len(values):
        remaining = len(values) - index
        best: tuple[int, int, int] | None = None
        for period in range(1, min(maximum_pattern, remaining // 2) + 1):
            if values[index:index + period] != values[index + period:index + 2 * period]:
                continue
            repeat = 2
            while (
                index + (repeat + 1) * period <= len(values)
                and values[index:index + period]
                == values[index + repeat * period:index + (repeat + 1) * period]
            ):
                repeat += 1
            saving = period * (repeat - 1)
            if best is None or saving > best[2]:
                best = (period, repeat, saving)
        if best is None:
            result.append(values[index])
            index += 1
            continue
        period, repeat, _ = best
        start = len(result)
        result.extend(values[index:index + period])
        groups.append((start, period, repeat))
        index += period * repeat
    return result, groups


def raw_events_to_degraded_store(
    raw_path: Path,
    store_path: Path,
    summary_path: Path,
    *,
    project: str,
    test: str,
    test_class: str,
    test_method: str,
    process_exit_code: int | None,
    assertion_instrumentation: dict[str, Any],
    defect_context: dict[str, Any],
    capture_config: dict[str, Any],
    threshold_bytes: int,
    degradation_reason: str = "raw_trace_size_threshold",
) -> dict[str, Any]:
    """Build a value-free, subtree-interned query store from a huge raw trace.

    Complete child subgraphs are identified bottom-up.  Equal subgraphs share a
    single SQLite node, while adjacent repeated child sequences are represented
    once plus a repetition record (AAA -> A, ABABAB -> AB).
    """
    if degradation_reason not in {
        "raw_trace_size_threshold", "capture_timeout",
        "conversion_resource_fallback",
    }:
        raise ValueError("invalid trace degradation reason")
    temporary = store_path.with_suffix(store_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    connection = _connect(temporary)
    try:
        connection.executescript(_QUERY_SCHEMA)
        method_keys: dict[tuple[str, str, str], int] = {}
        call_method_keys: set[tuple[str, str, str]] = set()
        structures: dict[tuple[Any, ...], int] = {}
        # frame: parent, class, method, descriptor, excluded, is_call,
        #        enter_seq, origin_line, compact child node ids
        active: dict[int, list[Any]] = {}
        active_descriptors: dict[int, str] = {}
        starts: list[dict[str, Any]] = []
        ends: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        assertion_events = 0
        event_count = entered = logical_call_count = 0
        previous_invocation_id = 0

        def method_key(key: tuple[str, str, str]) -> int:
            existing = method_keys.get(key)
            if existing is not None:
                return existing
            value = len(method_keys) + 1
            method_keys[key] = value
            connection.execute(
                "INSERT INTO query_methods VALUES (?,?,?,?,?)",
                (value, None, *key),
            )
            return value

        def close_node(invocation_id: int, event: dict[str, Any]) -> int | None:
            frame = active.pop(invocation_id, None)
            if frame is None:
                raise ValueError(f"exit without ENTER: invocation {invocation_id}")
            if frame[4]:
                return None
            children, groups = _compressed_sequence(frame[8])
            if not frame[5] and not children:
                return None
            key = (str(frame[1]), str(frame[2]), str(frame[3]))
            key_id = method_key(key)
            exit_type = str(event.get("type") or "THROW")
            exception_class = (
                str(event.get("exception_class") or "")
                if exit_type == "THROW" else ""
            )
            structure_key = (
                key_id, 1 if frame[5] else 0, exit_type, exception_class,
                tuple(children), tuple(groups),
            )
            existing = structures.get(structure_key)
            if existing is not None:
                return existing
            structure = _json(structure_key)
            digest = hashlib.sha256(structure.encode("utf-8")).digest()
            connection.execute(
                """INSERT INTO query_nodes VALUES (
                       ?,0,?,?,?,?,?,?,?,?,?,?,NULL,?,?
                   )""",
                (
                    invocation_id, 1 if frame[5] else 0, key_id,
                    int(frame[6]), int(event.get("seq") or 0), exit_type,
                    int(frame[7]), None, None, exception_class,
                    str(event.get("message") or "") if exit_type == "THROW" else "",
                    digest, structure,
                ),
            )
            structures[structure_key] = invocation_id
            connection.executemany(
                "INSERT INTO query_edges VALUES (?,?,?)",
                [
                    (invocation_id, ordinal, child_id)
                    for ordinal, child_id in enumerate(children)
                ],
            )
            connection.executemany(
                "INSERT INTO query_repetitions VALUES (?,?,?,?)",
                [
                    (invocation_id, start, length, repeat)
                    for start, length, repeat in groups
                ],
            )
            # Keep one deterministic representative caller path for each
            # interned child.  The graph remains bounded even when the same
            # complete subtree occurred millions of times.
            connection.executemany(
                "UPDATE query_nodes SET parent_id=? "
                "WHERE invocation_id=? AND parent_id=0",
                [(invocation_id, child_id) for child_id in children],
            )
            return invocation_id

        with connection:
            for line_number, _, event in iter_raw_events(raw_path):
                event_count += 1
                event_type = event.get("type")
                if event_type not in ALLOWED_EVENTS:
                    raise ValueError(
                        f"unsupported event type at line {line_number}: {event_type!r}"
                    )
                _validate_capture_event(
                    event, capture_config, active_descriptors,
                    validate_payload=False,
                )
                if event_type == "ENTER":
                    invocation_id = int(event.get("invocation_id") or 0)
                    parent_id = int(event.get("parent_id") or 0)
                    if invocation_id <= previous_invocation_id:
                        raise ValueError("non-monotonic invocation ids in raw trace")
                    previous_invocation_id = invocation_id
                    parent = active.get(parent_id)
                    if parent_id and parent is None:
                        raise ValueError("missing active invocation parent")
                    class_name = str(event.get("class") or "")
                    method = str(event.get("method") or "")
                    descriptor = str(event.get("descriptor") or "")
                    excluded = method == "<clinit>" or bool(
                        parent is not None and parent[4]
                    )
                    is_call = bool(
                        parent_id and parent is not None and not excluded
                        and not _is_noise_method(parent[1], parent[2])
                        and not _is_noise_method(class_name, method)
                    )
                    active[invocation_id] = [
                        parent_id, class_name, method, descriptor, excluded,
                        is_call, int(event.get("seq") or 0),
                        int(event.get("origin_test_line") or 0), [],
                    ]
                    entered += 1
                    if is_call:
                        logical_call_count += 1
                        call_method_keys.add((class_name, method, descriptor))
                elif event_type in {"RETURN", "THROW"}:
                    invocation_id = int(event.get("invocation_id") or 0)
                    parent_id = int(active.get(invocation_id, [0])[0])
                    node_id = close_node(invocation_id, event)
                    if node_id is not None and parent_id in active:
                        active[parent_id][8].append(node_id)
                elif event_type == "TEST_START":
                    starts.append(event)
                elif event_type == "TEST_END":
                    ends.append(event)
                elif event_type == "TEST_FAILURE":
                    failures.append(event)
                elif event_type in ASSERTION_EVENTS:
                    assertion_events += 1

            if entered == 0:
                raise ValueError("fullchain v2 requires at least one ENTER event")
            if not starts or not ends:
                raise ValueError("test process did not record TEST_START/TEST_END")
            test_start, test_end = starts[-1], ends[-1]
            terminal_stack_overflow = (
                test_end.get("successful") is False
                and any(
                    item.get("exception_class") == "java.lang.StackOverflowError"
                    for item in failures
                )
            )
            if active:
                if not terminal_stack_overflow:
                    raise ValueError("unclosed invocations: " + str(sorted(active)[:20]))
                failure = next(
                    item for item in failures
                    if item.get("exception_class") == "java.lang.StackOverflowError"
                )
                terminal_seq = int(failure.get("seq") or test_end.get("seq") or 0)
                for invocation_id in reversed(list(active)):
                    parent_id = int(active[invocation_id][0])
                    node_id = close_node(invocation_id, {
                        "type": "THROW", "seq": terminal_seq,
                        "exception_class": "java.lang.StackOverflowError",
                        "message": str(failure.get("message") or ""),
                    })
                    if node_id is not None and parent_id in active:
                        active[parent_id][8].append(node_id)
                active_descriptors.clear()
            if test_start.get("agent_protocol_version") != AGENT_PROTOCOL_VERSION:
                raise ValueError("requested Fullchain agent v5 but raw trace used another protocol")
            if test_start.get("value_capture") != capture_config:
                raise ValueError(
                    "requested value capture configuration does not match agent TEST_START"
                )
            derived_exit_code = 0 if test_end.get("successful") is True else 1
            if process_exit_code is None:
                process_exit_code = derived_exit_code
            elif process_exit_code != derived_exit_code:
                raise ValueError(
                    "Java process exit code does not match the recorded TEST_END outcome"
                )
            if logical_call_count == 0:
                raise ValueError("no calls remain after noise filtering")
            connection.execute(
                "CREATE INDEX query_node_method ON query_nodes(method_key,is_call,invocation_id)"
            )
            connection.execute(
                "CREATE INDEX query_node_parent ON query_nodes(parent_id,enter_seq,invocation_id)"
            )
            connection.execute(
                "CREATE INDEX query_edge_child ON query_edges(child_id)"
            )
            effective_capture = dict(capture_config)
            effective_capture["capture_values"] = False
            folding = {
                "original_call_count": logical_call_count,
                "retained_call_count": logical_call_count,
                "folded_call_count": 0,
                "assertion_interval_count": 0,
                "successful_assertion_count": 0,
                "failed_assertion_count": 0,
                "unmatched_assertion_event_count": assertion_events,
            }
            degradation = {
                "enabled": True,
                "reason": degradation_reason,
                "raw_size_bytes": raw_path.stat().st_size,
                "threshold_bytes": threshold_bytes,
                "values_omitted": True,
                "subtree_counts_omitted": True,
                "repeated_subtrees_compressed": True,
                "stored_call_node_count": int(connection.execute(
                    "SELECT count(*) FROM query_nodes WHERE is_call=1"
                ).fetchone()[0]),
            }
            for key, value in {
                "storage_kind": DEGRADED_STORE_KIND,
                "schema_version": 1,
                "project": project,
                "test": test,
                "test_class": test_class,
                "test_method": test_method,
                "process_exit_code": process_exit_code,
                "test_start": test_start,
                "test_end": test_end,
                "test_failures": failures,
                "requested_capture": capture_config,
                "capture": effective_capture,
                "assertion_instrumentation": assertion_instrumentation,
                "defect_context": defect_context,
                "assertion_folding": folding,
                "assertion_folding_strategy": "unavailable-degraded",
                "event_count": event_count,
                "call_count": logical_call_count,
                "degradation": degradation,
            }.items():
                _put_metadata(connection, key, value)
        connection.close()
        temporary.replace(store_path)
        methods = sorted([list(key) for key in call_method_keys])
        summary = {
            "schema": METHOD_SUMMARY_SCHEMA,
            "schema_version": METHOD_SUMMARY_VERSION,
            "test": test,
            # Reuse compatibility is based on what the agent was asked to
            # capture, even though the degraded query store intentionally drops it.
            "capture": capture_config,
            "call_count": logical_call_count,
            "methods": methods,
        }
        validate_method_summary(summary)
        write_compact_json(summary_path, summary)
        return summary
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise


def _copy_metadata(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    target.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        list(source.execute("SELECT key,value FROM metadata")),
    )


def _general_to_query_store(
    source_path: Path,
    target_path: Path,
) -> None:
    source = _connect(source_path)
    target = _connect(target_path)
    try:
        target.executescript(_QUERY_SCHEMA)
        with target:
            _copy_metadata(source, target)
            keys = [
                (str(row[0]), str(row[1]), str(row[2]))
                for row in source.execute(
                    """SELECT DISTINCT invocation.class_name,invocation.method,
                                      invocation.descriptor
                       FROM invocations AS invocation
                       WHERE invocation.invocation_id IN (
                           SELECT calls.invocation_id FROM calls
                           LEFT JOIN folded USING(invocation_id)
                           WHERE folded.invocation_id IS NULL
                           UNION
                           SELECT invocation.parent_id FROM calls
                           JOIN invocations AS invocation USING(invocation_id)
                           LEFT JOIN folded USING(invocation_id)
                           WHERE folded.invocation_id IS NULL
                       ) ORDER BY 1,2,3"""
                )
            ]
            key_ids = {key: index for index, key in enumerate(keys, 1)}
            target.executemany(
                "INSERT INTO query_methods VALUES (?,?,?,?,?)",
                [(value, None, *key) for key, value in key_ids.items()],
            )
            call_ids = {
                int(row[0]) for row in source.execute(
                    "SELECT calls.invocation_id FROM calls "
                    "LEFT JOIN folded USING(invocation_id) "
                    "WHERE folded.invocation_id IS NULL"
                )
            }
            node_ids = set(call_ids)
            node_ids.update(
                int(row[0]) for row in source.execute(
                    """SELECT DISTINCT invocation.parent_id FROM calls
                       JOIN invocations AS invocation USING(invocation_id)
                       LEFT JOIN folded USING(invocation_id)
                       WHERE folded.invocation_id IS NULL"""
                )
            )
            pending_counts: dict[int, int] = {}
            rows: list[tuple[Any, ...]] = []
            ordered = source.execute(
                """SELECT invocation.invocation_id,invocation.parent_id,
                          invocation.class_name,invocation.method,invocation.descriptor,
                          invocation.enter_seq,exits.seq,exits.exit_type,
                          invocation.origin_test_line,invocation.arguments_json,
                          exits.return_value_json,exits.exception_class,exits.message
                   FROM invocations AS invocation JOIN exits USING(invocation_id)
                   ORDER BY invocation.enter_seq DESC,invocation.invocation_id DESC"""
            )
            for row in ordered:
                invocation_id = int(row[0])
                if invocation_id not in node_ids:
                    continue
                parent_id = int(row[1])
                count = (1 if invocation_id in call_ids else 0) + pending_counts.pop(
                    invocation_id, 0
                )
                if parent_id in node_ids:
                    pending_counts[parent_id] = pending_counts.get(parent_id, 0) + count
                key = (str(row[2]), str(row[3]), str(row[4]))
                rows.append((
                    invocation_id, parent_id, 1 if invocation_id in call_ids else 0,
                    key_ids[key], int(row[5]), int(row[6]), str(row[7]),
                    int(row[8]), row[9], row[10], str(row[11]), str(row[12]),
                    count, None, None,
                ))
                if len(rows) >= 10000:
                    target.executemany(
                        "INSERT INTO query_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        rows,
                    )
                    rows.clear()
            target.executemany(
                "INSERT INTO query_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            edge_rows: list[tuple[int, int, int]] = []
            current_parent = None
            ordinal = 0
            for parent_id, child_id in target.execute(
                """SELECT parent_id,invocation_id FROM query_nodes
                   WHERE is_call=1 ORDER BY parent_id,enter_seq,invocation_id"""
            ):
                parent_id = int(parent_id)
                if current_parent != parent_id:
                    current_parent, ordinal = parent_id, 0
                edge_rows.append((parent_id, ordinal, int(child_id)))
                ordinal += 1
                if len(edge_rows) >= 10000:
                    target.executemany(
                        "INSERT INTO query_edges VALUES (?,?,?)", edge_rows
                    )
                    edge_rows.clear()
            target.executemany("INSERT INTO query_edges VALUES (?,?,?)", edge_rows)
            target.execute(
                "CREATE INDEX query_node_method ON query_nodes(method_key,is_call,invocation_id)"
            )
            target.execute(
                "CREATE INDEX query_node_parent ON query_nodes(parent_id,enter_seq,invocation_id)"
            )
            target.execute("CREATE INDEX query_edge_child ON query_edges(child_id)")
            _put_metadata(target, "storage_kind", QUERY_STORE_KIND)
            _put_metadata(target, "degradation", {
                "enabled": False,
                "values_omitted": False,
                "subtree_counts_omitted": False,
                "repeated_subtrees_compressed": False,
                "stored_call_node_count": len(call_ids),
            })
    finally:
        source.close()
        target.close()


def _assign_query_method_ids(
    connection: sqlite3.Connection,
    method_ids: dict[tuple[str, str, str], str],
) -> None:
    rows = [
        (int(row[0]), (str(row[1]), str(row[2]), str(row[3])))
        for row in connection.execute(
            "SELECT method_key,class_name,method,descriptor FROM query_methods "
            "ORDER BY class_name,method,descriptor"
        )
    ]
    boundary = [key for _, key in rows if key not in method_ids]
    boundary_ids = {key: f"B{index}" for index, key in enumerate(boundary, 1)}
    connection.executemany(
        "UPDATE query_methods SET method_id=? WHERE method_key=?",
        [
            (method_ids.get(key) or boundary_ids[key], method_key)
            for method_key, key in rows
        ],
    )
    missing = connection.execute(
        "SELECT invocation_id FROM query_nodes JOIN query_methods USING(method_key) "
        "WHERE query_methods.method_id IS NULL LIMIT 1"
    ).fetchone()
    if missing is not None:
        raise ValueError("query store contains an unmapped method")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(STREAM_BUFFER_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def final_trace_archive_path(path: Path) -> Path:
    """Return the compressed sidecar for a logical final SQLite path."""
    return path.with_name(path.name + FINAL_TRACE_ARCHIVE_SUFFIX)


def final_trace_artifact_path(path: Path) -> Path:
    """Resolve an uncompressed final trace or its compressed sidecar."""
    if path.is_file():
        return path
    archive = final_trace_archive_path(path)
    if archive.is_file():
        return archive
    raise ValueError(f"SQLite refinement trace not found: {path}")


def final_trace_artifact_exists(path: Path) -> bool:
    return path.is_file() or final_trace_archive_path(path).is_file()


def _iter_final_archive_chunks(archive: Path) -> Iterator[bytes]:
    """Decompress one final-trace frame and require its explicit end marker."""
    try:
        with archive.open("rb") as source:
            decompressor = zstandard.ZstdDecompressor().decompressobj()
            while True:
                chunk = source.read(STREAM_BUFFER_BYTES)
                if not chunk:
                    break
                plain = decompressor.decompress(chunk)
                if plain:
                    yield plain
                if decompressor.eof:
                    if decompressor.unused_data or source.read(1):
                        raise ValueError(
                            f"SQLite trace archive has trailing data: {archive}"
                        )
                    break
            if not decompressor.eof:
                raise ValueError(f"incomplete SQLite trace archive: {archive}")
            tail = decompressor.flush()
            if tail:
                yield tail
    except zstandard.ZstdError as error:
        raise ValueError(f"cannot decompress SQLite trace {archive}: {error}") from error
    except OSError as error:
        raise ValueError(f"cannot read SQLite trace {archive}: {error}") from error


def _sha256_zstd_file(archive: Path) -> str:
    digest = hashlib.sha256()
    for chunk in _iter_final_archive_chunks(archive):
        digest.update(chunk)
    return digest.hexdigest()


def final_trace_sha256(path: Path) -> str:
    """Hash the logical SQLite bytes without retaining a decompressed copy."""
    artifact = final_trace_artifact_path(path)
    return _sha256_file(path) if artifact == path else _sha256_zstd_file(artifact)


def archive_final_trace_store(path: Path, *, level: int = 1) -> Path:
    """Atomically archive a final SQLite trace after lossless verification."""
    if not path.is_file():
        archive = final_trace_archive_path(path)
        if archive.is_file():
            _sha256_zstd_file(archive)
            return archive
        raise ValueError(f"final SQLite trace is missing: {path}")
    expected = _sha256_file(path)
    archive = final_trace_archive_path(path)
    compress_zstd_file(path, archive, level=level)
    try:
        if _sha256_zstd_file(archive) != expected:
            raise ValueError("compressed SQLite trace fingerprint mismatch")
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    path.unlink()
    return archive


def _materialize_final_trace_store(path: Path) -> tuple[Path, Path | None]:
    artifact = final_trace_artifact_path(path)
    if artifact == path:
        return path, None
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".materializing", dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            for chunk in _iter_final_archive_chunks(artifact):
                handle.write(chunk)
        return temporary, temporary
    except (OSError, ValueError) as error:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"cannot materialize SQLite trace {artifact}: {error}") from error


def finalize_trace_store(
    source_path: Path,
    target_path: Path,
    *,
    project: str,
    test_id: str,
    test: str,
    method_ids: dict[tuple[str, str, str], str],
    catalog_fingerprint: str,
) -> str:
    """Promote one conversion store to the final directly-queryable artifact."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_suffix(target_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source = _connect(source_path)
    try:
        kind = _metadata_or(source, "storage_kind")
    finally:
        source.close()
    try:
        if kind == DEGRADED_STORE_KIND:
            source_path.replace(temporary)
        elif kind in {None, "raw-jsonl"}:
            _general_to_query_store(source_path, temporary)
        else:
            raise ValueError(f"unsupported trace store kind for finalization: {kind}")
        connection = _connect(temporary)
        try:
            with connection:
                if str(_metadata(connection, "project")) != project:
                    raise ValueError("trace store project does not match suite")
                if str(_metadata(connection, "test")) != test:
                    raise ValueError("trace store test does not match suite")
                _assign_query_method_ids(connection, method_ids)
                _put_metadata(connection, "final_schema", "sqlite-refinement-trace")
                _put_metadata(connection, "final_schema_version", 1)
                _put_metadata(connection, "test_id", test_id)
                _put_metadata(
                    connection, "method_catalog_fingerprint", catalog_fingerprint
                )
        finally:
            connection.close()
        temporary.replace(target_path)
        final_trace_archive_path(target_path).unlink(missing_ok=True)
        if source_path.exists():
            source_path.unlink()
        return _sha256_file(target_path)
    except Exception:
        # A degraded source was moved into the temporary name. Restore it on
        # failure so collection remains resumable and diagnostic evidence stays.
        if kind == DEGRADED_STORE_KIND and temporary.exists() and not source_path.exists():
            temporary.replace(source_path)
        else:
            temporary.unlink(missing_ok=True)
        raise


class _SqlIdMembership:
    def __init__(self, topology: "SQLiteTraceTopology") -> None:
        self.topology = topology

    def __contains__(self, invocation_id: object) -> bool:
        return self.topology.has_node(int(invocation_id)) if isinstance(invocation_id, int) else False


class _SqlInvocationSequence(Sequence[int]):
    def __init__(self, connection: sqlite3.Connection, method_id: str) -> None:
        self.connection = connection
        self.method_id = method_id

    def __len__(self) -> int:
        return int(self.connection.execute(
            """SELECT count(*) FROM query_nodes JOIN query_methods USING(method_key)
               WHERE is_call=1 AND method_id=?""",
            (self.method_id,),
        ).fetchone()[0])

    def __getitem__(self, index: int | slice) -> int | tuple[int, ...]:
        if isinstance(index, slice):
            start = 0 if index.start is None else index.start
            stop = len(self) if index.stop is None else index.stop
            if start < 0 or stop < 0 or (index.step not in (None, 1)):
                return tuple(list(self)[index])
            return tuple(
                int(row[0]) for row in self.connection.execute(
                    """SELECT invocation_id FROM query_nodes
                       JOIN query_methods USING(method_key)
                       WHERE is_call=1 AND method_id=?
                       ORDER BY enter_seq,invocation_id LIMIT ? OFFSET ?""",
                    (self.method_id, max(0, stop - start), start),
                )
            )
        if index < 0:
            index += len(self)
        row = self.connection.execute(
            """SELECT invocation_id FROM query_nodes
               JOIN query_methods USING(method_key)
               WHERE is_call=1 AND method_id=?
               ORDER BY enter_seq,invocation_id LIMIT 1 OFFSET ?""",
            (self.method_id, index),
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return int(row[0])


class _SqlMethodInvocations(Mapping[str, Sequence[int]]):
    def __init__(self, topology: "SQLiteTraceTopology") -> None:
        self.topology = topology

    def __iter__(self):
        for row in self.topology.connection.execute(
            """SELECT DISTINCT method_id FROM query_methods
               JOIN query_nodes USING(method_key)
               WHERE is_call=1 AND method_id LIKE 'M%' ORDER BY method_id"""
        ):
            yield str(row[0])

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __getitem__(self, method_id: str) -> Sequence[int]:
        value = _SqlInvocationSequence(self.topology.connection, method_id)
        if len(value) == 0:
            raise KeyError(method_id)
        return value

    def get(self, method_id: str, default=None):
        value = _SqlInvocationSequence(self.topology.connection, method_id)
        return value if len(value) else default


class SQLiteTraceTopology:
    """Lazy topology facade over a final SQLite refinement trace."""

    def __init__(self, path: Path) -> None:
        self.path = path
        materialized, temporary = _materialize_final_trace_store(path)
        self._temporary_path = temporary
        self._closed = False
        self.connection = None
        try:
            self.connection = sqlite3.connect(
                f"file:{materialized}?mode=ro", uri=True
            )
            # On POSIX the open read-only descriptor remains usable after unlink.
            # This prevents crashes from leaving decompressed trace artifacts.
            if self._temporary_path is not None:
                try:
                    self._temporary_path.unlink()
                    self._temporary_path = None
                except OSError:
                    pass
            if _metadata(self.connection, "final_schema") != "sqlite-refinement-trace":
                raise ValueError("unsupported SQLite refinement trace schema")
            if _metadata(self.connection, "final_schema_version") != 1:
                raise ValueError("unsupported SQLite refinement trace version")
        except Exception:
            self.close()
            raise
        self.degradation = dict(_metadata(self.connection, "degradation"))
        self.exact_omission_counts = not bool(
            self.degradation.get("subtree_counts_omitted")
        )
        self.stored_call_count = int(self.connection.execute(
            "SELECT count(*) FROM query_nodes WHERE is_call=1"
        ).fetchone()[0])
        context = _metadata(self.connection, "defect_context")
        failures = [
            {
                "exception_class": str(item.get("exception_class") or ""),
                "message": str(item.get("message") or ""),
            }
            for item in _metadata(self.connection, "test_failures")
        ]
        self.trace = {
            "schema": "sqlite-refinement-trace",
            "schema_version": 1,
            "project": str(_metadata(self.connection, "project")),
            "test_id": str(_metadata(self.connection, "test_id")),
            "test": str(_metadata(self.connection, "test")),
            "method_catalog_fingerprint": str(
                _metadata(self.connection, "method_catalog_fingerprint")
            ),
            "capture": _metadata(self.connection, "capture"),
            "degradation": self.degradation,
            "failure": {
                "process_exit_code": int(_metadata(self.connection, "process_exit_code")),
                "error_stack": str(context.get("error_stack") or ""),
                "test_output": str(context.get("test_output") or ""),
                "events": failures,
            },
            "assertion_folding": {
                **_metadata(self.connection, "assertion_folding"),
                "strategy": _metadata_or(
                    self.connection, "assertion_folding_strategy", "legacy-enabled"
                ),
            },
            "call_count": int(_metadata(self.connection, "call_count")),
            "raw_capture_fingerprint": _metadata_or(
                self.connection, "raw_capture_fingerprint"
            ),
        }
        self.methods = {
            str(row[0]): (str(row[1]), str(row[2]), str(row[3]))
            for row in self.connection.execute(
                "SELECT method_id,class_name,method,descriptor FROM query_methods"
            )
        }
        self.row_positions = _SqlIdMembership(self)
        self.method_invocations = _SqlMethodInvocations(self)

    @classmethod
    def open(cls, path: Path) -> "SQLiteTraceTopology":
        return cls(path)

    def close(self) -> None:
        if not self._closed:
            if self.connection is not None:
                self.connection.close()
            self._closed = True
        if self._temporary_path is not None:
            self._temporary_path.unlink(missing_ok=True)
            self._temporary_path = None

    def __enter__(self) -> "SQLiteTraceTopology":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def has_node(self, invocation_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM query_nodes WHERE invocation_id=?", (invocation_id,)
        ).fetchone() is not None

    def _node_row(self, invocation_id: int) -> tuple[Any, ...]:
        row = self.connection.execute(
            """SELECT invocation_id,parent_id,method_id,enter_seq,exit_seq,
                      exit_type,origin_test_line,subtree_call_count,arguments_json,
                      result_json,exception_class,message,is_call
               FROM query_nodes JOIN query_methods USING(method_key)
               WHERE invocation_id=?""",
            (invocation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown refinement invocation: {invocation_id}")
        return row

    def row(self, invocation_id: int) -> list[Any]:
        row = self._node_row(invocation_id)
        children = list(self.children(invocation_id))
        count = int(row[7]) if row[7] is not None else 0
        prefix = [0]
        if self.exact_omission_counts:
            for child in children:
                prefix.append(prefix[-1] + self.subtree_call_count(child))
        else:
            prefix.extend(0 for _ in children)
        return [
            int(row[0]), int(row[1]), str(row[2]), int(row[3]), int(row[4]),
            str(row[5]), int(row[6]), children, count, None, None, prefix,
        ]

    def has_call(self, invocation_id: int) -> bool:
        row = self.connection.execute(
            "SELECT is_call FROM query_nodes WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        return row is not None and int(row[0]) == 1

    def children(self, invocation_id: int) -> tuple[int, ...]:
        return tuple(
            int(row[0]) for row in self.connection.execute(
                "SELECT child_id FROM query_edges WHERE parent_id=? ORDER BY ordinal",
                (invocation_id,),
            )
        )

    def repetition_groups(self, invocation_id: int) -> tuple[dict[str, int], ...]:
        return tuple({
            "start": int(row[0]), "pattern_length": int(row[1]),
            "repeat_count": int(row[2]),
        } for row in self.connection.execute(
            """SELECT start_ordinal,pattern_length,repeat_count
               FROM query_repetitions WHERE parent_id=? ORDER BY start_ordinal""",
            (invocation_id,),
        ))

    def subtree_call_count(self, invocation_id: int) -> int:
        row = self.connection.execute(
            "SELECT subtree_call_count FROM query_nodes WHERE invocation_id=?",
            (invocation_id,),
        ).fetchone()
        if row is None or row[0] is None:
            raise ValueError("subtree call counts are unavailable in degraded mode")
        return int(row[0])

    def child_range_count(self, invocation_id: int, start: int, end: int) -> int:
        if not self.exact_omission_counts:
            raise ValueError("child range counts are unavailable in degraded mode")
        children = self.children(invocation_id)[start:end]
        return sum(self.subtree_call_count(child) for child in children)

    def stored_subgraph_call_count(self, invocation_id: int) -> int:
        if not self.has_node(invocation_id):
            raise ValueError(f"unknown refinement invocation: {invocation_id}")
        row = self.connection.execute(
            """WITH RECURSIVE reachable(invocation_id) AS (
                   SELECT ?
                   UNION
                   SELECT edge.child_id
                   FROM query_edges AS edge
                   JOIN reachable AS parent
                     ON edge.parent_id=parent.invocation_id
               )
               SELECT count(*)
               FROM query_nodes AS node
               JOIN reachable USING(invocation_id)
               WHERE node.is_call=1""",
            (invocation_id,),
        ).fetchone()
        return int(row[0])

    def stored_outer_context_sides(
        self, invocation_id: int,
    ) -> tuple[bool, bool]:
        row = self.connection.execute(
            "SELECT enter_seq,exit_seq FROM query_nodes WHERE invocation_id=?",
            (invocation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown refinement invocation: {invocation_id}")
        sides = self.connection.execute(
            """WITH RECURSIVE reachable(invocation_id) AS (
                   SELECT ?
                   UNION
                   SELECT edge.child_id
                   FROM query_edges AS edge
                   JOIN reachable AS parent
                     ON edge.parent_id=parent.invocation_id
               )
               SELECT
                 EXISTS(
                   SELECT 1 FROM query_nodes AS node
                   WHERE node.is_call=1
                     AND node.invocation_id NOT IN (SELECT invocation_id FROM reachable)
                     AND node.enter_seq < ?
                 ),
                 EXISTS(
                   SELECT 1 FROM query_nodes AS node
                   WHERE node.is_call=1
                     AND node.invocation_id NOT IN (SELECT invocation_id FROM reachable)
                     AND node.enter_seq > ?
                 )""",
            (invocation_id, int(row[0]), int(row[1])),
        ).fetchone()
        return bool(sides[0]), bool(sides[1])

    def calls_before(self, invocation_id: int) -> int:
        row = self.connection.execute(
            "SELECT enter_seq FROM query_nodes WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown refinement invocation: {invocation_id}")
        return int(self.connection.execute(
            "SELECT count(*) FROM query_nodes WHERE is_call=1 AND enter_seq<?",
            (int(row[0]),),
        ).fetchone()[0])

    def method_id(self, invocation_id: int) -> str:
        return str(self._node_row(invocation_id)[2])

    def invocation(self, invocation_id: int) -> dict[str, Any]:
        row = self._node_row(invocation_id)
        class_name, method, descriptor = self.methods[str(row[2])]
        value: dict[str, Any] = {
            "invocation_id": int(row[0]), "parent_id": int(row[1]),
            "class": class_name, "method": method, "descriptor": descriptor,
            "enter_seq": int(row[3]), "exit_seq": int(row[4]),
            "exit_type": str(row[5]), "origin_test_line": int(row[6]),
        }
        if row[8] is not None:
            value["arguments"] = json.loads(str(row[8]))
        if str(row[5]) == "THROW":
            value["exception_class"] = str(row[10])
            value["message"] = str(row[11])
        elif row[9] is not None:
            value["return_value"] = json.loads(str(row[9]))
        return value

    def continuous_event_window(
        self, invocation_id: int, *, before: int, after: int,
    ) -> dict[str, Any]:
        """Return a deterministic contiguous CALL/RETURN/THROW event slice."""
        if before < 0 or after < 0:
            raise ValueError("continuous event budgets must be non-negative")
        if self.degradation.get("enabled"):
            raise ValueError("raw-trace does not support degraded trace stores")
        row = self.connection.execute(
            "SELECT enter_seq FROM query_nodes WHERE invocation_id=? AND is_call=1",
            (invocation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown refinement invocation: {invocation_id}")
        focus_seq = int(row[0])
        event_sql = """
            SELECT enter_seq AS seq, 0 AS event_order, 'CALL' AS event_type,
                   invocation_id
            FROM query_nodes WHERE is_call=1
            UNION ALL
            SELECT exit_seq AS seq, 1 AS event_order,
                   CASE WHEN exit_type='THROW' THEN 'THROW' ELSE 'RETURN' END,
                   invocation_id
            FROM query_nodes WHERE is_call=1
        """
        earlier = int(self.connection.execute(
            "SELECT count(*) FROM (" + event_sql + ") WHERE seq<?", (focus_seq,)
        ).fetchone()[0])
        later = int(self.connection.execute(
            "SELECT count(*) FROM (" + event_sql + ") WHERE seq>?", (focus_seq,)
        ).fetchone()[0])
        take_before = min(before, earlier)
        take_after = min(after, later)
        remaining = before + after - take_before - take_after
        extra_after = min(max(0, later - take_after), remaining)
        take_after += extra_after
        remaining -= extra_after
        take_before += min(max(0, earlier - take_before), remaining)
        prior = list(self.connection.execute(
            "SELECT seq,event_order,event_type,invocation_id FROM (" + event_sql + ") "
            "WHERE seq<? ORDER BY seq DESC,event_order DESC,invocation_id DESC LIMIT ?",
            (focus_seq, take_before),
        ))
        following = list(self.connection.execute(
            "SELECT seq,event_order,event_type,invocation_id FROM (" + event_sql + ") "
            "WHERE seq>? ORDER BY seq,event_order,invocation_id LIMIT ?",
            (focus_seq, take_after),
        ))
        prior.reverse()
        selected = prior + [(focus_seq, 0, "CALL", invocation_id)] + following
        events = []
        for seq, _, event_type, raw_id in selected:
            invocation = self.invocation(int(raw_id))
            parent = self.invocation(int(invocation["parent_id"]))
            events.append({
                "seq": int(seq),
                "type": str(event_type),
                "invocation": invocation,
                "caller": parent,
            })
        return {
            "events": events,
            "event_count": len(events),
            "call_count": len({int(item["invocation"]["invocation_id"]) for item in events}),
            "omitted_before": max(0, earlier - len(prior)),
            "omitted_after": max(0, later - len(following)),
        }

    def call(self, invocation_id: int) -> dict[str, Any] | None:
        if not self.has_call(invocation_id):
            return None
        invocation = self.invocation(invocation_id)
        parent = self.invocation(int(invocation["parent_id"]))
        return {
            "caller": f"{parent['class']}.{parent['method']}",
            "callee": f"{invocation['class']}.{invocation['method']}",
            "caller_class": parent["class"], "callee_class": invocation["class"],
            "caller_method": parent["method"], "callee_method": invocation["method"],
            "caller_descriptor": parent["descriptor"],
            "callee_descriptor": invocation["descriptor"],
            "parent_invocation_id": int(invocation["parent_id"]),
            "invocation_id": invocation_id, "enter_seq": int(invocation["enter_seq"]),
            "exit_seq": int(invocation["exit_seq"]),
            "exit_type": str(invocation["exit_type"]),
            "origin_test_line": int(invocation["origin_test_line"]),
        }

    def node(self, invocation_id: int) -> dict[str, Any]:
        invocation = self.invocation(invocation_id)
        call = self.call(invocation_id)
        participants = {str(invocation["class"])}
        if call is not None:
            participants.add(str(call["caller_class"]))
        return {
            "representative_invocation_id": invocation_id,
            "call": call, "invocation": invocation,
            "participant_classes": sorted(participants),
        }

    def caller_signature(self, invocation_id: int) -> str:
        from dpex.domain.refinement_trace import method_signature
        parent_id = int(self._node_row(invocation_id)[1])
        return method_signature(self.methods[self.method_id(parent_id)])
