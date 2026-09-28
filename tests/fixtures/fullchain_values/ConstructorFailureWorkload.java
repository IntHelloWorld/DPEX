package valuefixture;

public final class ConstructorFailureWorkload {
    public ConstructorFailureWorkload() {
        this(true);
    }

    public ConstructorFailureWorkload(boolean fail) {
        if (fail) {
            throw new IllegalStateException("constructor failure");
        }
    }

    public static void scenario() {
        new ConstructorFailureWorkload();
    }
}
