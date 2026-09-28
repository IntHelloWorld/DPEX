package valuefixture;

import fltrace.TraceRuntime;

public final class ConstructorFailureDriver {
    private ConstructorFailureDriver() {}

    public static void main(String[] args) {
        TraceRuntime.testStart(
                "valuefixture.ConstructorFailureWorkload", "scenario");
        try {
            ConstructorFailureWorkload.scenario();
            TraceRuntime.testEnd(true, 0);
        } catch (Throwable error) {
            TraceRuntime.testFailure(error);
            TraceRuntime.testEnd(false, 1);
            System.out.println("CONSTRUCTOR_FAILURE_RECORDED");
        }
    }
}
