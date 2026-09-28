package fltrace;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.atomic.AtomicLong;

public final class TraceRuntime {
    private static final int PROTOCOL_VERSION = 5;
    private static final boolean CAPTURE_VALUES = booleanProperty(
            "fltrace.capture.values", true);
    private static final int VALUE_STRING_EDGE_CHARS = positiveIntProperty(
            "fltrace.value.string.edge.chars", 10);
    private static final int VALUE_CONTAINER_EDGE_ITEMS = positiveIntProperty(
            "fltrace.value.container.edge.items", 2);
    private static final int VALUE_NESTED_CONTAINER_EDGE_ITEMS = positiveIntProperty(
            "fltrace.value.nested.container.edge.items", 1);
    private static final int VALUE_MAX_DEPTH = nonNegativeIntProperty(
            "fltrace.value.max.depth", 2);
    private static final int VALUE_MAX_ARGUMENTS = positiveIntProperty(
            "fltrace.value.max.arguments", 8);
    private static final AtomicLong NEXT_INVOCATION_ID = new AtomicLong(1);
    private static final AtomicLong NEXT_SEQUENCE = new AtomicLong(1);
    private static final ConcurrentMap<Long, Frame> OPEN_FRAMES =
            new ConcurrentHashMap<Long, Frame>();
    private static final ThreadLocal<Deque<Frame>> STACK =
            new ThreadLocal<Deque<Frame>>() {
                @Override protected Deque<Frame> initialValue() {
                    return new ArrayDeque<Frame>();
                }
            };
    private static final ThreadLocal<Integer> TEST_LINE =
            new ThreadLocal<Integer>() {
                @Override protected Integer initialValue() {
                    return Integer.valueOf(0);
                }
            };
    private static final List<AssertionRange> ASSERTION_RANGES = assertionRanges();
    private static final ThreadLocal<ActiveAssertion> ACTIVE_ASSERTION =
            new ThreadLocal<ActiveAssertion>();
    private static FileOutputStream OUT = null;

    private static final class Frame {
        final long invocationId;
        final String className;
        final String methodName;
        final String descriptor;
        final long enterNs;
        boolean emitted;

        Frame(long invocationId, String className, String methodName,
              String descriptor, long enterNs) {
            this.invocationId = invocationId;
            this.className = className;
            this.methodName = methodName;
            this.descriptor = descriptor;
            this.enterNs = enterNs;
            this.emitted = false;
        }
    }

    private static final class AssertionRange {
        final String id;
        final int startLine;
        final int endLine;

        AssertionRange(String id, int startLine, int endLine) {
            this.id = id;
            this.startLine = startLine;
            this.endLine = endLine;
        }

        boolean contains(int line) {
            return line >= startLine && line <= endLine;
        }
    }

    private static final class ActiveAssertion {
        final AssertionRange range;

        ActiveAssertion(AssertionRange range) {
            this.range = range;
        }
    }

    static {
        try {
            String path = System.getProperty("fltrace.raw.file");
            if (path != null && path.length() > 0) {
                File file = new File(path);
                File parent = file.getParentFile();
                if (parent != null) parent.mkdirs();
                OUT = new FileOutputStream(file, true);
                Runtime.getRuntime().addShutdownHook(new Thread(new Runnable() {
                    @Override public void run() {
                        synchronized (TraceRuntime.class) {
                            try {
                                if (OUT != null) OUT.close();
                            } catch (Throwable ignored) {
                            }
                        }
                    }
                }, "fltrace-shutdown"));
            }
        } catch (Throwable error) {
            error.printStackTrace();
        }
    }

    private TraceRuntime() {}

    public static void testLine(String className, String methodName, int line) {
        try {
            String expectedClass = System.getProperty("fltrace.test.class", "");
            String expectedMethod = System.getProperty("fltrace.test.method", "");
            if (expectedClass.equals(className) && expectedMethod.equals(methodName)) {
                AssertionRange next = assertionAt(line);
                ActiveAssertion active = ACTIVE_ASSERTION.get();
                boolean repeatedStart = active != null
                        && next != null
                        && active.range.id.equals(next.id)
                        && line == active.range.startLine
                        && TEST_LINE.get().intValue() == line;
                if (active != null && (next == null
                        || !active.range.id.equals(next.id)
                        || repeatedStart)) {
                    assertionOutcome("ASSERT_PASS", active.range, null);
                    ACTIVE_ASSERTION.remove();
                    active = null;
                }
                if (next != null && active == null) {
                    assertionStart(next);
                    ACTIVE_ASSERTION.set(new ActiveAssertion(next));
                }
                TEST_LINE.set(Integer.valueOf(line));
            }
        } catch (Throwable ignored) {
        }
    }

    public static void enter(String className, String methodName, String descriptor,
                             Object[] arguments, Class<?>[] declaredTypes) {
        Deque<Frame> stack = null;
        Frame frame = null;
        try {
            if (OUT == null) return;
            stack = STACK.get();
            long parentId = 0L;
            for (Frame parent : stack) {
                if (parent.emitted && OPEN_FRAMES.containsKey(parent.invocationId)) {
                    parentId = parent.invocationId;
                    break;
                }
            }
            long invocationId = NEXT_INVOCATION_ID.getAndIncrement();
            long timestamp = System.nanoTime();
            frame = new Frame(invocationId, className, methodName, descriptor, timestamp);
            String captured = CAPTURE_VALUES
                    ? ",\"arguments\":" + summarizeArguments(arguments, declaredTypes)
                    : "";
            String record = "{\"type\":\"ENTER\"" + common(timestamp) +
                    ",\"invocation_id\":" + invocationId +
                    ",\"parent_id\":" + parentId +
                    ",\"class\":\"" + esc(className) + "\"" +
                    ",\"method\":\"" + esc(methodName) + "\"" +
                    ",\"descriptor\":\"" + esc(descriptor) + "\"" +
                    ",\"origin_test_line\":" + TEST_LINE.get().intValue() +
                    captured + "}";
            stack.push(frame);
            frame.emitted = emit(record);
            if (!frame.emitted) {
                if (!stack.isEmpty() && stack.peek() == frame) stack.pop();
                return;
            }
            OPEN_FRAMES.put(Long.valueOf(frame.invocationId), frame);
        } catch (Throwable ignored) {
            try {
                if (stack != null && frame != null && !frame.emitted) {
                    stack.removeFirstOccurrence(frame);
                }
            } catch (Throwable cleanupIgnored) {
            }
        }
    }

    public static void exitNormal(String className, String methodName, String descriptor,
                                  Object value, Class<?> declaredType) {
        exit(className, methodName, descriptor, "RETURN", null, value, declaredType);
    }

    public static void exitThrow(String className, String methodName, String descriptor,
                                 Throwable error) {
        exit(className, methodName, descriptor, "THROW", error, null, null);
    }

    private static void exit(String className, String methodName, String descriptor,
                             String type, Throwable error, Object value, Class<?> declaredType) {
        try {
            if (OUT == null) return;
            Deque<Frame> stack = STACK.get();
            Frame frame = null;
            while (!stack.isEmpty()) {
                Frame candidate = stack.pop();
                if (candidate.className.equals(className)
                        && candidate.methodName.equals(methodName)
                        && candidate.descriptor.equals(descriptor)) {
                    frame = candidate;
                    break;
                }
                emitTraceGap(candidate, "missing exit callback before "
                        + className + "." + methodName + descriptor);
            }
            if (frame == null) return;
            if (!frame.emitted) return;
            synchronized (frame) {
                if (OPEN_FRAMES.get(Long.valueOf(frame.invocationId)) != frame) return;
                long timestamp = System.nanoTime();
                if (isTargetTest(frame)) {
                    ActiveAssertion active = ACTIVE_ASSERTION.get();
                    if (active != null) {
                        assertionOutcome(
                                "THROW".equals(type) ? "ASSERT_FAIL" : "ASSERT_PASS",
                                active.range,
                                error
                        );
                        ACTIVE_ASSERTION.remove();
                    }
                }
                String extra = "";
                if (error != null) {
                    extra = ",\"exception_class\":\"" + esc(error.getClass().getName()) + "\"" +
                            ",\"message\":\"" + esc(error.getMessage()) + "\"";
                }
                if (CAPTURE_VALUES && "RETURN".equals(type)) {
                    extra += ",\"return_value\":" + summarizeReturn(value, declaredType);
                }
                if (emit("{\"type\":\"" + type + "\"" + common(timestamp) +
                        ",\"invocation_id\":" + frame.invocationId +
                        ",\"duration_ns\":" + Math.max(0L, timestamp - frame.enterNs) +
                        ",\"origin_test_line\":" + TEST_LINE.get().intValue() +
                        extra + "}")) {
                    OPEN_FRAMES.remove(Long.valueOf(frame.invocationId), frame);
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private static void emitTraceGap(Frame frame, String reason) {
        if (frame == null || !frame.emitted) return;
        synchronized (frame) {
            if (OPEN_FRAMES.get(Long.valueOf(frame.invocationId)) != frame) return;
            if (emitTraceGapRecord(frame, reason)) {
                OPEN_FRAMES.remove(Long.valueOf(frame.invocationId), frame);
            }
        }
    }

    private static void closeTraceGaps(String reason) {
        for (Map.Entry<Long, Frame> item : OPEN_FRAMES.entrySet()) {
            Frame frame = item.getValue();
            synchronized (frame) {
                if (OPEN_FRAMES.get(item.getKey()) == frame
                        && emitTraceGapRecord(frame, reason)) {
                    OPEN_FRAMES.remove(item.getKey(), frame);
                }
            }
        }
        STACK.get().clear();
    }

    private static boolean emitTraceGapRecord(Frame frame, String reason) {
        long timestamp = System.nanoTime();
        return emit("{\"type\":\"THROW\"" + common(timestamp) +
                ",\"invocation_id\":" + frame.invocationId +
                ",\"duration_ns\":" + Math.max(0L, timestamp - frame.enterNs) +
                ",\"origin_test_line\":" + TEST_LINE.get().intValue() +
                ",\"exception_class\":\"fltrace.TraceGap\"" +
                ",\"message\":\"" + esc(reason) + "\"}");
    }

    public static void testStart(String className, String methodName) {
        try {
            TEST_LINE.set(Integer.valueOf(0));
            ACTIVE_ASSERTION.remove();
            long timestamp = System.nanoTime();
            emit("{\"type\":\"TEST_START\"" + common(timestamp) +
                    ",\"class\":\"" + esc(className) + "\"" +
                    ",\"method\":\"" + esc(methodName) + "\"" +
                    ",\"agent_protocol_version\":" + PROTOCOL_VERSION +
                    ",\"value_capture\":{" +
                    "\"capture_values\":" + CAPTURE_VALUES +
                    ",\"value_string_edge_chars\":" + VALUE_STRING_EDGE_CHARS +
                    ",\"value_container_edge_items\":" + VALUE_CONTAINER_EDGE_ITEMS +
                    ",\"value_nested_container_edge_items\":" +
                    VALUE_NESTED_CONTAINER_EDGE_ITEMS +
                    ",\"value_max_depth\":" + VALUE_MAX_DEPTH +
                    ",\"value_max_arguments\":" + VALUE_MAX_ARGUMENTS + "}}");
        } catch (Throwable ignored) {
        }
    }

    public static void testFailure(Throwable error) {
        try {
            closeTraceGaps("missing exit callback before TEST_FAILURE");
            long timestamp = System.nanoTime();
            String exceptionClass = error == null ? "" : error.getClass().getName();
            String message = error == null ? "" : error.getMessage();
            StackTraceElement frame = testFrame(error);
            String sourceFile = frame == null ? "" : frame.getFileName();
            int sourceLine = frame == null ? TEST_LINE.get().intValue() : frame.getLineNumber();
            emit("{\"type\":\"TEST_FAILURE\"" + common(timestamp) +
                    ",\"exception_class\":\"" + esc(exceptionClass) + "\"" +
                    ",\"message\":\"" + esc(message) + "\"" +
                    ",\"source_file\":\"" + esc(sourceFile) + "\"" +
                    ",\"source_line\":" + sourceLine + "}");
        } catch (Throwable ignored) {
        }
    }

    private static StackTraceElement testFrame(Throwable error) {
        if (error == null) return null;
        String testClass = System.getProperty("fltrace.test.class", "");
        String testMethod = System.getProperty("fltrace.test.method", "");
        for (StackTraceElement frame : error.getStackTrace()) {
            if (testClass.equals(frame.getClassName()) && testMethod.equals(frame.getMethodName())) {
                return frame;
            }
        }
        return null;
    }

    public static void testEnd(boolean successful, int failureCount) {
        try {
            closeTraceGaps("missing exit callback before TEST_END");
            ACTIVE_ASSERTION.remove();
            long timestamp = System.nanoTime();
            emit("{\"type\":\"TEST_END\"" + common(timestamp) +
                    ",\"successful\":" + successful +
                    ",\"failure_count\":" + failureCount + "}");
        } catch (Throwable ignored) {
        }
    }

    private static String common(long timestamp) {
        Thread thread = Thread.currentThread();
        return ",\"seq\":" + NEXT_SEQUENCE.getAndIncrement() +
                ",\"ts_ns\":" + timestamp +
                ",\"thread_id\":" + thread.getId() +
                ",\"thread_name\":\"" + esc(thread.getName()) + "\"";
    }

    private static boolean isTargetTest(Frame frame) {
        return System.getProperty("fltrace.test.class", "").equals(frame.className)
                && System.getProperty("fltrace.test.method", "").equals(frame.methodName);
    }

    private static AssertionRange assertionAt(int line) {
        for (AssertionRange range : ASSERTION_RANGES) {
            if (range.contains(line)) return range;
        }
        return null;
    }

    private static void assertionStart(AssertionRange range) {
        long timestamp = System.nanoTime();
        emit("{\"type\":\"ASSERT_START\"" + common(timestamp) +
                ",\"assertion_id\":\"" + esc(range.id) + "\"" +
                ",\"source_start_line\":" + range.startLine +
                ",\"source_end_line\":" + range.endLine + "}");
    }

    private static void assertionOutcome(
            String type, AssertionRange range, Throwable error) {
        long timestamp = System.nanoTime();
        String extra = "";
        if (error != null) {
            extra = ",\"exception_class\":\"" + esc(error.getClass().getName()) + "\"" +
                    ",\"message\":\"" + esc(error.getMessage()) + "\"";
        }
        emit("{\"type\":\"" + type + "\"" + common(timestamp) +
                ",\"assertion_id\":\"" + esc(range.id) + "\"" +
                ",\"source_start_line\":" + range.startLine +
                ",\"source_end_line\":" + range.endLine + extra + "}");
    }

    private static List<AssertionRange> assertionRanges() {
        List<AssertionRange> result = new ArrayList<AssertionRange>();
        String configured = System.getProperty("fltrace.assert.ranges", "");
        if (configured.length() == 0) return result;
        for (String raw : configured.split(";")) {
            try {
                String[] identity = raw.split(":", 2);
                String[] lines = identity[1].split("-", 2);
                int startLine = Integer.parseInt(lines[0]);
                int endLine = Integer.parseInt(lines[1]);
                if (identity[0].length() > 0 && startLine > 0 && endLine >= startLine) {
                    result.add(new AssertionRange(identity[0], startLine, endLine));
                }
            } catch (RuntimeException ignored) {
            }
        }
        return result;
    }

    private static synchronized boolean emit(String json) {
        if (OUT == null) return false;
        try {
            byte[] record = (json + "\n").getBytes(StandardCharsets.UTF_8);
            OUT.write(record);
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static String summarizeArguments(Object[] values, Class<?>[] declaredTypes) {
        Object[] safeValues = values == null ? new Object[0] : values;
        Class<?>[] safeTypes = declaredTypes == null ? new Class<?>[0] : declaredTypes;
        int count = safeValues.length;
        int limit = Math.min(count, VALUE_MAX_ARGUMENTS);
        StringBuilder items = new StringBuilder();
        int emitted = 0;
        boolean truncated = count > limit || safeTypes.length != count;
        for (int index = 0; index < limit; index++) {
            Class<?> declared = index < safeTypes.length ? safeTypes[index] : null;
            ValueSummary summary = safeSummary(safeValues[index], declared);
            String item = summary.json(index);
            if (emitted > 0) items.append(',');
            items.append(item);
            emitted++;
            truncated = truncated || summary.truncated;
        }
        int omitted = count - emitted;
        return "{\"count\":" + count + ",\"items\":[" + items +
                "],\"omitted_count\":" + omitted + ",\"truncated\":" +
                (truncated || omitted > 0) + "}";
    }

    private static String summarizeReturn(Object value, Class<?> declaredType) {
        if (declaredType == null || declaredType == Void.TYPE) {
            return new ValueSummary(typeName(declaredType), "", "void", "", false)
                    .json(null);
        }
        return safeSummary(value, declaredType).json(null);
    }

    private static ValueSummary safeSummary(Object value, Class<?> declaredType) {
        try {
            IdentityHashMap<Object, Boolean> seen = new IdentityHashMap<Object, Boolean>();
            TextValue rendered = renderValue(value, 0, seen);
            return new ValueSummary(
                    typeName(declaredType), value == null ? "" : value.getClass().getName(),
                    rendered.kind, rendered.text, rendered.truncated);
        } catch (Throwable error) {
            String name = error == null ? "java.lang.Throwable" : error.getClass().getName();
            String text = "<capture-error:" + name + ">";
            return new ValueSummary(typeName(declaredType),
                    value == null ? "" : safeClassName(value), "error",
                    text, false);
        }
    }

    private static TextValue renderValue(Object value, int depth,
                                         IdentityHashMap<Object, Boolean> seen) {
        if (value == null) return new TextValue("null", "null", false);
        Class<?> type = value.getClass();
        if (value instanceof String) {
            return renderString((String) value);
        }
        if (value instanceof Character) {
            return new TextValue("char", "'" + escapeText(String.valueOf(value)) + "'", false);
        }
        if (value instanceof Boolean) {
            return new TextValue("boolean", ((Boolean) value).booleanValue() ? "true" : "false", false);
        }
        if (value instanceof Byte || value instanceof Short || value instanceof Integer
                || value instanceof Long || value instanceof Float || value instanceof Double) {
            return new TextValue("number", String.valueOf(value), false);
        }
        if (value instanceof Enum<?>) {
            return new TextValue("enum", type.getName() + "." + ((Enum<?>) value).name(), false);
        }
        if (type.isArray()) {
            return renderArray(value, depth, seen);
        }
        if (isWhitelistedCollection(type)) {
            return renderCollection((Collection<?>) value, depth, seen);
        }
        if (isWhitelistedMap(type)) {
            return renderMap((Map<?, ?>) value, depth, seen);
        }
        return new TextValue("object", "<" + type.getName() + ">", false);
    }

    private static TextValue renderString(String value) {
        int edge = VALUE_STRING_EDGE_CHARS;
        if (value.length() <= edge * 2) {
            return new TextValue("string", quote(value), false);
        }
        String text = "\"" + escapeText(value.substring(0, edge)) + "…"
                + escapeText(value.substring(value.length() - edge)) + "\"";
        return new TextValue("string", text, true);
    }

    private static boolean isContainer(Object value) {
        if (value == null) return false;
        Class<?> type = value.getClass();
        return type.isArray() || isWhitelistedCollection(type)
                || isWhitelistedMap(type);
    }

    private static int containerEdgeItems(boolean containsContainer, int depth) {
        return depth > 0 || containsContainer
                ? VALUE_NESTED_CONTAINER_EDGE_ITEMS
                : VALUE_CONTAINER_EDGE_ITEMS;
    }

    private static TextValue renderArray(Object value, int depth,
                                         IdentityHashMap<Object, Boolean> seen) {
        if (seen.containsKey(value)) return new TextValue("cycle", "<cycle>", false);
        if (depth >= VALUE_MAX_DEPTH) return new TextValue("array", "<max-depth>", true);
        seen.put(value, Boolean.TRUE);
        try {
            int length = java.lang.reflect.Array.getLength(value);
            boolean containsContainer = false;
            for (int index = 0; index < length && !containsContainer; index++) {
                containsContainer = isContainer(java.lang.reflect.Array.get(value, index));
            }
            int edge = containerEdgeItems(containsContainer, depth);
            StringBuilder out = new StringBuilder("[");
            boolean truncated = length > edge * 2;
            int firstEnd = truncated ? edge : length;
            for (int index = 0; index < firstEnd; index++) {
                if (index > 0) out.append(", ");
                TextValue child = renderValue(java.lang.reflect.Array.get(value, index),
                        depth + 1, seen);
                out.append(child.text);
                truncated = truncated || child.truncated;
            }
            if (length > edge * 2) {
                out.append(", …");
                for (int index = length - edge; index < length; index++) {
                    out.append(", ");
                    TextValue child = renderValue(java.lang.reflect.Array.get(value, index),
                            depth + 1, seen);
                    out.append(child.text);
                    truncated = truncated || child.truncated;
                }
            }
            out.append(']');
            return new TextValue("array", out.toString(), truncated);
        } finally {
            seen.remove(value);
        }
    }

    private static TextValue renderCollection(Collection<?> value, int depth,
                                              IdentityHashMap<Object, Boolean> seen) {
        if (seen.containsKey(value)) return new TextValue("cycle", "<cycle>", false);
        if (depth >= VALUE_MAX_DEPTH) return new TextValue("collection", "<max-depth>", true);
        seen.put(value, Boolean.TRUE);
        try {
            List<Object> first = new ArrayList<Object>();
            List<Object> tail = new ArrayList<Object>();
            int bufferEdge = Math.max(
                    VALUE_CONTAINER_EDGE_ITEMS, VALUE_NESTED_CONTAINER_EDGE_ITEMS);
            Iterator<?> iterator = value.iterator();
            int count = 0;
            boolean containsContainer = false;
            while (iterator.hasNext()) {
                Object item = iterator.next();
                containsContainer = containsContainer || isContainer(item);
                if (first.size() < bufferEdge) first.add(item);
                if (tail.size() == bufferEdge) tail.remove(0);
                tail.add(item);
                count++;
            }
            int edge = containerEdgeItems(containsContainer, depth);
            boolean truncated = count > edge * 2;
            StringBuilder out = new StringBuilder("[");
            int emitted = 0;
            int firstCount = truncated ? edge : Math.min(count, first.size());
            for (int index = 0; index < firstCount; index++) {
                if (emitted++ > 0) out.append(", ");
                TextValue child = renderValue(first.get(index), depth + 1, seen);
                out.append(child.text);
                truncated = truncated || child.truncated;
            }
            if (count > edge * 2) {
                out.append(", …");
                Object[] tailValues = tail.toArray();
                for (int index = tailValues.length - edge; index < tailValues.length; index++) {
                    out.append(", ");
                    TextValue child = renderValue(tailValues[index], depth + 1, seen);
                    out.append(child.text);
                    truncated = truncated || child.truncated;
                }
            } else {
                Object[] tailValues = tail.toArray();
                for (int globalIndex = firstCount; globalIndex < count; globalIndex++) {
                    int index = globalIndex - (count - tailValues.length);
                    if (emitted++ > 0) out.append(", ");
                    TextValue child = renderValue(tailValues[index], depth + 1, seen);
                    out.append(child.text);
                    truncated = truncated || child.truncated;
                }
            }
            out.append(']');
            return new TextValue("collection", out.toString(), truncated);
        } finally {
            seen.remove(value);
        }
    }

    private static TextValue renderMap(Map<?, ?> value, int depth,
                                       IdentityHashMap<Object, Boolean> seen) {
        if (seen.containsKey(value)) return new TextValue("cycle", "<cycle>", false);
        if (depth >= VALUE_MAX_DEPTH) return new TextValue("map", "<max-depth>", true);
        seen.put(value, Boolean.TRUE);
        try {
            List<Map.Entry<?, ?>> first = new ArrayList<Map.Entry<?, ?>>();
            List<Map.Entry<?, ?>> tail = new ArrayList<Map.Entry<?, ?>>();
            int bufferEdge = Math.max(
                    VALUE_CONTAINER_EDGE_ITEMS, VALUE_NESTED_CONTAINER_EDGE_ITEMS);
            Iterator<? extends Map.Entry<?, ?>> iterator = value.entrySet().iterator();
            int count = 0;
            boolean containsContainer = false;
            while (iterator.hasNext()) {
                Map.Entry<?, ?> entry = iterator.next();
                containsContainer = containsContainer || isContainer(entry.getKey())
                        || isContainer(entry.getValue());
                if (first.size() < bufferEdge) first.add(entry);
                if (tail.size() == bufferEdge) tail.remove(0);
                tail.add(entry);
                count++;
            }
            int edge = containerEdgeItems(containsContainer, depth);
            boolean truncated = count > edge * 2;
            StringBuilder out = new StringBuilder("{");
            int emitted = 0;
            int firstCount = truncated ? edge : Math.min(count, first.size());
            for (int index = 0; index < firstCount; index++) {
                if (emitted++ > 0) out.append(", ");
                Map.Entry<?, ?> entry = first.get(index);
                TextValue key = renderValue(entry.getKey(), depth + 1, seen);
                TextValue item = renderValue(entry.getValue(), depth + 1, seen);
                out.append(key.text).append('=').append(item.text);
                truncated = truncated || key.truncated || item.truncated;
            }
            if (count > edge * 2) {
                out.append(", …");
                Object[] tailValues = tail.toArray();
                for (int index = tailValues.length - edge; index < tailValues.length; index++) {
                    out.append(", ");
                    Map.Entry<?, ?> entry = (Map.Entry<?, ?>) tailValues[index];
                    TextValue key = renderValue(entry.getKey(), depth + 1, seen);
                    TextValue item = renderValue(entry.getValue(), depth + 1, seen);
                    out.append(key.text).append('=').append(item.text);
                    truncated = truncated || key.truncated || item.truncated;
                }
            } else {
                Object[] tailValues = tail.toArray();
                for (int globalIndex = firstCount; globalIndex < count; globalIndex++) {
                    int index = globalIndex - (count - tailValues.length);
                    if (emitted++ > 0) out.append(", ");
                    Map.Entry<?, ?> entry = (Map.Entry<?, ?>) tailValues[index];
                    TextValue key = renderValue(entry.getKey(), depth + 1, seen);
                    TextValue item = renderValue(entry.getValue(), depth + 1, seen);
                    out.append(key.text).append('=').append(item.text);
                    truncated = truncated || key.truncated || item.truncated;
                }
            }
            out.append('}');
            return new TextValue("map", out.toString(), truncated);
        } finally {
            seen.remove(value);
        }
    }

    private static boolean isWhitelistedCollection(Class<?> type) {
        String name = type.getName();
        return name.equals("java.util.ArrayList") || name.equals("java.util.LinkedList")
                || name.equals("java.util.HashSet") || name.equals("java.util.LinkedHashSet")
                || name.equals("java.util.TreeSet") || name.equals("java.util.ArrayDeque")
                || name.equals("java.util.Arrays$ArrayList")
                || name.equals("java.util.Collections$EmptyList")
                || name.equals("java.util.Collections$EmptySet")
                || name.equals("java.util.Collections$SingletonList")
                || name.equals("java.util.Collections$SingletonSet")
                || name.equals("java.util.Collections$UnmodifiableCollection")
                || name.equals("java.util.Collections$UnmodifiableList")
                || name.equals("java.util.Collections$UnmodifiableRandomAccessList")
                || name.equals("java.util.Collections$UnmodifiableSet")
                || name.equals("java.util.Collections$UnmodifiableSortedSet")
                || name.equals("java.util.Collections$UnmodifiableNavigableSet");
    }

    private static boolean isWhitelistedMap(Class<?> type) {
        String name = type.getName();
        return name.equals("java.util.HashMap") || name.equals("java.util.LinkedHashMap")
                || name.equals("java.util.TreeMap") || name.equals("java.util.Hashtable")
                || name.equals("java.util.Collections$EmptyMap")
                || name.equals("java.util.Collections$SingletonMap")
                || name.equals("java.util.Collections$UnmodifiableMap")
                || name.equals("java.util.Collections$UnmodifiableSortedMap")
                || name.equals("java.util.Collections$UnmodifiableNavigableMap");
    }

    private static String typeName(Class<?> type) {
        return type == null ? "" : type.getName();
    }

    private static String safeClassName(Object value) {
        try {
            return value.getClass().getName();
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static String quote(String value) {
        return "\"" + escapeText(value) + "\"";
    }

    private static String escapeText(String value) {
        StringBuilder out = new StringBuilder();
        for (int index = 0; index < value.length(); index++) {
            char current = value.charAt(index);
            if (current == '\\') out.append("\\\\");
            else if (current == '"') out.append("\\\"");
            else if (current == '\n') out.append("\\n");
            else if (current == '\r') out.append("\\r");
            else if (current == '\t') out.append("\\t");
            else if (current < 0x20) {
                String hex = Integer.toHexString(current);
                out.append("\\u");
                for (int pad = hex.length(); pad < 4; pad++) out.append('0');
                out.append(hex);
            } else out.append(current);
        }
        return out.toString();
    }

    private static boolean booleanProperty(String name, boolean fallback) {
        String value = System.getProperty(name);
        return value == null ? fallback : Boolean.parseBoolean(value);
    }

    private static int positiveIntProperty(String name, int fallback) {
        try {
            int value = Integer.parseInt(System.getProperty(name, String.valueOf(fallback)));
            return value > 0 ? value : fallback;
        } catch (RuntimeException ignored) {
            return fallback;
        }
    }

    private static int nonNegativeIntProperty(String name, int fallback) {
        try {
            int value = Integer.parseInt(System.getProperty(name, String.valueOf(fallback)));
            return value >= 0 ? value : fallback;
        } catch (RuntimeException ignored) {
            return fallback;
        }
    }

    private static final class TextValue {
        final String kind;
        final String text;
        final boolean truncated;

        TextValue(String kind, String text, boolean truncated) {
            this.kind = kind;
            this.text = text;
            this.truncated = truncated;
        }
    }

    private static final class ValueSummary {
        final String declaredType;
        final String runtimeType;
        final String kind;
        final String text;
        final boolean truncated;

        ValueSummary(String declaredType, String runtimeType, String kind,
                     String text, boolean truncated) {
            this.declaredType = declaredType;
            this.runtimeType = runtimeType;
            this.kind = kind;
            this.text = text;
            this.truncated = truncated;
        }

        String json(Integer index) {
            String prefix = index == null ? "{" : "{\"index\":" + index + ",";
            return prefix + "\"declared_type\":\"" + esc(declaredType) + "\"" +
                    ",\"runtime_type\":\"" + esc(runtimeType) + "\"" +
                    ",\"kind\":\"" + esc(kind) + "\"" +
                    ",\"text\":\"" + esc(text) + "\"" +
                    ",\"truncated\":" + truncated + "}";
        }
    }

    private static String esc(String value) {
        if (value == null) return "";
        return escapeText(value);
    }
}
