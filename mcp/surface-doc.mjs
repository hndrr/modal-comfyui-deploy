import policy from './surface.json' with {type:'json'};
import pkg from './package.json' with {type:'json'};
import {writeFile} from 'node:fs/promises';
const labels={direct:'上流を使用',split:'split で代替',deferred:'未提供'};
export const guide=[
  '# 上流と split のツール対応表','',
  'artokun/comfyui-mcp '+pkg.dependencies['comfyui-mcp']+' の全ツール（'+Object.keys(policy).length+'件）とそのアクションを示します。実際に接続先で使えるツール・引数・実行先は[リモート MCP のツール一覧](tools.md)、操作の流れは[利用ガイド](usage.md)を参照してください。','',
  '「上流を使用」は split の CPU で元の npm パッケージを呼びます。「split で代替」は右欄に記載した範囲の機能を提供します。「未提供」はこの HTTPS 接続では呼べません。クライアントは HTTPS 接続に統一しています。CPU 内で呼ぶ上流パッケージは残します。','',
  '| 上流ツール | アクション | HTTPS での扱い | 使い方・理由 |',
  '|---|---|---|---|',
  ...Object.entries(policy).flatMap(([name,rows])=>rows.map(({actions,mode,note})=>
    '| `'+name+'` | '+actions.map(a=>a==='(call)'?'引数 action なし':'`'+a+'`').join('、')+' | '+labels[mode]+' | '+note+' |')),
  '',
].join('\n');
if(import.meta.main) await writeFile(new URL('../docs/mcp/upstream.md',import.meta.url),guide);
