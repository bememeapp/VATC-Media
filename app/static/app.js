const $ = id => document.getElementById(id);
const busy = p => ['queued','analysing','editing'].includes(p.status);
let batchId = new URLSearchParams(location.hash.slice(1)).get('batch') || localStorage.getItem('vatc-batch');
let posts = [], filter = 'all', configured = false, uploading = false, selected = null, currentView = 'result';
let toastTimer, polling = false, queueing = false, switchingPost = false;
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const url = (p, kind) => `/api/batches/${p.batch}/posts/${p.id}/file/${kind}?v=${p.updated}`;
const endpoint = (p, suffix='') => `/api/batches/${p.batch}/posts/${p.id}${suffix}`;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6500); }
async function api(path, options={}) {
  const response = await fetch(path, { ...options, headers: { ...(options.body && typeof options.body === 'string' ? {'Content-Type':'application/json'} : {}), ...options.headers }});
  let data;
  try { data = await response.json(); } catch { throw new Error('The server did not respond. Please try again.'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Please check your entries and try again.');
  return data;
}
async function action(fn) { try { await fn(); } catch(error) { toast(error.message); } }
async function ensureBatch() {
  if (!batchId) {
    const batch = await api('/api/batches',{method:'POST'});
    batchId = batch.id;
    history.replaceState(null,'',`#batch=${batchId}`);
    localStorage.setItem('vatc-batch',batchId);
  }
  return batchId;
}
async function refresh() {
  if (!batchId || polling) return;
  polling = true;
  try {
    const batch = await api(`/api/batches/${batchId}`);
    const changed = JSON.stringify(posts) !== JSON.stringify(batch.posts);
    posts = batch.posts;
    $('expiry').textContent = `Files expire ${new Date(batch.expires*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;
    if(changed) render();
  } finally { polling = false; }
}
function status(p) {
  if (p.status === 'ready') return ['Ready','ready'];
  if (p.status === 'failed') return ['Could not generate','attention'];
  if (p.status === 'review') return ['Could not generate','attention'];
  if (p.status === 'editing') return [p.stage || 'Editing backgrounds','busy'];
  if (p.status === 'analysing') return ['Writing & analysing','busy'];
  if (p.status === 'queued') return ['In the queue','busy'];
  return ['Ready to generate',''];
}
function render() {
  const ready = posts.filter(p=>p.has_result && p.caption.trim() && p.status==='ready');
  const attention = posts.filter(p=>['failed','review'].includes(p.status));
  const active = posts.filter(busy);
  $('allCount').textContent = $('totalBadge').textContent = posts.length;
  $('readyCount').textContent = ready.length;
  $('batchTag').textContent = batchId ? `BATCH ${batchId.slice(0,6).toUpperCase()}` : 'NEW BATCH';
  $('batchSummary').textContent = posts.length ? `${posts.length} post${posts.length===1?'':'s'} · ${ready.length} ready${active.length ? ` · ${active.length} processing` : ''}${attention.length ? ` · ${attention.length} could not generate` : ''}` : 'Good posts start here. Add your first images above.';
  $('generateAll').disabled = !configured || uploading || queueing || !posts.some(p=>p.status === 'uploaded');
  $('downloadAll').disabled = !ready.length;
  $('newBatch').disabled = uploading || queueing;
  const visible = filter === 'all' ? posts : ready;
  $('emptyState').hidden = !!visible.length;
  $('emptyState').querySelector('h3').textContent = posts.length ? 'No posts in this view yet' : 'Your next batch is a drop away';
  $('emptyState').querySelector('p').innerHTML = posts.length ? 'Your posts will appear here as they progress.' : 'Upload your originals, generate the edits,<br>then download your images and captions.';
  $('grid').innerHTML = visible.map(p=>{
    const [label,cls] = status(p);
    return `<article class="post-card"><button class="card-image" data-action="open" data-id="${p.id}" aria-label="Open ${escape(p.filename)}"><img loading="lazy" src="${url(p,p.has_result?'result':'thumb')}" alt="${escape(p.filename)}"><span class="status-pill ${cls}">${escape(label)}</span></button><div class="card-body"><div class="card-title">${escape(p.title || p.filename)}</div><div class="card-filename">${escape(p.filename)} · ${p.width} × ${p.height}</div><p class="card-caption">${escape(p.error || p.caption || 'Your refreshed image and caption will appear here.')}</p><div class="card-actions">${busy(p) ? '<button class="button small secondary" disabled>Processing…</button>' : p.status==='uploaded' ? `<button class="button small primary" data-action="generate" data-id="${p.id}" ${!configured?'disabled':''}>✧ Generate</button>` : `<button class="button small primary" data-action="regenerate" data-id="${p.id}" ${!configured?'disabled':''}>↻ Regenerate image</button>`}<button class="icon-button" data-action="remove" data-id="${p.id}" aria-label="Remove ${escape(p.filename)}" ${busy(p)?'disabled':''}>×</button></div>${p.has_result ? `<div class="card-actions"><a class="button small secondary" href="${url(p,'result')}" download="${escape(p.filename.replace(/\.[^.]+$/,''))}-edited.png">↓ Image</a><button class="button small secondary" data-action="copy" data-id="${p.id}" ${!p.caption.trim()?'disabled':''}>Copy caption</button></div>` : ''}</div></article>`;
  }).join('');
  if ($('editor').open) updatePostNavigation();
}
async function uploadFiles(fileList) {
  if (uploading) return;
  const files = [...fileList];
  if (!files.length) return;
  uploading = true; render();
  $('uploadProgress').hidden = false;
  let failed = 0;
  try {
    const id = await ensureBatch();
    for (let i=0; i<files.length; i++) {
      $('uploadProgress').textContent = `Uploading ${i+1} of ${files.length}: ${files[i].name}`;
      try {
        if (files[i].size > 12*1024*1024) throw new Error(`${files[i].name} is larger than 12 MB.`);
        await api(`/api/batches/${id}/posts`, {method:'POST',headers:{'Content-Type':files[i].type || 'application/octet-stream','X-Filename':encodeURIComponent(files[i].name)},body:files[i]});
      } catch(e) { failed++; toast(e.message); }
      await refresh();
    }
    toast(`${files.length-failed} post${files.length-failed===1?'':'s'} uploaded${failed ? `; ${failed} could not be uploaded` : '. Ready when you are.'}`);
  } finally {
    uploading = false; $('uploadProgress').hidden = true; $('fileInput').value = ''; render();
  }
}
$('dropzone').onclick = e => { if (e.target !== $('fileInput') && !uploading) $('fileInput').click(); };
$('dropzone').onkeydown = e => { if (['Enter',' '].includes(e.key)) {e.preventDefault();$('fileInput').click();} };
$('fileInput').onchange = e => action(()=>uploadFiles(e.target.files));
for (const name of ['dragenter','dragover']) $('dropzone').addEventListener(name,e=>{e.preventDefault();$('dropzone').classList.add('dragover');});
for (const name of ['dragleave','drop']) $('dropzone').addEventListener(name,e=>{e.preventDefault();$('dropzone').classList.remove('dragover');});
$('dropzone').addEventListener('drop',e=>action(()=>uploadFiles(e.dataTransfer.files)));
$('newBatch').onclick = ()=>action(async()=>{batchId=null;posts=[];filter='all';await ensureBatch();render();toast('New batch ready. Earlier batch links remain active until expiry.');});
$('studioNav').onclick = ()=>window.scrollTo({top:0,behavior:'smooth'});
for (const id of ['styleNav','recipeDetails']) $(id).onclick = ()=>$('styleDialog').showModal();
$('closeStyle').onclick = ()=>$('styleDialog').close();
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('selected',x===b));render();});
$('generateAll').onclick = ()=>action(async()=>{
  const pending = posts.filter(p=>p.status==='uploaded');
  queueing = true; render();
  try {
    for (const p of pending) await api(endpoint(p,'/run'),{method:'POST',body:JSON.stringify({mode:'all'})});
    await refresh(); toast(`${pending.length} posts added to the queue. You can leave this tab and return using its link.`);
  } finally { queueing = false; render(); }
});
$('downloadAll').onclick = ()=>{location.href=`/api/batches/${batchId}/download`;};
$('grid').onclick = e=>action(async()=>{
  const button = e.target.closest('[data-action]'); if(!button)return;
  const p = posts.find(p=>p.id===button.dataset.id); if(!p)return;
  if (button.dataset.action === 'open') await openEditor(p);
  if (button.dataset.action === 'copy') {await navigator.clipboard.writeText(p.caption);toast('Caption copied.');}
  if (button.dataset.action === 'generate') {button.disabled=true;await api(endpoint(p,'/run'),{method:'POST',body:JSON.stringify({mode:p.status==='failed'?'retry':'all'})});await refresh();}
  if (button.dataset.action === 'regenerate') {button.disabled=true;await api(endpoint(p,'/run'),{method:'POST',body:JSON.stringify({mode:'image'})});await refresh();}
  if (button.dataset.action === 'remove') {await api(endpoint(p),{method:'DELETE'});await refresh();}
});
async function loadImage(src) {
  const img=new Image(); img.src=src; await img.decode(); return img;
}
async function openEditor(p) {
  if(busy(p)) {toast('This post is still processing. It will be ready shortly.');return;}
  selected=p;
  $('editorTitle').textContent=p.title||p.filename;
  $('captionText').value=p.caption;
  updateCharacterCount();
  $('reviewNote').textContent=p.error||p.note;
  $('reviewNote').hidden=!(p.error||p.note);
  $('downloadImage').hidden=!p.has_result;
  $('downloadImage').href=url(p,'result');
  $('downloadImage').download=`${p.filename.replace(/\.[^.]+$/,'')}-edited.png`;
  $('redoImage').disabled=$('redoCaption').disabled=!configured;
  $('saveCaption').disabled=false;
  await showView(p.has_result?'result':'original');
  if (!$('editor').open) $('editor').showModal();
  updatePostNavigation();
  $('editor').querySelector('.editor-panel').scrollTop=0;
}
function updateCharacterCount() {$('characterCount').textContent=`${$('captionText').value.length.toLocaleString()} / 2,200`;}
$('captionText').oninput=updateCharacterCount;
async function persistCaption() {
  if(selected && $('captionText').value !== selected.caption) {
    selected=await api(endpoint(selected,'/caption'),{method:'PATCH',body:JSON.stringify({caption:$('captionText').value})});
    posts=posts.map(p=>p.id===selected.id?selected:p);
  }
}
function navigablePosts() {
  return posts.filter(p=>!busy(p) && (filter==='all' || (p.has_result && p.caption.trim() && p.status==='ready')));
}
function updatePostNavigation() {
  const list=navigablePosts(), index=list.findIndex(p=>p.id===selected?.id);
  $('previousPost').disabled=switchingPost || index<=0;
  $('nextPost').disabled=switchingPost || index<0 || index>=list.length-1;
  $('postPosition').textContent=index<0?'':` · ${index+1} / ${list.length}`;
}
async function navigatePost(direction) {
  if(switchingPost)return;
  const list=navigablePosts(), index=list.findIndex(p=>p.id===selected?.id);
  const target=index<0?null:list[index+direction];
  if(!target)return;
  switchingPost=true;updatePostNavigation();
  try {
    await persistCaption();
    await openEditor(posts.find(p=>p.id===target.id) || target);
  } finally {switchingPost=false;updatePostNavigation();}
}
$('previousPost').onclick=()=>action(()=>navigatePost(-1));
$('nextPost').onclick=()=>action(()=>navigatePost(1));
$('editor').addEventListener('keydown',e=>{
  if(e.target.closest('textarea,input,[contenteditable]') || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey)return;
  if(e.key==='ArrowLeft' || e.key==='ArrowRight') {e.preventDefault();action(()=>navigatePost(e.key==='ArrowLeft'?-1:1));}
});
$('closeEditor').onclick=()=>action(async()=>{await persistCaption();$('editor').close();await refresh();});
$('editor').addEventListener('cancel',e=>{e.preventDefault();$('closeEditor').click();});
$('saveCaption').onclick=()=>action(async()=>{await persistCaption();await refresh();toast('Caption saved.');});
$('copyCaption').onclick=()=>action(async()=>{await navigator.clipboard.writeText($('captionText').value);toast('Caption copied.');});
async function rerun(mode) {
  await persistCaption();
  await api(endpoint(selected,'/run'),{method:'POST',body:JSON.stringify({mode})});
  $('editor').close(); await refresh();toast(mode==='caption'?'Writing a fresh caption.':'Creating a fresh background.');
}
$('redoImage').onclick=()=>action(()=>rerun('image'));
$('redoCaption').onclick=()=>action(()=>rerun('caption'));
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>action(()=>showView(b.dataset.view)));
async function showView(view) {
  currentView=view;
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('selected',b.dataset.view===view));
  $('reviewImage').src=url(selected,view==='result'&&selected.has_result?'result':'original');
  $('visualHelp').textContent=view==='result'&&!selected.has_result?'Your edited image will appear here after generation.':'Same post. A refreshed background.';
}
async function init(){
  const config=await api('/api/config'); configured=config.configured;
  $('connection').innerHTML=`<i></i> ${configured?'Ready to create':'Setup needed'}`;
  $('connection').classList.toggle('off',!configured);$('setupNotice').hidden=configured;
  $('expiry').textContent=`Temporary workspace · files expire after ${config.retention_hours}h`;
  if(batchId){try{await refresh();history.replaceState(null,'',`#batch=${batchId}`);}catch(e){batchId=null;posts=[];localStorage.removeItem('vatc-batch');history.replaceState(null,'',location.pathname);toast(e.message);}}
  render();
}
action(init);
setInterval(()=>{if(batchId&&!document.hidden)refresh().catch(()=>{});},3500);
