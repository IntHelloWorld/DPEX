package fltrace.runner;

import fltrace.TraceRuntime;
import junit.framework.TestCase;
import junit.framework.TestSuite;
import org.junit.runner.JUnitCore;
import org.junit.runner.Request;
import org.junit.runner.Result;
import org.junit.runner.notification.Failure;

public class SingleTestRunner {
    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: SingleTestRunner <testClass> <testMethod>");
            System.exit(2);
        }
        String clsName = args[0];
        String method = args[1];

        TraceRuntime.testStart(clsName, method);
        Class<?> cls = Class.forName(clsName);
        JUnitCore core = new JUnitCore();
        Result result;
        try {
            if (TestCase.class.isAssignableFrom(cls)) {
                result = core.run(TestSuite.createTest(cls, method));
            } else {
                result = core.run(Request.method(cls, method));
            }
        } catch (Throwable error) {
            TraceRuntime.testFailure(error);
            TraceRuntime.testEnd(false, 1);
            throw error;
        }

        for (Failure f : result.getFailures()) {
            TraceRuntime.testFailure(f.getException());
            System.out.println(f.toString());
            if (f.getException() != null) {
                f.getException().printStackTrace(System.out);
            }
        }

        System.out.println("Run count: " + result.getRunCount());
        System.out.println("Failure count: " + result.getFailureCount());
        TraceRuntime.testEnd(result.wasSuccessful(), result.getFailureCount());

        if (!result.wasSuccessful()) {
            System.exit(1);
        }
    }
}
