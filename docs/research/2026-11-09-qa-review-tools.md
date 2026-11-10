# QA、代码评审与运行监控工具调研

- 日期：2026-11-09
- 范围：为“设计意图、跨层并发、真实用户工作流、运行监控”收敛方案选择可采用的 QA、代码评审与可观测性工具。
- 方法：只采用官方文档、官方源码仓库或一手论文；未采用营销榜单。文中区分“已证实”“未找到官方证据”和“本项目研判”。
- 边界：本次未安装工具、未修改产品代码、未读取敏感配置，也未向任何外部服务上传仓库内容。

## 一、结论先行

1. **Qoder 不只是 IDE 内置评审。** 已证实 Qoder IDE 有内置 Ultra Review，独立终端产品 Qoder CLI 也内置 `/review`；但未找到一个可单独安装、只包含 Code Review Agent 的本地 CLI 或独立 skill。CLI 是本地客户端，不等于本地模型或代码不出网。
2. **Qodo、CodeRabbit 的“本地运行”也不能直接等同于“私有代码不上传”。** Qodo Reviewer skill 明确会把工作树 diff 发给 Qodo 引擎；CodeRabbit IDE 明确上传被审文件的完整内容和 diff。两者的 SaaS PR 评审都需要相应 SCM/PR 权限。其 ticket/issue 上下文和仓库规则能力，确实比只做 diff 风格检查更接近“意图符合性”。
3. **CodeQL/Semgrep 应做定点规则和已知缺陷模式检查，不能充当通用并发正确性证明。** 它们适合把本项目已经确认的坏模式固化为回归门禁，但难以理解需求意图、证明跨层协议或穷举线程交错。
4. **测试有效性的核心组合是 Hypothesis 状态机/不变量 + 小范围变异测试 + 真实故障注入。** Hypothesis 负责生成操作序列，mutmut 检查测试是否能杀死实现变异，Toxiproxy 负责 TCP 网络故障；三者互补，均不能单独证明设计意图。
5. **运行监控建议直接围绕跨层语义埋点，而非先建通用大平台。** OpenTelemetry Python 可创建自定义 span、Counter 和异步 Gauge；Prometheus Python client 可暴露自定义指标；Grafana 可用版本化文件 provision 数据源和 dashboard。适合落地跨层时延、迁移、重试、代际丢弃和在途资源指标。

## 二、AI 代码评审工具

### 2.1 Qoder Code Review Agent / Qoder CLI

**已证实：产品形态**

- Qoder IDE 的 Ultra Review 是内置能力，官方说明无需额外配置，以 `/ultra-review` 触发；其 Completeness 维度会检查变更是否实现预期需求、验收条件、API 契约和状态分支是否覆盖。[Qoder Ultra Review 官方文档](https://docs.qoder.com/user-guide/chat/ultra-review)
- 独立 Qoder CLI 确实存在，并内置 `/review`：可审查当前改动、与基线分支比较、指定 commit，或按自定义要求评审；官方示例直接包含“检查并发安全”。`/review` 与 `/diff` 需要在 Git 仓库中运行。[Qoder CLI：View and Review Changes](https://docs.qoder.com/cli/review-changes)
- 因此准确表述应是：**评审是 Qoder IDE 和通用 Qoder CLI 的内置功能，不是已核实的独立“只做评审”的本地 CLI/skill 包。** CLI 总览见 [Qoder CLI 官方文档](https://docs.qoder.com/cli/overview)。
- Qoder CLI 需要浏览器 OAuth 或 Personal Access Token 登录。[Qoder CLI Authentication](https://docs.qoder.com/cli/authentication)

**未找到官方证据**

- 未找到 Qoder 官方对私有代码留存、是否用于训练、评审数据完整流向及私有化部署的足够明确说明。这里只能记为“待向厂商确认”，不能据此断言一定安全或一定不安全。
- 未找到 Qoder 原生连接 Jira/Linear 等 ticket 并逐条核对验收条件的官方证据。当前可确认的意图输入主要是 Completeness 评审、自定义评审指令和仓库规则/记忆。[Qoder CLI Memory](https://docs.qoder.com/cli/memory)

**本项目研判**

- Qoder CLI 的 `/review` 适合在**非敏感样例或明确允许外发的变更**上试点“并发安全 + 验收条件”提示词；不应因命令在本机执行就默认定私有代码不出网。
- 若目标是可重复门禁，需保存评审范围、提示词和判定标准，避免只依赖一次性的 Agent 结论。

### 2.2 Qodo 与 CodeRabbit：权限、上传范围和意图符合性

| 项目 | 已证实的数据/权限边界 | 有助于“意图符合性”的能力 | 本项目判断 |
|---|---|---|---|
| Qodo SaaS | GitHub 多租户部署需要安装 App 并选择仓库范围；连接后会同步仓库、索引 PR 历史并做跨仓库发现，不是仅在本机看 diff。[Qodo Multi-Tenant](https://docs.qodo.ai/install-qodo/github/qodo-multi-tenant) | Ticket 集成支持 Jira、Linear、Azure DevOps、Monday.com 及 GitHub/GitLab Issues；可从 PR 描述或分支名识别 ticket，并给出需求对齐反馈。[Qodo Ticketing Integrations](https://docs.qodo.ai/integrations/ticketing-integrations) | 意图证据最完整之一，但默认 SaaS 不适合未经批准的私有仓库 |
| Qodo Agentic Toolbox / Reviewer skill | 官方明确写明 Agent 会把 working-tree diff 发送给 Qodo review engine，所以本地 skill 仍有云端数据流。[Qodo Reviewer Skill](https://docs.qodo.ai/agentic-toolbox/agentic-toolbox-codebase-review-skill) | 本地评审可附 ticket/spec/design 链接及会话意图摘要；治理侧可用 Review Standards 固化项目规则。[Qodo Review Standards](https://docs.qodo.ai/governance/review-standards) | 可在脱敏样例试点；不能标成“完全本地评审” |
| Qodo 私有化 / 开源 PR-Agent | Qodo 提供 single-tenant、on-premises 等部署模型。[Qodo Deployment Models](https://docs.qodo.ai/deployment-models) 开源 PR-Agent 已迁为社区维护的 legacy 项目；其数据流取决于自行配置的 LLM 提供商。[The-PR-Agent/pr-agent 官方仓库](https://github.com/The-PR-Agent/pr-agent#data-privacy) | 开源评审配置支持 `extra_instructions` 和 ticket 合规相关选项。[PR-Agent Review 文档](https://docs.pr-agent.ai/tools/review/) | 有强隐私要求时才评估自托管；需把社区维护成本计入 |
| CodeRabbit SaaS | GitHub App 官方权限页列出 code、checks、issues、pull requests 等读写权限及其他只读权限；实际授权应以安装时页面为准。[CodeRabbit GitHub Permissions](https://docs.coderabbit.ai/platforms/github-com) | Linked issues 可根据 GitHub/GitLab Issues、Jira、Linear、Azure DevOps 工作项验证 PR 是否满足需求。[CodeRabbit PR Validation](https://docs.coderabbit.ai/issues/pr-validation) | 适合有明确 SCM 授权的组织，不适合本轮直接接入私有仓库 |
| CodeRabbit IDE/CLI | IDE 隐私页明确列出完整文件内容和 diff 等上传数据；path filter 在上传后才应用，不能当作阻止上传的机制。[CodeRabbit IDE Privacy](https://docs.coderabbit.ai/ide/privacy) CLI/IDE 总览见 [IDE & CLI Review](https://docs.coderabbit.ai/overview/ide-cli-review) | 支持按路径自定义评审指令、读取仓库 code guidelines，以及自然语言 custom checks。[Path Instructions](https://docs.coderabbit.ai/configuration/path-instructions) [Code Guidelines](https://docs.coderabbit.ai/knowledge-base/code-guidelines) [Custom Checks](https://docs.coderabbit.ai/pr-reviews/custom-checks) | 比单纯风格检查更接近意图审查，但上传边界必须先过隐私评审 |
| CodeRabbit 自托管 | 官方提供 Enterprise 自托管形态；自托管时评审发往自有实例。[Self-Hosted Overview](https://docs.coderabbit.ai/self-hosted/overview) | 与上述规则/issue 能力组合才有意义 | 本轮不为单一项目引入企业级自托管 |

**时效性说明**

- Qodo 官方说明旧 IDE 插件已于 2026-11-01 退休，当前应看 Agentic Toolbox，而不是继续设计基于旧 Qodo IDE 插件的方案。[Qodo IDE Plugin Deprecation](https://docs.qodo.ai/qodo-ide-plugin-deprecation)

**共同边界**

- Ticket 链接、需求文本和自定义规则能改善“有没有按意图做”的检查，但仍属于模型辅助判断，不能替代可执行验收测试、人工架构评审和跨层不变量。
- 本轮默认不向这些云服务发送真实私有仓库；若试点，应使用公开/合成/脱敏代码，并先确认权限、留存和子处理方条款。

## 三、CodeQL 与 Semgrep 对 Python 并发、跨层语义缺陷的实际边界

### 3.1 已证实的能力

- CodeQL 通过可执行查询分析代码，Python 官方查询索引覆盖正确性、可靠性、安全等规则，并允许基于 CodeQL 仓库中的示例编写自定义查询。[CodeQL Python Query Help](https://codeql.github.com/codeql-query-help/python/) [CodeQL CLI 官方说明](https://docs.github.com/en/code-security/codeql-cli/getting-started-with-the-codeql-cli/about-the-codeql-cli)
- Semgrep 支持自定义 pattern 和 taint 规则；taint mode 可声明 sources、sinks、sanitizers、propagators，适合把特定数据流约束固化为规则。[Semgrep Taint Mode](https://semgrep.dev/docs/writing-rules/data-flow/taint-mode)
- Semgrep 官网把 Python 的 cross-file dataflow、framework-specific control flow 和 Pro rules 列为 Semgrep Code 能力，因此不能把商业 Pro 的跨文件能力默认算到本地 CE。[Semgrep Supported Languages](https://semgrep.dev/docs/supported-languages)

### 3.2 对本项目的适用点

**适合立即固化的规则例子：**

- 某些旧代际回调不得写入新代际状态；
- 特定资源释放后不得再发送或复用；
- 已知危险 API、缺失超时、异常吞噬、敏感数据流向日志；
- 特定模块间禁止绕过既有 facade/管理器直接调用；
- 将历史修复中的具体坏模式变成 Semgrep 规则，将需要较精确语义/数据流的少数规则写成 CodeQL 查询。

这些是“从已知缺陷提炼回归门禁”，不是让工具凭空理解设计。

### 3.3 明确边界

- 在 CodeQL 官方 Python 查询索引中，本次未找到一个可作为通用 Python 线程竞态/跨层协议证明器的内置查询。这是对该索引的核验结果，不是声称所有自定义 QL 都无法表达任何并发属性。
- 静态工具通常不知道真实 DNS 返回时序、socket 到达顺序、worker 生命周期、前端切换和远端故障组合；即使写出锁/代际规则，也只能检查所建模的属性。
- “需求允许哪些状态迁移”“用户操作最终是否完成”“旧请求是否污染新会话”等跨层语义，必须先由项目写成不变量、状态机和测试预言，工具才能帮助检查。
- 因此本轮不建议投入建设大型 CodeQL 并发查询库；优先使用 Semgrep 做轻量反模式门禁，CodeQL 做少量高价值定点查询，把复杂时序交给测试与监控。

## 四、Hypothesis、变异测试与故障注入

### 4.1 Hypothesis：从示例测试升级为操作序列和不变量

**已证实**

- Hypothesis 的 `RuleBasedStateMachine` 可用 `@rule` 定义操作、`@precondition` 限制适用状态、`@invariant` 持续检查属性，并生成不同的操作序列。[Hypothesis Stateful Testing](https://hypothesis.readthedocs.io/en/latest/stateful.html)
- 官方状态机文档并未声称会自动探索任意 OS 线程交错；线程并发需要测试自行建模、启动和协调。

**本项目价值**

- 将“启动 → DNS 查询 → 选择路由 → 建连 → 前端切换/失败 → 重试 → 取消/关闭”建成状态机，以参考模型定义允许的状态和终态。
- 不变量示例：一个逻辑请求至多交付一次；取消后不得成功交付；旧代际结果不得修改新代际；最终不存在未释放的在途资源；重试不得突破剩余期限。
- 对真实竞态，应在测试中使用 Event/Barrier 等确定性控制点安排关键先后次序，再用 Hypothesis 生成操作组合。它比单纯随机压力测试更容易复现和缩减失败用例。

### 4.2 mutmut / 变异测试：验证测试是否真的能发现回归

**已证实**

- mutmut 是 Python mutation testing 工具，可运行 pytest、记录进度、查看/重测变异体，并支持按模块或函数限定范围。[mutmut 官方仓库](https://github.com/boxed/mutmut)
- 变异测试通过故意改变实现并观察测试是否失败，直接检验断言和测试预言的强度；它不是普通行覆盖率的替代品，而是补充。
- 等价变异体可能与原程序行为相同而无法被“杀死”，这是变异测试长期已知的问题，不能把存活率机械等同于缺陷率。[Jia & Harman，Mutation Testing 综述，DOI](https://doi.org/10.1109/TSE.2010.62)

**本项目价值与边界**

- 优先只变异修复过的纯逻辑：剩余期限计算、退避、代际比较、状态迁移、去重/取消判断。
- 存活变异体能揭示“测试执行了但没断言”的空白；被杀死的变异体不证明并发时序已穷尽。
- 不建议本轮全仓库运行：网络和线程测试耗时、等价变异体及波动测试会放大成本。先做一个模块的基线，记录 killed/survived/timeout 和人工判定。

### 4.3 Toxiproxy：面向真实用户工作流的 TCP 故障注入

**已证实**

- Toxiproxy 是面向测试、CI 和开发的 TCP 代理，可通过 HTTP API 动态操纵连接；支持 latency、down、bandwidth、slow_close、timeout、reset_peer、slicer、limit_data、packet_loss 等 toxics。[Shopify Toxiproxy 官方仓库](https://github.com/Shopify/toxiproxy)

**本项目价值与边界**

- 可验证“连接超时后切换前端”“响应中途断开”“慢连接不得阻塞新会话”“重试后用户请求仍只交付一次”等端到端工作流。
- 它是 **TCP** 代理，不能直接覆盖 DNS/UDP 故障。DNS 应另用可控本地假 DNS 服务或测试 hook 模拟超时、错误码、乱序和重复应答。
- 故障注入验证的是系统在故障下的行为，不会自动证明实现符合需求；仍需把用户可见结果和资源清理写成断言。

## 五、OpenTelemetry、Prometheus、Grafana 的适用性

### 5.1 已证实的能力

- OpenTelemetry Python 支持手动创建 span 和 attributes，也支持自定义 Counter、异步 Gauge 等指标，适合表达项目自己的跨层语义，而不只依赖框架自动插桩。[OpenTelemetry Python Instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
- Prometheus 官方 Python client 提供 Counter、Gauge、Histogram、Summary 及 labels，可暴露应用自定义指标。[prometheus/client_python 官方仓库](https://github.com/prometheus/client_python)
- Grafana 的 provisioning 系统可用配置文件定义数据源和 dashboards，并将这些文件纳入版本控制。[Grafana Provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)

### 5.2 推荐的跨层指标与 tracing 设计

**指标只保留有限、可聚合的标签：**

- `xxnet_stage_duration_seconds{layer,operation,outcome}`：DNS、路由、建连、首包、完整交付等阶段时延；
- `xxnet_transition_total{layer,from_state,to_state,outcome}`：受控状态迁移计数；
- `xxnet_retry_total{component,reason}` 与 `xxnet_front_switch_total{from_front,to_front,reason}`：重试和前端切换；
- `xxnet_stale_result_total{layer,reason}`：旧代际/迟到结果被丢弃；
- `xxnet_inflight{layer,resource}`：在途请求、连接、worker、buffer 等；
- `xxnet_user_workflow_total{workflow,outcome}`：从用户视角统计成功、失败、取消，而非只看内部调用成功。

**Tracing 用于单次请求因果链：**

- 以 trace/span 串起 `smart_router → DNS → route → front → x_tunnel/gae_proxy → response`，记录状态迁移、重试原因和剩余期限。
- request id、connection id、IP、域名、完整 URL 等不应成为 Prometheus label；高基数字段只放在受控日志/trace 中，并先做脱敏与采样。
- OpenTelemetry 是采集/上下文与导出标准，Prometheus 是指标存储和查询，Grafana 是可视化；三者职责不同，不应把 Grafana 当成埋点库。

### 5.3 最小 dashboard

1. 用户工作流成功率与失败原因；
2. 各层 p50/p95/p99 时延；
3. 重试、前端切换、旧代际丢弃速率；
4. 在途资源和连接泄漏趋势；
5. 单次失败工作流的 trace 下钻入口。

本轮只需本地、小流量、有限标签的最小闭环，不需要先上完整生产级可观测性平台。

## 六、明确采用建议

### 立即可采用

1. **Semgrep CE 本地规则门禁**：先把已确认的跨层反模式和禁止调用写成少量高精确规则；使用本地扫描并关闭非必要遥测，不外发仓库。
2. **CodeQL 定点查询**：沿用本地 CLI/默认 Python 查询做基线，只针对高价值数据流或已知缺陷补自定义查询；不承诺通用竞态检测。
3. **Hypothesis 状态机与不变量**：优先覆盖 DNS/路由/连接/取消/前端切换的跨层工作流，配合确定性 Event/Barrier 测试复现竞态。
4. **跨层可观测性最小闭环**：用 OpenTelemetry API 表达 trace 与自定义指标；可先用 Prometheus client 暴露少量指标，再用 Grafana 版本化 dashboard。先定义语义和标签预算，再埋点。

### 可选试点

1. **Qoder CLI `/review`**：在非敏感或获准外发的变更上，用固定并发/验收提示词评估命中率和误报率；隐私条款未确认前不接入真实私有仓库。
2. **Qodo / CodeRabbit**：若组织允许 SCM 授权与代码外发，试点 ticket/issue 对齐、Review Standards/Path Instructions；否则只看私有化或合成样例，不默认启用 SaaS。
3. **mutmut 小范围变异测试**：先选 1–2 个纯逻辑模块，以人工复核存活变异体为目标，不设全仓库分数门槛。
4. **Toxiproxy TCP 故障注入**：为 2–3 条高价值用户工作流增加可控断连、延迟和超时；DNS/UDP 另行构造故障。
5. **开源自托管 PR-Agent**：只有确需本地/私有模型评审且愿意承担社区 legacy 项目维护成本时再评估。

### 不适合本轮

- 把 Qoder、Qodo 或 CodeRabbit 的“本地 CLI/skill”当作不上传代码的证明，或未经审批直接授予其私有仓库/PR 权限。
- 期待 CodeQL/Semgrep 自动发现所有跨层竞态、状态机缺陷或设计意图偏差。
- 全仓库变异测试、以 mutation score 作为机械 KPI，或把存活变异体全部当作真实缺陷。
- 用 Toxiproxy 覆盖 DNS/UDP，或一开始引入重型生产混沌工程平台。
- 先建庞大监控平台、把高基数请求标识写入 Prometheus labels，或只有 dashboard 而没有跨层语义埋点。

## 最终收敛路径

**本地静态反模式门禁（Semgrep/CodeQL） → Hypothesis 状态机与确定性竞态测试 → 小范围 mutmut 检查测试预言 → Toxiproxy/假 DNS 驱动真实工作流 → OpenTelemetry + Prometheus + Grafana 观察同类不变量在生产/验收运行中的表现。**

AI 评审工具只作为可选的第二双眼睛：其价值主要来自需求/ticket 上下文和项目规则，而不是替代上述可执行验证。

## 本项目最终采用裁定

主线程结合本轮“不上传仓库、不新增运行依赖、先落本地 Dashboard”的约束，对上述通用建议作如下收窄：

- **现在直接执行**：独立意图审查、证据门禁、确定性交错测试、定向变异/反转、本地故障注入和白名单运行指标。
- **环境已具备时才试点**：Hypothesis、mutmut、Semgrep、CodeQL；本轮不安装依赖，不把它们设为准入门禁。
- **不接入真实仓库**：Qoder、Qodo、CodeRabbit 的云端评审；其本地 CLI/IDE 不等于代码不出网。
- **不进入本轮运行路径**：OpenTelemetry、Prometheus、Grafana 和 Toxiproxy。它们保留为长期观测或专项端到端故障注入的后续选项，本轮先用标准库 snapshot、本地页面和受控测试服务完成闭环。
