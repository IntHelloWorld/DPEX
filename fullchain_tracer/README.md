# Fullchain Tracer

Java 8-compatible bytecode agent for the fault-localization pipeline. Version 2 emits JSONL events for method `ENTER`, `RETURN`, and `THROW`, plus `TEST_START`, `TEST_FAILURE`, and `TEST_END`. Each invocation includes a stable invocation/parent relationship, JVM method descriptor, timestamps, and thread metadata. Object identity is intentionally not captured.

Build and install from the repository root:

```bash
mvn -f fullchain_tracer/pom.xml clean package
cp fullchain_tracer/target/fullchain-tracer.jar lib/fullchain-tracer.jar
```

`python -m mllmfl trace` sets `fltrace.raw.file`, supplies instrumented package prefixes through
`-javaagent`, validates the event stream, and writes the versioned `fullchain-trace` and
`fullchain-window` artifacts. Legacy `CALL`-only traces are not supported.
