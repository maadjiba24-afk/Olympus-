// Actual inline gallery script, owned DOM/fetch only. No sockets/providers.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const nodes = new Map();
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.value = ''; this.disabled = false;
    const classes = new Set(['open']);
    this.classList = {toggle(v){classes.has(v)?classes.delete(v):classes.add(v)}, contains(v){return classes.has(v)}}; }
  setAttribute() {}
  set innerHTML(v) { assert.equal(v, '', 'Never insert untrusted HTML'); this.children = []; }
  append(...v) { this.children.push(...v); }
  appendChild(v) { this.append(v); return v; }
}
nodes.set('gallery', new Element('div')); nodes.set('galbtn', new Element('button'));
const image = {name:'owned.png',id:'c'.repeat(32),revision:'d'.repeat(64),bytes:10};
let posts=[], requests=[], mode='list', resolvePost, promptCount=0, accepted=true, promptValue='owned edit', lookupCode=400, timerCallback;
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const ctx=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:t=>new Element(t)},
  crypto:{randomUUID:()=> 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'},
  session:'owned-session',hdrs:()=>({}),encodeURIComponent,AbortController,
  setTimeout:(callback,delay)=>{assert.equal(delay,120000);timerCallback=callback;return 1},clearTimeout:()=>{timerCallback=null},
  window:{confirm:()=>accepted,prompt:()=>{promptCount++; return promptValue},open(){}},
  fetch:async(url,opts)=>{
    if(opts.method){const body=JSON.parse(opts.body);posts.push(body);
      assert.equal(nodes.get('gallery').children[1].value,body.operation_id,'ID visible before fetch');
      if(mode==='delayed') return new Promise(resolve=>{resolvePost=resolve});
      if(mode==='lost') throw Error('owned lost ack');
      if(mode==='conflict') return reply({error:'stale revision'},409);
      if(mode==='missing') return reply({error:'Operation not found'},404);
      if(mode==='failed') return reply({status:'failed',operation_id:body.operation_id,error:{message:'Provider output rejected'}},502);
      if(mode==='lookup-error') return reply({error:'Lookup refused'},lookupCode);
      if(mode==='wrong-receipt') return reply({status:'complete',operation_id:'f'.repeat(32)});
      if(mode==='hung') return new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>{const error=Error('aborted');error.name='AbortError';reject(error)}));
      return reply({status:body.op==='recover'?'indeterminate':'complete',operation_id:body.operation_id},body.op==='recover'?202:200);
    }
    if(mode==='outoforder') return new Promise(resolve=>requests.push(resolve));
    if(mode==='error') return reply({error:'<script>unavailable</script>'},503);
    return reply({images:[image],unclaimed:false});
  }});
vm.runInContext(fs.readFileSync(0,'utf8'),ctx);
const gallery=nodes.get('gallery'), message=gallery.children[0], recoveryID=gallery.children[1], recover=gallery.children[2], grid=gallery.children[3];
const buttons=()=>grid.children[0].children[2].children;
(async()=>{
 await ctx.renderGallery(); assert.equal(grid.children.length,1);
 assert.match(grid.children[0].children[0].src,/expected_revision=/);
 grid.children[0].children[0].onerror();assert.match(grid.children[0].children[0].alt,/unavailable/);
 mode='error'; await ctx.renderGallery(); assert.match(message.textContent,/unavailable/);assert.equal(grid.children.length,1);
 mode='outoforder';const old=ctx.renderGallery(), fresh=ctx.renderGallery();
 requests[1](reply({images:[{...image,name:'new.png'}]}));await fresh;
 requests[0](reply({images:[]}));await old;assert.equal(grid.children[0].children[1].children[0].textContent,'new.png');
 // Closing invalidates an outstanding response; reopening begins a fresh load.
 requests=[];const closing=ctx.renderGallery();nodes.get('galbtn').onclick();
 requests[0](reply({images:[]}));await closing;assert.equal(grid.children.length,1);
 mode='list';nodes.get('galbtn').onclick();await new Promise(setImmediate);
 mode='delayed';const first=ctx.editImage(image); const second=ctx.editImage(image);await second;
 assert.equal(posts.length,1);assert.equal(promptCount,1);assert.equal(buttons()[0].disabled,true);
 assert.equal(posts[0].expected_id,image.id);assert.equal(posts[0].expected_revision,image.revision);
 resolvePost(reply({status:'indeterminate',operation_id:posts[0].operation_id},202));await first;
 assert.equal(recoveryID.value,'a'.repeat(32));assert.match(message.textContent,/indeterminate/);
 mode='list';await recover.onclick();assert.equal(posts.length,2);assert.equal(posts[1].op,'recover');assert.equal(posts[1].operation_id,posts[0].operation_id);
 // Recovery itself must not trigger another edit.
 assert.equal(posts.filter(p=>p.op==='edit').length,1);
 // Blank/different lookups cannot mint IDs or overwrite an unresolved mutation.
 const beforeLookup=posts.length; recoveryID.value='';await recover.onclick();assert.equal(posts.length,beforeLookup);assert.equal(recoveryID.value,'a'.repeat(32));
 assert.equal(buttons()[0].disabled,true);recoveryID.value='f'.repeat(32);await recover.onclick();
 assert.equal(posts.length,beforeLookup);assert.equal(recoveryID.value,'a'.repeat(32));assert.equal(buttons()[0].disabled,true);
 for (lookupCode of [400,401,403,409,429]) {mode='lookup-error';await recover.onclick();assert.equal(buttons()[0].disabled,true);assert.equal(recoveryID.value,'a'.repeat(32));}
 mode='wrong-receipt';await recover.onclick();assert.match(message.textContent,/did not match/);assert.equal(buttons()[0].disabled,true);
 mode='hung';const hanging=recover.onclick();assert.equal(recover.disabled,true);timerCallback();await hanging;
 assert.match(message.textContent,/timed out/);assert.equal(recover.disabled,false);assert.equal(gallery.children[4].disabled,false);assert.equal(buttons()[0].disabled,true);
 // Confirmed recovery unlocks future mutations, including stale delete refusal.
 mode='delayed';const recovery=ctx.galPost({op:'recover',operation_id:'a'.repeat(32)});
 resolvePost(reply({status:'complete',operation_id:'a'.repeat(32)})); mode='list';await recovery;
 mode='conflict';await ctx.galPost({op:'delete',name:image.name,expected_id:image.id,expected_revision:image.revision});
 assert.match(message.textContent,/stale revision/);assert.equal(posts.at(-1).expected_revision,image.revision);
 mode='lost';await ctx.editImage(image);assert.match(message.textContent,/outcome unconfirmed/);
 const count=posts.length;await ctx.editImage(image);assert.equal(posts.length,count);
 mode='error';await ctx.renderGallery();assert.match(message.textContent,/unavailable/);assert.equal(grid.children.length,1);
 // Explicit dismissal is local only; no cancellation or replay is submitted.
 const dismiss=gallery.children[4]; accepted=false;dismiss.onclick();assert.equal(buttons()[0].disabled,true);
 accepted=true;dismiss.onclick();assert.equal(posts.length,count);assert.equal(buttons()[0].disabled,false);
 mode='missing';recoveryID.value='e'.repeat(32);await recover.onclick();assert.match(message.textContent,/not found/);
 assert.equal(buttons()[0].disabled,true);dismiss.onclick();assert.equal(buttons()[0].disabled,false);
 assert.equal(recoveryID.value,'e'.repeat(32));assert.match(message.textContent,/outcome unchanged/);
 mode='failed';await recover.onclick();assert.match(message.textContent,/Provider output rejected/);assert.equal(buttons()[0].disabled,false);
 const beforeCancel=posts.length;promptValue=null;await ctx.editImage(image);assert.equal(posts.length,beforeCancel);
 accepted=false;await buttons()[1].onclick();assert.equal(posts.length,beforeCancel);assert.equal(buttons()[0].disabled,false);
 process.stdout.write('OWNED_GALLERY_UI_PASSED\n');
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1});
