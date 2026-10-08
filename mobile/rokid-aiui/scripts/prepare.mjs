#!/usr/bin/env node
import { chmod, cp, mkdir, readFile, realpath, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { validateConfig } from '../lib/assistant.js';

const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repo = path.resolve(source, '../..');
const args = process.argv.slice(2);
const option = (name) => { const index = args.indexOf(name); return index < 0 ? '' : args[index + 1]; };
const configFile = option('--config');
const outputArg = option('--out');
const isWithin = (root, value) => value === root || value.startsWith(root + path.sep);

try {
  if (!configFile || !outputArg) throw new Error('用法：node scripts/prepare.mjs --config /私人目录/rokid.json --out /私人目录/rokid-build');
  const inputPath = await realpath(path.resolve(configFile));
  if (isWithin(repo, inputPath)) throw new Error('密钥配置必须放在仓库外。');
  const config = JSON.parse(await readFile(inputPath, 'utf8'));
  const problem = validateConfig(config);
  if (problem) throw new Error(problem);
  const output = path.resolve(outputArg);
  const parent = await realpath(path.dirname(output));
  if (isWithin(repo, parent) || isWithin(source, output)) throw new Error('私有构建目录必须放在仓库外。');
  try {
    await stat(output);
    throw new Error('构建输出已存在，请指定一个新的空目录名。');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const safeConfig = {
    endpoint: config.endpoint,
    token: config.token,
    sessionId: config.sessionId || 'voice-memory',
    timeoutMs: config.timeoutMs || 120000,
  };
  if (typeof safeConfig.sessionId !== 'string' || safeConfig.sessionId.length > 128 ||
      typeof safeConfig.timeoutMs !== 'number' || !Number.isFinite(safeConfig.timeoutMs) ||
      safeConfig.timeoutMs < 1000 || safeConfig.timeoutMs > 180000) {
    throw new Error('sessionId 或 timeoutMs 配置不正确。');
  }
  await mkdir(output, { mode: 0o700 });
  for (const filename of ['LICENSE', 'NOTICE', 'AGENTS.md', 'app.json', 'app.js', '.aixignore', 'pages', 'lib']) {
    await cp(path.join(source, filename), path.join(output, filename), { recursive: true });
  }
  await writeFile(path.join(output, 'config.js'), `export default ${JSON.stringify(safeConfig, null, 2)};\n`, { mode: 0o600 });
  await chmod(output, 0o700);
  process.stdout.write(`已准备私有构建目录：${output}\n其中包含专用访问密钥，仅供主人私人调试。\n`);
} catch (error) {
  // Never print config values or arbitrary JSON parse errors containing source text.
  process.stderr.write(error instanceof SyntaxError ? '配置文件不是有效 JSON。\n' : `${error.message}\n`);
  process.exitCode = 1;
}
