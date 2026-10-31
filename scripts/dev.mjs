import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import process from 'node:process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import {ensureDevPostgres, getDevDatabaseUrl} from './dev-postgres.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');
const frontendDir = resolve(repoRoot, 'frontend');
const backendScript = resolve(repoRoot, 'scripts', 'run-backend-dev.mjs');
const devStopScript = resolve(repoRoot, 'scripts', 'dev-stop.mjs');
const defaultFrontendPort = 5173;
const defaultBackendPort = 8000;
const frontendHost = process.env.FRONTEND_HOST || process.env.VITE_HOST || '0.0.0.0';
const backendHost = process.env.BACKEND_HOST || '127.0.0.1';
const defaultBackendHealthTimeoutMs = 120000;

const children = new Set();
const restartTimers = new Set();
let shuttingDown = false;
let exitCode = 0;
let sharedEnv = buildSharedEnv(defaultFrontendPort, defaultBackendPort);

function parsePort(value, fallback, label) {
  if (!value) {
    return fallback;
  }

  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${label} must be an integer between 1 and 65535. Received: ${value}`);
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

function formatListenError(error) {
  if (!error) {
    return 'unknown listen error';
  }

  const code = 'code' in error && error.code ? `${error.code}: ` : '';
  return `${code}${error.message}`;
}

async function chooseFrontendPort() {
  const configuredPort = process.env.FRONTEND_PORT || process.env.VITE_PORT;
  const requestedPort = parsePort(configuredPort, defaultFrontendPort, 'FRONTEND_PORT');
  const firstCheck = await checkPortAvailable(requestedPort, frontendHost);

  if (firstCheck.available) {
    return requestedPort;
  }

  throw new Error(
    `Frontend port ${requestedPort} is unavailable on ${frontendHost} (${formatListenError(
      firstCheck.error,
    )}). Run npm run stop or stop the process that owns this port before starting dev.`,
  );
}

async function chooseBackendPort() {
  const configuredPort = process.env.BACKEND_PORT || process.env.PORT;
  const requestedPort = parsePort(configuredPort, defaultBackendPort, 'BACKEND_PORT');
  const firstCheck = await checkPortAvailable(requestedPort, backendHost);

  if (firstCheck.available) {
    return requestedPort;
  }

  throw new Error(
    `Backend port ${requestedPort} is unavailable on ${backendHost} (${formatListenError(
      firstCheck.error,
    )}). Run npm run stop or stop the process that owns this port before starting dev.`,
  );
}

function mergeCorsOrigins(frontendPort) {
  const frontendOrigins = [`http://localhost:${frontendPort}`, `http://127.0.0.1:${frontendPort}`];
  const configuredOrigins = process.env.CORS_ORIGINS || process.env.BACKEND_CORS_ORIGINS;

  if (!configuredOrigins) {
    return frontendOrigins.join(',');
  }

  const origins = configuredOrigins
    .split(',')
    .map((origin) => origin.trim())
    .filter(Boolean);

  for (const origin of frontendOrigins) {
    if (!origins.includes(origin)) {
      origins.push(origin);
    }
  }

  return origins.join(',');
}

function buildSharedEnv(frontendPort, backendPort) {
  return {
    ...process.env,
    DATABASE_URL: process.env.DATABASE_URL || getDevDatabaseUrl(),
    BACKEND_HOST: backendHost,
    BACKEND_PORT: String(backendPort),
    FRONTEND_HOST: frontendHost,
    FRONTEND_PORT: String(frontendPort),
    VITE_HOST: process.env.VITE_HOST || frontendHost,
    VITE_PORT: String(frontendPort),
    ...(process.env.VITE_API_BASE_URL ? {VITE_API_BASE_URL: process.env.VITE_API_BASE_URL} : {}),
    CORS_ORIGINS: mergeCorsOrigins(frontendPort),
  };
}

function prefixOutput(stream, name) {
  if (!stream) {
    return;
  }

  let buffer = '';
  stream.on('data', (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? '';

    for (const line of lines) {
      if (line.length > 0) {
        console.log(`[${name}] ${line}`);
      }
    }
  });

  stream.on('end', () => {
    if (buffer.length > 0) {
      console.log(`[${name}] ${buffer}`);
    }
  });
}

function startProcess(name, command, args, cwd, options = {}) {
  const child = spawn(command, args, {
    cwd,
    env: sharedEnv,
    stdio: [options.stdin ?? 'inherit', 'pipe', 'pipe'],
    windowsHide: true,
  });

  prefixOutput(child.stdout, name);
  prefixOutput(child.stderr, name);

  child.on('error', (error) => {
    console.error(`[${name}] Failed to start: ${error.message}`);
    if (options.restartOnExit && !shuttingDown) {
      scheduleProcessRestart(name, command, args, cwd, options);
      return;
    }

    exitCode = 1;
    void shutdown();
  });

  child.on('exit', (code, signal) => {
    children.delete(child);
    if (shuttingDown) {
      return;
    }

    if (options.restartOnExit) {
      console.error(
        `[${name}] Exited unexpectedly with ${signal ? `signal ${signal}` : `code ${code ?? 0}`}.`,
      );
      scheduleProcessRestart(name, command, args, cwd, options);
      return;
    }

    if (signal || code) {
      exitCode = code ?? 1;
      console.error(
        `[${name}] Exited unexpectedly with ${signal ? `signal ${signal}` : `code ${code}`}.`,
      );
      void shutdown();
    }
  });

  children.add(child);
  return child;
}

function scheduleProcessRestart(name, command, args, cwd, options) {
  const delay = options.restartDelayMs ?? 1000;
  console.error(`[${name}] Restarting in ${delay}ms...`);
  const timer = setTimeout(() => {
    restartTimers.delete(timer);
    if (!shuttingDown) {
      startProcess(name, command, args, cwd, options);
    }
  }, delay);
  restartTimers.add(timer);
}

function terminateProcess(child) {
  return new Promise((resolvePromise) => {
    if (!child.pid) {
      resolvePromise();
      return;
    }

    if (process.platform === 'win32') {
      const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
        stdio: 'ignore',
      });
      killer.on('exit', () => resolvePromise());
      killer.on('error', () => resolvePromise());
      return;
    }

    child.kill('SIGTERM');
    child.once('exit', () => resolvePromise());
  });
}

async function shutdown() {
  if (shuttingDown) {
    return;
  }

  shuttingDown = true;
  for (const timer of restartTimers) {
    clearTimeout(timer);
  }
  restartTimers.clear();
  await Promise.all(Array.from(children).map((child) => terminateProcess(child)));
  process.exit(exitCode);
}

function buildStartupCleanupEnv() {
  const existingExcludes = process.env.INFOFLOW_DEV_STOP_EXCLUDE_PIDS;
  const excludePids = [String(process.pid)];
  if (existingExcludes) {
    excludePids.push(existingExcludes);
  }

  return {
    ...process.env,
    INFOFLOW_DEV_STOP_EXCLUDE_PIDS: excludePids.join(','),
    INFOFLOW_DEV_STOP_FORCE_BACKEND_PORT: '1',
  };
}

async function runStartupCleanup() {
  console.log('[dev] Clearing existing frontend/backend dev processes...');
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(process.execPath, [devStopScript], {
      cwd: repoRoot,
      env: buildStartupCleanupEnv(),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });

    prefixOutput(child.stdout, 'cleanup');
    prefixOutput(child.stderr, 'cleanup');

    child.on('error', rejectPromise);
    child.on('exit', (code, signal) => {
      if (signal || code) {
        rejectPromise(
          new Error(
            `Startup cleanup exited with ${signal ? `signal ${signal}` : `code ${code}`}.`,
          ),
        );
        return;
      }

      resolvePromise();
    });
  });
}

function sleep(ms) {
  return new Promise((resolvePromise) => {
    setTimeout(resolvePromise, ms);
  });
}

async function waitForBackendHealth(backendUrl, timeoutMs = 30000) {
  const healthUrl = `${backendUrl}/api/health`;
  const deadline = Date.now() + timeoutMs;
  let lastError = 'no response';

  while (!shuttingDown && Date.now() < deadline) {
    try {
      const response = await fetch(healthUrl, {
        headers: {Accept: 'application/json'},
      });
      if (response.ok) {
        return;
      }
      lastError = `HTTP ${response.status} ${response.statusText}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }

    await sleep(500);
  }

  throw new Error(`Backend did not become healthy at ${healthUrl} within ${timeoutMs}ms (${lastError}).`);
}

process.on('SIGINT', () => {
  void shutdown();
});

process.on('SIGTERM', () => {
  void shutdown();
});

async function main() {
  await runStartupCleanup();

  const frontendPort = await chooseFrontendPort();
  const backendPort = await chooseBackendPort();
  const backendHealthTimeoutMs = parsePositiveInteger(
    process.env.BACKEND_HEALTH_TIMEOUT_MS,
    defaultBackendHealthTimeoutMs,
    'BACKEND_HEALTH_TIMEOUT_MS',
  );
  sharedEnv = buildSharedEnv(frontendPort, backendPort);
  const frontendUrl = `http://localhost:${frontendPort}`;
  const backendUrl = `http://localhost:${backendPort}`;

  console.log('[dev] Ensuring development PostgreSQL is running...');
  await ensureDevPostgres();

  console.log(`[dev] Starting backend on ${backendUrl}`);
  console.log(`[dev] Using PostgreSQL at ${sharedEnv.DATABASE_URL}`);
  console.log(
    `[dev] Frontend API base URL: ${
      sharedEnv.VITE_API_BASE_URL ?? `frontend default http://localhost:${backendPort}/api`
    }`,
  );
  console.log(`[dev] Backend CORS origins: ${sharedEnv.CORS_ORIGINS}`);
  console.log(`[dev] Backend health timeout: ${backendHealthTimeoutMs}ms`);
  console.log('[dev] Press Ctrl+C to stop frontend and backend. PostgreSQL container stays running.');

  startProcess('backend', process.execPath, [backendScript], repoRoot, {
    stdin: 'ignore',
  });

  console.log('[dev] Waiting for backend health check...');
  await waitForBackendHealth(backendUrl, backendHealthTimeoutMs);

  console.log(`[dev] Backend is healthy. Starting frontend on ${frontendUrl}`);
  startProcess(
    'frontend',
    process.platform === 'win32' ? 'cmd.exe' : 'npm',
    process.platform === 'win32' ? ['/c', 'npm run dev'] : ['run', 'dev'],
    frontendDir,
  );
}

main().catch((error) => {
  exitCode = 1;
  console.error(`[dev] ${error.message}`);
  void shutdown();
});
