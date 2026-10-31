import {readFileSync, rmSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import process from 'node:process';

const root = resolve(import.meta.dirname, '..');
const frontendRoot = join(root, 'frontend');
const outDir = join(root, '.tmp', 'frontend-logic-tests');
rmSync(outDir, {force: true, recursive: true});

const tsc = spawnSync(
  process.platform === 'win32' ? 'npm.cmd' : 'npm',
  [
    'exec',
    'tsc',
    '--',
    '--target',
    'ES2022',
    '--module',
    'NodeNext',
    '--moduleResolution',
    'NodeNext',
    '--strict',
    '--skipLibCheck',
    '--outDir',
    outDir,
    'src/hooks/info-flow/insight-artifact-fill.ts',
    'src/hooks/info-flow/insight-artifact-fill.test.ts',
  ],
  {cwd: frontendRoot, stdio: 'inherit'},
);
if (tsc.status !== 0) {
  process.exit(tsc.status ?? 1);
}

writeFileSync(join(outDir, 'package.json'), '{"type":"module"}\n', 'utf8');

const node = spawnSync(
  process.execPath,
  [join(outDir, 'insight-artifact-fill.test.js')],
  {cwd: root, stdio: 'inherit'},
);
if (node.status !== 0) {
  process.exit(node.status ?? 1);
}

const settingsPanelSource = readFileSync(
  join(frontendRoot, 'src/components/insight-engine-settings-panel.tsx'),
  'utf8',
);
function extractSettingsPanelFunction(functionName) {
  const start = settingsPanelSource.indexOf(`function ${functionName}(`);
  if (start === -1) {
    throw new Error(`Expected ${functionName} to exist in InsightEngineSettingsPanel.`);
  }
  const bodyStart = settingsPanelSource.indexOf('{', start);
  if (bodyStart === -1) {
    throw new Error(`Could not find the body of ${functionName}.`);
  }
  let depth = 0;
  for (let index = bodyStart; index < settingsPanelSource.length; index += 1) {
    const char = settingsPanelSource[index];
    if (char === '{') {
      depth += 1;
    } else if (char === '}') {
      depth -= 1;
      if (depth === 0) {
        return settingsPanelSource.slice(start, index + 1);
      }
    }
  }
  throw new Error(`Could not find the end of ${functionName}.`);
}

const refreshAttemptsSource = extractSettingsPanelFunction('refreshAttempts');
if (
  !/attemptRequestSeq\.current \+= 1;[\s\S]*setAttemptOffset\(0\);/.test(refreshAttemptsSource)
) {
  throw new Error('Expected refreshAttempts to invalidate stale attempt requests before resetting offset.');
}

const refreshAttemptsForLatestFiltersSource = extractSettingsPanelFunction('refreshAttemptsForLatestFilters');
if (
  !/attemptFiltersRef\.current[\s\S]*setAttemptOffset\(0\);[\s\S]*loadAttempts\(\{filters: \{\.\.\.latestFilters, offset: 0\}\}\)/.test(
    refreshAttemptsForLatestFiltersSource,
  )
) {
  throw new Error('Expected batch polling completion to refresh attempts with the latest filters, not a stale closure.');
}

if (
  !settingsPanelSource.includes('attemptFiltersRef') ||
  !settingsPanelSource.includes('coverageSourceIdRef') ||
  !settingsPanelSource.includes('hasCoverageScanRef')
) {
  throw new Error('Expected diagnostics polling to keep latest attempt/coverage filters in refs.');
}

if (
  !/refreshAttemptsForLatestFilters\(\);[\s\S]*hasCoverageScanRef\.current[\s\S]*loadCoverage\('后台洞察任务完成，已重新检测当前范围。', coverageSourceIdRef\.current\)/.test(
    settingsPanelSource,
  )
) {
  throw new Error('Expected operation polling completion to refresh coverage with the latest source filter.');
}

const filterChangeFunctions = [
  'changeAttemptStatus',
  'changeAttemptSourceId',
  'changeAttemptProfile',
  'changeAttemptTimeRange',
];
for (const functionName of filterChangeFunctions) {
  const functionSource = extractSettingsPanelFunction(functionName);
  if (!/attemptRequestSeq\.current \+= 1;[\s\S]*setAttemptOffset\(0\);/.test(functionSource)) {
    throw new Error(`Expected ${functionName} to invalidate stale attempt requests before resetting offset.`);
  }
}

const postActionFunctions = [
  'runAttemptItemAction',
  'runCoverageItemAction',
  'buildCoverageBatch',
];
for (const functionName of postActionFunctions) {
  const functionSource = extractSettingsPanelFunction(functionName);
  if (!/refreshAttempts\(\)/.test(functionSource) || /await loadAttempts\(\)/.test(functionSource)) {
    throw new Error(`Expected ${functionName} to refresh attempts through refreshAttempts().`);
  }
}

if (settingsPanelSource.includes(['批量', '重试失败'].join(''))) {
  throw new Error('Expected the ambiguous failed-attempt batch retry button label to stay out of the diagnostics UI.');
}

const diagnosticsUiExpectations = [
  '洞察覆盖与生成诊断',
  '失败/跳过历史日志',
  '历史日志不会因 item 补齐成功而删除',
  '刷新历史日志',
  '当前问题',
  '阻塞诊断',
  '正文提取失败/仅跳转链接',
];
for (const expectedText of diagnosticsUiExpectations) {
  if (!settingsPanelSource.includes(expectedText)) {
    throw new Error(`Expected Insight Engine diagnostics UI to include "${expectedText}".`);
  }
}
