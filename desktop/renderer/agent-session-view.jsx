import React, {useCallback, useEffect, useMemo, useState} from 'react';
import {RefreshCw} from 'lucide-react';

const list = value => Array.isArray(value) ? value : [];
const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};

function elapsed(value) {
  if (value === null || value === undefined || value === '') return '—';
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds)) return '—';
  if (milliseconds < 1000) return Math.round(milliseconds) + ' ms';
  if (milliseconds < 60000) return (milliseconds / 1000).toFixed(milliseconds < 10000 ? 1 : 0) + ' s';
  return Math.floor(milliseconds / 60000) + 'm ' + Math.round((milliseconds % 60000) / 1000) + 's';
}

function platformOperationTimingText(operation) {
  if (!Number.isFinite(operation.duration_ms)) return operation.state || '未计时';
  const parts = [`${operation.state || 'unknown'} · ${durationScopeLabel(operation.duration_scope)} ${elapsed(operation.duration_ms)}`];
  if (Number.isFinite(operation.queue_wait_ms)) parts.push(`排队 ${elapsed(operation.queue_wait_ms)}`);
  if (Number.isFinite(operation.channel_acquire_duration_ms)) parts.push(`获取通道 ${elapsed(operation.channel_acquire_duration_ms)}`);
  if (Number.isFinite(operation.transport_duration_ms)) parts.push(`RPC 往返（含远端处理） ${elapsed(operation.transport_duration_ms)}`);
  if (operation.channel_reused === false && Number.isFinite(operation.channel_startup_duration_ms))
    parts.push(`通道冷启基线 ${elapsed(operation.channel_startup_duration_ms)}`);
  if (operation.channel_reused === true) parts.push('长驻通道复用');
  return parts.join(' · ');
}

function durationScopeLabel(value) {
  if (value === 'remote_execution') return '远端执行';
  if (value === 'broker_wall') return '平台墙钟';
  return '旧记录口径未标明';
}

function eventDepths(events) {
  const byId = new Map(events.map(item => [item.event_id, item]));
  const depths = new Map();
  for (const event of events) {
    let depth = 0;
    let parent = event.parent_id;
    const seen = new Set([event.event_id]);
    while (parent && byId.has(parent) && !seen.has(parent) && depth < 5) {
      seen.add(parent);
      depth += 1;
      parent = byId.get(parent)?.parent_id;
    }
    depths.set(event.event_id, depth);
  }
  return depths;
}

function SessionEvent({event, depth}) {
  const label = event.role === 'assistant' ? 'Assistant'
    : event.role === 'user' ? 'User'
      : event.role === 'system' ? 'System' : 'Event';
  return <article className={'code-agent-event role-' + event.role} style={{'--event-depth': Math.min(depth, 5)}}>
    <header><span>{label}{event.sidechain ? ' · Sidechain' : ''}</span><time>{event.timestamp ? new Date(event.timestamp).toLocaleTimeString('zh-CN', {hour12: false}) : '—'}</time></header>
    {list(event.blocks).map((block, index) => {
      if (block.type === 'text') return <p key={index} className="code-agent-text">{block.preview}{block.truncated ? '…' : ''}</p>;
      if (block.type === 'thinking') return <details key={index} className="code-agent-thinking"><summary>推理记录 · {block.character_count || 0} 字符</summary><p>只保留长度和时间信息；推理原文默认不读取、不展示。</p></details>;
      if (block.type === 'tool_use') return <div key={index} className={'code-agent-tool-call ' + (block.result?.is_error ? 'error' : '')}>
        <div className="code-agent-tool-title"><strong>{block.name}</strong><span title="Agent JSONL 中 tool_use 到 tool_result 的观察跨度；它可能包含排队、权限处理、适配和结果序列化。">{block.result ? `观察跨度 ${elapsed(block.observed_duration_ms ?? block.duration_ms)}` : '等待结果'}</span></div>
        {Object.keys(record(block.input)).length > 0 && <dl>{Object.entries(record(block.input)).map(([key, value]) => <React.Fragment key={key}><dt>{key}</dt><dd>{String(value)}</dd></React.Fragment>)}</dl>}
        {block.result && <details open={block.result.is_error === true}><summary>{block.result.is_error ? '工具失败' : '工具结果'}{block.result.truncated ? ' · 已截断' : ''}</summary><pre>{block.result.preview || '没有文本输出'}</pre></details>}
      </div>;
      if (block.type === 'tool_result') return <div key={index} className={'code-agent-orphan-result ' + (block.is_error ? 'error' : '')}>未找到对应调用的工具结果：{block.preview}</div>;
      return null;
    })}
  </article>;
}

export function CodeAgentSessionExplorer({invoke, ErrorNotice, LoadingPage}) {
  const [catalog, setCatalog] = useState(null);
  const [selectedSourceId, setSelectedSourceId] = useState('');
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const acceptCatalog = useCallback(value => {
    const sessions = list(value?.sessions);
    setCatalog(value);
    setSelectedSourceId(current => sessions.some(item => item.source_id === current) ? current : sessions[0]?.source_id || '');
  }, []);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      acceptCatalog(await invoke('listCodeAgentSessions', {limit: 300}));
      setError(null);
    } catch (nextError) { setError(nextError); }
    finally { setLoading(false); }
  }, [acceptCatalog, invoke]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!selectedSourceId) { setDetail(null); return; }
    let active = true;
    setLoading(true);
    void invoke('getCodeAgentSession', {sourceId: selectedSourceId}).then(value => {
      if (!active) return;
      setDetail(value);
      setError(null);
      setLoading(false);
    }, nextError => {
      if (!active) return;
      setError(nextError);
      setLoading(false);
    });
    return () => { active = false; };
  }, [invoke, selectedSourceId]);
  const chooseRoot = async () => {
    try {
      const value = await invoke('chooseCodeAgentSessionRoot');
      if (value) { acceptCatalog(value); setError(null); }
    } catch (nextError) { setError(nextError); }
  };
  const useAutomaticRoot = async () => {
    try {
      acceptCatalog(await invoke('configureCodeAgentSessionRoot', {rootPath: ''}));
      setError(null);
    } catch (nextError) { setError(nextError); }
  };
  const sessions = list(catalog?.sessions);
  const metrics = record(detail?.metrics);
  const depths = useMemo(() => eventDepths(list(detail?.timeline)), [detail]);
  const observedSkills = list(detail?.skills).filter(item => item.loaded);
  const calledPlatformTools = list(detail?.tools).filter(item => item.platform_tool);
  const calledOtherTools = list(detail?.tools).filter(item => !item.platform_tool);
  const platformTiming = record(detail?.platform_timing);
  const platformOperations = list(platformTiming.operations);
  return <section className="work-section code-agent-session-explorer">
    <div className="section-heading"><div><span className="eyebrow">本地 JSONL 证据</span><h2>Code Agent Session</h2></div><div className="code-agent-session-actions"><button onClick={() => void chooseRoot()}>选择目录</button><button onClick={() => void useAutomaticRoot()}>自动发现</button><button className="icon-button" title="刷新 Session" onClick={() => void refresh()}><RefreshCw size={15}/></button></div></div>
    <div className="code-agent-session-root"><span className={catalog?.root?.available ? 'online' : 'offline'}></span><code>{catalog?.root?.path || '正在发现 %USERPROFILE%\\.cac\\projects'}</code><small>{catalog?.root?.configured ? '自定义目录' : '自动发现'} · {sessions.length} 个会话</small></div>
    {error && <ErrorNotice error={error} source="code-agent-session-history"/>}
    <div className="code-agent-session-layout">
      <aside className="code-agent-session-list">{sessions.map(session => <button key={session.source_id} className={session.source_id === selectedSourceId ? 'selected' : ''} onClick={() => setSelectedSourceId(session.source_id)}><strong>{session.title}</strong><span>{session.project}</span><small>{new Date(session.updated_at).toLocaleString('zh-CN', {hour12: false})} · {(session.size_bytes / (session.size_bytes < 1024 * 1024 ? 1024 : 1024 * 1024)).toFixed(session.size_bytes < 1024 * 1024 ? 0 : 1)} {session.size_bytes < 1024 * 1024 ? 'KiB' : 'MiB'}</small></button>)}{!loading && sessions.length === 0 && <div className="migrated-empty">没有发现 JSONL Session。可选择 <code>.cac\projects</code> 或其中的项目目录。</div>}</aside>
      <div className="code-agent-session-detail">{loading && !detail ? <LoadingPage label="正在流式解析 Session…"/> : detail && <>
        <div className="code-agent-session-metrics"><span><small>消息事件</small><strong>{metrics.event_count || 0}</strong></span><span><small>工具调用</small><strong>{metrics.tool_call_count || 0}</strong></span><span><small>工具错误</small><strong>{metrics.tool_error_count || 0}</strong></span><span><small>会话跨度</small><strong>{elapsed(metrics.duration_ms)}</strong></span><span><small>推理记录</small><strong>{metrics.thinking_block_count || 0}</strong></span></div>
        <div className="code-agent-session-meta"><code>{detail.session?.session_id}</code><span>{detail.session?.cwd || '未记录 cwd'}</span><span>{detail.session?.version ? 'Agent ' + detail.session.version : '版本未知'}</span>{detail.platform_run && <span className="linked">已关联平台执行 · {detail.platform_run.match === 'session_id' ? 'Session ID' : '启发式：目录与时间'}</span>}</div>
        <div className="migrated-notice info">Session 工具耗时是 <code>tool_use → tool_result</code> 的观察跨度，可能包含 Agent 排队、权限处理、MCP/CLI 适配和结果序列化；它不等于读取文件或执行函数本体的耗时。平台侧 RPC 往返包含远端处理，各时间项口径不同、不可相加。关联到 Desktop 记录时，请用下面的平台侧计时判断瓶颈。</div>
        <div className="code-agent-asset-grid">
          <details open><summary>平台 Skill · 已加载 {observedSkills.length}/{list(detail.skills).length}</summary><div className="code-agent-asset-pills">{list(detail.skills).map(skill => <span key={skill.skill_id} className={skill.loaded ? 'used' : ''}>{skill.skill_id}</span>)}{list(detail.skills).length === 0 && <small>当前平台没有物化 Skill。</small>}</div></details>
          <details open><summary>平台工具 · 已调用 {calledPlatformTools.reduce((sum, item) => sum + item.call_count, 0)}/{list(detail.available_tools).length}</summary><div className="code-agent-tool-stats">{calledPlatformTools.map(tool => <span key={tool.name}><strong>{tool.name}</strong><small>{tool.call_count} 次 · 错误 {tool.error_count} · 累计观察跨度 {elapsed(tool.total_observed_duration_ms ?? tool.total_duration_ms)}</small></span>)}{calledPlatformTools.length === 0 && <small>本会话没有观察到平台工具调用。</small>}</div><details className="code-agent-available-assets"><summary>查看未调用工具</summary><div className="code-agent-asset-pills">{list(detail.available_tools).filter(tool => !tool.call_count).map(tool => <span key={tool.name}>{tool.name}</span>)}</div></details></details>
          <details><summary>Agent 内置及其他工具 · {calledOtherTools.length} 类</summary><div className="code-agent-tool-stats">{calledOtherTools.map(tool => <span key={tool.name}><strong>{tool.name}</strong><small>{tool.call_count} 次 · 错误 {tool.error_count} · 累计观察跨度 {elapsed(tool.total_observed_duration_ms ?? tool.total_duration_ms)}</small></span>)}</div></details>
          {platformOperations.length > 0 && <details open><summary>平台侧执行计时 · {platformTiming.timed_operation_count || 0}/{platformTiming.operation_count || 0}</summary><div className="code-agent-tool-stats">{platformOperations.map((operation, index) => <span key={operation.operation_id || index}><strong>{operation.label}</strong><small>{platformOperationTimingText(operation)}</small></span>)}</div></details>}
        </div>
        {metrics.timeline_truncated && <div className="migrated-notice warning">该会话共有 {metrics.event_count} 个事件；首版界面只展示前 {metrics.rendered_event_count} 个，统计覆盖完整解析范围。</div>}
        <div className="code-agent-session-timeline">{list(detail.timeline).map(event => <SessionEvent key={event.event_id} event={event} depth={depths.get(event.event_id) || 0}/>)}</div>
      </>}</div>
    </div>
  </section>;
}
