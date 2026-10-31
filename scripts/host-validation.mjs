// Portable project adapter. Only explicitly configured hosts use the shared slot.
import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

let [command, ...args] = process.argv.slice(2);
if (!command) throw new Error('Usage: node scripts/host-validation.mjs <command> [arguments...]');
if (command === '--shell') {
  if (args.length !== 1) throw new Error('--shell requires one existing script string');
  [command, args] = process.platform === 'win32'
    ? [process.env.ComSpec || 'cmd.exe', ['/d', '/s', '/c', args[0]]]
    : ['/bin/sh', ['-c', args[0]]];
}
const configPath = process.env.VALIDATION_SERIAL_CONFIG || join(homedir(), '.config/validation-serial/host.json');
if (process.env.VALIDATION_SERIAL_CONFIG || existsSync(configPath)) {
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  if (config.schema !== 1) throw new Error('Unsupported validation host configuration');
  args = [config.script, '--state-dir', config.state_dir, 'run', '--', command, ...args];
  command = config.python;
}
const child = spawn(command, args, { stdio: 'inherit', shell: process.platform === 'win32' });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.on('error', error => { console.error(error.message); process.exitCode = 1; });
child.on('exit', (code, signal) => {
  process.exitCode = code ?? (signal === 'SIGINT' ? 130 : signal === 'SIGTERM' ? 143 : 1);
});
