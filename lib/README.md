# Java 工具依赖

- `fullchain-tracer.jar`：由 `fullchain_tracer/` 构建的 v2 字节码代理，记录 ENTER/RETURN/THROW、测试结果、调用父子关系、方法描述符和线程信息；不记录对象 ID。
- `plantuml.jar`：将 Stage 3 生成的 `.puml` 文件离线渲染为 PNG；具体版本见 `plantuml.version`。

这些 JAR 属于流水线工具；`defects4j/` 和实验 `workspace/` 内的项目依赖保持在各自目录。升级 PlantUML 时，替换稳定文件名 `plantuml.jar`、更新版本文件，并重新验证 Chart-1 冒烟流程。
