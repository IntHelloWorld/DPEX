package overflowfixture;

public final class StackOverflowWorkload {
    private StackOverflowWorkload() {}

    public static int recurse(int depth) {
        return recurse(depth + 1) + 1;
    }
}
