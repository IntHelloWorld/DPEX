package valuefixture;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class ValueWorkload {
    enum Shade { BLUE }

    static final class Evil {
        static int toStringCalls = 0;

        @Override public String toString() {
            toStringCalls++;
            throw new AssertionError("business toString must not run");
        }
    }

    static final class ThrowingList extends ArrayList<Object> {
        @Override public java.util.Iterator<Object> iterator() {
            throw new IllegalStateException("capture-only iterator failure");
        }
    }

    private final int seed;

    public ValueWorkload(int seed) {
        this.seed = seed;
    }

    static int scalar(int number, String text, Character character,
                      Boolean flag, Object nullable, Shade shade) {
        return number + text.length() + character.charValue() + (flag ? 1 : 0)
                + (nullable == null ? 1 : 0) + shade.ordinal();
    }

    static int array(int[] values) {
        return values.length;
    }

    static int containers(List<Object> cycle, Map<String, Object> map) {
        return cycle.size() + map.size();
    }

    static int business(Evil value) {
        return value == null ? 0 : 11;
    }

    static int captureFailure(Collection<Object> value) {
        return value == null ? 0 : 17;
    }

    static int mutate(List<String> values) {
        values.add("after-entry");
        return values.size();
    }

    static int repeat(int value) {
        return value + 1;
    }

    static int many(int a, int b, int c, int d, int e,
                    int f, int g, int h, int i) {
        return a + b + c + d + e + f + g + h + i;
    }

    static int boundedValues(String text, int[] array, List<Integer> list,
                             Map<Integer, Integer> map, Object[] nested) {
        return text.length() + array.length + list.size() + map.size()
                + nested.length;
    }

    static int argumentBudget(String a, String b, String c, String d,
                              String e, String f) {
        return a.length() + b.length() + c.length() + d.length()
                + e.length() + f.length();
    }

    static void noop() {
    }

    static Void boxedVoid() {
        return null;
    }

    static int explode() {
        throw new IllegalArgumentException("expected\u0004control");
    }

    public static int scenario() {
        ValueWorkload instance = new ValueWorkload(7);
        int result = instance.seed;
        result += scalar(3, "雪\\\"\n", Character.valueOf('x'), Boolean.TRUE,
                null, Shade.BLUE);
        result += array(new int[] {0, 1, 2, 3, 4, 5, 6, 7, 8, 9});
        List<Object> cycle = new ArrayList<Object>();
        cycle.add(cycle);
        Map<String, Object> map = new LinkedHashMap<String, Object>();
        map.put("key", Integer.valueOf(9));
        result += containers(cycle, map);
        result += business(new Evil());
        ThrowingList throwing = new ThrowingList();
        result += captureFailure(Collections.unmodifiableCollection(throwing));
        List<String> mutable = new ArrayList<String>();
        mutable.add("before-entry");
        result += mutate(mutable);
        result += repeat(1) + repeat(1) + repeat(2);
        result += many(1, 2, 3, 4, 5, 6, 7, 8, 9);
        String longText = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                + "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                + "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";
        int[] longArray = new int[] {0, 1, 2, 3, 4, 5, 6, 7, 8, 9};
        List<Integer> longList = new ArrayList<Integer>();
        Map<Integer, Integer> longMap = new LinkedHashMap<Integer, Integer>();
        for (int index = 0; index < 10; index++) {
            longList.add(Integer.valueOf(index));
            longMap.put(Integer.valueOf(index), Integer.valueOf(index + 10));
        }
        Object[] nested = new Object[] {
            new int[] {0, 1, 2, 3, 4},
            new int[] {5, 6, 7, 8, 9},
            new int[] {10, 11, 12, 13, 14}
        };
        result += boundedValues(longText, longArray, longList, longMap, nested);
        String budgetValue = longText.substring(0, 110);
        result += argumentBudget(
                budgetValue, budgetValue, budgetValue,
                budgetValue, budgetValue, budgetValue);
        noop();
        boxedVoid();
        try {
            explode();
        } catch (IllegalArgumentException expected) {
            result += 1;
        }
        if (Evil.toStringCalls != 0) {
            throw new AssertionError("business toString was called");
        }
        return result;
    }
}
