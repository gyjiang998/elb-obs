'use client'

import {
  Activity,
  ArrowDownToLine,
  BrainCircuit,
  BookOpenCheck,
  Check,
  ChevronRight,
  CircleAlert,
  Cpu,
  Database,
  FileCode2,
  FlaskConical,
  FolderGit2,
  GitBranch,
  Gauge,
  HardDrive,
  History,
  Lightbulb,
  LoaderCircle,
  Network,
  Play,
  RefreshCw,
  Route,
  Search,
  Sparkles,
  Trash2,
  Upload,
  Workflow,
  X,
} from 'lucide-react'
import { ChangeEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AgentPanel } from './components/AgentPanel'
import { ApplicationFlowStudio, type ApplicationFlowAssistantContext } from './components/ApplicationFlowStudio'
import { CodeKnowledgeStudio, type CodeKnowledgeAssistantContext } from './components/CodeKnowledgeStudio'
import { AnalysisRecordTrace } from './components/AnalysisRecordTrace'
import { ExperienceEvolutionStudio } from './components/ExperienceEvolutionStudio'
import { ExperimentalFlowPlayground, type ExperimentalFlowAssistantContext } from './components/ExperimentalFlowPlayground'
import { ScenarioCaptureStudio, type ScenarioCaptureAssistantContext } from './components/ScenarioCaptureStudio'
import { ParallelModelingStudio, type ParallelModelingAssistantContext } from './components/ParallelModelingStudio'
import { TraceLibraryStudio } from './components/TraceLibraryStudio'
import { OptimizationCalibrationStudio } from './components/OptimizationCalibrationStudio'
import { WorkloadFeaturesStudio } from './components/WorkloadFeaturesStudio'
import { HardwareModelsStudio } from './components/HardwareModelsStudio'
import './parallel-modeling.css'
import { HardwareStrategyEvaluation } from './components/HardwareStrategyEvaluation'
import { HardwareSemanticRoutingEvaluation } from './components/HardwareSemanticRoutingEvaluation'
import { ReasoningLab, type ReasoningAssistantContext, type ReasoningRecordRestoreRequest } from './components/ReasoningLab'
import { SkillLibraryManager } from './components/SkillLibraryManager'
import { TraceStageTimeline } from './components/TraceStageTimeline'
import { api, digest, formatBytes, upload } from './lib/api'
import { listAnalysisWorkspaces, readAnalysisWorkspace, saveAnalysisWorkspace, type AnalysisWorkspaceSnapshot } from './lib/analysis-workspace'
import { invalidateAnalysisCatalog, loadCustomApiCatalog } from './lib/analysis-catalog-cache'
import { analysisErrorPayload, appendAnalysisTraceEvent } from './lib/analysis-observability'
import { createTraceAnalysisRecord, readAnalysisHistory, sortAnalysisHistory, writeAnalysisHistory, type AnalysisDeviceValidationRecord, type AnalysisHotspotSummary, type AnalysisInferenceRecord, type TraceAnalysisStart, type TraceAnalysisRecord } from './lib/analysis-history'
import { clearAnalysisRouteId, readAnalysisRouteId, writeAnalysisRouteId } from './lib/analysis-route'
import { hardwareSemanticRoutingSkill } from './lib/hardware-semantic-routing'
import { createIdleOptimizationWorkflow, deviceValidationControlSkill, frontendSkillGovernance, fullOptimizationAnalysisSkill, pageAgentControlSkill, strategySkillPrompts, type OptimizationWorkflowEvent, type OptimizationWorkflowState } from './lib/optimization-workflow'
import { requestReasoningLabAction } from './lib/reasoning-action-bridge'
import { randomUuid } from './lib/random-id'
import { readSourceConnectorId, writeSourceConnectorId } from './lib/source-connector-selection'
import { applySkillPrompt, readSkillProfileId, writeSkillProfileId, type SkillVersionLibrary, type SkillVersionProfile } from './lib/skill-version-library'
import { readBrowserModelConfig } from './lib/model-config'
import { browserPlatformBuild, shortBuildId, shortRevision, type PlatformVersion } from './lib/platform-version'
import { createTargetPlatformProfile, readTargetPlatformProfile, writeTargetPlatformProfile, type TargetPlatformProfile } from './lib/target-platform-profile'
import { deviceValidationSnapshotEvent, normalizeDeviceValidationSnapshot, readDeviceValidationSnapshot, storeDeviceValidationSnapshot } from './lib/device-validation-snapshot'
import type { Artifact, Connector, HotspotCandidate, JsonRecord, Overview, PageAction, TraceCluster, TraceEventCandidate } from './lib/types'

type ViewId = 'scenarios' | 'reasoning' | 'flows' | 'skills' | 'experience' | 'knowledge' | 'sources' | 'code_knowledge' | 'results' | 'playground_hiperf' | 'playground_trace' | 'analysis_history' | 'scenario_capture' | 'parallel_modeling' | 'trace_library' | 'optimization_calibration' | 'workload_features' | 'hardware_models'
type TraceInputMode = 'external_report' | 'raw_htrace'
type TraceExplorerTab = 'hotspots' | 'clusters'
type SearchableKnowledgeBaseKind = 'optimization' | 'hardware'
type AcceleratorUnit = 'gpu' | 'dsp' | 'media'
type KnowledgeBaseKind = SearchableKnowledgeBaseKind | 'npu_api' | 'accelerator_api' | 'custom_api'

type KBSearchResponse = {
  knowledge_base: KnowledgeBaseKind
  kb_id: string
  kb_version: string
  kb_content_sha256: string
  summary: JsonRecord
  records: Array<JsonRecord>
  case_records: Array<JsonRecord>
}

type StrictCaseDetailResponse = {
  kb: JsonRecord
  case: JsonRecord
  cpu_source: JsonRecord
}

type NpuApiCatalogResponse = {
  npu_api_kb: JsonRecord
  query: string
  record_count: number
  summary: JsonRecord
  records: JsonRecord[]
}

type NpuApiDetailResponse = {
  npu_api_kb: JsonRecord
  record: JsonRecord
}

type AcceleratorApiCatalogResponse = {
  accelerator_api_kb: JsonRecord
  accelerator_unit: AcceleratorUnit
  query: string
  record_count: number
  summary: JsonRecord
  records: JsonRecord[]
}

type AcceleratorApiDetailResponse = {
  accelerator_api_kb: JsonRecord
  accelerator_unit: AcceleratorUnit
  record: JsonRecord
}

type CustomApiKnowledgeBaseMetadata = JsonRecord & {
  kb_id: string
  display_name: string
  accelerator_unit: string
  created_at: string
  source: JsonRecord
  summary: JsonRecord
  scan: JsonRecord
}

type CustomApiKnowledgeBaseCatalogResponse = {
  schema_version: string
  count: number
  platform_merge_policy: string
  knowledge_bases: CustomApiKnowledgeBaseMetadata[]
}

type CustomApiKnowledgeBaseDetailResponse = {
  knowledge_base: CustomApiKnowledgeBaseMetadata
  query: string
  record_count: number
  records: JsonRecord[]
}

type CustomApiRecordDetailResponse = {
  knowledge_base: CustomApiKnowledgeBaseMetadata
  record: JsonRecord
}

type OpenHarmonySdkAssetStatus = {
  schema_version: string
  bundle: JsonRecord
  selection: JsonRecord | null
  source: JsonRecord | null
}

type ClonePlan = JsonRecord & {
  plan_id: string
  capacity: { available_bytes: number; required_total_bytes: number; can_clone: boolean }
  hard_gates: string[]
}

type ClusterExplorerSummary = {
  moduleNames: string[]
  moduleKeys: string[]
  topFunctionName: string
  systemModuleOnly: boolean
}

type SelfTimeDiscoveryResponse = {
  events: TraceEventCandidate[]
  quality: JsonRecord
  module_catalog?: JsonRecord
}

const traceModuleRules = [
  { key: 'ffrt_runtime', name: 'FFRT 运行时', system: true, tokens: ['ffrt', 'os_ffrt', 'ffrtd'] },
  { key: 'binder_ipc', name: 'Binder / IPC', system: true, tokens: ['binder', 'os_ipc', 'ipc_', 'iremote', 'talkwithdriver', 'hwbinder', 'rpc'] },
  { key: 'kernel_scheduler', name: '内核调度', system: true, tokens: ['kworker', 'ksoftirqd', 'softirq', 'rcu_', 'rcu-', 'migration', 'cpuhp', 'swapper', 'irq/', 'irq-'] },
  { key: 'system_service', name: '系统服务', system: true, tokens: ['sainit', 'samgr', 'hiview', 'hilogd', 'system_ability', 'sysmgr', 'hdf', 'hiprofiler'] },
  { key: 'render_rosen', name: '渲染 / Rosen', system: false, tokens: ['render_service', 'renderthread', 'renderframe', 'rosen', 'render', 'surface', 'compositor'] },
  { key: 'graphics_gpu', name: '图形 / GPU', system: false, tokens: ['gpu', 'vulkan', 'opengl', 'egl', 'hwc', 'composer', 'gralloc', 'skia', 'drawing', 'raster'] },
  { key: 'arkui_ui', name: 'ArkUI / UI', system: false, tokens: ['arkui', 'ace::', 'ace_', 'uicontent', 'ui_content'] },
  { key: 'media_codec', name: '媒体 / 编解码', system: false, tokens: ['media', 'codec', 'audio', 'camera', 'video', 'image', 'avcodec'] },
  { key: 'network_io', name: '网络 / I/O', system: false, tokens: ['network', 'netd', 'wifi'] },
] as const

const navItems: Array<{ id: ViewId; label: string; icon: typeof Activity; hidden?: boolean }> = [
  { id: 'scenario_capture', label: '负载采样', icon: Activity },
  { id: 'workload_features', label: '负载特征', icon: Activity },
  { id: 'hardware_models', label: '硬件模型', icon: Cpu },
  { id: 'optimization_calibration', label: '寻优校准', icon: FlaskConical },
  { id: 'parallel_modeling', label: '并行建模', icon: Network },
  { id: 'flows', label: '应用流程', icon: Route },
  { id: 'reasoning', label: '机会推理', icon: BrainCircuit },
  { id: 'sources', label: '代码仓', icon: FolderGit2 },
  { id: 'code_knowledge', label: '代码知识库', icon: BookOpenCheck },
  { id: 'knowledge', label: '优化知识库', icon: Database },
  { id: 'results', label: '机会报告', icon: Workflow },
  { id: 'skills', label: '技能列表', icon: FileCode2 },
  { id: 'trace_library', label: 'Trace 管理', icon: HardDrive },
  { id: 'experience', label: '经验演进', icon: Lightbulb },
  { id: 'scenarios', label: '追踪场景', icon: Activity, hidden: true },
  { id: 'playground_hiperf', label: 'Playground', icon: Gauge, hidden: true },
  { id: 'playground_trace', label: 'Playground 2', icon: FlaskConical, hidden: true },
]

const studioOwnHeadingViews: ViewId[] = [
  'scenario_capture',
  'workload_features',
  'hardware_models',
  'optimization_calibration',
  'parallel_modeling',
  'trace_library',
]

type AnalysisSkillItem = {
  id: string
  label: string
  detail: string
  icon: typeof Activity
  targetView: ViewId
  execution: string
  input: string
  output: string
  boundary: string
  prompt?: string
  version?: string
  qualityGate?: string
  evaluation?: string
  iteration?: string
}

const analysisSkillItems: AnalysisSkillItem[] = [
  {
    id: 'scenario-step-capture', label: '负载采样', icon: Activity, targetView: 'scenario_capture',
    detail: '将操作步骤转为 Python 自动化计划，采集指定步骤的 Trace、HiLog 和 PMU',
    execution: '页面 Agent 生成计划，自动识别设备与连接方式，执行采集并归档',
    input: '场景操作、目标步骤、界面定位、采集项与设备',
    output: '可复用 Python 脚本、步骤采集批次与数据清单',
    boundary: '以选中步骤为观测窗口，保留前置动作及数据来源。',
  },
  {
    id: 'builditem-parallel-modeling', label: 'BuildItem 并行建模', icon: Network, targetView: 'parallel_modeling',
    detail: '拆解关键路径，提取负载特征，仿真并行与核频配置及预期收益',
    execution: '数值分析与离线校准模型驱动的异步仿真寻优',
    input: '场景数据、目标帧、资源范围、校准模型与时延预算',
    output: '推荐并行配置、预测耗时、收益与资源代价',
    boundary: '结合实测输入和模型适用条件解释预测。',
  },
  {
    id: 'application-execution-flow-reconstruction',
    label: '应用执行流程重建',
    detail: '构建模块代码底图，围绕用户目标确认流程，再生成有序阶段与 HiTrace 边界',
    icon: Route,
    targetView: 'flows',
    execution: 'Tree-sitter/SQLite 底图 + 页面 Agent + 源码复核 + 确定性 Schema 校验',
    input: '已登记代码仓、模块目录、用户操作描述与候选确认',
    output: '代码底图、候选流程、应用流程场景 v2 有序 Stage JSON/Mermaid 与源码 HiTrace 边界',
    boundary: '源码插桩存在不等于事件实际发生；未闭合 start/end 的阶段保持草稿状态。',
  },
  {
    id: 'experimental-flow-hiperf-reconstruction',
    label: '实验流程重建（HiPerf）',
    detail: '结合源码与 HiPerf 采样生成函数级自由 Mermaid，并映射 Self 指标',
    icon: Gauge,
    targetView: 'playground_hiperf',
    execution: '页面 Agent 原生代码理解 + HiPerf 符号映射 + 实验工件发布',
    input: '用户流程、代码仓范围与 HiPerf 原始数据或报告',
    output: '自由 Mermaid、函数 Self 指标映射与证据说明',
    boundary: '采样百分比、样本数和估算时间保持原始口径，不套用正式应用流程 Schema。',
  },
  {
    id: 'experimental-flow-trace-reconstruction',
    label: '实验流程重建（Trace）',
    detail: '结合源码与真实 Trace 生成事件校准的函数级自由 Mermaid',
    icon: FlaskConical,
    targetView: 'playground_trace',
    execution: '页面 Agent 原生代码理解 + Trace 时序映射 + 实验工件发布',
    input: '用户流程、代码仓范围与 .htrace、Trace JSON 或文本报告',
    output: '自由 Mermaid、函数/事件映射、运行时序与证据说明',
    boundary: 'Trace wall/span/inclusive 时间保持运行时口径，不冒充 CPU self time。',
  },
  {
    id: 'code-knowledge-analysis',
    label: '代码知识分析',
    detail: '检索模块静态底图、复核关键源码，并持续发布 Markdown 与 Mermaid 知识报告',
    icon: BookOpenCheck,
    targetView: 'code_knowledge',
    execution: 'Tree-sitter/SQLite 底图 + 页面 Agent 原生代码工具 + MCP 报告发布',
    input: 'ready 代码底图、已登记源码快照、当前报告和用户代码问题',
    output: '带底图血缘、父版本、问题与 Agent Run 的 Markdown 报告',
    boundary: '静态调用候选、源码事实和 Agent 语义推断必须分开陈述；不声明运行时关键路径。',
  },
  {
    id: 'heterogeneous-opportunity-orchestrator',
    label: '全流程机会分析',
    detail: '从真实 Trace 到可验证的优化结论',
    icon: Workflow,
    targetView: 'reasoning',
    execution: '页面 Agent 引导',
    input: '真实 Trace、页面状态、热点代码和策略选择',
    output: '一个受状态机约束的下一步操作与证据边界说明',
    boundary: '不替用户选择 Trace，不虚构热点、源码、硬件或收益。',
  },
  {
    id: 'full-optimization-analysis',
    label: '全流程机会分析约束',
    detail: '最终策略推理共享的证据与校准规则',
    icon: Check,
    targetView: 'reasoning',
    execution: '浏览器模型推理',
    input: '单个已准备的策略 context pack',
    output: '受 JSON 契约约束的异构、并行或算法结论',
    boundary: '必须同时以 Trace 时间事实和源码/AST 事实支撑结论；不把画像或模型假设升级为设备事实。',
    prompt: fullOptimizationAnalysisSkill,
  },
  {
    id: 'page-agent-control',
    label: '页面 Agent 控制',
    detail: '将对话限制为可审计的页面 action',
    icon: BrainCircuit,
    targetView: 'reasoning',
    execution: '浏览器模型推理 + 已注册 action',
    input: '当前页面状态、受限热点目录和 action catalog',
    output: '简短回复及 schema 合法的 action 请求',
    boundary: '不得读取密钥、执行未注册操作或把聊天内容当作 action 成功事实。',
    prompt: pageAgentControlSkill,
  },
  {
    id: 'device-validation-control',
    label: '页面 Agent 真机实验',
    detail: '用真实构建、上传与手机输出完成自主迭代',
    icon: HardDrive,
    targetView: 'reasoning',
    execution: '页面 Agent + 固定 profile 工具链 + probe 绑定 HDC',
    input: '设备探测、热点代码、策略结果与完整实验源码',
    output: '源码、构建上传、远端执行和设备观察工件',
    boundary: '模型不能控制工作站路径、编译参数或 HDC；构建/运行通过不等于热点已卸载或有收益。',
    prompt: deviceValidationControlSkill,
  },
  {
    id: 'trace-hotspot-discovery',
    label: 'Trace 热点提取',
    detail: '按输入类型生成可追溯热点目录',
    icon: Activity,
    targetView: 'scenarios',
    execution: '本地确定性分析',
    input: '真实 .htrace 或追踪团队 JSON 报告',
    output: '按模块归类的热点事件、自耗时和质量信息',
    boundary: '原始 Trace 只能输出其解析器支持的事实，不能等价为关键路径结论。',
  },
  {
    id: 'target-platform-profile',
    label: '目标设备与系统画像',
    detail: '将用户选定的麒麟/鸿蒙目标写入推理上下文',
    icon: HardDrive,
    targetView: 'reasoning',
    execution: '本地浏览器状态',
    input: '设备 / SoC 型号和操作系统版本选择',
    output: '版本化 target_platform_profile/v1 context 字段',
    boundary: '用户选择的范围不是设备探测，不证明加速器、驱动、接口或收益可用。',
  },
  {
    id: 'trace-event-source-resolution',
    label: '热点源码定位',
    detail: '插桩、模块与受限符号反查',
    icon: Search,
    targetView: 'scenarios',
    execution: '本地确定性分析',
    input: 'Trace 事件、模块线索与已选择的代码仓快照',
    output: '源码定位 artifact、匹配等级与局部代码块',
    boundary: '文本命中、包围函数和精确插桩点的证据等级必须分开保存。',
  },
  {
    id: 'ast-feature-extraction',
    label: 'AST 特征提取',
    detail: '函数级或局部代码片段的结构特征',
    icon: FileCode2,
    targetView: 'scenarios',
    execution: '本地确定性分析',
    input: '精确源码绑定的函数或局部执行片段',
    output: '循环、控制流、数据类型、内存访问和调用边界特征',
    boundary: '编译上下文不完整时必须输出诊断，不能把文本特征伪装成精准 AST。',
  },
  {
    id: 'strategy-kb-rag',
    label: '优化知识库上下文推理',
    detail: '严格 pair 与已有案例的受控比对',
    icon: Database,
    targetView: 'reasoning',
    execution: '浏览器模型推理',
    input: '热点代码、Trace 事实和带来源的优化知识库 context pack',
    output: '带 evidence_id 的机会假设、风险和验证计划',
    boundary: '已有异构 + CPU fallback pair 是参考模式，不证明当前热点一定可卸载。',
    prompt: strategySkillPrompts.kb_rag,
  },
  {
    id: 'strategy-hardware-kb',
    label: '硬件机会推理',
    detail: '模型先按代码语义选择硬件目录，再对该目录 API Top-N 做受控精核',
    icon: Network,
    targetView: 'reasoning',
    execution: '浏览器模型推理',
    input: '热点代码、Trace 事实和硬件能力 context pack',
    output: '候选异构单元/API、CPU 并行判断、端到端代价与缺失设备证据',
    boundary: '单元目录来自独立模型语义路由，不是规则或设备事实；没有本轮 API evidence 时不得猜测具体接口，能力记录未明确覆盖的芯片、驱动、格式或接口只能作为待验证假设。',
    prompt: strategySkillPrompts.hardware_kb,
  },
  {
    id: 'hardware-semantic-routing-experiment',
    label: '模型语义硬件路由',
    detail: '以代码、硬件中立画像与平台/自建能力卡独立选择 API 目录',
    icon: Network,
    targetView: 'reasoning',
    execution: '浏览器模型推理 + 本地结构校验',
    input: '当前热点代码、硬件中立词法画像、候选作用域、平台能力卡和用户自建目录能力卡',
    output: '0–2 个单元级候选或需要展开计算核心的诊断',
    boundary: '规则基线不进入模型输入；不选择具体 API，不声明设备支持，结果只作为后续单元内 API Top-N 精核的候选入口。',
    prompt: hardwareSemanticRoutingSkill,
  },
  {
    id: 'strategy-direct-baseline',
    label: '直接代码推理',
    detail: '结合目标画像、无 KB 事实的保守并行化基线',
    icon: BrainCircuit,
    targetView: 'reasoning',
    execution: '浏览器模型推理',
    input: '热点代码、Trace 事实、源码绑定等级和目标设备/系统画像',
    output: 'CPU 并行候选、异构方向假设和实验设计',
    boundary: '目标画像不是能力证据；不得声称具体硬件单元、接口或性能收益已经可用。',
    prompt: strategySkillPrompts.direct_baseline,
  },
  {
    id: 'opportunity-recognition',
    label: '机会证据门禁',
    detail: '把候选方向约束为可审计结论',
    icon: Check,
    targetView: 'results',
    execution: '本地确定性校验',
    input: '热点时间语义、源码绑定、AST 特征和上下文证据',
    output: '机会状态、置信度、缺失条件和验证任务',
    boundary: '证据不足必须返回待验证或拒绝，不能升级为已确认优化。',
  },
  {
    id: 'skill-evolution-governance',
    label: 'Skill 演进治理',
    detail: '让每项分析能力可回归、可审计、可回滚',
    icon: History,
    targetView: 'skills',
    execution: '版本化 registry 与自动回归',
    input: 'Skill manifest、失败样本、推理 transcript 和冻结评测结果',
    output: '可发布的版本、质量门禁和演进记录',
    boundary: '模型不能根据单次回答自动改写 prompt、阈值或知识库。',
  },
  {
    id: 'execution-pitfall-distillation',
    label: '执行踩坑提炼',
    detail: '从跨模型、Agent、宿主与设备历史中提炼可复用问题和候选处置',
    icon: Lightbulb,
    targetView: 'experience',
    execution: '页面 Agent + 历史执行源工具 + 不可变评审工件 + 用户研判',
    input: 'Agent Run、页面分析 Trace、真机学习记录与环境身份',
    output: '问题类、根因假设、适用边界、候选处置和验证状态',
    boundary: '单次失败不能自动修改环境、代码、知识库或生产 Skill；未知历史环境保持未知。',
  },
]

// Keep the legacy contracts readable for historical analysis records, but do
// not expose retired strategy entry points in the current product surface.
const hiddenAnalysisSkillIds = new Set(['strategy-kb-rag', 'strategy-direct-baseline'])

const skillPromptSlots: Record<string, string> = {
  'scenario-step-capture': 'scenario_capture',
  'builditem-parallel-modeling': 'parallel_modeling',
  'application-execution-flow-reconstruction': 'application_flow',
  'experimental-flow-hiperf-reconstruction': 'experimental_flow_hiperf',
  'experimental-flow-trace-reconstruction': 'experimental_flow_trace',
  'code-context-retrieval': 'code_context',
  'code-knowledge-analysis': 'code_knowledge',
  'custom-accelerator-api-kb-builder': 'custom_api_kb',
  'page-agent-control': 'page_agent',
  'page-analysis-artifact-orchestrator': 'page_analysis',
  'device-validation-control': 'device_validation',
  'agent-native-device-experimentation': 'device_validation',
  'full-optimization-analysis': 'full_analysis',
  'strategy-hardware-kb': 'strategy_hardware_kb',
  'strategy-kb-rag': 'strategy_kb_rag',
  'strategy-direct-baseline': 'strategy_direct_baseline',
  'hardware-semantic-routing-experiment': 'hardware_route',
  'blind-opportunity-analysis': 'blind_reasoning',
  'blind-flow-comparison': 'blind_comparison',
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function stringValue(value: unknown, fallback = '—'): string {
  return typeof value === 'string' && value.length > 0 ? value : fallback
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : []
}

type SkillDisplayGovernance = {
  version: string
  qualityGate: string
  evaluation: string
  iteration: string
}

const localSkillGovernance: Record<string, SkillDisplayGovernance> = {
  'heterogeneous-opportunity-orchestrator': {
    version: '1.1.0',
    qualityGate: '只调用已注册 action，不能跳过页面状态机或子 Skill 的证据门禁。',
    evaluation: 'action schema、状态机边界、审计字段和 resource-heavy 计划回归。',
    iteration: '接入统一 registry 治理与上下文最小充分投影。',
  },
  'trace-hotspot-discovery': {
    version: '1.1.0',
    qualityGate: 'self-time、inclusive、模块 Top-N 和解析质量必须同时保留。',
    evaluation: '真实 .htrace、自耗时语义、模块分组和 DB 失败场景回归。',
    iteration: '明确 span 记账不是 CPU callee 时间，也不是关键路径证明。',
  },
  'target-platform-profile': {
    version: '1.0.0',
    qualityGate: '画像仅是用户选择的目标范围，不能被当作 capability probe。',
    evaluation: '画像切换必须使 context SHA 变化且旧策略结果失效。',
    iteration: '与模型策略共用设备事实边界。',
  },
  'trace-event-source-resolution': {
    version: '1.3.0',
    qualityGate: '精确插桩、函数派生、文本候选与 unresolved 必须分级。',
    evaluation: 'marker 高亮、受限搜索、文件上限和 non-exact 阻断回归。',
    iteration: '将 HiTrace 模块和插桩证据加入源码反查诊断。',
  },
  'ast-feature-extraction': {
    version: '1.0.0',
    qualityGate: '词法扫描与精准 AST 必须严格区分，未解析 compute core 不可伪装。',
    evaluation: '函数范围、编译上下文、AST 不可用与 wrapper/core 分离回归。',
    iteration: '把函数级 AST 作为 KB 匹配前的独立门禁。',
  },
  'hardware-semantic-routing-experiment': {
    version: '3.1.0',
    qualityGate: '候选只能来自本轮能力卡，最多两个；自建目录使用独立 custom:* ID，不能并入平台目录。生产链不执行规则基线，模型输入不含规则结果、完整 API 目录、trace 名、模块名、源码路径或额外函数提示，也不得升级为 API 或设备事实。',
    evaluation: '函数改名、等价控制流改写、NPU/GPU/DSP/媒体正例、自建目录动态注册/删除/变更、语义缺失与 IPC/锁/I/O/依赖反例，以及模型输出 schema/证据白名单回归。',
    iteration: 'dynamic-custom-api-route-funnel-v1：模型比较本轮平台与自建目录；所选 custom:* 目录再独立执行 API Top-N，不与平台目录合并。',
  },
  'opportunity-recognition': {
    version: '1.0.0',
    qualityGate: '未知源码绑定、缺失 AST 或无 KB 证据必须停在 evidence_required。',
    evaluation: '阻塞条件、KB manifest、source resolution 匹配和不升级断言。',
    iteration: '将候选方向与已有实现 reference 明确分离。',
  },
}

type BackendSkillPresentation = Pick<AnalysisSkillItem, 'label' | 'detail' | 'icon' | 'targetView'>

const backendSkillPresentation: Record<string, BackendSkillPresentation> = {
  'application-execution-flow-reconstruction': { label: '应用执行流程重建', detail: '发现业务流程、闭合源码 HiTrace 边界并生成标准产物', icon: Route, targetView: 'flows' },
  'code-context-retrieval': { label: '代码语义检索', detail: '跨页面检索静态代码底图并以真实源码复核关键关系', icon: BookOpenCheck, targetView: 'code_knowledge' },
  'blind-opportunity-analysis': { label: '独立盲推理基线', detail: '仅凭代码、用户问题和目标画像形成隔离基线', icon: BrainCircuit, targetView: 'reasoning' },
  'blind-flow-comparison': { label: '盲推理与流程对照', detail: '在独立 Session 中诊断流程资产的增益与束缚', icon: GitBranch, targetView: 'reasoning' },
  'trace-result-ingestion': { label: '追踪团队报告归一化', detail: '保留上游报告的事实与来源等级', icon: Upload, targetView: 'scenarios' },
  'raw-htrace-analysis': { label: '原始 .htrace 解析', detail: '由 TraceStreamer 产生基础 span 与调度候选', icon: Activity, targetView: 'scenarios' },
  'trace-clustering': { label: '调度唤醒簇分析', detail: '实验性 .sys 窗口聚类与质量诊断', icon: Workflow, targetView: 'scenarios' },
  'trace-event-extraction': { label: 'Trace 簇事件提取', detail: '从指定 wakeup/span cluster 枚举事件', icon: Activity, targetView: 'scenarios' },
  'trace-hotspot-discovery-source-resolution': { label: '热点事件源码反查', detail: '为 trace-wide self-time event 生成分级源码证据', icon: Search, targetView: 'scenarios' },
  'trace-source-snapshot-attestation': { label: '源码快照声明绑定', detail: '将真实 trace 绑定到当前开发快照及本地修改指纹', icon: GitBranch, targetView: 'scenarios' },
  'source-snapshot-resolver': { label: '源码快照解析', detail: '生成可复现的 clean/dirty 工作树快照', icon: FolderGit2, targetView: 'sources' },
  'selected-event-opportunity-identification': { label: '已选事件机会识别', detail: '以源码绑定、AST 与 KB 门禁生成机会报告', icon: Check, targetView: 'results' },
  'kb-retrieval': { label: '知识库检索', detail: '按固定 manifest 返回带来源的优化或硬件记录', icon: Database, targetView: 'knowledge' },
  'npu-api-semantic-retrieval': { label: 'NPU API 语义检索', detail: '用代码静态信号排列 Top-N NNRT 图算子候选', icon: Network, targetView: 'reasoning' },
  'accelerator-api-semantic-retrieval': { label: '加速器 API 语义检索', detail: '按图像、PCM、JPEG/HEIF 或 HCodec 领域信号排列 GPU/DSP/媒体候选', icon: Network, targetView: 'reasoning' },
  'hardware-unit-routing': { label: '规则路由审计基线', detail: '保留确定性规则用于离线对照与回归，不进入生产模型输入或决定 API 目录', icon: Network, targetView: 'reasoning' },
  'device-capability-probe': { label: '真机能力探测', detail: '使用固定 HDC 只读白名单记录设备时刻观察', icon: HardDrive, targetView: 'reasoning' },
  'model-device-dispatch-assessment': { label: '模型下发判断', detail: '由模型形成单元级语义下发合同，不依赖参考源码文本匹配', icon: BrainCircuit, targetView: 'reasoning' },
  'registered-device-validation-planner': { label: '真机实测方案', detail: '将模型下发合同或已登记参考实现绑定到设备基线与受控配方', icon: FlaskConical, targetView: 'reasoning' },
  'registered-device-validation-runner': { label: '受控真机配方执行', detail: '用户明确确认后运行固定配方并记录清理与证据校准', icon: Play, targetView: 'reasoning' },
  'execution-pitfall-distillation': { label: '执行踩坑提炼', detail: '比较历史执行证据与环境差异，形成可审阅的问题类和处置方案', icon: Lightbulb, targetView: 'experience' },
}

function firstQualityGate(manifest: JsonRecord): string {
  const first = Array.isArray(manifest.quality_gates) ? asRecord(manifest.quality_gates[0]) : {}
  return stringValue(first.requirement, '')
}

function evaluationSummary(manifest: JsonRecord): string {
  const evaluation = asRecord(manifest.evaluation)
  const semantic = stringList(evaluation.semantic_assertions).slice(0, 2)
  const failures = stringList(evaluation.failure_scenarios).slice(0, 2)
  if (semantic.length === 0 && failures.length === 0) return ''
  return [semantic.length > 0 && `语义：${semantic.join('；')}`, failures.length > 0 && `失败：${failures.join('；')}`].filter(Boolean).join('。')
}

function backendSkillItem(manifest: JsonRecord): AnalysisSkillItem | null {
  const id = stringValue(manifest.skill_id, '')
  if (!id) return null
  const presentation = backendSkillPresentation[id] ?? {
    label: id,
    detail: '已登记的后端分析 Skill',
    icon: FileCode2,
    targetView: 'skills' as ViewId,
  }
  const inputSchema = asRecord(manifest.input_schema)
  const outputSchema = asRecord(manifest.output_schema)
  const requiredInputs = stringList(inputSchema.required)
  const requiredOutputs = stringList(outputSchema.required)
  return {
    id,
    ...presentation,
    execution: stringValue(manifest.executor, '本地确定性执行'),
    input: requiredInputs.length > 0 ? requiredInputs.join('、') : '由输入 schema 定义的可选字段',
    output: requiredOutputs.length > 0 ? requiredOutputs.join('、') : '由输出 schema 定义',
    boundary: stringValue(manifest.claim_boundary, '遵守登记的证据边界。'),
    version: stringValue(manifest.version, '—'),
    qualityGate: firstQualityGate(manifest),
    evaluation: evaluationSummary(manifest),
    iteration: stringList(asRecord(manifest.evolution).event_ids).length > 0
      ? `已关联演进记录：${stringList(asRecord(manifest.evolution).event_ids).join('、')}`
      : '已纳入统一 Skill 治理基线。',
  }
}

function enrichSkillItem(item: AnalysisSkillItem, manifest?: JsonRecord): AnalysisSkillItem {
  const local = frontendSkillGovernance[item.id] ?? localSkillGovernance[item.id]
  return {
    ...item,
    version: stringValue(manifest?.version, item.version ?? local?.version ?? '1.0.0'),
    qualityGate: firstQualityGate(manifest ?? {}) || item.qualityGate || local?.qualityGate || '遵守声明的证据边界。',
    evaluation: evaluationSummary(manifest ?? {}) || item.evaluation || local?.evaluation || '版本化语义与失败场景回归。',
    iteration: item.iteration || local?.iteration || (stringList(asRecord(manifest?.evolution).event_ids).length > 0
      ? `已关联演进记录：${stringList(asRecord(manifest?.evolution).event_ids).join('、')}`
      : '已纳入统一 Skill 治理基线。'),
  }
}

function analysisTime(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value.slice(0, 16)
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(parsed)
}

function traceModuleForLabel(value: unknown) {
  const label = typeof value === 'string' ? value.trim().toLocaleLowerCase() : ''
  if (!label || label === 'unknown' || label === '[no process]') return undefined
  return traceModuleRules.find((rule) => rule.tokens.some((token) => label.includes(token)))
}

function clusterExplorerSummary(cluster: TraceCluster, hotspots: HotspotCandidate[]): ClusterExplorerSummary {
  const serverSummary = asRecord(cluster.module_summary)
  const serverModuleNames = stringList(serverSummary.module_names)
  const serverModuleKeys = stringList(serverSummary.module_keys)
  const threadRefs = Array.isArray(cluster.thread_refs) ? cluster.thread_refs.map(asRecord) : []
  const topEdges = Array.isArray(asRecord(cluster.edge_summary).top_edges) ? asRecord(cluster.edge_summary).top_edges as JsonRecord[] : []
  const labels = [
    ...threadRefs.flatMap((thread) => [thread.task, thread.process]),
    ...topEdges.map((edge) => edge.edge),
    ...hotspots.map(hotspotName),
  ]
  const fallbackFromTrace = labels.find((label) => typeof label === 'string' && label.trim() && label !== 'unknown')
  const topFunctionName = stringValue(serverSummary.top_function_name, stringValue(fallbackFromTrace, 'unknown'))
  const fallbackRules = labels.map(traceModuleForLabel).filter((rule): rule is typeof traceModuleRules[number] => Boolean(rule))
  const moduleNames = (serverModuleNames.length > 0 ? serverModuleNames : Array.from(new Set(fallbackRules.map((rule) => rule.name)))).map(displayModuleName)
  const moduleKeys = serverModuleKeys.length > 0 ? serverModuleKeys : Array.from(new Set(fallbackRules.map((rule) => rule.key)))
  const systemModuleOnly = typeof serverSummary.system_module_only === 'boolean'
    ? serverSummary.system_module_only
    : moduleKeys.length > 0 && moduleKeys.every((key) => traceModuleRules.find((rule) => rule.key === key)?.system)
  return { moduleNames, moduleKeys, topFunctionName, systemModuleOnly }
}

function hotspotName(hotspot: HotspotCandidate): string {
  return stringValue(asRecord(hotspot.function_ref).symbol, stringValue(hotspot.unresolved_reason, 'unresolved'))
}

function eventMetric(event: TraceEventCandidate): string {
  const metrics = asRecord(event.metrics)
  if (typeof metrics.self_time_estimate_total_duration_ms === 'number') return `${numberValue(metrics.self_time_estimate_total_duration_ms).toFixed(2)} ms 自耗时`
  if (typeof metrics.overlap_total_duration_ms === 'number') return `${numberValue(metrics.overlap_total_duration_ms).toFixed(2)} ms 重叠`
  if (typeof metrics.max_duration_ms === 'number') return `${numberValue(metrics.max_duration_ms).toFixed(2)} ms 最大值`
  if (typeof metrics.max_span_duration_ms === 'number') return `${numberValue(metrics.max_span_duration_ms).toFixed(2)} ms Span`
  if (typeof metrics.wakeup_count === 'number') return `${numberValue(metrics.wakeup_count)} 次唤醒`
  return '追踪证据'
}

function selfTimeMetric(event: TraceEventCandidate): string {
  const value = asRecord(event.metrics).self_time_estimate_total_duration_ms
  return typeof value === 'number' ? `${numberValue(value).toFixed(2)} ms` : '—'
}

function inclusiveTimeMetric(event: TraceEventCandidate): string {
  const value = asRecord(event.metrics).inclusive_total_duration_ms
  return typeof value === 'number' ? `${numberValue(value).toFixed(2)} ms` : '—'
}

function maxSelfTimeMetric(event: TraceEventCandidate): string {
  const value = asRecord(event.metrics).max_self_time_estimate_duration_ms
  return typeof value === 'number' ? `${numberValue(value).toFixed(2)} ms` : '—'
}

function maxSpanTimeMetric(event: TraceEventCandidate): string {
  const value = asRecord(event.metrics).max_span_duration_ms
  return typeof value === 'number' ? `${numberValue(value).toFixed(2)} ms` : '—'
}

function childCoveredTimeMetric(event: TraceEventCandidate): string {
  const metrics = asRecord(event.metrics)
  const inclusive = metrics.inclusive_total_duration_ms
  const selfTime = metrics.self_time_estimate_total_duration_ms
  if (typeof inclusive !== 'number' || typeof selfTime !== 'number') return '—'
  return `${Math.max(0, inclusive - selfTime).toFixed(2)} ms`
}

function moduleTimeMetric(module: JsonRecord, metric: string): string {
  const value = module[metric]
  return typeof value === 'number' ? `${numberValue(value).toFixed(2)} ms` : '—'
}

function eventOccurrenceCount(event: TraceEventCandidate): string {
  const metrics = asRecord(event.metrics)
  if (typeof metrics.occurrence_count === 'number') return numberValue(metrics.occurrence_count).toLocaleString()
  if (typeof metrics.call_count === 'number') return numberValue(metrics.call_count).toLocaleString()
  return '—'
}

function traceModuleForEvent(event: TraceEventCandidate): JsonRecord {
  return asRecord(event.trace_module)
}

function selfTimeModuleGroups(moduleCatalog: JsonRecord | null): JsonRecord[] {
  const groups = asRecord(moduleCatalog).groups
  return Array.isArray(groups) ? groups.map(asRecord) : []
}

function selfTimeModuleEvidence(event: TraceEventCandidate): string {
  const evidence = asRecord(traceModuleForEvent(event).evidence)
  const macros = stringList(evidence.tag_macros)
  if (macros.length > 0) return macros.map((macro) => macro.replace('HITRACE_TAG_', '')).join(' · ')
  const kind = stringValue(evidence.kind, '')
  const label = stringValue(evidence.label, '')
  return kind && label ? `${kind}: ${label}` : kind || '未分类'
}

function eventHitraceSummary(event: TraceEventCandidate): string {
  const marker = asRecord(event.hitrace_marker)
  if (stringValue(marker.format, '') !== 'hitrace_marker_v1') return ''
  const level = stringValue(asRecord(marker.output_level).name, '')
  const moduleTags = Array.isArray(marker.module_tags) ? marker.module_tags.map(asRecord) : []
  const modules = moduleTags.map((tag) => stringValue(tag.display_name, '')).filter((value) => value && value !== '—')
  const tagText = modules.join(' · ')
  return [level && `HiTrace ${level}`, tagText].filter(Boolean).join(' · ')
}

function bindingClass(value?: string): string {
  if (value === 'exact') return 'success'
  if (value === 'mismatch') return 'danger'
  return 'warning'
}

const stateLabels: Record<string, string> = {
  exact: '精确匹配',
  compatible: '兼容',
  unknown: '未知',
  mismatch: '不匹配',
  available: '可用',
  partial: '部分可用',
  unavailable: '不可用',
  loading: '加载中',
  selected: '已选中',
  pending: '待处理',
  rejected: '已排除',
  clean: '干净',
  dirty: '包含本地修改',
  declared_source: '已声明源码位置',
  symbol_text_candidate: '符号文本候选',
  symbol_text_reference: '符号文本引用',
  instrumentation_point_exact: '精确插桩点',
  instrumentation_point_function_derived: '函数派生插桩点',
  instrumentation_point_pattern: '插桩模式匹配',
  instrumentation_point_candidate: '插桩点候选',
  unresolved: '未解析',
  external_symbol_only: '外部符号信息',
  raw_htrace_symbol_only: '原始追踪符号信息',
  scheduler_task_labels_are_not_source_symbols: '调度标签不是源码符号',
  source_binding_required: '需要精确源码绑定',
  source_location_required: '需要源码位置',
  temporal_evidence_required: '需要时间证据',
  ast_features_required: '需要 AST 特征',
  source_inspection_failed: '源码检查失败',
  no_kb_match: '未命中知识库',
  deferred_by_run_limit: '因运行上限延后',
  development_candidate: '开发候选',
  not_scored: '未评分',
  confirmed: '已确认',
  source_confirmed: '源码确认',
  source_confirmed_integration: '源码集成确认',
  evidence_required: '需要设备证据',
}

const fixtureLabels: Record<string, string> = {
  'captured-raw-htrace': '已采集的原始 .htrace',
  'captured-report-json': '已采集追踪分析报告',
  'direct-report-json': '直接分析报告',
  'hitrace-text-sys': 'HiTrace 文本调度跟踪',
}

function displayState(value: unknown, fallback = '—'): string {
  const state = stringValue(value, '')
  return stateLabels[state] || state || fallback
}

function displayFixtureLabel(fixtureId: string, fallback: string): string {
  return fixtureLabels[fixtureId] ?? fallback
}

function displayModuleName(value: unknown): string {
  const moduleName = stringValue(value, '')
  const known = {
    'FFRT runtime': 'FFRT 运行时',
    'Kernel scheduler': '内核调度',
    'System service': '系统服务',
    'Render / Rosen': '渲染 / Rosen',
    'Graphics / GPU': '图形 / GPU',
    'Media / Codec': '媒体 / 编解码',
    'Network / I-O': '网络 / I/O',
    'Application framework': '应用框架',
    Unclassified: '未分类',
  }
  return known[moduleName as keyof typeof known] || moduleName || '未分类'
}

function displayReason(value: unknown): string {
  const reason = stringValue(value, '')
  const missingScope = reason.match(/Curated candidate source scope\(s\) absent from the selected connector:\s*(.+)\.$/)
  if (missingScope) return `当前已绑定代码仓中未找到匹配的 HiTrace 插桩点；缺少该事件所属源码范围：${missingScope[1]}。`
  const known = {
    'This scheduler label is not a source symbol and cannot be mapped to a code block.': '该调度标签不是源码符号，无法映射到代码块。',
    'No source location could be resolved for this trace event.': '未能为该追踪事件解析源码位置。',
    'No usable source candidates were found.': '未找到可用的源码候选。',
    'AST extraction and KB matching require an exact trace-to-source snapshot binding.': 'AST 提取和知识库匹配需要精确的追踪到源码快照绑定。',
    'Selected event has no valid declared source line for AST extraction.': '所选事件没有可用于 AST 提取的有效声明源码行。',
    'Precise AST extraction did not produce usable function features for this selected event.': '精准 AST 提取没有为所选事件产出可用的函数特征。',
    'AST features were extracted, but no KB pattern passed its deterministic threshold and readiness filter.': '已提取 AST 特征，但没有知识库模式通过确定性阈值和就绪度筛选。',
  }
  return known[reason as keyof typeof known] || reason || '暂无详细原因。'
}

function displayBlockingCondition(value: string): string {
  const code = value.split(':', 1)[0]
  const labels = {
    SOURCE_BINDING_UNKNOWN: '源码绑定未知：所选事件必须先绑定到精确源码快照，才能进入 AST 与知识库匹配。',
    SOURCE_LOCATION_REQUIRED: '需要源码位置：插桩点或符号文本证据只能浏览，不能进入 AST 与知识库匹配。',
    AST_FEATURES_REQUIRED: '需要 AST 特征：当前源码位置没有产出可用的精准 AST 特征。',
    NO_KB_MATCH: '未命中知识库：已提取的 AST 特征没有匹配到符合条件的知识库记录。',
    SOURCE_INSPECTION_FAILED: '源码检查失败：请检查源码快照、路径和编译上下文。',
  }
  return labels[code as keyof typeof labels] || value
}

function displayResolutionMethod(value: unknown): string {
  const method = stringValue(value, '')
  const labels = {
    external_trace_source_location: '上游声明源码位置',
    hitrace_tag_instrumentation_lookup: 'HiTrace 插桩反查',
    hitrace_tag_instrumentation_lookup_and_bounded_symbol_text_search: 'HiTrace 插桩反查与受限符号检索',
    bounded_symbol_text_search: '受限符号文本检索',
    not_applicable: '不适用',
  }
  return labels[method as keyof typeof labels] || method || '未提供'
}

function displayTraceKind(value: unknown): string {
  const kind = stringValue(value, '')
  const labels = {
    external_aggregate: '外部分析聚合',
    external_report_hotspot: '外部报告热点',
    callstack_span: '调用栈 Span',
    scheduler_wakeup_window: '调度唤醒窗口',
    basic_span_candidate: '基础 Span 候选',
  }
  return labels[kind as keyof typeof labels] || kind || '—'
}

function displayProjectMode(value: unknown): string {
  const mode = stringValue(value, '')
  if (mode === 'single_user_single_machine') return '单用户单机版'
  return mode ? mode.replaceAll('_', ' ') : '连接中'
}

function viewEyebrow(view: ViewId): string {
  if (view === 'scenarios') return '追踪场景'
  if (view === 'reasoning') return '机会推理'
  if (view === 'flows') return '应用执行流程'
  if (view === 'scenario_capture') return '负载采样'
  if (view === 'parallel_modeling') return '并行性建模分析'
  if (view === 'workload_features') return '阶段与资源特征'
  if (view === 'hardware_models') return '设备拓扑与代价模型'
  if (view === 'trace_library') return 'Trace 管理'
  if (view === 'optimization_calibration') return '模型与实测校准'
  if (view === 'skills') return '技能列表'
  if (view === 'experience') return '执行经验'
  if (view === 'knowledge') return '优化与硬件知识'
  if (view === 'sources') return '代码仓'
  if (view === 'code_knowledge') return '代码知识库'
  if (view === 'playground_hiperf') return '实验流程 · HiPerf'
  if (view === 'playground_trace') return '实验流程 · Trace'
  if (view === 'analysis_history') return '优化分析记录'
  return '机会报告'
}

function reportStateLabel(value: unknown): string {
  const state = stringValue(value, '')
  return displayState(state, '未知')
}

function reportOpportunityTitle(item: JsonRecord): string {
  const location = asRecord(item.source_location)
  const sourceFunction = stringValue(location.function, stringValue(item.hotspot_id, '未解析热点'))
  const target = typeof item.target_unit === 'string' ? item.target_unit : ''
  return target ? `${sourceFunction} -> ${target}` : sourceFunction
}

function reportOpportunityDetail(item: JsonRecord): string {
  const location = asRecord(item.source_location)
  const path = typeof location.path === 'string' ? location.path : ''
  const line = numberValue(location.line, 0)
  const primary = stringValue(item.recommendation, displayReason(item.reason))
  return path ? `${primary} ${path}${line > 0 ? `:${line}` : ''}` : primary
}

function reportConfidence(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : displayState(value)
}

function fixtureKindLabel(kind: string): string {
  if (kind === 'raw_htrace') return '原始 .htrace'
  if (kind === 'sys_trace_experimental') return '实验性输入'
  return '追踪团队报告'
}

function Metric({ label, value, tone = '' }: { label: string; value: string; tone?: string }) {
  return <div className={`metric ${tone}`}><span>{label}</span><strong>{value}</strong></div>
}

function Pill({ children, tone = 'neutral' }: { children: React.ReactNode; tone?: string }) {
  return <span className={`pill ${tone}`}>{children}</span>
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="empty-state"><span>{title}</span><p>{detail}</p></div>
}

function EventCodePanel({ resolutionArtifact, onSelectSuggestedConnector }: { resolutionArtifact: Artifact; onSelectSuggestedConnector?: (connectorId: string) => void }) {
  const resolution = asRecord(resolutionArtifact.resolution)
  const codeBlock = asRecord(resolutionArtifact.code_block)
  const codeSource = asRecord(codeBlock.source)
  const codePrecheck = asRecord(codeBlock.structural_precheck)
  const codeMatchEvidence = asRecord(codeBlock.match_evidence)
  const astTarget = asRecord(codeMatchEvidence.ast_target)
  const sourceCandidates = Array.isArray(resolutionArtifact.source_candidates) ? resolutionArtifact.source_candidates as JsonRecord[] : []
  const codeExcerpt = Array.isArray(codePrecheck.source_excerpt) ? codePrecheck.source_excerpt as JsonRecord[] : []
  const codeLine = numberValue(codeSource.line, 0)
  const locatedFunction = asRecord(resolutionArtifact.located_function)
  const locatedFunctionName = stringValue(locatedFunction.name, '')
  const astTargetName = stringValue(astTarget.name, '')
  const targetFunctionName = astTargetName || locatedFunctionName
  const targetFunctionStart = numberValue(astTarget.declaration_line, numberValue(astTarget.line, 0))
  const targetFunctionEnd = numberValue(astTarget.body_end_line, 0)
  const matchLines = Array.isArray(codeMatchEvidence.matched_lines) ? codeMatchEvidence.matched_lines.map(asRecord) : []
  const matchesByLine = new Map<number, JsonRecord[]>()
  matchLines.forEach((match) => {
    const line = numberValue(match.line, 0)
    if (line <= 0) return
    matchesByLine.set(line, [...(matchesByLine.get(line) ?? []), match])
  })
  if (codeLine > 0 && !matchesByLine.has(codeLine)) {
    matchesByLine.set(codeLine, [{ line: codeLine, role: 'source_location', label: '已解析的源码位置' }])
  }
  const instrumentationCandidate = asRecord(sourceCandidates[0])
  const instrumentationDetail = asRecord(instrumentationCandidate.instrumentation)
  const labelDerivation = stringValue(instrumentationDetail.label_derivation, '')
  const tagMatch = stringValue(instrumentationDetail.tag_match, '')
  const instrumentation = asRecord(resolutionArtifact.hitrace_instrumentation)
  const resolvedMarker = asRecord(instrumentation.marker)
  const sourceScope = asRecord(instrumentation.source_scope)
  const sourceScopeRoots = stringList(sourceScope.roots).join(', ')
  const missingSourceScopeRoots = stringList(sourceScope.missing_relative_roots).join(', ')
  const sourceConnectorRecommendations = Array.isArray(instrumentation.source_connector_recommendations)
    ? instrumentation.source_connector_recommendations.map(asRecord).filter((item) => stringValue(item.connector_id, '') !== '')
    : []
  const resolvedModules = Array.isArray(resolvedMarker.module_tags)
    ? resolvedMarker.module_tags.map(asRecord).map((tag) => stringValue(tag.display_name, '')).filter((value) => value && value !== '—').join(' · ')
    : ''
  const sourceState = stringValue(resolution.state, 'unresolved')

  return <div className="event-code-block table-shell">
    <div className="event-code-heading"><div><span className="eyebrow">代码块</span><h3>{stringValue(codeSource.path, stringValue(asRecord(resolutionArtifact.event).event_name, '源码未解析'))}</h3></div><Pill tone={sourceState === 'declared_source' || sourceState === 'instrumentation_point_exact' || sourceState === 'instrumentation_point_function_derived' ? 'success' : sourceState === 'unresolved' ? 'warning' : 'neutral'}>{displayState(sourceState)}</Pill></div>
    <div className="event-code-meta"><span>{displayResolutionMethod(resolution.method)}</span><span>{displayState(resolution.trace_binding_match_level)}</span>{targetFunctionName && <span>AST 目标 {targetFunctionName}{targetFunctionStart > 0 ? `:${targetFunctionStart}` : ''}{targetFunctionEnd > 0 ? `-${targetFunctionEnd}` : ''}</span>}{labelDerivation && <span>标签派生 {labelDerivation}</span>}{tagMatch && <span>标签匹配 {tagMatch}</span>}{resolvedModules && <span>HiTrace {resolvedModules}</span>}{sourceScopeRoots && <span>检索范围 {sourceScopeRoots}</span>}{missingSourceScopeRoots && <span>缺失范围 {missingSourceScopeRoots}</span>}{codeLine > 0 && <span>插桩行 {codeLine}</span>}</div>
    {codeExcerpt.length > 0 ? <div className="event-code-source" aria-label="已解析的源代码"><div className="event-code-lines">{codeExcerpt.map((item) => { const line = numberValue(item.line, 0); const matches = matchesByLine.get(line) ?? []; const roles = matches.map((match) => stringValue(match.role, '')); const isInstrumentation = roles.includes('instrumentation_point'); const isFunction = roles.includes('enclosing_function'); return <div className={`event-code-line${isInstrumentation ? ' is-instrumentation-match' : ''}${isFunction ? ' is-function-match' : ''}`} key={`${line}-${stringValue(item.text, '')}`}><span className="event-code-line-number">{String(line || '').padStart(5, ' ')}</span><code>{stringValue(item.text, '')}</code>{matches.length > 0 && <span className="event-code-match-labels">{matches.map((match, index) => <span key={`${stringValue(match.role, 'match')}-${index}`}>{stringValue(match.label, '源码匹配')}</span>)}</span>}</div> })}</div></div> : <div className="event-code-unavailable"><strong>暂无可验证的代码块</strong><span>{displayReason(resolution.reason)}</span>{sourceCandidates.length > 0 && <span>{sourceCandidates.length} 个受限文本搜索候选已保留在证据 artifact 中。</span>}{sourceConnectorRecommendations.length > 0 && <div className="source-connector-suggestions"><div><strong>建议切换代码仓</strong><span>以下已登记代码仓包含当前 HiTrace 模块所需的源码范围。切换不会建立 trace 与源码的对应关系；请重新导入追踪，或在已确认设备构建对应当前工作树后绑定当前追踪，再重新解析事件。</span></div>{sourceConnectorRecommendations.map((recommendation) => { const connectorId = stringValue(recommendation.connector_id, ''); const sourceRoot = stringValue(recommendation.source_root, ''); const coveredScopes = stringList(recommendation.covered_relative_roots).join('、'); const clean = Boolean(recommendation.snapshot_clean); return <div className="source-connector-suggestion" key={connectorId}><div><strong>{stringValue(recommendation.display_name, connectorId)}</strong><span title={sourceRoot}>{sourceRoot}</span><small>{clean ? '工作树干净' : '包含本地修改'}{coveredScopes ? ` · 覆盖 ${coveredScopes}` : ''}</small></div>{onSelectSuggestedConnector && <button className="quiet-button" type="button" title="切换代码仓后需要重新绑定和解析" onClick={() => onSelectSuggestedConnector(connectorId)}><GitBranch size={14} />切换仓</button>}</div> })}</div>}</div>}
  </div>
}

const astEligibleSourceStates = new Set(['declared_source', 'instrumentation_point_exact', 'instrumentation_point_function_derived'])

function TraceSourceBindingPanel({ trace, connector, busy, refreshBusy, onBind, onRefresh }: { trace: Artifact; connector?: Connector; busy: boolean; refreshBusy: boolean; onBind: () => void; onRefresh: () => void }) {
  const binding = asRecord(trace.source_binding)
  const matchLevel = stringValue(binding.match_level, 'unknown')
  const attestation = asRecord(trace.source_binding_attestation)
  const attestationActor = stringValue(attestation.attestation_actor, '')
  const bindingHasLocalChanges = stringValue(binding.snapshot_state, '') === 'dirty'
  if (matchLevel === 'exact') {
    if (!attestationActor) return null
    return <div className="trace-binding-panel confirmed table-shell">
      <div className="trace-binding-panel-icon"><Check size={17} /></div>
      <div className="trace-binding-panel-content">
        <div><span className="eyebrow">源码快照绑定</span><strong>已按{attestationActor === 'agent' ? '页面助手' : '用户'}声明绑定到当前快照</strong></div>
        <p>平台已校验当前源码快照{bindingHasLocalChanges ? '及本地修改内容指纹' : ''}一致；设备构建来源仍以该声明工件为准，并非由平台自动证明。</p>
        <span className="trace-binding-snapshot">绑定工件 {stringValue(attestation.binding_artifact_id, '—')}</span>
      </div>
    </div>
  }
  const snapshotId = stringValue(connector?.snapshot.snapshot_id, '')
  const clean = Boolean(connector?.snapshot.clean)
  const isUnknown = matchLevel === 'unknown'
  const inputLabel = trace.input_kind === 'raw_htrace' ? '原始 .htrace' : '当前追踪输入'
  const canAttest = isUnknown && Boolean(snapshotId)
  const explanation = isUnknown
    ? `${inputLabel} 未携带 source_revision。确认手机构建来自当前工作树后即可绑定；本地修改会一并写入内容指纹，后续源码变更需刷新快照并重新绑定。`
    : matchLevel === 'mismatch'
      ? '该追踪已经声明了另一个源码版本，不能用当前代码仓覆盖。请切换到匹配的代码仓。'
      : '当前源码快照尚不能建立可用于机会识别的 trace 绑定。'

  return <div className="trace-binding-panel table-shell">
    <div className="trace-binding-panel-icon"><GitBranch size={17} /></div>
    <div className="trace-binding-panel-content">
      <div><span className="eyebrow">源码快照绑定</span><strong>{isUnknown ? '需要声明 trace 对应的源码快照' : '源码快照尚不可用于机会识别'}</strong></div>
      <p>{explanation}</p>
      {snapshotId && <span className="trace-binding-snapshot">当前快照 {snapshotId}{clean ? ' · 工作树干净' : ' · 包含本地修改内容指纹'}</span>}
    </div>
    <div className="trace-binding-panel-actions">
      {connector?.kind === 'local' && <button className="icon-button" type="button" disabled={refreshBusy} title="刷新当前工作树快照，无需提交修改" aria-label="刷新当前工作树快照" onClick={onRefresh}>{refreshBusy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}</button>}
      {canAttest && <button className="quiet-button" type="button" disabled={busy} title="仅在已确认设备构建来自当前工作树时使用" onClick={onBind}>{busy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}确认绑定此快照</button>}
    </div>
  </div>
}

function EventOpportunityPanel({ resolutionArtifact, busy, bindingBusy, onIdentify, onBindSourceSnapshot }: { resolutionArtifact: Artifact; busy: boolean; bindingBusy: boolean; onIdentify: () => void; onBindSourceSnapshot: () => void }) {
  const resolution = asRecord(resolutionArtifact.resolution)
  const event = asRecord(resolutionArtifact.event)
  const codeBlock = asRecord(resolutionArtifact.code_block)
  const codeSource = asRecord(codeBlock.source)
  const sourceState = stringValue(resolution.state, 'unresolved')
  const binding = stringValue(resolution.trace_binding_match_level, 'unknown')
  const astReady = astEligibleSourceStates.has(sourceState) && binding === 'exact'
  const eventLabel = stringValue(event.event_name, stringValue(codeSource.path, '已选 Event'))
  const instrumentation = asRecord(resolutionArtifact.hitrace_instrumentation)
  const sourceScope = asRecord(instrumentation.source_scope)
  const missingScopeRoots = stringList(sourceScope.missing_relative_roots)
  const gateDetail = binding !== 'exact'
    ? '先确认该 trace 对应当前源码快照；本地修改会随内容指纹绑定，绑定后需要重新解析事件源码。'
    : astEligibleSourceStates.has(sourceState)
      ? ''
      : missingScopeRoots.length > 0
        ? `当前代码仓缺少事件所属源码范围：${missingScopeRoots.join('、')}。补齐代码仓后重新解析事件。`
        : '当前仅得到文本候选或未解析结果，不能把它当作事件的精确源码位置。'

  return <div className="event-opportunity-action table-shell">
    <div className="event-opportunity-meta"><div><span className="eyebrow">机会识别</span><strong title={eventLabel}>{eventLabel}</strong><span>AST 特征提取 -&gt; 优化知识库匹配</span></div><div className="event-opportunity-status"><Pill tone={astReady ? 'success' : 'warning'}>{astReady ? 'AST / 优化 KB 已就绪' : '证据门禁'}</Pill><Pill tone={bindingClass(binding)}>{displayState(binding)}</Pill></div></div>
    {!astReady && <div className="event-opportunity-gate"><CircleAlert size={15} /><span>{gateDetail}</span>{binding === 'unknown' && <button className="quiet-button" type="button" disabled={bindingBusy} title="仅在已确认设备构建来自当前快照时使用" onClick={onBindSourceSnapshot}>{bindingBusy ? <LoaderCircle size={14} className="spin" /> : <GitBranch size={14} />}绑定当前快照</button>}</div>}
    <button className="primary-button" type="button" title={astReady ? '提取 AST 特征并匹配优化知识库' : '请先满足源码快照与事件源码定位的证据门禁'} disabled={busy || !astReady} onClick={onIdentify}>{busy ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}识别机会</button>
  </div>
}

function HeterogeneousCaseDetailPanel({ detail, onClose }: { detail: StrictCaseDetailResponse; onClose: () => void }) {
  const strictCase = asRecord(detail.case)
  const accelerator = asRecord(strictCase.accelerator)
  const fallback = asRecord(strictCase.cpu_software_fallback)
  const featureSummary = asRecord(strictCase.cpu_code_feature_summary)
  const featureStructure = asRecord(featureSummary.structured_features)
  const memory = asRecord(featureStructure.memory_access)
  const control = asRecord(featureStructure.control_flow)
  const computation = asRecord(featureStructure.computation)
  const parallelism = asRecord(featureStructure.parallelism)
  const astProfile = asRecord(strictCase.cpu_fallback_ast_profile)
  const astFeatures = asRecord(astProfile.ast_features)
  const cpuSource = asRecord(detail.cpu_source)
  const cpuSourceState = stringValue(cpuSource.state, 'not_available')
  const cpuFunction = asRecord(cpuSource.function)
  const sourceInfo = asRecord(cpuSource.source)
  const sourceExcerpt = Array.isArray(cpuSource.excerpt) ? cpuSource.excerpt.map(asRecord) : []
  const descriptionSentences = stringList(featureSummary.sentences)
  const evidenceRefs = stringList(asRecord(strictCase.provenance).references)
  const astAvailable = stringValue(featureSummary.availability, '') === 'ast_available'
  const cpuSourceStateLabel: Record<string, string> = {
    available: '源码与 AST 快照一致',
    source_changed: '源码已变化',
    too_large: '源码范围过大',
    not_available: '无可验证 CPU 源码',
  }

  return <section className="heterogeneous-case-detail table-shell">
    <header className="heterogeneous-case-detail-heading">
      <div><span className="eyebrow">异构案例详情</span><h3>{stringValue(strictCase.name)}</h3><p>{stringValue(strictCase.algorithm)} · {stringValue(strictCase.source_group)}</p></div>
      <button className="icon-button" type="button" title="关闭案例详情" aria-label="关闭案例详情" onClick={onClose}><X size={16} /></button>
    </header>

    <div className="heterogeneous-case-facts">
      <div><span>异构单元</span><strong>{stringValue(strictCase.target_unit)}</strong><small>{stringValue(strictCase.accelerator_kind)}</small></div>
      <div><span>异构实现</span><strong>{stringValue(accelerator.implementation)}</strong><small>{stringValue(accelerator.reference)}</small></div>
      <div><span>CPU/software fallback</span><strong>{stringValue(fallback.implementation)}</strong><small>{stringValue(fallback.execution_domain)} · {stringValue(fallback.reference)}</small></div>
      <div><span>AST 绑定</span><strong>{displayState(featureStructure.binding_status)}</strong><small>compute core：{displayState(featureStructure.compute_core_status)}</small></div>
    </div>

    <section className="heterogeneous-case-feature-summary">
      <div className="case-detail-section-heading"><div><span className="eyebrow">CPU 代码特征</span><h4>可用于异构匹配的证据</h4></div><Pill tone={astAvailable ? 'success' : 'warning'}>{astAvailable ? '函数级 AST 可用' : '无可用函数级 AST'}</Pill></div>
      <div className="case-feature-description">{descriptionSentences.length > 0 ? descriptionSentences.map((sentence, index) => <p key={index}>{sentence}</p>) : <p>该案例尚未生成 CPU 代码特征摘要。</p>}</div>
      <div className="case-feature-grid">
        <div><span>数据类型线索</span><strong>{stringList(featureStructure.data_type_evidence).join('、') || '未从当前 AST 证明'}</strong></div>
        <div><span>访存形态</span><strong>{stringList(memory.classification).join('、') || '未证明'}</strong><small>连续性：{stringValue(memory.continuity)}</small></div>
        <div><span>控制流</span><strong>{numberValue(control.loop_count)} 个循环 · {numberValue(control.branch_count)} 个分支</strong><small>最大嵌套：{numberValue(control.max_loop_nesting)}</small></div>
        <div><span>计算调用</span><strong>{stringList(computation.math_calls).join('、') || '未见可确认数学调用'}</strong><small>{stringList(computation.notable_callees).join('、') || '未见可确认 callee'}</small></div>
        <div><span>并行线索</span><strong>{numberValue(parallelism.openmp_pragma_count)} 个 OpenMP pragma</strong><small>{stringValue(parallelism.classification)}</small></div>
        <div><span>匹配边界</span><strong>{stringValue(featureSummary.matching_guidance)}</strong></div>
      </div>
    </section>

    <section className="heterogeneous-case-source-section">
      <div className="case-detail-section-heading"><div><span className="eyebrow">CPU 实现</span><h4>{stringValue(cpuSource.implementation, stringValue(fallback.implementation))}</h4></div><Pill tone={cpuSourceState === 'available' ? 'success' : 'warning'}>{cpuSourceStateLabel[cpuSourceState] ?? cpuSourceState}</Pill></div>
      <p className="case-source-meta">{stringValue(cpuFunction.signature, stringValue(cpuSource.reference))}</p>
      {cpuSourceState === 'available' && sourceExcerpt.length > 0 ? <pre className="case-source-code">{sourceExcerpt.map((line) => `${String(numberValue(line.line)).padStart(5, ' ')}  ${stringValue(line.text, '')}`).join('\n')}</pre> : <div className="case-source-unavailable"><strong>{stringValue(cpuSource.reason, 'CPU 源码暂不可展示')}</strong>{stringValue(sourceInfo.path, '') && <span>{stringValue(sourceInfo.path)}:{numberValue(sourceInfo.line_start)}-{numberValue(sourceInfo.line_end)}</span>}</div>}
    </section>

    <section className="heterogeneous-case-ast-section">
      <details open={astAvailable} className="case-ast-details">
        <summary>函数级 AST{astAvailable ? ` · ${stringValue(astFeatures.ast_backend)} · ${stringValue(astFeatures.parse_status)}` : ' 不可用'}</summary>
        {astAvailable ? <pre>{JSON.stringify(astFeatures, null, 2)}</pre> : <p>该案例的 CPU fallback 由 runtime provider、factory、后端选择或未解析符号决定，因此没有可展示的唯一函数 AST。</p>}
      </details>
      {evidenceRefs.length > 0 && <details className="case-evidence-details"><summary>源码证据引用（{evidenceRefs.length} 条）</summary><ul>{evidenceRefs.map((reference) => <li key={reference}>{reference}</li>)}</ul></details>}
    </section>
  </section>
}

function NpuApiDetailPanel({ detail, onClose }: { detail: NpuApiDetailResponse; onClose: () => void }) {
  const apiRecord = asRecord(detail.record)
  const semanticFamily = asRecord(apiRecord.semantic_family)
  const codeFeatureMatch = asRecord(apiRecord.code_feature_match)
  const constructionPath = asRecord(apiRecord.construction_path)
  const capabilityBoundary = asRecord(apiRecord.capability_boundary)
  const integration = asRecord(apiRecord.source_integration_evidence)
  const source = asRecord(apiRecord.source_contract)
  const runtimeVerification = stringList(apiRecord.runtime_verification)
  const astSignals = stringList(codeFeatureMatch.ast_signals)
  const dataForms = stringList(codeFeatureMatch.expected_data_forms)
  const operatorAliases = stringList(codeFeatureMatch.operator_aliases)
  const sourceConstraints = stringList(source.source_constraints_en)
  const strictCaseIds = stringList(integration.strict_case_ids)
  const modelLifecycle = stringList(constructionPath.model_lifecycle)
  const recordType = stringValue(apiRecord.record_type)
  const isOperation = recordType === 'npu_operation'
  const sourceLocation = [stringValue(source.source_file, ''), source.source_line ? `:${numberValue(source.source_line)}` : ''].filter(Boolean).join('')

  return <section className="npu-api-detail table-shell">
    <header className="heterogeneous-case-detail-heading">
      <div><span className="eyebrow">NPU API 详情</span><h3>{stringValue(apiRecord.symbol)}</h3><p>{stringValue(apiRecord.one_line_abstract_zh)}</p></div>
      <button className="icon-button" type="button" title="关闭 NPU API 详情" aria-label="关闭 NPU API 详情" onClick={onClose}><X size={16} /></button>
    </header>

    <div className="heterogeneous-case-facts npu-api-facts">
      <div><span>记录类型</span><strong>{isOperation ? 'NNRT 图算子' : '公开 API 入口'}</strong><small>{stringValue(apiRecord.api_layer_label_zh, stringValue(apiRecord.api_layer))}</small></div>
      <div><span>{isOperation ? '语义族' : '工作流职责'}</span><strong>{isOperation ? stringValue(semanticFamily.label_zh) : stringValue(apiRecord.workflow_role)}</strong><small>{isOperation ? `OH_NN_OPS 枚举 #${numberValue(apiRecord.operation_id)}` : stringValue(apiRecord.api_layer)}</small></div>
      <div><span>源码契约</span><strong>{stringValue(source.library, '源码头文件接口')}</strong><small title={sourceLocation}>{sourceLocation || '—'}</small></div>
      <div><span>设备可用性</span><strong>{displayState(capabilityBoundary.target_device_availability, '需要设备证据')}</strong><small>API 存在不等于设备支持</small></div>
    </div>

    <section className="heterogeneous-case-feature-summary">
      <div className="case-detail-section-heading"><div><span className="eyebrow">语义说明</span><h4>{stringValue(apiRecord.name_zh, stringValue(apiRecord.symbol))}</h4></div><Pill tone="warning">完整图与设备验证必需</Pill></div>
      <div className="case-feature-description"><p>{stringValue(apiRecord.detailed_description_zh)}</p></div>
      <div className="npu-api-matching-grid">
        {isOperation && <><div><span>静态代码信号</span><strong>{astSignals.join('、') || '该图算子未定义通用静态信号'}</strong></div><div><span>期望数据形态</span><strong>{dataForms.join('、') || '以 API 源码约束为准'}</strong></div><div><span>精确别名</span><strong>{operatorAliases.join('、') || '不使用名称别名匹配'}</strong><small>族级词只做辅助信号，不作为精确算子证明。</small></div><div><span>独立卸载边界</span><strong>{stringValue(codeFeatureMatch.standalone_offload_note)}</strong></div></>}
        {!isOperation && <div><span>工作流角色</span><strong>{stringValue(apiRecord.workflow_role)}</strong><small>该入口负责构图、设备选择、编译或执行中的一个步骤，不直接证明某个业务函数可卸载。</small></div>}
      </div>
    </section>

    {isOperation && <section className="npu-api-section">
      <div className="case-detail-section-heading"><div><span className="eyebrow">NNRT 构图位置</span><h4>{stringValue(constructionPath.graph_entry_api)}</h4></div></div>
      <ol className="npu-api-step-list">{modelLifecycle.map((step) => <li key={step}><code>{step}</code></li>)}</ol>
    </section>}

    <section className="npu-api-section npu-api-runtime-section">
      <div className="case-detail-section-heading"><div><span className="eyebrow">运行时验证门禁</span><h4>命中 Top-N 后仍需逐项通过</h4></div><Pill tone="warning">evidence_required</Pill></div>
      <p>{stringValue(capabilityBoundary.statement_zh)}</p>
      <ol className="npu-api-step-list">{runtimeVerification.map((step) => <li key={step}>{step}</li>)}</ol>
    </section>

    <section className="npu-api-section">
      <div className="case-detail-section-heading"><div><span className="eyebrow">源码与现有实现证据</span><h4>接口、严格案例与内部 delegate 分开保存</h4></div></div>
      <div className="npu-api-evidence-grid">
        <div><span>SystemCapability</span><strong>{stringValue(source.system_capability)}</strong><small>since {stringValue(source.since)} · {stringValue(source.version)}</small></div>
        <div><span>严格案例关联</span><strong>{strictCaseIds.length > 0 ? strictCaseIds.join('、') : '当前没有精确 NNRT 算子映射'}</strong><small>{stringValue(integration.statement_zh)}</small></div>
      </div>
      <details className="case-evidence-details"><summary>源码接口约束{sourceConstraints.length > 0 ? `（${sourceConstraints.length} 条）` : ''}</summary>{sourceConstraints.length > 0 ? <ul>{sourceConstraints.map((item) => <li key={item}>{item}</li>)}</ul> : <p>当前接口头文件没有提取到额外约束行。</p>}</details>
      <details className="case-evidence-details"><summary>完整源文件接口说明</summary><pre>{stringValue(source.source_doc_detail_en, stringValue(source.declaration, '当前记录没有可展示的 Doxygen 说明。'))}</pre></details>
    </section>
  </section>
}

function AcceleratorApiDetailPanel({ detail, onClose }: { detail: AcceleratorApiDetailResponse; onClose: () => void }) {
  const apiRecord = asRecord(detail.record)
  const semanticFamily = asRecord(apiRecord.semantic_family)
  const codeFeatureMatch = asRecord(apiRecord.code_feature_match)
  const capabilityBoundary = asRecord(apiRecord.capability_boundary)
  const integration = asRecord(apiRecord.source_integration_evidence)
  const source = asRecord(apiRecord.source_contract)
  const runtimeVerification = stringList(apiRecord.runtime_verification)
  const astSignals = stringList(codeFeatureMatch.ast_signals)
  const dataForms = stringList(codeFeatureMatch.expected_data_forms)
  const operatorAliases = stringList(codeFeatureMatch.operator_aliases)
  const strictCaseIds = stringList(integration.strict_case_ids)
  const sourceLocation = [stringValue(source.source_file, ''), source.source_line ? `:${numberValue(source.source_line)}` : ''].filter(Boolean).join('')
  const unitLabels: Record<AcceleratorUnit, string> = { gpu: 'GPU', dsp: 'DSP / 音频效果', media: '媒体硬件加速' }

  return <section className="npu-api-detail table-shell">
    <header className="heterogeneous-case-detail-heading">
      <div><span className="eyebrow">{unitLabels[detail.accelerator_unit]} API 详情</span><h3>{stringValue(apiRecord.symbol)}</h3><p>{stringValue(apiRecord.one_line_abstract_zh)}</p></div>
      <button className="icon-button" type="button" title="关闭加速器 API 详情" aria-label="关闭加速器 API 详情" onClick={onClose}><X size={16} /></button>
    </header>

    <div className="heterogeneous-case-facts npu-api-facts">
      <div><span>加速器单元</span><strong>{unitLabels[detail.accelerator_unit]}</strong><small>{stringValue(apiRecord.accelerator_unit)}</small></div>
      <div><span>语义族</span><strong>{stringValue(semanticFamily.label_zh)}</strong><small>{stringValue(semanticFamily.id)}</small></div>
      <div><span>暴露层级</span><strong>{stringValue(source.exposure, '未声明')}</strong><small title={sourceLocation}>{sourceLocation || '—'}</small></div>
      <div><span>设备可用性</span><strong>{displayState(capabilityBoundary.target_device_availability, '需要设备证据')}</strong><small>接口或源码路径不等于当前设备可用</small></div>
    </div>

    <section className="heterogeneous-case-feature-summary">
      <div className="case-detail-section-heading"><div><span className="eyebrow">语义与静态匹配</span><h4>{stringValue(apiRecord.name_zh, stringValue(apiRecord.symbol))}</h4></div><Pill tone="warning">静态候选</Pill></div>
      <div className="case-feature-description"><p>{stringValue(apiRecord.detailed_description_zh)}</p></div>
      <div className="npu-api-matching-grid">
        <div><span>代码 / AST 信号</span><strong>{astSignals.join('、') || '当前记录未定义通用静态信号'}</strong></div>
        <div><span>期望数据形态</span><strong>{dataForms.join('、') || '以接口和运行时约束为准'}</strong></div>
        <div><span>精确别名</span><strong>{operatorAliases.join('、') || '不使用名称别名匹配'}</strong><small>必须与单元领域信号共同出现。</small></div>
        <div><span>独立卸载边界</span><strong>{stringValue(codeFeatureMatch.standalone_offload_note)}</strong></div>
      </div>
    </section>

    <section className="npu-api-section npu-api-runtime-section">
      <div className="case-detail-section-heading"><div><span className="eyebrow">运行时验证门禁</span><h4>命中 API 候选后仍需逐项通过</h4></div><Pill tone="warning">evidence_required</Pill></div>
      <p>{stringValue(capabilityBoundary.statement_zh)}</p>
      <ol className="npu-api-step-list">{runtimeVerification.map((step) => <li key={step}>{step}</li>)}</ol>
    </section>

    <section className="npu-api-section">
      <div className="case-detail-section-heading"><div><span className="eyebrow">源码与现有实现证据</span><h4>公开 SDK、系统集成与 HDI/VDI 合同分开保存</h4></div></div>
      <div className="npu-api-evidence-grid">
        <div><span>源码契约</span><strong>{stringValue(source.library, '源码接口')}</strong><small>{stringValue(source.system_capability, 'SystemCapability 未由当前记录断言')}</small></div>
        <div><span>严格案例关联</span><strong>{strictCaseIds.length > 0 ? strictCaseIds.join('、') : '当前没有直接 strict case 关联'}</strong><small>{stringValue(integration.statement_zh)}</small></div>
      </div>
      <details className="case-evidence-details"><summary>源码声明或调用片段</summary><pre>{stringValue(source.declaration_or_usage_excerpt, '当前记录没有可展示的源码片段。')}</pre></details>
    </section>
  </section>
}

function CustomApiDetailPanel({ detail, onClose }: { detail: CustomApiRecordDetailResponse; onClose: () => void }) {
  const record = asRecord(detail.record)
  const family = asRecord(record.semantic_family)
  const source = asRecord(record.source_contract)
  const match = asRecord(record.code_feature_match)
  const boundary = asRecord(record.capability_boundary)
  const runtimeVerification = stringList(record.runtime_verification)
  const aliases = stringList(match.operator_aliases)
  const dataForms = stringList(match.expected_data_forms)
  const sourceLocation = `${stringValue(source.source_file)}:${numberValue(source.source_line)}`

  return <section className="npu-api-detail table-shell">
    <header className="heterogeneous-case-detail-heading">
      <div><span className="eyebrow">{detail.knowledge_base.display_name} · API 详情</span><h3>{stringValue(record.symbol)}</h3><p>{stringValue(record.one_line_abstract_zh)}</p></div>
      <button className="icon-button" type="button" title="关闭自建 API 详情" aria-label="关闭自建 API 详情" onClick={onClose}><X size={16} /></button>
    </header>
    <div className="heterogeneous-case-facts npu-api-facts">
      <div><span>异构单元</span><strong>{stringValue(record.accelerator_unit)}</strong><small>用户自建目录</small></div>
      <div><span>语义角色</span><strong>{stringValue(family.label_zh)}</strong><small>{stringValue(family.id)}</small></div>
      <div><span>源码位置</span><strong>{sourceLocation}</strong><small>{stringValue(source.exposure)}</small></div>
      <div><span>证据等级</span><strong>静态源码候选</strong><small>{stringValue(record.evidence_status)}</small></div>
    </div>
    <section className="heterogeneous-case-feature-summary">
      <div className="case-detail-section-heading"><div><span className="eyebrow">静态语义</span><h4>{stringValue(record.symbol)}</h4></div><Pill tone="warning">非精准 AST</Pill></div>
      <div className="case-feature-description"><p>{stringValue(record.detailed_description_zh)}</p></div>
      <div className="npu-api-matching-grid">
        <div><span>词法别名</span><strong>{aliases.join('、') || '未提取'}</strong></div>
        <div><span>数据形态线索</span><strong>{dataForms.join('、') || '未从声明确认'}</strong></div>
        <div><span>分析方法</span><strong>{stringValue(match.analysis_method)}</strong></div>
        <div><span>匹配边界</span><strong>{stringValue(match.standalone_offload_note)}</strong></div>
      </div>
    </section>
    <section className="npu-api-section">
      <div className="case-detail-section-heading"><div><span className="eyebrow">源码声明 / 调用证据</span><h4>{sourceLocation}</h4></div></div>
      <pre className="custom-api-declaration">{stringValue(source.declaration)}</pre>
      {stringValue(source.leading_comment) && <p className="custom-api-source-comment">{stringValue(source.leading_comment)}</p>}
    </section>
    <section className="npu-api-section npu-api-runtime-section">
      <div className="case-detail-section-heading"><div><span className="eyebrow">运行时验证</span><h4>设备能力与收益仍需独立闭环</h4></div><Pill tone="warning">evidence_required</Pill></div>
      <p>{stringValue(boundary.statement_zh)}</p>
      <ol className="npu-api-step-list">{runtimeVerification.map((step) => <li key={step}>{step}</li>)}</ol>
    </section>
  </section>
}

export default function Workbench() {
  const [view, setView] = useState<ViewId>('scenario_capture')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [platformVersion, setPlatformVersion] = useState<PlatformVersion | null>(null)
  const [selectedConnectorId, setSelectedConnectorId] = useState('')
  const [openharmonySdkStatus, setOpenharmonySdkStatus] = useState<OpenHarmonySdkAssetStatus | null>(null)
  const [openharmonySdkConnectorId, setOpenharmonySdkConnectorId] = useState('')
  const [pageActions, setPageActions] = useState<PageAction[]>([])
  const [backendSkillCatalog, setBackendSkillCatalog] = useState<JsonRecord[]>([])
  const [skillVersionLibrary, setSkillVersionLibrary] = useState<SkillVersionLibrary | null>(null)
  const [selectedSkillProfileId, setSelectedSkillProfileId] = useState('')
  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBaseKind>('optimization')
  const [kbSearches, setKbSearches] = useState<Record<SearchableKnowledgeBaseKind, KBSearchResponse | null>>({ optimization: null, hardware: null })
  const [selectedHeterogeneousCaseId, setSelectedHeterogeneousCaseId] = useState<string | null>(null)
  const [selectedHeterogeneousCase, setSelectedHeterogeneousCase] = useState<StrictCaseDetailResponse | null>(null)
  const [npuApiCatalog, setNpuApiCatalog] = useState<NpuApiCatalogResponse | null>(null)
  const [npuApiQuery, setNpuApiQuery] = useState('')
  const [selectedNpuApiRecordId, setSelectedNpuApiRecordId] = useState<string | null>(null)
  const [selectedNpuApiRecord, setSelectedNpuApiRecord] = useState<NpuApiDetailResponse | null>(null)
  const [acceleratorApiUnit, setAcceleratorApiUnit] = useState<AcceleratorUnit>('gpu')
  const [acceleratorApiCatalogs, setAcceleratorApiCatalogs] = useState<Record<AcceleratorUnit, AcceleratorApiCatalogResponse | null>>({ gpu: null, dsp: null, media: null })
  const [acceleratorApiQuery, setAcceleratorApiQuery] = useState('')
  const [selectedAcceleratorApiRecordId, setSelectedAcceleratorApiRecordId] = useState<string | null>(null)
  const [selectedAcceleratorApiRecord, setSelectedAcceleratorApiRecord] = useState<AcceleratorApiDetailResponse | null>(null)
  const [customApiKbCatalog, setCustomApiKbCatalog] = useState<CustomApiKnowledgeBaseCatalogResponse | null>(null)
  const [customApiUnitName, setCustomApiUnitName] = useState('')
  const [customApiConnectorId, setCustomApiConnectorId] = useState('')
  const [customApiQuery, setCustomApiQuery] = useState('')
  const [selectedCustomApiKbId, setSelectedCustomApiKbId] = useState<string | null>(null)
  const [selectedCustomApiKb, setSelectedCustomApiKb] = useState<CustomApiKnowledgeBaseDetailResponse | null>(null)
  const [selectedCustomApiRecordId, setSelectedCustomApiRecordId] = useState<string | null>(null)
  const [selectedCustomApiRecord, setSelectedCustomApiRecord] = useState<CustomApiRecordDetailResponse | null>(null)
  const [activeTrace, setActiveTrace] = useState<Artifact | null>(null)
  const [selection, setSelection] = useState<Artifact | null>(null)
  const [clusterEvents, setClusterEvents] = useState<TraceEventCandidate[]>([])
  const [clusterEventQuality, setClusterEventQuality] = useState<JsonRecord | null>(null)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)
  const [eventSourceResolution, setEventSourceResolution] = useState<Artifact | null>(null)
  const [selfTimeEvents, setSelfTimeEvents] = useState<TraceEventCandidate[]>([])
  const [selfTimeQuality, setSelfTimeQuality] = useState<JsonRecord | null>(null)
  const [selfTimeModuleCatalog, setSelfTimeModuleCatalog] = useState<JsonRecord | null>(null)
  const [expandedSelfTimeModuleKeys, setExpandedSelfTimeModuleKeys] = useState<string[]>([])
  const [selfTimeModuleResults, setSelfTimeModuleResults] = useState<Record<string, SelfTimeDiscoveryResponse>>({})
  const [selfTimeModuleErrors, setSelfTimeModuleErrors] = useState<Record<string, string>>({})
  const [selfTimeLoadingModuleKeys, setSelfTimeLoadingModuleKeys] = useState<string[]>([])
  const [selectedSelfTimeEventId, setSelectedSelfTimeEventId] = useState<string | null>(null)
  const [selfTimeEventSourceResolution, setSelfTimeEventSourceResolution] = useState<Artifact | null>(null)
  const [analysisRun, setAnalysisRun] = useState<Artifact | null>(null)
  const [opportunityReport, setOpportunityReport] = useState<Artifact | null>(null)
  const [inspection, setInspection] = useState<Artifact | null>(null)
  const [query, setQuery] = useState('')
  const [notice, setNotice] = useState<{ tone: 'success' | 'error' | 'info'; text: string } | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [gitUrl, setGitUrl] = useState('')
  const [gitName, setGitName] = useState('')
  const [localPath, setLocalPath] = useState('')
  const [clonePlan, setClonePlan] = useState<ClonePlan | null>(null)
  const [includeUnverified, setIncludeUnverified] = useState(true)
  const [traceInputMode, setTraceInputMode] = useState<TraceInputMode>('external_report')
  const [traceExplorerTab, setTraceExplorerTab] = useState<TraceExplorerTab>('hotspots')
  const [clusterFilter, setClusterFilter] = useState('')
  const [showSystemClusters, setShowSystemClusters] = useState(false)
  const [optimizationWorkflow, setOptimizationWorkflow] = useState<OptimizationWorkflowState>(() => createIdleOptimizationWorkflow())
  const [reasoningInstanceKey, setReasoningInstanceKey] = useState(0)
  const [skillEditorFocusId, setSkillEditorFocusId] = useState('')
  const [reasoningAssistantContext, setReasoningAssistantContext] = useState<ReasoningAssistantContext>({
    analysis_record_id: null,
    trace: null,
    hotspot_limit: 6,
    hotspot_module_count: 0,
    hotspots: [],
    code_unit: null,
    hardware_routing: null,
    blind_baseline: { status: 'not_run', result_artifact_id: null, comparison_artifact_id: null, session_isolated: true },
    strategy_results: [],
    selected_api_record_id: null,
  })
  const [codeKnowledgeAssistantContext, setCodeKnowledgeAssistantContext] = useState<CodeKnowledgeAssistantContext | null>(null)
  const [applicationFlowAssistantContext, setApplicationFlowAssistantContext] = useState<ApplicationFlowAssistantContext | null>(null)
  const [experimentalFlowAssistantContext, setExperimentalFlowAssistantContext] = useState<ExperimentalFlowAssistantContext | null>(null)
  const [scenarioCaptureAssistantContext, setScenarioCaptureAssistantContext] = useState<ScenarioCaptureAssistantContext | null>(null)
  const [parallelModelingAssistantContext, setParallelModelingAssistantContext] = useState<ParallelModelingAssistantContext | null>(null)
  const [workloadFeaturesAssistantContext, setWorkloadFeaturesAssistantContext] = useState<JsonRecord | null>(null)
  const [initialFeaturesDatasetId, setInitialFeaturesDatasetId] = useState<string | undefined>(undefined)
  const [initialFeatureProfileId, setInitialFeatureProfileId] = useState<string | undefined>(undefined)
  const [initialModelingDatasetId, setInitialModelingDatasetId] = useState<string | undefined>(undefined)
  const [initialModelingModelId, setInitialModelingModelId] = useState<string | undefined>(undefined)
  const [modelingHandoffRevision, setModelingHandoffRevision] = useState(0)
  const [analysisHistory, setAnalysisHistory] = useState<TraceAnalysisRecord[]>([])
  const [analysisHistoryLoaded, setAnalysisHistoryLoaded] = useState(false)
  const [initialRouteAnalysisId, setInitialRouteAnalysisId] = useState<string | null>(null)
  const [initialRouteResolved, setInitialRouteResolved] = useState(false)
  const [selectedAnalysisRecordId, setSelectedAnalysisRecordId] = useState<string | null>(null)
  const [reasoningRestoreRequest, setReasoningRestoreRequest] = useState<ReasoningRecordRestoreRequest | null>(null)
  const [workspaceDeviceSnapshotRevision, setWorkspaceDeviceSnapshotRevision] = useState(0)
  const [targetPlatformProfile, setTargetPlatformProfile] = useState<TargetPlatformProfile>(() => createTargetPlatformProfile())
  const uploadRef = useRef<HTMLInputElement>(null)
  const selfTimeRequestRef = useRef(0)
  const selfTimeModuleRequestRefs = useRef<Record<string, number>>({})
  const analysisHistoryRef = useRef<TraceAnalysisRecord[]>([])
  const initialRouteRestoreHandledRef = useRef(false)
  const connectorSelectionRestoredRef = useRef(false)

  useEffect(() => {
    const listener = () => setWorkspaceDeviceSnapshotRevision((current) => current + 1)
    window.addEventListener(deviceValidationSnapshotEvent, listener)
    return () => window.removeEventListener(deviceValidationSnapshotEvent, listener)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [overviewResponse, actionResponse, skillLibraryResponse, initialOptimizationSearch, initialHardwareSearch, customApiCatalogResponse, platformVersionResponse, sdkStatusResponse] = await Promise.all([
        api<Overview>('/v1/overview'),
        api<{ actions: PageAction[]; skills?: JsonRecord[] }>('/v1/page-actions'),
        api<SkillVersionLibrary>('/v1/skill-version-library'),
        api<KBSearchResponse>('/v1/optimization-kb/search', { method: 'POST', body: JSON.stringify({ query: '', include_unverified: false, include_cases: false }) }),
        api<KBSearchResponse>('/v1/hardware-kb/search', { method: 'POST', body: JSON.stringify({ query: '', include_unverified: true, include_cases: false }) }),
        loadCustomApiCatalog<CustomApiKnowledgeBaseCatalogResponse>(),
        api<PlatformVersion>('/v1/platform-version', { cache: 'no-store' }).catch(() => null),
        api<OpenHarmonySdkAssetStatus>('/v1/runtime-assets/openharmony-sdk'),
      ])
      setOverview(overviewResponse)
      setPlatformVersion(platformVersionResponse)
      setPageActions(actionResponse.actions)
      setBackendSkillCatalog(Array.isArray(actionResponse.skills) ? actionResponse.skills.map(asRecord) : [])
      setSkillVersionLibrary(skillLibraryResponse)
      setSelectedSkillProfileId((current) => {
        const requested = current || readSkillProfileId(skillLibraryResponse.default_profile_id)
        return skillLibraryResponse.profiles.some((profile) => profile.profile_id === requested)
          ? requested
          : skillLibraryResponse.default_profile_id
      })
      setKbSearches({ optimization: initialOptimizationSearch, hardware: initialHardwareSearch })
      setCustomApiKbCatalog(customApiCatalogResponse)
      setOpenharmonySdkStatus(sdkStatusResponse)
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '无法连接本地 API' })
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const refreshSkillVersionLibrary = useCallback(async () => {
    const response = await api<SkillVersionLibrary>('/v1/skill-version-library')
    setSkillVersionLibrary(response)
    setSelectedSkillProfileId((current) => {
      const requested = current || readSkillProfileId(response.default_profile_id)
      return response.profiles.some((profile) => profile.profile_id === requested)
        ? requested
        : response.default_profile_id
    })
  }, [])

  useEffect(() => {
    const listener = () => { void refreshSkillVersionLibrary() }
    window.addEventListener('skill-library-updated', listener)
    return () => window.removeEventListener('skill-library-updated', listener)
  }, [refreshSkillVersionLibrary])

  const selectedSkillProfile = useMemo<SkillVersionProfile | null>(() => {
    if (!skillVersionLibrary) return null
    return skillVersionLibrary.profiles.find((profile) => profile.profile_id === selectedSkillProfileId)
      ?? skillVersionLibrary.profiles.find((profile) => profile.profile_id === skillVersionLibrary.default_profile_id)
      ?? null
  }, [selectedSkillProfileId, skillVersionLibrary])

  const selectSkillProfile = useCallback((profileId: string) => {
    if (!skillVersionLibrary?.profiles.some((profile) => profile.profile_id === profileId)) return
    setSelectedSkillProfileId(profileId)
    writeSkillProfileId(profileId)
  }, [skillVersionLibrary])

  useEffect(() => {
    let cancelled = false
    const localRecords = readAnalysisHistory()
    void listAnalysisWorkspaces(40).then((serverRecords) => {
      if (cancelled) return
      const recordsById = new Map(localRecords.map((record) => [record.analysisId, record]))
      serverRecords.forEach(({ analysis_record: serverRecord }) => {
        const localRecord = recordsById.get(serverRecord.analysisId)
        if (!localRecord || serverRecord.updatedAt >= localRecord.updatedAt) recordsById.set(serverRecord.analysisId, serverRecord)
      })
      const records = sortAnalysisHistory(Array.from(recordsById.values()))
      analysisHistoryRef.current = records
      setAnalysisHistory(records)
      writeAnalysisHistory(records)
      setAnalysisHistoryLoaded(true)
    }).catch(() => {
      if (cancelled) return
      analysisHistoryRef.current = localRecords
      setAnalysisHistory(localRecords)
      setAnalysisHistoryLoaded(true)
    })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    setInitialRouteAnalysisId(readAnalysisRouteId())
    setInitialRouteResolved(true)
  }, [])

  useEffect(() => {
    setTargetPlatformProfile(readTargetPlatformProfile())
  }, [])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = asRecord((event as CustomEvent<unknown>).detail)
      const result = asRecord(detail.result)
      const metadata = asRecord(result.knowledge_base)
      const kbId = stringValue(metadata.kb_id, '')
      if (!kbId) return
      setBusyAction('custom-api-catalog')
      void Promise.all([
        loadCustomApiCatalog<CustomApiKnowledgeBaseCatalogResponse>({ force: true }),
        api<CustomApiKnowledgeBaseDetailResponse>(`/v1/custom-api-kbs/${encodeURIComponent(kbId)}`),
      ]).then(([catalog, knowledgeBaseDetail]) => {
        setCustomApiKbCatalog(catalog)
        setSelectedCustomApiKbId(kbId)
        setSelectedCustomApiKb(knowledgeBaseDetail)
        setSelectedCustomApiRecordId(null)
        setSelectedCustomApiRecord(null)
        setKnowledgeBase('custom_api')
        setView('knowledge')
        setNotice({ tone: 'success', text: `${stringValue(metadata.display_name, '自建 API 知识库')} 已生成` })
      }).catch((error) => {
        setNotice({ tone: 'error', text: error instanceof Error ? error.message : '自建 API 知识库已生成，但页面刷新失败' })
      }).finally(() => setBusyAction(null))
    }
    window.addEventListener('custom-api-kb-updated', listener)
    return () => window.removeEventListener('custom-api-kb-updated', listener)
  }, [])

  useEffect(() => {
    if (!overview || connectorSelectionRestoredRef.current) return
    const rememberedConnectorId = readSourceConnectorId()
    setSelectedConnectorId((current) => {
      if (overview.connectors.some((connector) => connector.connector_id === current)) return current
      if (overview.connectors.some((connector) => connector.connector_id === rememberedConnectorId)) return rememberedConnectorId
      return overview.default_connector?.connector_id ?? overview.connectors[0]?.connector_id ?? ''
    })
    connectorSelectionRestoredRef.current = true
  }, [overview])

  useEffect(() => {
    if (!overview || !connectorSelectionRestoredRef.current || !selectedConnectorId) return
    if (overview.connectors.some((connector) => connector.connector_id === selectedConnectorId)) {
      writeSourceConnectorId(selectedConnectorId)
    }
  }, [overview, selectedConnectorId])

  useEffect(() => {
    if (!customApiConnectorId && overview?.default_connector?.connector_id) setCustomApiConnectorId(overview.default_connector.connector_id)
  }, [customApiConnectorId, overview, selectedConnectorId])

  useEffect(() => {
    if (!overview || !openharmonySdkStatus) return
    setOpenharmonySdkConnectorId((current) => {
      if (overview.connectors.some((connector) => connector.connector_id === current)) return current
      const persisted = stringValue(openharmonySdkStatus.selection?.source_connector_id, '')
      if (overview.connectors.some((connector) => connector.connector_id === persisted)) return persisted
      if (overview.connectors.some((connector) => connector.connector_id === selectedConnectorId)) return selectedConnectorId
      return overview.default_connector?.connector_id ?? overview.connectors[0]?.connector_id ?? ''
    })
  }, [openharmonySdkStatus, overview, selectedConnectorId])

  useEffect(() => {
    if (!activeTrace || activeTrace.input_kind !== 'raw_htrace') {
      setSelfTimeEvents([])
      setSelfTimeQuality(null)
      setSelfTimeModuleCatalog(null)
      setExpandedSelfTimeModuleKeys([])
      setSelfTimeModuleResults({})
      setSelfTimeModuleErrors({})
      setSelfTimeLoadingModuleKeys([])
      return
    }
    let cancelled = false
    const requestId = ++selfTimeRequestRef.current
    setSelfTimeEvents([])
    setSelfTimeQuality({ status: 'loading' })
    setSelfTimeModuleCatalog(null)
    setExpandedSelfTimeModuleKeys([])
    setSelfTimeModuleResults({})
    setSelfTimeModuleErrors({})
    setSelfTimeLoadingModuleKeys([])
    selfTimeModuleRequestRefs.current = {}
    setSelectedSelfTimeEventId(null)
    setSelfTimeEventSourceResolution(null)
    void api<SelfTimeDiscoveryResponse>(`/v1/projects/local-default/trace-hotspot-discoveries`, {
      method: 'POST',
      body: JSON.stringify({ analysis_artifact_id: activeTrace.artifact_id, discovery_id: 'event_self_time' }),
    }).then((response) => {
      if (cancelled || requestId !== selfTimeRequestRef.current) return
      setSelfTimeEvents(response.events)
      setSelfTimeQuality(response.quality)
      setSelfTimeModuleCatalog(response.module_catalog ?? null)
    }).catch((error) => {
      if (cancelled || requestId !== selfTimeRequestRef.current) return
      setSelfTimeQuality({ status: 'unavailable', reason: error instanceof Error ? error.message : '自耗时热点提取失败' })
    })
    return () => { cancelled = true }
  }, [activeTrace])

  const activeConnector = overview?.connectors.find((connector) => connector.connector_id === selectedConnectorId) ?? overview?.default_connector ?? undefined
  const connectorId = activeConnector?.connector_id ?? ''
  const openharmonySdkBundle = asRecord(openharmonySdkStatus?.bundle)
  const openharmonySdkSource = asRecord(openharmonySdkStatus?.source)
  const openharmonySdkReady = stringValue(openharmonySdkBundle.status) === 'ready'
  const selectedClusterId = selection?.selected_cluster?.cluster_id
  const clusterExplorerRows = useMemo(() => {
    if (!activeTrace) return []
    const hotspotsByCluster = new Map<string, HotspotCandidate[]>()
    for (const hotspot of activeTrace.hotspot_candidates ?? []) {
      const existing = hotspotsByCluster.get(hotspot.cluster_id) ?? []
      existing.push(hotspot)
      hotspotsByCluster.set(hotspot.cluster_id, existing)
    }
    return (activeTrace.clusters ?? []).map((cluster) => ({
      cluster,
      summary: clusterExplorerSummary(cluster, hotspotsByCluster.get(cluster.cluster_id) ?? []),
    }))
  }, [activeTrace])
  const visibleClusterExplorerRows = useMemo(() => {
    const normalizedFilter = clusterFilter.trim().toLocaleLowerCase()
    return clusterExplorerRows.filter(({ cluster, summary }) => {
      if (!showSystemClusters && summary.systemModuleOnly && cluster.cluster_id !== selectedClusterId) return false
      if (!normalizedFilter) return true
      const edges = asRecord(cluster.edge_summary)
      const searchText = [
        cluster.cluster_id,
        ...summary.moduleNames,
        summary.topFunctionName,
        stringValue(edges.kind, ''),
        ...stringList(edges.top_edges).flatMap((edge) => [edge]),
        ...(Array.isArray(edges.top_edges) ? (edges.top_edges as JsonRecord[]).map((edge) => stringValue(edge.edge, '')) : []),
      ].join(' ').toLocaleLowerCase()
      return searchText.includes(normalizedFilter)
    })
  }, [clusterExplorerRows, clusterFilter, selectedClusterId, showSystemClusters])
  const systemClusterCount = useMemo(
    () => clusterExplorerRows.filter(({ summary }) => summary.systemModuleOnly).length,
    [clusterExplorerRows],
  )
  const selfTimeModules = useMemo(() => selfTimeModuleGroups(selfTimeModuleCatalog), [selfTimeModuleCatalog])
  const allSelfTimeEvents = useMemo(() => {
    const uniqueEvents = new Map<string, TraceEventCandidate>()
    for (const event of selfTimeEvents) uniqueEvents.set(event.event_id, event)
    for (const response of Object.values(selfTimeModuleResults)) {
      for (const event of response.events) uniqueEvents.set(event.event_id, event)
    }
    return Array.from(uniqueEvents.values())
  }, [selfTimeEvents, selfTimeModuleResults])

  function showNotice(tone: 'success' | 'error' | 'info', text: string) {
    setNotice({ tone, text })
  }

  const commitAnalysisHistory = useCallback((records: TraceAnalysisRecord[]) => {
    const next = sortAnalysisHistory(records)
    analysisHistoryRef.current = next
    setAnalysisHistory(next)
    writeAnalysisHistory(next)
  }, [])

  const startTraceAnalysisRecord = useCallback((trace: TraceAnalysisStart): string => {
    const analysisId = randomUuid()
    writeAnalysisRouteId(analysisId)
    commitAnalysisHistory([createTraceAnalysisRecord(analysisId, trace), ...analysisHistoryRef.current])
    void appendAnalysisTraceEvent(analysisId, {
      category: 'workflow',
      operation: 'analysis.record.start',
      phase: 'analysis',
      status: 'started',
      payload: { trace },
    })
    return analysisId
  }, [commitAnalysisHistory])

  const updateTraceAnalysisHotspot = useCallback((analysisId: string, patch: Partial<AnalysisHotspotSummary>) => {
    const updatedAt = new Date().toISOString()
    commitAnalysisHistory(analysisHistoryRef.current.map((item) => item.analysisId === analysisId
      ? { ...item, updatedAt, hotspot: { ...item.hotspot, ...patch } }
      : item))
    void appendAnalysisTraceEvent(analysisId, {
      category: 'workflow',
      operation: 'analysis.hotspot.update',
      phase: 'hotspot',
      status: 'succeeded',
      payload: { patch },
    })
  }, [commitAnalysisHistory])

  const recordTraceAnalysisInference = useCallback((analysisId: string, inference: AnalysisInferenceRecord) => {
    const updatedAt = new Date().toISOString()
    commitAnalysisHistory(analysisHistoryRef.current.map((item) => item.analysisId === analysisId
      ? { ...item, updatedAt, inferences: [inference, ...item.inferences].slice(0, 48) }
      : item))
    void appendAnalysisTraceEvent(analysisId, {
      category: 'workflow',
      operation: 'analysis.inference.record',
      phase: 'inference',
      status: inference.status,
      requestId: inference.inferenceId,
      runId: inference.runId,
      payload: { inference },
    })
  }, [commitAnalysisHistory])

  const updateTraceAnalysisInference = useCallback((analysisId: string, inferenceId: string, patch: Partial<AnalysisInferenceRecord>) => {
    const updatedAt = new Date().toISOString()
    commitAnalysisHistory(analysisHistoryRef.current.map((item) => item.analysisId === analysisId
      ? {
        ...item,
        updatedAt,
        inferences: item.inferences.map((inference) => inference.inferenceId === inferenceId ? { ...inference, ...patch } : inference),
      }
      : item))
    void appendAnalysisTraceEvent(analysisId, {
      category: 'workflow',
      operation: 'analysis.inference.update',
      phase: 'inference',
      status: typeof patch.status === 'string' ? patch.status : 'updated',
      requestId: inferenceId,
      payload: { patch },
    })
  }, [commitAnalysisHistory])

  const recordTraceAnalysisDeviceValidation = useCallback((analysisId: string, validation: AnalysisDeviceValidationRecord) => {
    const updatedAt = new Date().toISOString()
    commitAnalysisHistory(analysisHistoryRef.current.map((item) => {
      if (item.analysisId !== analysisId) return item
      const existing = item.deviceValidations ?? []
      const index = existing.findIndex((candidate) => candidate.validationId === validation.validationId)
      const deviceValidations = index < 0
        ? [validation, ...existing].slice(0, 16)
        : existing.map((candidate, candidateIndex) => candidateIndex === index ? { ...candidate, ...validation } : candidate)
      return { ...item, updatedAt, deviceValidations }
    }))
    void appendAnalysisTraceEvent(analysisId, {
      category: 'workflow',
      operation: 'analysis.device_validation.record',
      phase: 'device_validation',
      status: validation.status,
      requestId: validation.validationId,
      payload: { validation },
    })
  }, [commitAnalysisHistory])

  const updateTargetPlatformProfile = useCallback((profile: TargetPlatformProfile) => {
    setTargetPlatformProfile(profile)
    writeTargetPlatformProfile(profile)
  }, [])

  useEffect(() => {
    const analysisId = reasoningAssistantContext.analysis_record_id?.trim()
    if (!analysisId || !analysisHistoryLoaded) return
    const analysisRecord = analysisHistory.find((item) => item.analysisId === analysisId)
    if (!analysisRecord) return
    const timer = window.setTimeout(() => {
      const deviceSnapshot = readDeviceValidationSnapshot(analysisId)
      void saveAnalysisWorkspace(analysisId, {
        schema_version: 'analysis_workbench_state/v1',
        source_connector_id: analysisRecord.trace.sourceConnectorId,
        analysis_record: analysisRecord,
        workflow: optimizationWorkflow,
        reasoning: reasoningAssistantContext as unknown as Record<string, unknown>,
        target_platform_profile: targetPlatformProfile,
        device_validation_snapshot: deviceSnapshot as unknown as Record<string, unknown> | null,
        selected_skill_profile_id: selectedSkillProfileId,
        saved_by: 'browser',
      }).catch(() => undefined)
    }, 600)
    return () => window.clearTimeout(timer)
  }, [analysisHistory, analysisHistoryLoaded, optimizationWorkflow, reasoningAssistantContext, selectedSkillProfileId, targetPlatformProfile, workspaceDeviceSnapshotRevision])

  const startOptimizationAnalysis = useCallback(() => {
    setView('reasoning')
    setOptimizationWorkflow((current) => {
      if (current.stage !== 'idle') return current
      return {
        runId: randomUuid(),
        revision: current.revision + 1,
        stage: 'select_trace',
        detail: '分析已启动，等待用户选择或导入本轮真实 Trace。',
      }
    })
  }, [])

  const resetOptimizationAnalysis = useCallback(() => {
    clearAnalysisRouteId()
    setOptimizationWorkflow((current) => ({
      ...createIdleOptimizationWorkflow(),
      revision: current.revision + 1,
    }))
    setReasoningRestoreRequest(null)
    setReasoningAssistantContext({
      analysis_record_id: null,
      trace: null,
      hotspot_limit: 6,
      hotspot_module_count: 0,
      hotspots: [],
      code_unit: null,
      hardware_routing: null,
      blind_baseline: { status: 'not_run', result_artifact_id: null, comparison_artifact_id: null, session_isolated: true },
      strategy_results: [],
      selected_api_record_id: null,
    })
    setReasoningInstanceKey((current) => current + 1)
    setView('reasoning')
  }, [])

  const handleOptimizationWorkflowEvent = useCallback((event: OptimizationWorkflowEvent) => {
    setOptimizationWorkflow((current) => {
      if (event.type === 'analysis_record_restored') {
        if (event.restoredWorkflow) {
          return {
            ...event.restoredWorkflow,
            revision: Math.max(current.revision, event.restoredWorkflow.revision) + 1,
            detail: event.detail,
          }
        }
        return {
          runId: randomUuid(),
          revision: current.revision + 1,
          stage: event.restoredResultCount > 0 ? 'complete' : event.restoredRoute ? 'choose_strategy' : event.hasCode ? 'route_hardware' : 'select_hotspot_or_code',
          detail: event.detail,
          traceLabel: event.traceLabel,
          hotspotLabel: event.hotspotLabel,
        }
      }
      if (event.type === 'analysis_record_restore_failed') {
        return {
          ...createIdleOptimizationWorkflow(),
          revision: current.revision + 1,
          detail: `历史记录装载失败：${event.detail}`,
        }
      }
      if (!current.runId) {
        if (event.type === 'trace_selected') {
          return {
            runId: randomUuid(),
            revision: current.revision + 1,
            stage: 'extract_hotspots',
            detail: `已选择真实 Trace：${event.traceLabel}。`,
            traceLabel: event.traceLabel,
          }
        }
        if (event.type === 'hotspot_code_ready') {
          return {
            runId: randomUuid(),
            revision: current.revision + 1,
            stage: 'route_hardware',
            detail: `热点代码已就绪（${event.sourceKind === 'trace_source' ? '真实事件源码' : '用户粘贴代码'}）：${event.hotspotLabel}。请先完成模型语义硬件路由，或明确跳过该步骤。`,
            hotspotLabel: event.hotspotLabel,
          }
        }
        return current
      }
      const next = (stage: OptimizationWorkflowState['stage'], detail: string, patch: Partial<OptimizationWorkflowState> = {}): OptimizationWorkflowState => ({
        ...current,
        ...patch,
        revision: current.revision + 1,
        stage,
        detail,
      })

      if (event.type === 'trace_selected') {
        return current.stage === 'select_trace'
          ? next('extract_hotspots', `已选择真实 Trace：${event.traceLabel}。`, { traceLabel: event.traceLabel, hotspotLabel: undefined, strategyId: undefined })
          : current
      }
      if (event.type === 'hotspots_extracted') {
        if (current.stage !== 'extract_hotspots') return current
        return event.available
          ? next('select_hotspot_or_code', event.detail)
          : next('extract_hotspots', event.detail)
      }
      if (event.type === 'hotspot_code_ready') {
        return current.stage === 'select_hotspot_or_code' || current.stage === 'extract_hotspots' || current.stage === 'select_trace'
          ? next('route_hardware', `热点代码已就绪（${event.sourceKind === 'trace_source' ? '真实事件源码' : '用户粘贴代码'}）：${event.hotspotLabel}。请先完成模型语义硬件路由，或明确跳过该步骤。`, { hotspotLabel: event.hotspotLabel })
          : current
      }
      if (event.type === 'hardware_route_ready') {
        return next('choose_strategy', event.routeOrigin === 'user_manual'
          ? '已记录人工指定的硬件目录；现在可运行硬件机会推理，或直接选择 API 方向。'
          : current.stage === 'complete' || current.stage === 'strategy_running'
            ? '已根据后续证据生成新的模型语义路由；旧策略保留为历史记录，现在需要重新运行机会推理。'
            : '模型语义硬件路由已完成；现在可运行硬件机会推理，或直接选择 API 方向。', { strategyId: undefined })
      }
      if (event.type === 'hardware_route_skipped') {
        return current.stage === 'route_hardware'
          ? next('choose_strategy', '已跳过硬件路由；当前没有硬件机会推理目录，可返回选择硬件或并行优化。')
          : current
      }
      if (event.type === 'manual_opportunity_selected') {
        return next('complete', event.detail, { strategyId: undefined })
      }
      if (event.type === 'strategy_selected') {
        return current.stage === 'choose_strategy'
          ? next('strategy_running', `已选择 ${event.strategyId} 策略，正在加载其分析 Skill 并执行推理。`, { strategyId: event.strategyId })
          : current
      }
      if (event.type === 'strategy_completed') {
        return current.stage === 'strategy_running' && current.strategyId === event.strategyId
          ? next('complete', event.detail)
          : current
      }
      if (event.type === 'strategy_failed') {
        return current.stage === 'strategy_running' && current.strategyId === event.strategyId
          ? next('choose_strategy', `策略未完成：${event.detail}。请选择同一策略重试或改用另一策略。`, { strategyId: undefined })
          : current
      }
      return current
    })
  }, [])

  const loadAnalysisRecordToWorkbench = useCallback(async (analysis: TraceAnalysisRecord) => {
    writeAnalysisRouteId(analysis.analysisId)
    setSelectedAnalysisRecordId(analysis.analysisId)
    setOptimizationWorkflow((current) => ({
      runId: randomUuid(),
      revision: current.revision + 1,
      stage: 'select_trace',
      detail: `正在恢复历史工作台：${analysis.trace.label}。`,
      traceLabel: analysis.trace.label,
    }))

    let workspace: AnalysisWorkspaceSnapshot | null = null
    try {
      workspace = await readAnalysisWorkspace(analysis.analysisId)
    } catch {
      // Records created before workspace snapshots remain recoverable from
      // immutable artifacts and the browser history projection.
    }
    const persistedRecord = workspace?.snapshot.analysis_record
    const restoredAnalysis = persistedRecord?.analysisId === analysis.analysisId ? persistedRecord : analysis
    const historyConnectorId = restoredAnalysis.trace.sourceConnectorId
    const availableConnectorIds = new Set([
      ...(overview?.connectors ?? []).map((connector) => connector.connector_id),
      overview?.default_connector?.connector_id,
    ].filter((value): value is string => Boolean(value)))
    if (!historyConnectorId || !availableConnectorIds.has(historyConnectorId)) {
      setNotice({ tone: 'error', text: '该历史记录绑定的代码仓当前不可用，无法安全装载。请先在“代码仓”中恢复或重新连接对应代码仓。' })
      return
    }
    const frozenProfileId = workspace?.snapshot.selected_skill_profile_id || [...restoredAnalysis.inferences]
      .reverse()
      .map((inference) => inference.context.skillProfile?.profileId ?? '')
      .find(Boolean)
    if (frozenProfileId) {
      if (skillVersionLibrary?.profiles.some((profile) => profile.profile_id === frozenProfileId)) {
        setSelectedSkillProfileId(frozenProfileId)
        writeSkillProfileId(frozenProfileId)
      } else {
        setNotice({ tone: 'info', text: `该记录使用的 Skill 配置 ${frozenProfileId} 当前未安装；历史结果仍可审计，但新推理将使用当前配置。` })
      }
    }
    setSelectedConnectorId(historyConnectorId)
    if (workspace?.snapshot.target_platform_profile?.schema_version === 'target_platform_profile/v1') {
      updateTargetPlatformProfile(workspace.snapshot.target_platform_profile)
    }
    const restoredDeviceSnapshot = normalizeDeviceValidationSnapshot(
      workspace?.snapshot.device_validation_snapshot,
      restoredAnalysis.analysisId,
    )
    if (restoredDeviceSnapshot) storeDeviceValidationSnapshot(restoredDeviceSnapshot)
    if (workspace?.snapshot.workflow?.stage) {
      setOptimizationWorkflow(workspace.snapshot.workflow as OptimizationWorkflowState)
    }
    if (persistedRecord) {
      commitAnalysisHistory([
        restoredAnalysis,
        ...analysisHistoryRef.current.filter((item) => item.analysisId !== restoredAnalysis.analysisId),
      ])
    }
    setReasoningRestoreRequest({ requestId: randomUuid(), analysis: restoredAnalysis, workspace })
    setView('reasoning')
    window.dispatchEvent(new CustomEvent('analysis-agent-session-restore-request', {
      detail: {
        analysis_id: restoredAnalysis.analysisId,
        run_id: workspace?.agent_session?.run?.run_id ?? '',
      },
    }))
  }, [commitAnalysisHistory, overview, skillVersionLibrary, updateTargetPlatformProfile])

  useEffect(() => {
    if (initialRouteRestoreHandledRef.current || !initialRouteResolved || !analysisHistoryLoaded || !overview) return
    initialRouteRestoreHandledRef.current = true
    if (!initialRouteAnalysisId) return
    const record = analysisHistoryRef.current.find((item) => item.analysisId === initialRouteAnalysisId)
    if (!record) {
      clearAnalysisRouteId()
      return
    }
    void loadAnalysisRecordToWorkbench(record)
  }, [analysisHistoryLoaded, initialRouteAnalysisId, initialRouteResolved, loadAnalysisRecordToWorkbench, overview])

  const importTraceFromAssistant = useCallback(async (file: File): Promise<{ artifact: Artifact }> => {
    const name = file.name.toLowerCase()
    if (!name.endsWith('.htrace') && !name.endsWith('.json')) {
      throw new Error('页面助手只接受原始 .htrace 或 Trace 团队 JSON 分析报告。')
    }
    if (!connectorId) throw new Error('请先选择一个代码仓，再导入真实 Trace。')
    startOptimizationAnalysis()
    setView('reasoning')
    setBusyAction('assistant-trace-upload')
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('source_connector_id', connectorId)
      const response = await upload<{ artifact: Artifact }>('/v1/projects/local-default/trace-imports/upload', formData)
      window.setTimeout(() => window.dispatchEvent(new CustomEvent('reasoning-lab-action', {
        detail: { action: 'assistant_import_trace', artifact: response.artifact },
      })), 0)
      void api('/v1/agent-action-audits', {
        method: 'POST',
        body: JSON.stringify({
          action_id: 'assistant.import_pending_trace',
          conversation_id: 'browser-agent-local',
          source_connector_id: connectorId,
          input_sha256: await digest({ name: file.name, size: file.size, type: file.type, last_modified: file.lastModified }),
          output_sha256: await digest({ artifact_id: response.artifact.artifact_id }),
          result: 'succeeded',
        }),
      }).catch(() => undefined)
      return response
    } finally {
      setBusyAction(null)
    }
  }, [connectorId, startOptimizationAnalysis])

  const pasteHotspotCodeFromAssistant = useCallback(async (input: { code: string; functionHint?: string; eventName?: string }): Promise<{ artifact: Artifact; analysisId: string }> => {
    if (!connectorId) throw new Error('请先选择一个代码仓，再保存热点代码。')
    if (!input.code.trim()) throw new Error('没有可保存的热点代码。')
    if (input.code.length > 16 * 1024) throw new Error('热点代码不能超过 16 KiB；请只粘贴目标函数或最小可分析片段。')
    setView('reasoning')
    setBusyAction('assistant-hotspot-code')
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/hotspot-code/paste', {
        method: 'POST',
        body: JSON.stringify({
          code: input.code,
          function_hint: input.functionHint ?? '',
          event_name: input.eventName ?? '',
          source_connector_id: connectorId,
        }),
      })
      const hotspot = asRecord(response.artifact.hotspot)
      const label = input.eventName?.trim()
        || input.functionHint?.trim()
        || stringValue(hotspot.event_name, '')
        || stringValue(hotspot.function_hint, '')
        || '用户粘贴代码'
      const analysisId = reasoningAssistantContext.analysis_record_id || startTraceAnalysisRecord({
        artifactId: response.artifact.artifact_id,
        label: `手动代码：${label}`,
        inputKind: 'manual_code',
        sourceConnectorId: connectorId,
      })
      updateTraceAnalysisHotspot(analysisId, {
        eventName: label,
        sourceKind: 'pasted_code',
        codeArtifactId: response.artifact.artifact_id,
      })
      await requestReasoningLabAction({
        action: 'assistant_paste_code',
        artifact: response.artifact,
        analysis_id: analysisId,
        hints: { function_hint: input.functionHint ?? '', event_name: input.eventName ?? '' },
      }, 30_000)
      void api('/v1/agent-action-audits', {
        method: 'POST',
        body: JSON.stringify({
          action_id: 'assistant.paste_hotspot_code',
          conversation_id: 'browser-agent-local',
          source_connector_id: connectorId,
          input_sha256: await digest({ code: input.code, function_hint: input.functionHint ?? '', event_name: input.eventName ?? '' }),
          output_sha256: await digest({ artifact_id: response.artifact.artifact_id }),
          result: 'succeeded',
        }),
      }).catch(() => undefined)
      return { ...response, analysisId }
    } finally {
      setBusyAction(null)
    }
  }, [connectorId, reasoningAssistantContext.analysis_record_id, startTraceAnalysisRecord, updateTraceAnalysisHotspot])

  function resetTraceSelection() {
    setSelection(null)
    setClusterEvents([])
    setClusterEventQuality(null)
    setSelectedEventId(null)
    setEventSourceResolution(null)
    setSelfTimeEvents([])
    setSelfTimeQuality(null)
    setSelfTimeModuleCatalog(null)
    setExpandedSelfTimeModuleKeys([])
    setSelfTimeModuleResults({})
    setSelfTimeModuleErrors({})
    setSelfTimeLoadingModuleKeys([])
    setSelectedSelfTimeEventId(null)
    setSelfTimeEventSourceResolution(null)
    setAnalysisRun(null)
    setOpportunityReport(null)
  }

  function selectSuggestedConnector(nextConnectorId: string) {
    const connector = overview?.connectors.find((item) => item.connector_id === nextConnectorId)
    if (!connector) {
      showNotice('error', '建议的代码仓已不可用，请在代码仓页面重新登记。')
      return
    }
    setSelectedConnectorId(nextConnectorId)
    setEventSourceResolution(null)
    setSelfTimeEventSourceResolution(null)
    showNotice('info', `已切换到 ${connector.display_name}。请重新导入追踪，或在确认手机构建对应当前工作树后绑定当前追踪，再重新解析事件。`)
  }

  async function loadSelfTimeEvents(trace = activeTrace, moduleKey = '') {
    if (!trace) throw new Error('请先导入追踪场景')
    if (trace.input_kind !== 'raw_htrace') {
      if (!moduleKey) {
        setSelfTimeEvents([])
        setSelfTimeQuality({ status: 'unavailable', reason: '自耗时热点目前只适用于原始 .htrace 输入。' })
        setSelfTimeModuleCatalog(null)
      }
      return { events: [] as TraceEventCandidate[], quality: { status: 'unavailable' } as JsonRecord } as SelfTimeDiscoveryResponse
    }
    if (moduleKey) {
      const requestId = (selfTimeModuleRequestRefs.current[moduleKey] ?? 0) + 1
      selfTimeModuleRequestRefs.current[moduleKey] = requestId
      setSelfTimeLoadingModuleKeys((previous) => previous.includes(moduleKey) ? previous : [...previous, moduleKey])
      setSelfTimeModuleErrors((previous) => {
        const { [moduleKey]: _, ...rest } = previous
        return rest
      })
      try {
        const response = await api<SelfTimeDiscoveryResponse>(`/v1/projects/local-default/trace-hotspot-discoveries`, {
          method: 'POST',
          body: JSON.stringify({ analysis_artifact_id: trace.artifact_id, discovery_id: 'event_self_time', module_key: moduleKey }),
        })
        if (selfTimeModuleRequestRefs.current[moduleKey] === requestId) {
          setSelfTimeModuleResults((previous) => ({ ...previous, [moduleKey]: response }))
          setSelfTimeModuleCatalog((previous) => response.module_catalog ?? previous)
        }
        return response
      } catch (error) {
        if (selfTimeModuleRequestRefs.current[moduleKey] === requestId) {
          setSelfTimeModuleErrors((previous) => ({ ...previous, [moduleKey]: error instanceof Error ? error.message : '模块事件提取失败' }))
        }
        throw error
      } finally {
        if (selfTimeModuleRequestRefs.current[moduleKey] === requestId) {
          setSelfTimeLoadingModuleKeys((previous) => previous.filter((key) => key !== moduleKey))
        }
      }
    }
    const requestId = ++selfTimeRequestRef.current
    const response = await api<SelfTimeDiscoveryResponse>(`/v1/projects/local-default/trace-hotspot-discoveries`, {
      method: 'POST',
      body: JSON.stringify({ analysis_artifact_id: trace.artifact_id, discovery_id: 'event_self_time' }),
    })
    if (requestId !== selfTimeRequestRef.current) return response
    setSelfTimeEvents(response.events)
    setSelfTimeQuality(response.quality)
    setSelfTimeModuleCatalog(response.module_catalog ?? null)
    return response
  }

  function toggleSelfTimeModule(moduleKey: string) {
    const isExpanded = expandedSelfTimeModuleKeys.includes(moduleKey)
    setExpandedSelfTimeModuleKeys((previous) => isExpanded ? previous.filter((key) => key !== moduleKey) : [...previous, moduleKey])
    if (!isExpanded && !selfTimeModuleResults[moduleKey] && !selfTimeLoadingModuleKeys.includes(moduleKey)) {
      void loadSelfTimeEvents(activeTrace, moduleKey).catch(() => undefined)
    }
  }

  async function loadClusterEvents(clusterId: string, trace = activeTrace) {
    if (!trace) throw new Error('请先导入追踪场景')
    const response = await api<{ events: TraceEventCandidate[]; quality: JsonRecord }>(`/v1/projects/local-default/trace-events`, {
      method: 'POST',
      body: JSON.stringify({ analysis_artifact_id: trace.artifact_id, cluster_id: clusterId }),
    })
    setClusterEvents(response.events)
    setClusterEventQuality(response.quality)
    setSelectedEventId(null)
    setEventSourceResolution(null)
    return response
  }

  async function resolveTraceEvent(event: TraceEventCandidate) {
    if (!activeTrace || !selectedClusterId) throw new Error('请先选择唤醒链簇')
    setTraceExplorerTab('clusters')
    setSelectedEventId(event.event_id)
    setEventSourceResolution(null)
    setBusyAction(`event:${event.event_id}`)
    try {
      const response = await api<{ artifact: Artifact }>(`/v1/projects/local-default/trace-events/resolve-source`, {
        method: 'POST',
        body: JSON.stringify({ analysis_artifact_id: activeTrace.artifact_id, cluster_id: selectedClusterId, event_id: event.event_id }),
      })
      setEventSourceResolution(response.artifact)
      const resolution = asRecord(response.artifact.resolution)
      if (resolution.state === 'unresolved') showNotice('info', displayReason(resolution.reason))
      return response.artifact
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '事件源码解析失败')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function resolveSelfTimeEvent(event: TraceEventCandidate) {
    if (!activeTrace || activeTrace.input_kind !== 'raw_htrace') throw new Error('请先导入原始 .htrace')
    setTraceExplorerTab('hotspots')
    setSelectedSelfTimeEventId(event.event_id)
    setSelfTimeEventSourceResolution(null)
    setBusyAction(`self-time-event:${event.event_id}`)
    try {
      const response = await api<{ artifact: Artifact }>(`/v1/projects/local-default/trace-hotspot-discoveries/resolve-source`, {
        method: 'POST',
        body: JSON.stringify({
          analysis_artifact_id: activeTrace.artifact_id,
          discovery_id: 'event_self_time',
          event_id: event.event_id,
          module_key: stringValue(traceModuleForEvent(event).module_key, '') || undefined,
        }),
      })
      setSelfTimeEventSourceResolution(response.artifact)
      const resolution = asRecord(response.artifact.resolution)
      if (resolution.state === 'unresolved') showNotice('info', displayReason(resolution.reason))
      return response.artifact
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '自耗时事件源码解析失败')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function attestActiveTraceSourceSnapshot(attestationActor: 'user' | 'agent' = 'user', attestationNote = '') {
    if (!activeTrace) throw new Error('请先导入追踪场景')
    if (!activeConnector) throw new Error('请先选择代码仓')
    const actionKey = 'trace-source-binding'
    setBusyAction(actionKey)
    try {
      const response = await api<{ artifact: Artifact; binding_artifact: Artifact }>('/v1/projects/local-default/trace-source-bindings', {
        method: 'POST',
        body: JSON.stringify({
          analysis_artifact_id: activeTrace.artifact_id,
          source_connector_id: activeConnector.connector_id,
          source_snapshot_id: activeConnector.snapshot.snapshot_id,
          attestation_actor: attestationActor,
          attestation_note: attestationNote,
        }),
      })
      setActiveTrace(response.artifact)
      await refresh()
      resetTraceSelection()
      setClusterFilter('')
      setShowSystemClusters(false)
      showNotice('success', '已创建源码快照绑定；请重新选择并解析事件源码')
      return response
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '源码快照绑定失败')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function importFixture(fixtureId: string) {
    setBusyAction(`fixture:${fixtureId}`)
    try {
      const response = await api<{ artifact: Artifact }>(`/v1/projects/local-default/fixtures/${fixtureId}/import`, { method: 'POST', body: JSON.stringify({ source_connector_id: connectorId }) })
      setActiveTrace(response.artifact)
      resetTraceSelection()
      setClusterFilter('')
      setShowSystemClusters(false)
      setTraceExplorerTab(response.artifact.input_kind === 'sys_trace_experimental' ? 'clusters' : 'hotspots')
      setView('scenarios')
      showNotice('success', `已导入 ${displayFixtureLabel(fixtureId, fixtureId)}`)
      return response.artifact
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '追踪样例导入失败')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function clusterFixture(fixtureId: string) {
    setBusyAction(`cluster:${fixtureId}`)
    try {
      const response = await api<{ artifact: Artifact }>(`/v1/projects/local-default/fixtures/${fixtureId}/cluster`, { method: 'POST', body: JSON.stringify({ source_connector_id: connectorId }) })
      setActiveTrace(response.artifact)
      resetTraceSelection()
      setClusterFilter('')
      setShowSystemClusters(false)
      setTraceExplorerTab('clusters')
      setView('scenarios')
      showNotice('info', '实验性唤醒窗口簇已生成')
      return response.artifact
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '实验性追踪聚类失败')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function selectCluster(clusterId: string, rationale = '从工作台唤醒链簇表选择。', selectionActor: 'user' | 'agent' = 'user') {
    if (!activeTrace) throw new Error('请先导入追踪场景')
    setTraceExplorerTab('clusters')
    setBusyAction(`select:${clusterId}`)
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/cluster-selections', {
        method: 'POST',
        body: JSON.stringify({ analysis_artifact_id: activeTrace.artifact_id, cluster_id: clusterId, rationale, selection_actor: selectionActor }),
      })
      setSelection(response.artifact)
      setClusterEvents([])
      setClusterEventQuality(null)
      setSelectedEventId(null)
      setEventSourceResolution(null)
      setAnalysisRun(null)
      setOpportunityReport(null)
      let eventExtractionNotice: string | null = null
      try {
        await loadClusterEvents(clusterId)
      } catch (error) {
        eventExtractionNotice = error instanceof Error ? `已选择追踪簇，但事件提取不可用：${error.message}` : '已选择追踪簇，但事件提取不可用'
      }
      showNotice(eventExtractionNotice ? 'info' : 'success', eventExtractionNotice ?? '已选择追踪簇')
      return response.artifact
    } finally {
      setBusyAction(null)
    }
  }

  async function startAnalysis() {
    if (!activeTrace || !selection) throw new Error('请选择一个追踪簇')
    setBusyAction('analysis')
    try {
      const response = await api<{ run: Artifact; report: Artifact }>('/v1/projects/local-default/analysis-runs', {
        method: 'POST',
        body: JSON.stringify({ analysis_artifact_id: activeTrace.artifact_id, cluster_selection_artifact_id: selection.artifact_id, include_unverified: includeUnverified }),
      })
      setAnalysisRun(response.run)
      setOpportunityReport(response.report)
      setView('results')
      showNotice('info', `分析完成：${reportStateLabel(response.run.state)}`)
      return response
    } finally {
      setBusyAction(null)
    }
  }

  async function identifySelectedEventOpportunity(sourceResolutionArtifact: Artifact) {
    if (!activeTrace) throw new Error('请先导入追踪场景')
    const actionKey = `event-opportunity:${sourceResolutionArtifact.artifact_id}`
    setBusyAction(actionKey)
    try {
      const response = await api<{ run: Artifact; report: Artifact }>('/v1/projects/local-default/event-opportunity-runs', {
        method: 'POST',
        body: JSON.stringify({
          analysis_artifact_id: activeTrace.artifact_id,
          source_resolution_artifact_id: sourceResolutionArtifact.artifact_id,
          include_unverified: includeUnverified,
        }),
      })
      setAnalysisRun(response.run)
      setOpportunityReport(response.report)
      setView('results')
      showNotice('info', `识别完成：${reportStateLabel(response.run.state)}`)
      return response
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '事件机会识别失败')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function searchKnowledge(
    searchQuery = query,
    includeDevelopment = includeUnverified,
    targetKnowledgeBase: SearchableKnowledgeBaseKind = knowledgeBase === 'optimization' || knowledgeBase === 'hardware' ? knowledgeBase : 'optimization',
  ) {
    setBusyAction(`kb-search:${targetKnowledgeBase}`)
    try {
      const endpoint = targetKnowledgeBase === 'optimization' ? '/v1/optimization-kb/search' : '/v1/hardware-kb/search'
      const result = await api<KBSearchResponse>(endpoint, {
        method: 'POST',
        body: JSON.stringify({
          query: searchQuery,
          include_unverified: includeDevelopment,
          include_cases: false,
        }),
      })
      setKbSearches((previous) => ({ ...previous, [targetKnowledgeBase]: result }))
      if (targetKnowledgeBase === 'optimization') {
        setSelectedHeterogeneousCaseId(null)
        setSelectedHeterogeneousCase(null)
      }
      setKnowledgeBase(targetKnowledgeBase)
      setView('knowledge')
      return result
    } finally {
      setBusyAction(null)
    }
  }

  async function loadNpuApiCatalog(searchQuery = npuApiQuery) {
    setBusyAction('npu-api-catalog')
    try {
      const params = new URLSearchParams()
      if (searchQuery.trim()) params.set('query', searchQuery.trim())
      const suffix = params.size > 0 ? `?${params.toString()}` : ''
      const response = await api<NpuApiCatalogResponse>(`/v1/hardware-kb/npu-api/records${suffix}`)
      setNpuApiCatalog(response)
      setSelectedNpuApiRecordId(null)
      setSelectedNpuApiRecord(null)
      setKnowledgeBase('npu_api')
      setView('knowledge')
      return response
    } finally {
      setBusyAction(null)
    }
  }

  async function openNpuApiRecord(recordId: string) {
    if (recordId === selectedNpuApiRecordId) {
      setSelectedNpuApiRecordId(null)
      setSelectedNpuApiRecord(null)
      return
    }
    setSelectedNpuApiRecordId(recordId)
    setSelectedNpuApiRecord(null)
    setBusyAction(`npu-api-record:${recordId}`)
    try {
      const response = await api<NpuApiDetailResponse>(`/v1/hardware-kb/npu-api/records/${encodeURIComponent(recordId)}`)
      setSelectedNpuApiRecord(response)
      return response
    } catch (error) {
      setSelectedNpuApiRecordId(null)
      showNotice('error', error instanceof Error ? error.message : '无法读取 NPU API 详情')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function loadAcceleratorApiCatalog(unit: AcceleratorUnit = acceleratorApiUnit, searchQuery = acceleratorApiQuery) {
    setBusyAction(`accelerator-api-catalog:${unit}`)
    try {
      const params = new URLSearchParams()
      if (searchQuery.trim()) params.set('query', searchQuery.trim())
      const suffix = params.size > 0 ? `?${params.toString()}` : ''
      const response = await api<AcceleratorApiCatalogResponse>(`/v1/hardware-kb/accelerator-api/${unit}/records${suffix}`)
      setAcceleratorApiCatalogs((previous) => ({ ...previous, [unit]: response }))
      setAcceleratorApiUnit(unit)
      setSelectedAcceleratorApiRecordId(null)
      setSelectedAcceleratorApiRecord(null)
      setKnowledgeBase('accelerator_api')
      setView('knowledge')
      return response
    } finally {
      setBusyAction(null)
    }
  }

  async function openAcceleratorApiRecord(recordId: string) {
    if (recordId === selectedAcceleratorApiRecordId) {
      setSelectedAcceleratorApiRecordId(null)
      setSelectedAcceleratorApiRecord(null)
      return
    }
    setSelectedAcceleratorApiRecordId(recordId)
    setSelectedAcceleratorApiRecord(null)
    const actionKey = `accelerator-api-record:${acceleratorApiUnit}:${recordId}`
    setBusyAction(actionKey)
    try {
      const response = await api<AcceleratorApiDetailResponse>(`/v1/hardware-kb/accelerator-api/${acceleratorApiUnit}/records/${encodeURIComponent(recordId)}`)
      setSelectedAcceleratorApiRecord(response)
      return response
    } catch (error) {
      setSelectedAcceleratorApiRecordId(null)
      showNotice('error', error instanceof Error ? error.message : '无法读取加速器 API 详情')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function loadCustomApiKnowledgeBases(preferredKbId = '') {
    setBusyAction('custom-api-catalog')
    try {
      const catalog = await loadCustomApiCatalog<CustomApiKnowledgeBaseCatalogResponse>({ force: true })
      setCustomApiKbCatalog(catalog)
      const targetId = preferredKbId || selectedCustomApiKbId || catalog.knowledge_bases[0]?.kb_id || ''
      if (targetId && catalog.knowledge_bases.some((item) => item.kb_id === targetId)) {
        const detail = await api<CustomApiKnowledgeBaseDetailResponse>(`/v1/custom-api-kbs/${encodeURIComponent(targetId)}`)
        setSelectedCustomApiKbId(targetId)
        setSelectedCustomApiKb(detail)
      } else {
        setSelectedCustomApiKbId(null)
        setSelectedCustomApiKb(null)
      }
      setSelectedCustomApiRecordId(null)
      setSelectedCustomApiRecord(null)
      setKnowledgeBase('custom_api')
      setView('knowledge')
      return catalog
    } finally {
      setBusyAction(null)
    }
  }

  async function openCustomApiKnowledgeBase(kbId: string, searchQuery = '') {
    if (kbId === selectedCustomApiKbId && !searchQuery && !customApiQuery) {
      setSelectedCustomApiKbId(null)
      setSelectedCustomApiKb(null)
      setSelectedCustomApiRecordId(null)
      setSelectedCustomApiRecord(null)
      return
    }
    setSelectedCustomApiKbId(kbId)
    setSelectedCustomApiKb(null)
    setSelectedCustomApiRecordId(null)
    setSelectedCustomApiRecord(null)
    setBusyAction(`custom-api-kb:${kbId}`)
    try {
      const params = new URLSearchParams()
      if (searchQuery.trim()) params.set('query', searchQuery.trim())
      const suffix = params.size ? `?${params.toString()}` : ''
      const detail = await api<CustomApiKnowledgeBaseDetailResponse>(`/v1/custom-api-kbs/${encodeURIComponent(kbId)}${suffix}`)
      setSelectedCustomApiKb(detail)
      return detail
    } catch (error) {
      setSelectedCustomApiKbId(null)
      showNotice('error', error instanceof Error ? error.message : '无法读取自建 API 知识库')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function openCustomApiRecord(recordId: string) {
    if (!selectedCustomApiKbId) return
    if (recordId === selectedCustomApiRecordId) {
      setSelectedCustomApiRecordId(null)
      setSelectedCustomApiRecord(null)
      return
    }
    setSelectedCustomApiRecordId(recordId)
    setSelectedCustomApiRecord(null)
    setBusyAction(`custom-api-record:${recordId}`)
    try {
      const detail = await api<CustomApiRecordDetailResponse>(`/v1/custom-api-kbs/${encodeURIComponent(selectedCustomApiKbId)}/records/${encodeURIComponent(recordId)}`)
      setSelectedCustomApiRecord(detail)
      return detail
    } catch (error) {
      setSelectedCustomApiRecordId(null)
      showNotice('error', error instanceof Error ? error.message : '无法读取自建 API 详情')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  function requestCustomApiKnowledgeBaseBuild() {
    const unitName = customApiUnitName.trim()
    const connector = overview?.connectors.find((item) => item.connector_id === customApiConnectorId)
    if (!unitName) {
      showNotice('error', '请填写异构单元名称')
      return
    }
    if (!connector) {
      showNotice('error', '请选择一个已登记的代码仓')
      return
    }
    setKnowledgeBase('custom_api')
    setView('knowledge')
    window.dispatchEvent(new CustomEvent('page-agent-autonomous-task', {
      detail: {
        task_kind: 'custom_api_kb',
        message: `请针对已登记代码仓构建一份新的自建异构单元 API 知识库。异构单元名称：${unitName}；source_connector_id：${connector.connector_id}；服务端代码仓路径：${connector.source_root}。先核对 Connector，再完整遵循 custom-accelerator-api-kb-builder Skill：以代码消费方可见的公开 API、算子/能力枚举和 HDI/VDI/IDL 合同为知识库主体，内部 Service/Manager/Builder 方法只能作为实现证据。调用构建工具后检查 quality 及 public contract、operator、internal implementation 计数，质量不合格时不得仅因资产已写入就报告成功。即使同名单元已存在也新建一份，不合并或覆盖平台内置库。`,
        display_message: `针对 ${connector.display_name} 构建 ${unitName.toUpperCase()} API（自建）`,
      },
    }))
    showNotice('info', '构建任务已交给页面助手')
  }

  async function deleteCustomApiKnowledgeBase(kb: CustomApiKnowledgeBaseMetadata) {
    if (!window.confirm(`删除“${kb.display_name}”？该操作只影响这份自建知识库。`)) return
    setBusyAction(`custom-api-delete:${kb.kb_id}`)
    try {
      await api(`/v1/custom-api-kbs/${encodeURIComponent(kb.kb_id)}`, { method: 'DELETE' })
      invalidateAnalysisCatalog(`/v1/custom-api-kbs/${encodeURIComponent(kb.kb_id)}`)
      const catalog = await loadCustomApiCatalog<CustomApiKnowledgeBaseCatalogResponse>({ force: true })
      setCustomApiKbCatalog(catalog)
      if (selectedCustomApiKbId === kb.kb_id) {
        setSelectedCustomApiKbId(null)
        setSelectedCustomApiKb(null)
        setSelectedCustomApiRecordId(null)
        setSelectedCustomApiRecord(null)
      }
      showNotice('info', `已删除 ${kb.display_name}`)
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '删除自建 API 知识库失败')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function openHeterogeneousCase(recordId: string) {
    if (recordId === selectedHeterogeneousCaseId) {
      setSelectedHeterogeneousCaseId(null)
      setSelectedHeterogeneousCase(null)
      return
    }
    setSelectedHeterogeneousCaseId(recordId)
    setSelectedHeterogeneousCase(null)
    setBusyAction(`heterogeneous-case:${recordId}`)
    try {
      const response = await api<StrictCaseDetailResponse>(`/v1/optimization-kb/strict-cases/${encodeURIComponent(recordId)}`)
      setSelectedHeterogeneousCase(response)
      return response
    } catch (error) {
      setSelectedHeterogeneousCaseId(null)
      showNotice('error', error instanceof Error ? error.message : '无法读取异构案例详情')
      throw error
    } finally {
      setBusyAction(null)
    }
  }

  async function registerLocal(path = localPath, displayName = '') {
    if (!path.trim()) throw new Error('请输入本地代码仓路径')
    setBusyAction('local-connector')
    try {
      const response = await api<{ connector: Connector }>('/v1/source-connectors/local', {
        method: 'POST',
        body: JSON.stringify({ source_root: path, display_name: displayName }),
      })
      setSelectedConnectorId(response.connector.connector_id)
      await refresh()
      showNotice('success', `已登记 ${response.connector.display_name}`)
      return response.connector
    } finally {
      setBusyAction(null)
    }
  }

  async function provisionOpenHarmonySdk() {
    const connector = overview?.connectors.find((item) => item.connector_id === openharmonySdkConnectorId)
    if (!connector) throw new Error('请选择一个已登记的 OpenHarmony 代码仓')
    setBusyAction('openharmony-sdk-provision')
    try {
      await api('/v1/runtime-assets/openharmony-sdk/provision', {
        method: 'POST',
        body: JSON.stringify({ source_connector_id: connector.connector_id }),
      })
      const status = await api<OpenHarmonySdkAssetStatus>('/v1/runtime-assets/openharmony-sdk')
      setOpenharmonySdkStatus(status)
      showNotice('success', `已从 ${connector.display_name} 生成紧凑 OpenHarmony SDK`)
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '生成紧凑 OpenHarmony SDK 失败')
    } finally {
      setBusyAction(null)
    }
  }

  async function refreshSourceSnapshot(connector = activeConnector) {
    if (!connector) throw new Error('请先选择代码仓')
    if (connector.kind !== 'local') throw new Error('受控克隆代码仓不支持刷新本地工作树快照')
    const actionKey = `snapshot:${connector.connector_id}`
    setBusyAction(actionKey)
    try {
      const response = await api<{ connector: Connector }>(`/v1/source-connectors/${connector.connector_id}/refresh`, { method: 'POST' })
      setSelectedConnectorId(response.connector.connector_id)
      await refresh()
      showNotice('success', response.connector.snapshot.clean ? '已刷新源码快照' : '已刷新源码快照，已包含本地修改内容指纹')
      return response.connector
    } finally {
      setBusyAction(null)
    }
  }

  async function createClonePlan(url = gitUrl, displayName = gitName) {
    if (!url.trim()) throw new Error('请输入 HTTPS 或 SSH Git URL')
    setBusyAction('clone-plan')
    try {
      const response = await api<{ plan: ClonePlan }>('/v1/source-connectors/git/plan', {
        method: 'POST',
        body: JSON.stringify({ url, display_name: displayName, source_connector_id: connectorId }),
      })
      setClonePlan(response.plan)
      showNotice(response.plan.capacity.can_clone ? 'info' : 'error', response.plan.capacity.can_clone ? '已生成克隆执行计划' : '磁盘空间未通过克隆门禁')
      return response.plan
    } finally {
      setBusyAction(null)
    }
  }

  async function executeClone(planId = clonePlan?.plan_id) {
    if (!planId) throw new Error('请先生成克隆计划')
    setBusyAction('clone-execute')
    try {
      const response = await api<{ connector: Connector; audit: Artifact }>('/v1/source-connectors/git/clone', {
        method: 'POST',
        body: JSON.stringify({ plan_id: planId }),
      })
      setClonePlan(null)
      setSelectedConnectorId(response.connector.connector_id)
      await refresh()
      showNotice('success', `已固定到 ${response.connector.pinned_commit?.slice(0, 12) ?? '提交版本'}`)
      return response
    } finally {
      setBusyAction(null)
    }
  }

  async function handleUpload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (!file) return
    const lowerName = file.name.toLowerCase()
    if (traceInputMode === 'raw_htrace' && !lowerName.endsWith('.htrace')) {
      showNotice('error', '原始 HiTrace 模式只接受 .htrace 文件')
      event.target.value = ''
      return
    }
    if (traceInputMode === 'external_report' && !lowerName.endsWith('.json')) {
      showNotice('error', '追踪团队分析模式只接受 JSON 报告')
      event.target.value = ''
      return
    }
    setBusyAction('upload')
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('source_connector_id', connectorId)
      const response = await upload<{ artifact: Artifact }>('/v1/projects/local-default/trace-imports/upload', formData)
      setActiveTrace(response.artifact)
      resetTraceSelection()
      setClusterFilter('')
      setShowSystemClusters(false)
      setTraceExplorerTab(response.artifact.input_kind === 'sys_trace_experimental' ? 'clusters' : 'hotspots')
      showNotice('success', `已导入 ${file.name}`)
    } catch (error) {
      showNotice('error', error instanceof Error ? error.message : '追踪导入失败')
    } finally {
      setBusyAction(null)
      event.target.value = ''
    }
  }

  async function executePageAction(actionId: string, input: Record<string, unknown>): Promise<unknown> {
    if (actionId.startsWith('device.') && overview?.deployment?.device_validation_enabled === false) {
      throw new Error(overview.deployment.device_validation_message)
    }
    switch (actionId) {
      case 'trace.import_fixture': return importFixture(stringValue(input.fixture_id, traceInputMode === 'raw_htrace' ? 'captured-raw-htrace' : 'captured-report-json'))
      case 'trace.cluster_sys_fixture': return clusterFixture(stringValue(input.fixture_id, 'hitrace-text-sys'))
      case 'workspace.navigate': {
        const nextView = stringValue(input.view, 'scenarios') as ViewId
        if (!navItems.some((item) => item.id === nextView)) throw new Error('未知工作区视图')
        setView(nextView)
        return { view: nextView }
      }
      case 'workspace.read_file': {
        const path = stringValue(input.path, '')
        if (!path) throw new Error('需要代码仓内的相对文件路径')
        return api<JsonRecord>('/v1/workspace-files/read', {
          method: 'POST',
          body: JSON.stringify({
            source_connector_id: connectorId,
            path,
            ...(typeof input.max_bytes === 'number' ? { max_bytes: input.max_bytes } : {}),
          }),
        })
      }
      case 'workspace.write_file': {
        const path = stringValue(input.path, '')
        const content = typeof input.content === 'string' ? input.content : ''
        if (!path) throw new Error('需要代码仓内的相对文件路径')
        const modelConfig = readBrowserModelConfig()
        if (modelConfig.agentExecutionPolicy !== 'full_access') {
          throw new Error('当前页面助手执行策略不允许写入代码仓；请在助手设置中选择全授权。')
        }
        return api<JsonRecord>('/v1/workspace-files/write', {
          method: 'POST',
          body: JSON.stringify({
            source_connector_id: connectorId,
            path,
            content,
            expected_sha256: stringValue(input.expected_sha256, '') || null,
            execution_policy: modelConfig.agentExecutionPolicy,
          }),
        })
      }
      case 'target-platform.set': {
        const profile = createTargetPlatformProfile(
          stringValue(input.chipset_model_id, targetPlatformProfile.chipset.model_id),
          stringValue(input.operating_system_version_id, targetPlatformProfile.operating_system.version_id),
        )
        updateTargetPlatformProfile(profile)
        setView('reasoning')
        return { target_platform_profile: profile }
      }
      case 'reasoning.refresh_traces': {
        setView('reasoning')
        window.setTimeout(() => window.dispatchEvent(new CustomEvent('reasoning-lab-action', { detail: { action: 'refresh_traces' } })), 0)
        return { queued_action: 'refresh_traces' }
      }
      case 'reasoning.load_hotspots': {
        setView('reasoning')
        window.setTimeout(() => window.dispatchEvent(new CustomEvent('reasoning-lab-action', { detail: { action: 'load_hotspots' } })), 0)
        return { queued_action: 'load_hotspots' }
      }
      case 'reasoning.resolve_hotspot': {
        const eventId = stringValue(input.event_id, '')
        if (!eventId) throw new Error('需要真实热点 event_id')
        setView('reasoning')
        return requestReasoningLabAction({ action: 'resolve_hotspot', event_id: eventId }, 2 * 60_000)
      }
      case 'reasoning.select_hotspot_source_candidate': {
        setView('reasoning')
        return requestReasoningLabAction({
          action: 'select_hotspot_source_candidate',
          source_resolution_artifact_id: stringValue(input.source_resolution_artifact_id, ''),
          path: stringValue(input.path, ''),
          line_start: input.line_start,
          line_end: input.line_end,
          function_hint: stringValue(input.function_hint, ''),
          candidate_kind: stringValue(input.candidate_kind, ''),
          relation_to_anchor: stringValue(input.relation_to_anchor, ''),
          rationale: stringValue(input.rationale, ''),
          expected_source_sha256: stringValue(input.expected_source_sha256, '') || null,
          call_path: Array.isArray(input.call_path) ? input.call_path : [],
        }, 2 * 60_000)
      }
      case 'reasoning.paste_hotspot_code': {
        const code = stringValue(input.code, '')
        const response = await pasteHotspotCodeFromAssistant({
          code,
          functionHint: stringValue(input.function_hint, ''),
          eventName: stringValue(input.event_name, ''),
        })
        return { queued_action: 'paste_hotspot_code', artifact_id: response.artifact.artifact_id, analysis_id: response.analysisId }
      }
      case 'reasoning.prepare_hardware_semantic_route': {
        setView('reasoning')
        return requestReasoningLabAction({ action: 'prepare_hardware_semantic_route' }, 2 * 60_000)
      }
      case 'reasoning.run_hardware_semantic_route': {
        setView('reasoning')
        return requestReasoningLabAction({
          action: 'run_hardware_semantic_route',
          context_artifact_id: stringValue(input.context_artifact_id, ''),
          agent_result: asRecord(input.agent_result),
          reason: stringValue(input.reason, ''),
          retry_nonce: stringValue(input.retry_nonce, ''),
        }, 6 * 60_000)
      }
      case 'reasoning.run_blind_analysis': {
        setView('reasoning')
        return requestReasoningLabAction({
          action: 'run_blind_analysis',
          question: stringValue(input.question, ''),
          retry_nonce: stringValue(input.retry_nonce, ''),
        }, 10 * 60_000)
      }
      case 'reasoning.compare_blind_flow': {
        setView('reasoning')
        return requestReasoningLabAction({
          action: 'compare_blind_flow',
          reason: stringValue(input.reason, ''),
          retry_nonce: stringValue(input.retry_nonce, ''),
        }, 10 * 60_000)
      }
      case 'device.discover': {
        setView('reasoning')
        return requestReasoningLabAction({ action: 'device_discover' }, 60_000)
      }
      case 'device.probe': {
        const deviceSerial = stringValue(input.device_serial, '')
        if (!deviceSerial) throw new Error('需要已发现的真机序列号')
        setView('reasoning')
        return requestReasoningLabAction({ action: 'device_probe', device_serial: deviceSerial }, 90_000)
      }
      case 'device.describe_experiment_profiles': {
        const profileId = stringValue(input.profile_id, '')
        const headerQuery = stringValue(input.header_query, '')
        if (profileId && !['native_c', 'native_cpp', 'gpu_gles3', 'gpu_vulkan', 'npu_nnrt', 'media_avcodec'].includes(profileId)) {
          throw new Error('需要指定受支持的真机实验 profile')
        }
        const query = new URLSearchParams()
        if (profileId) query.set('profile_id', profileId)
        if (headerQuery) query.set('header_query', headerQuery.slice(0, 120))
        return api(`/v1/device-experiment-profiles${query.size > 0 ? `?${query.toString()}` : ''}`)
      }
      case 'device.stage_experiment_source': {
        const profileId = stringValue(input.profile_id, '')
        const language = stringValue(input.language, '')
        const source = stringValue(input.source, '')
        if (!['native_c', 'native_cpp', 'gpu_gles3', 'gpu_vulkan', 'npu_nnrt', 'media_avcodec'].includes(profileId)) throw new Error('需要指定受支持的真机实验 profile')
        if (language !== 'c' && language !== 'cpp') throw new Error('实验源码语言必须为 c 或 cpp')
        if (!source.trim()) throw new Error('需要完整的真机实验源码')
        if (new TextEncoder().encode(source).length > 192 * 1024) throw new Error('实验源码超过 192 KiB 上限')
        const modelConfig = readBrowserModelConfig()
        if (modelConfig.agentExecutionPolicy !== 'full_access') {
          throw new Error('当前页面助手执行策略为确认模式，不能暂存模型编写的真机实验源码。')
        }
        setView('reasoning')
        return requestReasoningLabAction({
          action: 'device_stage_experiment_source',
          profile_id: profileId,
          language,
          title: stringValue(input.title, ''),
          source,
          shader_source: stringValue(input.shader_source, ''),
        }, 120_000)
      }
      case 'device.build_upload_experiment': {
        const stagedSourceArtifactId = stringValue(input.staged_source_artifact_id, '')
        if (!/^art_[a-f0-9]{24}$/.test(stagedSourceArtifactId)) throw new Error('需要已暂存实验源码的 artifact_id')
        const modelConfig = readBrowserModelConfig()
        if (modelConfig.agentExecutionPolicy !== 'full_access') {
          throw new Error('当前页面助手执行策略为确认模式，不能构建或上传真机实验。')
        }
        const timeout = Number(input.timeout_seconds)
        const timeoutSeconds = Number.isInteger(timeout) && timeout >= 1 && timeout <= 900 ? timeout : 240
        setView('reasoning')
        return requestReasoningLabAction({
          action: 'device_build_upload_experiment',
          staged_source_artifact_id: stagedSourceArtifactId,
          timeout_seconds: timeoutSeconds,
        }, timeoutSeconds * 1000 + 180_000)
      }
      case 'device.execute_shell': {
        const command = stringValue(input.command, '')
        if (!command) throw new Error('需要一段将在已绑定真机上执行的 Shell 命令')
        if (new TextEncoder().encode(command).length > 16 * 1024) throw new Error('远端 Shell 命令超过 16 KiB 上限')
        const modelConfig = readBrowserModelConfig()
        if (modelConfig.agentExecutionPolicy !== 'full_access') {
          throw new Error('当前页面助手执行策略要求确认模式，不能由页面助手直接执行远端 Shell 命令。')
        }
        const timeout = Number(input.timeout_seconds)
        const timeoutSeconds = Number.isInteger(timeout) && timeout >= 1 && timeout <= 900 ? timeout : 240
        setView('reasoning')
        return requestReasoningLabAction({ action: 'device_execute_shell', command, timeout_seconds: timeoutSeconds }, timeoutSeconds * 1000 + 30_000)
      }
      case 'reasoning.prepare_context':
      case 'reasoning.run_strategy': {
        const strategyId = stringValue(input.strategy_id, '')
        if (!['kb_rag', 'hardware_kb', 'direct_baseline'].includes(strategyId)) throw new Error('未知推理策略')
        setView('reasoning')
        const action = actionId === 'reasoning.prepare_context' ? 'prepare_context' : 'run_strategy'
        if (action === 'run_strategy') {
          return requestReasoningLabAction({
            action,
            strategy_id: strategyId,
            context_artifact_id: stringValue(input.context_artifact_id, ''),
            agent_result: asRecord(input.agent_result),
            reason: stringValue(input.reason, ''),
            retry_nonce: stringValue(input.retry_nonce, ''),
          }, 10 * 60_000)
        }
        return requestReasoningLabAction({ action, strategy_id: strategyId }, 2 * 60_000)
      }
      case 'trace.explorer.select_tab': {
        const tab = stringValue(input.tab, traceExplorerTab) as TraceExplorerTab
        if (tab !== 'hotspots' && tab !== 'clusters') throw new Error('未知热点探索标签页')
        setTraceExplorerTab(tab)
        setView('scenarios')
        return { tab }
      }
      case 'trace.bind_source_snapshot': {
        const requestedAnalysisId = stringValue(input.analysis_artifact_id, activeTrace?.artifact_id ?? '')
        if (!activeTrace || requestedAnalysisId !== activeTrace.artifact_id) throw new Error('只能为当前追踪场景创建源码快照绑定')
        const requestedConnectorId = stringValue(input.source_connector_id, connectorId)
        if (requestedConnectorId !== connectorId) throw new Error('请先在页面中选择要绑定的代码仓')
        const requestedSnapshotId = stringValue(input.source_snapshot_id, activeConnector?.snapshot.snapshot_id ?? '')
        if (requestedSnapshotId !== activeConnector?.snapshot.snapshot_id) throw new Error('只能绑定当前页面显示的源码快照')
        const attestationNote = stringValue(input.attestation_note, '').trim()
        if (!attestationNote) throw new Error('页面助手需要记录用于确认源码快照的场景证据')
        return attestActiveTraceSourceSnapshot('agent', attestationNote)
      }
      case 'repository.select': {
        const requestedId = stringValue(input.source_connector_id, '')
        const connector = overview?.connectors.find((item) => item.connector_id === requestedId)
        if (!connector) throw new Error('代码仓连接器不存在')
        setSelectedConnectorId(connector.connector_id)
        setView('sources')
        return { source_connector_id: connector.connector_id, snapshot_id: connector.snapshot.snapshot_id }
      }
      case 'repository.refresh_snapshot': {
        const requestedId = stringValue(input.source_connector_id, connectorId)
        const connector = overview?.connectors.find((item) => item.connector_id === requestedId)
        if (!connector) throw new Error('代码仓连接器不存在')
        return refreshSourceSnapshot(connector)
      }
      case 'cluster.select': return selectCluster(stringValue(input.cluster_id, activeTrace?.clusters?.[0]?.cluster_id ?? ''), stringValue(input.rationale, '由页面助手选择。'), 'agent')
      case 'trace.list_cluster_events': {
        const clusterId = stringValue(input.cluster_id, selectedClusterId ?? '')
        if (!clusterId) throw new Error('请先选择追踪簇')
        setBusyAction(`events:${clusterId}`)
        try {
          return await loadClusterEvents(clusterId)
        } finally {
          setBusyAction(null)
        }
      }
      case 'trace.resolve_event_source': {
        const eventId = stringValue(input.event_id, selectedEventId ?? '')
        let event = clusterEvents.find((item) => item.event_id === eventId)
        if (!event && selectedClusterId) {
          const response = await loadClusterEvents(selectedClusterId)
          event = response.events.find((item) => item.event_id === eventId)
        }
        if (!event) throw new Error('请选择一个可解析的追踪事件')
        return resolveTraceEvent(event)
      }
      case 'trace.list_hotspot_discovery': {
        const discoveryId = stringValue(input.discovery_id, 'event_self_time')
        if (discoveryId !== 'event_self_time') throw new Error(`尚未实现热点发现方式: ${discoveryId}`)
        const moduleKey = stringValue(input.module_key, '')
        if (moduleKey) setExpandedSelfTimeModuleKeys((previous) => previous.includes(moduleKey) ? previous : [...previous, moduleKey])
        setBusyAction('self-time-events')
        try {
          return await loadSelfTimeEvents(activeTrace, moduleKey)
        } finally {
          setBusyAction(null)
        }
      }
      case 'trace.resolve_hotspot_discovery_event': {
        const discoveryId = stringValue(input.discovery_id, 'event_self_time')
        if (discoveryId !== 'event_self_time') throw new Error(`尚未实现热点发现方式: ${discoveryId}`)
        const moduleKey = stringValue(input.module_key, '')
        const eventId = stringValue(input.event_id, selectedSelfTimeEventId ?? '')
        let event = allSelfTimeEvents.find((item) => item.event_id === eventId)
        if (!event) {
          if (moduleKey) setExpandedSelfTimeModuleKeys((previous) => previous.includes(moduleKey) ? previous : [...previous, moduleKey])
          const response = await loadSelfTimeEvents(activeTrace, moduleKey)
          event = response.events.find((item) => item.event_id === eventId)
        }
        if (!event) throw new Error('请选择一个可解析的自耗时事件')
        return resolveSelfTimeEvent(event)
      }
      case 'event.identify_opportunity': {
        const requestedArtifactId = stringValue(input.source_resolution_artifact_id, '')
        const candidates = [selfTimeEventSourceResolution, eventSourceResolution].filter((item): item is Artifact => Boolean(item))
        const sourceResolution = requestedArtifactId
          ? candidates.find((item) => item.artifact_id === requestedArtifactId)
          : traceExplorerTab === 'hotspots' ? selfTimeEventSourceResolution : eventSourceResolution
        if (!sourceResolution) throw new Error('请先选择一个事件并完成源码证据解析')
        return identifySelectedEventOpportunity(sourceResolution)
      }
      case 'cluster.filter': {
        const nextQuery = typeof input.query === 'string' ? input.query : clusterFilter
        const nextShowSystemModules = typeof input.show_system_modules === 'boolean' ? input.show_system_modules : showSystemClusters
        setClusterFilter(nextQuery)
        setShowSystemClusters(nextShowSystemModules)
        return { query: nextQuery, show_system_modules: nextShowSystemModules }
      }
      case 'analysis.start': return startAnalysis()
      case 'kb.search':
      case 'optimization-kb.search':
      case 'hardware-kb.search': {
        const requested = stringValue(input.query, '')
        const includeDevelopment = typeof input.include_unverified === 'boolean' ? input.include_unverified : includeUnverified
        const requestedKnowledgeBase = actionId === 'hardware-kb.search' || input.knowledge_base === 'hardware' ? 'hardware' : 'optimization'
        setIncludeUnverified(includeDevelopment)
        setQuery(requested === '—' ? '' : requested)
        return searchKnowledge(requested === '—' ? '' : requested, includeDevelopment, requestedKnowledgeBase)
      }
      case 'source.inspect_location': {
        const path = stringValue(input.path, '')
        const line = numberValue(input.line, 1)
        const response = await api<{ artifact: Artifact }>('/v1/source-inspections', {
          method: 'POST',
          body: JSON.stringify({ source_connector_id: stringValue(input.source_connector_id, connectorId), path, line }),
        })
        setInspection(response.artifact)
        return response.artifact
      }
      case 'repository.register_local': return registerLocal(stringValue(input.source_root, ''), stringValue(input.display_name, ''))
      case 'repository.plan_git_clone': return createClonePlan(stringValue(input.url, ''), stringValue(input.display_name, ''))
      case 'repository.clone_git': return executeClone(stringValue(input.plan_id, clonePlan?.plan_id ?? ''))
      case 'report.export': {
        const runId = stringValue(input.analysis_run_artifact_id, analysisRun?.artifact_id ?? '')
        return api(`/v1/reports/${runId}`)
      }
      case 'trace.upload_result':
      case 'trace.upload_raw_htrace': throw new Error('浏览器文件选择需要用户授予文件访问。')
      default: throw new Error(`未注册操作：${actionId}`)
    }
  }

  async function executeAgentAction(actionId: string, input: Record<string, unknown>): Promise<unknown> {
    const inputHash = await digest(input)
    const analysisId = reasoningAssistantContext.analysis_record_id
    const requestId = randomUuid()
    const isTool = actionId.startsWith('device.') || actionId.startsWith('workspace.')
    await appendAnalysisTraceEvent(analysisId, {
      category: isTool ? 'tool_call' : 'page_action',
      operation: actionId,
      phase: 'action',
      status: 'started',
      requestId,
      payload: { input, input_sha256: inputHash },
    })
    try {
      const output = await executePageAction(actionId, input)
      await api('/v1/agent-action-audits', {
        method: 'POST',
        body: JSON.stringify({ action_id: actionId, conversation_id: 'browser-agent-local', source_connector_id: connectorId, input_sha256: inputHash, output_sha256: await digest(output), result: 'succeeded' }),
      })
      await appendAnalysisTraceEvent(analysisId, {
        category: 'tool_result',
        operation: actionId,
        phase: 'action',
        status: 'succeeded',
        requestId,
        payload: { input, output: output ?? null },
      })
      return output
    } catch (error) {
      await api('/v1/agent-action-audits', {
        method: 'POST',
        body: JSON.stringify({ action_id: actionId, conversation_id: 'browser-agent-local', source_connector_id: connectorId, input_sha256: inputHash, output_sha256: await digest({ error: String(error) }), result: 'failed' }),
      }).catch(() => undefined)
      await appendAnalysisTraceEvent(analysisId, {
        category: 'tool_result',
        operation: actionId,
        phase: 'action',
        status: 'failed',
        requestId,
        payload: { input, error: analysisErrorPayload(error) },
      })
      throw error
    }
  }

  const agentContext = useMemo(() => ({
    active_view: view,
    project_id: overview?.project.project_id,
    source_connector_id: connectorId,
    source_snapshot_id: activeConnector?.snapshot.snapshot_id,
    source_connectors: (overview?.connectors ?? []).map((connector) => ({
      source_connector_id: connector.connector_id,
      display_name: connector.display_name,
      source_root: connector.source_root,
      source_snapshot_id: connector.snapshot.snapshot_id,
    })),
    optimization_kb_version: overview?.optimization_kb.version,
    hardware_kb_version: overview?.hardware_kb.version,
    custom_api_knowledge_bases: (customApiKbCatalog?.knowledge_bases ?? []).map((kb) => ({
      kb_id: kb.kb_id,
      display_name: kb.display_name,
      accelerator_unit: kb.accelerator_unit,
      record_count: numberValue(asRecord(kb.summary).record_count),
    })),
    skill_profile: selectedSkillProfile ? {
      profile_id: selectedSkillProfile.profile_id,
      label_zh: selectedSkillProfile.label_zh,
      profile_sha256: selectedSkillProfile.profile_sha256,
      skill_versions: Object.fromEntries(Object.entries(selectedSkillProfile.skills).map(([skillId, skill]) => [skillId, skill.version])),
    } : null,
    target_platform_profile: targetPlatformProfile,
    active_trace: activeTrace ? { artifact_id: activeTrace.artifact_id, binding: activeTrace.source_binding.match_level, clusters: activeTrace.clusters?.map((item) => item.cluster_id) } : null,
    trace_explorer: activeTrace ? { tab: traceExplorerTab, cluster_query: clusterFilter, show_system_modules: showSystemClusters, visible_cluster_count: visibleClusterExplorerRows.length, system_cluster_count: systemClusterCount } : null,
    selected_cluster_id: selectedClusterId ?? null,
    cluster_events: selectedClusterId ? { quality: clusterEventQuality, events: clusterEvents.map((event) => ({ event_id: event.event_id, event_name: event.event_name, event_kind: event.event_kind })) } : null,
    selected_event_id: selectedEventId,
    hotspot_discovery: activeTrace?.input_kind === 'raw_htrace' ? {
      discovery_id: 'event_self_time',
      quality: selfTimeQuality,
      expanded_module_keys: expandedSelfTimeModuleKeys,
      modules: selfTimeModuleGroups(selfTimeModuleCatalog).map((module) => ({
        module_key: module.module_key,
        module_name: module.module_name,
        self_time_ms: module.self_time_estimate_total_duration_ms,
        inclusive_time_ms: module.inclusive_total_duration_ms,
        event_groups: module.event_group_count,
      })),
      events: allSelfTimeEvents.map((event) => ({
        event_id: event.event_id,
        event_name: event.event_name,
        module_key: traceModuleForEvent(event).module_key,
        self_time_ms: asRecord(event.metrics).self_time_estimate_total_duration_ms,
      })),
      selected_event_id: selectedSelfTimeEventId,
    } : null,
    selected_source_resolutions: [selfTimeEventSourceResolution, eventSourceResolution].filter((item): item is Artifact => Boolean(item)).map((artifact) => ({
      artifact_id: artifact.artifact_id,
      state: asRecord(artifact.resolution).state,
      trace_binding_match_level: asRecord(artifact.resolution).trace_binding_match_level,
      event_id: asRecord(artifact.event).event_id,
    })),
    analysis_run_id: analysisRun?.artifact_id ?? null,
    reasoning_lab: reasoningAssistantContext,
    code_knowledge: view === 'code_knowledge' ? codeKnowledgeAssistantContext : null,
    application_flow: view === 'flows' ? applicationFlowAssistantContext : null,
    experimental_flow: view === 'playground_hiperf' || view === 'playground_trace' ? experimentalFlowAssistantContext : null,
    workload_features: view === 'workload_features' ? workloadFeaturesAssistantContext : null,
    scenario_capture: view === 'scenario_capture' ? scenarioCaptureAssistantContext : null,
    parallel_modeling: view === 'parallel_modeling' ? parallelModelingAssistantContext : null,
    guided_optimization_workflow: optimizationWorkflow,
  }), [workloadFeaturesAssistantContext, scenarioCaptureAssistantContext, parallelModelingAssistantContext, activeConnector?.snapshot.snapshot_id, activeTrace, allSelfTimeEvents, analysisRun, applicationFlowAssistantContext, clusterEventQuality, clusterEvents, clusterFilter, codeKnowledgeAssistantContext, connectorId, customApiKbCatalog?.knowledge_bases, eventSourceResolution, expandedSelfTimeModuleKeys, experimentalFlowAssistantContext, optimizationWorkflow, overview?.connectors, overview?.hardware_kb.version, overview?.optimization_kb.version, overview?.project.project_id, reasoningAssistantContext, selectedClusterId, selectedEventId, selectedSelfTimeEventId, selectedSkillProfile, selfTimeEventSourceResolution, selfTimeModuleCatalog, selfTimeQuality, showSystemClusters, systemClusterCount, targetPlatformProfile, traceExplorerTab, view, visibleClusterExplorerRows.length])

  const fixtures = overview?.fixtures ?? []
  const primaryFixtures = fixtures.filter((fixture) => traceInputMode === 'raw_htrace' ? fixture.kind === 'raw_htrace' : fixture.kind === 'external_report_json')
  const experimentalFixtures = fixtures.filter((fixture) => fixture.kind === 'sys_trace_experimental')
  const uploadAccept = traceInputMode === 'raw_htrace' ? '.htrace,application/octet-stream' : '.json,application/json'
  const currentBinding = activeTrace?.source_binding.match_level ?? 'unknown'
  const reportOpportunities = (opportunityReport?.opportunities ?? []) as JsonRecord[]
  const reportSummary = asRecord(opportunityReport?.summary)
  const reportBlockingConditions = Array.isArray(opportunityReport?.blocking_conditions) ? opportunityReport.blocking_conditions as string[] : []
  const inspectionPrecheck = asRecord(inspection?.structural_precheck)
  const inspectionFeatures = Array.isArray(inspection?.features) ? inspection.features as JsonRecord[] : []
  const inspectionDiagnostics = Array.isArray(inspection?.ast_diagnostics) ? inspection.ast_diagnostics as string[] : []
  const activeTraceQuality = asRecord(activeTrace?.quality)
  const activeTraceTableCounts = asRecord(activeTraceQuality.table_counts)
  const activeTraceEventCounts = asRecord(activeTraceQuality.supported_event_counts)
  const activeTraceStages = Array.isArray(activeTrace?.stage_catalog) ? activeTrace.stage_catalog.map(asRecord) : []
  const traceWindowMs = numberValue(activeTraceQuality.trace_window_ms, -1)
  const optimizationRecords = (kbSearches.optimization?.records ?? [])
  const strictOptimizationCases = optimizationRecords.filter((record) => record.record_type === 'strict_heterogeneous_case')
  const hardwareRecords = kbSearches.hardware?.records ?? []
  const acceleratorApiCatalog = acceleratorApiCatalogs[acceleratorApiUnit]
  const activeKnowledgeMetadata = knowledgeBase === 'optimization'
    ? overview?.optimization_kb
    : knowledgeBase === 'hardware'
      ? overview?.hardware_kb
      : knowledgeBase === 'npu_api'
        ? npuApiCatalog?.npu_api_kb
        : knowledgeBase === 'accelerator_api'
          ? acceleratorApiCatalog?.accelerator_api_kb
          : selectedCustomApiKb?.knowledge_base
  const activeKnowledgeBusy = knowledgeBase === 'npu_api'
    ? busyAction === 'npu-api-catalog'
    : knowledgeBase === 'accelerator_api'
      ? busyAction === `accelerator-api-catalog:${acceleratorApiUnit}`
      : knowledgeBase === 'custom_api'
        ? busyAction === 'custom-api-catalog' || busyAction?.startsWith('custom-api-kb:') === true
        : busyAction === `kb-search:${knowledgeBase}`
  const selectedAnalysisRecord = analysisHistory.find((item) => item.analysisId === selectedAnalysisRecordId) ?? null
  const analysisSkillCatalog = useMemo(() => {
    const manifests = new Map(backendSkillCatalog
      .map((manifest) => [stringValue(manifest.skill_id, ''), manifest] as const)
      .filter(([skillId]) => Boolean(skillId)))
    const primary = analysisSkillItems
      .filter((item) => !hiddenAnalysisSkillIds.has(item.id))
      .map((item) => enrichSkillItem(item, manifests.get(item.id)))
    const knownIds = new Set(primary.map((item) => item.id))
    const backendOnly = backendSkillCatalog
      .filter((manifest) => {
        const skillId = stringValue(manifest.skill_id, '')
        return !knownIds.has(skillId) && !hiddenAnalysisSkillIds.has(skillId)
      })
      .map(backendSkillItem)
      .filter((item): item is AnalysisSkillItem => Boolean(item))
    return [...primary, ...backendOnly]
  }, [backendSkillCatalog])
  const platformBuildMatches = platformVersion?.build.build_id === browserPlatformBuild.build_id
  const platformVersionState = platformVersion === null ? 'unavailable' : platformBuildMatches ? 'matched' : 'mismatch'
  const platformVersionSummary = `${browserPlatformBuild.release} · ${shortRevision(browserPlatformBuild)}${browserPlatformBuild.source_dirty ? '*' : ''}`
  const conciseViewTitle = view === 'knowledge'
    ? knowledgeBase === 'optimization' ? '优化知识库' : knowledgeBase === 'hardware' ? '硬件知识库' : knowledgeBase === 'npu_api' ? 'NPU API' : knowledgeBase === 'accelerator_api' ? '异构 API' : '自建 API'
    : view === 'analysis_history' ? '分析记录'
      : navItems.find((item) => item.id === view)?.label ?? '机会报告'

  return (
    <main className="workbench-shell">
      <header className="global-header">
        <div className="brand-lockup">
          <div className="brand-mark"><Sparkles size={17} /></div>
          <div><strong>异构使能优化</strong><span>本地工程工作台</span></div>
        </div>
        <div className="header-context">
          <Pill tone="success">本地</Pill>
          <span>{displayProjectMode(overview?.project.mode)}</span>
          <details className={`platform-version ${platformVersionState}`}>
            <summary title="查看浏览器、后端和 MCP 契约版本"><GitBranch size={13} /><code>{platformVersionSummary}</code>{platformVersionState !== 'matched' && <i>!</i>}</summary>
            <div className="platform-version-popover">
              <strong>{platformVersionState === 'matched' ? '前后端版本一致' : platformVersionState === 'mismatch' ? '前后端版本不一致' : '后端未提供版本信息'}</strong>
              <div><span>浏览器构建</span><code>{shortBuildId(browserPlatformBuild.build_id)}</code></div>
              <div><span>浏览器源码</span><code>{shortRevision(browserPlatformBuild)}</code></div>
              <div><span>后端构建</span><code>{platformVersion ? shortBuildId(platformVersion.build.build_id) : '旧部署 / 不可用'}</code></div>
              <div><span>后端源码</span><code>{platformVersion ? shortRevision(platformVersion.build) : '未知'}</code></div>
              <div><span>API 进程</span><code>{platformVersion ? `${platformVersion.api_runtime.process_id} · ${platformVersion.api_runtime.started_at}` : '未知'}</code></div>
              <div><span>平台 MCP</span><code>{platformVersion ? `${platformVersion.contracts.platform_mcp} · ${platformVersion.contracts.mcp_tool_count} tools · ${platformVersion.contracts.mcp_facade_sha256.slice(0, 12)}` : '未知'}</code></div>
              {platformVersionState !== 'matched' && <p>停止旧服务后重新执行安装和启动，并新建 Agent PTY Session。</p>}
            </div>
          </details>
          <button className="icon-button" type="button" title="刷新平台状态" aria-label="刷新平台状态" onClick={() => void refresh()}><RefreshCw size={16} /></button>
        </div>
      </header>

      <div className="workbench-grid">
        <nav className="side-nav" aria-label="主导航">
          <div className="nav-project"><span className="eyebrow">项目</span><strong>{overview?.project.project_id ?? 'local-default'}</strong></div>
          <div className="nav-items">
            {navItems.filter((item) => !item.hidden).map((item) => {
              const Icon = item.icon
              return <button key={item.id} className={`nav-item ${view === item.id ? 'active' : ''}`} type="button" onClick={() => {
                setView(item.id)
                if (item.id === 'knowledge') {
                  if (knowledgeBase === 'npu_api') {
                    if (!npuApiCatalog) void loadNpuApiCatalog()
                  } else if (knowledgeBase === 'accelerator_api') {
                    if (!acceleratorApiCatalog) void loadAcceleratorApiCatalog(acceleratorApiUnit)
                  } else if (knowledgeBase === 'custom_api') {
                    if (!customApiKbCatalog) void loadCustomApiKnowledgeBases()
                  } else if (!kbSearches[knowledgeBase]) {
                    void searchKnowledge('', includeUnverified, knowledgeBase)
                  }
                }
              }}><Icon size={17} /><span>{item.label}</span><ChevronRight size={14} /></button>
            })}
          </div>
          <details className="nav-section nav-analysis-history" open={analysisHistory.length > 0}>
            <summary><span><History size={15} />优化分析记录</span><small>{analysisHistory.length}</small></summary>
            <div className="nav-analysis-history-list">
              {analysisHistory.length === 0 && <div className="nav-history-empty">选择真实 Trace 或保存手动代码后，这里会保留分析会话及每次推理的上下文摘要。</div>}
              {analysisHistory.map((analysis) => <button className={`nav-analysis-record ${view === 'analysis_history' && selectedAnalysisRecordId === analysis.analysisId ? 'active' : ''}`} type="button" key={analysis.analysisId} onClick={() => { setSelectedAnalysisRecordId(analysis.analysisId); setView('analysis_history') }}>
                <span>{analysis.trace.label}</span><small>{analysisTime(analysis.updatedAt)} · {analysis.inferences.length} 次推理</small>
                <em>{analysis.hotspot?.eventName ?? '尚未选择热点'} · {analysis.inferences.some((item) => item.transcriptArtifactId) ? '可查看完整记录' : analysis.inferences.length > 0 ? '历史摘要' : '等待推理'}</em>
              </button>)}
            </div>
          </details>
          <div className="nav-footer">
            <span>分析工件存储</span>
            <strong>{overview ? `${formatBytes(overview.artifact_store.filesystem_free_bytes)} 可用` : '—'}</strong>
          </div>
        </nav>

        <section className="workspace" aria-label="异构优化工作台">
          {!studioOwnHeadingViews.includes(view) && <div className="workspace-heading">
            <div>
              <h1 className="workspace-page-title" tabIndex={0} title={viewEyebrow(view)}>{conciseViewTitle}</h1>
            </div>
          </div>}
          {notice && <div className="workspace-global-notice"><button className={`notice ${notice.tone}`} type="button" onClick={() => setNotice(null)}>{notice.text}<X size={14} /></button></div>}

          {view !== 'workload_features' && view !== 'hardware_models' && view !== 'scenario_capture' && view !== 'parallel_modeling' && view !== 'trace_library' && view !== 'optimization_calibration' && <div className="status-strip">
            <Metric label="源码快照" value={activeConnector?.snapshot.snapshot_id.slice(0, 22) ?? '—'} tone={activeConnector?.snapshot.clean ? 'success' : 'warning'} />
            <Metric label="优化知识库" value={overview?.optimization_kb.version ?? '—'} />
            <Metric label="硬件知识库" value={overview?.hardware_kb.version ?? '—'} />
            <Metric label="异构案例" value={overview ? `${numberValue(overview.optimization_kb.summary.strict_case_count)} 个` : '—'} tone="success" />
            <Metric label="追踪绑定" value={displayState(currentBinding)} tone={bindingClass(currentBinding)} />
          </div>}

          {view === 'scenarios' && (
            <div className="workspace-scroll">
              <section className="work-section">
                <div className="section-heading">
                  <div><span className="eyebrow">输入</span><h2>追踪输入</h2></div>
                  <div className="heading-actions">
                    <input ref={uploadRef} className="visually-hidden" type="file" accept={uploadAccept} onChange={handleUpload} />
                    <button className="quiet-button" type="button" onClick={() => uploadRef.current?.click()} disabled={busyAction === 'upload'}>
                      <Upload size={15} />{traceInputMode === 'raw_htrace' ? '导入 .htrace' : '导入 JSON'}
                    </button>
                  </div>
                </div>
                <div className="trace-input-modes" role="group" aria-label="追踪输入类型">
                  <button className={traceInputMode === 'external_report' ? 'selected' : ''} type="button" onClick={() => setTraceInputMode('external_report')}>
                    <ArrowDownToLine size={15} />追踪团队分析
                  </button>
                  <button className={traceInputMode === 'raw_htrace' ? 'selected' : ''} type="button" onClick={() => setTraceInputMode('raw_htrace')}>
                    <Activity size={15} />原始 .htrace
                  </button>
                </div>
                <div className="fixture-list">
                  {primaryFixtures.map((fixture) => (
                    <div className="fixture-row" key={fixture.fixture_id}>
                      <div className="fixture-icon">{fixture.kind === 'raw_htrace' ? <Activity size={17} /> : <ArrowDownToLine size={17} />}</div>
                      <div className="fixture-meta"><strong>{displayFixtureLabel(fixture.fixture_id, fixture.label)}</strong><span>{formatBytes(fixture.bytes)} · {fixtureKindLabel(fixture.kind)}</span></div>
                      <button className="quiet-button" type="button" disabled={busyAction === `fixture:${fixture.fixture_id}`} onClick={() => { void importFixture(fixture.fixture_id).catch(() => undefined) }}>
                        {busyAction === `fixture:${fixture.fixture_id}` ? <LoaderCircle size={14} className="spin" /> : <ArrowDownToLine size={14} />}导入
                      </button>
                    </div>
                  ))}
                </div>
                {experimentalFixtures.length > 0 && <details className="experimental-trace-input">
                  <summary>实验性调度文本</summary>
                  <div className="fixture-list">
                    {experimentalFixtures.map((fixture) => <div className="fixture-row" key={fixture.fixture_id}>
                      <div className="fixture-icon"><Network size={17} /></div>
                      <div className="fixture-meta"><strong>{displayFixtureLabel(fixture.fixture_id, fixture.label)}</strong><span>{formatBytes(fixture.bytes)} · 实验性输入</span></div>
                      <button className="quiet-button" type="button" disabled={busyAction === `cluster:${fixture.fixture_id}`} onClick={() => { void clusterFixture(fixture.fixture_id).catch(() => undefined) }}>
                        {busyAction === `cluster:${fixture.fixture_id}` ? <LoaderCircle size={14} className="spin" /> : <Network size={14} />}聚类
                      </button>
                    </div>)}
                  </div>
                </details>}
              </section>

              <section className="work-section trace-workspace">
                <div className="section-heading"><div><span className="eyebrow">热点探索</span><h2>{activeTrace ? stringValue(activeTrace.trace_name, activeTrace.artifact_id) : '等待场景输入'}</h2></div>{activeTrace && <Pill tone={bindingClass(currentBinding)}>{displayState(currentBinding)}</Pill>}</div>
                {!activeTrace ? <EmptyState title="尚未载入追踪数据" detail={traceInputMode === 'raw_htrace' ? '导入原始 .htrace，生成基础 Span 与调度候选。' : '导入追踪团队提供的 JSON 分析报告。'} /> : <>
                  {Boolean(activeTrace.experimental) && <div className="warning-banner"><CircleAlert size={16} /><span>.sys 唤醒窗口结果属于实验性能力，不作为关键路径结论。</span></div>}
                  {activeTrace.input_kind === 'raw_htrace' && <div className="warning-banner"><CircleAlert size={16} /><span>原始 .htrace 当前只做基础 Span 与调度事实提取；候选簇不等同于关键路径，也不会自动推断源码位置或 GPU 时间。</span></div>}
                  <TraceSourceBindingPanel trace={activeTrace} connector={activeConnector} busy={busyAction === 'trace-source-binding'} refreshBusy={busyAction === `snapshot:${connectorId}`} onBind={() => void attestActiveTraceSourceSnapshot()} onRefresh={() => void refreshSourceSnapshot()} />
                  {activeTrace.input_kind === 'raw_htrace' && <div className="trace-evidence-grid" aria-label="原始追踪提取质量">
                    <div><span>采集窗口</span><strong>{traceWindowMs >= 0 ? `${traceWindowMs.toFixed(2)} ms` : '不可用'}</strong></div>
                    <div><span>调用栈 Span</span><strong>{numberValue(activeTraceTableCounts.callstack).toLocaleString()}</strong></div>
                    <div><span>调度切片</span><strong>{numberValue(activeTraceTableCounts.sched_slice).toLocaleString()}</strong></div>
                    <div><span>唤醒事件</span><strong>{numberValue(activeTraceEventCounts.sched_wakeup).toLocaleString()}</strong></div>
                  </div>}
                  <div className="trace-explorer-tabs" role="tablist" aria-label="热点发现路径">
                    <button className={traceExplorerTab === 'hotspots' ? 'selected' : ''} type="button" role="tab" aria-selected={traceExplorerTab === 'hotspots'} onClick={() => setTraceExplorerTab('hotspots')}><Activity size={15} />热点事件</button>
                    <button className={traceExplorerTab === 'clusters' ? 'selected' : ''} type="button" role="tab" aria-selected={traceExplorerTab === 'clusters'} onClick={() => setTraceExplorerTab('clusters')}><Workflow size={15} />唤醒链簇</button>
                  </div>
                  {traceExplorerTab === 'hotspots' && (activeTrace.input_kind === 'raw_htrace' ? <>
                    <div className="self-time-heading">
                      <div><span className="eyebrow">热点发现</span><h3>按模块统计事件自耗时</h3></div>
                      <div className="self-time-controls">
                        {selfTimeQuality && <Pill tone={selfTimeQuality.status === 'partial' ? 'success' : selfTimeQuality.status === 'loading' ? 'neutral' : 'warning'}>{displayState(selfTimeQuality.status)}</Pill>}
                      </div>
                    </div>
                    {selfTimeModules.length > 0 ? <div className="self-time-module-list" aria-label="按模块排序的事件自耗时">
                      {selfTimeModules.map((module) => {
                        const moduleKey = stringValue(module.module_key, '')
                        const isExpanded = expandedSelfTimeModuleKeys.includes(moduleKey)
                        const moduleResult = selfTimeModuleResults[moduleKey]
                        const moduleError = selfTimeModuleErrors[moduleKey]
                        const isLoading = selfTimeLoadingModuleKeys.includes(moduleKey)
                        const containsSelectedEvent = Boolean(selectedSelfTimeEventId && moduleResult?.events.some((event) => event.event_id === selectedSelfTimeEventId))
                        return <section className={`self-time-module-group ${isExpanded ? 'expanded' : ''}`} key={moduleKey}>
                          <button className="self-time-module-toggle" type="button" aria-expanded={isExpanded} onClick={() => toggleSelfTimeModule(moduleKey)}>
                            <ChevronRight size={16} className={isExpanded ? 'expanded' : ''} />
                            <span className="self-time-module-name"><strong>{displayModuleName(module.module_name)}</strong><small>{numberValue(module.event_group_count).toLocaleString()} 个事件分组</small></span>
                            <span className="self-time-module-metric" title="直接子 Span 时长扣除估计，不是采样 CPU 自耗时。"><small>自耗时估计</small><strong>{moduleTimeMetric(module, 'self_time_estimate_total_duration_ms')}</strong></span>
                            <span className="self-time-module-metric" title="插桩 Span 的包容时长。"><small>包容时长</small><strong>{moduleTimeMetric(module, 'inclusive_total_duration_ms')}</strong></span>
                            <span className="self-time-module-metric" title="包容时长减去直接子 Span 自耗时估计。"><small>子调用覆盖</small><strong>{`${Math.max(0, numberValue(module.inclusive_total_duration_ms) - numberValue(module.self_time_estimate_total_duration_ms)).toFixed(2)} ms`}</strong></span>
                          </button>
                          {isExpanded && <div className="self-time-module-content">
                            {isLoading ? <div className="self-time-module-loading"><LoaderCircle size={15} className="spin" /><span>正在按模块计算事件自耗时</span></div> : moduleError ? <div className="event-empty">{displayReason(moduleError)}</div> : moduleResult && moduleResult.events.length > 0 ? <>
                              <div className="self-time-table table-shell">
                                <div className="table-header self-time"><span>事件名称</span><span title="直接子 Span 时长扣除估计，不是采样 CPU 自耗时。">自耗时总计</span><span title="包容时长减去自耗时估计，用于近似解释直接子 Span 的覆盖时间。">子调用覆盖</span><span title="插桩 Span 的包容时长。">包容时长</span><span>最大自耗时</span><span>最大 Span</span><span>出现次数</span><span>源码</span><span /></div>
                                {moduleResult.events.map((event) => {
                                  const selected = selectedSelfTimeEventId === event.event_id
                                  const sourceState = stringValue(event.source_resolution, 'unresolved')
                                  const hitraceSummary = eventHitraceSummary(event)
                                  const traceContext = asRecord(event.trace_context)
                                  const contextSummary = [hitraceSummary || displayTraceKind(event.event_kind), stringValue(traceContext.process_name, ''), stringValue(traceContext.thread_name, ''), selfTimeModuleEvidence(event)].filter((value) => value && value !== '—').join(' · ')
                                  return <div className={`table-row self-time ${selected ? 'selected' : ''}`} key={event.event_id}>
                                    <div><strong title={event.event_name}>{event.event_name}</strong><span title={contextSummary}>#{event.rank} · {contextSummary}</span></div>
                                    <span>{selfTimeMetric(event)}</span>
                                    <span>{childCoveredTimeMetric(event)}</span>
                                    <span>{inclusiveTimeMetric(event)}</span>
                                    <span>{maxSelfTimeMetric(event)}</span>
                                    <span>{maxSpanTimeMetric(event)}</span>
                                    <span>{eventOccurrenceCount(event)}</span>
                                    <span><Pill tone={asRecord(event.source_location).path ? 'success' : 'neutral'}>{displayState(sourceState)}</Pill></span>
                                    <button className="icon-button" type="button" title="关联此事件的代码块" aria-label="关联此事件的代码块" disabled={busyAction === `self-time-event:${event.event_id}`} onClick={() => void resolveSelfTimeEvent(event)}>{busyAction === `self-time-event:${event.event_id}` ? <LoaderCircle size={15} className="spin" /> : selected ? <Check size={15} /> : <FileCode2 size={15} />}</button>
                                  </div>
                                })}
                              </div>
                              {containsSelectedEvent && selfTimeEventSourceResolution && <>
                                <EventCodePanel resolutionArtifact={selfTimeEventSourceResolution} onSelectSuggestedConnector={selectSuggestedConnector} />
                                <EventOpportunityPanel resolutionArtifact={selfTimeEventSourceResolution} busy={busyAction === `event-opportunity:${selfTimeEventSourceResolution.artifact_id}`} bindingBusy={busyAction === 'trace-source-binding'} onIdentify={() => void identifySelectedEventOpportunity(selfTimeEventSourceResolution)} onBindSourceSnapshot={() => void attestActiveTraceSourceSnapshot()} />
                              </>}
                            </> : <div className="event-empty">该模块没有可用的自耗时事件</div>}
                          </div>}
                        </section>
                      })}
                    </div> : <div className="event-empty">{selfTimeQuality?.status === 'loading' ? '正在统计全追踪事件自耗时' : displayReason(selfTimeQuality?.reason)}</div>}
                  </> : activeTraceStages.length > 0 ? <TraceStageTimeline stages={activeTraceStages} /> : <EmptyState title="当前输入不提供阶段热点目录" detail="报告没有携带可识别的 stage_catalog；可切换到唤醒链簇继续查看可调查的 Trace 锚点。" />)}
                  {traceExplorerTab === 'clusters' && <>
                  <div className="cluster-explorer-toolbar">
                    <label className="cluster-filter">
                      <Search size={15} />
                      <input value={clusterFilter} onChange={(event) => setClusterFilter(event.target.value)} placeholder="筛选模块、函数或唤醒链簇" aria-label="筛选追踪簇" />
                    </label>
                    <span className="cluster-count">{visibleClusterExplorerRows.length} / {clusterExplorerRows.length} 个簇</span>
                    {systemClusterCount > 0 && <details className="system-cluster-collapse">
                      <summary>{showSystemClusters ? `系统模块 ${systemClusterCount}` : `已折叠系统模块 ${systemClusterCount}`}</summary>
                      <div className="system-cluster-controls">
                        <label className="check-control"><input type="checkbox" checked={showSystemClusters} onChange={(event) => setShowSystemClusters(event.target.checked)} />显示系统模块</label>
                        <span>{traceModuleRules.filter((rule) => rule.system).map((rule) => rule.name).join(' · ')}</span>
                      </div>
                    </details>}
                  </div>
                  <div className="cluster-table table-shell">
                    <div className="cluster-table-scroll">
                      <div className="table-header cluster"><span>唤醒链簇</span><span>模块 / 顶层函数</span><span>时间窗</span><span>边</span><span>状态</span><span /></div>
                      {visibleClusterExplorerRows.map(({ cluster, summary }) => {
                        const edges = asRecord(cluster.edge_summary)
                        const range = asRecord(cluster.time_range_ns)
                        const selected = selectedClusterId === cluster.cluster_id
                        const displayModule = summary.moduleNames.length > 0 ? summary.moduleNames.join(' · ') : summary.topFunctionName
                        return <div className={`table-row ${selected ? 'selected' : ''}`} key={cluster.cluster_id}><div><strong>{cluster.cluster_id}</strong><span>{displayTraceKind(edges.kind)}</span></div><div><strong title={displayModule}>{displayModule}</strong><span>{summary.moduleNames.length > 0 ? stringValue(summary.topFunctionName, '') : '追踪标签回退'}</span></div><span>{range.start_ns ? `${String(range.start_ns).slice(-8)} ns` : '未提供'}</span><span>{numberValue(edges.edge_count)}</span><span><Pill tone={selected ? 'success' : 'neutral'}>{selected ? '已选中' : displayState(cluster.selection_state)}</Pill></span><button className="icon-button" type="button" title="选择此追踪簇" aria-label="选择此追踪簇" disabled={busyAction === `select:${cluster.cluster_id}`} onClick={() => void selectCluster(cluster.cluster_id)}>{selected ? <Check size={15} /> : <ChevronRight size={15} />}</button></div>
                      })}
                      {visibleClusterExplorerRows.length === 0 && <div className="cluster-empty">没有匹配的追踪簇</div>}
                    </div>
                  </div>
                  {selectedClusterId && <>
                    <div className="event-heading"><div><span className="eyebrow">热点事件</span><h3>{clusterEvents.length > 0 ? `${clusterEvents.length} 个事件` : '正在提取事件'}</h3></div>{clusterEventQuality && <Pill tone={clusterEventQuality.status === 'available' || clusterEventQuality.status === 'partial' ? 'success' : 'warning'}>{displayState(clusterEventQuality.status)}</Pill>}</div>
                    {clusterEvents.length > 0 ? <div className="event-table table-shell"><div className="table-header event"><span>事件名称</span><span>时间窗重叠</span><span>出现次数</span><span>源码</span><span /></div>{clusterEvents.map((event) => { const selected = selectedEventId === event.event_id; const sourceState = stringValue(event.source_resolution, 'unresolved'); const hitraceSummary = eventHitraceSummary(event); const eventKind = displayTraceKind(event.event_kind); return <div className={`table-row event ${selected ? 'selected' : ''}`} key={event.event_id}><div><strong title={event.event_name}>{event.event_name}</strong><span title={hitraceSummary || eventKind}>{hitraceSummary || eventKind}</span></div><span>{eventMetric(event)}</span><span>{eventOccurrenceCount(event)}</span><span><Pill tone={asRecord(event.source_location).path ? 'success' : 'neutral'}>{displayState(sourceState)}</Pill></span><button className="icon-button" type="button" title="解析此事件的代码块" aria-label="解析此事件的代码块" disabled={busyAction === `event:${event.event_id}`} onClick={() => void resolveTraceEvent(event)}>{busyAction === `event:${event.event_id}` ? <LoaderCircle size={15} className="spin" /> : selected ? <Check size={15} /> : <FileCode2 size={15} />}</button></div> })}</div> : <div className="event-empty">{busyAction === `select:${selectedClusterId}` ? '正在提取此时间窗内的调用栈事件' : displayReason(clusterEventQuality?.reason)}</div>}
                    {selectedEventId && eventSourceResolution && <>
                      <EventCodePanel resolutionArtifact={eventSourceResolution} onSelectSuggestedConnector={selectSuggestedConnector} />
                      <EventOpportunityPanel resolutionArtifact={eventSourceResolution} busy={busyAction === `event-opportunity:${eventSourceResolution.artifact_id}`} bindingBusy={busyAction === 'trace-source-binding'} onIdentify={() => void identifySelectedEventOpportunity(eventSourceResolution)} onBindSourceSnapshot={() => void attestActiveTraceSourceSnapshot()} />
                    </>}
                  </>}
                  </>}
                </>}
              </section>
            </div>
          )}

          <div className={`preserved-reasoning-view ${view === 'reasoning' ? 'is-active' : 'is-hidden'}`} aria-hidden={view !== 'reasoning'}>
            <ReasoningLab key={reasoningInstanceKey} deployment={overview?.deployment} sourceConnectorId={connectorId} workflow={optimizationWorkflow} onWorkflowEvent={handleOptimizationWorkflowEvent} onResetAnalysis={resetOptimizationAnalysis} onTraceAnalysisStarted={startTraceAnalysisRecord} onTraceAnalysisHotspotUpdated={updateTraceAnalysisHotspot} onInferenceRecorded={recordTraceAnalysisInference} onInferenceUpdated={updateTraceAnalysisInference} onDeviceValidationRecorded={recordTraceAnalysisDeviceValidation} targetPlatformProfile={targetPlatformProfile} onTargetPlatformProfileChange={updateTargetPlatformProfile} onAssistantStateChange={setReasoningAssistantContext} restoreRequest={reasoningRestoreRequest} skillProfile={selectedSkillProfile} />
          </div>

          {view === 'flows' && <ApplicationFlowStudio connectors={overview?.connectors ?? []} selectedConnectorId={connectorId} loadedTrace={activeTrace} onSelectConnector={setSelectedConnectorId} onRefreshConnectors={refresh} onAssistantContextChange={setApplicationFlowAssistantContext} />}

          {view === 'scenario_capture' && <ScenarioCaptureStudio onNavigateTraceLibrary={() => setView('trace_library')} onAssistantContextChange={setScenarioCaptureAssistantContext} onNavigateModeling={(datasetId) => { setInitialModelingDatasetId(datasetId); setInitialFeatureProfileId(undefined); setModelingHandoffRevision(current => current + 1); setView('parallel_modeling') }} />}

          {view === 'trace_library' && <TraceLibraryStudio onNavigateCapture={() => setView('scenario_capture')} onNavigateImport={() => setView('scenarios')} />}

          {view === 'optimization_calibration' && <OptimizationCalibrationStudio onNavigateModeling={(modelId) => { setInitialModelingModelId(modelId); setView('parallel_modeling') }} onNavigateHardwareModels={() => setView('hardware_models')} />}

          {view === 'workload_features' && <WorkloadFeaturesStudio initialDatasetId={initialFeaturesDatasetId} onAssistantContextChange={setWorkloadFeaturesAssistantContext} onNavigateModeling={(datasetId, featureProfileId) => { setInitialModelingDatasetId(datasetId); setInitialFeatureProfileId(featureProfileId); setModelingHandoffRevision(current => current + 1); setView('parallel_modeling') }} onNavigateCapture={() => setView('scenario_capture')} onNavigateCalibration={() => setView('optimization_calibration')} onNavigateHardwareModels={() => setView('hardware_models')} />}

          {view === 'hardware_models' && <HardwareModelsStudio onNavigateModeling={() => setView('parallel_modeling')} />}

          {view === 'parallel_modeling' && <ParallelModelingStudio onAssistantContextChange={setParallelModelingAssistantContext} initialDatasetId={initialModelingDatasetId} initialModelId={initialModelingModelId} initialFeatureProfileId={initialFeatureProfileId} handoffRevision={modelingHandoffRevision} onNavigateFeatures={(datasetId) => { setInitialFeaturesDatasetId(datasetId); setView('workload_features') }} onNavigateCapture={() => setView('scenario_capture')} onNavigateCalibration={() => setView('optimization_calibration')} onNavigateHardwareModels={() => setView('hardware_models')} />}

          {view === 'code_knowledge' && <CodeKnowledgeStudio connectors={overview?.connectors ?? []} selectedConnectorId={connectorId} onSelectConnector={setSelectedConnectorId} onAssistantContextChange={setCodeKnowledgeAssistantContext} />}

          {view === 'playground_hiperf' && <ExperimentalFlowPlayground mode="hiperf" connectors={overview?.connectors ?? []} selectedConnectorId={connectorId} onSelectConnector={setSelectedConnectorId} onAssistantContextChange={setExperimentalFlowAssistantContext} />}

          {view === 'playground_trace' && <ExperimentalFlowPlayground mode="trace" connectors={overview?.connectors ?? []} selectedConnectorId={connectorId} onSelectConnector={setSelectedConnectorId} onAssistantContextChange={setExperimentalFlowAssistantContext} />}

          {view === 'analysis_history' && (selectedAnalysisRecord
            ? <AnalysisRecordTrace analysis={selectedAnalysisRecord} onLoadToWorkbench={() => loadAnalysisRecordToWorkbench(selectedAnalysisRecord)} />
            : <div className="workspace-scroll"><section className="work-section"><div className="event-empty">请选择左侧的一条优化分析记录。</div></section></div>)}

          {view === 'experience' && <div className="workspace-scroll"><ExperienceEvolutionStudio
            onOpenView={(target) => setView(target)}
            onOpenSkill={(skillId) => {
              setSkillEditorFocusId(skillId)
              setView('skills')
            }}
          /></div>}

          {view === 'skills' && (
            <div className="workspace-scroll">
              <section className="work-section skills-work-section">
                <div className="section-heading">
                  <div><span className="eyebrow">可审计执行单元</span><h2>分析 Skill</h2></div>
                  <Pill tone="success">用户版本可热加载</Pill>
                </div>
                {skillVersionLibrary && <div className="skill-version-selector table-shell">
                  <label className="skill-version-control"><span>运行时 Skill 配置</span><select value={selectedSkillProfile?.profile_id ?? ''} onChange={(event) => selectSkillProfile(event.target.value)}>{skillVersionLibrary.profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.label_zh} · {profile.profile_id}</option>)}</select></label>
                  <div className="skill-version-summary"><strong>{selectedSkillProfile?.label_zh}</strong><p>{selectedSkillProfile?.description_zh}</p><small>{skillVersionLibrary.selection_scope_zh}</small></div>
                  <details><summary>查看此版本配置</summary><ul>{Object.entries(selectedSkillProfile?.skills ?? {}).map(([skillId, version]) => <li key={skillId}><code>{skillId}</code><span>v{version.version} · {version.prompt_mode === 'replace' ? '精简替换' : '兼容追加'}{version.content_chars ? ` · ${version.content_chars.toLocaleString()} 字` : ''} · {version.content_sha256.slice(0, 12)}</span></li>)}</ul></details>
                </div>}
                <SkillLibraryManager
                  profileId={selectedSkillProfile?.profile_id ?? skillVersionLibrary?.default_profile_id ?? ''}
                  presentations={analysisSkillCatalog.map((skill) => ({
                    skill_id: skill.id,
                    label_zh: skill.label,
                    description_zh: skill.detail,
                    target_view: skill.targetView,
                    prompt_slot: skillPromptSlots[skill.id],
                    fallback_content: applySkillPrompt(skill.prompt ?? '', selectedSkillProfile, skillPromptSlots[skill.id] ?? ''),
                    execution: skill.execution,
                    input: skill.input,
                    output: skill.output,
                    boundary: skill.boundary,
                  }))}
                  onLibraryChanged={refreshSkillVersionLibrary}
                  onOpenWorkspace={(target) => setView(target as ViewId)}
                  focusSkillId={skillEditorFocusId}
                />
              </section>
              <HardwareSemanticRoutingEvaluation targetPlatformProfile={targetPlatformProfile} />
              <HardwareStrategyEvaluation targetPlatformProfile={targetPlatformProfile} />
            </div>
          )}

          {view === 'knowledge' && (
            <div className="workspace-scroll">
              <section className="work-section">
                <div className="section-heading"><div><span className="eyebrow">分层检索</span><h2>{knowledgeBase === 'optimization' ? '优化知识库' : knowledgeBase === 'hardware' ? '硬件知识库' : knowledgeBase === 'npu_api' ? 'NPU API 知识库' : knowledgeBase === 'accelerator_api' ? 'GPU / DSP / 媒体 API 知识库' : '自建 API 知识库'}</h2></div>{knowledgeBase === 'hardware' && <label className="check-control"><input type="checkbox" checked={includeUnverified} onChange={(event) => setIncludeUnverified(event.target.checked)} />包含开发阶段记录</label>}</div>
                <div className="trace-explorer-tabs knowledge-tabs" role="tablist" aria-label="知识库类型">
                  <button className={knowledgeBase === 'optimization' ? 'selected' : ''} type="button" role="tab" aria-selected={knowledgeBase === 'optimization'} onClick={() => {
                    setKnowledgeBase('optimization')
                    if (!kbSearches.optimization) void searchKnowledge(query, false, 'optimization')
                  }}><Database size={15} />优化知识库</button>
                  <button className={knowledgeBase === 'hardware' ? 'selected' : ''} type="button" role="tab" aria-selected={knowledgeBase === 'hardware'} onClick={() => {
                    setKnowledgeBase('hardware')
                    if (!kbSearches.hardware) void searchKnowledge(query, includeUnverified, 'hardware')
                  }}><Network size={15} />硬件知识库</button>
                  <button className={knowledgeBase === 'npu_api' ? 'selected' : ''} type="button" role="tab" aria-selected={knowledgeBase === 'npu_api'} onClick={() => {
                    setKnowledgeBase('npu_api')
                    if (!npuApiCatalog) void loadNpuApiCatalog()
                  }}><Network size={15} />NPU API</button>
                  <button className={knowledgeBase === 'accelerator_api' ? 'selected' : ''} type="button" role="tab" aria-selected={knowledgeBase === 'accelerator_api'} onClick={() => {
                    setKnowledgeBase('accelerator_api')
                    if (!acceleratorApiCatalog) void loadAcceleratorApiCatalog(acceleratorApiUnit)
                  }}><Network size={15} />GPU / DSP / 媒体 API</button>
                  <button className={knowledgeBase === 'custom_api' ? 'selected' : ''} type="button" role="tab" aria-selected={knowledgeBase === 'custom_api'} onClick={() => {
                    setKnowledgeBase('custom_api')
                    if (!customApiKbCatalog) void loadCustomApiKnowledgeBases()
                  }}><Sparkles size={15} />自建 API</button>
                </div>
                <p className="knowledge-kb-note">{knowledgeBase === 'optimization'
                  ? '优化知识库只展示 40 条严格确认的异构案例。点击案例可查看 CPU/software fallback、代码特征摘要、函数级 AST、异构实现与当前源码一致时的 CPU 函数代码。案例证明源码关系，不等价于当前设备可用或已有收益。'
                  : knowledgeBase === 'hardware'
                    ? '硬件知识库收录源码可见的硬件执行路径、接口、约束和关联案例。所有设备可用性、驱动格式、性能与功耗结论仍必须由目标设备证据确认。'
                    : knowledgeBase === 'npu_api'
                      ? 'NPU API 知识库从 OpenHarmony NNRT 和 MindSpore Lite 公开头文件生成。它列出图算子和构图/设备/编译/执行入口；Top-N 只表示源码语义接近，仍须验证完整图、实际 deviceID、算子支持、编译与端到端基准。'
                      : knowledgeBase === 'accelerator_api'
                        ? 'GPU、DSP 与媒体 API 知识库从当前 OpenHarmony 源码生成。它严格区分公开 SDK、系统源码集成和 HDI/VDI 合同；Top-N 仅在图像、PCM effect 或 codec 生命周期等领域信号存在时返回，仍需运行时设备与端到端验证。'
                        : '自建 API 知识库来自用户登记代码仓的有界合同扫描，优先公开 API、算子/能力枚举和 HDI/VDI/IDL 接口，内部实现仅作为补充证据。它与平台内置知识库分开保存；用户可以删除自建库，发布合并只由开发者完成。'}</p>
                {knowledgeBase === 'custom_api'
                  ? <div className="custom-api-builder table-shell">
                    <label><span>代码仓</span><select value={customApiConnectorId} onChange={(event) => setCustomApiConnectorId(event.target.value)}>{(overview?.connectors ?? []).map((connector) => <option value={connector.connector_id} key={connector.connector_id}>{connector.display_name} · {connector.source_root}</option>)}</select></label>
                    <label><span>异构单元名称</span><input value={customApiUnitName} onChange={(event) => setCustomApiUnitName(event.target.value)} placeholder="例如 NPU、AAE、ISP" maxLength={80} /></label>
                    <button className="primary-button" type="button" onClick={requestCustomApiKnowledgeBaseBuild} disabled={!customApiUnitName.trim() || !customApiConnectorId}><Sparkles size={15} />交给页面助手构建</button>
                  </div>
                  : knowledgeBase === 'npu_api'
                  ? <form className="search-bar" onSubmit={(event) => { event.preventDefault(); void loadNpuApiCatalog(npuApiQuery) }}><Search size={17} /><input value={npuApiQuery} onChange={(event) => setNpuApiQuery(event.target.value)} placeholder="NNRT 算子、公开 API、语义族或工作流职责" /><button className="primary-button" type="submit" disabled={activeKnowledgeBusy}>{activeKnowledgeBusy ? <LoaderCircle size={15} className="spin" /> : <Search size={15} />}检索 API</button></form>
                  : knowledgeBase === 'accelerator_api'
                    ? <form className="search-bar" onSubmit={(event) => { event.preventDefault(); void loadAcceleratorApiCatalog(acceleratorApiUnit, acceleratorApiQuery) }}><Search size={17} /><input value={acceleratorApiQuery} onChange={(event) => setAcceleratorApiQuery(event.target.value)} placeholder="API 名称、图像 / PCM / codec 语义、公开层级或严格案例" /><button className="primary-button" type="submit" disabled={activeKnowledgeBusy}>{activeKnowledgeBusy ? <LoaderCircle size={15} className="spin" /> : <Search size={15} />}检索 API</button></form>
                    : <form className="search-bar" onSubmit={(event) => { event.preventDefault(); void searchKnowledge(query, knowledgeBase === 'hardware' ? includeUnverified : false, knowledgeBase) }}><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={knowledgeBase === 'optimization' ? '案例名、算法、CPU fallback 或异构单元' : '硬件单元、接口、算子、数据路径或约束'} /><button className="primary-button" type="submit" disabled={activeKnowledgeBusy}>{activeKnowledgeBusy ? <LoaderCircle size={15} className="spin" /> : <Search size={15} />}检索</button></form>}
                <div className="knowledge-kb-metadata"><span><strong>{stringValue(activeKnowledgeMetadata?.kb_id, knowledgeBase === 'custom_api' ? `${customApiKbCatalog?.count ?? 0} 份自建库` : '')}</strong><small>{stringValue(activeKnowledgeMetadata?.version, stringValue(asRecord(activeKnowledgeMetadata).kb_version))}</small></span><span>{knowledgeBase === 'optimization' ? `${numberValue(asRecord(activeKnowledgeMetadata?.summary).strict_case_count)} 条异构案例` : knowledgeBase === 'hardware' ? `${numberValue(asRecord(activeKnowledgeMetadata?.summary).record_count)} 条记录` : knowledgeBase === 'npu_api' ? `${npuApiCatalog?.record_count ?? 0} 条 API 记录` : knowledgeBase === 'accelerator_api' ? `${acceleratorApiCatalog?.record_count ?? 0} 条 API / 集成入口` : `${selectedCustomApiKb?.record_count ?? 0} 条 API 合同候选`}</span><span>设备可用性：{knowledgeBase === 'optimization' ? '由硬件知识库单独判断' : '仍需设备证据'}</span></div>

                {knowledgeBase === 'optimization' ? <>
                  <div className="table-shell heterogeneous-case-table"><div className="table-header heterogeneous-case"><span>异构案例</span><span>异构单元</span><span>CPU/software fallback</span><span>CPU 代码特征摘要</span><span /></div><div className="knowledge-table-scroll">{strictOptimizationCases.map((record) => {
                    const caseRecord = asRecord(record.record)
                    const fallback = asRecord(caseRecord.cpu_software_fallback)
                    const featureSummary = asRecord(caseRecord.cpu_code_feature_summary)
                    const recordId = stringValue(record.record_id, '')
                    const selected = recordId === selectedHeterogeneousCaseId
                    const loading = busyAction === `heterogeneous-case:${recordId}`
                    return <button className={`table-row heterogeneous-case-row${selected ? ' selected' : ''}`} type="button" key={recordId} aria-expanded={selected} onClick={() => void openHeterogeneousCase(recordId)}><div><strong>{stringValue(record.name)}</strong><span>{stringValue(record.algorithm)} · {stringValue(caseRecord.source_group)}</span></div><span><strong>{stringValue(record.target_unit)}</strong><small>{stringValue(caseRecord.accelerator_kind)}</small></span><span title={stringValue(fallback.implementation)}>{stringValue(fallback.implementation)}</span><span title={stringValue(featureSummary.summary)}>{stringValue(featureSummary.summary)}</span><span>{loading ? <LoaderCircle size={14} className="spin" /> : <ChevronRight size={16} />}</span></button>
                  })}{strictOptimizationCases.length === 0 && <div className="knowledge-empty">没有匹配的异构案例。</div>}</div></div>
                  {selectedHeterogeneousCaseId && !selectedHeterogeneousCase && <div className="case-detail-loading"><LoaderCircle size={15} className="spin" />正在读取异构案例详情…</div>}
                  {selectedHeterogeneousCase && <HeterogeneousCaseDetailPanel detail={selectedHeterogeneousCase} onClose={() => { setSelectedHeterogeneousCaseId(null); setSelectedHeterogeneousCase(null) }} />}
                </> : knowledgeBase === 'hardware' ? <div className="table-shell hardware-kb-table"><div className="table-header hardware-kb"><span>硬件单元 / 来源记录</span><span>类型</span><span>关联异构案例</span><span>设备可用性</span></div><div className="knowledge-table-scroll">{hardwareRecords.map((record) => {
                  const hardwareRecord = asRecord(record.record)
                  const strictCaseIds = Array.isArray(hardwareRecord.strict_case_ids) ? hardwareRecord.strict_case_ids : []
                  return <div className="table-row hardware-kb" key={`${stringValue(record.record_type)}:${stringValue(record.record_id)}`}><div><strong>{stringValue(record.name)}</strong><span>{stringValue(record.record_type)} · {stringValue(hardwareRecord.software_interface, '源码能力记录')}</span></div><span>{stringValue(hardwareRecord.unit_type)}</span><span>{strictCaseIds.length > 0 ? `${strictCaseIds.length} 条` : '—'}</span><span><Pill tone="warning">{displayState(hardwareRecord.device_availability, '需要设备证据')}</Pill></span></div>
                })}{hardwareRecords.length === 0 && <div className="knowledge-empty">没有匹配的硬件知识记录。</div>}</div></div> : knowledgeBase === 'npu_api' ? <>
                  <div className="table-shell npu-api-table"><div className="table-header npu-api"><span>算子 / API</span><span>语义族 / 职责</span><span>一句话摘要</span><span>严格案例</span><span>设备</span><span /></div><div className="knowledge-table-scroll">{(npuApiCatalog?.records ?? []).map((record) => {
                    const recordId = stringValue(record.record_id, '')
                    const family = asRecord(record.semantic_family)
                    const strictCaseIds = stringList(record.strict_case_ids)
                    const selected = recordId === selectedNpuApiRecordId
                    const loading = busyAction === `npu-api-record:${recordId}`
                    const isOperation = stringValue(record.record_type) === 'npu_operation'
                    return <button className={`table-row npu-api-row${selected ? ' selected' : ''}`} type="button" key={recordId} aria-expanded={selected} onClick={() => void openNpuApiRecord(recordId)}><div><strong>{stringValue(record.symbol)}</strong><span>{isOperation ? `NNRT 图算子 #${numberValue(record.operation_id)}` : '公开 API 入口'}</span></div><span>{isOperation ? stringValue(family.label_zh) : stringValue(record.workflow_role)}</span><span title={stringValue(record.one_line_abstract_zh)}>{stringValue(record.one_line_abstract_zh)}</span><span>{strictCaseIds.length > 0 ? `${strictCaseIds.length} 条` : '—'}</span><span><Pill tone="warning">{displayState(record.device_availability, '需要设备证据')}</Pill></span><span>{loading ? <LoaderCircle size={14} className="spin" /> : <ChevronRight size={16} />}</span></button>
                  })}{!npuApiCatalog && <div className="knowledge-empty">正在读取 NPU API 目录…</div>}{npuApiCatalog && npuApiCatalog.records.length === 0 && <div className="knowledge-empty">没有匹配的 NPU API 记录。</div>}</div></div>
                  {selectedNpuApiRecordId && !selectedNpuApiRecord && <div className="case-detail-loading"><LoaderCircle size={15} className="spin" />正在读取 NPU API 详情…</div>}
                  {selectedNpuApiRecord && <NpuApiDetailPanel detail={selectedNpuApiRecord} onClose={() => { setSelectedNpuApiRecordId(null); setSelectedNpuApiRecord(null) }} />}
                </> : knowledgeBase === 'accelerator_api' ? <>
                  <div className="trace-explorer-tabs knowledge-tabs accelerator-api-unit-tabs" role="tablist" aria-label="加速器 API 单元">
                    {(['gpu', 'dsp', 'media'] as AcceleratorUnit[]).map((unit) => {
                      const labels: Record<AcceleratorUnit, string> = { gpu: 'GPU', dsp: 'DSP / 音频', media: '媒体编解码' }
                      return <button className={acceleratorApiUnit === unit ? 'selected' : ''} type="button" role="tab" aria-selected={acceleratorApiUnit === unit} key={unit} onClick={() => {
                        setAcceleratorApiUnit(unit)
                        setSelectedAcceleratorApiRecordId(null)
                        setSelectedAcceleratorApiRecord(null)
                        if (!acceleratorApiCatalogs[unit]) void loadAcceleratorApiCatalog(unit)
                      }}>{labels[unit]}</button>
                    })}
                  </div>
                  <div className="table-shell npu-api-table"><div className="table-header npu-api"><span>API / 集成入口</span><span>语义族</span><span>一句话摘要</span><span>暴露层级</span><span>严格案例</span><span /></div><div className="knowledge-table-scroll">{(acceleratorApiCatalog?.records ?? []).map((record) => {
                    const recordId = stringValue(record.record_id, '')
                    const family = asRecord(record.semantic_family)
                    const strictCaseIds = stringList(record.strict_case_ids)
                    const selected = recordId === selectedAcceleratorApiRecordId
                    const loading = busyAction === `accelerator-api-record:${acceleratorApiUnit}:${recordId}`
                    return <button className={`table-row npu-api-row${selected ? ' selected' : ''}`} type="button" key={recordId} aria-expanded={selected} onClick={() => void openAcceleratorApiRecord(recordId)}><div><strong>{stringValue(record.symbol)}</strong><span>{stringValue(record.accelerator_unit)} · 源码目录</span></div><span>{stringValue(family.label_zh)}</span><span title={stringValue(record.one_line_abstract_zh)}>{stringValue(record.one_line_abstract_zh)}</span><span><Pill tone="neutral">{stringValue(record.exposure)}</Pill></span><span>{strictCaseIds.length > 0 ? `${strictCaseIds.length} 条` : '—'}</span><span>{loading ? <LoaderCircle size={14} className="spin" /> : <ChevronRight size={16} />}</span></button>
                  })}{!acceleratorApiCatalog && <div className="knowledge-empty">正在读取 {acceleratorApiUnit.toUpperCase()} API 目录…</div>}{acceleratorApiCatalog && acceleratorApiCatalog.records.length === 0 && <div className="knowledge-empty">没有匹配的 API / 集成入口。</div>}</div></div>
                  {selectedAcceleratorApiRecordId && !selectedAcceleratorApiRecord && <div className="case-detail-loading"><LoaderCircle size={15} className="spin" />正在读取加速器 API 详情…</div>}
                  {selectedAcceleratorApiRecord && <AcceleratorApiDetailPanel detail={selectedAcceleratorApiRecord} onClose={() => { setSelectedAcceleratorApiRecordId(null); setSelectedAcceleratorApiRecord(null) }} />}
                </> : <>
                  <div className="custom-api-kb-list table-shell">
                    <div className="table-header custom-api-kb"><span>自建知识库</span><span>代码仓 / 快照</span><span>API 候选</span><span>扫描状态</span><span /></div>
                    <div className="knowledge-table-scroll">{(customApiKbCatalog?.knowledge_bases ?? []).map((kb) => {
                      const source = asRecord(kb.source)
                      const summary = asRecord(kb.summary)
                      const scan = asRecord(kb.scan)
                      const quality = asRecord(kb.quality)
                      const selected = kb.kb_id === selectedCustomApiKbId
                      const loading = busyAction === `custom-api-kb:${kb.kb_id}`
                      return <div className={`table-row custom-api-kb-row${selected ? ' selected' : ''}`} key={kb.kb_id}>
                        <button className="custom-api-kb-open" type="button" aria-expanded={selected} onClick={() => void openCustomApiKnowledgeBase(kb.kb_id)}><span><strong>{kb.display_name}</strong><small>{kb.kb_id}</small></span><span><strong>{stringValue(source.display_name)}</strong><small title={stringValue(source.source_root)}>{stringValue(source.source_snapshot_id)}</small></span><span><strong>{numberValue(summary.record_count)}</strong><small>{numberValue(summary.public_contract_record_count)} 条公开/契约</small></span><span><Pill tone={quality.status === 'warning' || scan.timed_out ? 'warning' : 'success'}>{quality.status === 'warning' ? '质量待复核' : scan.timed_out ? '超时截断' : '质量通过'}</Pill></span><span>{loading ? <LoaderCircle size={14} className="spin" /> : <ChevronRight size={16} />}</span></button>
                        <button className="icon-button custom-api-delete" type="button" title={`删除 ${kb.display_name}`} aria-label={`删除 ${kb.display_name}`} disabled={busyAction === `custom-api-delete:${kb.kb_id}`} onClick={() => void deleteCustomApiKnowledgeBase(kb)}>{busyAction === `custom-api-delete:${kb.kb_id}` ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}</button>
                      </div>
                    })}{customApiKbCatalog && customApiKbCatalog.knowledge_bases.length === 0 && <div className="knowledge-empty">尚无自建 API 知识库。</div>}{!customApiKbCatalog && <div className="knowledge-empty">正在读取自建 API 知识库…</div>}</div>
                  </div>
                  {selectedCustomApiKb && <>
                    <div className="custom-api-selected-meta">
                      <div><span className="eyebrow">当前自建库</span><strong>{selectedCustomApiKb.knowledge_base.display_name}</strong><small title={stringValue(asRecord(selectedCustomApiKb.knowledge_base.source).source_root)}>{stringValue(asRecord(selectedCustomApiKb.knowledge_base.source).display_name)} · {stringValue(asRecord(selectedCustomApiKb.knowledge_base.source).source_snapshot_id)}</small></div>
                      <div><span>{numberValue(asRecord(selectedCustomApiKb.knowledge_base.scan).files_read)} 个文件</span><span>公开/契约 {numberValue(asRecord(selectedCustomApiKb.knowledge_base.summary).public_contract_record_count)}</span><span>算子 {numberValue(asRecord(selectedCustomApiKb.knowledge_base.summary).operator_record_count)}</span><span>内部 {numberValue(asRecord(selectedCustomApiKb.knowledge_base.summary).internal_implementation_record_count)}</span><Pill tone={asRecord(selectedCustomApiKb.knowledge_base.quality).status === 'warning' ? 'warning' : 'success'}>{asRecord(selectedCustomApiKb.knowledge_base.quality).status === 'warning' ? '质量待复核' : '质量通过'}</Pill></div>
                    </div>
                    <form className="search-bar custom-api-search" onSubmit={(event) => { event.preventDefault(); if (selectedCustomApiKbId) void openCustomApiKnowledgeBase(selectedCustomApiKbId, customApiQuery) }}><Search size={17} /><input value={customApiQuery} onChange={(event) => setCustomApiQuery(event.target.value)} placeholder="API 名称、声明、注释、语义角色或数据形态" /><button className="primary-button" type="submit" disabled={activeKnowledgeBusy}>{activeKnowledgeBusy ? <LoaderCircle size={15} className="spin" /> : <Search size={15} />}检索</button></form>
                    <div className="table-shell custom-api-record-table"><div className="table-header custom-api-record"><span>API 候选</span><span>语义角色</span><span>一句话摘要</span><span>源码位置</span><span /></div><div className="knowledge-table-scroll">{selectedCustomApiKb.records.map((record) => {
                      const recordId = stringValue(record.record_id)
                      const family = asRecord(record.semantic_family)
                      const source = asRecord(record.source_contract)
                      const selected = recordId === selectedCustomApiRecordId
                      const loading = busyAction === `custom-api-record:${recordId}`
                      return <button className={`table-row custom-api-record-row${selected ? ' selected' : ''}`} type="button" key={recordId} aria-expanded={selected} onClick={() => void openCustomApiRecord(recordId)}><div><strong>{stringValue(record.symbol)}</strong><span>{stringValue(record.exposure)}</span></div><span>{stringValue(family.label_zh)}</span><span title={stringValue(record.one_line_abstract_zh)}>{stringValue(record.one_line_abstract_zh)}</span><span title={stringValue(source.source_file)}>{stringValue(source.source_file)}:{numberValue(source.source_line)}</span><span>{loading ? <LoaderCircle size={14} className="spin" /> : <ChevronRight size={16} />}</span></button>
                    })}{selectedCustomApiKb.records.length === 0 && <div className="knowledge-empty">没有匹配的自建 API 记录。</div>}</div></div>
                  </>}
                  {selectedCustomApiRecordId && !selectedCustomApiRecord && <div className="case-detail-loading"><LoaderCircle size={15} className="spin" />正在读取自建 API 详情…</div>}
                  {selectedCustomApiRecord && <CustomApiDetailPanel detail={selectedCustomApiRecord} onClose={() => { setSelectedCustomApiRecordId(null); setSelectedCustomApiRecord(null) }} />}
                </>}
              </section>
            </div>
          )}

          {view === 'sources' && (
            <div className="workspace-scroll">
              <section className="work-section">
                <div className="section-heading"><div><span className="eyebrow">连接器</span><h2>已登记代码仓</h2></div><Pill tone="success">只读</Pill></div>
                <div className="connector-list">{(overview?.connectors ?? []).map((connector) => <div className={`connector-row ${connector.connector_id === connectorId ? 'selected' : ''}`} key={connector.connector_id}><GitBranch size={17} /><div><strong>{connector.display_name}</strong><span>{connector.source_root}</span></div><div><span>{connector.snapshot.snapshot_id}</span><Pill tone={connector.snapshot.clean ? 'success' : 'warning'}>{connector.snapshot.clean ? '工作树干净' : '包含本地修改'}</Pill>{connector.kind === 'local' && <button className="icon-button" type="button" title="刷新当前工作树快照，无需提交修改" aria-label="刷新当前工作树快照" disabled={busyAction === `snapshot:${connector.connector_id}`} onClick={() => void refreshSourceSnapshot(connector)}>{busyAction === `snapshot:${connector.connector_id}` ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={15} />}</button>}<button className="icon-button" type="button" title="选择此代码仓" aria-label="选择此代码仓" onClick={() => setSelectedConnectorId(connector.connector_id)}>{connector.connector_id === connectorId ? <Check size={15} /> : <ChevronRight size={15} />}</button></div></div>)}</div>
              </section>
              <section className="work-section sdk-source-section">
                <div className="section-heading"><div><span className="eyebrow">运行资产</span><h2>真机构建 SDK</h2></div><Pill tone={openharmonySdkReady ? 'success' : 'warning'}>{openharmonySdkReady ? '已就绪' : '待生成'}</Pill></div>
                <div className="sdk-provision-form">
                  <label><span>OpenHarmony 代码仓</span><select value={openharmonySdkConnectorId} onChange={(event) => setOpenharmonySdkConnectorId(event.target.value)}>{(overview?.connectors ?? []).map((connector) => <option value={connector.connector_id} key={connector.connector_id}>{connector.display_name} · {connector.source_root}</option>)}</select></label>
                  <button className="primary-button" type="button" disabled={!openharmonySdkConnectorId || busyAction === 'openharmony-sdk-provision'} onClick={() => void provisionOpenHarmonySdk()}>{busyAction === 'openharmony-sdk-provision' ? <LoaderCircle size={15} className="spin" /> : <HardDrive size={15} />}{openharmonySdkReady ? '更新 SDK' : '生成 SDK'}</button>
                </div>
                <div className="sdk-asset-status"><span title={stringValue(openharmonySdkBundle.sdk_root)}>{stringValue(openharmonySdkBundle.sdk_root, '尚未生成运行资产')}</span><strong>{stringValue(openharmonySdkSource.display_name, '未绑定代码仓')}</strong>{numberValue(asRecord(openharmonySdkBundle.manifest).file_count) > 0 && <small>{numberValue(asRecord(openharmonySdkBundle.manifest).file_count)} 个文件 · {stringValue(openharmonySdkBundle.manifest_sha256).slice(0, 12)}</small>}</div>
              </section>
              <section className="work-section source-forms">
                <div className="section-heading"><div><span className="eyebrow">本地路径</span><h2>登记本地代码仓</h2></div></div>
                <div className="inline-form"><input value={localPath} onChange={(event) => setLocalPath(event.target.value)} placeholder="/绝对路径/源码根目录" /><button className="quiet-button" type="button" disabled={busyAction === 'local-connector'} onClick={() => void registerLocal()}>{busyAction === 'local-connector' ? <LoaderCircle size={14} className="spin" /> : <FolderGit2 size={14} />}登记</button></div>
                <div className="section-heading git-heading"><div><span className="eyebrow">Git 连接器</span><h2>创建受控克隆计划</h2></div></div>
                <div className="inline-form git-form"><input value={gitUrl} onChange={(event) => setGitUrl(event.target.value)} placeholder="https://github.com/org/repository.git" /><input value={gitName} onChange={(event) => setGitName(event.target.value)} placeholder="显示名称（可选）" /><button className="quiet-button" type="button" disabled={busyAction === 'clone-plan'} onClick={() => void createClonePlan()}>{busyAction === 'clone-plan' ? <LoaderCircle size={14} className="spin" /> : <HardDrive size={14} />}计划</button></div>
                {clonePlan && <div className={`clone-plan ${clonePlan.capacity.can_clone ? 'ready' : 'blocked'}`}><div><strong>{clonePlan.capacity.can_clone ? '硬门禁通过' : '硬门禁未通过'}</strong><span>{formatBytes(clonePlan.capacity.available_bytes)} 可用 / {formatBytes(clonePlan.capacity.required_total_bytes)} 需要</span></div><ul>{clonePlan.hard_gates.map((gate) => <li key={gate}>{gate}</li>)}</ul><button className="primary-button" type="button" disabled={!clonePlan.capacity.can_clone || busyAction === 'clone-execute'} onClick={() => void executeClone()}>{busyAction === 'clone-execute' ? <LoaderCircle size={15} className="spin" /> : <GitBranch size={15} />}执行已校验克隆</button></div>}
              </section>
            </div>
          )}

          {view === 'results' && (
            <div className="workspace-scroll">
              <section className="work-section">
                <div className="section-heading"><div><span className="eyebrow">机会报告</span><h2>{analysisRun ? analysisRun.artifact_id : '等待分析运行'}</h2></div>{opportunityReport && <Pill tone={opportunityReport.state === 'development_candidate' ? 'success' : 'warning'}>{reportStateLabel(opportunityReport.state)}</Pill>}</div>
                {!opportunityReport ? <EmptyState title="尚无机会报告" detail="从追踪场景选择一个事件并完成机会识别。" /> : <>
                  {reportBlockingConditions.length > 0 && <div className="report-gates"><CircleAlert size={17} /><div><strong>证据门禁</strong>{reportBlockingConditions.map((condition) => <span key={condition}>{displayBlockingCondition(condition)}</span>)}</div></div>}
                  <div className="report-summary"><span>{numberValue(reportSummary.selected_hotspot_count)} 个已选热点</span><span>{numberValue(reportSummary.temporal_eligible_count)} 个通过时间判据</span><span>{numberValue(reportSummary.ast_available_count)} 个 AST 已就绪</span><span>{numberValue(reportSummary.kb_match_count)} 个 KB 命中</span></div>
                  <div className="table-shell report-table"><div className="table-header report"><span>机会 / 建议</span><span>状态</span><span>置信度</span><span>证据</span></div>{reportOpportunities.map((item) => <div className="table-row report" key={stringValue(item.opportunity_id)}><div><strong>{reportOpportunityTitle(item)}</strong><span>{reportOpportunityDetail(item)}</span></div><span><Pill tone={item.state === 'development_candidate' ? 'warning' : 'neutral'}>{reportStateLabel(item.state)}</Pill></span><span>{reportConfidence(item.confidence)}</span><span>{Array.isArray(item.evidence_refs) ? item.evidence_refs.length : 0} 条引用</span></div>)}</div>
                </>}
              </section>
            </div>
          )}

          {inspection && <section className="inspection-drawer"><div className="inspection-heading"><div><span className="eyebrow">源码检查</span><h2>{stringValue(asRecord(inspection.source).path)}</h2></div><button className="icon-button" type="button" title="关闭源码检查" aria-label="关闭源码检查" onClick={() => setInspection(null)}><X size={16} /></button></div><div className="inspection-meta"><Pill tone={inspection.feature_status === 'available' ? 'success' : 'warning'}>{displayState(inspection.feature_status)}</Pill><span>{stringValue(asRecord(inspection.source).sha256).slice(0, 16)}</span><span>{numberValue(inspectionPrecheck.loop_tokens)} 个循环 · {numberValue(inspectionPrecheck.branch_tokens)} 个分支</span></div>{inspectionDiagnostics.length > 0 && <div className="diagnostic-list">{inspectionDiagnostics.slice(0, 3).map((item) => <span key={item}>{item}</span>)}</div>}<div className="inspection-content"><pre>{Array.isArray(inspectionPrecheck.source_excerpt) ? (inspectionPrecheck.source_excerpt as Array<JsonRecord>).map((item) => `${String(item.line).padStart(5, ' ')}  ${stringValue(item.text, '')}`).join('\n') : ''}</pre><div className="feature-list">{inspectionFeatures.length > 0 ? inspectionFeatures.map((feature, index) => <div key={index}><strong>{stringValue(feature.function)}</strong><span>{Array.isArray(feature.loops) ? `${feature.loops.length} 个 AST 循环` : 'AST 特征'}</span></div>) : <span>精准 AST 适配器尚未产出特征；保留非语义预检与诊断。</span>}</div></div></section>}
        </section>

        <AgentPanel actions={pageActions} context={agentContext} onExecuteAction={executeAgentAction} workflow={optimizationWorkflow} onImportTraceFile={importTraceFromAssistant} skillProfile={selectedSkillProfile} />
      </div>
    </main>
  )
}
