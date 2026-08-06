# Fullchain Tracer

Java 8-compatible bytecode agent for the fault-localization pipeline. Version 3 emits JSONL events for method `ENTER`, `RETURN`, and `THROW`, plus `TEST_START`, `TEST_FAILURE`, and `TEST_END`. Each invocation includes a stable invocation/parent relationship, JVM method descriptor, timestamps, thread metadata, and the test-source line that triggered it. Failure events include the matching test stack frame. Object identity is intentionally not captured. JVM class initializers (`<clinit>`) are retained only in raw events so the Python trace builder can remove their complete invocation subtrees without inventing caller relationships.

Build and install from the repository root:

```bash
mvn -f fullchain_tracer/pom.xml clean package
cp fullchain_tracer/target/fullchain-tracer.jar lib/fullchain-tracer.jar
```

`python -m mllmfl trace` sets the raw-event and selected-test system properties, supplies instrumented package prefixes through
`-javaagent`, validates the event stream, and writes the versioned `fullchain-trace` and
`fullchain-execution` artifacts. It also writes a conservative test-method backward slice. Legacy
`CALL`-only traces are not supported.
