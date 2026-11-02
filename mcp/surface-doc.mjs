import policy from './surface.json' with {type:'json'};
import pkg from './package.json' with {type:'json'};
import {writeFile} from 'node:fs/promises';
const labels={direct:'上流を使用',split:'split で代替',deferred:'未提供'};
export const guide=[
  '# 上流と split のツール対応表','',
  'artokun/comfyui-mcp '+pkg.dependencies['comfyui-mcp']+' の全ツール（'+Object.keys(policy).length+'件）とそのアクションを示します。使い方は[チャットから ComfyUI を使う](split-mcp.md)を参照してください。','',
  '「上流を使用」は split の CPU で元の npm パッケージを呼びます。「split で代替」は右欄に記載した範囲の機能を提供します。「未提供」はこの HTTPS 接続では呼べません。ローカル stdio の機能は変わりません。','',
  '| 上流ツール | アクション | HTTPS での扱い | 使い方・理由 |',
  '|---|---|---|---|',
  ...Object.entries(policy).flatMap(([name,rows])=>rows.map(({actions,mode,note})=>
    '| `'+name+'` | '+actions.map(a=>a==='(call)'?'引数 action なし':'`'+a+'`').join('、')+' | '+labels[mode]+' | '+note+' |')),
  '',
].join('\n');
if(import.meta.main) await writeFile(new URL('../docs/split-mcp-tools.md',import.meta.url),guide);
