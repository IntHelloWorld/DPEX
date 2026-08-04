package fltrace;

import javassist.ByteArrayClassPath;
import javassist.ClassPool;
import javassist.CtBehavior;
import javassist.CtClass;
import javassist.CtConstructor;
import javassist.LoaderClassPath;
import javassist.Modifier;

import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.security.ProtectionDomain;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class Agent {
    private static final Set<String> EXACT = new HashSet<String>();
    private static final List<String> PREFIX = new ArrayList<String>();

    private Agent() {}

    public static void premain(String agentArgs, Instrumentation instrumentation) {
        parseArgs(agentArgs);
        instrumentation.addTransformer(new Transformer(), true);
    }

    private static void parseArgs(String args) {
        if (args == null) return;
        for (String raw : args.split(",")) {
            String value = raw.trim();
            if (value.length() == 0) continue;
            if (value.startsWith("class:")) EXACT.add(value.substring("class:".length()));
            else PREFIX.add(value);
        }
    }

    private static boolean shouldInstrument(String className) {
        if (className == null || className.startsWith("fltrace.")) return false;
        if (EXACT.contains(className)) return true;
        for (String prefix : PREFIX) {
            if (className.startsWith(prefix)) return true;
        }
        return false;
    }

    private static String javaLiteral(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    public static final class Transformer implements ClassFileTransformer {
        @Override
        public byte[] transform(ClassLoader loader, String className, Class<?> classBeingRedefined,
                                ProtectionDomain protectionDomain, byte[] classfileBuffer) {
            if (className == null) return null;
            String dottedName = className.replace('/', '.');
            if (!shouldInstrument(dottedName)) return null;

            CtClass transformedClass = null;
            try {
                ClassPool pool = new ClassPool(true);
                if (loader != null) pool.appendClassPath(new LoaderClassPath(loader));
                pool.insertClassPath(new ByteArrayClassPath(dottedName, classfileBuffer));
                transformedClass = pool.get(dottedName);
                if (transformedClass.isInterface() || transformedClass.isAnnotation()) return null;

                CtClass throwable = pool.get("java.lang.Throwable");
                for (CtBehavior behavior : transformedClass.getDeclaredBehaviors()) {
                    instrumentBehavior(behavior, dottedName, throwable);
                }
                return transformedClass.toBytecode();
            } catch (Throwable error) {
                if (Boolean.getBoolean("fltrace.debug")) error.printStackTrace();
                return null;
            } finally {
                if (transformedClass != null) transformedClass.detach();
            }
        }

        private void instrumentBehavior(CtBehavior behavior, String className, CtClass throwable) {
            try {
                int modifiers = behavior.getModifiers();
                if (Modifier.isAbstract(modifiers) || Modifier.isNative(modifiers) || behavior.isEmpty()) return;

                String methodName;
                if (behavior instanceof CtConstructor) {
                    CtConstructor constructor = (CtConstructor) behavior;
                    methodName = constructor.isClassInitializer() ? "<clinit>" : "<init>";
                } else {
                    methodName = behavior.getName();
                }
                String descriptor = behavior.getSignature();
                String enter = "{ fltrace.TraceRuntime.enter(\"" + javaLiteral(className) +
                        "\",\"" + javaLiteral(methodName) + "\",\"" +
                        javaLiteral(descriptor) + "\"); }";
                behavior.insertBefore(enter);
                behavior.insertAfter("{ fltrace.TraceRuntime.exitNormal(); }", false);
                behavior.addCatch("{ fltrace.TraceRuntime.exitThrow($e); throw $e; }", throwable);
            } catch (Throwable error) {
                if (Boolean.getBoolean("fltrace.debug")) error.printStackTrace();
            }
        }
    }
}
