'use strict';
/*
 * Muse Bridge headless agent for ${PC_NAME}.
 *
 * Replaces the VS Code extension entry (src/extension.js): builds the same
 * MuseBridgeClient options object, but reads settings from a JSON file and
 * the token from a local file, logs to a file, and records status as JSON.
 * No VS Code API is used anywhere below this file.
 */
const fs = require('fs');
const path = require('path');
const os = require('os');
const dns = require('dns');
const net = require('net');
const { MuseBridgeClient } = require('./src/muse/muse-bridge-client');

const HOME = os.homedir();
const BASE = path.join(HOME, '.muse-bridge');
const CONFIG_PATH = path.join(BASE, 'config.json');
const LOG_PATH = path.join(BASE, 'agent.log');
const STATUS_PATH = path.join(BASE, 'status.json');

function logLine(stream, msg) {
  const line = `${new Date().toISOString()} [${stream}] ${msg}`;
  try { fs.appendFileSync(LOG_PATH, line + '\n'); } catch (_) {}
  if (stream === 'error') console.error(line); else console.log(line);
}
const logger = {
  info: (m) => logLine('info', String(m)),
  error: (m) => logLine('error', String(m)),
};

function writeStatus(state, info) {
  try {
    fs.writeFileSync(STATUS_PATH, JSON.stringify({
      state,
      at: new Date().toISOString(),
      details: (info && info.details) || null,
      pid: process.pid,
      host: os.hostname(),
    }, null, 2));
  } catch (_) {}
}

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
  const cfg = JSON.parse(raw);
  if (!cfg.vpsHost) throw new Error('config: vpsHost missing');
  return cfg;
}

async function main() {
  try {
    const st = fs.statSync(LOG_PATH);
    if (st.size > 5 * 1024 * 1024) fs.renameSync(LOG_PATH, LOG_PATH + '.1');
  } catch (_) {}

  const cfg = loadConfig();

  // The bridge entry is addressed by its SNI name (${VPS_SNI}) so the
  // TLS SNI and Host header match the nginx vhost. The name is private and
  // has no public DNS, and editing the hosts file would need admin rights,
  // so resolve it inside this process only: dns.lookup is what net/tls and
  // the ws client use when connecting.
  if (cfg.vpsIp && cfg.vpsHost && !net.isIP(cfg.vpsHost)) {
    const origLookup = dns.lookup.bind(dns);
    dns.lookup = (hostname, options, callback) => {
      if (typeof options === 'function') { callback = options; options = {}; }
      if (hostname === cfg.vpsHost) {
        if (options && options.all) {
          return process.nextTick(callback, null, [{ address: cfg.vpsIp, family: 4 }]);
        }
        return process.nextTick(callback, null, cfg.vpsIp, 4);
      }
      return origLookup(hostname, options, callback);
    };
    logger.info(`[agent] in-process DNS: ${cfg.vpsHost} -> ${cfg.vpsIp}`);
  }

  const tokenFile = cfg.tokenFile || path.join(HOME, '.Muse', 'token');
  let token = '';
  try { token = fs.readFileSync(tokenFile, 'utf8').trim(); } catch (_) {}
  if (!token) {
    logger.error(`[agent] token file missing or empty: ${tokenFile}`);
    writeStatus('error', { details: 'token missing' });
    process.exit(2);
  }

  const client = new MuseBridgeClient({
    vpsHost: cfg.vpsHost,
    vpsPort: cfg.vpsPort || 443,
    token,
    sni: cfg.sni || '${VPS_SNI}',
    caCertPath: cfg.caCertPath || '',
    readOnly: cfg.readOnly === true,
    clashSelfHeal: false,
    gitBashPath: cfg.gitBashPath || '',
    poolSize: cfg.poolSize || 16,
    maxPoolSize: cfg.maxPoolSize || 32,
    shellPool: cfg.shellPool === true,
    workspaceRoot: cfg.workspaceRoot || process.cwd(),
    localProxyUrl: cfg.proxyUrl || '',
    onStatusChange: (state, info) => {
      writeStatus(state, info);
      logger.info(`[agent] status -> ${state}${info && info.details ? ' (' + info.details + ')' : ''}`);
    },
    logger,
    vscode: null,
    getLspDiagnostics: null,
  });

  const shutdown = (sig) => {
    logger.info(`[agent] ${sig} received, stopping`);
    try { client.stop(); } catch (_) {}
    writeStatus('stopped', { details: sig });
    setTimeout(() => process.exit(0), 300).unref();
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('uncaughtException', (err) => {
    logger.error(`[agent] uncaughtException: ${err && err.stack || err}`);
  });

  writeStatus('starting', null);
  logger.info(`[agent] starting (host=${os.hostname()}, node=${process.version}, vps=${cfg.vpsHost}:${cfg.vpsPort || 443}, sni=${cfg.sni || '${VPS_SNI}'}, shellPool=${cfg.shellPool === true})`);
  await client.start();
  logger.info('[agent] client.start() returned');
  // Keep the event loop alive even if every client timer is unref'd.
  setInterval(() => {}, 60 * 1000);
}

main().catch((err) => {
  logger.error(`[agent] fatal: ${err && err.stack || err}`);
  writeStatus('error', { details: String(err && err.message || err) });
  process.exit(1);
});
