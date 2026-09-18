// Owned DOM/fetch contract for the actual comparison script. No sockets/browser
// are opened. Native browser CI is separate evidence; this is not a live trial.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const nodes = new Map();
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.value = ''; this.classList = {add(){}, toggle(){}, contains(){return true;}}; }
  set id(v) { this._id = v; nodes.set(v, this); }
  get id() { return this._id; }
  set innerHTML(v) { assert.equal(v, '', 'untrusted text must never enter innerHTML'); this.children = []; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
}
for (const id of ['compare', 'cmpbtn']) { const e = new Element('div'); e.id = id; }
let posts = [], failReveal = true, infoFailure = false;
const cid = 'a'.repeat(32);
const response = {id: cid, phase: 'complete', answers: [
  {label: 'A', text: 'owned answer', eligible: true}, {label: 'B', text: 'failed', eligible: false}]};
const ctx = vm.createContext({
  document: {getElementById: id => nodes.get(id), createElement: tag => new Element(tag)},
  crypto: {randomUUID: () => 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'},
  session: 'owned-session', hdrs: () => ({}), encodeURIComponent,
  fetch: async (url, options) => {
    if (!options.method) return {json: async () => infoFailure
      ? {error: '<script>owned pool error</script>', code: 'pool_unavailable'}
      : {models: ['owned-a', 'owned-b'], recent: [], tally_rows: []}};
    const body = JSON.parse(options.body); posts.push(body);
    if (body.op === 'run') return {json: async () => response};
    if (body.op === 'reveal' && failReveal) { failReveal = false; throw Error('owned lost ack'); }
    return {json: async () => ({...response, phase: 'revealed', choice: 'A',
      mapping: {A: 'owned-a', B: 'owned-b'}, tally_rows: [{model: 'owned-a', identity: 'b'.repeat(64), count: 1}]})};
  }
});
vm.runInContext(fs.readFileSync(0, 'utf8'), ctx);
(async () => {
  await ctx.renderCompare();
  await ctx.runCompare('owned prompt');
  assert.equal(posts[0].id, cid);
  assert.equal(nodes.get('cmpid').value, cid);
  const cards = nodes.get('cmpresults').children.filter(e => e.className === 'cmpcard');
  assert.equal(cards[0].children[0].children.filter(e => e.tag === 'button').length, 1);
  assert.equal(cards[1].children[0].children.filter(e => e.tag === 'button').length, 0);
  await ctx.revealCompare('A');
  assert.match(nodes.get('cmpmessage').textContent, /unconfirmed/);
  await ctx.loadCompare(true);
  assert.deepEqual(posts.map(p => p.op), ['run', 'reveal', 'recover']);
  assert.match(nodes.get('cmptally').textContent, /owned-a .* 1/);
  infoFailure = true;
  await ctx.renderCompare();
  assert.equal(nodes.get('cmpmessage').textContent, '<script>owned pool error</script>');
  nodes.get('cmpid').value = cid;
  await ctx.loadCompare(true);
  assert.equal(posts.at(-1).op, 'recover');
  ctx.showComparison({id: cid, error: '<script>owned failure</script>'});
  assert.equal(nodes.get('cmpresults').children.length, 0);
  assert.equal(nodes.get('cmpmessage').textContent, '<script>owned failure</script>');
  process.stdout.write('OWNED_COMPARE_UI_PASSED\n');
})().catch(e => { process.stderr.write(e.stack + '\n'); process.exitCode = 1; });
