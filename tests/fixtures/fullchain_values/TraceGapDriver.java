package valuefixture;

import fltrace.TraceRuntime;

public final class TraceGapDriver {
    private TraceGapDriver() {
    }

    public static void main(String[] args) throws Exception {
        Object[] noArguments = new Object[0];
        Class<?>[] noTypes = new Class<?>[0];
        TraceRuntime.testStart("valuefixture.TraceGapDriver", "scenario");
        TraceRuntime.enter("valuefixture.TraceGapDriver", "outer", "()V",
                noArguments, noTypes);
        TraceRuntime.enter("valuefixture.TraceGapDriver", "inner", "()V",
                noArguments, noTypes);
        TraceRuntime.exitNormal("valuefixture.TraceGapDriver", "outer", "()V",
                null, Void.TYPE);
        TraceRuntime.enter("valuefixture.TraceGapDriver", "leftOpen", "()V",
                noArguments, noTypes);
        Thread worker = new Thread(new Runnable() {
            @Override public void run() {
                TraceRuntime.enter("valuefixture.TraceGapDriver", "workerLeftOpen", "()V",
                        new Object[0], new Class<?>[0]);
            }
        }, "trace-gap-worker");
        worker.start();
        worker.join();
        TraceRuntime.testFailure(new AssertionError("expected failure"));
        TraceRuntime.testEnd(false, 1);
        System.out.println("TRACE_GAPS_RECORDED");
    }
}
