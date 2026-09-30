const $ = id => document.getElementById(id);
const busy = p => ['queued','analysing','editing'].includes(p.status);
let batchId = new URLSearchParams(location.hash.slice(1)).get('batch') || localStorage.getItem('vatc-batch');
let posts = [], filter = 'all', configured = false, uploading = false, selected = null, currentView = 'result';
let regions = [], baseImage, maskCanvas, brush = 'protect', drawing = false, startPoint;
let toastTimer, polling = false, queueing = false;
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
  if (p.approved) return ['✓ Reviewed','ready'];
  if (p.status === 'ready') return ['Ready to review','ready'];
  if (p.status === 'failed') return ['Needs attention','attention'];
  if (p.status === 'review') return ['Check selection','attention'];
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
  $('attentionCount').textContent = attention.length;
  $('batchTag').textContent = batchId ? `BATCH ${batchId.slice(0,6).toUpperCase()}` : 'NEW BATCH';
  $('batchSummary').textContent = posts.length ? `${posts.length} post${posts.length===1?'':'s'} · ${ready.length} ready${active.length ? ` · ${active.length} processing` : ''}${attention.length ? ` · ${attention.length} need a look` : ''}` : 'Good posts start here. Add your first images above.';
  $('generateAll').disabled = !configured || uploading || queueing || !posts.some(p=>p.status === 'uploaded');
  $('downloadAll').disabled = !ready.length;
  $('newBatch').disabled = uploading || queueing;
  const visible = filter === 'all' ? posts : filter === 'ready' ? ready : attention;
  $('emptyState').hidden = !!visible.length;
  $('emptyState').querySelector('h3').textContent = posts.length ? 'No posts in this view yet' : 'Your next batch is a drop away';
  $('emptyState').querySelector('p').innerHTML = posts.length ? 'Your posts will appear here as they progress.' : 'Upload your originals, generate the edits, then give<br>each post a final look before downloading.';
  $('grid').innerHTML = visible.map(p=>{
    const [label,cls] = status(p);
    return `<article class="post-card"><button class="card-image" data-action="review" data-id="${p.id}" aria-label="Review ${escape(p.filename)}"><img loading="lazy" src="${url(p,p.has_result?'result':'thumb')}" alt="${escape(p.filename)}"><span class="status-pill ${cls}">${escape(label)}</span></button><div class="card-body"><div class="card-title">${escape(p.title || p.filename)}</div><div class="card-filename">${escape(p.filename)} · ${p.width} × ${p.height}</div><p class="card-caption">${escape(p.error || p.caption || 'Your refreshed image and caption will appear here.')}</p><div class="card-actions">${p.status==='uploaded' || p.status==='failed' ? `<button class="button small primary" data-action="generate" data-id="${p.id}" ${!configured?'disabled':''}>${p.status==='failed'?'↻ Retry post':'✧ Generate'}</button>` : `<button class="button small secondary" data-action="review" data-id="${p.id}" ${busy(p)?'disabled':''}>${busy(p)?'Processing…':'Review post ↗'}</button>`}<button class="icon-button" data-action="remove" data-id="${p.id}" aria-label="Remove ${escape(p.filename)}" ${busy(p)?'disabled':''}>×</button></div></div></article>`;
  }).join('');
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
  if (button.dataset.action === 'review') await openEditor(p);
  if (button.dataset.action === 'generate') {button.disabled=true;await api(endpoint(p,'/run'),{method:'POST',body:JSON.stringify({mode:p.status==='failed'?'retry':'all'})});await refresh();}
  if (button.dataset.action === 'remove') {await api(endpoint(p),{method:'DELETE'});await refresh();}
});
async function loadImage(src) {
  const img=new Image(); img.src=src; await img.decode(); return img;
}
async function openEditor(p) {
  if(busy(p)) {toast('This post is still processing. It will be ready to review shortly.');return;}
  selected=p; regions=structuredClone(p.regions);
  $('editorTitle').textContent=p.title||p.filename;
  $('captionText').value=p.caption;
  updateCharacterCount();
  $('reviewNote').textContent=p.error||p.note;
  $('reviewNote').hidden=!(p.error||p.note);
  $('approvePost').textContent=p.approved?'✓ Reviewed':'✓ Mark reviewed';
  $('approvePost').disabled=!p.has_result||!p.caption.trim();
  $('downloadImage').hidden=!p.has_result;
  $('downloadImage').href=url(p,'result');
  $('downloadImage').download=`${p.filename.replace(/\.[^.]+$/,'')}-edited.png`;
  $('redoImage').disabled=$('redoCaption').disabled=!configured;
  $('saveCaption').disabled=false;
  baseImage=await loadImage(url(p,'original'));
  maskCanvas=document.createElement('canvas'); maskCanvas.width=p.width;maskCanvas.height=p.height;
  const ctx=maskCanvas.getContext('2d'); ctx.fillStyle='black';ctx.fillRect(0,0,p.width,p.height);
  if(p.has_mask) ctx.drawImage(await loadImage(url(p,'mask')),0,0);
  await showView(p.status==='review'?(p.has_mask?'mask':'layout'):(p.has_result?'result':'original'));
  $('editor').showModal();
}
function updateCharacterCount() {$('characterCount').textContent=`${$('captionText').value.length.toLocaleString()} / 2,200`;}
$('captionText').oninput=updateCharacterCount;
async function persistCaption() {
  if(selected && $('captionText').value !== selected.caption) selected=await api(endpoint(selected,'/caption'),{method:'PATCH',body:JSON.stringify({caption:$('captionText').value})});
}
$('closeEditor').onclick=()=>action(async()=>{await persistCaption();$('editor').close();await refresh();});
$('editor').addEventListener('cancel',e=>{e.preventDefault();$('closeEditor').click();});
$('saveCaption').onclick=()=>action(async()=>{await persistCaption();await refresh();toast('Caption saved.');});
$('copyCaption').onclick=()=>action(async()=>{await navigator.clipboard.writeText($('captionText').value);toast('Caption copied.');});
$('approvePost').onclick=()=>action(async()=>{await persistCaption();selected=await api(endpoint(selected,'/approve'),{method:'POST'});$('approvePost').textContent=selected.approved?'✓ Reviewed':'✓ Mark reviewed';await refresh();toast(selected.approved?'Post marked as reviewed.':'Review mark removed.');});
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
  const editing=['mask','layout'].includes(view);
  $('editCanvas').hidden=!editing;$('reviewImage').hidden=editing;
  $('maskControls').hidden=view!=='mask';$('layoutControls').hidden=view!=='layout';
  if(!editing) {
    $('reviewImage').src=url(selected,view==='result'&&selected.has_result?'result':'original');
    $('visualHelp').textContent=view==='result'&&!selected.has_result?'An edited version will appear after generation. Showing the original for now.':'Compare with the original and check the subject edges.';
  } else {
    $('editCanvas').width=selected.width;$('editCanvas').height=selected.height;
    $('visualHelp').textContent=view==='mask'?'Blue areas can change. Everything else stays original. Protect missed subject details, or paint background to edit.':'Draw one rectangle around each photo only. Exclude text, avatars and white gaps. Clear areas to start over.';
    drawCanvas(); if(view==='layout')renderRegions();
  }
}
function drawCanvas(preview) {
  const canvas=$('editCanvas'),ctx=canvas.getContext('2d');ctx.drawImage(baseImage,0,0);
  if(currentView==='mask') {
    const data=maskCanvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height);
    for(let i=0;i<data.data.length;i+=4){const allowed=data.data[i];data.data[i]=49;data.data[i+1]=94;data.data[i+2]=234;data.data[i+3]=allowed>127?105:0;}
    const overlay=document.createElement('canvas');overlay.width=canvas.width;overlay.height=canvas.height;overlay.getContext('2d').putImageData(data,0,0);ctx.drawImage(overlay,0,0);
  } else {
    const all=preview?[...regions,preview]:regions;
    all.forEach((r,i)=>{const x=r.x*canvas.width/1000,y=r.y*canvas.height/1000,w=r.w*canvas.width/1000,h=r.h*canvas.height/1000;ctx.fillStyle='#315eea22';ctx.fillRect(x,y,w,h);ctx.strokeStyle='#315eea';ctx.lineWidth=Math.max(2,canvas.width/300);ctx.strokeRect(x,y,w,h);ctx.fillStyle='#315eea';ctx.font=`bold ${Math.max(20,canvas.width/35)}px sans-serif`;ctx.fillText(String(i+1),x+8,y+Math.max(26,canvas.width/30));});
  }
}
function renderRegions() {
  $('regionsList').innerHTML=regions.map((r,i)=>`<div class="region-row"><span>Photo ${i+1}</span><textarea data-region="${i}" aria-label="Background for photo ${i+1}" maxlength="1500">${escape(r.background)}</textarea><button class="icon-button" data-remove-region="${i}" aria-label="Remove photo area ${i+1}">×</button></div>`).join('');
}
$('regionsList').oninput=e=>{if(e.target.dataset.region!==undefined)regions[Number(e.target.dataset.region)].background=e.target.value;};
$('regionsList').onclick=e=>{const button=e.target.closest('[data-remove-region]');if(button){regions.splice(Number(button.dataset.removeRegion),1);renderRegions();drawCanvas();}};
$('clearRegions').onclick=()=>{regions=[];renderRegions();drawCanvas();};
function point(e) {const r=$('editCanvas').getBoundingClientRect();return {x:Math.max(0,Math.min(selected.width,(e.clientX-r.left)*selected.width/r.width)),y:Math.max(0,Math.min(selected.height,(e.clientY-r.top)*selected.height/r.height))};}
function rectangle(a,b){return {x:Math.round(Math.min(a.x,b.x)*1000/selected.width),y:Math.round(Math.min(a.y,b.y)*1000/selected.height),w:Math.round(Math.abs(a.x-b.x)*1000/selected.width),h:Math.round(Math.abs(a.y-b.y)*1000/selected.height),background:'A natural, different background appropriate to the subject, matching the original lighting and perspective.'};}
let lastPoint;
function paint(p){const ctx=maskCanvas.getContext('2d');ctx.strokeStyle=ctx.fillStyle=brush==='protect'?'black':'white';ctx.lineWidth=Number($('brushSize').value)*selected.width/$('editCanvas').getBoundingClientRect().width;ctx.lineCap='round';ctx.beginPath();ctx.moveTo((lastPoint||p).x,(lastPoint||p).y);ctx.lineTo(p.x,p.y);ctx.stroke();ctx.beginPath();ctx.arc(p.x,p.y,ctx.lineWidth/2,0,Math.PI*2);ctx.fill();lastPoint=p;drawCanvas();}
$('editCanvas').onpointerdown=e=>{if(currentView==='layout'&&regions.length>=8){toast('Up to eight photos per post.');return;}drawing=true;startPoint=point(e);lastPoint=null;e.target.setPointerCapture(e.pointerId);if(currentView==='mask')paint(startPoint);};
$('editCanvas').onpointermove=e=>{if(!drawing)return;const p=point(e);if(currentView==='mask')paint(p);else drawCanvas(rectangle(startPoint,p));};
$('editCanvas').onpointerup=e=>{if(!drawing)return;drawing=false;if(currentView==='layout'){const r=rectangle(startPoint,point(e));if(r.w>15&&r.h>15){regions.push(r);renderRegions();}drawCanvas();}};
$('editCanvas').onpointercancel=()=>{drawing=false;drawCanvas();};
$('protectBrush').onclick=()=>{brush='protect';$('protectBrush').classList.add('selected');$('editBrush').classList.remove('selected');};
$('editBrush').onclick=()=>{brush='edit';$('editBrush').classList.add('selected');$('protectBrush').classList.remove('selected');};
$('saveLayout').onclick=()=>action(async()=>{await persistCaption();selected=await api(endpoint(selected,'/layout'),{method:'PUT',body:JSON.stringify({regions:regions.map(({x,y,w,h,background})=>({x,y,w,h,background}))})});$('editor').close();await refresh();toast('Photo areas saved. Generate the post to apply them.');});
$('saveMask').onclick=()=>action(async()=>{await persistCaption();selected=await api(endpoint(selected,'/mask'),{method:'PUT',body:JSON.stringify({image:maskCanvas.toDataURL('image/png')})});$('editor').close();await refresh();toast('Edit mask saved. Generate the post to apply it.');});
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
