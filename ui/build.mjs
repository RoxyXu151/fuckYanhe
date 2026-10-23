import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
mkdirSync('../src/yanhe_sentinel/web/static/ui',{recursive:true});
await build({entryPoints:['src/main.tsx'],bundle:true,minify:true,format:'iife',jsx:'automatic',outfile:'../src/yanhe_sentinel/web/static/ui/app.js',legalComments:'external',define:{'process.env.NODE_ENV':'"production"'}});
execFileSync(process.execPath,['node_modules/tailwindcss/lib/cli.js','-c','tailwind.config.cjs','-i','src/styles.css','-o','../src/yanhe_sentinel/web/static/ui/style.css','--minify'],{stdio:'inherit'});
