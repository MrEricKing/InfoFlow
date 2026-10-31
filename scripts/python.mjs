import {spawn} from 'node:child_process';
import {resolve} from 'node:path';
import process from 'node:process';

import {buildCondaPythonSpawn} from './conda-command.mjs';

const projectCondaEnvName = 'infoflowaggregator';
let args = process.argv.slice(2);
let cwd = process.cwd();

if (args[0] === '--cwd') {
  if (!args[1]) {
    console.error('[py] --cwd requires a directory.');
    process.exit(1);
  }

  cwd = resolve(process.cwd(), args[1]);
  args = args.slice(2);
}

if (args.length === 0) {
  console.error('[py] Usage: node ./scripts/python.mjs <python args...>');
  process.exit(1);
}

const pythonCommand = buildCondaPythonSpawn(args, {envName: projectCondaEnvName});
console.error(`[py] Python runner: ${pythonCommand.description}`);

const child = spawn(pythonCommand.command, pythonCommand.args, {
  cwd,
  env: pythonCommand.env,
  stdio: 'inherit',
  windowsHide: true,
});

function signalExitCode(signal) {
  if (signal === 'SIGINT') {
    return 130;
  }
  if (signal === 'SIGTERM') {
    return 143;
  }
  return 1;
}

function forwardSignal(signal) {
  if (!child.killed) {
    child.kill(signal);
  }
}

process.on('SIGINT', () => forwardSignal('SIGINT'));
process.on('SIGTERM', () => forwardSignal('SIGTERM'));

child.on('error', (error) => {
  console.error(`[py] Failed to start Python: ${error.message}`);
  process.exit(1);
});

child.on('exit', (code, signal) => {
  if (signal) {
    process.exit(signalExitCode(signal));
  }

  process.exit(code ?? 0);
});
