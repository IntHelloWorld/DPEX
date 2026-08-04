package fltrace;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.concurrent.atomic.AtomicLong;

public final class TraceRuntime {
    private static final AtomicLong NEXT_INVOCATION_ID = new AtomicLong(1);
    private static final AtomicLong NEXT_SEQUENCE = new AtomicLong(1);
    private static final ThreadLocal<Deque<Frame>> STACK =
            new ThreadLocal<Deque<Frame>>() {
                @Override protected Deque<Frame> initialValue() {
                    return new ArrayDeque<Frame>();
                }
            };
    private static PrintWriter OUT = null;

    private static final class Frame {
        final long invocationId;
        final String className;
        final String methodName;
        final String descriptor;
        final long enterNs;

        Frame(long invocationId, String className, String methodName,
              String descriptor, long enterNs) {
            this.invocationId = invocationId;
            this.className = className;
            this.methodName = methodName;
            this.descriptor = descriptor;
            this.enterNs = enterNs;
        }
    }

    static {
        try {
            String path = System.getProperty("fltrace.raw.file");
            if (path != null && path.length() > 0) {
                File file = new File(path);
                File parent = file.getParentFile();
                if (parent != null) parent.mkdirs();
                OUT = new PrintWriter(new OutputStreamWriter(
                        new FileOutputStream(file, true), "UTF-8"), true);
                Runtime.getRuntime().addShutdownHook(new Thread(new Runnable() {
                    @Override public void run() {
                        synchronized (TraceRuntime.class) {
                            if (OUT != null) OUT.close();
                        }
                    }
                }, "fltrace-shutdown"));
            }
        } catch (Throwable error) {
            error.printStackTrace();
        }
    }

    private TraceRuntime() {}

    public static void enter(String className, String methodName, String descriptor) {
        try {
            if (OUT == null) return;
            Deque<Frame> stack = STACK.get();
            long parentId = stack.isEmpty() ? 0L : stack.peek().invocationId;
            long invocationId = NEXT_INVOCATION_ID.getAndIncrement();
            long timestamp = System.nanoTime();
            Frame frame = new Frame(invocationId, className, methodName, descriptor, timestamp);
            stack.push(frame);
            emit("{\"type\":\"ENTER\"" + common(timestamp) +
                    ",\"invocation_id\":" + invocationId +
                    ",\"parent_id\":" + parentId +
                    ",\"class\":\"" + esc(className) + "\"" +
                    ",\"method\":\"" + esc(methodName) + "\"" +
                    ",\"descriptor\":\"" + esc(descriptor) + "\"}");
        } catch (Throwable ignored) {
        }
    }

    public static void exitNormal() {
        exit("RETURN", null);
    }

    public static void exitThrow(Throwable error) {
        exit("THROW", error);
    }

    private static void exit(String type, Throwable error) {
        try {
            if (OUT == null) return;
            Deque<Frame> stack = STACK.get();
            if (stack.isEmpty()) return;
            Frame frame = stack.pop();
            long timestamp = System.nanoTime();
            String extra = "";
            if (error != null) {
                extra = ",\"exception_class\":\"" + esc(error.getClass().getName()) + "\"" +
                        ",\"message\":\"" + esc(error.getMessage()) + "\"";
            }
            emit("{\"type\":\"" + type + "\"" + common(timestamp) +
                    ",\"invocation_id\":" + frame.invocationId +
                    ",\"duration_ns\":" + Math.max(0L, timestamp - frame.enterNs) +
                    extra + "}");
        } catch (Throwable ignored) {
        }
    }

    public static void testStart(String className, String methodName) {
        try {
            long timestamp = System.nanoTime();
            emit("{\"type\":\"TEST_START\"" + common(timestamp) +
                    ",\"class\":\"" + esc(className) + "\"" +
                    ",\"method\":\"" + esc(methodName) + "\"}");
        } catch (Throwable ignored) {
        }
    }

    public static void testFailure(Throwable error) {
        try {
            long timestamp = System.nanoTime();
            String exceptionClass = error == null ? "" : error.getClass().getName();
            String message = error == null ? "" : error.getMessage();
            emit("{\"type\":\"TEST_FAILURE\"" + common(timestamp) +
                    ",\"exception_class\":\"" + esc(exceptionClass) + "\"" +
                    ",\"message\":\"" + esc(message) + "\"}");
        } catch (Throwable ignored) {
        }
    }

    public static void testEnd(boolean successful, int failureCount) {
        try {
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

    private static synchronized void emit(String json) {
        if (OUT != null) OUT.println(json);
    }

    private static String esc(String value) {
        if (value == null) return "";
        return value.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t");
    }
}
