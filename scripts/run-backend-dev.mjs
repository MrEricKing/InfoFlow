import {execFile, spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {existsSync, readdirSync, statSync, watch} from 'node:fs';
import process from 'node:process';
import {dirname, relative, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

import {buildCondaPythonSpawn} from './conda-command.mjs';

const execFileAsync = promisify(execFile);

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');
const backendDir = resolve(repoRoot, 'backend');
const watchedDirs = [resolve(backendDir, 'app'), resolve(backendDir, 'alembic')];
const stableRunMs = 5000;
const reloadQuietMs = parsePositiveInteger(process.env.BACKEND_RELOAD_QUIET_MS, 1200, 'BACKEND_RELOAD_QUIET_MS');
const pollingIntervalMs = parsePositiveInteger(
  process.env.BACKEND_WATCH_POLL_INTERVAL_MS,
  1000,
  'BACKEND_WATCH_POLL_INTERVAL_MS',
);
const shutdownTimeoutMs = parsePositiveInteger(
  process.env.BACKEND_SHUTDOWN_TIMEOUT_MS,
  4000,
  'BACKEND_SHUTDOWN_TIMEOUT_MS',
);
const portReleaseTimeoutMs = parsePositiveInteger(
  process.env.BACKEND_PORT_RELEASE_TIMEOUT_MS,
  4000,
  'BACKEND_PORT_RELEASE_TIMEOUT_MS',
);
const backendHost = process.env.BACKEND_HOST || '127.0.0.1';
const backendPort = parsePort(process.env.BACKEND_PORT || process.env.PORT || '8000');
const backendCondaEnvName = 'infoflowaggregator';
const configuredPolling = parseBoolean(process.env.BACKEND_WATCH_POLLING, 'BACKEND_WATCH_POLLING');
const usePollingWatcher = configuredPolling ?? (process.platform === 'win32' || isWsl());

function parsePort(value) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`BACKEND_PORT must be an integer between 1 and 65535. Received: ${value}`);
  }
  return parsed;
}

function parsePositiveInteger(value, fallback, label) {
  if (!value) {
    return fallback;
  }

  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`${label} must be a positive integer. Received: ${value}`);
  }

  return parsed;
}

function parseBoolean(value, label) {
  if (value === undefined || value === null || value === '') {
    return null;
  }
  if (/^(1|true|yes|on)$/i.test(value)) {
    return true;
  }
  if (/^(0|false|no|off)$/i.test(value)) {
    return false;
  }
  throw new Error(`${label} must be true/false, 1/0, yes/no, or on/off. Received: ${value}`);
}

function isWsl() {
  return Boolean(process.env.WSL_DISTRO_NAME || process.env.WSL_INTEROP);
}

const backendPythonArgs = [
  '-m',
  'uvicorn',
  'app.main:app',
  '--host',
  backendHost,
  '--port',
  String(backendPort),
];

const migratePythonArgs = [
  '-m',
  'alembic',
  '-c',
  'alembic.ini',
  'upgrade',
  'head',
];

let child = null;
let backendStarting = false;
let backendStopping = false;
let backendFailedWaitingForChange = false;
let reloadTimer = null;
let pendingReloadReason = null;
let shuttingDown = false;
let watchers = [];
const watchedFileSnapshots = new Map();

function pipeOutput(stream, target) {
  if (!stream) {
    return;
  }

  stream.on('data', (chunk) => {
    target.write(chunk);
  });
}

function formatExit(code, signal) {
  if (signal) {
    return `signal ${signal}`;
  }
  return `code ${code ?? 0}`;
}

function formatListenError(error) {
  if (!error) {
    return 'unknown listen error';
  }

  const code = 'code' in error && error.code ? `${error.code}: ` : '';
  return `${code}${error.message}`;
}

function relativeToBackend(filePath) {
  return relative(backendDir, filePath).replaceAll('\\', '/');
}

function isIgnoredPath(filePath) {
  return filePath.split(/[\\/]/).includes('__pycache__');
}

function isPythonFile(filePath) {
  return typeof filePath === 'string' && filePath.endsWith('.py') && !isIgnoredPath(filePath);
}

function snapshotKey(filePath) {
  return resolve(filePath);
}

function readFileSnapshot(filePath) {
  try {
    const stats = statSync(filePath);
    if (!stats.isFile()) {
      return null;
    }

    return {mtimeMs: stats.mtimeMs, size: stats.size};
  } catch {
    return null;
  }
}

function collectPythonFileSnapshots(directory, snapshots = new Map()) {
  let entries;
  try {
    entries = readdirSync(directory, {withFileTypes: true});
  } catch {
    return snapshots;
  }

  for (const entry of entries) {
    const entryPath = resolve(directory, entry.name);
    if (isIgnoredPath(entryPath)) {
      continue;
    }

    if (entry.isDirectory()) {
      collectPythonFileSnapshots(entryPath, snapshots);
      continue;
    }

    if (!entry.isFile() || !isPythonFile(entryPath)) {
      continue;
    }

    const snapshot = readFileSnapshot(entryPath);
    if (snapshot) {
      snapshots.set(snapshotKey(entryPath), snapshot);
    }
  }

  return snapshots;
}

function seedWatchedFileSnapshots() {
  watchedFileSnapshots.clear();
  for (const watchedDir of watchedDirs) {
    collectPythonFileSnapshots(watchedDir, watchedFileSnapshots);
  }
}

function snapshotsMatch(left, right) {
  return left?.mtimeMs === right?.mtimeMs && left?.size === right?.size;
}

function summarizeReasons(reasons) {
  if (reasons.length === 1) {
    return reasons[0];
  }
  return `Detected ${reasons.length} Python file changes; last change: ${reasons.at(-1)}`;
}

function resolveWatcherChange(watchedDir, eventType, filename) {
  if (typeof filename !== 'string' || !isPythonFile(filename)) {
    return null;
  }

  const filePath = resolve(watchedDir, filename);
  if (!isPythonFile(filePath)) {
    return null;
  }

  const key = snapshotKey(filePath);
  const previous = watchedFileSnapshots.get(key) ?? null;
  const current = readFileSnapshot(filePath);
  const displayPath = relativeToBackend(filePath);

  if (!current) {
    if (!previous) {
      return null;
    }

    watchedFileSnapshots.delete(key);
    return `Detected Python ${eventType} for deleted ${displayPath}`;
  }

  if (snapshotsMatch(previous, current)) {
    return null;
  }

  watchedFileSnapshots.set(key, current);
  const changeType = previous ? 'change' : 'new file';
  return `Detected Python ${changeType} (${eventType}) in ${displayPath}`;
}

function scanForPollingChanges() {
  const currentSnapshots = new Map();
  for (const watchedDir of watchedDirs) {
    collectPythonFileSnapshots(watchedDir, currentSnapshots);
  }

  const reasons = [];
  for (const [key, current] of currentSnapshots) {
    const previous = watchedFileSnapshots.get(key) ?? null;
    if (!previous) {
      reasons.push(`new ${relativeToBackend(key)}`);
      continue;
    }
    if (!snapshotsMatch(previous, current)) {
      reasons.push(relativeToBackend(key));
    }
  }

  for (const key of watchedFileSnapshots.keys()) {
    if (!currentSnapshots.has(key)) {
      reasons.push(`deleted ${relativeToBackend(key)}`);
    }
  }

  watchedFileSnapshots.clear();
  for (const [key, snapshot] of currentSnapshots) {
    watchedFileSnapshots.set(key, snapshot);
  }

  if (reasons.length > 0) {
    queueReload(summarizeReasons(reasons));
  }
}

function queueReload(reason) {
  if (shuttingDown || !reason) {
    return;
  }

  pendingReloadReason = reason;
  if (reloadTimer) {
    clearTimeout(reloadTimer);
  }

  reloadTimer = setTimeout(() => {
    reloadTimer = null;
    const queuedReason = pendingReloadReason;
    pendingReloadReason = null;
    void restartBackend(queuedReason);
  }, reloadQuietMs);
}

function queuePendingReloadIfNeeded() {
  if (!pendingReloadReason || reloadTimer || backendStopping || backendStarting || shuttingDown) {
    return;
  }

  const reason = pendingReloadReason;
  pendingReloadReason = null;
  queueReload(reason);
}

function sleep(ms) {
  return new Promise((resolvePromise) => {
    setTimeout(resolvePromise, ms);
  });
}

function checkPortAvailable(port, host) {
  return new Promise((resolvePromise) => {
    const server = createServer();
    let resolved = false;

    function resolveOnce(result) {
      if (resolved) {
        return;
      }
      resolved = true;
      server.removeAllListeners();
      resolvePromise(result);
    }

    server.once('error', (error) => {
      resolveOnce({available: false, error});
    });

    server.listen(port, host, () => {
      server.close(() => resolveOnce({available: true}));
    });
  });
}

async function runQuiet(command, args) {
  try {
    const result = await execFileAsync(command, args, {
      windowsHide: true,
      maxBuffer: 1024 * 1024,
    });
    return result.stdout;
  } catch (error) {
    return error.stdout ?? '';
  }
}

async function describePortOwners() {
  if (process.platform === 'win32') {
    const stdout = await runQuiet('powershell.exe', [
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      [
        `$connections = Get-NetTCPConnection -LocalPort ${backendPort} -State Listen -ErrorAction SilentlyContinue`,
        '$connections | ForEach-Object {',
        '  $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue',
        '  [PSCustomObject]@{ LocalAddress=$_.LocalAddress; LocalPort=$_.LocalPort; OwningProcess=$_.OwningProcess; CommandLine=$processInfo.CommandLine }',
        '} | ConvertTo-Json -Compress',
      ].join('; '),
    ]);
    return stdout.trim();
  }

  return (
    await runQuiet('lsof', ['-nP', `-iTCP:${backendPort}`, '-sTCP:LISTEN'])
  ).trim();
}

async function ensureBackendPortAvailable(context) {
  const check = await checkPortAvailable(backendPort, backendHost);
  if (check.available) {
    return true;
  }

  const owners = await describePortOwners();
  console.error(
    `[backend] Cannot ${context}: ${backendHost}:${backendPort} is already in use (${formatListenError(
      check.error,
    )}).`,
  );
  if (owners) {
    console.error(`[backend] Current listener(s):\n${owners}`);
  }
  console.error('[backend] Waiting for another Python file change before trying again.');
  backendFailedWaitingForChange = true;
  return false;
}

async function waitForPortRelease() {
  const deadline = Date.now() + portReleaseTimeoutMs;
  while (!shuttingDown && Date.now() < deadline) {
    const check = await checkPortAvailable(backendPort, backendHost);
    if (check.available) {
      return true;
    }
    await sleep(100);
  }

  return false;
}

function waitForExit(target, timeoutMs) {
  if (target.exitCode !== null || target.signalCode !== null) {
    return Promise.resolve(true);
  }

  return new Promise((resolvePromise) => {
    const timer = setTimeout(() => {
      target.off('exit', onExit);
      resolvePromise(false);
    }, timeoutMs);

    function onExit() {
      clearTimeout(timer);
      resolvePromise(true);
    }

    target.once('exit', onExit);
  });
}

async function terminateProcess(target) {
  if (!target?.pid) {
    return;
  }

  if (process.platform === 'win32') {
    await new Promise((resolvePromise) => {
      const killer = spawn('taskkill', ['/PID', String(target.pid), '/T', '/F'], {
        stdio: 'ignore',
        windowsHide: true,
      });
      killer.on('exit', () => resolvePromise());
      killer.on('error', () => resolvePromise());
    });
    return;
  }

  try {
    process.kill(-target.pid, 'SIGTERM');
  } catch {
    try {
      target.kill('SIGTERM');
    } catch {
      // Already exited.
    }
  }

  const exited = await waitForExit(target, shutdownTimeoutMs);
  if (exited) {
    return;
  }

  try {
    process.kill(-target.pid, 'SIGKILL');
  } catch {
    try {
      target.kill('SIGKILL');
    } catch {
      // Already exited.
    }
  }

  await waitForExit(target, 1000);
}

async function shutdown(code = 0) {
  if (shuttingDown) {
    return;
  }

  shuttingDown = true;
  if (reloadTimer) {
    clearTimeout(reloadTimer);
    reloadTimer = null;
  }
  for (const watcher of watchers) {
    watcher.close();
  }
  watchers = [];

  backendStopping = true;
  await terminateProcess(child);
  process.exit(code);
}

async function restartBackend(reason) {
  if (shuttingDown || !reason) {
    return;
  }

  if (backendStarting || backendStopping) {
    pendingReloadReason = reason;
    return;
  }

  backendStopping = true;
  const action = backendFailedWaitingForChange && !child ? 'Starting backend again' : 'Restarting backend';
  console.error(`[backend] ${reason}. ${action} after ${reloadQuietMs}ms quiet window...`);
  await terminateProcess(child);
  child = null;
  const portReleased = await waitForPortRelease();
  backendStopping = false;

  if (!portReleased) {
    backendFailedWaitingForChange = true;
    const owners = await describePortOwners();
    console.error(`[backend] Port ${backendHost}:${backendPort} did not become available after shutdown.`);
    if (owners) {
      console.error(`[backend] Current listener(s):\n${owners}`);
    }
    console.error('[backend] Waiting for another Python file change before trying again.');
    queuePendingReloadIfNeeded();
    return;
  }

  await startBackend();
}

function buildBackendPythonSpawn(args) {
  return buildCondaPythonSpawn(args, {envName: backendCondaEnvName});
}

function runPythonCommand(args, label) {
  return new Promise((resolvePromise, rejectPromise) => {
    const pythonCommand = buildBackendPythonSpawn(args);
    const currentChild = spawn(pythonCommand.command, pythonCommand.args, {
      cwd: backendDir,
      env: pythonCommand.env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });

    pipeOutput(currentChild.stdout, process.stdout);
    pipeOutput(currentChild.stderr, process.stderr);

    currentChild.on('error', rejectPromise);
    currentChild.on('exit', (code, signal) => {
      if (signal || code) {
        rejectPromise(new Error(`${label} exited with ${formatExit(code, signal)}`));
        return;
      }
      resolvePromise();
    });
  });
}

async function runMigrations() {
  console.error('[backend] Running database migrations...');
  await runPythonCommand(migratePythonArgs, 'Database migration');
}

async function startBackend() {
  if (backendStarting || backendStopping || shuttingDown) {
    return;
  }

  backendStarting = true;
  backendFailedWaitingForChange = false;
  try {
    if (!(await ensureBackendPortAvailable('start backend'))) {
      return;
    }

    await runMigrations();

    if (!(await ensureBackendPortAvailable('start backend'))) {
      return;
    }
  } catch (error) {
    backendFailedWaitingForChange = true;
    console.error(
      `[backend] Backend startup failed: ${error instanceof Error ? error.message : String(error)}`,
    );
    console.error('[backend] Waiting for another Python file change before trying again.');
    return;
  } finally {
    backendStarting = false;
    queuePendingReloadIfNeeded();
  }

  const pythonCommand = buildBackendPythonSpawn(backendPythonArgs);
  const startedAt = Date.now();

  const currentChild = spawn(pythonCommand.command, pythonCommand.args, {
    cwd: backendDir,
    env: pythonCommand.env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
    detached: process.platform !== 'win32',
  });
  child = currentChild;

  pipeOutput(currentChild.stdout, process.stdout);
  pipeOutput(currentChild.stderr, process.stderr);

  currentChild.on('exit', (code, signal) => {
    if (child === currentChild) {
      child = null;
    }
    if (shuttingDown || backendStopping) {
      return;
    }

    const ranForMs = Date.now() - startedAt;
    const stableSuffix = ranForMs >= stableRunMs ? ' after a stable run' : ' during startup';
    backendFailedWaitingForChange = true;
    console.error(
      `[backend] Backend dev server exited with ${formatExit(
        code,
        signal,
      )}${stableSuffix}. Waiting for another Python file change before trying again.`,
    );
  });

  currentChild.on('error', (error) => {
    if (child === currentChild) {
      child = null;
    }
    if (shuttingDown || backendStopping) {
      return;
    }
    backendFailedWaitingForChange = true;
    console.error(
      `[backend] Failed to start backend dev server: ${error.message}. Waiting for another Python file change before trying again.`,
    );
  });
}

function startPollingWatcher() {
  console.error(
    `[backend] Watching Python files by polling every ${pollingIntervalMs}ms. Set BACKEND_WATCH_POLLING=0 to try native file events.`,
  );
  const timer = setInterval(scanForPollingChanges, pollingIntervalMs);
  watchers.push({
    close() {
      clearInterval(timer);
    },
  });
}

function startNativeWatchers() {
  try {
    for (const watchedDir of watchedDirs) {
      if (!existsSync(watchedDir)) {
        console.error(`[backend] Watch directory does not exist: ${watchedDir}`);
        continue;
      }

      const watcher = watch(watchedDir, {recursive: true}, (eventType, filename) => {
        queueReload(resolveWatcherChange(watchedDir, eventType, filename));
      });

      watcher.on('error', (error) => {
        console.error(`[backend] File watcher failed for ${watchedDir}: ${error.message}`);
      });

      watchers.push(watcher);
      console.error(`[backend] Watching ${watchedDir} for Python changes.`);
    }
  } catch (error) {
    console.error(
      `[backend] Native file watcher is unavailable: ${
        error instanceof Error ? error.message : String(error)
      }. Falling back to polling.`,
    );
    for (const watcher of watchers) {
      watcher.close();
    }
    watchers = [];
    startPollingWatcher();
  }
}

function startWatcher() {
  const pythonCommand = buildBackendPythonSpawn(['--version']);
  console.error(`[backend] Python runner: ${pythonCommand.description}`);
  console.error(`[backend] Backend address: ${backendHost}:${backendPort}`);
  console.error(`[backend] Reload quiet window: ${reloadQuietMs}ms`);

  seedWatchedFileSnapshots();
  if (usePollingWatcher) {
    startPollingWatcher();
  } else {
    startNativeWatchers();
  }
}

process.on('SIGINT', () => {
  void shutdown(0);
});

process.on('SIGTERM', () => {
  void shutdown(0);
});

startWatcher();
void startBackend();
