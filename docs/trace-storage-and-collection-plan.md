# 大型 Trace 采集、存储与 UML 查询改造方案

状态：设计稿，尚未实现。日期：2026-09-09。

目标：采用头部值采样、紧凑分块存储、可展开的重复结构压缩和磁盘索引，解决大型 trace 在采集、转换、suite 汇总及 refinement 初始化阶段的内存放大，同时保持调用身份、顺序、采样值和局部 UML 查询可审计。

本方案中的参数是建议起点，性能指标是验收目标，不是已测结果。本文只规划工程改造，不授权重跑历史实验或调用模型 API。

## 1. 主要决策

1. 数组、Collection、Map 改成只读取头部 N 个元素或条目；不再访问尾部，也不扫描整个容器来判断是否存在嵌套容器。
2. 顶层容器默认 N=2，嵌套容器默认 N=1，最大容器展开深度保持 2，最多采集 8 个参数。
3. 字符串仍保留前后各 10 个字符。这里将用户提出的“头部 N 个元素”应用于容器；字符串采用独立配置，不随本次容器策略改变。
4. Java 直接输出经过字典编码的独立 Zstd 事件块；Python 采集转换器逐块消费，不生成巨型未压缩 raw 文件。
5. 最终保存一份基础调用证据；拓扑、值摘要和控制事件分开分块存储。过滤及成功断言折叠用视图元数据表示。
6. SQLite 保存可重建的标量索引和统计；版本化 JSON manifest 与 JSON/JSONL 块是阶段交接及重建依据。
7. 保留精确的 T<n>-C<invocation_id> 身份。相同结构可以共享模板，逐次调用的位置、顺序和值差异仍可恢复。
8. 存储压缩默认不改变 UML 展示方式。图仍按 invocation 定位并展开局部视窗，准确标注 OMITTED_CALLS。
9. 从采集到渲染都禁止依赖全量事件、全量节点字典或全量 children 数组。缓存和批次按字节预算限制。
10. 旧产物只做显式存储迁移，不把旧头尾摘要伪装成新头部摘要，不在恢复过程中悄悄重采或运行模型。

## 2. 现状与改造范围

当前已经在 Java 端完成值摘要，而不是在绘图时才截断。但仍存在以下放大：

| 环节 | 当前问题 | 本方案 |
| --- | --- | --- |
| Java 输出 | 每条事件重复方法、类型及 JSON 字段名；未压缩 raw 持续增长 | 方法/类型字典、紧凑事件数组、独立压缩块 |
| 容器摘要 | 获取头尾和检测嵌套可能遍历整个容器 | 深度驱动的头部采样 |
| 事件读取 | load_events 逐行读取后放入完整列表并排序 | 流式读取，新协议有序输出，旧协议外部归并 |
| 建图 | Invocation、Call、完整 trace、投影和断言裁剪对象同时存在 | 逐次完成记录、磁盘关联、视图标记 |
| suite | 为汇总方法再次解压完整 execution | 合并每个 trigger 的方法摘要 |
| refine | 为所有选中 trigger 创建常驻完整拓扑 | 轻量 manifest/索引句柄、局部读取 |
| viewport | 获取完整 children 后才切片 | 存储层分页、序号范围查询、区间计数 |
| 校验 | 全量 JSON、ID 集合及父链映射 | 逐块校验、磁盘唯一性和拓扑验证 |

当前代码入口：

- [TraceRuntime.java](../fullchain_tracer/src/main/java/fltrace/TraceRuntime.java)：值摘要、事件序号、写入。
- [Agent.java](../fullchain_tracer/src/main/java/fltrace/Agent.java)：取参及方法插桩。
- [domain/trace.py](../mllmfl/domain/trace.py)：事件读取、建图、投影及校验。
- [assertion_folding.py](../mllmfl/domain/assertion_folding.py)：成功断言折叠。
- [refinement_trace.py](../mllmfl/domain/refinement_trace.py)：方法目录、归一化记录和内存拓扑。
- [stages/trace.py](../mllmfl/stages/trace.py)：trigger 采集、复用及 suite 汇总。
- [io.py](../mllmfl/infrastructure/io.py)：压缩 JSON 读写。
- [trace_suite.py](../mllmfl/domain/schemas/trace_suite.py)：suite 文件及引用校验。
- [graphs.py](../mllmfl/stages/refine/graphs.py)、[focus_viewport.py](../mllmfl/domain/focus_viewport.py)：定位与局部图查询。
- [sequence_diagram.py](../mllmfl/infrastructure/sequence_diagram.py)：值标签及 PlantUML。

## 3. 值采集契约

### 3.1 建议的新配置

~~~json
{
  "trace": {
    "capture_values": true,
    "value_container_sampling": "head",
    "value_string_edge_chars": 10,
    "value_container_head_items": 2,
    "value_nested_container_head_items": 1,
    "value_max_depth": 2,
    "value_max_arguments": 8
  }
}
~~~

新采集配置不再接受 value_container_edge_items 和 value_nested_container_edge_items。旧字段不能被静默解释为新语义；出现旧字段时报告迁移提示，旧产物读取器仍按原版本解释。

N 是总采样数量。旧配置“头 2 + 尾 2”改成“头 2”，不是自动改成“头 4”。上述 head 字段必须为正整数，depth 为非负整数，布尔值不能作为整数通过校验。第一版仅支持 head 模式，不新增无消费者的采样选项。

TEST_START、最终 manifest 和配置 fingerprint 必须保存实际生效的完整采样配置，Python 严格核对请求配置与 Java 生效配置。

### 3.2 头部采样规则

- 容器深度从 0 开始：参数或返回值本身的容器是 0，其元素中的容器是 1。
- depth=0 使用 value_container_head_items；depth>0 使用 value_nested_container_head_items。
- 当 depth >= value_max_depth 时，输出类型和 max-depth 标记，不创建迭代器、不访问元素。
- 不再根据“容器里是否存在任意嵌套容器”改变顶层采样数量。这个规则变化是避免全容器扫描的必要条件。
- 数组先取得长度，再只读取索引 0 到 min(N,length)-1。
- Collection 只执行至多 N 次 next；结束后允许一次 hasNext 来判断是否还有元素。
- Map 只获取头部 N 个 entry，每个 entry 采集 key/value 的摘要；N 计条目，不计 key/value 总数。
- 不调用 size 来计算一般 Collection/Map 的精确省略数，不调用 toArray，不排序，不维护尾部环形缓冲。
- 无序集合与 Map 按本次执行的迭代器顺序采样。排序会额外读取元素，也会改变观察语义。
- 循环引用按当前采样路径识别；只维护这个有界路径的 identity 集合，不创建跨所有调用的对象身份表。
- 保持当前受限类型白名单。普通业务对象只记录类型，不访问字段，不调用业务 toString。
- 摘要必须在 ENTER/RETURN 当时完成并固化；异步写入队列只接收不可变摘要，不能保留活的业务对象供稍后取值。

示例，默认参数：

~~~text
数组 [1,2,3,4,5]        → [1,2,…]，总数 5，省略 3
一般集合 [1,2,3,4,5]    → [1,2,…]，确认存在更多元素，省略总数未知
嵌套数组 [[1,2],[3,4]]  → [[1,…],[3,…]]
超过最大深度的容器      → <max-depth>
~~~

集合恰好只有 N 项时，hasNext=false，应标记该层完整，而不是机械添加省略号。子值截断与当前容器存在剩余元素是两种不同信息。

### 3.3 值摘要的机器语义

值层保存有界结构化摘要，渲染适配器再生成当前箭头标签需要的文本。建议逻辑字段如下；物理块内使用固定顺序数组和局部字典 ID。

| 字段 | 语义 |
| --- | --- |
| kind | number/string/char/boolean/enum/object/null/void/array/collection/map/cycle/error |
| type_ref | 完整运行时类型的字典引用，不能只存短类名 |
| payload | 标量文本、类型占位、采样子值引用或 Map 键值对引用 |
| sample_count | 当前容器实际采到的元素/条目数 |
| total_count | 能直接可靠获取时为整数，否则 null |
| has_more | 已确认存在未采样元素时为 true；无法判断时为 null |
| omitted_count | 能精确确定时为整数，否则 null，不能把未知写成 0 或 1 |
| truncation | head_limit/string_limit/max_depth/capture_error 等原因；保留嵌套层级 |

参数列表总数由方法 descriptor 确定，因此超过最多 8 个参数时，参数 omitted_count 可以精确计算；它与参数内部容器的省略数分开。

区分 null、void、未启用采集、采集失败、超出深度和未知数量。省略号是标签呈现，不承担全部机器语义。

原始字符串仍按现有边缘字符配置截断；本次不改变其字符计数单位。字符串策略及 Unicode 边界应在测试中锁定。

头部采样限制的是读取次数，不保证每次底层 iterator/hasNext 调用的耗时。受限白名单、采集失败标记和外部测试超时仍然需要保留。

### 3.4 取参及字段减量

- 调整插桩，使 capture_values=false 时不构造无用的完整 $args/$sig。
- 已知方法签名时，只为前 N 个待采样参数构造取值/装箱代码，声明类型和参数总数使用方法元数据。
- 方法表保存 class、method、descriptor；调用记录只引用局部 method ID。
- 参数声明类型从 descriptor 恢复，参数 index 由位置表达，void 可由 descriptor 与 outcome 表达。
- 运行时类型用类型表引用；普通对象的占位文本由类型生成。
- 第一轮编码改造保留时序及异常恢复所需的现有字段。完成消费者适配后，普通采集关闭逐调用纳秒时间及 duration，诊断模式可以记录它们。
- enter_seq、exit_seq、parent_id、thread_id、origin_test_line、异常和断言证据属于基础契约；不能因为当前图中未显示就删除。
- 大型异常文本、测试输出和错误堆栈使用可分片文本附件引用，避免一个 JSON 记录打破内存预算；不得静默截断后伪称完整。

诊断字段开关以 evidence_profile 写入 manifest，并参与逻辑证据 fingerprint。关闭时间测量与仅改变压缩级别是不同的变更，不能使用同一个指纹把两者混为等价产物。

## 4. 存储格式与目录

选择“紧凑 JSON/JSONL 记录 + 独立 Zstd 块 + 可重建 SQLite 索引”。第一版不引入自研二进制文件格式，也不把完整值重复保存在 SQLite 行中。

示意目录，全部运行文件位于指定 --root 内：

~~~text
artifacts/Closure/bug_145/
  trace_suite.json
  traces/T1/
    manifest.json
    methods.json
    dictionaries/part-000001.jsonl.zst
    topology/part-000001.jsonl.zst
    values/part-000001.jsonl.zst
    controls/part-000001.jsonl.zst
    views/default/part-000001.jsonl.zst
    templates/part-000001.jsonl.zst
    occurrences/part-000001.jsonl.zst
    attachments/part-000001.jsonl.zst
    index.sqlite
  triggers/trigger_1/ingest/
    capture.json
    events/part-000001.jsonl.zst
    checkpoint.json
workspace/.../trace-tmp/
logs/trace/Closure/bug_145/...
summaries/...
~~~

目录示例不要求每类数据都有文件；无模板、无附件时不生成空目录。临时索引、归并段和 SQLite 临时文件同样必须落在本次 root 的 workspace/artifacts 中，不能泄漏到未受运行预算管理的临时目录。

### 4.1 分块与字典

- 以未压缩字节数为切块依据，建议起点 4 MiB/块；常规记录不跨块。
- 超大附件显式分片。块和读取器必须检查声明的解压大小，不能因损坏块无限分配。
- 每个块第一行是带 schema、schema_version、记录布局和字典依赖的 JSON header，后续为紧凑数组。
- 块结束记录包含记录数、序号范围及校验信息；压缩流必须正常结束才能提交该块。
- 不逐条单独压缩小记录；一组记录共享压缩上下文。
- 方法字典通常较小，可缓存；类型和值字典必须有字节预算。值去重先采用块内字典，避免所有唯一值形成无界全局 hash map。
- 模板/字典引用必须显式记录依赖，不允许依靠解码前面全部历史块才能恢复当前块。
- 块 ID 与行位置可准确定位记录；调用 ID 不要求连续，序号缺口不能凭空补齐。
- 4 MiB、压缩级别 1 等都是实验起点，后续比较 1/4/16 MiB 与级别 1/3 的大小、吞吐和局部读放大。

独立 frame 可以分别解压，适合用块索引实现局部读取；第一版采用独立文件，不必先实现单文件 seek table。[Zstd 格式说明](https://github.com/facebook/zstd/blob/dev/contrib/seekable_format/zstd_seekable_compression_format.md)

### 4.2 基础调用与值分离

基础调用逻辑记录至少包含：

~~~text
invocation_id, parent_id, local_method_id, thread_id,
enter_seq, exit_seq, outcome, origin_test_line,
argument_ref, result_or_exception_ref, recovery_ref
~~~

子调用列表、同胞序号、方法 occurrence 序号、子树调用数及累计计数放到可重建索引，不在每个父记录里嵌入可能包含百万项的 children/prefix 数组。

普通记录与模板 occurrence 是互斥的物理表示；不能在保存模板后仍保留同一批完整展开记录作为另一份正式明细。索引只重复查询必需的标量。

明细包含本次采样契约下的调用证据；它不是完整 Java 对象快照，也不承诺逐字节恢复旧 raw 的冗余字段。

### 4.3 Manifest、fingerprint 与 JSON 交接

manifest 保存身份、采样配置、采集范围、块目录页引用、方法摘要、控制事件引用、各类逻辑计数、完成状态和恢复说明。

块清单过大时也分页，不把百万个块描述一次读入内存。SQLite 是内部查询实现，不能成为唯一的阶段交接协议或无法重建的数据来源。

分开记录：

- capture_fingerprint：按确定的逻辑事件顺序、采样配置和完整字段语义计算；改变采样策略必须改变此指纹。
- storage_fingerprint：对块内容、字典依赖和格式版本计算；用于发现损坏以及使旧索引失效。
- view_fingerprint：绑定过滤/断言折叠规则与对应统计。
- method_catalog_fingerprint：保持 bug 级确定性方法目录的身份。

流式计算指纹，不对全量对象 json.dumps。最终完整校验时，可用外部有序扫描验证解码后的逻辑记录；局部查询不重复全量校验。

## 5. 采集与转换数据流

~~~mermaid
flowchart LR
    A[Java 头部值采样] --> B[字典编码与独立事件块]
    B --> C[逐块消费与闭合调用处理]
    C --> D[有界结构压缩]
    D --> E[拓扑块与值块]
    C --> F[控制事件与视图标记]
    E --> G[磁盘索引]
    F --> G
    G --> H[局部拓扑查询]
    H --> I[按需取值并渲染 UML]
    C --> J[每个 trigger 的方法摘要]
    J --> K[suite manifest]
    K --> H
~~~

### 5.1 Java 输出

- 方法进入/退出时立即采集摘要，生成紧凑事件。
- 在同一输出临界区中分配 seq 并提交记录，保证新协议文件顺序与 seq 一致。ts_ns 若启用只是时间测量，不代替顺序号。
- 不在输出锁内执行业务值遍历；锁保护顺序号与已固化记录的提交。增加 tracer 自身重入保护，避免压缩/错误处理触发递归记录。
- 字典定义必须先于其引用提交，或由块明确指向已提交的独立字典。
- 压缩器使用有界缓冲；第一版不引入无界异步队列。慢磁盘通过写入阻塞形成背压，采集时间包含这部分成本。
- 更换目前可能吞掉 I/O 失败的写入方式，检查写入错误并设置持久的 recorder_failed 状态。即使业务测试完成，也不能把写失败的 trace 标记为完整。
- Java 压缩依赖需要验证 Java 8、运行平台、本地库加载与 shaded JAR；同步构建产物到 lib/fullchain-tracer.jar，并进行实际 agent 冒烟。

### 5.2 Python 流式转换

采集器消费已经封口并提交的事件块，可以与测试执行重叠；失败后也能离线重放这些块。第一版的复杂结构压缩放在这个采集转换器中，Java 不在每次方法调用时执行全局图分析。

- ENTER：记录待闭合调用的必要状态。
- RETURN/THROW：生成完整调用记录，写入拓扑/值存储，并释放待闭合状态。
- 活跃状态只保存单个 frame 必需的字段和累计统计，不累积全部已完成子节点。
- 活跃调用过多时溢写磁盘；待配对记录、重复 ID 检测、异常恢复列表不能形成新的无限字典。
- 方法/值字典有缓存预算，未命中查询或写入磁盘。记录输出、索引更新以固定字节预算分批提交。
- 控制事件立即追加到独立流；TEST_FAILURE、断言和 TEST_END 不因为结构压缩而消失。
- 新协议遇到重复/倒退 seq、重复 ID、非法退出、缺失父节点等，报告结构错误。旧协议使用显式的外部排序和兼容恢复规则。
- 父指针和同线程嵌套用于结构关联，不推断原 trace 没有提供的跨线程因果关系。

不能在转换器末尾再调用旧 build_trace/project_execution/fold_successful_assertions/build_refinement_trace 的完整对象路径。小样本参考实现可以保留在测试或旧产物迁移适配器中。

## 6. 循环与重复结构压缩

### 6.1 无损边界

压缩保存的是“新采样契约下可观察的记录”，不是证明两次执行的完整程序状态等价。即使两个头部摘要相同，未采集的后续元素仍可能不同。

每次 invocation 的 ID、父关系、顺序、线程、来源行、结果、异常、采样值与恢复状态必须可恢复。允许用模板和差分表达这些内容，不允许只保存代表调用。

存储层的压缩不要求在图中显示 loop/repeat。默认图仍逐次展开选中视窗；若未来增加图上循环折叠，应作为独立展示功能验证，不与本次存储切换绑定。

### 6.2 第一层：重复叶子调用

对同线程、同父节点、同来源行和同视图区间内，相邻且正常闭合的叶子调用，比较 method、参数摘要、返回摘要及其他保留字段。

相同调用可以存为模板加 occurrence run。不同 ID/seq 等逐次字段用差分列表或可证明正确的映射表示，不能假设 ID 连续。

遇到异常、断言边界、不同父调用、线程切换的全局顺序约束或记录不完整时，保守结束当前 run。其他线程的交错不能通过合并被隐藏，原始全局顺序仍必须可恢复。

### 6.3 第二层：重复序列与完整子树

- 只压缩已闭合结构。识别的是观测到的重复调用序列，不直接声称找到了源码循环边界。
- 结构相同但值不同：共享结构模板，保留逐次值引用和异常/顺序元数据。
- 结构和值摘要都相同：结构与值模板都可复用。
- 结构指纹使用相对节点关系、方法身份及保留字段；绝对 ID 等由 occurrence 表恢复。
- 指纹只用于定位候选；复用前比较完整规范化模板内容，不能仅凭 hash 相同认定相等。
- 模板依赖必须是可验证的无环结构，并限制展开深度。超过限额回退普通分块记录。
- 检测窗口、候选字典、模板节点数与模板字节数均有上限。巨大子树使用已落盘片段和增量统计，不能保留完整子树直到根返回。
- 超过预算或估算无空间收益时，输出普通记录。压缩失败只降低压缩收益，不能丢记录。
- 估算收益包含模板、occurrence 元数据、引用和新增索引成本，不能只比较模板正文。

### 6.4 递归

所有递归先使用普通父指针记录即可支持磁盘化，不要求等待专门递归编码才能处理大型递归。

专门编码仅用于已经闭合、边界明确的规则递归链：共享重复的层级结构，保存深度以及每层 ID、seq、参数、返回/异常和旁支信息。

不能把 f(3) → f(2) → f(1) → f(0) 写成一个普通 repeat_count=4。有分支、非规则旁路、异常或未闭合调用时回退普通记录或通用模板。

真实 StackOverflowError 的缺失退出恢复必须保持独立可审计：保留实际错误证据、原本未闭合的调用及合成结果标记，不能伪造正常返回。进程被杀不能使用该恢复规则冒充正常闭合。

## 7. 磁盘索引与有界查询

建议每个 trigger 一个数据库，由一个转换进程写入。finalize 后以只读方式查询，避免多个写者共同修改一个 suite 数据库。

主要索引：

| 索引/统计 | 主要键或字段 | 消费者 |
| --- | --- | --- |
| invocation locator | invocation_id → 块/行或模板/occurrence | 精确调用查询 |
| parent edge | parent_id, sibling_ordinal, child_id | 父子关系、前后同胞 |
| method occurrence | method_id, occurrence_ordinal, invocation_id | 方法分页 |
| invocation scalars | parent、enter/exit seq、outcome、thread、来源行、visibility | 视窗规划 |
| subtree statistics | invocation_id, visible_subtree_count | 省略计数 |
| sibling prefix | parent_id, ordinal, cumulative_visible_count | 区间计数 |
| method summary | method_id, visible_count | 可用方法、分页总数 |
| block/template locator | block_id、位置、大小、依赖 | 局部解压 |

方法的 occurrence_ordinal 按 enter_seq、invocation_id 确定，不按调用闭合顺序编号。父节点同胞序号也使用确定的进入顺序。

接口建议：

~~~text
get_invocation(id)
has_call(id, view)
get_parent(id)
get_children(parent_id, start_ordinal, limit, view)
get_siblings_around(id, before, after, view)
get_method_occurrences(method_id, offset, limit, view)
get_method_count(method_id, view)
get_subtree_count(id, view)
get_child_range_count(parent_id, start, end, view)
get_values(value_refs)
~~~

禁止 fetchall 全部 children/occurrences 后切片。已有 offset/limit 工具参数不变，在库内将 offset 映射成 occurrence 序号范围，避免深页 OFFSET 扫描或加载前面所有 ID。

SQLite 复合索引可支持筛选和排序；需要用查询计划及实际读行数验证，没有索引时不能仅凭接口有 limit 宣称高效。[SQLite 查询规划](https://www.sqlite.org/queryplanner.html)

控制 page cache、临时排序、mmap、事务批次及并发打开连接数量。cache_size 约束的是页缓存，不是整个进程内存上限；还需要进程监测和 Python 缓存预算。[SQLite 缓存说明](https://www.sqlite.org/pragma.html#pragma_cache_size)

第一版允许逐调用标量索引占 O(N) 磁盘空间。对高度重复 trace，实测索引成为主要开销后，增加模板 occurrence 范围索引；不能为追求小文件而让单次图查询展开全部重复调用。

## 8. 过滤、断言与 suite

### 8.1 一份基础证据，多种视图

基础证据保留已采集调用及控制事件。clinit 子树、噪声和成功断言折叠以独立的视图标记/关系记录表达，不生成 full/projected/pruned 三份拓扑。

同一调用可以具有多个排除原因。计数和默认可见性按集合语义计算，不能把各类排除数量直接相加导致重复扣除。默认视图继续保持现有过滤语义，并保留必要边界上下文。

成功断言区间按线程、动态 occurrence 和来源行匹配。只有完全位于区间内、正常返回且满足当前子树安全规则的调用才能默认隐藏。

不推断“断言通过意味着没有后续副作用”，不在 Java 阶段提前永久删除这部分证据。区间与子树判断使用磁盘关系、逐次状态和迭代式后序处理，避免每个断言扫描整个调用图以及 Python 深递归。

恢复隐藏视图只读取相应块，不依赖可选 debug raw。对历史已经丢失隐藏明细的产物，迁移时必须声明无法恢复。

### 8.2 suite 汇总

每个 trigger finalize 时输出可见方法摘要、边界方法、计数、失败信息和 trace manifest 引用。方法摘要超过内存预算时也采用有序分片。

suite 对这些摘要做确定性的去重归并，按 class、method、descriptor 生成 bug 级 M<n>；不解压调用/值明细。trace 内保持局部方法 ID，suite 保存局部到全局映射，避免改写全部调用。

T<n> 继续根据稳定 trigger 顺序分配，不以完成顺序分配。任一必需 trigger 不完整时，suite 只能处于明确的 partial 状态，不能伪称完整采集成功。

复用、suite-only 恢复和最终校验入口都改用轻量 manifest，不得继续调用整份 read_zstd_json。

## 9. UML 与 refinement 适配

引入领域层 TraceReader 协议，由基础设施层实现 SQLite/块读取。RefinementTraceTopology 的完整内存实现仅作为小样本测试参考或受控旧格式适配，新的大 trace 路径统一使用 reader。

一次图查询的顺序：

1. 校验 test_id 和 invocation_id。
2. 从索引获取焦点、限定数量的祖先/同胞/后代及省略统计。
3. 只展开这些节点涉及的模板 occurrence。
4. 合并所需 value_refs，按块批量读取值摘要。
5. 构造有界的局部执行对象，交给现有图构造和 PlantUML 渲染适配器。

最大祖先深度、同胞数量和内部节点数量沿用现有视窗配置；BFS 队列、边界上下文和图缓存同样有界。禁止 children() 返回整份元组再切片。

保持源码锚定查方法的 name/line 参数、METHOD 输出协议、T<n>-C<id> 及来源范围校验。索引、块 ID、模板 ID 和 catalog 内部信息不扩展为模型可见的额外协议。

默认视图满足：

~~~text
visible_call_count + omitted_call_count == default_view_call_count
~~~

这里不把边界上下文计作新的调用，也不把存储模板数量当作调用次数。另行记录 captured_call_count、filtered/folded 的集合计数。

图标签保留现有 scalar/object/enum/void/throw 的显示意义，并增加头部采样和未知省略数的准确呈现。新旧头尾/头部采样产生的图不要求像素相同；同一组逻辑采样记录使用不同存储编码时，局部图应等价。

已经渲染的 PUML/PNG、会话和 token usage 继续按现有证据保留规则保存，不因 trace 存储精简而删除。

## 10. 完整性、恢复与资源预算

### 10.1 提交状态

建议状态机：

~~~text
CAPTURING → INGESTING → VALIDATING → COMPLETE
     └─────────失败──────────→ PARTIAL / FAILED
~~~

CAPTURING 与 INGESTING 可以重叠，状态字段需同时记录 Java 是否结束、已提交块和转换进度，不能只靠一个字符串推断全部状态。

块写到 .partial 文件，完成压缩流、校验并刷盘后 rename。checkpoint 只引用已持久化块。顺序为：数据/依赖提交 → 索引事务提交 → checkpoint 提交；跨文件崩溃时按已提交块重放，允许可重建索引落后，不允许 manifest 引用缺失块。

最终 manifest 只有在 TEST_END、进程结果、事件闭合/显式恢复、引用、计数和完整性校验都满足后才原子替换为 COMPLETE。索引也绑定 storage_fingerprint，损坏/失配时重建。

checkpoint 必须包含或引用同一提交点的活跃调用、活动断言、字典依赖和计数状态；无法恢复这些状态时，从仍保留的事件块重新重放，不能只跳到某个 seq 后继续。

第一版在 trigger 尚未完成时保留全部已提交压缩事件块，磁盘不足则明确停止采集；不以删除未完成任务的恢复源来控制积压。正式存储完整校验通过后，普通模式可以清理本次任务自有的 ingest 暂存块，debug 模式保留。失败/部分任务保留恢复源；历史 raw/work 的移除继续通过显式 cleanup，而不夹带在迁移命令内。

### 10.2 失败分类

结构化记录 error_type、message、traceback、stage、trigger、最后提交块/seq、进程返回码和资源观测。

明确区分 PYTHON_MEMORY_ERROR、MEMORY_LIMIT、HOST_MEMORY_LOW、TIMEOUT、DISK_LIMIT、RECORDER_IO_ERROR、INVALID_TRACE 和 EXTERNAL_TERMINATION。不能只写 str(error)，也不能仅凭接近阈值把所有失败推断为 OOM。

内存耗尽时错误处理使用预先准备的最小输出路径，详细资源证据由父进程保留；失败处理不得立刻尝试再次加载或压缩全量对象。

被杀且没有 TEST_END 的 trace 可保留为部分证据，但不得供正式完整 trace 路径静默使用。

### 10.3 建议初始预算

| 项目 | 起点 | 说明 |
| --- | --- | --- |
| 独立块目标大小 | 4 MiB 未压缩 | 大附件分片 |
| Python 解析/写入批次 | 16 MiB | 按字节，不按固定事件数量 |
| 转换器模板候选缓存 | 32 MiB | 淘汰后回退普通记录 |
| 解压/值缓存 | 每进程总计 128 MiB | 所有 trigger 共用上限 |
| SQLite 页缓存 | 每个活跃连接 64 MiB | 限制同时活跃连接数量 |
| 待处理事件块窗口 | 按磁盘剩余和 checkpoint 控制 | 背压或明确失败，不删除恢复源 |
| Python 硬限制 | 初期保留现有 6 GiB | 不依赖抬高限制取得成功 |
| 转换/查询进程目标峰值 | 1 GiB 内 | 待实测，Java/PlantUML 另列 |

采集器、Java、渲染器和其他 workers 的内存需要同时计入机器预算；Python RLIMIT_AS 与 RSS 是不同指标，均需记录。

理论内存主要随活跃调用状态、缓存和固定批次增长；活跃深度/线程数变化会影响内存，不能承诺与所有输入规模都无关。极深状态可溢写，但磁盘/CPU 使用仍随实际执行增长。

设置可配置的测试耗时、磁盘预留和采集字节预算，超过时明确失败并保留 checkpoint。外部归并、数据库临时文件、源压缩块和目标块的重叠体积都计入预算。

压缩块大小、级别、缓存和模板检测预算放在独立的 trace_storage 配置；进程/磁盘/超时预算放在 trace_resources。它们不与第 3 节的值采样字段混在同一个严格字段集合中。纯存储参数参与 storage_fingerprint；任何改变采样或保留字段的参数仍参与 capture_fingerprint。

## 11. 协议版本及兼容

建议版本分配如下，实施前核对未被其他变更占用：

| 协议 | 当前 | 新方案 |
| --- | --- | --- |
| Fullchain agent protocol | 5 | 6，头部采样和有序紧凑事件 |
| Fullchain events block | 无 | 新 schema，v1 |
| trace value capture metadata | 随旧 trace | 独立 schema，v1 |
| refinement trace store manifest | 单体 refinement-trace v2 | 新 schema，v1 |
| trace topology/value/control/view/template 块 | 无 | 各自 schema，v1 |
| execution-trace-suite | 2 | 3，引用 manifest 与局部方法映射 |
| trace index | 现有内存拓扑 | 内部格式 v1，绑定存储指纹 |

新 storage schema 与旧 fullchain-trace schema 分开，不复用同一个数字表达不同布局。排序、采样和视图语义变化都参与配置/输入兼容检查；仅重压缩不应改变逻辑采样证据。

refinement 结果、上下文构建及已有结果复用需要检查新的 suite/trace 指纹。若其 JSON 字段发生变化则增加结果 schema 版本；仅后端读取实现变化时保持外部 METHOD/工具契约。

## 12. 旧产物迁移与这 25 个失败的恢复

提供明确的离线迁移入口，读取旧 raw JSONL、raw JSONL.zst、work JSON.zst 及单体 refinement-trace JSON.zst。迁移命令采用独立输出 root，原文件不覆盖。

- raw 按行读取；乱序 seq 使用有界排序段及受限 fan-in 的多轮归并，避免一次打开所有归并段。
- work/refinement 单体 JSON 使用真正的增量 JSON 解析器处理各数组；若对象键顺序妨碍关联，分批落盘后关联，不回退 json.load。
- 先验证原协议及数据完整性，再转换存储；记录 source_fingerprint、原 capture 配置和迁移工具版本。
- 迁移后的采样 profile 仍是原来的 head-tail。不能凭已截断的文本补出未记录的头部元素，也不能把原结果改标为 head。
- 历史 normalized trace 可能不再含完整断言隐藏记录/线程细节；按其实际证据标记 completeness/availability，不能补造。
- 新采样与旧采样结果不能作为同配置的直接实验对照。恢复旧实验可保持旧语义；要采用新的 head 配置则必须重新采集对应 trigger，并保留清晰 provenance。
- 复用只发生在源码/测试、插桩范围、采样配置、协议和完整性匹配时。已成功 trigger 不因存储迁移自动重采。

恢复优先级：

| 分组 | 处理 |
| --- | --- |
| Closure-145 | 优先流式迁移已有 work，重建方法摘要和 suite，先确认两个 work 均完整 |
| 20 个 raw 完成后失败的任务 | 校验 raw 的 TEST_END、退出状态与配对关系后离线转换；复用兼容的成功 trigger |
| Closure-90/95/96/152 | 已被终止的 raw 不当作完整 trace；用于格式/部分证据验证，需要完整结果时另行重采 |
| 149 个成功任务 | 不自动重跑；如需新格式，离线迁移并保留旧采样配置 |

迁移、重建索引、suite 汇总、重采和 refinement 是不同操作。重采或模型阶段由之后的具体执行任务明确安排。

## 13. 代码实施清单与顺序

| 阶段 | 内容 | 交付及退出条件 |
| --- | --- | --- |
| P1：契约与头部采样 | 新配置、Java 取参/摘要、机器省略语义、版本校验 | 采样边界测试通过，读取次数受限，结果不被采样操作改变 |
| P2：分块读写与索引 | schema、压缩块、manifest、fingerprint、TraceReader、磁盘索引 | 合成大型记录能写入/读取，局部查询不加载全图 |
| P3：全链路切换 | 有序 Java 输出、流式转换、磁盘视图、suite、refine/viewport | 采集到本地 UML 流程通过，所有复用/校验入口消除全量加载 |
| P4：结构压缩 | 叶子 run、重复序列/子树、规则递归链、差分 occurrence | 压缩开/关逻辑记录等价，有界回退和模板随机查询通过 |
| P5：迁移与恢复工具 | 旧协议增量读取、外部归并、checkpoint、错误分类 | 旧产物转换不改变采样语义，损坏输入明确失败 |
| P6：性能与真实验证 | 对照测量、单 bug 采集、局部渲染、失败恢复验证 | 达到第 14 节标准后，才安排批量恢复 |

P1 可以用小型测试适配器暂时验证采样；在 P3 完成前，不能把新配置上线到大规模批次并宣称已解决内存问题。P4 包括递归的通用正确处理与规则链优化，复杂不规则递归明确回退，压缩收益不作为正确性的前提。

建议新增领域模块：trace_store_schema、trace_reader、trace_compression、trace_capture_config，负责纯契约/算法。建议新增基础设施模块：trace_blocks、trace_store、trace_migration，负责文件、SQLite 与外部排序。

重点修改现有 trace/refinement_trace/assertion_folding/suite/graphs/focus_viewport/sequence_diagram；追踪 collect、CLI、批处理脚本、cleanup、结果复用与测试中的所有消费者。旧全量路径在迁移替代完成后从新生产链路移除，不保留隐藏的“大 trace 自动回退到内存加载”。

更新 config/mllm.example.json、README.md、fullchain_tracer/README.md、Java 构建与 lib JAR。配置中的模型、推理参数、API key 环境变量引用与本次改造无关，不顺带修改。

## 14. 验收方案

### 14.1 值采样与语义

- 数组、集合、Map：长度 0、1、N、N+1、大容量；确认只访问头部。
- 验证至多 N 次 next，没有 size/toArray/排序/尾部访问；确认边界 hasNext 不错误标记完整/省略。
- 顶层包含嵌套容器但嵌套项位于未采样尾部，采样数量仍只由深度决定。
- 默认深度边界、depth=0、自引用、互相引用、容器变更、迭代器抛错和未知长度。
- 多于 8 个参数、capture=false、void 与 boxed Void/null、Unicode/控制字符、异常与类型占位。
- 参数进入时取样，业务随后修改容器不会改变已记录摘要。
- 新配置拒绝旧 edge 字段和混合字段，不把布尔值当整数。

### 14.2 结构与压缩

- 固定深度的 10 万、100 万、1000 万调用；极深递归；单节点百万孩子。
- 同方法不同值、同摘要不同隐藏值、同结构不同异常、重复序列中间有断言边界。
- 多线程交错、非连续 invocation ID、乱序旧事件、重复 ENTER/EXIT、缺失父节点及未闭合事件。
- 压缩开/关使用同一逻辑输入，恢复后的字段、顺序、调用数量、视图与 semantic fingerprint 一致。
- 随机查询模板中的首/中/末次调用，验证 ID、父子、值和 omitted_count。
- 超大模板、无重复、高熵值、字典淘汰和人工 hash 冲突候选均正确回退。
- 递归与模板算法不依赖 Python 递归深度，模板引用无环。

### 14.3 恢复与完整性

- 空输入、损坏 JSON、损坏 Zstd、截断块、缺失字典、manifest/索引失配。
- 在块落盘、索引提交、checkpoint 和最终 manifest 提交之间分别中断，恢复不重复/遗漏调用。
- TEST_END 缺失、Java 写失败、MemoryError 空文本、磁盘预算和超时都产生明确状态。
- 仅真实终止性 StackOverflowError 按契约恢复；外部 kill 不伪造闭合。
- 旧 capture profile 迁移不变；旧 normalized 缺失证据如实标注。

### 14.4 suite 与渲染

- 增加 trigger 数量时，suite 只扫描方法摘要；初始化 refine 不读取全部 topology/value 块。
- 方法深页查询只读取指定 ordinal 页；高扇出节点只读取所需孩子。
- 相同逻辑记录经新存储后，方法定位、来源范围、局部节点、箭头值、调用顺序及省略数量一致。
- 生成 PUML 与 PNG 并实际检查可读性、焦点、异常、头部省略标记和空值展示。
- 保持 visible_call_count + omitted_call_count 的不变量及边界上下文计数规则。

### 14.5 性能与运行检查

记录总事件数、逻辑调用数、物理记录数、模板命中/回退、采样读取次数、原始逻辑字节、各压缩块/索引/临时文件体积、Java/Python 峰值 RSS 与 VmSize、吞吐、suite 时间、冷/热查询 p50/p95 和每次读入/解压字节。

验收目标：

- 固定线程数、深度和值配置，事件数从 100 万增至 1000 万时，Python 转换/查询峰值仍在配置预算内；初始目标不超过 1 GiB。
- 没有重复时也能完成采集，不能依赖压缩率防止 OOM。
- 单次方法分页和局部图查询的读取量由页/视窗及块大小限制，不随 trace 总调用数等比例增长。
- 多 trigger suite 汇总不重新加载全部调用；总数增长不会形成同时常驻所有 trace 的内存。
- 总磁盘成本必须包含索引与临时空间；不预先承诺固定倍数压缩收益。

实现时使用 /config/mllm/.venv/bin/python，按仓库要求执行单元测试、compileall、git diff --check；构建 Java agent 并校验 JAR 同步。涉及外部阶段时先安排隔离 root 的 Chart-1 单 bug 采集和本地 UML 渲染；本地 dry-run 通过不代表真实模型流程已经验证。

随后选择一个旧产物验证 suite-only 恢复，再选择一个完整大 raw 验证离线转换。具体恢复样本和运行预算在实际执行任务中确定；本文没有执行这些测试或调用模型。

## 15. 预期边界

本方案解决重复字段、尾部遍历、未压缩中间文件、全量 Python 对象、重复派生图和全量读取生命周期的问题。结构压缩进一步减少重复调用的物理体积。

它仍需要观察每次方法进入/退出以及配置要求的头部值；动态调用数很大时，采集 CPU 和磁盘工作仍存在。高熵值与不规则结构可能几乎无法做结构去重，但仍必须通过磁盘化稳定处理。

头部采样会失去旧方案可能记录到的尾部元素，这是用户已提出的采样策略调整。存储压缩以保留新采样契约下的逐次证据为边界，不再额外做调用采样、深度截断或按方法只保留前几次调用。
