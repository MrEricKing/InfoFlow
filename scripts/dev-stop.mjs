import {execFile} from 'node:child_process';
import process from 'node:process';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

const execFileAsync = promisify(execFile);

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');
const frontendDir = resolve(repoRoot, 'frontend');
const backendDir = resolve(repoRoot, 'backend');
const projectPathMarker = '\\infoflow-aggregator\\';
const frontendPathMarker = `${projectPathMarker}frontend`;
const backendPathMarker = `${projectPathMarker}backend`;
const scriptsPathMarker = `${projectPathMarker}scripts`;
const portWindow = parsePort(process.env.INFOFLOW_DEV_STOP_PORT_WINDOW, 100);
const currentPid = process.pid;
const excludedPids = parseExcludedPids(process.env.INFOFLOW_DEV_STOP_EXCLUDE_PIDS);
excludedPids.add(currentPid);
const backendPort = parsePort(process.env.BACKEND_PORT || process.env.PORT, 8000);
const forceBackendPortCleanup = /^(1|true|yes|on)$/i.test(
  String(process.env.INFOFLOW_DEV_STOP_FORCE_BACKEND_PORT ?? ''),
);

function parseExcludedPids(value) {
  return new Set(
    String(value ?? '')
      .split(/[,\s;]+/)
      .map((entry) => Number.parseInt(entry, 10))
      .filter((pid) => Number.isInteger(pid) && pid > 0),
  );
}

function isExcludedPid(pid) {
  return excludedPids.has(pid);
}

function parsePort(value, fallback) {
  if (!value) {
    return fallback;
  }

  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    return fallback;
  }

  return parsed;
}

function buildPortSet() {
  const frontendPort = parsePort(process.env.FRONTEND_PORT || process.env.VITE_PORT, 5173);
  const ports = new Set();

  for (let offset = 0; offset <= portWindow; offset += 1) {
    ports.add(frontendPort + offset);
    ports.add(backendPort + offset);
  }

  return ports;
}

function normalize(value) {
  return String(value ?? '').replaceAll('/', '\\').toLowerCase();
}

function includesPath(commandLine, path) {
  return normalize(commandLine).includes(normalize(path));
}

function includesInfoFlowPath(commandLine, pathMarker) {
  return normalize(commandLine).includes(pathMarker);
}

function formatCommand(commandLine) {
  const value = String(commandLine ?? '').replace(/\s+/g, ' ').trim();
  if (value.length <= 140) {
    return value;
  }
  return `${value.slice(0, 137)}...`;
}

function delay(ms) {
  return new Promise((resolvePromise) => {
    setTimeout(resolvePromise, ms);
  });
}

async function runQuiet(command, args, options = {}) {
  try {
    const result = await execFileAsync(command, args, {
      windowsHide: true,
      maxBuffer: 10 * 1024 * 1024,
      ...options,
    });
    return result.stdout;
  } catch (error) {
    if (options.allowFailure) {
      return error.stdout ?? '';
    }
    throw error;
  }
}

function parseJsonArray(stdout) {
  const trimmed = stdout.trim();
  if (!trimmed) {
    return [];
  }

  const parsed = JSON.parse(trimmed);
  return Array.isArray(parsed) ? parsed : [parsed];
}

async function getWindowsProcesses() {
  const stdout = await runQuiet('powershell.exe', [
    '-NoProfile',
    '-NonInteractive',
    '-Command',
    [
      'Get-CimInstance Win32_Process',
      'Select-Object ProcessId,ParentProcessId,Name,CommandLine',
      'ConvertTo-Json -Compress',
    ].join(' | '),
  ]);

  return parseJsonArray(stdout).map((processInfo) => ({
    pid: Number(processInfo.ProcessId),
    ppid: Number(processInfo.ParentProcessId),
    name: processInfo.Name ?? '',
    commandLine: processInfo.CommandLine ?? '',
  }));
}

async function getWindowsListeningPorts(portSet) {
  const ports = [...portSet].sort((a, b) => a - b).join(',');
  const stdout = await runQuiet('powershell.exe', [
    '-NoProfile',
    '-NonInteractive',
    '-Command',
    [
      `$ports = @(${ports}); Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue`,
      'Where-Object { $ports -contains $_.LocalPort }',
      'Select-Object LocalAddress,LocalPort,OwningProcess',
      'ConvertTo-Json -Compress',
    ].join(' | '),
  ]);

  return parseJsonArray(stdout).map((connection) => ({
    address: connection.LocalAddress ?? '',
    port: Number(connection.LocalPort),
    pid: Number(connection.OwningProcess),
  }));
}

function isProjectDevManager(processInfo) {
  const commandLine = normalize(processInfo.commandLine);
  if (isExcludedPid(processInfo.pid)) {
    return false;
  }

  return includesPath(commandLine, resolve(repoRoot, 'scripts', 'dev.mjs')) ||
    includesPath(commandLine, resolve(repoRoot, 'scripts', 'run-backend-dev.mjs')) ||
    (includesInfoFlowPath(commandLine, scriptsPathMarker) &&
      (commandLine.includes('dev.mjs') || commandLine.includes('run-backend-dev.mjs')));
}

function isProjectFrontendProcess(processInfo) {
  const commandLine = normalize(processInfo.commandLine);
  return (
    (includesPath(commandLine, frontendDir) || includesInfoFlowPath(commandLine, frontendPathMarker)) &&
    (commandLine.includes('vite') || commandLine.includes('npm') || commandLine.includes('node'))
  );
}

function isProjectBackendProcess(processInfo) {
  const commandLine = normalize(processInfo.commandLine);
  return (
    includesPath(commandLine, backendDir) ||
    includesInfoFlowPath(commandLine, backendPathMarker) ||
    (commandLine.includes('uvicorn') && commandLine.includes('app.main:app')) ||
    commandLine.includes('run-backend-dev.mjs')
  );
}

function isTargetPortOwner(processInfo) {
  return isProjectFrontendProcess(processInfo) || isProjectBackendProcess(processInfo);
}

async function killWindowsTree(pid) {
  await runQuiet('taskkill', ['/PID', String(pid), '/T', '/F'], {allowFailure: true});
}

async function getUnixProcesses() {
  const stdout = await runQuiet('ps', ['-eo', 'pid=,ppid=,command='], {allowFailure: true});
  return stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^(\d+)\s+(\d+)\s+(.*)$/);
      if (!match) {
        return null;
      }
      return {
        pid: Number(match[1]),
        ppid: Number(match[2]),
        name: '',
        commandLine: match[3],
      };
    })
    .filter(Boolean);
}

async function getUnixListeningPorts(portSet) {
  const connections = [];
  const seen = new Set();
  const ports = [...portSet].sort((a, b) => a - b);
  const chunkSize = 80;

  for (let index = 0; index < ports.length; index += chunkSize) {
    const portArgs = ports.slice(index, index + chunkSize).map((port) => `-iTCP:${port}`);
    const stdout = await runQuiet('lsof', ['-nP', ...portArgs, '-sTCP:LISTEN', '-Fpn'], {
      allowFailure: true,
    });
    let currentPid = null;

    for (const line of stdout.split(/\r?\n/)) {
      if (line.startsWith('p')) {
        const pid = Number(line.slice(1));
        currentPid = Number.isInteger(pid) && pid > 0 ? pid : null;
        continue;
      }

      if (!line.startsWith('n') || currentPid === null) {
        continue;
      }

      const portMatch = line.match(/:(\d+)$/);
      if (!portMatch) {
        continue;
      }

      const port = Number(portMatch[1]);
      if (!portSet.has(port)) {
        continue;
      }

      const key = `${currentPid}:${port}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      connections.push({address: '', port, pid: currentPid});
    }
  }

  return connections;
}

async function killUnixTree(pid, processList) {
  const childrenByParent = new Map();
  for (const processInfo of processList) {
    if (!childrenByParent.has(processInfo.ppid)) {
      childrenByParent.set(processInfo.ppid, []);
    }
    childrenByParent.get(processInfo.ppid).push(processInfo.pid);
  }

  const descendants = [];
  const visit = (targetPid) => {
    for (const childPid of childrenByParent.get(targetPid) ?? []) {
      visit(childPid);
      descendants.push(childPid);
    }
  };
  visit(pid);

  for (const targetPid of [...descendants, pid]) {
    if (isExcludedPid(targetPid)) {
      continue;
    }
    try {
      process.kill(targetPid, 'SIGTERM');
    } catch {
      // Already exited.
    }
  }

  await delay(800);

  for (const targetPid of [...descendants, pid]) {
    if (isExcludedPid(targetPid)) {
      continue;
    }
    try {
      process.kill(targetPid, 0);
      process.kill(targetPid, 'SIGKILL');
    } catch {
      // Already exited.
    }
  }
}

function describeProcess(processInfo) {
  const command = formatCommand(processInfo.commandLine);
  return `${processInfo.pid}${command ? ` ${command}` : ''}`;
}

function uniqueProcessesByPid(processes) {
  return [...new Map(processes.map((processInfo) => [processInfo.pid, processInfo])).values()]
    .filter((processInfo) => Number.isInteger(processInfo.pid) && processInfo.pid > 0)
    .filter((processInfo) => !isExcludedPid(processInfo.pid));
}

function processInfoForListener(connection, processByPid) {
  return processByPid.get(connection.pid) ?? {
    pid: connection.pid,
    ppid: 0,
    name: '',
    commandLine: '',
  };
}

async function stopDevProcesses() {
  const portSet = buildPortSet();
  const isWindows = process.platform === 'win32';
  const getProcesses = isWindows ? getWindowsProcesses : getUnixProcesses;
  const getListeningPorts = isWindows ? getWindowsListeningPorts : getUnixListeningPorts;
  const killTree = isWindows
    ? (pid) => killWindowsTree(pid)
    : (pid, processes) => killUnixTree(pid, processes);

  console.log('[stop] Stopping InfoFlowAggregator frontend/backend dev processes...');

  let processes = await getProcesses();
  const managers = uniqueProcessesByPid(processes.filter(isProjectDevManager));
  for (const processInfo of managers) {
    console.log(`[stop] Killing dev manager: ${describeProcess(processInfo)}`);
    await killTree(processInfo.pid, processes);
  }

  if (managers.length > 0) {
    await delay(1000);
  }

  processes = await getProcesses();
  let listeners = await getListeningPorts(portSet);
  const processByPid = new Map(processes.map((processInfo) => [processInfo.pid, processInfo]));
  const portOwners = uniqueProcessesByPid(
    listeners
      .map((connection) => processByPid.get(connection.pid))
      .filter(Boolean)
      .filter(isTargetPortOwner),
  );

  for (const processInfo of portOwners) {
    const ownedPorts = listeners
      .filter((connection) => connection.pid === processInfo.pid)
      .map((connection) => connection.port)
      .sort((a, b) => a - b)
      .join(', ');
    console.log(`[stop] Killing port owner on ${ownedPorts}: ${describeProcess(processInfo)}`);
    await killTree(processInfo.pid, processes);
  }

  let forcedBackendPortOwners = [];
  if (forceBackendPortCleanup) {
    await delay(portOwners.length > 0 ? 1000 : 0);
    processes = await getProcesses();
    listeners = await getListeningPorts(portSet);
    const refreshedProcessByPid = new Map(processes.map((processInfo) => [processInfo.pid, processInfo]));
    forcedBackendPortOwners = uniqueProcessesByPid(
      listeners
        .filter((connection) => connection.port === backendPort)
        .map((connection) => processInfoForListener(connection, refreshedProcessByPid)),
    );

    for (const processInfo of forcedBackendPortOwners) {
      console.log(
        `[stop] Force killing backend port ${backendPort} owner: ${describeProcess(processInfo)}`,
      );
      await killTree(processInfo.pid, processes);
    }
  }

  await delay(1000);

  listeners = await getListeningPorts(portSet);
  processes = await getProcesses();
  const remainingByPid = new Map(processes.map((processInfo) => [processInfo.pid, processInfo]));
  const remainingTargets = listeners
    .map((connection) => ({
      ...connection,
      processInfo: processInfoForListener(connection, remainingByPid),
    }))
    .filter((connection) => {
      if (!connection.processInfo) {
        return false;
      }
      return (
        isTargetPortOwner(connection.processInfo) ||
        (forceBackendPortCleanup && connection.port === backendPort)
      );
    });

  if (remainingTargets.length > 0) {
    console.error('[stop] Some frontend/backend ports are still occupied:');
    for (const connection of remainingTargets) {
      console.error(
        `[stop] ${connection.address}:${connection.port} pid=${describeProcess(connection.processInfo)}`,
      );
    }
    process.exitCode = 1;
    return;
  }

  const stoppedCount = managers.length + portOwners.length + forcedBackendPortOwners.length;
  if (stoppedCount === 0) {
    console.log('[stop] No matching frontend/backend dev processes were running.');
  } else {
    console.log(`[stop] Stopped ${stoppedCount} process tree(s).`);
  }
  console.log('[stop] Frontend/backend dev ports are clear.');
}

stopDevProcesses().catch((error) => {
  console.error(`[stop] ${error.message}`);
  process.exit(1);
});
