package valuefixture;

import fltrace.TraceRuntime;

public final class ValueDriver {
    public static void main(String[] args) {
        TraceRuntime.testStart("valuefixture.ValueWorkload", "scenario");
        int result = ValueWorkload.scenario();
        TraceRuntime.testEnd(true, 0);
        System.out.println("RESULT=" + result);
    }
}
