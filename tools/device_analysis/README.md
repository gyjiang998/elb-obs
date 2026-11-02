# 真机应用启动采集工具

`app_startup_trace.py` 在一台已连接的 OpenHarmony 设备上连续冷启动一组应用，同时采集
HiTrace 与 HiPerf，并生成 ArkUI self-time、逐应用启动窗口和 CPU 采样热点报告。

## 运行

```bash
python3 tools/device_analysis/app_startup_trace.py \
  --serial <HDC_DEVICE_SERIAL> \
  --app-count 20 \
  --dwell-seconds 1.5
```

HDC 依次从 `--hdc`、`HETERO_HDC_BIN` 和本地运行时查找。TraceStreamer 依次从
`--trace-streamer`、`HETERO_TRACE_STREAMER` 和本地 artifact 查找，并按宿主操作系统选择
macOS 或 Linux 二进制。

默认使用 `arkui-startup` trace profile，采集 `ace`、`ark`、`app`、`graphic`、
`window`、`ability`、`binder`、`rpc` 和 `ffrt`。如需调度器明细，可显式指定
`--trace-profile broad`，同时应根据实验长度增加 `--trace-buffer-kb`。

## 故障恢复

设备采集完成后，即使 TraceStreamer 或离线分析失败，也不需要重新启动应用：

```bash
python3 tools/device_analysis/app_startup_trace.py \
  --serial <HDC_DEVICE_SERIAL> \
  --resume-run .local-artifacts/device-app-launch-runs/app_startup_YYYYMMDD_HHMMSS
```

恢复模式会复用已存在的 `trace_streamer.db`；数据库不存在时才重新转换原始 trace。

## 输出

每次运行在 `.local-artifacts/device-app-launch-runs/<run-id>/` 下生成：

- `startup.ftrace`：设备文本 HiTrace。
- `perf.data`：系统级 HiPerf CPU cycle 样本。
- `perf_report.txt`、`perf_callstack.txt`：全局 flat 与调用栈报告。
- `perf_apps_report.txt`：仅包含成功启动应用 PID 的报告。
- `trace_streamer.db`：TraceStreamer SQLite 数据库。
- `summary.json`：结构化模块、ArkUI、逐应用和 HiPerf 结果。
- `report.md`：面向人工阅读的中文报告。
- `commands.json`：设备命令、返回码、耗时和有界输出，供失败诊断。

报告必须先检查 `hitrace.launch_window_retention.complete`。只有成功窗口全部保留时，
逐应用结论才视为完整。HiTrace self-time 是父 span 减去直接子 span 的估算；HiPerf
应用热度是经过报告阈值裁剪后的下界，两者都不能单独等同于关键路径或优化收益。

## 设备影响

工具只执行 `aa force-stop`、隐式 launcher 启动、trace/perf 采集和自身临时目录清理。
它不会清除应用数据，不会安装或卸载应用，也不会读取应用页面内容。

## 测试

```bash
python3 -m unittest backend.tests.test_app_startup_trace_tool
```

## 已有 Trace 的阶段化报告

`stage_trace_report.py` 根据真实 start/end marker，把 TraceStreamer 数据库转换为平台可导入的
`summary + stages + top_events` JSON。阶段边界按 marker 精确还原；事件按“父 span 减直接子
span”信号排序。该信号用于缩小源码调查范围，不等同于采样得到的 CPU self-time。

```bash
python3 tools/device_analysis/stage_trace_report.py \
  --database trace_streamer.db \
  --trace capture.htrace \
  --stage 'select|选择照片|PHOTO_SELECT_START|PHOTO_SELECT_END' \
  --stage 'save|保存照片|PHOTO_SAVE_START|PHOTO_SAVE_END' \
  --process-id 1234 \
  --top-events 30 \
  --output stage_analysis.json
```

`--process-id` 可以重复指定。对于已知的环境等待 span，可重复使用 `--exclude-pattern` 从热点
排序中排除；原始 trace 与阶段边界不会被修改。
