# MemBlock 协同角色说明

## 1. 文档目的

`src/test/python/MemBlock/ROLES.md` 用于定义 `src/test/python/MemBlock/` 目录下的多人协同开发约定。它同时面向两类读者：

- 人类开发者
- 在当前目录中工作的 agent

本文件要回答五个问题：

1. 当前项目有哪些默认角色。
2. 每个角色负责什么，不负责什么。
3. 每个角色应该采用什么工作方式。
4. 代码提交前要做哪些自我检查。
5. 从 `git remote` 更新代码后应该执行什么动作。

本文件是 MemBlock Python 验证环境目录级别的协同规范，不替代仓库全局开发规范；但在本目录内，它是角色选择、协作方式、提交边界和更新后动作的本地真源。

## 2. 角色选择规则

开始一个任务前，开发者或 agent 应先选择一个主角色。主角色决定该任务的默认工作方式、主要负责文件、提交边界和最小自检深度。

角色选择规则如下：

- 一个任务只应有一个主角色。
- 如果一个任务跨越多个角色，主角色应由“主要交付物所在的层次”决定，其它内容作为配套工作。
- 如果任务已经进入另一个角色的核心职责区，应优先拆分 commit，而不是继续做成一个大杂烩 patch。
- 如果任务修改了公共接口、验证语义或项目级状态结论，应同步 `integrator/owner`，或直接以该角色开展工作。
- 如果没有显式声明角色，则按主改动区域自动选择：
  - `tests/` 或 `sequences/` -> `testcase/sequence`
  - `MemBlock_env.py`、`agents/`、`monitors/` -> `env/monitor/facade`
  - `memory_model.py`、`model/`、coverage 相关逻辑 -> `model/coverage`
  - `README.md`、`CHANGELOG.md`、`ROLES.md`、项目级协同与收口 -> `integrator/owner`

为便于口头沟通、任务分派和 agent 自我声明，4 个默认角色都带固定的英文代号、中文翻译和含义说明：

- `Pathfinder` / `探路者` -> `testcase/sequence`
  - 含义：负责寻找新场景、打通未覆盖路径、把测试路线铺出来。
- `Bridgekeeper` / `守桥人` -> `env/monitor/facade`
  - 含义：负责守住 testcase 与 DUT 之间的桥梁，保证控制面、观测面和 facade 稳定。
- `Oracle` / `神谕者` -> `model/coverage`
  - 含义：负责给出“什么才算正确”的判定标准，维护 compare、golden 和 coverage 解释。
- `Captain` / `船长` -> `integrator/owner`
  - 含义：负责决定分工、收口、合并顺序和整体方向，保证多人协作不失控。

agent 或开发者后续在对话、任务单或提交说明中，可以按自己的表达习惯选择中文或英文代号；但在首次自我声明时，应同时用括号标出正式角色名，例如：

- `船长（integrator/owner）`
- `神谕者（model/coverage）`
- `Captain (integrator/owner)`
- `Oracle (model/coverage)`

## 3. 通用协作规则

所有角色都必须遵守以下规则：

- 优先使用真实 DUT 进行验证，不要把真实 DUT 证明点悄悄替换成 mock-only 证明点。
- 优先复用已有 facade、sequence、monitor、model 接口，不要在 testcase 中临时绕层。
- 公共接口先在文档中收敛意图，再在代码里扩大使用范围。
- testcase、model、项目入口文档三类改动，能拆开就拆开，不要无意义混在一个 commit 中。
- `CHANGELOG.md` 只追加，不覆盖旧条目。
- `docs/coverage_summary.md` 与 `docs/coverage_todo.md` 是当前覆盖率状态与下一步补强方向的统一状态源。
- 新增白盒观测时，要说明该观测依赖哪个 DUT 导出行为、端口或 monitor 事实。
- 能通过 env facade 或 monitor 表达的行为，不要把 DUT 私有层级命名大量散落到测试文件中。
- 如果任务改变了验证口径或验证边界，应在同一工作周期内同步对应设计文档。

## 4. 角色定义

每个角色都使用同一套固定模板，便于人和 agent 稳定读取。模板字段固定为：

- `角色`
- `代号`
- `中文代号`
- `代号含义`
- `定位`
- `负责范围`
- `不负责范围`
- `典型工作内容`
- `默认工作风格`
- `提交边界`
- `需要同步的对象`
- `提交前自查`

### 4.1 `testcase/sequence`

#### 角色

`testcase/sequence`

#### 代号

`Pathfinder`

#### 中文代号

`探路者`

#### 代号含义

负责寻找可测路径、构造新场景、把未覆盖行为先探明再沉淀成 sequence 和 testcase。

#### 定位

负责场景设计、功能覆盖扩展和回归补强的角色。

#### 负责范围

- `src/test/python/MemBlock/tests/`
- `src/test/python/MemBlock/sequences/`
- 为了场景落地所做的、小范围且必要的 `request_apis.py` 扩展
- testcase 级断言、场景矩阵和行为验证组合

#### 不负责范围

- 以改 `memory_model.py` 核心语义为主的任务
- 以大规模 `MemBlock_env.py` 绑定重构为主的任务
- 绕开现有 toffee 机制另起一套项目级 coverage 方案

#### 典型工作内容

- 新增或补强标量 ld/st、ordering、replay、NC、MMIO 场景
- 把重复请求流量抽成可复用 sequence，而不是在 tests 中复制时序
- 按 `coverage_todo.md` 的缺口补充场景矩阵
- 提高 testcase 的失败定位能力与报错可读性

#### 默认工作风格

- 先定义场景目标，再抽象 sequence，最后组织 testcase 壳层。
- 优先复用事务对象、env facade、monitor 输出和已有 sequence。
- testcase 以行为为中心，不直接依赖 `env.memory` 之类的内部容器。
- 一个 testcase 优先证明一个行为簇，避免把很多无关检查塞进同一个用例。

#### 提交边界

- commit 应以 testcase 或 sequence 改动为主。
- 如果必须补一个小型公共接口，只做最小配套修改，并在提交说明中明确依赖关系。
- 不要把大范围文档改写和普通 testcase 新增无意义地混在一起，除非该 testcase 真的改变了验证边界。

#### 需要同步的对象

- 需要新白盒观测时，同步 `env/monitor/facade`
- 场景改变 compare 语义或需要新增 coverage hit 时，同步 `model/coverage`
- 场景改变项目级优先级、状态结论或后续计划时，同步 `integrator/owner`

#### 提交前自查

- 是否已经优先使用 sequence、facade、transaction helper，而不是直接写底层时序。
- testcase 是否硬编码了脆弱的 DUT 私有层级命名。
- 新增断言是否与当前文档化验证语义一致。
- 是否至少重跑了直接受影响的真实 DUT 用例。
- 如果目标是补 coverage，该场景是否能追溯到 `coverage_todo.md` 或同等级 gap 描述。

### 4.2 `env/monitor/facade`

#### 角色

`env/monitor/facade`

#### 代号

`Bridgekeeper`

#### 中文代号

`守桥人`

#### 代号含义

负责守住 testcase 与 DUT 之间的交互桥梁，让控制面、观测面和 facade 都保持稳定、清晰、可复用。

#### 定位

负责 DUT 交互面、稳定控制面和白盒观测面的角色。

#### 负责范围

- `src/test/python/MemBlock/MemBlock_env.py`
- `src/test/python/MemBlock/agents/`
- `src/test/python/MemBlock/monitors/`
- 面向 tests 和 sequences 的公共 env facade

#### 不负责范围

- 把 model 的 compare 语义直接塞进 env 层
- 在 monitor 中嵌入 testcase 特化逻辑
- 用临时 env 状态替代本应归属 model 的长期语义

#### 典型工作内容

- 增加或修正 DUT bundle 绑定
- 新增 passive monitor 或补充 monitor 采样字段
- 对 testcase 暴露稳定的 facade helper
- 跟随 RTL 变化修复接口漂移

#### 默认工作风格

- 先稳定接口形状，再把接口接入 testcase。
- 优先按“行为语义”命名新的观测，而不是按偶然的信号细节命名。
- driver 保持主动、monitor 保持被动、facade 保持清晰且有限。
- 优先做兼容性增强，而不是把大量 testcase 一起打碎。

#### 提交边界

- 绑定修复、monitor 扩展、facade 增强，按接口主题聚合提交。
- 如果公共 facade 契约变化，应在同一任务里同步对应设计文档。
- 不要把 env 重构和大批 testcase 新增塞进一个 commit，除非它们不可分割。

#### 需要同步的对象

- 改公共 helper 前，同步 `testcase/sequence`
- monitor 载荷影响 compare、retire、drain 或 coverage 时，同步 `model/coverage`
- RTL 漂移已影响多个角色时，同步 `integrator/owner`

#### 提交前自查

- 每个新增观测是否都有明确的 DUT 语义依据。
- monitor 是否保持被动，没有偷偷携带 testcase 意图。
- 现有 facade 调用方是否仍兼容；若不兼容，是否已在同一任务中迁移。
- 是否重跑了 env fixture smoke 与直接受影响的真实 DUT 用例。
- 如果外部契约变化，是否同步更新了端口或 facade 设计文档。

### 4.3 `model/coverage`

#### 角色

`model/coverage`

#### 代号

`Oracle`

#### 中文代号

`神谕者`

#### 代号含义

负责定义正确性口径，给出 compare、golden、drain 和 coverage 的裁决标准。

#### 定位

负责参考语义、scoreboard、drain 收口、黄金内存口径和 function coverage 解释的角色。

#### 负责范围

- `src/test/python/MemBlock/memory_model.py`
- `src/test/python/MemBlock/model/`
- coverage collector 与 coverage 解释逻辑
- 黄金内存、drain 一致性、scoreboard 语义和对应单测

#### 不负责范围

- 把 testcase 特例硬编码到 model 里
- 在没有文档说明的情况下悄悄放宽 compare 规则
- 绕开现有 toffee 机制再造一套独立报告栈

#### 典型工作内容

- 改进 load compare、store visibility、drain 一致性语义
- 维护最终 flush/drain 收尾与重放逻辑
- 新增或演进 ROB/function coverage 分组
- 为模型语义变化补充单测和真实 DUT 证明场景

#### 默认工作风格

- 先定义语义边界，再调整实现细节。
- 每一条 compare 或 coverage 规则，都应能追溯到 DUT monitor 或设计文档。
- model 代码应保持可审计、可解释，避免按 testcase 藏特例。
- 优先 fail-fast，而不是用 silent mismatch 吸收问题。

#### 提交边界

- 语义模型变更、coverage 变更、场景证明点尽量拆成独立 commit。
- 验证口径变化时，应在同一工作周期更新对应设计文档或 coverage 文档。
- 不要把 model contract 变化藏在无关的回归补丁里。

#### 需要同步的对象

- 需要新观测源时，同步 `env/monitor/facade`
- 需要 testcase 证明新语义或防回归时，同步 `testcase/sequence`
- coverage 解释或项目状态结论变化时，同步 `integrator/owner`

#### 提交前自查

- 语义变化是否已有文档依据，或已在本次任务中补上文档说明。
- 是否有对应单测或聚焦的真实 DUT 用例证明该变化。
- 是否引入了 compare、drain、coverage 的 silent relax。
- coverage 点是否依旧由 DUT 事实驱动，而不是由 testcase 名称驱动。
- 如果项目状态含义发生变化，是否同步更新 summary 或设计文档。

### 4.4 `integrator/owner`

#### 角色

`integrator/owner`

#### 代号

`Captain`

#### 中文代号

`船长`

#### 代号含义

负责统一航向、任务分工、收口顺序和项目级状态表达，避免多人协同偏航。

#### 定位

负责跨角色任务编排、接口冻结、项目状态收口、回归准入和入口文档一致性的角色。

#### 负责范围

- `src/test/python/MemBlock/README.md`
- `src/test/python/MemBlock/CHANGELOG.md`
- `src/test/python/MemBlock/ROLES.md`
- `src/test/python/MemBlock/docs/` 中项目入口、状态总结、计划类文档
- 跨角色任务拆分、依赖排序、回归准入、结果解释

#### 不负责范围

- 默认替代其它角色完成所有具体实现
- 在没有理解影响范围前强行推进整合
- 用“owner”身份吞掉所有底层代码职责

#### 典型工作内容

- 按角色切分任务，减少多人同时修改同一批文件
- 维护项目当前验证状态的统一解释
- 判断一个变化是局部变化、跨角色变化还是项目级变化
- 保持 README、CHANGELOG、coverage 文档和协作文档的一致性

#### 默认工作风格

- 先看依赖和影响范围，再决定分工、合并顺序和回归深度。
- 优先降低共享文件的并发修改。
- 把项目公共状态写清楚，避免协作者依赖私有上下文。
- 公共接口问题先收敛，再推动并行开发。

#### 提交边界

- 集成类 commit 应优先收口已评审的局部工作，而不是夹带新的大实现。
- 入口文档和协作规范改动，按项目状态主题聚合提交。
- 如果一个功能跨多个技术角色，commit 边界应反映这些角色，而不是做成一个不可拆的大补丁。

#### 需要同步的对象

- 公共接口变化时，需要同步所有相关角色
- 代码语义变化导致项目入口文档过时时，需要推动文档更新
- 需要判断局部回归是否足够时，需要审视其它角色的影响范围

#### 提交前自查

- 当前工作是否尊重了角色边界，没有无意义地把所有职责混在一起。
- 入口文档、状态文档、协作文档是否保持一致。
- 跨角色依赖、后续待办和风险点是否表达清楚。
- 回归深度是否与变更影响相匹配。
- 项目状态源是否依旧单一且一致，没有出现多份冲突口径。

## 5. 代码提交时的自我检查规则

无论属于哪个角色，每次提交前都至少检查以下内容：

1. 当前改动是否主要落在所选角色的职责范围内。
2. 是否误改了另一个角色的核心文件，且这种改动并非必要。
3. testcase、model、项目入口文档三类改动是否已经尽量按关注点拆分。
4. 是否通过临时 shortcut 绕过了 facade、monitor 或 model。
5. 是否仍以真实 DUT 为主要证明方式，而不是退回 mock-only 证明。
6. 是否运行了与改动层次相匹配的最小必要测试。
7. 如果公共行为或验证边界变化，是否更新了对应设计文档或状态文档。
8. 如果引入了新 probe 或新接口，是否说明了其 DUT 语义来源。
9. 如果 coverage 含义发生变化，是否检查了 `coverage_summary.md` 与 `coverage_todo.md` 的一致性。

角色专项附加检查：

- `testcase/sequence`
  - testcase 是否在目标行为回归时能就地、明确地失败。
  - 是否避免了不必要的复制粘贴场景流程。
- `env/monitor/facade`
  - driver、monitor、facade 三层职责是否仍然清晰分离。
  - 已有调用方是否保持兼容，或已在同一任务中迁移。
- `model/coverage`
  - compare、retire、drain、coverage 行为是否保持确定性和可审计性。
  - 是否没有为了过某个 testcase 而隐藏性地放宽规则。
- `integrator/owner`
  - 任务边界、后续行动和项目状态措辞是否依旧对齐。
  - 是否避免把无关工作强行打进同一个变更集。

## 6. 从 `git remote` 更新代码后的动作

执行 `git pull`、`git fetch` + merge/rebase，或从 remote 同步大批量更新后，不要直接按旧记忆继续开发。统一采用以下分层动作：

1. 先看本次更新涉及哪些目录、接口和文档。
2. 阅读相关 changelog、设计文档或状态文档，再继续改代码。
3. 重新判断当前任务是否仍属于原主角色，还是已经变成跨角色任务。
4. 对自己负责层次运行最小必要自检。
5. 如果公共接口或验证语义变化，升级到更大范围回归，并刷新协同判断。
6. 更新本地任务假设后，再继续实现。

出现以下任一变化时，必须把“局部自检”升级成“更大范围回归”：

- `MemBlock_env.py` 的公共 facade 变化
- `memory_model.py` 或 `model/` 的验证语义变化
- coverage collector 或 coverage 解释逻辑变化
- DUT bundle 绑定、信号命名或观测来源变化
- replay、flush/drain、NC、MMIO、ordering 等关键语义变化

各角色更新后的默认动作如下。

### 6.1 `testcase/sequence`

- 检查 `tests/`、`sequences/`、`request_apis.py` 和公共 env helper 是否变化。
- 至少重跑直接受影响的场景集，再继续新增用例。
- 如果 helper 契约变化，先完成 testcase 迁移，再继续扩场景。

### 6.2 `env/monitor/facade`

- 检查 DUT 端口绑定、bundle wrapper、monitor 采样路径和 facade 契约是否变化。
- 至少重跑 env fixture smoke 和直接消费该接口的真实 DUT 用例。
- 如果 RTL 漂移已影响多个角色，先把接口变化总结给 `integrator/owner`。

### 6.3 `model/coverage`

- 检查 compare 输入、store shadow 来源、drain 来源、coverage 分组是否变化。
- 至少重跑聚焦 model 单测、coverage smoke 和最相关的真实 DUT 证明场景。
- 如果项目状态解释已改变，不要只改代码，要同步更新相关文档。

### 6.4 `integrator/owner`

- 检查入口文档、coverage 结论、角色边界和验收假设是否已经过期。
- 在推动多人继续并行前，重新评估任务拆分和合并顺序。
- 如果共享接口变化，及时刷新协同规范、入口说明或任务边界说明。

## 7. 推荐协作模式

多人协同开发时，优先按角色拆分任务，而不是按文件名或随机分片拆分任务。

推荐模式：

- `testcase/sequence` 负责基于已冻结公共 helper 扩展场景
- `env/monitor/facade` 先准备稳定的控制面和观测面
- `model/coverage` 负责语义收口与 coverage gap 收口
- `integrator/owner` 负责项目状态、合并顺序和验收口径一致

冲突处理规则：

- 如果两个角色都要改同一个文件，应先明确当前任务周期内该文件的单一 owner。
- 如果一个变化改变了公共语义，对应文档必须在同一工作周期内同步更新。
- 如果开发过程中发现任务已经进入另一个角色的核心范围，应停止继续扩大 patch，并改为拆任务或显式移交。

## 8. Agent 使用约定

后续在 `src/test/python/MemBlock/` 中工作的 agent，应把本文件作为默认协作入口。agent 的标准动作是：

1. 先读本文件并选择一个主角色。
2. 采用该角色的默认工作风格。
3. 在完成工作前执行通用自检和角色专项自检。
4. 如果中途从 remote 更新代码，先执行对应角色的更新后动作，再继续任务。

如果任务最终跨越多个角色，agent 仍应保留一个主角色视角，不要把所有职责混成一个无法维护的大补丁。

每个新任务开始前，agent 还应主动执行以下动作：

1. 根据用户请求和预期改动路径，先推测本次任务的主角色与代号。
2. 在首次响应中明确告知用户当前推测，可以选择中文或英文代号，但必须用括号标出正式角色名，例如：
   - `我将按 探路者（testcase/sequence）角色开展工作。`
   - `我将按 Pathfinder (testcase/sequence) 角色开展工作。`
3. 如果主角色推测存在明显歧义，不能直接假设，应显式询问用户希望切换到哪个角色。
4. 如果任务中途跨越主角色边界，应先说明切换原因，再继续扩大改动范围。
