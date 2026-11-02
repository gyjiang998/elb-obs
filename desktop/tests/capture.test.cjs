'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {CaptureManager} = require('../runtime/capture.cjs');

const fakeEntry = String.raw`
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const argv=process.argv.slice(2),command=argv.shift(),args={};for(let i=0;i<argv.length;i+=2)args[argv[i]]=argv[i+1];
const write=(p,v)=>{fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p,JSON.stringify(v))};
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
const catalog={templates:[{template_id:'fixture',name:'测试场景',steps:[{step_id:'step_01',title:'一次操作',operation:'wait'}]}],default_capture:{selected_step_ids:['step_01'],collectors:['trace'],pmu:{period_us:10}}};
if(command==='catalog'){emit({type:'result',...catalog});}
else if(command==='save-plan'){
 const draft=JSON.parse(fs.readFileSync(args['--input']));
 if(draft.invalid){emit({type:'error',message:'Invalid capture plan'});process.exitCode=2;}
 else {const plan_id='artifact_'+crypto.randomBytes(5).toString('hex'),plan={project_id:'desktop-local',...draft,artifact_id:plan_id,plan_id};
 const directory=path.join(args['--output-root'],plan_id);write(path.join(directory,'plan.json'),plan);write(path.join(directory,'package.json'),{plan_id});emit({type:'result',plan,plan_id,package_directory:directory});}
}
else if(command==='inspect-trace'){
 try { const p=args['--path'];const manifest=JSON.parse(fs.readFileSync(path.join(p,'manifest.json')));for(const file of manifest.files){if(file.relative_path.includes('..')||file.relative_path.includes('\\'))throw Error('unsafe path');const fp=path.join(p,file.relative_path);if(fs.lstatSync(fp).isSymbolicLink())throw Error('unsafe link');const bytes=fs.readFileSync(fp);if(bytes.length!==file.size_bytes||crypto.createHash('sha256').update(bytes).digest('hex')!==file.sha256)throw Error('hash mismatch');}emit({type:'result',manifest,run_directory:p}); }
 catch(error){emit({type:'error',message:error.message});process.exitCode=2;}
}
else if(command==='run'){
 const plan=JSON.parse(fs.readFileSync(path.join(args['--package'],'plan.json'))),mode=plan.testMode||'success',run_id='capture_'+crypto.randomBytes(6).toString('hex'),directory=path.join(args['--output-root'],'scenario-capture','runs',run_id);
 fs.mkdirSync(directory,{recursive:true});
 emit({type:'progress',stage:'preparing',message:'准备手机与采集器',run_id,run_directory:directory,device_serial:args['--device-serial']});
 if(mode==='no-result'){process.stderr.write('unexpected runtime exit\n');process.exitCode=1;}
 else {
 let timer; const finish=status=>{clearInterval(timer);const content=Buffer.from('采集现场 '+args['--device-serial']);fs.writeFileSync(path.join(directory,'trace.txt'),content);if(status==='cancelled')fs.writeFileSync(path.join(directory,'cleanup.txt'),'collectors stopped');
 const error=status==='failed'?{step_title:'启动图库',reason:'目标页面不匹配',suggestion:'返回图库主页'}:null;
 const manifest={schema_version:'scenario_capture_manifest/v1',run_id,plan_id:plan.plan_id,title:plan.title,status,device_serial:args['--device-serial'],steps:[{step_id:'step_01'}],quality:{errors:error?[error]:[]},files:[{relative_path:'trace.txt',name:'trace.txt',kind:'trace',step_id:'step_01',size_bytes:content.length,sha256:crypto.createHash('sha256').update(content).digest('hex')}]};
 write(path.join(directory,'manifest.json'),manifest);write(path.join(directory,'run.json'),{manifest,error});emit({type:'progress',stage:'archive',status:'succeeded',message:'正在归档'});emit({type:'result',run_id,status,run_directory:directory,manifest_path:path.join(directory,'manifest.json'),run_path:path.join(directory,'run.json')});process.exitCode=['succeeded','partial'].includes(status)?0:1;};
 if(mode==='wait')timer=setInterval(()=>{if(fs.existsSync(args['--cancel-file']))finish('cancelled')},10);
 else if(mode==='hang')timer=setInterval(()=>{},100);
 else setTimeout(()=>finish(mode==='failed'?'failed':mode==='partial'?'partial':'succeeded'),30);
 }
}
else {emit({type:'error',message:'unknown command'});process.exitCode=2;}
`;

function fixture(t, options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-capture-'));
  const entryPath = path.join(directory, 'entry.cjs'); fs.writeFileSync(entryPath, fakeEntry);
  const config = {userDataDir: directory, runtimeRoot: directory, pythonCommand: [process.execPath], entryPath, disposeGraceMs: 1000, ...options};
  const manager = new CaptureManager(config);
  t.after(async () => { await manager.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  manager.updateDevices({devices: [{serial: 'PHONE-A'}, {serial: 'PHONE-B'}]});
  const events = []; manager.on('event', event => events.push(event));
  return {directory, config, manager, events};
}
async function until(predicate, timeout = 8000) { const started = Date.now(); while (!predicate()) { if (Date.now() - started > timeout) throw new Error('condition timed out'); await new Promise(resolve => setTimeout(resolve, 15)); } }
async function planAndDevice(manager, testMode = 'success') {
  const plan = await manager.savePlan({plan: {title: '图库测试', testMode}}); manager.selectDevice({serial: 'PHONE-A'}); return plan;
}

test('catalog and validated plans persist; device rediscovery never changes user selection', async t => {
  const f = fixture(t); assert.equal((await f.manager.catalog()).default_capture.pmu.period_us, 10);
  const saved = await planAndDevice(f.manager); assert.ok(saved.planId); assert.equal(saved.plan.title, '图库测试');
  assert.throws(() => f.manager.selectDevice({serial: 'NOT-DISCOVERED'}), /发现列表/);
  f.manager.updateDevices({devices: [{serial: 'PHONE-B'}]}); assert.equal(f.manager.getState().selectedDeviceSerial, 'PHONE-A');
  await f.manager.dispose();
  const restored = new CaptureManager(f.config); t.after(() => restored.dispose());
  assert.equal(restored.getState().selectedDeviceSerial, 'PHONE-A'); assert.equal(restored.getState().plans[0].planId, saved.planId);
  assert.ok(f.events.some(event => event.kind === 'state' && event.state.plans.length));
});

test('Agent updates a saved plan by revisioned patch and emits an exact completion event', async t => {
  const f = fixture(t);
  const saved = await f.manager.savePlan({plan: {title: '差量计划', profile: {bundle: 'com.example', ability: 'MainAbility', targets: {entry: {id: 'old'}}}, steps: [{step_id: 'step_01', title: '打开入口', operation: 'unresolved'}], capture: {selected_step_ids: ['step_01'], collectors: ['trace'], pmu: {period_us: 10}}}});
  const updated = await f.manager.updatePlan({
    planId: saved.planId, expectedUpdatedAt: saved.updatedAt, requestId: 'capture_plan_patch_1',
    targetUpdates: {entry: {id: 'new'}}, stepUpdates: [{step_id: 'step_01', operation: 'click', target: 'entry'}], summary: '补全入口点击。',
  });
  assert.equal(updated.planId, saved.planId);
  assert.equal(updated.changed, true);
  const stored = f.manager.getState().plans.find(value => value.planId === saved.planId);
  assert.equal(stored.plan.steps[0].operation, 'click');
  assert.deepEqual(stored.plan.profile.targets.entry, {id: 'new'});
  assert.deepEqual(stored.plan.capture, saved.plan.capture, 'patch must preserve capture and PMU configuration');
  assert.ok(f.events.some(event => event.kind === 'agent-plan-completed' && event.requestId === 'capture_plan_patch_1' && event.planId === saved.planId));
  await assert.rejects(f.manager.updatePlan({planId: saved.planId, expectedUpdatedAt: saved.updatedAt, requestId: 'capture_plan_stale', confirmUnchanged: true}), /迟到结果/);

  const confirmed = await f.manager.updatePlan({planId: updated.planId, expectedUpdatedAt: updated.updatedAt, requestId: 'capture_plan_unchanged', confirmUnchanged: true, summary: '现有计划已满足要求。'});
  assert.equal(confirmed.changed, false);
  assert.equal(confirmed.updatedAt, updated.updatedAt);
  assert.ok(f.events.some(event => event.kind === 'agent-plan-completed' && event.requestId === 'capture_plan_unchanged' && event.changed === false));
});

test('concurrent patches on one plan revision use CAS so exactly one update succeeds', async t => {
  const f = fixture(t);
  const saved = await f.manager.savePlan({plan: {title: '并发差量计划', steps: [{step_id: 'step_01', title: '旧标题', operation: 'wait'}]}});
  const revision = await f.manager.savePlan({plan: {...saved.plan, planId: saved.planId, title: '并发差量计划'}});
  assert.notEqual(revision.plan.artifact_id, revision.planId, 'fixture creates an artifact alias for the same logical plan');
  const outcomes = await Promise.allSettled([
    f.manager.updatePlan({planId: revision.planId, expectedUpdatedAt: revision.updatedAt, requestId: 'capture_plan_cas_a', stepUpdates: [{step_id: 'step_01', title: '并发版本 A'}]}),
    f.manager.updatePlan({planId: revision.plan.artifact_id, expectedUpdatedAt: revision.updatedAt, requestId: 'capture_plan_cas_b', stepUpdates: [{step_id: 'step_01', title: '并发版本 B'}]}),
  ]);
  assert.equal(outcomes.filter(value => value.status === 'fulfilled').length, 1);
  assert.equal(outcomes.filter(value => value.status === 'rejected').length, 1);
  assert.match(outcomes.find(value => value.status === 'rejected').reason.message, /迟到结果/);
  const winner = outcomes.find(value => value.status === 'fulfilled').value;
  const stored = f.manager.getState().plans.find(value => value.planId === saved.planId);
  assert.equal(stored.updatedAt, winner.updatedAt);
  assert.notEqual(stored.updatedAt, revision.updatedAt);
  assert.ok(['并发版本 A', '并发版本 B'].includes(stored.plan.steps[0].title));
  assert.equal(f.events.filter(event => event.kind === 'agent-plan-completed' && ['capture_plan_cas_a', 'capture_plan_cas_b'].includes(event.requestId)).length, 1);
});

test('an in-flight Agent patch cannot overwrite a newer direct user save', async t => {
  const f = fixture(t);
  const saved = await f.manager.savePlan({plan: {title: '用户与 Agent 竞争', steps: [{step_id: 'step_01', title: '基线', operation: 'wait'}]}});
  const originalCommand = f.manager.command.bind(f.manager);
  let agentSaveStarted = false;
  let releaseAgentSave;
  const agentSaveRelease = new Promise(resolve => { releaseAgentSave = resolve; });
  f.manager.command = async (args, options) => {
    if (args[0] === 'save-plan') {
      const input = JSON.parse(fs.readFileSync(args[args.indexOf('--input') + 1], 'utf8'));
      if (input.steps?.[0]?.title === 'Agent 慢补丁') {
        agentSaveStarted = true;
        await agentSaveRelease;
      }
    }
    return originalCommand(args, options);
  };
  const agentUpdate = f.manager.updatePlan({planId: saved.planId, expectedUpdatedAt: saved.updatedAt,
    requestId: 'capture_plan_user_race', stepUpdates: [{step_id: 'step_01', title: 'Agent 慢补丁'}]});
  const rejectedAgentUpdate = assert.rejects(agentUpdate, /迟到结果/);
  await until(() => agentSaveStarted);
  let userSaved;
  try {
    userSaved = await f.manager.savePlan({plan: {...saved.plan, planId: saved.planId,
      steps: [{...saved.plan.steps[0], title: '用户新版本'}]}});
  } finally {
    releaseAgentSave();
  }
  await rejectedAgentUpdate;
  const stored = f.manager.getState().plans.find(value => value.planId === saved.planId);
  assert.equal(stored.updatedAt, userSaved.updatedAt);
  assert.equal(stored.plan.steps[0].title, '用户新版本');
});

test('requestId retries are exactly-once in flight and after restart, while changed parameters are rejected', async t => {
  const f = fixture(t);
  const saved = await f.manager.savePlan({plan: {title: '幂等差量计划', steps: [{step_id: 'step_01', title: '旧标题', operation: 'wait'}]}});
  const request = {
    planId: saved.planId,
    expectedUpdatedAt: saved.updatedAt,
    requestId: 'capture_plan_exactly_once',
    stepUpdates: [{step_id: 'step_01', title: '一次更新'}],
    summary: '仅用于幂等测试的私密摘要，不应持久化',
  };
  const [first, concurrentRetry] = await Promise.all([f.manager.updatePlan(request), f.manager.updatePlan(request)]);
  assert.deepEqual(concurrentRetry, first);
  assert.deepEqual(await f.manager.updatePlan(request), first);
  assert.equal(f.events.filter(event => event.kind === 'agent-plan-completed' && event.requestId === request.requestId).length, 1, 'replay must not repeat completion side effects');
  await assert.rejects(f.manager.updatePlan({...request, summary: '不同参数'}), /用于不同参数/);

  const persisted = JSON.parse(fs.readFileSync(path.join(f.directory, 'capture', 'state.json'), 'utf8'));
  const terminal = persisted.planUpdateRequests.find(value => value.requestId === request.requestId);
  assert.deepEqual(Object.keys(terminal).sort(), ['changed', 'completedAt', 'fingerprint', 'planId', 'readiness', 'requestId', 'status', 'updatedAt'].sort());
  assert.equal(JSON.stringify(terminal).includes(request.summary), false, 'terminal journal stores a hash, not request content or summary');

  await f.manager.dispose();
  const restored = new CaptureManager(f.config);
  const replay = await restored.updatePlan(request);
  assert.deepEqual(replay, first);
  await assert.rejects(restored.updatePlan({...request, stepUpdates: [{step_id: 'step_01', title: '另一次更新'}]}), /用于不同参数/);
  await restored.dispose();
});

test('confirmUnchanged consumes requestId and the persistent terminal journal is bounded', async t => {
  const f = fixture(t);
  const saved = await f.manager.savePlan({plan: {title: '无需修改计划', readiness: {ready: true}}});
  const confirmed = await f.manager.updatePlan({planId: saved.planId, expectedUpdatedAt: saved.updatedAt, requestId: 'capture_plan_confirm_once', confirmUnchanged: true, summary: '无需修改'});
  assert.equal(confirmed.changed, false);
  assert.deepEqual(await f.manager.updatePlan({planId: saved.planId, expectedUpdatedAt: saved.updatedAt, requestId: 'capture_plan_confirm_once', confirmUnchanged: true, summary: '无需修改'}), confirmed);
  await assert.rejects(f.manager.updatePlan({planId: saved.planId, expectedUpdatedAt: saved.updatedAt, requestId: 'capture_plan_confirm_once', confirmUnchanged: true, summary: '改变摘要'}), /用于不同参数/);

  for (let index = 0; index < 260; index += 1) {
    await f.manager.updatePlan({planId: saved.planId, expectedUpdatedAt: saved.updatedAt, requestId: `capture_plan_confirm_${String(index).padStart(3, '0')}`, confirmUnchanged: true});
  }
  const persisted = JSON.parse(fs.readFileSync(path.join(f.directory, 'capture', 'state.json'), 'utf8'));
  assert.equal(persisted.planUpdateRequests.length, 256);
  assert.equal(persisted.planUpdateRequests[0].requestId, 'capture_plan_confirm_259');
  assert.equal(persisted.planUpdateRequests.some(value => value.requestId === 'capture_plan_confirm_000'), false);
});

test('delta runtime rejects empty patches and confirmUnchanged mixed with changes', async t => {
  const f = fixture(t);
  const saved = await f.manager.savePlan({plan: {title: '补丁语义', steps: [{step_id: 'step_01', title: '等待', operation: 'wait'}]}});
  const base = {planId: saved.planId, expectedUpdatedAt: saved.updatedAt};
  for (const [suffix, patch] of [
    ['app', {app: {}}], ['targets', {targetUpdates: {}}], ['steps', {stepUpdates: []}],
    ['step-item', {stepUpdates: [{step_id: 'step_01'}]}],
    ['mixed', {confirmUnchanged: true, title: '不应更新'}],
  ]) await assert.rejects(f.manager.updatePlan({...base, requestId: `capture_plan_empty_${suffix}`, ...patch}), /非空|至少需要|不能与/);
  assert.equal(f.manager.getState().plans.find(value => value.planId === saved.planId).updatedAt, saved.updatedAt);
});

test('one active process freezes exact selected phone and does not end on terminal-looking progress', async t => {
  const f = fixture(t); const plan = await planAndDevice(f.manager, 'wait');
  assert.throws(() => f.manager.startCapture({planId: plan.planId, deviceSerial: 'PHONE-B'}), /不一致/);
  const run = f.manager.startCapture({planId: plan.planId, hdcPath: '/explicit/hdc'});
  assert.throws(() => f.manager.startCapture({planId: plan.planId}), /已有采集/);
  assert.throws(() => f.manager.selectDevice({serial: 'PHONE-B'}), /冻结/);
  await until(() => f.manager.getState().activeRun?.progress?.device_serial === 'PHONE-A');
  f.manager.updateDevices({devices: [{serial: 'PHONE-B'}]});
  assert.equal(f.manager.getState().activeRun.deviceSerial, 'PHONE-A');
  const requested = f.manager.cancelCapture({runId: run.runId}); assert.equal(requested.status, 'cancelling');
  await until(() => !f.manager.getState().activeRun);
  const finished = f.manager.getState().runs[0]; assert.equal(finished.status, 'cancelled'); assert.ok(fs.existsSync(path.join(finished.runDirectory, 'cleanup.txt')));
  assert.ok(f.events.some(event => event.kind === 'progress' && event.run.stage === 'archive' && event.run.status === 'cancelling'));
});

test('successful real child progress registers verified trace and preserves raw data after removing record', async t => {
  const f = fixture(t); const plan = await planAndDevice(f.manager);
  f.manager.startCapture({planId: plan.planId}); await until(() => !f.manager.getState().activeRun);
  const run = f.manager.getState().runs[0]; assert.equal(run.status, 'succeeded'); assert.ok(run.traceId);
  const trace = f.manager.getTrace({id: run.traceId}); assert.equal(trace.manifest.device_serial, 'PHONE-A'); assert.equal(trace.fileCount, 1);
  f.manager.state.traces.find(item => item.id === run.traceId).manifest.title = '';
  const sources = f.manager.listTraceAnalysisSources(); assert.equal(sources[0].planTitle, '图库测试');
  assert.equal(f.manager.isAllowedPath(trace.files[0].absolutePath), true); assert.equal(f.manager.isAllowedPath(f.directory), false);
  const again = await f.manager.registerTrace({path: trace.runDirectory}); assert.equal(again.id, trace.id); assert.equal(f.manager.listTraces().length, 1);
  assert.deepEqual(f.manager.removeTrace({id: trace.id}), {removed: true, filesDeleted: false, id: trace.id});
  assert.ok(fs.existsSync(trace.files[0].absolutePath)); assert.equal(f.manager.isAllowedPath(trace.files[0].absolutePath), false);
});

test('runner failure and missing result stay failed without fabricated success', async t => {
  const f = fixture(t); let plan = await planAndDevice(f.manager, 'no-result');
  f.manager.startCapture({planId: plan.planId}); await until(() => !f.manager.getState().activeRun);
  assert.equal(f.manager.getState().runs[0].status, 'failed'); assert.match(f.manager.getState().runs[0].message, /没有返回完整结果/); assert.equal(f.manager.listTraces().length, 0);
  plan = await planAndDevice(f.manager, 'failed'); f.manager.startCapture({planId: plan.planId}); await until(() => !f.manager.getState().activeRun);
  assert.equal(f.manager.getState().runs[0].status, 'failed'); assert.ok(f.manager.getState().runs[0].traceId);
  assert.match(f.manager.getState().runs[0].message, /启动图库.*目标页面不匹配.*返回图库主页/);
});

test('invalid plan never enters saved plan list and partial PMU run remains partial', async t => {
  const f = fixture(t); await assert.rejects(f.manager.savePlan({plan: {invalid: true}}), /Invalid capture plan/); assert.equal(f.manager.getState().plans.length, 0);
  const plan = await planAndDevice(f.manager, 'partial'); f.manager.startCapture({planId: plan.planId}); await until(() => !f.manager.getState().activeRun);
  assert.equal(f.manager.getState().runs[0].status, 'partial'); assert.equal(f.manager.listTraces()[0].status, 'partial');
});

test('normal shutdown requests cancellation and waits for collector cleanup', async t => {
  const f = fixture(t); const plan = await planAndDevice(f.manager, 'wait'); f.manager.startCapture({planId: plan.planId});
  await until(() => f.manager.getState().activeRun?.status === 'running'); const closing = f.manager.dispose();
  assert.throws(() => f.manager.startCapture({planId: plan.planId}), /关闭/);
  await assert.rejects(f.manager.savePlan({plan: {title: 'late save'}}), /关闭/);
  await closing;
  const run = f.manager.getState().runs[0]; assert.equal(run.status, 'cancelled'); assert.ok(fs.existsSync(path.join(run.runDirectory, 'cleanup.txt')));
});

test('restart recovers interrupted journal without rerunning or choosing another phone', async t => {
  const f = fixture(t); await planAndDevice(f.manager); await f.manager.dispose();
  const file = path.join(f.directory, 'capture', 'state.json'); const stored = JSON.parse(fs.readFileSync(file)); stored.runs = [{runId: 'desktop_run_recovery', status: 'running', deviceSerial: 'PHONE-A', log: []}]; fs.writeFileSync(file, JSON.stringify(stored));
  const recovered = new CaptureManager(f.config); t.after(() => recovered.dispose());
  assert.equal(recovered.getState().runs[0].status, 'interrupted'); assert.equal(recovered.getState().activeRun, null); assert.equal(recovered.getState().selectedDeviceSerial, 'PHONE-A');
  assert.match(recovered.getState().recoveryNotice, /未自动重跑/);
});

test('restart bridges only an exact active Desktop run from the selected device', async t => {
  const f = fixture(t); const plan = await planAndDevice(f.manager, 'wait'); await f.manager.dispose();
  const outputRoot = fs.realpathSync(path.join(f.directory, 'capture', 'captures'));
  const planSnapshot = {...plan.plan, project_id: 'desktop-local'};
  const ids = {
    exact: 'capture_aaaaaaaaaaaaaaaaaaaaaaaa', historical: 'capture_bbbbbbbbbbbbbbbbbbbbbbbb',
    otherDevice: 'capture_cccccccccccccccccccccccc', wrongPath: 'capture_dddddddddddddddddddddddd',
    wrongRoot: 'capture_eeeeeeeeeeeeeeeeeeeeeeee', wrongProject: 'capture_ffffffffffffffffffffffff',
  };
  const createRun = (captureRunId, {deviceSerial = 'PHONE-A', projectId = 'desktop-local', root = outputRoot} = {}) => {
    const directory = path.join(root, 'scenario-capture', 'runs', captureRunId); fs.mkdirSync(directory, {recursive: true});
    fs.writeFileSync(path.join(directory, 'run.json'), JSON.stringify({
      schema_version: 'scenario_capture_run/v1', run_id: captureRunId, status: 'running', connection_mode: 'local',
      device_serial: deviceSerial, project_id: projectId, plan_id: planSnapshot.artifact_id,
    }));
    return directory;
  };
  const exactDirectory = createRun(ids.exact);
  const historicalDirectory = createRun(ids.historical);
  const otherDeviceDirectory = createRun(ids.otherDevice, {deviceSerial: 'PHONE-B'});
  createRun(ids.wrongPath);
  const foreignRoot = path.join(f.directory, 'other-instance-captures'); fs.mkdirSync(foreignRoot);
  const wrongRootDirectory = createRun(ids.wrongRoot, {root: foreignRoot});
  const wrongProjectDirectory = createRun(ids.wrongProject, {projectId: 'another-project'});
  const record = (suffix, captureRunId, runDirectory, overrides = {}) => ({
    runId: `desktop_run_${suffix.repeat(32)}`, captureRunId, status: 'running', deviceSerial: 'PHONE-A',
    outputRoot, runDirectory, planSnapshot, log: [], ...overrides,
  });
  const file = path.join(f.directory, 'capture', 'state.json'); const stored = JSON.parse(fs.readFileSync(file));
  stored.runs = [
    record('1', ids.exact, exactDirectory),
    record('2', ids.historical, historicalDirectory, {status: 'interrupted'}),
    record('3', ids.otherDevice, otherDeviceDirectory, {deviceSerial: 'PHONE-B'}),
    record('4', ids.wrongPath, exactDirectory),
    record('5', ids.wrongRoot, wrongRootDirectory, {outputRoot: foreignRoot}),
    record('6', ids.wrongProject, wrongProjectDirectory, {planSnapshot: {...planSnapshot, project_id: 'another-project'}}),
  ];
  fs.writeFileSync(file, JSON.stringify(stored));

  const recovered = new CaptureManager(f.config); t.after(() => recovered.dispose());
  assert.equal(recovered.getState().runs.find(run => run.captureRunId === ids.exact).status, 'interrupted');
  assert.deepEqual(recovered.recoverableDeviceRunIds('PHONE-A', 'desktop-local'), [ids.exact]);
  assert.deepEqual(recovered.recoverableDeviceRunIds('PHONE-B', 'desktop-local'), [ids.otherDevice]);
  assert.deepEqual(recovered.recoverableDeviceRunIds('PHONE-A', 'another-project'), [ids.wrongProject]);

  // Revalidate disk identity at use time instead of trusting startup state.
  const exactMetadata = JSON.parse(fs.readFileSync(path.join(exactDirectory, 'run.json')));
  fs.writeFileSync(path.join(exactDirectory, 'run.json'), JSON.stringify({...exactMetadata, device_serial: 'PHONE-B'}));
  assert.deepEqual(recovered.recoverableDeviceRunIds('PHONE-A', 'desktop-local'), []);
  fs.writeFileSync(path.join(exactDirectory, 'run.json'), JSON.stringify(exactMetadata));

  const started = recovered.startCapture({planId: plan.planId});
  assert.deepEqual(started.recoverableDeviceRunIds, [ids.exact]);
  await until(() => recovered.getState().activeRun?.progress?.device_serial === 'PHONE-A');
  recovered.cancelCapture({runId: started.runId});
  await until(() => !recovered.getState().activeRun);
});

test('forced shutdown records interruption instead of successful cancellation', async t => {
  const f = fixture(t, {disposeGraceMs: 100}); const plan = await planAndDevice(f.manager, 'hang'); f.manager.startCapture({planId: plan.planId});
  await until(() => f.manager.getState().activeRun?.status === 'running'); await f.manager.dispose();
  assert.equal(f.manager.getState().runs[0].status, 'interrupted'); assert.equal(f.manager.getState().runs[0].traceId, '');
});

test('registered file opening rejects a symlink swapped outside the trace directory', {skip: process.platform === 'win32' ? 'Symlink creation needs Windows developer privileges.' : false}, async t => {
  const f = fixture(t); const plan = await planAndDevice(f.manager); f.manager.startCapture({planId: plan.planId}); await until(() => !f.manager.getState().activeRun);
  const trace = f.manager.getTrace({id: f.manager.listTraces()[0].id}); const file = trace.files[0].absolutePath;
  const outside = path.join(f.directory, 'outside-secret'); fs.writeFileSync(outside, 'outside'); fs.unlinkSync(file); fs.symlinkSync(outside, file);
  assert.equal(f.manager.isAllowedPath(file), false);
  await assert.rejects(f.manager.registerTrace({path: trace.runDirectory}), /unsafe link/);
  const linkedDirectory = path.join(f.directory, 'linked-trace'); fs.symlinkSync(trace.runDirectory, linkedDirectory);
  await assert.rejects(f.manager.registerTrace({path: linkedDirectory}), /符号链接/);
});

test('real Python catalog and save-plan prepare an executable package without touching a device', async t => {
  const python = process.env.DESKTOP_TEST_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  if (spawnSync(python, ['--version']).status !== 0) { t.skip('Python is not installed in this test environment.'); return; }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-capture-real-'));
  const root = path.resolve(__dirname, '..', '..');
  const manager = new CaptureManager({userDataDir: directory, runtimeRoot: root, pythonCommand: [python, '-I', '-S']});
  t.after(async () => { await manager.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  const catalog = await manager.catalog(); assert.equal(catalog.default_capture.pmu.period_us, 10); assert.equal(catalog.templates[0].steps.length, 17);
  const plan = await manager.savePlan({plan: {title: '桌面集成验证', template_id: 'gallery_17_steps_v1', capture: {...catalog.default_capture, collectors: ['trace']}}});
  assert.ok(fs.existsSync(path.join(plan.packageDirectory, 'plan.json'))); assert.ok(fs.existsSync(plan.packageZip)); assert.equal(plan.plan.capture.pmu.period_us, 10);
});

test('real Python verifies standard archive import, merges directory identity, rejects zip traversal and tampering', async t => {
  const python = process.env.DESKTOP_TEST_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  if (spawnSync(python, ['--version']).status !== 0) { t.skip('Python is not installed in this test environment.'); return; }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-trace-import-'));
  const manager = new CaptureManager({userDataDir: path.join(directory, 'app'), runtimeRoot: path.resolve(__dirname, '..', '..'), pythonCommand: [python, '-I', '-S']});
  t.after(async () => { await manager.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  const runDirectory = path.join(directory, 'collected-run'); fs.mkdirSync(runDirectory);
  const content = Buffer.from('trace evidence on selected phone\n');
  const manifest = {schema_version: 'scenario_capture_manifest/v1', run_id: 'capture_' + 'a'.repeat(24), plan_id: 'plan_import_fixture', project_id: 'desktop-local', title: '图库17步', status: 'succeeded', device_serial: 'PHONE-A', steps: [{step_id: 'step_01', ordinal: 1, title: '编辑图片', captured: true, collector_status: {trace: 'collected'}, trace_validation: {valid: true}}], files: [{relative_path: 'step_01.trace', name: 'step_01.trace', kind: 'trace', step_id: 'step_01', size_bytes: content.length, sha256: require('node:crypto').createHash('sha256').update(content).digest('hex')}]};
  fs.writeFileSync(path.join(runDirectory, 'manifest.json'), JSON.stringify(manifest)); fs.writeFileSync(path.join(runDirectory, 'step_01.trace'), content);
  const archive = path.join(directory, 'standard.capture.zip'); const malicious = path.join(directory, 'traversal.capture.zip');
  const zipped = spawnSync(python, ['-I', '-S', '-c', 'import sys,zipfile,pathlib; root=pathlib.Path(sys.argv[1]); z=zipfile.ZipFile(sys.argv[2],"w",zipfile.ZIP_DEFLATED); z.write(root/"manifest.json","manifest.json"); z.write(root/"step_01.trace","step_01.trace"); z.close(); z=zipfile.ZipFile(sys.argv[3],"w"); z.writestr("../escape.txt","outside"); z.close()', runDirectory, archive, malicious], {encoding: 'utf8'});
  assert.equal(zipped.status, 0, zipped.stderr);
  const registered = await manager.registerTrace({path: archive}); assert.equal(registered.archivePath, fs.realpathSync(archive));
  const sources = manager.listTraceAnalysisSources(); assert.equal(sources.length, 1); assert.equal(sources[0].archiveAvailable, true); assert.equal(sources[0].planTitle, '图库17步'); assert.equal(sources[0].steps[0].ready, true); assert.equal(sources[0].steps[0].stepOrdinal, 1); assert.equal(sources[0].steps[0].title, '编辑图片');
  assert.equal(manager.traceArchiveForAnalysis({id: registered.id}).archivePath, fs.realpathSync(archive));
  const directoryRecord = await manager.registerTrace({path: runDirectory}); assert.equal(directoryRecord.id, registered.id); assert.equal(manager.listTraces().length, 1);
  const details = manager.getTrace({id: registered.id}); assert.equal(details.files[0].absolutePath, path.join(fs.realpathSync(runDirectory), 'step_01.trace'));
  assert.equal(details.availability.runDirectoryExists, true); assert.equal(details.availability.archiveExists, true);
  assert.equal(manager.isAllowedPath(archive), true); assert.equal(manager.isAllowedPath(details.files[0].absolutePath), true);
  await assert.rejects(manager.registerTrace({path: malicious}), /路径|path|采集包/);
  assert.ok(!fs.existsSync(path.join(directory, 'escape.txt')));
  fs.writeFileSync(path.join(runDirectory, 'step_01.trace'), Buffer.alloc(content.length, 65));
  await assert.rejects(manager.registerTrace({path: runDirectory}), /校验失败|hash/i);
  fs.writeFileSync(path.join(runDirectory, 'step_01.trace'), content);
  fs.writeFileSync(path.join(runDirectory, 'manifest.json'), JSON.stringify({...manifest, status: 'partial'}));
  await assert.rejects(manager.registerTrace({path: runDirectory}), /不同内容/);
});
