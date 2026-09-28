# 代码库指南

## 项目结构

- `paper/` latex 论文项目目录。`paper/PAPER.md`中记录了论文的写作思路。
- `dpex/domain/` 定义版本化 trace 和 ranking 模型及纯逻辑。
- `dpex/infrastructure/` 封装 Defects4J、外部命令、文件、Java 源码和 PlantUML。
- `dpex/stages/` 实现 collect、trace、uml、localize、aggregate。
- `fullchain_tracer/` 是独立 Maven 字节码 agent；工具 JAR 位于 `lib/`。
- 所有运行产物必须写入 `--root` 的 `workspace/`、`artifacts/`、`logs/`、`summaries/`。

## 运行与验证

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
source d4j_env.sh
python -m dpex --help
python -m unittest discover -s tests -v
python -m compileall -q dpex
python -m dpex localize --root runs/smoke --projects Chart --bugs 1 \
  --config config/dpex.example.json --dry-run
```

提交前执行单元测试和 `compileall`。修改外部阶段时，用单项目/bug 冒烟；Stage 5 必须先
`--dry-run`。

## 编码与协议

使用 Python 3、UTF-8、4 空格和 `pathlib.Path`。外部命令必须设置超时并检查返回码。
阶段只能通过带 `schema` 和 `schema_version` 的 JSON 交接；协议变化必须增加验证和测试。
新增解析、筛选或聚合逻辑需覆盖空输入、损坏 JSON、超时、重复项和确定性排序。

## 安全

凭据只能通过配置中的 `api_key_env` 引用环境变量。禁止提交密钥、真实模型响应、运行
产物和 Defects4J checkout。候选生成不得读取 fixed version、补丁或 ground truth。
