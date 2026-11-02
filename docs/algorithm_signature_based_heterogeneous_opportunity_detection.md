# 基于算法签名的异构优化机会识别

> 目标：建立一套不依赖大模型、完全由工具链实现的异构优化机会发现方法。  
> 核心思想：把 CPU 实现抽象为**算法签名**（循环、数据类型、访存、运算、领域语义），从已有 fallback 对中学习"CPU 算法模式 → 异构单元"映射，再对 trace 热点做模式匹配。

---

## 零、当前实现状态与优先级

本文保留了 clang-query、libclang 和 LLVM IR 的研究性设计与实验记录，但它们不再被表述为已经落地的唯一主路径。当前工作区可复现、已用于严格 source-confirmed pair 的函数级基线如下：

1. 先以硬件路径、CPU/software 路径和可证明回退关系建立严格 pair；候选、组件事实和 source-confirmed pair 不得混用。
2. 对能解析到唯一 CPU/software 定义的 pair，使用 `build_cpu_fallback_ast_profiles.py` 的 Tree-sitter C/C++ 后端提取函数范围、控制流、访存、调用、类型、并行 pragma、算法标签和 fingerprint。
3. 工厂、dispatcher、runtime provider、动态软件后端和保留原 CPU 子图必须标为 `not_ready`；它们不是“没有 fallback”，而是尚未获得可安全匹配的具体算法函数。
4. 仅对 `ready` 的函数 profile，或由 trace/runtime 继续解析出的具体 callee，执行热点代码的结构匹配。命中只给出相似性或已有实现模式，仍要经过时间压力、搬运/同步成本、能力和正确性门禁。
5. clang-query / libclang 是高价值候选的语义增强与复核手段，适合需要真实编译参数、宏展开、类型、跨过程调用或别名信息的场景；它们的结果必须携带独立 backend/provenance，不能覆盖 Tree-sitter profile 的解析状态。

截至当前基线，严格 pair 共 80 条，其中 29 个 CPU/software 函数 profile 为源码 AST `ready`，51 个为 `not_ready`。源码 AST `ready` 不能单独作为特征匹配许可；必须通过 `compute_core` 的独立函数体或调用图证据门禁，当前已自动解锁的 profile 为 0。完整证据、代表案例和限制见 [严格 pair 总览](strict_heterogeneous_cpu_fallback_overview.md) 与 [知识库 README](../data/knowledge-bases/patterns/README.md)。下文历史章节中将 clang-query 称为“主路径”的位置，应以本节和权威 Spec 为准。

---

## 一、为什么不能用函数名匹配

| 问题 | 说明 |
|------|------|
| **函数名语义弱** | 不同模块的 CPU fallback 可能函数名完全不同，但算法一样 |
| **函数粒度粗** | 一个 CPU 函数可能包含多个算法分支，只有部分适合异构 |
| **跨模块难复用** | 模块自定义函数无法跨模块匹配，但算法模式可以 |
| **无法识别新机会** | 对于没有已知异构对的 CPU 函数，函数名匹配给不出建议 |

**结论**：必须下沉到实现逻辑，提取**算法签名**。

---

## 二、算法签名是什么

算法签名是描述一个计算片段本质特征的特征向量：

```json
{
  "function": "JpegDecoder::DoSwDecode",
  "file": "libjpegplugin/src/jpeg_decoder.cpp",
  "loop_nesting": 2,
  "loop_bounds": ["image_height", "scanline"],
  "data_types": ["uint8", "JSAMPLE"],
  "memory_pattern": "scanline_sequential",
  "operations": ["DCT", "color_space_convert", "upsampling"],
  "domain_keywords": ["jpeg", "scanline", "YCbCr", "RGB"],
  "api_calls": ["jpeg_read_scanlines", "memcpy"],
  "branch_density": "medium",
  "input_shape": ["H", "W", "C"],
  "compute_intensity": "high"
}
```

### 2.1 签名维度

| 维度 | 具体特征 | 为什么重要 |
|------|---------|-----------|
| **循环结构** | 嵌套层数、循环变量关系、trip count | 判断并行度、是否适合 GPU |
| **数据类型** | float32/float16/int8/uint8 | NPU 偏好低精度，GPU 偏好 float |
| **访存模式** | 连续、stride、随机、neighbor 访问 | 决定零拷贝和 cache 效率 |
| **运算类型** | MAC、卷积、插值、排序、分支 | 映射到不同加速器 |
| **领域语义** | jpeg、blur、resize、conv、yuv、hdr | 直接对应硬件能力 |
| **API 调用** | Skia、libjpeg、Eigen、memcpy | 判断已有优化层级 |
| **输入形状** | 1D/2D/3D tensor，HWC/CHW | NPU/GPU 对 shape 敏感 |
| **分支密度** | if/else 数量、循环内分支 | 分支多则 GPU 效率低 |

---

## 三、工具链实现方案

### 3.1 分层工具链

```text
┌─────────────────────────────────────────────────────────────┐
│  第一层：大规模初筛（轻量、快速、全量扫描）                      │
│  工具：Python + Tree-sitter / Regex                           │
│  脚本：docs_ffrt/tools/function_signature_extractor.py        │
│  输出：候选函数列表 + 粗略结构签名（用于缩小第二层输入范围）       │
├─────────────────────────────────────────────────────────────┤
│  第二层：函数级特征提取（针对已绑定候选）                        │
│  工具：Tree-sitter C/C++（当前基线）                            │
│  脚本：docs_ffrt/tools/build_cpu_fallback_ast_profiles.py     │
│  输出：函数 profile（范围、控制流、访存、调用、类型、标签）       │
├─────────────────────────────────────────────────────────────┤
│  第三层：fallback 对挖掘                                        │
│  工具：Tree-sitter + 启发式规则                                │
│  脚本：docs_ffrt/tools/fallback_pair_extractor.py             │
│  输出：已有 (hw_func, sw_func) 映射样本                         │
├─────────────────────────────────────────────────────────────┤
│  第四层：语义增强与模式复核（按需）                              │
│  工具：clang-query / libclang（需可靠编译参数）                  │
│  脚本：docs_ffrt/tools/ast_pattern_validator.py                │
│  输出：确认的算法模式（三重循环 MAC、2D 邻居访存等）              │
├─────────────────────────────────────────────────────────────┤
│  第五层：模式匹配与机会识别                                     │
│  工具：签名相似度计算 + 规则库                                  │
│  脚本：tools/analysis/matching_engine.py                      │
│  输出：异构优化机会清单                                         │
├─────────────────────────────────────────────────────────────┤
│  补充层：全局关系与模块依赖（可选、复用已有 Doxygen）             │
│  工具：Doxygen + Graphviz + doxygen_bridge.py                   │
│  输出：调用图、include 依赖图、模块 DAG 判断                     │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 工具选择对比

| 工具 | 优点 | 缺点 | 适用阶段 |
|------|------|------|---------|
| **Regex** | 最快、零依赖 | 不准确、易误报 | 初筛 |
| **Tree-sitter** | 轻量、可在未构建大仓时定位和解析函数，输出稳定结构 profile | 不提供完整类型、宏展开和跨翻译单元语义 | 当前 CPU fallback 函数 profile 基线、初筛 |
| **clang-query AST matcher** | matcher DSL 表达力强、可利用真实编译参数 | 依赖 LLVM、编译数据库和可解析的构建上下文 | 高价值候选的语义模式复核 |
| **libclang AST** | 类型、符号与调用信息更丰富 | 依赖编译环境，复杂模式需手写遍历 | Clang 语义增强、调用/类型补充 |
| **LLVM IR** | 跨语言、指令级 | 需要编译通过、门槛高 | 底层性能特征 |
| **Doxygen XML** | 全局调用图、目录依赖、跨文件关系 | 解析慢、需要先生成 XML | 模块关系图、调用图补充 |

### 3.3 Doxygen 桥接（补充全局关系）

`docs_ffrt/tools/doxygen_bridge.py` 把 Doxygen XML 输出转换为工具链可用的结构化数据：

- **函数调用图**：每个函数的 caller / callee 列表，补全 `precise_feature_extractor` 的跨文件调用。
- **文件 include 依赖**：文件引用了哪些头文件、被哪些文件引用。
- **目录/模块依赖**：聚合到目录级和模块级，生成 DOT 图并判断是否为 DAG。

用法：

```bash
# 1. 生成 Doxygen XML（已提供 camera_framework/Doxyfile）
cd openharmony/foundation/multimedia/camera_framework
doxygen Doxyfile

# 2. 桥接到工具链
python docs_ffrt/tools/doxygen_bridge.py \
  --doxygen-xml openharmony/foundation/multimedia/camera_framework/doxygen_output/xml \
  --out camera_framework_doxygen_report.json \
  --module-dot-out camera_framework_modules.dot \
  --top-modules common,dynamic_libs,frameworks,interfaces,mediastream,saver,services,test,tools

# 3. 渲染模块关系图
dot -Tpng camera_framework_modules.dot -o camera_framework_modules.png
```

输出示例（camera_framework）：

```json
{
  "summary": {
    "directories": 563,
    "files": 1550,
    "functions": 13853,
    "directory_edges": 1438,
    "module_edges": 26
  },
  "is_module_dag": false,
  "cycles": [
    {"level": "module", "path": ["common", "dynamic_libs", "common"]},
    {"level": "module", "path": ["frameworks", "interfaces", "frameworks"]}
  ]
}
```

> **注意**：Doxygen 的模块关系图基于 `#include` 依赖，不是构建依赖，因此通常**不是 DAG**。如果需要 DAG，应基于 `.so`/`.gn` 编译目标重新建模。

### 3.4 函数 profile 与 Clang 语义复核的关系

第二层先生成可复现的 Tree-sitter 函数 profile；第四层仅在需要更深语义时使用 Clang。两层的目标和输出不同：

| 维度 | 第二层：函数级 profile | 第四层：`ast_pattern_validator.py` / Clang 增强 |
|------|-------------------------------------------|-----------------------------------|
| **目标** | 为每个精确绑定函数生成稳定结构特征 | 验证高价值候选是否命中某个具体异构模式 |
| **输出** | 函数范围、循环、访存、调用、类型、分支、并行与标签 | 模式命中、类型/调用补充和解析质量 |
| **扫描范围** | 对候选函数做全面结构遍历 | 只关心模式相关 AST/Sema 子结构 |
| **典型结构** | function / loop / branch / call / pointer / subscript 节点 | `functionDecl(hasDescendant(...))` 等组合谓词 |
| **下游消费** | CPU fallback profile 匹配与相似性检索 | 过滤误报、确认语义 |

**结论**：

- 第二层不依赖 LLVM，先以 Tree-sitter 形成可审计的 profile；解析失败、函数不唯一或回退实现为动态 provider 时必须显式输出原因。
- 第四层可使用 `clang-query` 或 `libclang`。它们是增强而非静默回退：任务需声明编译参数、backend 版本、解析失败和新增/修正的语义证据。

---

## 四、第一层：大规模初筛工具

### 4.1 目标

在几分钟内扫描整个 `source_code/`，把函数标记为候选类型：

- `CANDIDATE_GPU_SHADER`：逐像素图像处理
- `CANDIDATE_NPU`：矩阵/卷积计算
- `CANDIDATE_CODEC`：编解码相关
- `CANDIDATE_FFRT_ASYNC`：同步阻塞可异步化
- `CANDIDATE_ZERO_COPY`：频繁 memcpy

### 4.2 实现方式

用 Tree-sitter 做轻量语法分析 + 启发式规则。

```python
# 示例：扫描 2D 逐像素循环
query = """
(for_statement
  condition: (binary_expression
    left: (identifier) @y
    right: (identifier) @height)
  body: (compound_statement
    (for_statement
      condition: (binary_expression
        left: (identifier) @x
        right: (identifier) @width)
      body: (compound_statement) @pixel_body)))
"""
```

具体实现见 `docs_ffrt/tools/function_signature_extractor.py`。

> 注意：第一层输出的是**粗略结构签名**，可以进入 `matching_engine.py` 做低置信度试探，但不能替代第二层函数级 profile。正式机会识别应使用精确绑定的 Tree-sitter profile；需要类型、宏或跨过程语义时，再附加 `precise_feature_extractor_cq.py` / libclang 输出，否则循环、访存、调用、运算细节不足，容易没有结果或产生误报。

---

## 五、函数级 AST 特征提取：Tree-sitter 基线与 Clang 语义增强

### 5.1 目标

第二层负责从严格 pair 中已精确绑定的 CPU/software 函数，以及 trace 已归因的热点 callee 中提取算法签名。当前基线使用 Tree-sitter 直接对函数定义生成结构化 profile；它保存解析质量，绝不把 factory、dispatcher 或运行时 provider 冒充算法函数。

本节随后保留的 clang-query matcher、JSON 和性能实验，是**可选的语义增强方案**：当需要解析宏、模板、完整类型或调用图，且有可靠 `compile_commands.json` 时才启用。它不能改变 source binding，也不能把一个 `not_ready` 的动态回退直接升级为可匹配函数。

### 5.2 何时启用 clang-query

- **与第四层共享后端**：离线建立知识库时只需部署一套 LLVM 工具链。
- **声明式提取**：用 `forStmt()`、`arraySubscriptExpr()` 等 matcher 直接表达“我要所有 for 循环 / 数组下标”，不需要手写递归遍历。
- **快且稳**：在示例文件上，clang-query 比 libclang 手写遍历快 2~3 倍（见 7.6 节），且解析 C++ 标准库头更稳。

### 5.3 关键特征与对应 matcher

| 特征维度 | clang-query matcher | 说明 |
|---|---|---|
| 函数定义 | `functionDecl(isDefinition()).bind("func")` | 定位每个函数起点，划分函数范围 |
| 参数 | `parmVarDecl(hasAncestor(functionDecl(isDefinition()))).bind("param")` | 提取参数类型与名字 |
| for 循环 | `forStmt().bind("loop_for")` | 循环结构、条件、递增 |
| while / do | `whileStmt().bind("loop_while")` / `doStmt().bind("loop_do")` | 其他循环形式 |
| 数组访存 | `arraySubscriptExpr().bind("array_access")` | C 风格数组 / 指针下标访问 |
| 函数调用 | `callExpr().bind("call")` | 普通函数、成员函数、`operator[]` |
| 二元 / 一元运算 | `binaryOperator().bind("binop")` / `unaryOperator().bind("unop")` | 运算密度统计 |
| 局部变量 | `varDecl(hasAncestor(...), unless(parmVarDecl())).bind("var")` | 排除参数 |
| 分支 | `ifStmt().bind("if")` / `switchStmt()` / `conditionalOperator()` | 控制流密度 |

### 5.4 工具用法

```bash
# 1. 准备 clang-query（macOS ARM64 示例，已下载到 tools/.llvm）
CQ=docs_ffrt/tools/.llvm/LLVM-22.1.8-macOS-ARM64/bin/clang-query

# 2. 单文件
python docs_ffrt/tools/precise_feature_extractor_cq.py \
  --src docs_ffrt/tools/test_blur.cpp \
  --out /tmp/cq_feats_blur.jsonl \
  --clang-query $CQ

# 3. 使用 compile_commands.json
python docs_ffrt/tools/precise_feature_extractor_cq.py \
  --src source_code/graphic_graphic_2d/.../rs_render_kawase_blur_filter.cpp \
  --out feats.jsonl \
  --compile-commands path/to/compile_commands.json
```

### 5.5 真实输出示例：CpuGaussianBlur

对 `docs_ffrt/tools/test_blur.cpp` 中的 `CpuGaussianBlur`，脚本输出如下（节选）：

```json
{
  "file": "docs_ffrt/tools/test_blur.cpp",
  "function": "CpuGaussianBlur",
  "signature": "void CpuGaussianBlur(const float* src, float* dst, int width, int height, const float* kernel, int radius)",
  "return_type": "void",
  "line": 5,
  "parameters": [
    {"name": "src", "type": "const float*"},
    {"name": "dst", "type": "float*"},
    {"name": "width", "type": "int"},
    {"name": "height", "type": "int"},
    {"name": "kernel", "type": "const float*"},
    {"name": "radius", "type": "int"}
  ],
  "loops": [
    {"type": "for", "depth": 0, "line": 11, "condition": "y < height", "increment": "++y"},
    {"type": "for", "depth": 1, "line": 12, "condition": "x < width",  "increment": "++x"},
    {"type": "for", "depth": 2, "line": 14, "condition": "k <= radius", "increment": "++k"}
  ],
  "memory_accesses": [
    {"kind": "array_subscript", "base": "src",    "index": "y * width + sx", "stride_hint": "row_major_2d"},
    {"kind": "array_subscript", "base": "kernel", "index": "k + radius",     "stride_hint": "linear_combination"},
    {"kind": "array_subscript", "base": "tmp",    "index": "y * width + x",  "stride_hint": "row_major_2d"},
    {"kind": "array_subscript", "base": "dst",    "index": "y * width + x",  "stride_hint": "row_major_2d"}
  ],
  "operators": ["*", "+", "++", "+=", "-", "<", "<=", ">="],
  "operation_hints": ["mac"],
  "has_neighbor_access": true,
  "loop_count": 6,
  "max_loop_depth": 3,
  "array_access_count": 6,
  "call_count": 0,
  "unique_calls": [],
  "branch_count": 4,
  "types_used": ["float", "int"]
}
```

从这份签名可以读出：

- **2D + 半径维度**：最大循环嵌套 3 层，适合 GPU / NPU 并行。
- **row_major_2d 访存**：连续 2D 行主序访问，适合 GPU shared memory / texture。
- **MAC 运算**：`*` 与 `+` 同时出现，提示卷积 / 乘加累加。
- **邻居访存**：`sx = x + k`、`sy = y + k`，适合 GPU shader / Hianimation 模糊。
- **分支**：4 个 if 做边界 clamp，会让 GPU warp divergence，但现代 shader 可用 `clamp` / texture border 消除。

### 5.6 真实输出示例：CpuSoftmax

对 `docs_ffrt/tools/test_sample.cpp` 中的 `CpuSoftmax`：

```json
{
  "file": "docs_ffrt/tools/test_sample.cpp",
  "function": "CpuSoftmax",
  "signature": "float CpuSoftmax(const std::vector<float>& x, std::vector<float>& out)",
  "return_type": "float",
  "parameters": [
    {"name": "x", "type": "const std::vector<float>&"},
    {"name": "out", "type": "std::vector<float>&"}
  ],
  "loops": [
    {"type": "for", "depth": 0, "line": 6,  "condition": "i < x.size()"},
    {"type": "for", "depth": 0, "line": 10, "condition": "i < x.size()"},
    {"type": "for", "depth": 0, "line": 14, "condition": "i < x.size()"}
  ],
  "memory_accesses": [],
  "calls": [
    {"callee": "x", "location": {"line": 5}},
    {"callee": "size", "location": {"line": 6}},
    {"callee": "x", "location": {"line": 7}},
    {"callee": "size", "location": {"line": 10}},
    {"callee": "expf", "location": {"line": 11}},
    {"callee": "out", "location": {"line": 12}},
    {"callee": "size", "location": {"line": 14}},
    {"callee": "out", "location": {"line": 15}}
  ],
  "operators": ["++", "+=", "-", "/=", "<", ">"],
  "operation_hints": ["exp"],
  "loop_count": 3,
  "max_loop_depth": 1,
  "array_access_count": 0,
  "call_count": 11,
  "unique_calls": ["expf", "out", "size", "x"],
  "branch_count": 1,
  "types_used": ["float", "size_t", "std::vector"]
}
```

注意：

- `std::vector` 的 `operator[]` 在 AST 上是 `callExpr`，因此 `memory_accesses` 为空，但 `calls` 里出现了 `x`、`out`、`size`。
- `expf` 调用 + 3 次一维循环 → 典型的 softmax 规约模式，适合 NPU。

### 5.7 从 binding 到函数级 JSON 的聚合逻辑

clang-query 对每个 matcher 输出类似：

```text
/path/file.cpp:11:10: note: "loop_for" binds here
/path/file.cpp:18:24: note: "array_access" binds here
```

脚本按以下步骤聚合：

1. **函数范围划分**：用 `functionDecl(isDefinition())` 的起点把源文件切成若干函数区间（假设顶层函数不交错）。
2. **binding 归属**：按行号把每个 binding 丢进对应的函数区间。
3. **结构化转换**：
   - 参数：从函数签名中拆分出每个参数，再分离类型和名字。
   - 循环：从 for / while 语句头中提取条件、递增，并用大括号匹配计算嵌套深度。
   - 数组访存：用正则从 `[index]` 中提取 base、index，并推断 stride hint。
   - 调用：提取 callee 名字；`operator[]` 的 base（如 `x`、`out`）也被识别。
   - 局部变量：从声明行提取类型和名字；循环变量同样支持。
   - 分支：收集 if 条件文本。
4. **派生特征**：`loop_count`、`max_loop_depth`、`array_access_count`、`call_count`、`unique_calls`、`branch_count`、`operation_hints`（如 mac / exp / sqrt）。

### 5.8 当前局限与补充

| 局限 | 说明 | 补充方式 |
|---|---|---|
| `std::vector` 访问不进入 `memory_accesses` | 它在 AST 上是 `operator[]` 调用 | 看 `calls` / `unique_calls` 中的 `x`、`out`、`size` |
| 只分析单文件 | 不知道跨文件调用关系 | `doxygen_bridge.py` / `compile_commands.json` |
| 复杂类型推导、模板、宏展开 | 正则无法精确还原 | `precise_feature_extractor.py`（libclang）回退 |
| 函数范围基于行号 | 不支持交错或 lambda | 先用 `functionDecl` 起点分段，再用 brace 匹配 |

### 5.9 libclang / LLVM IR 回退

- `precise_feature_extractor.py`（libclang）继续作为**完整精确后端**保留；当环境没有 clang-query 或需要更深层语义（类型推导、调用图）时回退使用。
- LLVM IR 特征提取（`ll_feature_extractor.py`）可后续补充指令级混合与向量化 hint。

### 5.10 AST 特征提取优化

大仓里不能对所有 C/C++ 文件无差别跑 clang-query。推荐流程是：

```text
trace 热点 / 第一层粗筛
        ↓
生成候选文件列表 file-list
        ↓
precise_feature_extractor_cq.py 只分析候选文件
        ↓
matching_engine.py 做机会识别
```

`precise_feature_extractor_cq.py` 已做以下优化：

| 优化点 | 说明 |
|---|---|
| 单次加载 `compile_commands.json` | 避免每个文件重复解析编译数据库 |
| 源码行缓存 | 每个文件只读一次，binding 聚合时直接复用行文本 |
| 函数起点缓存 | 函数归属用预计算 start line + `bisect`，避免每个 binding 重建列表 |
| 默认跳过隐藏/构建/工具链目录 | 避免误扫 `.llvm`、`.git`、`__pycache__`、`build`、`out` 等目录 |
| `--file-list` | 直接输入 trace/粗筛得到的候选文件列表 |
| `--exclude-regex` | 按路径过滤测试、third_party 或其他不关心目录 |
| `--jobs` | 可选多文件并行分析；建议小规模验证后再提高并发 |

用法示例：

```bash
# 只分析候选文件，而不是全仓扫描
python docs_ffrt/tools/precise_feature_extractor_cq.py \
  --src source_code \
  --file-list /tmp/hot_files.txt \
  --out /tmp/precise_features.jsonl \
  --jobs 2

# 扫目录时排除测试目录
python docs_ffrt/tools/precise_feature_extractor_cq.py \
  --src source_code/graphic_graphic_2d \
  --exclude-regex '/test/|/unittest/' \
  --out /tmp/graphic_features.jsonl
```

后续还可以继续加：

- **函数级 file-list**：只提取某些热点函数，而不是文件内全部函数；
- **内容 hash 缓存**：源码和编译参数未变时复用上次 JSON；
- **失败样本黑名单**：对长期解析失败或超时的头文件/模板文件自动跳过；
- **IR/Sema 补充层**：只对高价值候选启用更重的类型、别名、数据依赖分析。

---

## 六、第三层：fallback 对挖掘

### 6.1 从已有 fallback 对中学习

代码里已有的 fallback 结构是最好的训练样本：

```cpp
// 模式 A：if (hw) { hw(); } else { sw(); }
if (CanUseHardware(...)) {
    return HardwarePath(...);
}
return SoftwarePath(...);

// 模式 B：hw fail → sw fallback
if (hw_impl != nullptr) {
    ret = hw_impl->Do(...);
    if (ret == SUCCESS) return ret;
}
return SoftwareDo(...);

// 模式 C：ability check 守卫
if (!IsHardwareDecodeSupported(...)) {
    // caller 走软件路径
}
```

用 `docs_ffrt/tools/fallback_pair_extractor.py` 自动提取这些 `(hw_func, sw_func)` 对。

### 6.2 构建算法模式库

```yaml
algorithm_patterns:
  - name: jpeg_decode
    signatures:
      cpu: [scanline_loop, uint8, YCbCr, DCT, color_convert]
      heterogeneous: [CODEC_IMAGE_JPEG, HW_DECODER, YCbCr_only]
    target_unit: JPEG_HW_DECODER
    capability_constraints: [format==JPEG, color_space==YCbCr, size_in_range]
    
  - name: gaussian_blur
    signatures:
      cpu: [2D_loop, float32, neighbor_access, separable_kernel]
      heterogeneous: [Hianimation, blur_radius_range]
    target_unit: Hianimation
    
  - name: image_resize_up
    signatures:
      cpu: [2D_loop, interpolation, src<dst]
      heterogeneous: [AAE_ARSR]
    target_unit: AAE
    
  - name: matrix_multiply
    signatures:
      cpu: [triple_loop, float32, MAC]
      heterogeneous: [NPU, GPU_compute]
    target_unit: NPU
```

## 七、第四层：AST 模式复核（语义级确认）

`docs_ffrt/tools/ast_pattern_validator.py` 基于 **clang-query AST matcher** 做可复用的语义级模式验证。当环境中未安装 `clang-query` 时，会尝试回退到 `ast_pattern_validator_libclang.py`（libclang 手写遍历）。如果 libclang Python bindings 也不存在，工具会明确报错并提示安装 LLVM 或指定 `--clang-query`。

### 7.1 为什么需要 AST 复核

`function_signature_extractor` 和 `precise_feature_extractor` 是**快速/全面**的，但有时会误判：

- 把普通的三重循环当成 GEMM
- 把字符串处理当成数组访存
- 把含大量分支的循环当成可向量化

AST 模式复核用 **clang-query AST matcher** 做**语义级检查**（未安装 `clang-query` 时回退到 libclang）：

- 判断循环嵌套里是否有真正的乘加累加
- 判断数组下标是否连续/仿射
- 判断循环内是否有复杂控制流破坏并行
- 识别 `exp`/`sqrt` 等 math API 是否出现在循环内

### 7.2 内置模式

| 模式 | 说明 | 典型异构目标 |
|------|------|------------|
| `triple_loop_with_mac` | 三重循环 + 乘加累加 | NPU / GPU compute |
| `pixel_wise_2d` | 二层循环 + 逐像素数组访问 | GPU shader |
| `reduction_with_exp` | 规约 + `exp` 调用 | NPU / GPU compute |
| `neighbor_access_2d` | 2D 邻居访存（如 i±1, j±1） | GPU shader / Hianimation |
| `vectorizable_loop` | 单层循环 + 连续访存 + 简单算术 | SIMD / GPU |
| `parallelizable_loop` | 单层循环 + 无调用/分支/规约 + 仿射下标 | OpenMP / FFRT / GPU |

### 7.3 用法

```bash
# 复核单个候选函数
python docs_ffrt/tools/ast_pattern_validator.py \
  --src source_code/.../hot_func.cpp \
  --patterns all \
  -I path/to/include \
  --out validated_patterns.jsonl

# 只验证特定模式
python docs_ffrt/tools/ast_pattern_validator.py \
  --src source_code/.../hot_func.cpp \
  --patterns triple_loop_with_mac,pixel_wise_2d \
  --compile-commands path/to/compile_commands.json \
  --out validated_patterns.jsonl
```

输出示例（clang-query 后端）：

```json
{
  "file": "docs_ffrt/tools/test_blur.cpp",
  "function": "CpuGaussianBlur",
  "line": 5,
  "column": 1,
  "patterns": [
    {
      "pattern": "triple_loop_with_mac",
      "confidence": 0.85
    },
    {
      "pattern": "neighbor_access_2d",
      "confidence": 0.78
    }
  ]
}
```

> 注：若系统未安装 `clang-query`，工具会尝试回退到 libclang 手写遍历后端；该路径需要 Python `libclang` bindings。回退成功时输出会包含 `signature` 和更详细的 `evidence` 字段。

### 7.7 并行化机会识别（补充）

AST 不仅可以识别“适合哪种异构硬件”，还可以识别**代码本身能否并行执行**。分为两个层次：

#### 1. 循环级数据并行（Loop-level parallelism）

这是 AST 最容易识别的并行模式。判断标准：

- 单层循环，没有嵌套循环或循环内复杂控制流；
- 循环体对不同迭代的数据没有写后读/读后写依赖；
- 没有函数调用、没有规约操作（`+=` / `*=` 等）；
- 数组下标只依赖循环变量（仿射下标）。

`ast_pattern_validator.py` 内置了 `parallelizable_loop` 模式，对应这种场景。命中后可建议：

- OpenMP / pthread 并行；
- FFRT `task` 并行化；
- GPU compute shader / SIMD 向量化。

示例：

```cpp
void ScaleEachRow(float* dst, const float* src, int n) {
    for (int i = 0; i < n; ++i) {
        dst[i] = src[i] * 2.0f;  // 每次迭代独立，可并行
    }
}
```

#### 2. 函数级任务并行（Function-level parallelism）

你提到的场景：一个函数里顺序调用 `a()`、`b()`、`c()`，三者是否可并行？

**理论上可以，但 AST 单独做不够。** 需要判断 `a`、`b`、`c` 之间是否存在数据依赖或顺序约束：

| 需要分析的内容 | AST 能提供 | 还需什么 |
|--------------|-----------|---------|
| 调用顺序 | ✅ 语句在 AST 中的位置 | — |
| 是否读写同一变量/内存 | ⚠️ 只能看参数是否同名 | 别名分析、全局状态分析 |
| 是否有副作用（IO、锁、修改全局变量） | ⚠️ 能看到函数签名和直接调用 | 跨函数副作用分析、调用图 |
| 返回值是否被后续使用 | ✅ AST 可见 | — |

**可行做法**：

1. 用 AST 找出“顺序执行的多个调用语句”。
2. 用启发式快速排除明显不能并行的情况：
   - 参数或返回值存在数据流重叠；
   - 调用的是 `HILOG`、`printf`、`lock`、`memcpy` 等带副作用或阻塞语义的函数；
3. 对剩余候选，用**保守的静态依赖分析**或**动态 trace** 验证是否真的独立。

例如：

```cpp
void PrepareBuffers(Buffer* a, Buffer* b, Buffer* c) {
    InitBuffer(a);  // 写 a
    InitBuffer(b);  // 写 b
    InitBuffer(c);  // 写 c
    // 三者参数不同、无返回值依赖 → 可并行候选
}
```

这种函数级并行识别可以作为工具链的**第六层补充**：

```text
静态调用图 + AST 参数分析 → 候选并行调用组
                ↓
        保守依赖 / 别名分析过滤
                ↓
        trace 验证真实调用耗时
                ↓
        输出：可将顺序调用改为 task/fork-join 的机会
```

#### 3. 当前工具状态

- `parallelizable_loop` 已加入 `ast_pattern_validator.py`。
- 函数级任务并行暂未实现，但方法论和所需信息已经明确：AST + 调用图 + 副作用/别名分析 + trace。

---

### 7.4 扩展新模式

在 `ast_pattern_validator.py` 的 `PATTERN_MATCHERS` 中新增一个 matcher 字符串即可。模板中保留 `).bind("PATTERN")`，运行时会自动替换为独立的绑定名（如 `p_my_new_pattern`）：

```python
PATTERN_MATCHERS["my_new_pattern"] = {
    "matcher": (
        'functionDecl(isDefinition(), '
        'hasDescendant(/* 你的 matcher 条件 */)'
        ').bind("PATTERN")'
    ),
    "confidence": 0.8,
}
```

如果要实现 libclang 手写遍历无法表达的复杂模式（如“嵌套三层循环内某两个下标呈仿射关系”），可以把 matcher 写好后放在这里；对外 CLI 和输出格式保持不变。

### 7.5 为什么优先 clang-query（libclang 作为回退）

`ast_pattern_validator.py` 现在**优先使用 `clang-query`** 执行 AST 模式复核。对比维度如下：

| 维度 | clang-query | libclang 手写遍历 |
|------|-------------|------------------|
| **部署依赖** | 需要 LLVM 工具链及 `clang-query` 二进制 | 只需 `pip install libclang` |
| **模式表达** | matcher DSL 直接描述嵌套结构、仿射下标、调用组合 | 复杂模式需要手写递归，容易遗漏 |
| **执行性能** | matcher 引擎优化更好 | 单文件够用，复杂规则脚本膨胀 |
| **结果精度** | 在 `test_blur.cpp` 上额外命中 `neighbor_access_2d` | 邻居访存 heuristic 正则会漏判 |
| **集成方式** | 子进程调用，输出需解析 | Python 模块，可直接读写 JSON/YAML |

**当前选择**：
- 默认走 `clang-query`，因为它更快、更准，尤其适合离线建立优化知识库。
- 当环境中没有 `clang-query` 时，尝试回退到 `ast_pattern_validator_libclang.py`；如果 libclang bindings 未安装，会给出明确错误，避免静默失败。
- 后续新增复杂模式优先用 matcher DSL 实现，放在 `PATTERN_MATCHERS` 中。

> **备注**：`clang-query` 是开源的（源码在 LLVM 仓库 `clang-tools-extra/clang-query`），随 LLVM 一起发布。

### 7.6 clang-query 性能 / 效果对比实验

在同样的示例文件上，对 libclang 手写遍历与 clang-query matcher 做了 20 次平均对比。

**测试环境**
- Apple Silicon (arm64), macOS 24.6
- libclang：PyPI `libclang-18.1.1`
- clang-query：LLVM 22.1.8 预编译二进制

**性能对比**

| 测试文件 | libclang 平均耗时 | clang-query 平均耗时 | 加速比 |
|---------|------------------|---------------------|--------|
| `test_blur.cpp` | 147.9 ms | 74.5 ms | ~2.0× |
| `test_sample.cpp` | 708.8 ms | 262.1 ms | ~2.7× |

> 说明：`test_sample.cpp` 包含 `<vector>`，libclang 版本在解析 C++ 标准库头文件时明显更慢；clang-query 因为有完整的 LLVM 工具链和资源目录，解析更快。

**效果对比**

| 测试文件 | 后端 | 命中的模式 | 备注 |
|---------|------|-----------|------|
| `test_blur.cpp` | libclang | `triple_loop_with_mac` | 邻居访存模式因启发式正则限制未命中 |
| `test_blur.cpp` | clang-query | `triple_loop_with_mac` + `neighbor_access_2d` | AST matcher 更准确识别了 `src[y*width+sx]` 这类带 `+` 的下标 |
| `test_sample.cpp` | libclang | `reduction_with_exp` | — |
| `test_sample.cpp` | clang-query | `reduction_with_exp` | — |

**结论**

- `clang-query` 在该实验上**更快、模式表达能力更强**（如邻居访存、仿射下标等复杂结构用 matcher DSL 更自然）。
- 因此 `ast_pattern_validator.py` 默认使用 `clang-query`；未安装 LLVM 时尝试回退到 libclang。

**用法示例**

```bash
# 需要提前安装 LLVM 或下载预编译二进制
# macOS: brew install llvm
# 未安装时会尝试回退到 libclang
python docs_ffrt/tools/ast_pattern_validator.py \
    --src docs_ffrt/tools/test_blur.cpp \
    --patterns all \
    --clang-query /path/to/llvm/bin/clang-query \
    --out /tmp/validated_patterns.jsonl
```

---

## 八、第五层：模式匹配与机会识别

### 8.0 两类 JSON 的边界

异构优化知识库和异构优化机会识别结果必须拆成两个文件，不能混用：

| 文件 | 典型名称 | 生成时机 | 内容 | 是否依赖具体 trace |
|---|---|---|---|---|
| **异构优化知识库** | `heterogeneous_kb/` / `sample_kb.json` | 离线大规模扫描代码、挖 fallback 对、整理硬件能力后沉淀 | 算法模式、CPU 签名、硬件单元、capability、代价/反证规则、权重和阈值 | 否 |
| **异构优化机会文件** | `opportunities.jsonl` / `sample_opportunities.jsonl` | 针对具体场景采 trace，再把热点代码与知识库匹配后生成 | 场景、热点函数、runtime evidence、命中的 KB pattern、置信度、缺失条件、推荐动作 | 是 |

二者关系是**引用关系**，不是同一个 artifact：

```text
离线阶段：
  全仓源码扫描 + fallback 对挖掘 + 硬件能力整理
        ↓
  heterogeneous_kb/       （长期维护、可复用）

场景阶段：
  trace 热点 + 精准代码签名 + heterogeneous_kb/
        ↓
  opportunities.jsonl     （某个场景的一次诊断结果）
```

知识库条目回答“什么算法模式在什么约束下适合什么异构单元”；机会条目回答“某个场景里的某段热点代码是否命中这些判据，以及下一步该查什么/改什么”。因此机会文件只保存 `knowledge_base_pattern_id` 等引用和本次场景证据，不应该把知识库条目复制成新的事实源。

当前推荐的分层知识库目录是 `data/knowledge-bases/patterns/`：

| 分层文件 | 职责 |
|---|---|
| `code_signature_patterns.json` | 静态代码特征 → 算法语义，例如 blur、resize、jpeg decode、GEMM、softmax |
| `algorithm_hardware_mapping.json` | 算法语义 → 异构单元映射，包含权重、阈值、目标硬件和可选 capability |
| `hardware_capabilities.json` | 硬件本身的能力、入口函数、capability check、buffer/format/shape 约束 |
| `cost_models.json` | 数据搬运、调度、同步、format convert 等收益/风险判据 |
| `fallback_pairs.json` | 仓库中已存在的 CPU/HW fallback 对和失败原因 |
| `runtime_hotspot_rules.json` | 针对 trace 的热点判据和优先级提升规则 |

`sample_kb.json` 保留为旧版单文件兼容样例；新工具优先使用分层目录。

### 8.1 匹配引擎

工具：`tools/analysis/matching_engine.py`

匹配引擎把第二层输出的精准特征与异构优化知识库做规则化匹配，**不使用大模型**，核心逻辑：

1. **特征归一化**：把函数签名、循环、访存、调用、操作符等转换为可比较的集合。
2. **硬约束过滤**：检查 required_keywords、loop_nesting 范围、allowed_data_types、required_operations；不满足直接丢弃。
3. **加权相似度**：对每个知识库模式计算各维度相似度（pattern-recall  oriented），按可配置权重聚合为置信度。
4. **Top-K 输出**：输出独立的异构优化机会 JSONL，包含候选异构单元、置信度、命中/缺失特征、capability constraints、推荐理由。

工程实现上，`matching_engine.py` 同时兼容第一层粗筛 JSON 和第二层精准 JSON，但推荐输入仍然是第二层输出。第一层信息过粗，主要用于候选池和低成本 sanity check。

用法：

```bash
# 1. 提取热点函数精准特征（clang-query 版本）
python docs_ffrt/tools/precise_feature_extractor_cq.py \
  --src hot_func.cpp \
  --out query.jsonl \
  -I path/to/include \
  --clang-query docs_ffrt/tools/.llvm/LLVM-22.1.8-macOS-ARM64/bin/clang-query

# 2. 匹配知识库
python tools/analysis/matching_engine.py \
  --query query.jsonl \
  --kb data/knowledge-bases/patterns \
  --out opportunities.jsonl \
  --top-k 3 \
  --threshold 0.5
```

### 8.2 知识库条目示例

知识库文件是模式库，不包含某次 trace 的热点耗时。旧版单文件样例如 `docs_ffrt/tools/sample_kb.json`：

```json
{
  "pattern_id": "gaussian_blur_2d",
  "name": "2D Gaussian blur",
  "cpu_signature": {
    "domain_keywords": ["blur", "gaussian", "kawase", "mesa", "sigma", "radius", "filter"],
    "operations": ["convolution", "neighbor_access", "weighted_sum", "multi_pass"],
    "memory_patterns": ["array_subscript", "neighbor_access", "stride_unit_stride"],
    "data_types": ["float", "half", "uint8"],
    "loop_nesting_min": 2,
    "loop_nesting_max": 4
  },
  "heterogeneous": {
    "target_unit": "GPU_SHADER",
    "alternative_units": ["Hianimation", "AAE"],
    "capability_constraints": {
      "shape": "2D_image",
      "max_radius": 50,
      "data_types": ["float", "half"]
    }
  },
  "weights": {
    "domain_keywords": 3.0,
    "operations": 3.0,
    "memory_patterns": 2.5,
    "data_types": 1.5,
    "loop_nesting": 1.5
  },
  "threshold": 0.5,
  "hard_constraints": {
    "required_keywords": ["blur"]
  }
}
```

知识库里的 `hard_constraints` 还支持更保守的假阳性过滤，例如：

```json
{
  "required_operations": ["mac"],
  "loop_nesting_min": 3,
  "any_keywords": ["matmul", "gemm", "matrix", "dot", "linear"],
  "forbidden_keywords": ["blur", "gaussian", "kawase", "radius", "filter"]
}
```

这类约束用于避免“高斯模糊三层循环 + 乘加”被误判成 GEMM/NPU 机会。

### 8.2.1 机会条目示例

机会文件是场景诊断结果，必须带上 trace / hotspot 证据，并通过 `knowledge_base_pattern_id` 引用知识库。例如 `docs_ffrt/tools/sample_opportunities.jsonl` 中的一条记录：

```json
{
  "artifact_type": "heterogeneous_opportunity",
  "schema_version": "1.0",
  "scenario": {
    "name": "sample_blur_trace",
    "trace_file": "traces/sample_trace.ftrace",
    "process": "render_service",
    "thread": "RenderThread"
  },
  "query_file": "docs_ffrt/tools/test_blur.cpp",
  "query_function": "CpuGaussianBlur",
  "runtime_evidence": {
    "hotspot": true,
    "wall_time_ms": 12.4,
    "cpu_time_ms": 11.8,
    "call_count": 42,
    "critical_path": "frame_render"
  },
  "matches": [
    {
      "knowledge_base_pattern_id": "gaussian_blur_2d",
      "source_layers": {
        "code_signature_pattern_id": "code_sig_image_blur_2d",
        "algorithm": "image_blur",
        "mapping_id": "map_image_blur_to_gpu_shader",
        "hardware_id": "GPU_SHADER",
        "cost_model_id": "gpu_image_filter_cost"
      },
      "target_unit": "GPU_SHADER",
      "confidence": 0.6263,
      "recommendation": "Verify input format, radius, buffer type, and copy cost before implementation."
    }
  ]
}
```

### 8.3 匹配流程

```text
离线建库：
1. 全仓扫描 CPU 实现、异构实现、fallback 分发和 capability check
        ↓
2. 沉淀 heterogeneous_kb/

场景识别：
3. Trace 发现热点 CPU 函数 F
        ↓
4. AST 语义分析层
   ├─ precise_feature_extractor 提取 F 的完整算法签名
   └─ ast_pattern_validator 做 AST 级模式复核
        ↓
5. matching_engine 读取 heterogeneous_kb/ 做匹配
        ↓
6. 检查 capability_constraints / 代价模型 / 反证规则
        ↓
7. 输出 opportunities.jsonl：为什么没走异构 / 是否可改造使能 / 需要哪些 capability
```

### 8.4 验证示例

对示例 `docs_ffrt/tools/test_blur.cpp` 中的 `CpuGaussianBlur`：

```json
{
  "query_function": "CpuGaussianBlur",
  "matches": [
    {
      "pattern_id": "gaussian_blur_2d",
      "target_unit": "GPU_SHADER",
      "confidence": 0.6263,
      "scores": {
        "domain_keywords": 0.4286,
        "operations": 0.75,
        "memory_patterns": 0.6667,
        "data_types": 0.3333,
        "loop_nesting": 1.0
      }
    }
  ]
}
```

当前工具链回归结果：

- `function_signature_extractor.py` 粗筛输出可以被 `matching_engine.py` 读取，但因信息较粗，对 `CpuGaussianBlur` 不直接给出高置信度建议；
- `precise_feature_extractor_cq.py` 输出进入 `matching_engine.py` 后，使用分层 KB 得到 `map_image_blur_to_gpu_shader -> GPU_SHADER`，置信度约 `0.6263`；
- `ast_pattern_validator.py` 使用仓库内 LLVM `clang-query` 可命中 `triple_loop_with_mac` 和 `neighbor_access_2d`；
- 通过 `matrix_multiply` 的 `any_keywords` / `forbidden_keywords` 约束，已过滤 `CpuGaussianBlur -> matrix_multiply` 这类误报。

对示例 `docs_ffrt/tools/test_sample.cpp` 中的 `CpuSoftmax`：

```json
{
  "query_function": "CpuSoftmax",
  "matches": [
    {
      "pattern_id": "softmax",
      "target_unit": "NPU",
      "confidence": 0.5857
    }
  ]
}
```

---

## 九、已有 fallback 典型例子（算法签名视角）

### 9.1 JPEG 解码

| 对 | CPU 实现 | 异构实现 | 共同算法 |
|---|---------|---------|---------|
| 函数 | `JpegDecoder::DoSwDecode` | `JpegHardwareDecoder::Decode` | JPEG 解码 |
| 签名 | scanline_loop, uint8, YCbCr→RGB, DCT | JPEG HW, YCbCr 输入, 尺寸限制 | Baseline JPEG |

### 9.2 图像缩放

| 对 | CPU/GPU 实现 | 异构实现 | 共同算法 |
|---|-------------|---------|---------|
| 函数 | `ScaleByHDSampler` | `ScaleByAAE` | 图像上采样 |
| 签名 | 2D_loop, interpolation, HDSampleImageFilter | AAE_ARSR, DMA buffer, Vulkan | 超分上采样 |

### 9.3 模糊算法

| 对 | CPU/GPU 实现 | 异构实现 | 共同算法 |
|---|-------------|---------|---------|
| 函数 | `RSRenderKawaseBlurFilter::DrawBlur` | `HianimationManager::HianimationBuildTask` | 高斯模糊 |
| 签名 | 2D_loop, neighbor_access, separable, multi-pass | Hianimation IP, sigma/format 限制 | 模糊 |

### 9.4 HDR 处理

| 对 | GPU 实现 | 异构实现 | 共同算法 |
|---|---------|---------|---------|
| 函数 | `GenerateHDRHeteroShader` | `BuildHpaeHDRTask` | HDR tone mapping |
| 签名 | GPU Shader, float, color matrix | MDC, HDR/AIHDR channel | HDR 视频处理 |

### 9.5 JPEG 解码：具体代码对（可复用的 fallback 样本）

在 `source_code/multimedia_image_framework` 中可以找到一组清晰的 CPU / 硬件解码 fallback：

| 路径 | 函数 | 作用 |
|---|---|---|
| `source_code/multimedia_image_framework/plugins/common/libs/image/libjpegplugin/src/jpeg_decoder.cpp` | `JpegDecoder::DoSwDecode` | CPU 软件解码：分配 buffer、逐 scanline 调用 `jpeg_read_scanlines` |
| `source_code/multimedia_image_framework/plugins/common/libs/image/libextplugin/src/hardware/jpeg_hw_decoder.cpp` | `JpegHardwareDecoder::Decode` | 硬件解码：通过 `ICodecImage` HDI 调用 JPEG 硬件解码器 |
| `source_code/multimedia_image_framework/plugins/common/libs/image/libjpegplugin/src/jpeg_decoder.cpp` | `JpegDecoder::Decode` | fallback 分发：先尝试硬件，失败再调用 `DoSwDecode` |

**fallback 分发逻辑**（`jpeg_decoder.cpp:508-558`）：

```cpp
uint32_t JpegDecoder::Decode(uint32_t index, DecodeContext &context)
{
    // ... 状态检查与解码头准备 ...
    if (hwJpegDecompress_ != nullptr) {
        srcMgr_.inputStream->Seek(streamPosition_);
        uint32_t ret = hwJpegDecompress_->Decompress(&decodeInfo_, srcMgr_.inputStream, context);
        if (ret == Media::SUCCESS) {
            state_ = JpegDecodingState::IMAGE_DECODED;
            IMAGE_LOGD("jpeg hardware decode success.");
            ImageUtils::InvalidateContextSurfaceBuffer(context);
            return ret;
        }
    }
    uint32_t ret = DoSwDecode(context);
    if (ret == Media::SUCCESS) {
        state_ = JpegDecodingState::IMAGE_DECODED;
        IMAGE_LOGD("jpeg software decode success.");
        return Media::SUCCESS;
    }
    // ... 错误 / 部分图处理 ...
}
```

**硬件能力检查**（`jpeg_hw_decoder.cpp:107-143`）：

```cpp
bool JpegHardwareDecoder::IsHardwareDecodeSupported(const std::string& srcImgFormat, OHOS::Media::Size srcImgSize)
{
    if (hwDecoder_ == nullptr) { return false; }
    if (srcImgFormat != JPEG_FORMAT_DESC) { return false; }

    for (const CodecImageCapability& cap : capList_) {
        if (!cap.isSoftwareCodec && cap.role == CODEC_IMAGE_JPEG &&
            cap.type == CODEC_IMAGE_TYPE_DECODER &&
            srcImgSize.width >= static_cast<int32_t>(cap.minWidth) &&
            srcImgSize.width <= static_cast<int32_t>(cap.maxWidth) &&
            srcImgSize.height >= static_cast<int32_t>(cap.minHeight) &&
            srcImgSize.height <= static_cast<int32_t>(cap.maxHeight)) {
            return true;
        }
    }
    return false;
}
```

**对应的算法签名对比**：

| 签名维度 | CPU (`DoSwDecode`) | 硬件 (`JpegHardwareDecoder::Decode`) |
|---|---|---|
| 核心 API | `jpeg_read_scanlines` | `ICodecImage` HDI |
| 循环结构 | scanline 顺序循环 | 硬件内部 pipeline |
| 数据类型 | `uint8` RGBA / YCbCr | YCbCr 输入，DMA buffer 输出 |
| 输入约束 | 通用 | 仅 YCbCr、尺寸在 capability 范围内 |
| fallback 触发条件 | 硬件初始化失败 / 不支持格式或尺寸 / 解码失败 | — |

这正是我们建立知识库的典型样本：同一个 JPEG 解码算法，CPU 实现和硬件实现共享输入格式、颜色空间、尺寸约束，只有执行单元不同。

---

## 十、实际落地建议

### 10.1 分阶段实施

| 阶段 | 时间 | 产出 |
|------|------|------|
| Phase 1 | 1 周 | 跑通初筛脚本，输出候选函数列表 |
| Phase 2 | 1-2 周 | 对 Top 50 热点提取精准签名，建立 AST 模式库 |
| Phase 3 | 1-2 周 | 接入 ast_pattern_validator 做语义复核 |
| Phase 4 | 2 周 | 实现自动 fallback 对提取，扩充模式库 |
| Phase 5 | 1-2 周 | 接入匹配引擎，输出高置信度优化机会 |
| Phase 6 | 持续 | 把工具接入 CI，每次代码变更自动扫描新机会 |

### 10.2 避免误报

| 误报来源 | 解决方法 |
|---------|---------|
| 普通初始化代码被误判 | 过滤短函数、无循环函数 |
| 日志/DFX 代码干扰 | 忽略只调 `HILOG`/`RS_TRACE` 的函数 |
| 头文件/模板展开 | 只分析 `.cpp` 文件 |
| 测试代码 | 排除 `test/` 目录 |
| 粗筛误报 | 用第二层精准特征二次过滤 |
| 模式重叠（如 blur 像 GEMM） | AST 模式复核 + domain_keywords 硬约束 |

### 10.3 与 Trace 结合

静态分析只能判断**潜力**，trace 才能确认**真实热点**。 workflow：

```text
静态扫描 → 候选函数池
     ↕
   trace 热点 ∈ 候选函数池？
     ↓
   是 → AST 语义分析层
        ├─ precise_feature_extractor 提取精准特征
        └─ ast_pattern_validator AST 模式复核
     ↓
   matching_engine 匹配知识库
     ↓
   高优先级优化机会（带置信度）
```

### 10.4 Trace 簇、wakeup 链与时间局部性热点判据

> 本节保留算法签名与 trace 的衔接说明；wakeup 因果图、trace_cluster、时间局部性热点和输出契约的权威定义见 `trace_analysis_technology_spec.md`。

trace 输入不能直接按“进程总耗时”排序。业务进程常包含等待、Binder、Fence、调度和多个不相关场景；跨整段 trace 的累计耗时也会稀释短时卡顿。先构造因果执行图，再由用户或 Agent 选择可解释的 trace 簇。

#### 10.4.1 wakeup 因果图与簇

图节点是线程在时间区间内的 run/sleep/wakeup/sync slice，边至少包括：`wakeup`、`sched_switch`、`binder`、`fence`、`task_post` 和同线程时序边。边必须保留原始 event ID、时间戳和置信度；没有可靠因果证据时不能伪造边。

聚类流程：

```text
trace export
  -> 事件标准化与时钟校验
  -> wakeup/schedule/binder/fence 因果边
  -> 时间相邻 + 因果连通的 execution cluster
  -> 每簇统计关键路径、线程角色、函数 slice 与 burst
  -> 用户/Agent 选择 cluster
  -> cluster 内热点 -> AST/KB opportunity pipeline
```

每个 `trace_cluster` 至少包含 `cluster_id`、时间范围、参与进程/线程、根事件、因果边摘要、关键路径 wall time、clock/parse 质量、候选函数和 `selection_state`。`selection_state=pending` 的簇只能探索，不能生成 production opportunity。

#### 10.4.2 热点不是单一 total time

对函数 `f` 在簇 `c` 内同时计算：

| 指标 | 含义 | 防止的误判 |
|---|---|---|
| `total_active_ms` | 非阻塞执行 slice 的总时间 | 不把 sleep/fence 等待当 CPU 算法开销 |
| `call_count` / `rate_per_s` | 调用次数与频率 | 不把一次性初始化当高频核函数 |
| `avg_ms`、`p95_ms`、`max_ms` | 单次延迟分布 | 不被少数异常值或平均值掩盖 |
| `max_window_active_ms` | 任意短窗口（如帧预算或场景窗口）内的活跃时间 | 区分“10 秒累计 10ms”和“16ms 内 10ms” |
| `window_concentration` | `max_window_active_ms / total_active_ms` | 识别 burst 型压力 |
| `critical_path_ms` / `critical_path_ratio` | 在选定簇关键路径上的贡献 | 不优先优化旁路后台工作 |
| `self_active_ratio` | self 与 inclusive 活跃时间比例 | 区分自身计算和下游等待 |

热点分级采用场景 profile，而不是全局固定阈值。UI/渲染 profile 使用 frame budget 窗口；交互 profile 使用输入到呈现窗口；后台 batch profile 使用吞吐与 CPU 占用窗口。函数只有满足以下任一可解释条件才成为 `hotspot_candidate`：

1. 在关键路径上，且 `critical_path_ratio` 超过 profile 阈值；
2. `max_window_active_ms` 或 `p95_ms` 压迫该 profile 的延迟预算；
3. 高频执行且短窗口 active time 累积造成预算压力；
4. 经 Agent 选择的 wakeup 簇中存在明确的因果阻塞/计算链。

仅 `total_active_ms` 高、但时间分散、非关键路径且无预算压力的函数标记为 `low_temporal_pressure`，默认不进入异构机会排序。将 10ms 发生在 10 秒内与 16ms 窗口内的 10ms 显式区分，是避免伪热点的硬要求。

#### 10.4.3 Agent 与用户的职责

Agent 可以基于图统计提出簇候选，例如“渲染线程被 Binder 唤醒后连续三帧超预算的簇”，并展示边、窗口和指标；它不能自行把候选簇提升为生产结论。用户可选择簇、场景 profile 和时间窗口；选择记录进入 `analysis_run`，使同一 trace 的结论可复现。

#### 10.4.4 与异构机会匹配的门禁

```text
selected trace cluster
  + temporal-pressure hotspot candidate
  + source snapshot consistent
  + AST feature extraction succeeds
  + confirmed KB match
  => opportunity candidate
```

等待、锁竞争、Binder/Fence 驱动的簇可以是优化机会的证据，但不能被直接推荐为“迁移到 GPU/NPU”；匹配器必须将 `blocking_dominant`、`io_dominant`、`driver_wait_dominant` 作为负向约束，除非 KB 有明确的 pipeline/overlap 优化规则。

---

## 十一、clang-query 原理与 AST 作为优化数据的科学性

### 11.1 clang-query 源码级原理

为了理解 clang-query 的工作方式，我下载了 LLVM 22.1.8 源码中的 `clang-tools-extra/clang-query` 目录并阅读了核心文件：

- `tool/ClangQuery.cpp` — 命令行入口
- `QueryParser.cpp` — 命令解析
- `Query.cpp` — 真正的 matcher 执行与输出

其执行流程可以概括为三步：

```text
┌────────────────────────────────────────────────────────────────────┐
│  1. 前端解析：ClangTool 用传入的编译参数把源文件解析成 AST（ASTUnit）  │
├────────────────────────────────────────────────────────────────────┤
│  2. DSL 解析：QueryParser 把 "match functionDecl(...)" 这样的字符串   │
│     通过 Parser::parseMatcherExpression() 转成 DynTypedMatcher 对象  │
├────────────────────────────────────────────────────────────────────┤
│  3. AST 遍历：MatchFinder 对 AST 做深度遍历，节点满足 matcher 时把    │
│     绑定的节点（BoundNodes）回调给 CollectBoundNodes，最后按设置    │
│     输出 diag / print / detailed-ast                                │
└────────────────────────────────────────────────────────────────────┘
```

关键代码对应：

| 步骤 | 源码位置 | 关键类 / 函数 |
|------|---------|--------------|
| 构建 AST | `ClangQuery.cpp:104-121` | `CommonOptionsParser` → `ClangTool` → `buildASTs()` |
| 解析命令 | `QueryParser.cpp:263-279` | `Parser::parseMatcherExpression()` |
| 执行匹配 | `Query.cpp:109-203` | `MatchFinder::addDynamicMatcher()` → `Finder.matchAST()` |
| 收集绑定 | `Query.cpp:85-94` | `CollectBoundNodes::run(const MatchFinder::MatchResult &)` |

所以 clang-query 本质上是 **Clang AST 上的一个交互式 REPL**：它把 matcher DSL 编译成可执行的谓词树，再用 `MatchFinder` 对 AST 做自顶向下/自底向上的匹配。这与我们自己用 libclang 手写遍历没有本质区别，只是把遍历逻辑外包给了 LLVM 的 matcher 引擎。

### 11.2 为什么 AST 是科学的优化数据来源

把 CPU 代码映射到 ISP/AAE/NPU/GPU 等异构单元，核心问题是判断一段代码在算什么。函数名不可信，但 AST 保留了以下**算法本质特征**：

| AST 能提供的特征 | 与异构映射的关系 |
|-----------------|----------------|
| **循环嵌套结构** | 判断并行度（SIMD / GPU thread / NPU tensor） |
| **循环边界表达式** | 推断 tile size、workgroup 划分、零拷贝条件 |
| **数据类型** | NPU 偏好 int8/fp16，GPU 偏好 fp32，DSP 有定点指令 |
| **运算类型** | MAC → NPU/GPU compute；插值 → GPU shader / AAE；DCT → 编解码硬件 |
| **访存下标模式** | 连续/stride/仿射 → 决定 cache、DMA、shared memory 效率 |
| **控制流密度** | 分支多 → GPU warp divergence，不适合 shader |
| **调用图 / API** | 是否已调用 Skia/OpenGL/Neural Network Runtime 等已有加速层 |

这些特征与具体模块、命名风格、宏封装无关。例如 `JpegDecoder::DoSwDecode` 和某个新模块里的 `DecodeJpegSoftware` 可能函数名完全不同，但 AST 上都是“scanline 循环 + DCT + YCbCr→RGB + uint8 输出”，因此可以被同一个模式识别并建议走 JPEG 硬件解码。

从机器学习的角度看，这相当于把代码表示从**符号空间（函数名）**提升到了**语义特征空间（算法签名）**，满足：

1. **可迁移性**：同一算法模式跨模块、跨仓库仍然有效。
2. **可解释性**：每个匹配结果都能追溯到 AST 节点和源码位置，不是黑盒。
3. **可复现性**：同样的源码 + 同样的 matcher 必然得到同样的特征向量。
4. **可扩展性**：新增硬件能力只需新增模式，不需要重新训练模型。

### 11.3 静态 AST 分析的边界

AST 能提供“这段代码适合哪种硬件”的潜力，但它**不知道运行时的真实热度**。因此必须与 trace 结合：

- **AST 负责“能不能做”**：算法模式、数据类型、控制流是否满足硬件约束。
- **Trace 负责“值不值得做”**：该函数在真实场景中是否高频、是否耗时、输入尺寸是否符合硬件限制。

只在 AST 上识别机会会误报（比如初始化代码也有循环但只跑一次）；只看 trace 会漏报（比如一个轻量但被频繁调用的核函数）。两者结合才能给出高优先级的改造清单。

### 11.4 小结

- `clang-query` 的原理不神秘：它把 matcher DSL 编译成 AST 谓词树，再用 `MatchFinder` 遍历 AST。
- 当前函数 profile 基线使用 Tree-sitter；clang-query 的价值在于对少量高价值候选补充类型、宏和调用语义，而不是取代 source binding 或动态归因。
- 用 AST 做异构优化机会的“原料”是科学的，因为 AST 捕获的是算法语义而非表面函数名。
- AST 静态分析必须和运行时 trace 结合，才能既不错杀也不漏判。

---

## 十二、结论

这条路线可以做到**不靠大模型**，用确定性工具链持续沉淀和识别优化机会：

1. **大规模初筛**：Tree-sitter + 启发式规则；
2. **函数级特征提取**：对严格 pair 或精确热点 callee 建立 Tree-sitter profile，并记录 `ready` / `not_ready`；
3. **AST 语义增强**：按需使用 clang-query / libclang 做可复用的模式复核；
4. **fallback 对挖掘**：Tree-sitter + 启发式规则；
5. **机会识别**：`matching_engine.py` 算法模式库 + 签名相似度匹配。

这套方法的关键是把匹配粒度从"函数名"下沉到"算法签名"，从而跨模块复用已知异构优化模式。当前严格基线已沉淀 80 条 pair；下一步应补齐 runtime-selected 回退的具体实现绑定，并按 PoC → 小规模场景 → CI 扫描的节奏逐步收敛 schema、约束库和误报样本。

---

*关联文档：*
- `spec_index.md`
- `heterogeneous_optimization_opportunity_platform_spec.md`
- `strict_heterogeneous_cpu_fallback_overview.md`
- `tools/heterogeneous_kb/README.md`

*工具目录：*
- `docs_ffrt/tools/function_signature_extractor.py` — 第一层：大规模粗筛
- `docs_ffrt/tools/build_cpu_fallback_ast_profiles.py` — 第二层：严格 pair 的 Tree-sitter 函数 profile 基线
- `docs_ffrt/tools/match_hotspot_cpu_fallback_profiles.py` — 热点函数与 CPU fallback profile 的保守匹配
- `docs_ffrt/tools/precise_feature_extractor_cq.py` — 高价值候选的 clang-query 语义增强
- `tools/analysis/precise_feature_extractor.py` — 高价值候选的 libclang 语义增强
- `docs_ffrt/tools/fallback_pair_extractor.py` — 第三层：fallback 对挖掘
- `docs_ffrt/tools/ast_pattern_validator.py` — 第四层：AST 模式复核（优先 clang-query，可回退 libclang）
- `docs_ffrt/tools/ast_pattern_validator_libclang.py` — 第四层 libclang 手写遍历回退版本
- `tools/analysis/matching_engine.py` — 第五层：模式匹配与机会识别
- `docs_ffrt/tools/doxygen_bridge.py` — 补充层：Doxygen XML 桥接
- `data/knowledge-bases/patterns/` — 分层异构优化知识库（推荐）
- `docs_ffrt/tools/sample_kb.json` — 旧版单文件示例异构优化知识库
- `docs_ffrt/tools/sample_opportunities.jsonl` — 示例异构优化机会文件
