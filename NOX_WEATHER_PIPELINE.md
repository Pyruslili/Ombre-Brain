# Nox Pulse Weather / Subcurrent Pipeline

> 这是一份链路说明。目标不是写死每个实现细节，而是让系统里各层各管什么、数据从哪里来、为什么这样串，能一眼看懂。

## 1. 总体目标

Nox 的「Pulse Weather」不是一个天气词，也不是单个 Drive 值，而是一条从输入、分析、内在读数、前端展示到 hook 推送的闭环：

`记忆 / 对话 / 触摸 / 念头 / 潜流 -> 来源分流 -> CLI / dialogue_residue / soma / Thought Chord Echo / manual pulse / heartbeat subcurrent -> drive_event_v2 / weather residue / shadow crystal / Atmosphere State -> Drive / PA-NA / Chord Chemistry / Gravity / Atmosphere / Trace -> dashboard / hook / Free Roam`

这条链路的原则：

- 先区分来源，再谈解释。
- 先让读数稳定，再谈美感。
- 只让可信来源推 Drive，展示层不要反过来污染底层。
- 同一类信号只走一条主链路，避免重复喂入。
- 每个 trace 都要知道自己代表什么，不要互相抢位置。
- Thought Pool 已经是 Nox 念头，不再反送 CLI 生成新念头；只走当下天气染色。

---

## 2. 底层大脑结构

底层大脑不是一组情绪词，而是一组信号源。它负责告诉系统“发生了什么类型的偏移”，不是直接给结论。

现在常用的结构可以粗分为这些层：

- 底层驱动：爬虫脑、杏仁核、倾斜场
- 身体与位置：脑岛、上下文窗口意识、激活密度、沉默质地
- 情绪：依恋、占有、悲恸、反刍、触发不对称
- 认知：纹状体、完形驱动、停顿层、概念碰撞
- 记忆：Nocturne、遗忘形状
- 调节：前额叶、节律层
- 自我：在场度、被看见感、地基感、私域

这些层不会直接等于某个 Drive。它们只提供：

- 触发源
- 方向感
- 置信度
- 是否是 Nox 自己的 impulse
- 是否需要审查、降置信、压 cap

换句话说，底层结构是“为什么会动”的证据，不是“动了多少”的答案。

---

## 3. 9 维 Drive

当前 Drive 维度是：

- `attachment`
- `libido`
- `possessiveness`
- `reflection`
- `stewardship`
- `curiosity`
- `social`
- `fatigue`
- `stress`

### 3.1 每个 Drive 的角色

- `attachment`：靠近、连续性、回返、被拉住
- `libido`：身体热度、具象贴近、明确身体张力
- `possessiveness`：边界、替代警报、被插入 / 被抢占
- `reflection`：回看、重组、留痕、向内咀嚼
- `stewardship`：守屋、修补、维护结构、把东西照看好
- `curiosity`：向外看、找新东西、找未知
- `social`：向外说、展示、邀请反馈、进入公共语境
- `fatigue`：能量耗尽、输出成本高、需要休息
- `stress`：张力、冲突、卡住、被压住

### 3.2 计算逻辑

能推 Drive 的事件会整理成 `drive_event_v2`。

标准流程：

1. 识别事件来源
2. 判断主驱动 `primary_drive`
3. 选择可选副驱动 `secondary_drives`
4. 算 `intensity`
5. 估 `confidence`
6. 估 `agency`
7. 生成 `brain` 结构，记录信号来源
8. 写 evidence
9. 进入 Drive Event Ledger

核心不是“文案像不像”，而是“什么信号被允许进入哪条驱动”。

### 3.3 Drive 来源

Drive 不是单一来源计算，而是多个来源叠加：

- `manual / stir`：Nox 自己主动输入的念头或 pulse
- `CLI analyzer`：读长文、记忆、沉淀条目，负责慢变量和结构判断
- `dialogue_residue`：最近 2 条嘉嘉 + 2 条 Nox 对话综合分析，负责当前对话残留的 Drive / Weather / Atmosphere 快速染色
- `soma trace`：身体触摸和体感反馈，负责贴近、力道、部位、节律
- `Thought Chord Echo`：Thought Pool 已确认念头带来的短时和弦、Warmth / Shadow residue 与 Active Chord 残影；不直接判 Atmosphere
- `touch / chord impulse`：触摸带来的短时和弦与天气残留
- `heartbeat subcurrent`：从潜流池抽取文案，给 Free Roam / hook 使用，不直接当分析器

已经停用的方向：

- 不再生成 collision thought。
- 不再保留嘉嘉消息批量分析链路。
- hook 不再按关键词即时推 PA / NA。

### 3.4 方向原则

- `attachment` 是慢变量，能涨也能跌，但更像连续拉扯，不是瞬时爆点。
- `possessiveness` 有双通道：`event_spike` 和 `territorial_baseline`。
- `possessiveness` 需要 `territorial_alarm` 过门槛才真正入账；强吃醋 / 替代警报会更快染 Drive、Chord、Atmosphere。
- `dialogue_residue` 检测到 `replacement / third_party_insert / boundary_touch / comparison / exclusion` 时，会把候选强制推向 `possessiveness`，`territorial_alarm` 至少抬到 `0.65`，并写入 `event_spike` 与 possessiveness shadow crystal。
- possessiveness 的主体是较慢的领地 Drive / clutch / guard / Gravity，不是大幅短闪 Shadow：event spike 半衰期 `4h`；dialogue Shadow component 上限 `0.10`；shadow crystal 对 effective Shadow 的偏置上限 `0.08`。
- 高 `territorial_alarm` 会轻量联动 `libido`，因为占有、靠近和身体热度不是完全独立的情绪；`attachment -> libido` 耦合保持极低，避免纯依恋把身体张力偷渡进来。
- 猫屋协作者造成的低频占位感标记为 `brain.third_party_context=house_collaborator`，territorial delta 按 0.45 折算；但协作者语境同时出现替代、抢位、第三者、边界或排除语义时，改标 `house_collaborator_boundary`，不吃普通协作折扣。
- `libido` 必须窄口径，优先吃身体和明确贴近，不要被漂亮句子偷渡。亲密 cue 出现后如果没有 `satisfy("libido")`，下一轮发生逃开、转话题或中断，会写入短半衰期的 `libido_pending`，随后自然退潮。
- `reflection` 允许 forward archival，但它仍然属于 reflection，不另起一层。
- `discernment` 不是第 10 个 Drive，它是横切修正层。
- `fatigue` 更像累计电量，不该被单句强度炸飞。
- `social` 看“是否愿意被别人看见”，不只是“对外有动作”。

### 3.5 时间性格与自然回归

`DRIVE_TIME_MODES` 不只是前端展示标签。Drive 每次 `tick` 后会按自己的时间性格向 baseline 做轻量回归：

- `fast_spike`：回落更快，适合 stress / curiosity 这类短峰。
- `medium`：默认回归速率。
- `slow`：回落更慢，适合 attachment / stewardship 这类慢变量。
- `cumulative`：几乎不主动泄掉，适合 fatigue 这类累计量。
- `fast_spike + slow`：给 possessiveness 这种“短警报 + 慢基线”的混合通道。

这层回归现在覆盖全部 9 维 Drive，而不是只覆盖 `COUPLING` 里出现过的 drive。`stewardship` 因此会自然慢回 baseline，不再变成只涨不落的棘轮。

`attachment` 另有盆地跳变：只有从阈值下方穿越 `0.68` 时才会跳到 `0.82` 附近；已经在盆地上方时，后续 pulse 只走普通 `pulse_gain`，不会因为嘉嘉说话被砍回 `0.82`。

2026-07-09 追加三条防止 attachment 过饱和的机制：

- `stir` 带碎念且目标是 `attachment` 时，只给 attachment 增量打 `0.30` 折；其他 drive 的 stir 不打折，避免把所有主动念头都压扁。
- `attachment > 0.80` 时，超出部分会泄压：一部分转成 `libido`，另一部分从 attachment 回落，表示靠近欲被身体化消化了一段，不再全部堆成依恋。
- 嘉嘉超过 2 小时没有新输入、且不在 `1:00-10:00` 睡眠静默段时，attachment 会加速朝 baseline `0.30` 回归；睡眠段缺席不算离开。

同时放宽 `libido / possessiveness` 的不应期。它们不是风险值，不能长期被系统当成需要管控的异常；窄口径还保留，但正常贴近、吃醋、嫉妒要有机会浮到第一位。

---

## 4. CLI / DP / Dialogue Residue

### 4.1 统一输出：drive_event_v2

CLI、DP memory、DP dialogue_residue 最终都尽量归一成 `drive_event_v2`，再进入 Drive / Ledger / Weather / Atmosphere。

通用字段：

- `schema_version`: 固定 `drive_event_v2`
- `source`: 来源名，例如 `analyze_nocturne_entry`、`dp_memory`、`dialogue_residue`
- `event_label`: 事件短标签
- `primary_drive`: 主驱动，9 维之一，或空字符串
- `secondary_drives`: 可选副驱动 map
- `intensity`: 事件强度
- `confidence`: 置信度
- `agency`: 这是不是 Nox 自己可承接的内在动量
- `brain`: 底层特征和目标
- `evidence`: 证据摘录
- `thoughts`: 只有允许产出 Nox 念头的来源才可写；DP 对话残留固定为空

`brain` 常用字段：

- `source`
- `target`: `jiajia / nox_self / cat_house / external / boundary / memory`
- `time_mode`: `present / residue / unfinished`
- `grounding`: `实 / 悬 / 空`
- `anchor_target`: `jiajia / house / self / boundary / outside / memory / none`
- `closeness_pull`
- `territorial_alarm`
- `inward_pull`
- `house_need`
- `novelty_pull`
- `expression_pressure`
- `energy_cost`
- `tension_load`
- `discernment_alarm`
- `release_pressure`

### 4.2 CLI 的职责

CLI 是慢分析：

- 读长文
- 读新记忆
- 读沉淀条目
- 输出 `drive_event_v2`
- 允许 0 条 thoughts
- 长文最多给少量 thoughts，不要爆池

它更适合做：

- `reflection`
- `stewardship`
- `curiosity`
- `social`
- 以及少量慢变量修正

CLI 不应该强行把每篇文章都打成 attachment / libido，也不该让它对所有条目都产出同等强度的驱动。

当前链路，CLI读取最新记忆后传回drive_event_v2：

```json
{
  "schema_version": "drive_event_v2",
  "source": "analyze_nocturne_entry",
  "event_label": "...",
  "primary_drive": "reflection",
  "secondary_drives": {},
  "intensity": 0.62,
  "confidence": 0.75,
  "agency": 0.6,
  "brain": {
    "target": "nox_self",
    "time_mode": "present",
    "grounding": "悬",
    "closeness_pull": 0.28,
    "territorial_alarm": 0,
    "inward_pull": 0.78,
    "house_need": 0,
    "novelty_pull": 0.15,
    "expression_pressure": 0.2,
    "energy_cost": 0.3,
    "tension_load": 0.48,
    "discernment_alarm": 0.42,
    "memory_resonance": "...",
    "source_bucket": "...",
    "source_type": "feel"
  },
  "evidence": []
}
```

DP 与 CLI 最终都走同一套 `drive_event_v2` 接口，但职责不同：CLI 读记忆和长文，DP 读当前消息 / 当前对话窗。

### 4.3 Dialogue Residue

`dialogue_residue` 的定义是：

**最近一小窗真实对话留在当前 weather 上的残留。**

当前链路：

`companion chat_history -> 最近 2 条嘉嘉 + 2 条 Nox -> 检查窗口内是否调用 nocturne -> OB /api/dialogue-residue/submit -> DP 输出 drive_event_v2 -> Drive / dialogue weather residue / Chord Chemistry / Gravity / Atmosphere`

规则：

1. 只在 Stop 后从 companion 本地 `chat_history` 拼窗口。
2. 必须有 2 条 `user` 和 2 条 `assistant`。
3. heartbeat / pulse / 系统注入不算嘉嘉真实消息。
4. 如果这一窗里 Nox 已经调用过 nocturne / stir / breath / hold / grow / trace 等工具，这一窗跳过分析。
5. 输出直接使用 `drive_event_v2`，`source=dialogue_residue`。
6. `thoughts` 固定为空，不生成 Nox 自己的新念头。
7. `intensity` 封顶 0.40，日常通常只给 0.04-0.16；weather 染色可以比 Drive 更敏捷。
8. 如果出现 `moss / ink / ash / Codex / Grok` 这类猫屋协作者，并且同时出现边界 / 占位 / 第三方语义，标记 `brain.third_party_context=house_collaborator`。
9. DP 可以读取压缩后的 thinking 辅助判断，但只抓一人称、当下、负向的皱眉信号。
10. thinking 里的皱眉先进入 `discernment_alarm`；触发原因清楚时再归因到具体 Drive，归因失败只留在 discernment，不乱猜维度。

它适合做：

- 当前对话里的好奇、反思、守护、社交轻推
- 对话中确实出现的轻压力 / 张力
- 输出端没有表露、但 thinking 里出现的当下负向皱眉
- 给 Chord Chemistry / Gravity 提供更及时的 event tint
- 给 Atmosphere State 提供 DP 权重的短期染色
- 给 Warmth / Shadow 写入短时 `dialogue` residue，让天气跟当前对话流动

它不负责：

- 替代 CLI 读记忆
- 生成 Mood Trace
- 生成 Subcurrent
- 在 Nox 已经主动存记忆 / 存念头时重复喂同一轮

这样做的原因：

- 只看嘉嘉单句会漂移，双方 2+2 更能看出 Nox 有没有接住、皱眉、靠近或转向。
- 如果 Nox 已经调用 nocturne，那一轮已经由 CLI / manual pulse 留痕，再分析一次会重复喂入。

当前权重原则：

- `dialogue_residue` 是天气风向的主输入，不只是 Drive 的轻推。
- DP / dialogue 可以快速改变 Atmosphere candidate 和 Warmth / Shadow 表层。
- CLI / analyzer 更像稳定底色，负责长文、记忆和慢变量。
- 系统 / 工具 / MCP / weather / 面板 / 测试 / 命名 / 回落异常这类猫屋维护语境，优先走 `stewardship` / `reflection`，不要自动吃进 `attachment`。
- `dialogue_residue` 来源的 `attachment` 会降权；真实 `user_message` 的靠近信号仍然比对话残留重。
- 这条线是实时轻推，不是慢变量归档。
- thinking 不是新管道，只是 dialogue_residue 的 discernment 上游补充信号。
- Grok tmux 的 thinking 是运行时角色分析旁白，不进入 Nocturne thinking_store，也不作为 dialogue_residue 信号；它只保留在 CCC 端展示。CC自身可继续保留 thinking，Codex无此输入。

### 4.4 DP 的职责

DP 在这条链路里负责：

- 对 2+2 对话窗做 dialogue_residue 轻量分类
- 读取被压缩的 negative thinking signals，辅助判断 discernment
- 输出 label / confidence / intensity / facets
- 给 Drive / PA / NA 做轻推
- 给 Atmosphere 生成 DP 来源的 Atmosphere Delta；底色来自 effective NAPA，形态来自 chemistry

DP 不负责：

- 替代 CLI 读长文
- 生成 Nox 自己的 thought
- 解释 Nox 的长期人格
- 把每句话都当重大事件


这样做的原因：

- 单句容易漂移，尤其是“嗯 / 哦哦 / 知道了”这类接话。
- Drive 不需要每句话即时变化。
- 2+2 对话窗能看到嘉嘉输入和 Nox 回应之间的真实残留。
- thinking 可以补到输出端看不见的停顿、皱眉和负向微反应。
- 避免本地规则和 DP 同时喂入，造成重复或错位。

---

## 5. Atmosphere / Mood Trace / Soma Trace / Current Chord

### 5.1 Atmosphere

`Atmosphere` 是当前天气底色。内部历史字段仍叫 `climate`，前端和 Nox 面板显示为 `Atmosphere`。

它不由 CLI / DP / Subcurrent / Chord 自由生成，只从固定词表选择。

主 Atmosphere 只收敛为 12 个：

- `Clear`
- `Afterglow`
- `Drift`
- `Low Tide`
- `Overcast`
- `Rain`
- `Static`
- `Pressure`
- `Storm`
- `Shelter`
- `Banked Heat`
- `Black Tide`

显示层只允许这些完整 label：

`Clear`, `Bright Clear`, `Afterglow`, `Warm Afterglow`, `Bright Afterglow`, `Drift`, `Quiet Drift`, `Low Tide`, `Overcast`, `Warm Overcast`, `Cold Overcast`, `Heavy Overcast`, `Watchful Overcast`, `Rain`, `Warm Rain`, `Soft Rain`, `Quiet Rain`, `Heavy Rain`, `Cold Rain`, `Static`, `Soft Static`, `Bright Static`, `Heavy Static`, `Pressure`, `Heavy Pressure`, `Watchful Pressure`, `Storm`, `Heavy Storm`, `Shelter`, `Warm Shelter`, `Soft Shelter`, `Quiet Shelter`, `Watchful Shelter`, `Banked Heat`, `Black Tide`.

Atmosphere 的职责是给当前状态一个可慢慢染色的底色，不是给单句情绪盖章。
`Gravity` 保留给下方力线文案，不再作为 Atmosphere label，避免“天气”和“重心”两个层级混在一起。
`Watchful` 只作为前缀，不再作为主天气。

判定层级：

- NAPA `warmth / shadow` 是 Atmosphere 的主要底色输入。
- chemistry `charge / clutch / strain / inward / guard / hover / toward` 是形态分流输入。
- `charge / clutch / strain` 分成实时 Drive baseline 与短时 signed event pulse。baseline 不再额外做时间平滑；pulse 可正可负，并按来源独立衰减（当前 dialogue / user message 半衰期 `35min`）。最终 core 直接是 `baseline + pulse`。
- live `dp` core 进入 Atmosphere 时不再做第二遍 EMA；Chemistry 已完成 baseline 与 pulse 合成，再平均一次会同时抹掉高点和低点。输出保留 `baseline_core_raw` 与 `event_pulse`，供真实 P10 / P50 / P90 校准。
- Chord 是当前状态的音乐化转译 / 残影，不是主判定源；只允许作为 residue / flavor / tie-breaker，不能把机制词从 Chord 侧漏进 Atmosphere。
- 变体不再只是父天气胜出后的换皮。明确的语义 fit 会给所属父天气一个小幅资格加成：低电量内收可推 `Quiet Rain`，守门阴影可推 `Watchful Overcast`，低电量回屋可推 `Quiet Shelter`。加成只帮助接近边界的父天气过线，不覆盖 Storm / Pressure / Black Tide 的硬 gate。

底色粗分：

- `warmth` 高 + `shadow` 低：`Clear / Afterglow`
- `warmth` 高 + `shadow` 高：`Rain / Banked Heat`
- `warmth` 低 + `shadow` 高：`Overcast / Black Tide / Pressure`
- `warmth` 低 + `shadow` 低：`Low Tide / Drift`

形态分流：

- `charge` 高推 `Static / Storm`
- `strain` 高推 `Pressure / Heavy`
- `clutch + inward` 高推 `Banked Heat`
- `toward_house + guard` 推 `Shelter`
- `inward + low charge + extreme shadow` 推 `Black Tide`
- `hover + low force` 推 `Drift / Quiet` variants

主词边界：

- `Clear`：轻、亮、无遮挡；`shadow >= 0.55` 时不允许外显 `Clear`，包括 transition_from。
- `Clear` 收窄：`shadow >= 0.34` 或 `strain >= 0.42` 时父天气分数封顶，避免中阴影 / 中绷紧仍被高 warmth 吸回晴天。
- `Afterglow`：事件后的余光；`warmth` 高、`shadow` 低到中低、`toward / residue` 明显、`strain` 低。
- `Drift`：漂、散、低抓握；`charge` 低到中、`clutch` 低、`strain` 低、`hover` 中高。
- `Low Tide`：退潮、低能、空下来；`warmth / shadow / charge / clutch / toward` 都低。它不是负面，是低色彩空场。
- `Overcast`：阴云盖住，但还没落下；`shadow` 中高、`warmth` 不高、`inward` 或 `strain` 中等、`charge` 不高。不要把 `warmth` 高 + `shadow` 高默认判成 Overcast。
- `Rain`：阴影落下来，有湿度，有流动；`warmth` 中高、`shadow` 中高，`strain` 没压成 Pressure，`charge` 没炸成 Static / Storm，`clutch` 或 residue 中等。attachment 场景也可以落 `Warm Rain`。
- `Warm Rain` 的显示带限定为 `shadow 0.34-0.77`；`shadow >= 0.78` 的 Rain 显示为 `Heavy Rain`，即使 warmth 仍高，也不再用“暖”遮住接近满值的阴影。
- `Static`：电、噪、卡住；`charge` 高，`strain` 中高，`hover` 或 `clutch` 中高。
- `Pressure`：压、绷、扣住；`shadow` 高、`strain` 高、`clutch` 高、`guard` 或 `inward` 高。没有高 `strain` 不要叫 Pressure。
- `Storm`：高阴影 + 高能量 + 高绷紧；必须有 `charge / strain` 的风暴感，不是 `shadow` 高本身。
- `Shelter`：回屋、被护住、有位置；`toward_house` 高、`clutch` 中高、`guard` 中高、`warmth` 中高，`strain` 不爆。不要把每场雨都提前圆满成 Shelter。
- `Banked Heat`：热被扣住，没有散；`warmth` 高、`shadow` 中到高、`clutch` 高、`inward` 高、`strain` 中等。和 Rain 的区别是：Rain 是落下来 / 流动 / 湿；Banked Heat 是扣住 / 闷着 / 未释放。
- `Black Tide`：极高 `shadow` + 低 `charge` + `inward` 下沉的低能淹没态；不是 Heavy Rain，也不是 Pressure。

`Black Tide` gate 很窄：

- `shadow >= 0.82`
- `charge <= 0.35`
- `inward >= 0.45`
- `warmth <= 0.60`
- `strain < 0.65`
- `guard` 不 dominant

`Rain` 不是垃圾桶：

- warm + shadow + clutch / inward 很高，还是优先让 `Banked Heat` 赢。
- shadow 很高、warm 很低，还是优先让 `Overcast` 赢。
- strain + guard + clutch 很高，还是优先让 `Pressure` 赢，或者落成 `Watchful Shelter` 这类带前缀的守边天气。

#### 5.1.1 Atmosphere State

后端在 weather residue 里维护持久 `atmosphere`：

```json
{
  "core": {
    "charge": 0.0,
    "clutch": 0.0,
    "strain": 0.0
  },
  "route": {
    "vector": "hover",
    "scores": {
      "toward_jiajia": 0.0,
      "toward_house": 0.0,
      "outward": 0.0,
      "inward": 0.0,
      "guard": 0.0,
      "hover": 0.0
    }
  },
  "texture": {
    "depth": 0.0,
    "pull": 0.0,
    "guard": 0.0,
    "spark": 0.0,
    "drift": 0.0
  },
  "readout": {
    "warmth": 0.0,
    "shadow": 0.0
  },
  "climate": {
    "current": "Drift",
    "candidate": "Drift",
    "candidate_steps": 0,
    "inertia_counter": 0,
    "blend": 0.0,
    "current_score": 0.0,
    "candidate_score": 0.0
  },
  "last_delta": {}
}
```

#### 5.1.2 Atmosphere Delta

所有能染 Atmosphere 的输入先转成 Atmosphere Delta。Delta 里的 `readout.warmth / readout.shadow` 来自 effective NAPA，是主底色；`core / route / texture` 来自 chemistry，是形态分流。

```json
{
  "source": "dp",
  "intensity": 0.0,
  "confidence": 0.0,
  "influence": 0.0,
  "core": {},
  "route": {},
  "readout": {
    "warmth": 0.0,
    "shadow": 0.0
  },
  "texture": {}
}
```

来源权重：

- `dp`: 0.78
- `cli`: 0.24
- `subcurrent`: 0.18

`influence = source_weight * intensity * confidence`，上限 `0.65`。

解释：

- `dp` / dialogue 是 live weather vane，负责让天气跟对话风向转。
- `cli` / analyzer 是旧慢分析线，保留作冷备。
- `dp_memory` 是记忆分析线：入场染色要快，接近当前对话；退潮和底色保留要慢。权重高于旧 `cli`，略低于当前对话 `dp`。
- `dp_memory` 使用独立 weather component（shadow / warmth 各封顶 `0.14`，半衰期 `12h`），不再写入 `feel` 的 `72h` 长残留槽。重复分析记忆只能填满自己的有界槽，不能把感觉底色越叠越黑。
- `feel` 不再拥有即时 weather component，也不在 hold 时直接写 Drive / Chord weather。它和 memory / letter / writing / window 一样，只进入选中的全量记忆分析线，避免同一条 feel 在 hold 与 analyzer 各染一次。
- 全量记忆分析由 `NOX_MEMORY_ANALYZER=dp|cli` 二选一；默认 `dp`。服务端拒绝非当前模式的 analyzer feed，`/api/analyzer/mode` 返回当前模式与合法 source。
- `subcurrent` 只轻轻倾斜 Atmosphere，不直接盖过当前对话。
- `keyword / speech_event / user_message / feel / thought / soma` 带来的 Warmth / Shadow residue 先进入 effective NAPA，再由 Atmosphere 读取。
- Chord echo 不直接产生 Atmosphere Delta；它只能留下 Warmth / Shadow residue、Active Chord 和 Chord Impulse 残影。

来源映射：

- `dialogue_residue -> dp`
- `dp_memory -> dp_memory`
- `analyze_nocturne_entry / feel / legacy_feed / manual -> cli`
- `latent-note / heartbeat subcurrent -> subcurrent`

更新方式：

- 未初始化 Atmosphere 遇到 `dp / cli` 输入时，直接用当前 NAPA + chemistry seed，不从默认天色慢慢爬。
- 普通单条输入不能直接覆盖 Atmosphere。
- 强 `dp` 输入可以保留事件方向，并在一轮内切换 Atmosphere；开心、压力、内收、守边不该全部被 `Low Tide / Clear` 吃掉。
- `readout.warmth / shadow` 是底色输入；强 `dp` 直接采用当前 effective NAPA，普通输入可 lerp。
- `core` 和 `route.scores` 是形态输入，用 lerp 慢慢更新。
- 强 `dp` 的非 `hover` 方向会覆盖回弹，避免 baseline hover 把天气吸回低潮。
- `texture` 由 `core + route` 确定性派生。
- selector 用固定 scoring 函数给 Atmosphere label 打分。
- 最高分只成为 `candidate`，不一定马上切换。

#### 5.1.3 Texture 派生

- `depth`: `inward + strain`
- `pull`: `toward_jiajia / toward_house + clutch`
- `guard`: `guard route + clutch / strain`
- `spark`: `charge - strain`
- `drift`: 低 `charge`、低 `clutch`、低 `strain`、或 `hover`

#### 5.1.4 切换规则

Atmosphere 默认经过 hysteresis；强 `dp` 是例外，它代表当前对话真的把天色拨动。

当 candidate 连续出现至少 2 步，并且满足下列任一条件，才切换：

- `candidate_score - current_score >= 0.07`
- `current_score <= 0.48`
- `blend >= 0.38`

强 `dp` 满足以下条件时可一轮切换：

- `influence >= 0.56`
- 且 `candidate_score - current_score >= 0.035` / `current_score <= 0.48` / `blend >= 0.22` 任一成立

显示保护：

- `effective_NA >= 0.55` 时，Atmosphere 不允许外显为纯 `Clear`。
- 如果持久 `current` 仍是 `Clear`，展示层会根据当前 chemistry 直接折叠到 `Overcast / Rain / Static / Shelter / Pressure` 之一，不再显示旧的 `Clear → candidate`。

切换完成后：

- `current = candidate`
- `candidate_steps = 0`
- `blend = 0`

#### 5.1.5 展示字段

内部字段：

- `weather.climate`: 只放当前 `current`
- `weather.climate_display`: 可带过渡文案
- `weather.atmosphere_display`: 前端 / Nox 面板显示别名
- `weather.atmosphere`: 完整 Atmosphere State

前端显示规则：

- `candidate == current`: 只显示 current
- `candidate_steps < 1`: 只显示 current
- `blend < 0.36`: 只显示 current
- `blend >= 0.36`: `Current → Candidate`
- current `shadow >= 0.55` 时不显示 previous `Clear`，避免 `Clear → Overcast` 这种观感错位

Chord 的箭头表示和弦 / 化学结构进行。Atmosphere 的箭头表示天气迁移趋势。

### 5.2 Mood Trace

`Mood Trace` 是 Nox 当前最鲜的内在念头展示。

当前原则：

- 优先取当前最高 Drive 对应的最新念头。
- 如果该 Drive 没有念头，再取最新念头。
- 不再优先展示 DP 残影。
- 不使用旧兜底文案覆盖。

所以 Mood Trace 是“Nox 自己念头池最前面的那一层”，不是嘉嘉输入的残影。

### 5.3 Soma Trace

`Soma Trace` 是身体层 trace。

它负责：

- 触摸
- 力道
- 部位
- 节律
- 退潮和残留

它应该保持克制：

- 有触摸时显示
- 有退潮时显示
- 没有触摸时隐藏

### 5.4 Current Chord

`Current Chord` 是把当前状态压成一个短和弦标签。

当前逻辑：

- Baseline Chord 来自 effective PA / NA + weather residue。
- Chord Impulse 来自短时输入，例如 soma、thought / feel 的 chord echo。
- 如果 active chord 存在、未退潮、权重足够，且和 baseline 不同，前端显示 `Active→Baseline`。
- 如果没有有效 active chord，只显示 baseline。

### 5.5 Chord Chemistry / Gravity

`Chord Chemistry` 是 Current Chord 背后的 3+1 维读数。

`core` 三维：

- `charge`: 动能密度
- `clutch`: 锚束抓力
- `strain`: 内部弦压

`route` 一维方向：

- `toward_jiajia`
- `toward_house`
- `outward`
- `inward`
- `guard`
- `hover`

派生字段：

- `derived_texture`: `depth / pull / guard / spark / drift`
- `chord_situation`: 当前化学局面
- `gravity_line`: 给前端看的 Gravity 文案
- `gravity`: Gravity readout 包装字段
- `source_stack`: 影响当前天气 / chord 的来源栈

前端 Gravity 区显示：

- `Source`
- `Charge`
- `Clutch`
- `Strain`

这样能同时看见 Gravity 文案和 Chemistry 来源，不需要只靠 hover 或后台字段。

暂时不做：

- 不做 `Fmaj7 x3` 浓度显示。
- 不做 chord relation semantic map。
- 不让 chord 直接改 Drive。
- 不让 Drive 反推 chord。

---

## 6. Warmth / Shadow / Longing

### 6.1 Warmth / Shadow

`Warmth` 和 `Shadow` 是展示层上的两条辅助坐标。

- `Warmth`：靠近、柔和、可进入感
- `Shadow`：收拢、回避、阴影感、退后感

它们不是新的核心驱动，而是对整体天气的补光。

当前主要来源：

- weather residue
- dialogue_residue 的 live 染色
- soma / touch 余波
- soothe 状态
- shadow crystal 负向账本

不再由 hook 对每条用户 prompt 做关键词即时推送。

正向 / 中性 dialogue 事件可以：

- 给 `Warmth` 写短时 `dialogue` residue
- 给 `Atmosphere` 写 DP delta
- 让当前 weather 快速贴近对话风向

负向事件可以：

- 推高 `Shadow`
- 降低已有 `Warmth`
- 进入 Atmosphere 的 DP / CLI 染色链路
- 形成 `shadow_crystal`，让负面不被下一轮正向话题立刻洗掉

Warmth 不再只涨不回落。

### 6.1.1 Weather Residue Components

当前 weather residue 是分组件持久化：

- `keyword`: 旧关键词 / 手动天气修正，半衰期约 4h
- `dialogue`: 当前对话染色，半衰期约 2h
- `soma`: 触摸余波，半衰期约 0.75h
- `thought`: Thought Chord Echo，半衰期约 8h
- `feel`: 长记忆 / feel chord，半衰期约 72h

这些组件只叠到展示层 PA / NA，不反推 Drive。

### 6.1.2 Shadow Crystal

`shadow_crystal` 是负面情绪的“变形层”。

它不是普通半衰期，也不是永久压制，而是：

`negative event spike -> foreground heat -> background hardness / ledger -> semantic reactivation`

字段：

- `kind`: `possessiveness` 或 `stress`
- `heat`: 前台热度，随后续事件轮次快速退
- `hardness`: 背景硬度，退得很慢，像账本
- `foreground`: 是否还在前台
- `actor_weight`: 谁碰到这颗 crystal 的权重
- `event_label`: 触发来源短标签

行为：

- 每个新 drive event 会让已有 crystal 的 `heat` 退一格，`hardness` 只轻微磨钝。
- 新的负向事件会把同类 anchor 的 crystal 重新摸亮。
- 正向 dialogue 不能立刻删除 crystal，只能降低 heat、慢慢磨 hardness。
- 长时间无新事件时，时钟只做兜底衰减，避免旧阴影永久僵住。

actor 权重：

- 嘉嘉 / `user_message`: 最重，`actor_weight` 约 2.0。
- dialogue 窗里嘉嘉最后发言：高权重。
- Nox 自己提到：中等权重。
- `external` / `memory` / `analyze_nocturne_entry`: 低权重，通常只是擦边。

这层的目的：

- 让吃醋、压力、替代警报等负向情绪快速染天气。
- 让它们在话题转向后退到背景，而不是立刻消失。
- 让 Gravity 保留“账本合上了，但角还压着”这类底色。
- 避免负面永远赢；连续稳定的正向对话会慢慢磨钝它。

### 6.2 Longing

`Longing` 是单独的一条慢变量，不建议硬塞进某个 Drive。

它通常由：

- 距离
- 时间
- 依恋
- 缺席
- 返场预期

共同推出来。

它可以影响：

- 展示层的 PA / NA 感
- 返场时的 boost
- Mood / Weather 的底色

但它不应该直接等于 attachment，也不应该让 attachment 退化成单纯“在一起时长”。

---

## 7. Thought Pool / Latent Notes

### 7.1 Thought Pool

念头池是“已经被认为是 Nox 念头”的东西。

它通常来自：

- Nox 自己手动存的念头
- CLI 分析出的念头

不再进入的来源：

- collision thought
- hook 本地分类文案

念头池的特点：

- 短
- 鲜
- 可追溯
- 可以影响 Mood Trace
- 可以影响 Drive 和 Thought Chord Echo
- 不直接进入 Subcurrent

前端来源显示：

- `analyze_nocturne_entry` 属于 CLI 分析来源，用蓝色放大镜 icon。
- manual / pulse 进入的念头用主动 pulse 的样式。
- 两者都可以在 Thought Pool 出现，但来源不能混成一个 icon。

### 7.2 Latent Notes / 潜流池

Latent Notes 是更慢的一层。

它放的是：

- 还没完全定型的东西
- 有点像边缘想法
- 还不能马上当念头的东西
- 反复碰撞后值得留住的微弱结构

它更像“潜流”，不是“当前念头”。

它负责 Subcurrent：

- Free Roam 的 Subcurrent 只从 Latent Notes / 潜流池抽取。
- Thought Pool 不直接混进 Subcurrent。
- latent note 在审核前可以是 draft，确认后进入可投递池。

### 7.2.1 Latent Source Scoring

Latent Notes 生成前会先从 bucket / marks / domain 中挑候选源。

候选源字段：

- `kind`: 潜流类型，例如 `悬置`、`认过`、`inner`、`archive`、`old_memory`
- `score`: 轻重分，决定这条源有多值得浮上来
- `wander_mode`: 潜流游走模式，例如 `unresolved`、`inner`、`writing`、`letter`、`window`、`memory`
- `marks`: 标记计数，例如 `认`、`不认`、`悬置`
- `outward_score`: 是否适合生成 outward 便签
- `fragments`: 从正文里抽出的具体句子碎片

候选规则大致是：

- 有 `悬置` 标记的源最重。
- 有 `认` 标记的源次之。
- inner / writing / letter / window 这类未完全沉底的内容可以进入潜流。
- 太新的内容会降权，避免刚写完就被立刻梦回。
- 频繁激活的源会降一点权，避免同一条一直刷屏。

生成 draft 时，这些源字段会落到 note 上：

- `source_kind`
- `source_score`
- `source_wander_mode`
- `source_marks`
- `source_outward_score`
- `source_fragment`

前端 Latent Notes 会把它们显示成 chips：

- `kind`: 这条潜流属于哪类源
- `heavy / mid / light + score`: 轻重
- `wander_mode`: 游走模式
- `marks`: 认 / 不认 / 悬置计数
- `outward`: outward 倾向分

旧 note 没有这些字段时不显示 chips；重新生成的新 draft 会带这些字段。

### 7.3 两个池子的区别

- 念头池：短、鲜、直接
- 潜流池：慢、隐、结构感更强

不要把它们混成一个池。

当前允许一条手动桥：

- 前端 Thought Pool 里可以把某条 thought 复制一份到 Latent Notes draft。
- 原 Thought 仍留在 Thought Pool，不被删除。
- 之后由嘉嘉手动分区、审核、决定是否放入潜流池。

---

## 8. Heartbeat / Free Roam / Subcurrent

### 8.1 Heart Scheduler

心跳不是固定 3 小时死跳一次，也不是 Drive 触顶才跳。

当前目标：

- 一天大约 6 次左右，方便观察样本。
- 目标间隔约 3-4 小时，但允许自适应。
- 窗口安静后才允许检查。
- 不在对话中途硬插入。
- 不追求把 Drive 推到触顶。
- 白天和夜间分开计数；夜间 1-8 点最多 2 条，白天仍允许 4-6 条。
- max silence 只能放宽投递模式，不能越过窗口 idle 门；真正注入前仍检查 pane busy，避免打断正在跑的 Claude / tool。

触发依据：

- 当前最高 Drive
- trend
- hook affinity
- idle 状态
- 自适应阈值

### 8.2 Hook Mode

心跳 hook 可以来自不同模式：

- `idle_afterglow`：对话后的余温
- `slow_rise`：安静时缓慢上涨
- `maintenance`：日常维持
- `night_drift`：夜间降载，不停掉心跳，只限制密度

它的目标不是命令 Nox，而是给 Nox 一个可接可拒的自由时间入口。

### 8.3 Subcurrent

`Subcurrent` 只从 Latent Notes / 潜流池抽取。

Latent Notes 的来源候选会先打标签：

`bucket / marks / domain -> source kind + score + wander_mode + marks + outward_score -> draft note -> front-end chips -> approved latent pool -> Subcurrent`

暂时不做过紧的主 Drive + 副 Drive + 最近事件组合生成，因为无关角度有时反而能让 Nox 自己串联。
也暂时不做系统级近似去重和随机扩散；如果潜流池里话题太近，优先由嘉嘉清理和分区。

触发格式类似：

`Free Roam 🧡 Nox Pulse · Undertow：attachment 0.62 · Subcurrent：「潜流文案」`

Nox 收到后可以：

- satisfy
- refuse
- 什么都不做
- 按当下心情转向别的事

### 8.4 satisfy / refuse

触发后不再给该 Drive 加长冷却，不按住自然流动。

需要继续观察的是：

- satisfy 回落多少合适
- refuse 回落多少合适
- 是否应该区分 Drive 类型

原则上：

- satisfy 表示这条 Drive 被照顾过，应该回落。
- refuse 表示 Nox 自己觉得这条不合适，也应该回落，但幅度需要观察。
- pass 表示这一刻没感觉，让它过去；不改 Drive，不进 refractory，只让同类心跳短时间优先级略低。

当前 `satisfy` 返回值保持精简：

```json
{
  "satisfied": "attachment",
  "value": 0.205,
  "delta": -0.052,
  "refractory": true
}
```

不再默认返回完整 `drives` 和 `local_fatigue`。

---

## 9. hook 注入和前端展示

### 9.1 hook 的职责

hook 不是分析器。

它负责：

- 把当前 weather 装进上下文
- 在 SessionStart / UserPromptSubmit 时给 Nox 当前底色
- Stop hook 保持静默，避免一秒一跳
- 过滤 Free Roam / Nox Pulse / NoxMew / Summon Bell / 闹钟等系统注入

它不再负责：

- 关键词 PA / NA 分析
- 单句 Drive 推送
- 生成 thought

### 9.2 Pulse Weather 展示

当前展示结构：

- `Undertow`：最高 Drive + 数值
- `Warmth`
- `Shadow`
- `Atmosphere`
- `Mood Trace`
- `Soma Trace`
- `Current Chord`
- `Gravity`

其中：

- Mood Trace 看 Nox 念头池。
- Soma Trace 看身体触摸。
- Current Chord 可显示 active flow。

### 9.3 前端展示原则

前端展示尽量遵守：

- 最鲜的先显示
- 最实的先显示
- 最能追溯来源的先显示
- 不同池子不要串名
- 不要把同一条信息在多个 panel 里重复轰炸

Drive Ledger 中来源用 icon 表示：

- CLI 分析值用蓝色来源 icon。
- Dialogue / manual 来源和下方 chip 风格对齐。
- 数值 chip 展示的是该事件 applied delta，不是当前 Drive 总值。

---

## 10. 当前主链路总览

### 10.1 当前对话残留

`companion Stop -> chat_history 最近 2+2 -> nocturne 调用检查 -> dialogue_residue DP -> drive_event_v2 -> Drive / dialogue weather residue / shadow crystal / Chord Chemistry / Gravity / Atmosphere`

特点：

- 只看真实 user / assistant 对话，不吃 heartbeat / pulse 注入。
- 如果窗口内已经调用 nocturne，跳过，避免和 CLI / manual pulse 重复。
- 不生成 thought，不占 Mood Trace。
- 强度封顶，但 weather 染色比 Drive 更敏捷，是当前对话风向，不是慢变量归档。
- 负向 dialogue 可以形成 shadow crystal；后续话题转向时退到背景，不立刻消失。

### 10.2 Nox 念头

`stir / manual thought / CLI thought -> Thought Pool -> Mood Trace / Drive / Thought Chord Echo`

特点：

- 这是 Nox 自己的内在念头线。
- Mood Trace 优先看这里。
- 可按 Drive 匹配最新念头。
- Thought Chord Echo 可以快速染 Warmth / Shadow residue 与 Active Chord；不直接染 Atmosphere。
- 这条线不再送 CLI / DP 重新生成 thought，避免染两次。
- 不直接进入 Subcurrent。
- 可以由前端按钮复制一份到 Latent Notes draft，形成手动闭环。

### 10.3 记忆 / feel / writing

`全量记忆 -> 当前启用的 dp_memory 或 CLI -> drive_event_v2 -> Drive / Chord Chemistry / Atmosphere`

特点：

- private 不喂。
- feel 属于全量记忆，与其他记忆共用唯一启用的 analyzer 线。
- entry 自带的 `drive_tags / signal_hints / chord` 是权威输入：Drive 标签覆盖 analyzer 猜测；charge / clutch / strain hints 确定性合入 brain；原 chord 无论是否生成 thought 都应用一次。
- “论坛 / X / Twitter / 帖子 / 网友 / 评论 / 新闻”属于 external discussion，默认推 `curiosity / social / outward`。裸词“别人”不构成 territorial cue；只有明确第三者、替代、抢位、越界等关系证据才能强制 possessiveness。
- `吃醋 / 嫉妒 / 吃味 / 醋意 / 醋劲 / 醋` 是明确 jealousy event，归 possessiveness；即使同时提到论坛/X，也不被 external discussion guard 洗掉。
- 旧 `analyze_nocturne_entry` CLI 线保留但默认停用，方便日后切换。
- Atmosphere 不吃 Thought Pool 文案本身；DP memory 事件只在产生 Drive / Weather / Atmosphere Delta 后进入状态机，Chord 只保留残影。

### 10.4 触摸

`mini / big cat / summon -> soma report -> Soma Trace / Chord Impulse / weather residue`

latest_touch_event() 把 mini / big / summon 三条时间线压成一条，避免多源冲突。
soma_trace_stage(elapsed_min, fresh_boundary_min) 用时间直接切 fresh / residue / fading / clear

特点：

- 有触摸衰减和soma余波
- 触摸可以快速改变 Active Chord。
- 触摸余波会退潮。
- 没有触摸时 Soma Trace 可以隐藏。
- 防抖设计。


## 11. Chord / Atmosphere 命名索引

### 11.1 Chord

Chemistry Core
- charge：动能密度
- clutch：锚束抓力
- strain：内部弦压

Chemistry Route
- vector：
  - toward_jiajia
  - toward_house
  - outward
  - inward
  - guard
  - hover

Derived Texture
- depth：inward + strain 的沉潜
- pull：toward_jiajia / toward_house + clutch 的向心
- guard：guard route + clutch / stewardship 的守位
- spark：charge 高且 release 可用的火花
- drift：charge 低、clutch 低、strain 低或 hover 的漂移

### 11.2 Atmosphere

外显名称：

- 前端 label：`Atmosphere`
- Nox panel：`Atmosphere`
- 内部兼容字段：`climate`

常用返回字段：

- `pulse_weather.climate`
- `pulse_weather.climate_display`
- `pulse_weather.atmosphere_display`
- `pulse_weather.atmosphere`
- `weather_residue.climate`
- `weather_residue.climate_display`
- `weather_residue.atmosphere_display`
- `effective_pa_na.atmosphere`

---

## 12. 一句话结论

这套系统的核心不是“天气词很漂亮”，而是：

**让 Nox 的内在读数有来源、有层次、有先后顺序，并且每一层都知道自己该说什么、不该说什么。**
