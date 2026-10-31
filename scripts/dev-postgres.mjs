import {spawn} from 'node:child_process';
import process from 'node:process';
import {access} from 'node:fs/promises';
import {constants as fsConstants} from 'node:fs';

export const DEV_POSTGRES = {
  containerName: 'infoflowaggregator-postgres-dev',
  volumeName: 'infoflowaggregator_postgres_data',
  image: 'postgres:16',
  hostPort: '55432',
  containerPort: '5432',
  user: 'postgres',
  password: 'postgres',
  database: 'infoflowaggregator',
};

const DEFAULT_WINDOWS_DOCKER_DESKTOP = 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe';

function isMainModule() {
  return process.argv[1]?.endsWith('dev-postgres.mjs') ?? false;
}

export function getDevDatabaseUrl() {
  return `postgresql+psycopg://${DEV_POSTGRES.user}:${DEV_POSTGRES.password}@localhost:${DEV_POSTGRES.hostPort}/${DEV_POSTGRES.database}`;
}

function createChild(command, args, options = {}) {
  return spawn(command, args, {
    cwd: options.cwd ?? process.cwd(),
    env: options.env ?? process.env,
    stdio: options.stdio ?? ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
}

async function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = createChild(command, args, options);
    let stdout = '';
    let stderr = '';

    child.stdout?.on('data', (chunk) => {
      stdout += chunk.toString();
      if (options.onStdout) {
        options.onStdout(chunk.toString());
      }
    });

    child.stderr?.on('data', (chunk) => {
      stderr += chunk.toString();
      if (options.onStderr) {
        options.onStderr(chunk.toString());
      }
    });

    child.on('error', reject);
    child.on('exit', (code) => {
      if (code === 0) {
        resolve({stdout, stderr});
        return;
      }

      const failure = new Error(
        `Command failed: ${command} ${args.join(' ')}\n${stderr || stdout}`.trim(),
      );
      failure.stdout = stdout;
      failure.stderr = stderr;
      failure.exitCode = code ?? 1;
      reject(failure);
    });
  });
}

async function fileExists(path) {
  try {
    await access(path, fsConstants.F_OK);
    return true;
  } catch {
    return false;
  }
}

async function ensureDockerEngine() {
  try {
    await runCommand('docker', ['info']);
    return;
  } catch (error) {
    if (process.platform !== 'win32') {
      throw new Error('Docker engine is not running. Please start Docker first.');
    }

    if (!(await fileExists(DEFAULT_WINDOWS_DOCKER_DESKTOP))) {
      throw new Error(
        'Docker engine is not running, and Docker Desktop was not found at the default path. Please start Docker Desktop first.',
      );
    }

    console.log('[db] Docker engine is not running. Starting Docker Desktop...');
    const desktop = createChild(DEFAULT_WINDOWS_DOCKER_DESKTOP, [], {stdio: 'ignore'});
    desktop.unref();

    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      await delay(3_000);
      try {
        await runCommand('docker', ['info']);
        console.log('[db] Docker Desktop is ready.');
        return;
      } catch {
        // Keep waiting.
      }
    }

    throw new Error('Docker Desktop did not become ready within 90 seconds. Please start it manually and retry.');
  }
}

async function ensureVolume() {
  await runCommand('docker', ['volume', 'create', DEV_POSTGRES.volumeName]);
}

async function inspectContainer() {
  try {
    const result = await runCommand('docker', [
      'inspect',
      '-f',
      '{{.State.Running}}',
      DEV_POSTGRES.containerName,
    ]);
    return result.stdout.trim();
  } catch {
    return null;
  }
}

async function ensureContainer() {
  const runningState = await inspectContainer();
  if (runningState === 'true') {
    console.log(`[db] PostgreSQL container '${DEV_POSTGRES.containerName}' is already running.`);
    return;
  }

  if (runningState === 'false') {
    console.log(`[db] Starting existing PostgreSQL container '${DEV_POSTGRES.containerName}'...`);
    await runCommand('docker', ['start', DEV_POSTGRES.containerName], {
      onStdout: (line) => line.trim() && console.log(`[db] ${line.trim()}`),
    });
    return;
  }

  console.log(`[db] Creating PostgreSQL container '${DEV_POSTGRES.containerName}'...`);
  await runCommand('docker', [
    'run',
    '-d',
    '--name',
    DEV_POSTGRES.containerName,
    '-e',
    `POSTGRES_USER=${DEV_POSTGRES.user}`,
    '-e',
    `POSTGRES_PASSWORD=${DEV_POSTGRES.password}`,
    '-e',
    `POSTGRES_DB=${DEV_POSTGRES.database}`,
    '-p',
    `${DEV_POSTGRES.hostPort}:${DEV_POSTGRES.containerPort}`,
    '-v',
    `${DEV_POSTGRES.volumeName}:/var/lib/postgresql/data`,
    DEV_POSTGRES.image,
  ]);
}

async function waitForPostgres() {
  console.log('[db] Waiting for PostgreSQL to accept connections...');
  const deadline = Date.now() + 60_000;

  while (Date.now() < deadline) {
    try {
      const result = await runCommand('docker', [
        'exec',
        DEV_POSTGRES.containerName,
        'pg_isready',
        '-U',
        DEV_POSTGRES.user,
        '-d',
        DEV_POSTGRES.database,
      ]);

      if (result.stdout.includes('accepting connections')) {
        console.log('[db] PostgreSQL is ready.');
        return;
      }
    } catch {
      // Keep retrying until timeout.
    }

    await delay(2_000);
  }

  throw new Error('PostgreSQL container started, but did not become ready within 60 seconds.');
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function ensureDevPostgres() {
  await ensureDockerEngine();
  await ensureVolume();
  await ensureContainer();
  await waitForPostgres();

  return {
    ...DEV_POSTGRES,
    databaseUrl: getDevDatabaseUrl(),
  };
}

if (isMainModule()) {
  ensureDevPostgres()
    .then((info) => {
      console.log(`[db] Dev database ready at ${info.databaseUrl}`);
    })
    .catch((error) => {
      console.error(`[db] ${error.message}`);
      process.exit(1);
    });
}
