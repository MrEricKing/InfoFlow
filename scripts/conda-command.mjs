import { existsSync, realpathSync } from 'node:fs';
import { basename, delimiter, dirname, join } from 'node:path';
import process from 'node:process';

const defaultProjectCondaEnvName = 'infoflowaggregator';

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function pathCandidates() {
  const pathValue = process.env.PATH || process.env.Path || '';
  const pathDirs = pathValue.split(delimiter).filter(Boolean);
  const executableNames =
    process.platform === 'win32' ? ['conda.exe', 'conda.bat', 'conda.cmd'] : ['conda'];

  return pathDirs.flatMap((dir) => executableNames.map((name) => join(dir, name)));
}

function homeDir() {
  return process.env.HOME || process.env.USERPROFILE || null;
}

function condaRootFromExecutable(executablePath) {
  if (!executablePath || executablePath === 'conda' || !existsSync(executablePath)) {
    return null;
  }

  let resolvedPath = executablePath;
  try {
    resolvedPath = realpathSync(executablePath);
  } catch {
    // Fall back to the original path.
  }

  const parent = dirname(resolvedPath);
  const parentName = basename(parent).toLowerCase();
  if (['bin', 'condabin', 'scripts'].includes(parentName)) {
    return dirname(parent);
  }

  return dirname(dirname(resolvedPath));
}

function windowsDefaultCandidates() {
  if (process.platform !== 'win32') {
    return [];
  }

  const roots = unique([
    process.env.CONDA_PREFIX,
    process.env.CONDA_ROOT,
    process.env.MINICONDA_ROOT,
    process.env.ANACONDA_ROOT,
    process.env.USERPROFILE ? join(process.env.USERPROFILE, 'anaconda3') : null,
    process.env.USERPROFILE ? join(process.env.USERPROFILE, 'miniconda3') : null,
    'C:\\ProgramData\\anaconda3',
    'C:\\ProgramData\\miniconda3',
  ]);

  return roots.flatMap((root) => [
    join(root, 'Scripts', 'conda.exe'),
    join(root, 'condabin', 'conda.bat'),
    join(root, 'condabin', 'conda.cmd'),
  ]);
}

function condaRootCandidates() {
  const condaExeRoot = process.env.CONDA_EXE ? dirname(dirname(process.env.CONDA_EXE)) : null;
  const condaBatRoot = process.env.CONDA_BAT ? dirname(dirname(process.env.CONDA_BAT)) : null;
  const pathCondaRoots = pathCandidates().map(condaRootFromExecutable);
  const activePrefix = process.env.CONDA_PREFIX;
  const activePrefixRoot =
    activePrefix && basename(dirname(activePrefix)).toLowerCase() === 'envs'
      ? dirname(dirname(activePrefix))
      : null;
  const userHome = homeDir();

  return unique([
    process.env.CONDA_ROOT,
    condaExeRoot,
    condaBatRoot,
    ...pathCondaRoots,
    activePrefixRoot,
    process.env.MINICONDA_ROOT,
    process.env.ANACONDA_ROOT,
    userHome ? join(userHome, 'anaconda3') : null,
    userHome ? join(userHome, 'miniconda3') : null,
    userHome ? join(userHome, 'miniforge3') : null,
    '/opt/homebrew/Caskroom/miniforge/base',
    '/opt/homebrew/Caskroom/miniconda/base',
    '/opt/homebrew/Caskroom/anaconda/base',
    '/opt/homebrew/anaconda3',
    '/opt/homebrew/miniconda3',
    '/opt/homebrew/miniforge3',
    'C:\\ProgramData\\anaconda3',
    'C:\\ProgramData\\miniconda3',
  ]);
}

function condaEnvDirs() {
  const configuredEnvDirs = (process.env.CONDA_ENVS_PATH ?? '')
    .split(delimiter)
    .map((entry) => entry.trim())
    .filter(Boolean);
  const userHome = homeDir();

  return unique([
    ...configuredEnvDirs,
    ...condaRootCandidates().map((root) => join(root, 'envs')),
    userHome ? join(userHome, '.conda', 'envs') : null,
    process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, 'conda', 'conda', 'envs') : null,
  ]);
}

function pythonPathForPrefix(prefix) {
  return process.platform === 'win32' ? join(prefix, 'python.exe') : join(prefix, 'bin', 'python');
}

function condaEnvPrefixCandidates(envName) {
  const activePrefix = process.env.CONDA_PREFIX;
  const activePrefixMatches =
    activePrefix && basename(activePrefix).toLowerCase() === envName.toLowerCase()
      ? activePrefix
      : null;

  return unique([activePrefixMatches, ...condaEnvDirs().map((envDir) => join(envDir, envName))]);
}

function findPathKey(env) {
  return Object.keys(env).find((key) => key.toLowerCase() === 'path') ?? 'PATH';
}

function directCondaPythonEnv(prefix, envName, baseEnv = process.env) {
  const pathKey = findPathKey(baseEnv);
  const envPathDirs =
    process.platform === 'win32'
      ? [
          prefix,
          join(prefix, 'Library', 'mingw-w64', 'bin'),
          join(prefix, 'Library', 'usr', 'bin'),
          join(prefix, 'Library', 'bin'),
          join(prefix, 'Scripts'),
          join(prefix, 'bin'),
        ]
      : [join(prefix, 'bin')];
  const existingPath = baseEnv[pathKey] ?? '';
  const pathValue = unique([...envPathDirs, ...existingPath.split(delimiter)]).join(delimiter);

  return {
    ...baseEnv,
    CONDA_DEFAULT_ENV: envName,
    CONDA_PREFIX: prefix,
    [pathKey]: pathValue,
  };
}

export function resolveCondaEnvPython(envName = defaultProjectCondaEnvName) {
  for (const prefix of condaEnvPrefixCandidates(envName)) {
    const pythonPath = pythonPathForPrefix(prefix);
    if (existsSync(pythonPath)) {
      return { envName, prefix, pythonPath };
    }
  }

  return null;
}

export function resolveCondaCommand() {
  const candidates = unique([
    process.env.CONDA_EXE,
    ...pathCandidates(),
    ...windowsDefaultCandidates(),
    'conda',
  ]);

  for (const candidate of candidates) {
    if (candidate === 'conda' || existsSync(candidate)) {
      return candidate;
    }
  }

  return 'conda';
}

export function buildCondaSpawn(args) {
  const command = resolveCondaCommand();

  if (process.platform === 'win32' && /\.(bat|cmd)$/i.test(command)) {
    return {
      command: 'cmd.exe',
      args: ['/d', '/s', '/c', command, ...args],
    };
  }

  return { command, args };
}

export function buildCondaPythonSpawn(args, options = {}) {
  const envName = options.envName ?? defaultProjectCondaEnvName;
  const env = options.env ?? process.env;
  const resolved = resolveCondaEnvPython(envName);

  if (resolved) {
    return {
      command: resolved.pythonPath,
      args,
      env: directCondaPythonEnv(resolved.prefix, resolved.envName, env),
      mode: 'direct-python',
      description: `${resolved.pythonPath} (${resolved.envName})`,
    };
  }

  const condaSpawn = buildCondaSpawn([
    'run',
    '--no-capture-output',
    '-n',
    envName,
    'python',
    ...args,
  ]);
  return {
    ...condaSpawn,
    env,
    mode: 'conda-run',
    description: `conda run -n ${envName} python`,
  };
}
