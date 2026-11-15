// Prepares the on-device "Hey NOVA" wake-word engine for the extension.
//
//   cd extension && npm install && npm run setup
//
// 1. Builds an extension-safe copy of vosk-browser (Vosk speech recognition compiled to WASM):
//    - vosk-browser starts its Web Worker from a blob: URL, which Chrome's extension CSP forbids,
//      so the worker is written to its own file and loaded from the extension package instead.
//    - The worker's embind glue names functions with `new Function(...)`, which the extension CSP
//      also forbids (no 'unsafe-eval'); it is replaced with an equivalent closure.
// 2. Downloads the small English model (~41 MB) into extension/models/ (skipped if present).
import fs from 'node:fs';
import https from 'node:https';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const EXT = path.join(ROOT, 'extension');
const SRC = path.join(EXT, 'node_modules', 'vosk-browser', 'dist', 'vosk.js');
const VENDOR = path.join(EXT, 'vendor', 'vosk');
const MODEL_NAME = 'vosk-model-small-en-us-0.15.tar.gz';
const MODEL_URL = `https://ccoreilly.github.io/vosk-browser/models/${MODEL_NAME}`;
const MODEL_DEST = path.join(EXT, 'models', MODEL_NAME);
const MODEL_BYTES = 41184862;

function fail(msg) {
  console.error(`✗ ${msg}`);
  process.exit(1);
}

function buildVendor() {
  if (!fs.existsSync(SRC)) fail(`${SRC} not found. Run "npm install" in extension/ first.`);
  const lib = fs.readFileSync(SRC, 'utf8');

  const factory = /var WorkerFactory = createBase64WorkerFactory\('([A-Za-z0-9+/=]+)'[^;]*\);/;
  const m = lib.match(factory);
  if (!m) fail('Could not find the embedded worker in vosk.js (unexpected vosk-browser version).');

  let worker = Buffer.from(m[1], 'base64').toString('latin1');
  worker = worker.slice(worker.indexOf('\n', 10) + 1); // same as vosk-browser: drop the loader comment line

  const namedFn = /function createNamedFunction\(name,body\)\{name=makeLegalFunctionName\(name\);return new Function\([\s\S]*?\)\(body\)\}/;
  if (!namedFn.test(worker)) fail('Could not find createNamedFunction in the Vosk worker.');
  worker = worker.replace(
    namedFn,
    'function createNamedFunction(name,body){name=makeLegalFunctionName(name);var f=function(){"use strict";return body.apply(this,arguments)};try{Object.defineProperty(f,"name",{value:name})}catch(e){}return f}',
  );
  // embind builds its C++ call wrappers as source text and runs them with Function(); these are
  // the closure-based equivalents Emscripten itself uses when built with DYNAMIC_EXECUTION=0.
  const invokerStart = worker.indexOf('function craftInvokerFunction(');
  const invokerEnd = worker.indexOf('return invokerFunction}', invokerStart);
  if (invokerStart < 0 || invokerEnd < 0) fail('Could not find craftInvokerFunction in the Vosk worker.');
  worker =
    worker.slice(0, invokerStart) +
    `function craftInvokerFunction(humanName,argTypes,classType,cppInvokerFunc,cppTargetFunc){var argCount=argTypes.length;if(argCount<2){throwBindingError("argTypes array size mismatch! Must at least get return value and 'this' types!");}var isClassMethodFunc=argTypes[1]!==null&&classType!==null;var needsDestructorStack=false;for(var i=1;i<argTypes.length;++i){if(argTypes[i]!==null&&argTypes[i].destructorFunction===undefined){needsDestructorStack=true;break}}var returns=argTypes[0].name!=="void";var expected=argCount-2;var invokerFunction=function(){if(arguments.length!==expected){throwBindingError("function "+humanName+" called with "+arguments.length+" arguments, expected "+expected+" args!");}var destructors=needsDestructorStack?[]:null;var thisWired;var wired=new Array(expected);var callArgs=[cppTargetFunc];if(isClassMethodFunc){thisWired=argTypes[1].toWireType(destructors,this);callArgs.push(thisWired);}for(var i=0;i<expected;++i){wired[i]=argTypes[i+2].toWireType(destructors,arguments[i]);callArgs.push(wired[i]);}var rv=cppInvokerFunc.apply(null,callArgs);if(needsDestructorStack){runDestructors(destructors);}else{for(var i=isClassMethodFunc?1:2;i<argTypes.length;++i){var param=i===1?thisWired:wired[i-2];if(argTypes[i].destructorFunction!==null){argTypes[i].destructorFunction(param);}}}if(returns){return argTypes[0].fromWireType(rv)}};` +
    worker.slice(invokerEnd);

  const callerStart = worker.indexOf('var params=["retType"];var args=[retType];');
  const callerEndMarker = 'var invokerFunction=new_(Function,params).apply(null,args);';
  const callerEnd = worker.indexOf(callerEndMarker, callerStart);
  if (callerStart < 0 || callerEnd < 0) fail('Could not find __emval_get_method_caller in the Vosk worker.');
  worker =
    worker.slice(0, callerStart) +
    'var invokerFunction=function(handle,name,destructors,argsPtr){var offset=0;var a=new Array(argCount-1);for(var i=0;i<argCount-1;++i){a[i]=types[1+i].readValueFromPointer(argsPtr+offset);offset+=types[i+1]["argPackAdvance"];}var rv=handle[name].apply(handle,a);for(var i=0;i<argCount-1;++i){if(types[i+1]["deleteObject"]){types[i+1].deleteObject(a[i]);}}if(!retType.isVoid){return retType.toWireType(destructors,rv)}};' +
    worker.slice(callerEnd + callerEndMarker.length);

  if (/new Function|new_\(Function|[^\w.$]Function\(|\beval\(|createObjectURL/.test(worker)) fail('The Vosk worker still uses a CSP-forbidden API.');

  const patchedLib = lib.replace(
    factory,
    'var WorkerFactory = function (options) { return new Worker(globalThis.VOSK_WORKER_URL, options); };',
  );
  fs.mkdirSync(VENDOR, { recursive: true });
  fs.writeFileSync(path.join(VENDOR, 'vosk.js'), `// Generated by scripts/setup_wake_word.mjs from vosk-browser (Apache-2.0). Do not edit.\n${patchedLib}`);
  fs.writeFileSync(path.join(VENDOR, 'vosk-worker.js'), `// Generated by scripts/setup_wake_word.mjs from vosk-browser (Apache-2.0). Do not edit.\n${worker}`);
  console.log(`✓ vendor/vosk/vosk.js (${(patchedLib.length / 1024).toFixed(0)} KB) and vosk-worker.js (${(worker.length / 1048576).toFixed(1)} MB)`);
}

function download(url, dest, redirects = 5) {
  return new Promise((resolve, reject) => {
    https
      .get(url, (res) => {
        if ([301, 302, 307, 308].includes(res.statusCode) && res.headers.location && redirects > 0) {
          res.resume();
          return resolve(download(new URL(res.headers.location, url).href, dest, redirects - 1));
        }
        if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
        const tmp = `${dest}.part`;
        const out = fs.createWriteStream(tmp);
        res.pipe(out);
        out.on('finish', () => out.close(() => (fs.renameSync(tmp, dest), resolve())));
        out.on('error', reject);
      })
      .on('error', reject);
  });
}

async function ensureModel() {
  if (fs.existsSync(MODEL_DEST) && fs.statSync(MODEL_DEST).size === MODEL_BYTES) {
    console.log(`✓ models/${MODEL_NAME} already present`);
    return;
  }
  fs.mkdirSync(path.dirname(MODEL_DEST), { recursive: true });
  console.log(`… downloading ${MODEL_URL} (~41 MB)`);
  await download(MODEL_URL, MODEL_DEST);
  const size = fs.statSync(MODEL_DEST).size;
  if (size !== MODEL_BYTES) fail(`Model download is ${size} bytes, expected ${MODEL_BYTES}. Delete it and re-run.`);
  console.log(`✓ models/${MODEL_NAME}`);
}

buildVendor();
await ensureModel();
