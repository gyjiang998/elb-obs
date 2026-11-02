# 异构优化点发现方法论

> 目标：建立一套可落地的流程，从 OpenHarmony 海量代码中持续发现“可并行、可异构”的优化机会。
> 适用对象：graphic_graphic_2d、ai_neural_network_runtime、multimedia_image_framework、multimedia_player_framework、camera 等子系统。

---

## 一、总体工作流

核心思路是 **trace 驱动 + 特征匹配 + 异构单元映射 + 小步验证**。

```text
1. 定目标 → 2. 采 trace → 3. 可视化/自动化分析 → 4. 模式识别 → 5. 异构映射 → 6. 改动验证
```

---

## 二、定目标：先收窄范围，不要全量扫描

全量代码看不过来，先用业务场景圈定热点。

| 维度 | 建议 |
|------|------|
| **场景** | 启动、切应用、列表滑动、相机拍照、视频播放、AI 功能触发 |
| **指标** | 帧率、启动时延、卡顿帧、CPU 占用、功耗、内存 |
| **模块** | ArkUI / RenderService / Camera / Multimedia / AI / WindowManager |
| **已知痛点** | 从 issue、性能测试报告、用户反馈里找 |

---

## 三、采 trace：不是只采一次，要“对比采”

### 3.1 基础工具

- **`hitrace`**：OpenHarmony 自带，抓系统级 trace（用户态 + 内核态）。
- **`smartperf`** / `hiperf`：抓 CPU 采样、火焰图、内存。
- **`systrace` 风格工具**：看 RenderService、Vsync、GPU、HWC 时间线。
- **FFRT 自带 trace**：`RS_OPTIONAL_TRACE_NAME`、`HPAE_LOGD` 等埋点都会进 trace。

### 3.2 关键采集策略

| 采集方式 | 目的 |
|----------|------|
| **正常路径 trace** | 建立 baseline，看时间线分布 |
| **压力场景 trace** | 高负载下哪个模块先崩 / 先卡 |
| **AB 对比 trace** | 开关某个特性前后对比（如关闭 HPAE、关闭 HWC） |
| **多设备对比** | 低端机 vs 旗舰机，看哪些模块在低端机上恶化 |

### 3.3 具体命令示例

```bash
# 采集 hitrace（graphic、freq、irq、sched 等 tag）
hitrace -t 10 -o /data/local/tmp/trace.ftrace graphic freq irq sched mm

# 或者用 oh-trace-analysis-clean 工具链
cd oh-trace-analysis-clean
python scripts/collect_trace.py --scenario app_launch --device <ip> --output traces/
```

`traces/` 目录下已有的 `hiprofiler_data_20260604_105223_1.db` 就是可直接分析的素材。

---

## 四、分析 trace：三层分析法

### 4.1 第一层：可视化时间线（找“长条”）

用 trace 查看工具（Perfetto、smartperf IDE、viz 工具）看时间线：

```text
看哪些线程/函数占据时间线长条：
- 主线程长时间执行 → 可能阻塞 UI，考虑异步化 / FFRT
- RenderService 线程长时间 draw → 可能 GPU 负载高 / 合成复杂
- 某个 so 连续高 CPU → 可能是计算密集，考虑 NPU/GPU/专用 IP
- Vsync 周期内 GPU 没干完 → 瓶颈在 GPU
- Vsync 周期内 CPU 没干完 → 瓶颈在 CPU
```

### 4.2 第二层：统计聚合（找“高频”和“高耗时”）

从 trace db 提取：

| 指标 | 意义 |
|------|------|
| **函数 self time 总和 Top N** | 真正的 CPU 热点 |
| **函数调用次数 Top N** | 高频小函数，可能适合缓存/向量化 |
| **线程 wall time / CPU time 比** | 比值低说明阻塞/等待多，适合异步化 |
| **Vsync 掉帧位置** | 定位具体帧的瓶颈函数 |
| **锁竞争等待时间** | 适合减少临界区或 lock-free |

### 4.3 第三层：调用链/责任链分析（找“根因”）

核心问题：这个耗时最终是由哪个模块、哪个调用链引入的？

方法：
- 从终点（如第一帧显示）反向追调用链；
- 标注每个阶段的责任人（ArkUI / RS / HWC / Codec / NPU）；
- 找出“本可以不阻塞”的同步等待点。

---

## 五、从 trace 特征映射到优化手段

发现热点后，用代码特征判断适合哪种异构/并行优化。可参考 `kirin-heterogeneous-insight.md` 中的决策树。

### 5.1 识别信号 → 优化方向

| trace/代码特征 | 含义 | 优化方向 |
|----------------|------|----------|
| **主线程大段 CPU 计算，无 IO** | 计算密集 | GPU / NPU / 专用 IP 卸载 |
| **多层嵌套循环，浮点/卷积运算** | 矩阵/神经网络计算 | NPU（达芬奇） |
| **逐像素图像处理，数据并行度高** | 滤镜、缩放、颜色转换 | GPU Compute Shader / VPE / ISP |
| **频繁 malloc/free 或 memcpy** | 内存/拷贝开销 | 内存池、DMA、零拷贝 |
| **线程阻塞等待另一线程结果** | 同步开销 | 异步化 / FFRT / pipeline 并行 |
| **多段独立任务串行执行** | 可并行 | `ffrt::submit_h` + `ffrt::wait` |
| **周期性任务（Vsync、定时器）** | 实时调度 | 接入 FFRT QoS |
| **重复初始化/重复加载模型** | 资源复用缺失 | 对象池、缓存、引用计数 |
| **视频/音频编解码占 CPU** | 编解码热点 | 硬件 Codec / DSP |
| **多图层 GPU 合成耗时** | 合成瓶颈 | HWC 直通、减少 overdraw |

### 5.2 具体例子

假设 trace 里发现 `RSImageDetailEnhancerThread::ScaleByAAE` 耗时高：

1. **看代码**：`rs_image_detail_enhancer_thread.cpp:145`，调用 `Drawing::Image::ScaleImage(..., OPTION_AAE_ARSR)`；
2. **判断特征**：图像缩放，数据并行，逐像素处理；
3. **映射异构**：AAE/ARSR 已经是硬件/算法加速路径，但如果仍占主线程，可进一步检查：
   - AAE 是否命中，是否 fallback 到 `ScaleByHDSampler`；
   - 输入格式是否不支持，走了软件路径；
4. **验证**：加 trace 点统计 AAE 命中率和 fallback 率。

---

## 六、自动化发现：静态扫描 + 动态 trace 结合

### 6.1 静态代码扫描（快速定位候选）

基于 `kirin-heterogeneous-insight.md` 中的模式，扫 `source_code/`：

```bash
# 扫潜在异构优化点
grep -R -n -E "(for\s*\(|while\s*\()" source_code/graphic_graphic_2d \
  | xargs -I{} python analyze_loop.py --check_nesting --check_float_ops

# 扫拷贝热点
grep -R -n "memcpy\|malloc\|new " source_code/graphic_graphic_2d \
  | python analyze_copy_hotspot.py
```

可识别的模式：

| 模式 | 标记 |
|------|------|
| 四层以上嵌套循环 + 浮点运算 | `CANDIDATE_NPU` |
| 逐像素 `for(y) for(x)` | `CANDIDATE_GPU_SHADER` |
| 频繁 `memcpy` 在渲染路径 | `CANDIDATE_ZERO_COPY` |
| 同步 `WaitForCompletion` / `join()` | `CANDIDATE_FFRT_ASYNC` |
| 每次调用都 `LoadModel` / `InitDevice` | `CANDIDATE_POOLING` |
| `#ifdef CPU_ONLY` 分支 | `CANDIDATE_HETEROGENEOUS` |

### 6.2 动态 trace 扫描（确认真实热点）

```python
# 伪代码：从 hiprofiler db 提取 Top 耗时函数
import sqlite3
conn = sqlite3.connect('hiprofiler_data_20260604_105223_1.db')
cursor = conn.cursor()
cursor.execute("""
  SELECT name, SUM(dur)/1e6 AS total_ms, COUNT(*) AS cnt
  FROM slice
  WHERE dur > 0
  GROUP BY name
  ORDER BY total_ms DESC
  LIMIT 50
""")
for row in cursor.fetchall():
    print(row)
```

然后与静态扫描结果交叉：
- 静态标了 `CANDIDATE_NPU` 且在 trace Top 50 里 → 高优先级；
- 只在静态出现但 trace 没热点 → 低优先级。

---

## 七、用现有工具链加速

工作目录中已有的工具可以直接串成流水线：

| 工具/目录 | 用途 |
|-----------|------|
| `docs_ffrt/backbone_extractor.py` + `backbone_toolchain.sh` | 从代码/文档提取 backbone（知识骨架） |
| `docs_ffrt/backbone_visualizer.py` | 可视化依赖/热点 |
| `oh-trace-analysis-clean/oh_trace_analysis/` | trace 分析核心库 |
| `oh-trace-analysis-clean/scripts/` | 采集/处理脚本 |
| `traces/hiprofiler_data_*.db` | 真实 trace 数据源 |
| `docs_ffrt/*.md` | 已有的异构优化知识库 |

建议流水线：

```text
代码扫描（backbone_extractor） → 生成候选函数列表
        ↓
Trace 解析（oh_trace_analysis） → 生成真实热点列表
        ↓
交叉匹配 → 生成高优先级优化任务
        ↓
人工复核代码 + 设计异构方案
        ↓
小改动 + AB trace 验证
```

---

## 八、可落地的试点计划

### Phase 1：建基线（1-2 天）
1. 选 1 个业务场景（如 `app_launch` 或 `camera_preview`）；
2. 用 `hitrace` 或现有脚本采集 3-5 组 trace；
3. 用 `oh_trace_analysis` 出 Top 50 耗时函数和调用链。

### Phase 2：扫候选（2-3 天）
1. 对 Top 50 函数做人工分类（计算型 / IO 型 / 同步型 / 渲染型）；
2. 用 `kirin-heterogeneous-insight.md` 的决策树映射到 GPU/NPU/HPAE/FFRT/HWC；
3. 输出一份《XX 场景异构优化机会清单》。

### Phase 3：验证一个点（1 周）
1. 挑 1 个预期收益最大的点；
2. 加详细 trace 埋点，确认根因；
3. 做最小改动（如把一个同步初始化改成 `ffrt::submit_h`）；
4. AB trace 对比，量化收益。

### Phase 4：泛化（持续）
1. 把 Phase 1-3 的方法脚本化；
2. 对更多场景跑流水线；
3. 不断更新 `heterogeneous_optimization_knowledge_base.md` 等知识库。

---

## 九、关键原则

1. **不要先优化后找证据**：一定是 trace 先发现热点，再回头翻代码。
2. **异构不是万能**：同步开销、锁竞争、内存拷贝往往比“能不能用 NPU”更重要。
3. **必须做 fallback**：所有硬件加速都要有失败回退路径。
4. **小步验证**：每个异构改动都要有 AB trace，防止负优化。
5. **关注数据局部性**：NPU/GPU 最怕 CPU↔加速器之间反复拷数据，零拷贝往往比算子优化收益更大。

---

*记录时间：2026-07-07*  
*关联文档：`kirin-heterogeneous-insight.md`、`heterogeneous_optimization_knowledge_base.md`*
