package overflowfixture;

import fltrace.TraceRuntime;

public final class StackOverflowDriver {
    private StackOverflowDriver() {}

    public static void main(String[] args) {
        TraceRuntime.testStart("overflowfixture.StackOverflowWorkload", "recurse");
        try {
            StackOverflowWorkload.recurse(0);
            throw new AssertionError("expected StackOverflowError");
        } catch (StackOverflowError expected) {
            StackTraceElement[] stack = expected.getStackTrace();
            if (stack.length > 0
                    && stack[0].getClassName().equals("fltrace.TraceRuntime")) {
                throw new AssertionError("tracing replaced the workload StackOverflowError");
            }
            TraceRuntime.testFailure(expected);
            TraceRuntime.testEnd(false, 1);
            System.out.println("STACK_OVERFLOW_RECORDED");
            if (stack.length > 0) {
                System.out.println("STACK_OVERFLOW_TOP=" + stack[0].getClassName()
                        + "#" + stack[0].getMethodName());
            }
        }
    }
}
