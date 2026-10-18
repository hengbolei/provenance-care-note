# Provenance Care Note

[English](README.md) | **简体中文**

[![CI](https://github.com/hengbolei/provenance-care-note/actions/workflows/ci.yml/badge.svg)](https://github.com/hengbolei/provenance-care-note/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个以溯源为核心的纵向护理记录原型，面向单个合成患者。它帮助临床人员快速掌握紧急信息，同时让
AI 生成内容、人工撰写的记录、原始证据和访问边界都清晰可见。

> 本仓库只包含合成的演示数据。它不是医疗器械，也尚未达到生产可用的程度。

## 当前功能

- 32 个评测用例和 CI 评测门槛；最新结果（包括已知缺口）见[评测报告](evals/REPORT.md)。
- 界面支持英文、简体中文和繁体中文，可在顶栏切换，浏览器会记住选择。病历原文、服务端消息和模型输出保持原有语言，不做翻译。
- 10 秒速览（Glance）视图：确定性、可解释的优先级因子，以及风险原因、建议操作和审核状态。
- 范围明确的药物、剂量和过敏冲突检测，冲突时要么明确以临床医生意见为准，要么标记为待审核。
- 可执行的重点事项（highlight）工作流：按护理角色分派，支持待处理/进行中/已完成状态、处置备注、乐观并发检查和仅含元数据的审计事件。
- 纵向时间线，包含患者、护理人员、临床医生记录，以及三种不同的 AI 记录类型。
- 按角色撰写的手动时间线记录，作者和记录类型由服务端生成，浏览器无法伪造。
- 临床医生可以从精确选中的原文片段创建重点事项，并附带风险原因、建议操作、优先级、分派和审计元数据。
- 精确的重点事项溯源指针：先定位到具体记录和字符区间，再继续追溯到原始的合成消息或转录片段。
- 时间线中的原文片段精确高亮，以及带签名的临床医生/护理人员/患者演示会话。
- 服务端的诊所范围、角色归属和患者本人访问检查。
- 面向患者的内容过滤，排除内部重点事项和未经审核的 AI 记录。
- 患者端仪表盘：展示经临床医生确认并明确发布的指导、行动进度，并提供表单让患者补充症状或提问。
- 确定性的分区版本管理、审计元数据、回滚功能和乐观并发检查。
- 护理团队内部评论，可绑定到记录、分区或精确片段，支持回复、角色提及、分派、解决/重新打开、乐观锁和完整的修订快照。
- 可编辑的诊疗计划，带有 API 支持的修订历史、任意版本对比和回滚，以及完整的重点事项修订快照。
- 页面内仅含元数据的审计轨迹，覆盖记录创建、评论与回复、解决/重新打开、重点事项分派/完成/审核、计划编辑和回滚。
- 经过测试的 PHI 脱敏网关，供可选的外部文本摘要适配器使用。
- 确定性的合成数据，用于可重复的演示和测试。
- 响应式的英文 Web 界面，包含加载中、空数据、无权限、未找到和错误等状态。
- 带签名、会过期的会话：HttpOnly Cookie、登出即吊销、基于 PostgreSQL 的账户与诊所成员关系，并且不存在可交互登录的 `system` 身份。
- 可选的加密 PostgreSQL 快照，强制启用诊所级行级安全（RLS），加密的冷数据归档，哈希链式审计事件，请求日志最小化，以及 TLS 反向代理基线配置。
- WebSocket 在线状态与记录自动刷新，评论/重点事项任意快照的 A/B 对比，以及考虑曝光偏差、有上限的审核结果优先级调整。
- 可选的 OpenAI Responses API 摘要导入和音频转录。网关会脱敏已知标识符、要求服务端不存储数据、在内部保留原始来源，并且在未配置 API 密钥时默认拒绝调用。

## 技术栈

- Python 3.11+
- FastAPI 和 Uvicorn
- Pydantic 领域模型
- 原生 HTML、CSS 和 JavaScript
- Pytest 和 Ruff
- PostgreSQL/psycopg 与 Fernet 认证加密（可选的生产适配器）

开发环境默认使用内存存储。设置 `CARENOTE_DATABASE_URL` 后会切换到加密的 PostgreSQL 适配器，并执行 RLS 迁移。

## 快速开始

创建虚拟环境，并安装应用及开发依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

启动带自动重载的开发服务器：

```powershell
.\.venv\Scripts\python.exe -m uvicorn carenote.main:app --reload
```

然后打开：

- 应用：`http://127.0.0.1:8000`
- 交互式 API 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/api/health`

## 用 Docker 离线演示

```bash
docker compose up --build
```

这会以加密 PostgreSQL 和合成种子病人启动应用。用 `clinician` / `clinician-demo-2026` 登录，在医护工作区点
**Prepare review**。审阅 Agent 会回放 `evals/cassettes/` 里录制好的草稿，不需要 API key，也不会调用模型；
面板会显示草稿的来源。种子草稿是为演示流程手写的，并如实标注，不会冒充模型输出。若要用真实模型起草，设置
`CARENOTE_AGENT_GATEWAY=openai` 和 `OPENAI_API_KEY`。

`python scripts/smoke_demo.py` 会对运行中的演示登录、发起审阅并提交；CI 在每次改动时都会对 Docker 环境跑一遍。

## 合成演示账户

界面使用服务端登录和带签名、会过期的 HttpOnly 会话 Cookie。仅限开发环境的账户有 `patient`、`staff`、
`clinician` 和 `admin`，密码格式为 `<用户名>-demo-2026`。服务端从不接受浏览器传来的用户 ID 或角色/诊所请求头。
配置 PostgreSQL 后，账户、有效成员关系和哈希后的会话 ID 都会持久化保存。

## 验证

运行完整的自动化测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

运行静态检查和格式检查：

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check src tests scripts
node --check src/carenote/static/api.js
node --check src/carenote/static/app.js
node --check src/carenote/static/service-worker.js
.\.venv\Scripts\python.exe scripts\benchmark_glance.py --iterations 1000 --warmup 100
```

核心验收场景位于：

- `tests/test_rbac_scope.py`
- `tests/test_revision_history.py`
- `tests/test_highlight_provenance.py`
- `tests/test_concurrent_edits.py`

其他测试覆盖合成数据集、患者 API 以及前端与 API 的契约。当前本地基线为 45 个测试全部通过。

## 架构与信任边界

HTTP 请求先进入 API 层，再交给服务层；诊所范围、角色归属、作者身份、并发和溯源规则都在服务层强制执行。
存储适配器负责持久化。浏览器界面不是安全边界。

每个重点事项都包含 `risk_reason`、`suggested_action`、审核状态和 `provenance_pointer`。
如果指针无法解析到同一患者的时间线，或字符区间超出了原始记录的范围，存储层会直接拒绝。

当前架构和生产化方向见 `docs/ARCHITECTURE.md`。

## 安全与隐私现状

原型中已实现：

- 只使用合成数据。
- 服务端的诊所范围和角色归属检查。
- 患者只能访问本人数据。
- 面向患者的响应会过滤内部内容和未经审核的 AI 内容。
- 完整的分区快照与差异对比，另有一条独立的、仅含元数据的审计事件流。
- 在可选的外部文本摘要请求之前，使用确定性的 PHI 脱敏网关。
- 签名会话令牌、过期与吊销、PostgreSQL 账户/成员关系表结构，以及强制的诊所级 RLS 策略。
- PostgreSQL 临床快照和冷数据归档的认证加密；带数据库锚点的哈希链式审计事件；仅含元数据的请求日志和保留期维护。
- 可选且默认拒绝的外部模型与转录适配器；未明确配置项目 API 密钥时不会发起任何调用。
- 可复现的 Glance 基准测试：最近一次本地记录的热路径 P95 为 1.535 ms。

上线生产前还需要：

- 在目标托管 PostgreSQL 服务上验证 PostgreSQL 适配器和 RLS 策略。
- 接入 TLS 证书、托管的密钥/机密服务、不可篡改的审计导出、法律保全、备份与恢复，以及运维层面的保留期调度。
- 用机构自己的身份提供方、MFA/SSO、账户恢复和账户生命周期流程，替换本地的密码配置方式。
- 在接近生产的数据库、网络、并发和代表性数据集上重新进行 Glance 压测；本地内存环境的结果不代表容量承诺。

默认不会调用任何外部 LLM。只有设置 `OPENAI_API_KEY` 才会启用模型边界；真实的临床使用还需要机构的隐私、数据保留和验证审批。

## 项目结构

```text
src/carenote/
├── api/           HTTP 路由与响应处理
├── core/          应用配置
├── data/          确定性的合成种子数据
├── domain/        领域模型与 API 契约
├── privacy/       PHI 脱敏边界
├── repositories/  存储适配器
├── services/      用例与策略执行
└── static/        患者端 Web 界面
tests/             自动化验收与集成测试
docs/              架构与开发文档
```

## 文档

以下文档均为英文：

- [Architecture](docs/ARCHITECTURE.md)：分层、信任边界和生产化方向
- [Technical brief](docs/TECHNICAL_BRIEF.md)：产品理念、范围和设计取舍
- [Development](docs/DEVELOPMENT.md)：环境要求、安装、测试和运行
- [Production security](docs/PRODUCTION_SECURITY.md)：必需的配置和密钥管理
- [Performance](docs/PERFORMANCE.md)：Glance 热路径性能测量
- [Clinical Review Agent design](docs/AGENT_DESIGN.md)：拟建的多步审核 Agent 设计
- [Agent roadmap](docs/AGENT_ROADMAP.md)：构建阶段、进度和决策记录
- [Attribution](ATTRIBUTION.txt)：第三方许可证

## 许可证

基于 MIT 许可证发布，详见 `LICENSE`。

> 本文档为英文 [README](README.md) 的中文翻译，如有出入以英文版为准。
