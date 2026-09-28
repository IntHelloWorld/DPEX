package valuefixture;

import junit.framework.TestCase;

public final class JUnit3SelectionTest extends TestCase {
    public void testSelected() {
        fail("selected failure");
    }

    public void testOther() {
        fail("unselected test ran");
    }
}
