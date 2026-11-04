# Todo Agent

一个基于 Python 的对话式 Todo Agent。项目从 [Amankhan1009/todo-agent](https://github.com/Amankhan1009/todo-agent) 派生，经过运行时框架、数据模型、工具协议、权限控制、持久化和运维能力的扩展，形成当前版本的统一 Harness 架构。

## 项目特点

- 使用自然语言创建、查询、搜索、更新、完成、取消和归档任务
- 支持任务状态、执行记录、提醒、重复任务、目标、统计、账务和链接等扩展能力
- 使用统一的 `AgentLoop`、模型适配器、工具注册表和工具协议
- 通过 `ToolCallContext` 注入可信用户身份，工具不会从模型参数中接收 `user_id`
- 对修改、删除、归档、账务等高风险操作提供权限分级和确认机制
- 具备模型/工具超时、调用预算、取消、重试、错误码和运行时 Hook
- SQLite 持久化，支持版本化迁移、备份、恢复和完整性检查
- 提供 Streamlit Web 界面、命令行入口、Docker Compose 和 GitHub Actions CI

## 来源与版本说明

本项目不是原仓库的原样镜像，而是基于原项目进行的二次开发：

- 上游项目：[Amankhan1009/todo-agent](https://github.com/Amankhan1009/todo-agent)
- 当前仓库：`crab921-chen/TodoAgent`
- 主要新增和调整：Harness 统一运行时、工具协议与权限边界、任务/提醒/重复任务/目标/统计/账务能力、数据库迁移、运维检查、Docker 部署和自动化测试
- `legacy/` 保存原 LangGraph 实现，用于兼容、对照和历史诊断；当前 Web 与 CLI 入口使用 `harness/` 运行时

详细设计和版本资料位于 `docs/` 及 `docx/`。其中 `docx/` 是开发过程文档，不是运行时依赖。

## 技术栈

- Python 3.12
- Streamlit
- LangChain / LangGraph（历史实现和兼容层）
- OpenAI-compatible Chat Completions 模型接口，可配置智谱等服务
- SQLite
- Docker / Docker Compose
- unittest / GitHub Actions

## 目录结构

```text
.
├── app.py                    # Streamlit Web 入口
├── cli.py                    # 命令行入口
├── harness/                  # 统一 AgentLoop、模型、工具协议和安全边界
├── database/                 # 数据库、迁移、业务服务、备份和恢复
├── ops/                      # 配置、日志、健康检查、指标和运维命令
├── ui/                       # Streamlit 工作台和界面运行时
├── legacy/                   # 原 LangGraph 实现和兼容代码
├── graph/                    # 历史导入兼容层
├── tools/                    # 历史工具兼容层及 CI/发布检查
├── prompts/                  # 历史提示词兼容层
├── tests/                    # 单元、数据、协议和运行时测试
├── docs/                     # 架构、迁移、运维和发布文档
├── deploy/                   # Dockerfile、Compose 和容器启动脚本
├── requirements.txt
└── .env.example
```

## 快速开始

### 1. 准备环境

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS/Linux：

```bash
source .venv/bin/activate
```

安装依赖：

```bash
python -m pip install -r requirements.txt
```

### 2. 配置模型服务

复制 `.env.example` 为 `.env`，填写模型服务密钥和模型配置：

```powershell
Copy-Item .env.example .env
```

`.env` 只保存在本地或部署环境，禁止提交到 Git。当前示例默认使用 OpenAI-compatible 接口：

```dotenv
ZHIPU_API_KEY=your_api_key
MODEL_NAME=glm-5.3
MODEL_BASE_URL=https://open.bigmodel.cn/api/paas/v4
```

运行时也兼容 `GROQ_API_KEY`、`GROQ_MODEL` 和 `GROQ_TEMPERATURE` 等旧配置名。

### 3. 检查和初始化

```bash
python -m ops.startup_check
```

应用启动时会自动创建数据库目录并执行安全迁移。生产环境建议先完成备份策略和恢复演练。

### 4. 启动应用

Web 界面：

```bash
streamlit run app.py
```

命令行：

```bash
python cli.py
```

Docker Compose：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

默认 Web 端口为 `8501`。Compose 配置同时包含 Web 服务和提醒工作进程。

## 测试与质量检查

```bash
python tools/ci_checks.py --release
python -m compileall -q app.py cli.py database graph harness legacy ops prompts tests tools
python -m unittest discover -s tests -p "test_*.py" -v
python tools/release_signoff.py
```

GitHub Actions 会执行静态检查、编译检查、核心测试、依赖检查、Docker 构建、启动检查和发布签核。

## 配置项

完整默认值见 `.env.example`。常用配置如下：

| 配置 | 作用 |
| --- | --- |
| `APP_ENV` | `development`、`test`、`staging` 或 `production` |
| `ZHIPU_API_KEY` / `GROQ_API_KEY` | 模型服务密钥 |
| `MODEL_NAME` | 模型名称 |
| `MODEL_BASE_URL` | OpenAI-compatible API 地址 |
| `MODEL_TEMPERATURE` | 模型温度 |
| `MODEL_TIMEOUT_SECONDS` | 模型调用超时时间 |
| `MAX_TOOL_CALLS` | 单次请求最大工具调用次数 |
| `TODO_DATABASE_PATH` | Todo 数据库路径 |
| `TODO_CHECKPOINT_DATABASE_PATH` | 会话检查点数据库路径 |
| `TODO_DEFAULT_TIMEZONE` | 默认时区 |

## 数据与安全

运行产生的 `database/todo.db`、`database/checkpoints.db`、备份文件、日志和测试数据库不属于源代码，不应上传到公共仓库。API 密钥、完整提示词、原始工具参数和用户业务数据也不应写入普通日志。

当前登录方式是用户名级别的应用隔离，不等同于生产级身份认证。正式部署前应接入密码认证、OAuth 或外部身份提供商，并根据部署规模评估 SQLite 与单机提醒工作进程的适用性。

## 文档

- [代码布局](docs/code_layout.md)
- [运维手册](docs/operations.md)
- [统一框架迁移说明](docs/unified_framework_migration.md)
- [发布签核说明](docs/release_signoff.md)
- [开发需求与设计文档](docx/)

## 许可与致谢

本项目基于上游 [Amankhan1009/todo-agent](https://github.com/Amankhan1009/todo-agent) 进行修改和扩展。发布前请确认上游项目的许可证要求，并在仓库中补充本项目最终采用的许可证文件。
