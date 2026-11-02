# 离线校准模型包

桌面端随分析 Worker 分发已经通过质量门禁的真机校准模型。每个包是
`parallel_calibration_portable_package/v1` JSON，包含同一硬件身份下的：

- 硬件画像：核组、物理核代表、频率域和 OPP；
- 冻结校准方案：负载、频点、训练/留出条件和放置约束；
- 真机拟合：原始测量、拒绝样本、留出误差和模型预览；
- 已发布版本：模型内容哈希、测量哈希及适用域。

包本身使用内容寻址 ID 和 SHA-256 校验。桌面常驻分析 Worker 初始化时扫描
`data/parallel-modeling/calibration-packages/*.json`，验证引用链后幂等写入当前
工作空间的硬件画像、方案、拟合与版本目录。已有同 ID 本地记录只有内容一致
时才会复用；冲突或损坏会拒绝加载。这样曲线页既能读取已发布拟合线，也能
读取各 OPP 的实测中位数，寻优页可以按硬件画像选择该模型。

发布一个已经保存的校准版本：

```bash
python3 tools/export_portable_calibration.py \
  --state-root /path/to/calibration-state \
  --version-id cm_0123456789abcdef01234567 \
  --output data/parallel-modeling/calibration-packages/device-frequency-curves.json
```

当新包修正了旧包的硬件拓扑、测量证据或模型时，发布者应在新包中声明
`supersedes`，而不是只替换仓库中的 JSON 文件。`--supersedes-json` 接受一个目标
数组，每项显式记录旧包、旧硬件画像、旧校准版本和退役原因：

```json
[
  {
    "package_id": "pcp_0123456789abcdef01234567",
    "hardware_profile_id": "hp_0123456789abcdef01234567",
    "calibration_version_id": "cm_0123456789abcdef01234567",
    "reason": "旧包的物理核拓扑证据无效，由重新采集并通过门禁的模型替代。"
  }
]
```

升级安装分两阶段完成：先验证并物化目录中的全部包，再应用退役关系，因此结果
不依赖文件名排序。平台只会自动退役由声明旧包安装的版本，避免覆盖同 ID 的本地
校准证据；旧版本保留为审计记录但不再进入模型目录、默认选择和显式寻优，旧硬件
画像会归档且不能恢复。每次迁移在 `parallel-modeling/calibration/supersessions/`
保存幂等回执，记录新旧身份、原因和版本/画像处理结果。
回执是持久的安装门禁：即使后续启动目录中暂时只出现旧包，已记录的退役
目标也不会被重新物化为活动模型；目录扫描会忽略该旧包，直接安装接口则明确
返回 `parallel_calibration_package_superseded`。
若只找到同 ID 硬件画像却找不到由声明旧包安装的版本，平台无法证明该画像
的包来源，因此保留其活动状态并在回执中记为 `preserved_unverified_origin`。

校准包表示代理微基准在对应设备、核放置和 OPP 域内的实测模型。包内仍保留
`application_validation: not_application_validated`，不会把微基准校准表述为具体
应用改造收益验证。

## 当前随包模型审计

`data/parallel-modeling/calibration-packages/vyg-al00-frequency-curves-20261030.json`
来自一台 Kirin 9020 三核组设备的受管 v6 代理微基准。包身份为
`pcp_adfd74e00cb7eceed4b0f2d2`，硬件画像为 `hp_9ee581aae07fb10ae74d614c`，
已发布校准版本为 `cm_44b32853299ae053658c920f`，包内容 SHA-256 为
`adfd74e00cb7eceed4b0f2d280e981e72cb7d8bc875f4fe22a7aaff3572706aa`。

拟合接纳 153 个通过门禁的 repeat，
覆盖 3 个核组 × compute/memory/mixed 三类任务的 9 条曲线；每条曲线使用
5 个训练 OPP × 3 次重复，并以 1 个独立 OPP × 2 次重复做留出验证。整体留出
median APE 为 0.185%，P90 APE 为 2.476%，最大 repeat CV 为 5.365%。
另有 4 个被拒绝的尝试留在完整拟合审计中：2 个因目标逻辑 CPU 的实际频率
窗口未完整落入目标 OPP ±3%，2 个因同一物理核的同胞逻辑 CPU 忙占比超过
5%。有界补测只补齐原冻结 repeat，被拒绝尝试不会被删除或冒充有效样本。

该批次使用 per-CPU `cpuinfo_cur_freq`、自适应驻留和同胞逻辑 CPU 静默度联合门禁。
新包的 `supersedes` 精确指向旧包 `pcp_067a2405fa4d0b6c713f1409`、旧画像
`hp_7d824d3a3c76f2a58447fed7` 和旧版本 `cm_13da2fc02ef45d07c7dd4889`；旧版本不再进入
模型目录、默认选择或显式寻优，旧画像不能恢复。该模型仍标记
`independent_device_verification: false` 和
`application_validation: not_application_validated`。它可用于验证模型安装、曲线展示
和条件寻优链路，不代表具体应用 BuildItem 的并行收益已经在另一台设备上复验。
