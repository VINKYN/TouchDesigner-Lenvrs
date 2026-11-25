import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const ROOT_DIR = path.resolve(__dirname, '..', '..');
const SOURCE_TOE = path.join(ROOT_DIR, 'je ne sais pas pour qui je me prend.18.toe');
const OUTPUT_TOE = path.join(__dirname, 'project_live.toe');
const TD_BIN = 'C:\\Program Files\\Derivative\\TouchDesigner\\bin';
const TOE_EXPAND = path.join(TD_BIN, 'toeexpand.exe');
const TOE_COLLAPSE = path.join(TD_BIN, 'toecollapse.exe');

const force = process.argv.includes('--force');

if (!fs.existsSync(SOURCE_TOE)) {
  console.error(`Fichier .toe source introuvable : ${SOURCE_TOE}`);
  process.exit(1);
}

if (!force && fs.existsSync(OUTPUT_TOE)) {
  const srcTime = fs.statSync(SOURCE_TOE).mtimeMs;
  const outTime = fs.statSync(OUTPUT_TOE).mtimeMs;
  if (outTime >= srcTime) {
    console.log('[build-live-toe] project_live.toe est déjà à jour.');
    process.exit(0);
  }
}

console.log('[build-live-toe] Construction de project_live.toe depuis ton vrai fichier .toe...');

const workDir = path.join(__dirname, '_work_toe');
fs.rmSync(workDir, { recursive: true, force: true });
fs.mkdirSync(workDir, { recursive: true });

const tempToe = path.join(workDir, 'project.toe');
fs.copyFileSync(SOURCE_TOE, tempToe);

function runTdTool(exe, args, cwd, verifyPath) {
  try {
    execFileSync(exe, args, { stdio: 'inherit', cwd });
  } catch (err) {
    // toeexpand.exe / toecollapse.exe peuvent retourner exit code 1 même en cas de succès
    if (!fs.existsSync(verifyPath)) {
      throw err;
    }
  }
}

const toeDir = path.join(workDir, 'project.toe.dir');
const tocFile = path.join(workDir, 'project.toe.toc');

// 1) Décompression officielle avec toeexpand.exe
runTdTool(TOE_EXPAND, [tempToe], workDir, tocFile);

// 2) Injection d'un unique Execute DAT minimal dans /project1/web_bridge_boot
// qui charge web-app/td/td_bridge.py au démarrage de TouchDesigner
const tdDirLiteral = __dirname.replace(/\\/g, '\\\\');
const bootPy = `# Lance automatiquement le pont Web Bridge (web-app/td/td_bridge.py)
import os
import sys

_BRIDGE_DIR = "${tdDirLiteral}"

def _start_bridge():
\tif _BRIDGE_DIR not in sys.path:
\t\tsys.path.insert(0, _BRIDGE_DIR)
\timport importlib
\timport td_bridge
\timportlib.reload(td_bridge)
\ttd_bridge.boot()

def onStart():
\ttry:
\t\twith open(os.path.join(_BRIDGE_DIR, "bridge.log"), "a", encoding="utf-8") as f:
\t\t\tf.write("[boot] onStart() declenche\\n")
\texcept Exception:
\t\tpass
\trun("op('/project1/web_bridge_boot').module._start_bridge()", delayFrames=10)
\treturn

def onCreate():
\treturn
`;

function encodeTDTextDat(str) {
  const body = Buffer.from(str, 'utf8');
  const header = Buffer.from(
    '320a2a0000000100000001000000010000000100000002',
    'hex'
  );
  const lenBuf = Buffer.alloc(4);
  lenBuf.writeUInt32BE(body.length, 0);
  return Buffer.concat([header, lenBuf, body]);
}

const nodeName = 'web_bridge_boot';
const nContent = `DAT:execute
tile 3650 -50 130 90
flags =  viewer 1 parlanguage 0
color 0.25 0.65 0.55 
view -1 8 0 1 1 1 0 0 0 0 1 1 0
end
`;

const parmContent = `?
start 0 on
language 0 python
?
`;

const project1Dir = path.join(toeDir, 'project1');
fs.writeFileSync(path.join(project1Dir, `${nodeName}.n`), nContent, 'utf8');
fs.writeFileSync(path.join(project1Dir, `${nodeName}.parm`), parmContent, 'utf8');
fs.writeFileSync(path.join(project1Dir, `${nodeName}.text`), encodeTDTextDat(bootPy));

// Insère les 3 fichiers dans le manifeste .toc AVANT .application
const tocLines = fs.readFileSync(tocFile, 'utf8').split(/\r?\n/);
const entries = [
  `project1/${nodeName}.n`,
  `project1/${nodeName}.parm`,
  `project1/${nodeName}.text`,
];
const appIdx = tocLines.findIndex((l) => l.trim() === '.application');
const filtered = tocLines.filter((l) => !entries.includes(l.trim()));
if (appIdx >= 0) {
  const insertAt = filtered.findIndex((l) => l.trim() === '.application');
  filtered.splice(insertAt, 0, ...entries);
} else {
  filtered.push(...entries);
}
fs.writeFileSync(tocFile, filtered.join('\n'), 'utf8');

// Supprime le tempToe d'entrée avant de recompiler
fs.unlinkSync(tempToe);

// 3) Recompression officielle avec toecollapse.exe
runTdTool(TOE_COLLAPSE, [toeDir], workDir, tempToe);

if (!fs.existsSync(tempToe)) {
  throw new Error('toecollapse.exe n\'a pas généré project.toe');
}

fs.copyFileSync(tempToe, OUTPUT_TOE);
fs.rmSync(workDir, { recursive: true, force: true });

console.log(`[build-live-toe] Succès ! Généré : ${OUTPUT_TOE}`);
