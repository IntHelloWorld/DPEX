# Fullchain Tracer

Java 8-compatible bytecode agent for the fault-localization pipeline. Version 5 emits JSONL events for method `ENTER`, `RETURN`, and `THROW`, plus `TEST_START`, assertion boundaries, `TEST_FAILURE`, and `TEST_END`. Each invocation includes a stable invocation/parent relationship, JVM method descriptor, timestamps, thread metadata, and the test-source line that triggered it. By default, `ENTER` records a bounded argument snapshot and normal `RETURN` records a bounded return snapshot; `void` is explicit and `THROW` never carries a fabricated return value. `TEST_START` records protocol version 5 and the effective value-capture configuration.

Value summarization handles nulls, scalars, strings, characters, enums, arrays, and an explicit whitelist of JDK collections/maps. Ordinary application objects are represented only as `<fully.qualified.Class>`: the agent does not call application `toString()`, inspect fields, capture `this`, or expose object addresses. Capture errors and cycles become explicit placeholders and cannot escape into the tested program. JVM class initializers (`<clinit>`) are retained only in raw events so the Python trace builder can remove their complete invocation subtrees without inventing caller relationships.

Build and install from the repository root:

```bash
mvn -f fullchain_tracer/pom.xml clean package
cp fullchain_tracer/target/fullchain-tracer.jar lib/fullchain-tracer.jar
```

`python -m mllmfl trace` sets the raw-event and selected-test system properties, supplies instrumented package prefixes through
`-javaagent`, validates the event stream, and writes the versioned `fullchain-execution`, assertion
folding, and refinement lookup artifacts. Legacy v3 and v4 execution events remain readable, but
missing values are never synthesized. `CALL`-only traces are not supported.
