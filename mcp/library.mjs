// Shared by the Worker and the isolated artokun process. Only library names cross this boundary.
export const workflowFields = {
  list: [], get: ['filename','format'], analyze: ['filename','view','section'],
  query: ['filename','types','title','where','ids','upstream_of','downstream_of','depth','fields','group_by','limit','max_chars'],
  strip: ['filename','format'], slice: ['filename','groups'],
};
export function libraryName(s) {
  return typeof s==='string' && s.length<=512 && s.toLowerCase().endsWith('.json') && !/[\\:%\x00-\x1f\x7f]/.test(s)
    && s.split('/').every(p=>p && p!=='.' && p!=='..' && !p.startsWith('~'));
}
export function libraryRequest(a) {
  if (!a || !Object.hasOwn(workflowFields,a.action)) return false;
  const fields=workflowFields[a.action];
  return Object.keys(a).every(k=>k==='action'||fields.includes(k))
    && (a.action==='list'||libraryName(a.filename))
    && (a.format===undefined || (a.action==='strip'?['api','raw']:['api','ui']).includes(a.format))
    && (a.action!=='slice' || !!a.groups?.length)
    && (a.view!=='detail' || !!a.section);
}
