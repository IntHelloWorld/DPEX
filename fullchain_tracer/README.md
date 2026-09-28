# Fullchain Tracer

Java 8-compatible bytecode agent for the fault-localization pipeline. Version 5 emits JSONL events for method `ENTER`, `RETURN`, and `THROW`, plus `TEST_START`, assertion boundaries, `TEST_FAILURE`, and `TEST_END`. Each invocation includes a stable invocation/parent relationship, JVM method descriptor, timestamps, thread metadata, and the test-source line that triggered it. By default, `ENTER` records a bounded argument snapshot and normal `RETURN` records a bounded return snapshot; `void` is explicit and `THROW` never carries a fabricated return value. `TEST_START` records protocol version 5 and the effective value-capture configuration.

Value summarization handles nulls, scalars, strings, characters, enums, arrays, and an explicit whitelist of JDK collections/maps. Ordinary application objects are represented only as `<fully.qualified.Class>`: the agent does not call application `toString()`, inspect fields, capture `this`, or expose object addresses. Capture errors and cycles become explicit placeholders and cannot escape into the tested program. JVM class initializers (`<clinit>`) are retained only in raw events so the Python trace builder can remove their complete invocation subtrees without inventing caller relationships.

When `fltrace.capture.values=false`, the transformer also avoids materializing
`$args`/`$sig` and boxing `$_`; emitted events retain invocation topology and
terminal outcomes without argument or normal-return payloads.

Build and install from the repository root:

```bash
mvn -f fullchain_tracer/pom.xml clean package
cp fullchain_tracer/target/fullchain-tracer.jar lib/fullchain-tracer.jar
```

`python -m dpex trace` points the raw-event property at a named pipe. The unchanged JSONL event
stream is drained by `/usr/bin/zstd -1`, validated as a complete frame before atomic promotion, and
incrementally converted into the final queryable SQLite refinement trace. Streaming
decompression and terminal protocol records still reject incomplete input. Compressor failure, Java failure,
missing terminal events, and an incomplete Zstd stream are reported separately. The refinement
stage reads indexed SQLite rows directly; the retired refinement-trace v2 compressed JSON format is
not accepted. `CALL`-only raw traces are not supported.

When the compressed raw trace exceeds the configured threshold, conversion omits argument/return
values and subtree counts, interns identical complete call subgraphs, and records adjacent repeated
child sequences once with a repetition factor. UML renders those records as PlantUML loops and uses
an unnumbered omitted-call marker because exact omitted subtree counts are unavailable by design.
