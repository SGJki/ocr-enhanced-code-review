# OCR + Codex 高效代码审查技术方案

## 1. 文档状态

- 状态：已批准；阶段 1-4 的确定性协议与 helper 已实现，阶段 5 的运行指标仍待接入调用方
- 适用范围：工作区、commit、branch range，以及 OCR scan 选择出的全文件审查
- 设计依据：当前项目的 `skill/SKILL.md`、`skill/scripts/prepare_review.py`，以及 OpenCodeReview `v1.12.6` 的 LLM review 实现

## 2. 背景与问题

当前项目已经把 OCR 定位为确定性规划器：OCR 负责选择变更文件和解析规则，Codex 负责读取代码、验证问题并输出 CR。这条边界是正确的，不应改成直接调用 `ocr review`。

需要吸收 OCR 的优秀设计，但不复制 OCR 的完整 LLM 链路：

1. OCR 的选集、规则、分组和覆盖控制较成熟，可以降低 LLM 自由发挥造成的漏审和越界。
2. OCR 的 plan、主审查、评论过滤和工具循环能提供可借鉴的任务拆分与质量控制。
3. OCR 将多种职责串在多个 LLM 请求中，存在 token 成本高、延迟高、上下文重复和状态复杂的问题。
4. OCR 的 `code_comment` 同时承担问题发现、评论格式化和代码定位，导致 LLM 需要处理过多机械工作。

本方案的目标是：保留“脚本/OCR 控制边界，LLM/Codex 负责推理”的总体架构，将确定性工作进一步脚本化，将 LLM 调用收敛到必要的推理和少量按需验证。

## 3. 目标

### 3.1 功能目标

- OCR 的 `reviewable_files` 是唯一完整审查集合，不允许 LLM 或后续脚本自行扩大、缩小或替换。
- 每个选中文件都有明确的 rule group、diff 和 changed-line 映射。
- 大变更可以先生成结构化风险计划，小变更跳过 plan 以节省调用。
- LLM 只提交候选 finding，不直接修改最终评论集合。
- 脚本验证 finding 的文件范围、变更行、schema、定位、重复项和严重性。
- 对高风险或语义不确定 finding 提供可选的二次 LLM 验证，并且默认保留无法证伪的 finding。
- 最终输出稳定、可定位、可去重，并包含审查覆盖和限制信息。

### 3.2 工程目标

- 明确脚本/OCR 与 LLM/Codex 的职责边界。
- 尽量复用现有 `prepare_review.py` 的 JSON 校验和 batching helper。
- 同一输入 manifest 在重复执行时产生稳定的 review packet 和输出顺序。
- 以每个 review group 为并行单位，限制单次 prompt 大小和总 token 预算。
- 所有失败都可分类，不能用静默降级掩盖漏审。

## 4. 非目标

- 不直接调用 OCR 的 `ocr review`，不复用 OCR 的 provider、session 或内部 LLM loop。
- 不把 LLM 变成变更文件选择器、规则解析器或最终行号裁判。
- 不要求所有 review 都经过 plan 或二次 judge。
- 不在本阶段实现自动修改代码、自动提交 patch 或自动发布 PR 评论。
- 不以复制 OCR 的全部 prompt 和 tool schema 为目标。

## 5. 总体架构

```text
用户请求与 scope
        |
        v
脚本层：OCR preview + OCR rule
        |
        v
确定性 Review Manifest
  - review set
  - rule groups
  - diff / changed lines
  - context / budget metadata
        |
        v
脚本层：构建 Review Packet
        |
        +--> 小变更 --------------------+
        |                               |
        +--> 大变更 -> LLM Plan --------+
                                        v
                              Codex 主审查 LLM
                              - 读取 packet
                              - 必要时查上下文
                              - 输出候选 findings JSON
                                        |
                                        v
                              脚本确定性校验
                              - 范围 / 行号 / schema
                              - 去重 / 排序 / 定位
                                        |
                           高风险或歧义 finding
                                        v
                              可选 LLM 验证
                                        |
                                        v
                              脚本最终渲染 CR
```

## 6. 职责边界

| 能力 | OCR/脚本层 | LLM/Codex 层 |
| --- | --- | --- |
| scope 解析 | 负责 | 不负责 |
| 变更文件选择 | 负责 | 不负责 |
| rule resolution | 负责 | 不负责 |
| changed-line 计算 | 负责 | 不负责 |
| packet 分组与 token 预算 | 负责 | 不负责 |
| 风险点和业务语义分析 | 提供必要上下文 | 负责 |
| 跨文件推理 | 提供 group 内文件 | 负责 |
| finding 生成 | 校验和整理 | 负责 |
| 代码定位 | 验证、修复或拒绝 | 提供候选 anchor |
| 去重、排序、格式化 | 负责 | 不负责 |
| 复杂 finding 的二次判断 | 按策略调用 | 负责 |
| 最终输出 | 负责 | 不直接写入 |

原则：LLM 负责“代码是否存在问题以及为什么”，脚本负责“这个 finding 是否属于本次任务、是否可定位以及如何稳定输出”。

## 7. Review Manifest

### 7.1 来源与约束

脚本继续调用：

```text
ocr delegate preview --format json
ocr delegate rule --format json
```

全文件 scan 使用 `ocr scan --preview`，仍然只使用 `will_review == true` 的文件。现有 `prepare_review.py` 的以下行为必须保留：

- OCR 选集为空时立即结束；
- OCR JSON 非法、命令失败或 rule 覆盖不完整时停止；
- rule command 按参数长度 batching；
- selected paths 必须被 rule groups 恰好覆盖一次。

### 7.2 建议扩展字段

现有 manifest 是兼容输入；后续可增加 `review_packets` 或由脚本内存构建，不强制把大 diff 持久化到 manifest。

```json
{
  "schema_version": "2",
  "source": "ocr-delegate",
  "repository": "/repo",
  "scope": {
    "mode": "workspace|commit|range|scan",
    "commit": "...",
    "from": "...",
    "to": "..."
  },
  "selection": {
    "reviewable_files": [
      {"path": "internal/a.go", "status": "modified", "insertions": 12, "deletions": 3}
    ]
  },
  "rule_groups": [
    {
      "group_id": 1,
      "files": ["internal/a.go"],
      "rule": "..."
    }
  ]
}
```

`schema_version` 升级时必须保留旧 manifest 的读取兼容或给出明确错误，不得静默按空选集继续审查。

## 8. Review Packet

每个 packet 是一个独立的 LLM 审查任务，最小包含：

```text
<review_packet>
  <scope>workspace / commit / range</scope>
  <review_files>完整文件列表及状态</review_files>
  <file path="...">
    <diff>统一 diff</diff>
    <changed_lines>新文件行号集合或区间</changed_lines>
  </file>
  <rules for="...">适用规则</rules>
  <requirement_background>可选需求背景</requirement_background>
  <plan_guidance>可选结构化计划</plan_guidance>
</review_packet>
```

构建 packet 的脚本负责：

- 使用 Git 获取正确 scope 的 diff；
- 给每个文件建立新文件行号到 diff 行的映射；
- 保留 group 内完整 diff；
- 只以文件列表形式提供 group 外变更，不把无关 diff 全量塞入 prompt；
- 按 token 上限拆分超大 group；
- 对未跟踪文件读取内容并将可评论范围标记为新增行；
- 以稳定路径顺序输出，避免 prompt cache 和结果顺序漂移。

## 9. LLM 调用设计

### 9.1 Plan（可选）

触发条件由脚本决定，例如：

- 单文件 changed lines 超过阈值；
- group 总 changed lines 超过阈值；
- group 文件数超过阈值；
- packet 被拆分或涉及多个相互依赖文件。

Plan prompt 只允许输出 JSON，不允许调用工具或提交评论：

```json
{
  "summary": "...",
  "checkpoints": [
    {
      "focus": "error handling",
      "lines": "42-67",
      "why": "..."
    }
  ]
}
```

最多 5 个高信号 checkpoint，优先正确性、安全、并发、数据一致性和错误处理。

Plan 解析失败时，脚本记录 warning 并继续主审查，但不能把未经解析的自然语言直接当作计划注入。

### 9.2 主审查

主审查 LLM 的输入是一个 packet，输出只能是 finding JSON。建议 prompt 明确以下约束：

- 只评论 packet 中的选中文件；
- 只针对新增或修改代码；
- 删除代码和 unchanged context 只能作为理解依据；
- 组外文件只能作为背景，不能成为评论目标；
- 每个 finding 必须有可复现的事实、影响和修复方向；
- 不报告纯风格建议，除非规则明确要求；
- 无法确认时使用 context tool，不凭假设下结论；
- 完成后输出 JSON，不输出 Markdown 或解释性前缀。

建议 finding schema：

```json
{
  "findings": [
    {
      "path": "internal/a.go",
      "anchor": {"start_line": 42, "end_line": 45},
      "severity": "critical|high|medium|low",
      "category": "bug|security|performance|concurrency|data_integrity|maintainability|test|other",
      "claim": "问题是什么",
      "evidence": "仓库证据和推理链",
      "impact": "会造成什么影响",
      "fix": "建议如何修复",
      "confidence": "high|medium|low"
    }
  ]
}
```

工具调用可以继续由 Codex 使用，但工具结果只用于形成最终 JSON，不直接写入评论存储。主审查可以按 group 并行，单 group 采用有限 round；不默认复制 OCR 的 100 次 tool request 和独立 grace round。

### 9.3 选择性验证

不是每个 finding 都调用二次 LLM。进入验证队列的条件：

- `critical` 或 `high`；
- `confidence != high`；
- 跨文件推理；
- 多条 finding 可能是同一根因；
- 脚本定位修复失败但 finding 内容可能仍然有效。

验证 prompt 使用最小证据集：候选 finding、对应 diff、必要上下文、规则。验证结果建议为：

```json
{
  "decision": "keep|revise|uncertain",
  "reason": "...",
  "revised_finding": null
}
```

规则：

- `uncertain` 必须保留，不得静默删除；
- 只有确定性校验或明确证据矛盾才能丢弃；
- 内存安全、并发、兼容性、数据一致性和未使用参数等保护类别默认保留；
- `revise` 只能修改表达和定位，不能改变原 finding 的事实范围而不留痕。

## 10. 脚本确定性校验

脚本在接受 LLM finding 前按以下顺序处理：

1. JSON schema 校验：字段类型、枚举、必填字段和最大长度。
2. 范围校验：`path` 必须属于 `selection.reviewable_files`。
3. changed-line 校验：anchor 必须落在该文件的新增/修改行范围内。
4. 内容校验：anchor 或代码片段必须能在当前 diff 中匹配；不匹配时尝试确定性 relocation。
5. 规则校验：finding 必须归属于该文件的 rule group。
6. 空值和噪声校验：空 claim、空 fix、纯风格建议按策略拒绝或标记。
7. 去重：相同 path、重叠 anchor、相同根因的 finding 合并；不能仅因为相同模块就合并。
8. 排序：按严重性、路径、起始行稳定排序。
9. 渲染：生成最终 `path:line` 评论、建议和摘要。

脚本校验失败的 finding 不得直接进入最终输出。可以将结构化错误反馈给主 LLM 做一次修正，但修正仍需完整重跑上述校验。

## 11. 失败处理与覆盖保证

### 11.1 输入失败

- OCR 不存在：报告可执行文件路径问题，不自动替换成未经用户同意的普通 Git 审查。
- OCR 命令失败、超时或 JSON 非法：停止，不在不完整选集上继续。
- rule group 覆盖不完整、重复或包含未选中文件：停止。

### 11.2 LLM 失败

- Plan 失败：记录 warning，主审查不带 plan 继续。
- 主审查失败：该 group 标记失败，并在最终摘要中列出；不得把失败 group 报告为已覆盖。
- 主审查输出 JSON 非法：最多进行有限次数修正；仍失败则记录失败原因。
- 选择性验证失败：保留经过确定性校验的原 finding，并标记未完成验证。

### 11.3 覆盖报告

最终摘要至少包括：

- OCR 选中文件数；
- 实际成功审查文件数；
- 失败、跳过和未验证的 group；
- 产生、合并、拒绝和保留的 finding 数量；
- 是否运行 plan/验证；
- 未运行测试或上下文不足等限制。

## 12. 性能与准确性策略

- 以 rule group 为默认并行单位，避免每个文件一次 LLM 调用。
- 小 group 直接审查，大 group 才 plan。
- packet 只携带当前 group 的完整 diff，其他变更只提供元数据。
- 上下文工具按需调用，不预读整个仓库。
- 对主审查和验证分别设置 token budget。
- 验证只覆盖高风险和不确定 finding。
- 稳定排序和固定 XML/JSON 结构，改善 prompt cache 命中与可复现性。
- 通过脚本完成定位、去重和渲染，减少 LLM 输出 token。

准确性优先级：

1. 不漏审选中文件；
2. 不允许越界评论未选中文件或未变更行；
3. 不删除无法证伪的真实 finding；
4. 对高风险 finding 提供额外验证；
5. 最后才优化风格、摘要和评论数量。

## 13. 实施阶段

### 阶段 1：协议和 packet 基础

- 定义 manifest/review packet schema；
- 在现有 helper 中增加 changed-line 和 group packet 构建；
- 复用现有 JSON 校验、path batching 和 rule coverage 校验；
- 增加 packet fixture 和 schema 测试。

### 阶段 2：结构化主审查

- 更新 SKILL 的主审查指令为 JSON finding 协议；
- 增加 finding schema 校验和错误反馈；
- 保留现有人类可读输出格式，但改由脚本渲染；
- 增加 changed-line、越界路径、非法枚举和空 finding 测试。

### 阶段 3：条件性 Plan

- 增加基于 group churn 和文件数的 plan gate；
- 增加 plan JSON 解析和失败降级；
- 验证小变更不会触发 plan 调用，大变更能够向主审查传入计划。

### 阶段 4：确定性整理与选择性验证

- 增加 finding anchor 校验、定位修复、去重和稳定排序；
- 为 high/critical、低 confidence 和跨文件 finding 增加验证队列；
- 增加 keep/revise/uncertain 结果处理和保护类别测试。

### 阶段 5：性能与可观测性

- 增加 group 级 token、耗时、失败和覆盖指标；
- 比较启用/禁用 plan 与验证的 token、延迟、finding precision；
- 为 prompt packet 和最终输出增加版本字段，支持回溯。

当前实现对应关系：`prepare_review.py --packets` 输出 schema-2 manifest 和按 rule group 的 packet，并通过 `--packet-max-bytes` 对超大 group 或单文件 diff 进行确定性分片；`changed_lines_from_diff`、`validate_manifest`、`normalize_findings`、`validation_queue` 和 `apply_validation_decision` 分别覆盖变更定位、旧版本兼容、finding 确定性清理、选择性验证门控和不确定结果保留。LLM 调用本身仍由宿主 Codex 编排，不由 helper 发起。

## 14. 测试与验收标准

### 14.1 单元测试

- manifest schema 和旧版本兼容；
- rule group 精确覆盖校验；
- changed-line 映射；
- packet 稳定排序；
- finding schema 和枚举；
- 未选中文件、未变更行、重复 finding 的拒绝；
- 去重和 severity 排序；
- plan gate 和验证 gate。

### 14.2 集成测试

- workspace、commit、range 三种 scope；
- 新文件、删除文件、重命名文件、binary 文件、未跟踪文件；
- 多个 rule group 和多文件交叉依赖；
- OCR 空选集、malformed JSON、rule coverage mismatch；
- LLM 超时、非法 JSON、部分 group 失败；
- 高风险 finding 验证失败时仍保留原 finding。

### 14.3 验收标准

- 任意输出 finding 的 path 都在 OCR 选集内；
- 任意输出 finding 的 anchor 都能定位到 changed line；
- OCR 选集中的每个成功 group 都有覆盖记录；
- 输入相同且上下文未变化时，packet 和脚本排序稳定；
- 小变更不会产生 plan 或验证调用；
- 不确定的验证结果不会导致 finding 被静默删除；
- 脚本失败时不会伪造“已完成审查”摘要；
- 与当前 `uv` 验证和测试命令兼容。

## 15. 风险与取舍

### 风险：严格 changed-line 校验丢失真实跨文件问题

处理：评论目标仍必须是选中文件的变更行；跨文件证据可以出现在 `evidence`，但不能把未变更文件作为评论目标。必要时使用最小 changed span。

### 风险：结构化 JSON 降低模型表达质量

处理：把 claim、evidence、impact、fix 分开，给出少量正反例；脚本只校验结构，不重写模型事实。

### 风险：二次验证增加延迟

处理：只验证 high/critical、低 confidence 和跨文件 finding；支持配置关闭，但关闭状态必须出现在摘要。

### 风险：rule group 过细导致上下文不足

处理：默认按 OCR rule group，只有明确的 producer/consumer、接口/实现或同一行为契约才允许显式合并，并记录合并原因。

### 风险：去重误合并不同问题

处理：只有相同根因、相同或相近 anchor 才合并；severity 不一致时保留更高严重性并保留必要的文件级细节。

## 16. 设计决策总结

本方案的核心不是增加更多 LLM，而是让每一次 LLM 调用都只负责不可确定化的工作：理解代码、判断风险、解释影响和提出修复。文件选择、规则、变更行、覆盖、定位、去重和最终输出均由脚本控制。

OCR 提供确定性规划和成熟的任务编排参考；Codex 提供主推理能力；选择性验证只作为高价值候选的保险层。这样可以同时获得：

- 比直接 `ocr review` 更清晰的职责分离；
- 比纯自由格式 Codex CR 更稳定的范围和定位控制；
- 比 OCR 多层 LLM 链路更低的 token 成本和延迟；
- 对高风险 finding 更可解释、更不容易被误删的质量控制。
