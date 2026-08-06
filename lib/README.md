# Java 工具依赖

- `fullchain-tracer.jar`：由 `fullchain_tracer/` 构建的 v3 字节码代理，记录 ENTER/RETURN/THROW、测试结果、调用父子关系、方法描述符、线程信息和触发调用的测试源码行；失败事件同时记录匹配的测试栈帧，但不记录对象 ID。`<clinit>` 仅保留在 raw 事件中，随后连同完整调用子树从方法级 trace 中删除。
- `plantuml.jar`：将 Stage 3 生成的 `.puml` 文件离线渲染为 PNG；具体版本见 `plantuml.version`。

这些 JAR 属于流水线工具；`defects4j/` 和实验 `workspace/` 内的项目依赖保持在各自目录。升级 PlantUML 时，替换稳定文件名 `plantuml.jar`、更新版本文件，并重新验证 Chart-1 冒烟流程。
