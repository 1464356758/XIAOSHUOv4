import {parseRepository,fetchManifest,fetchChapter,exportBook,formatHan,safeFilename,researchPrompt,startupPrompt,readShelf,RequestGate,PLATFORM_NAMES,PHASE_LABELS} from './core.mjs';

const $=id=>document.getElementById(id);
const storage={getItem:key=>{try{return localStorage.getItem(key);}catch{return null;}},setItem:(key,value)=>{try{localStorage.setItem(key,value);}catch{}}};
const make=(tag,className,text)=>{const el=document.createElement(tag);if(className)el.className=className;if(text!==undefined)el.textContent=text;return el;};
const icon=id=>{const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href','#i-'+id);svg.append(use);return svg;};
const DEFAULT_REPO='https://github.com/1464356758/XIAOSHUOv4';
const state={shelf:readShelf(storage),active:null,manifest:null,chapter:null,text:null,platform:'qidian',tab:'dashboard',lastChecked:null,font:Math.min(28,Math.max(16,Number(storage.getItem('novel-font'))||20)),paper:storage.getItem('novel-paper')||'',cooldownUntil:0};
const syncGate=new RequestGate(),chapterGate=new RequestGate(),exportGate=new RequestGate(),formGate=new RequestGate();
let toastTimer;
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4300);}
function persist(){storage.setItem('novel-v4-shelf',JSON.stringify(state.shelf));if(state.active)storage.setItem('novel-v4-active',state.active.key);}
function prettyDate(value,short=false){if(!value)return'暂无更新';const d=new Date(value);if(Number.isNaN(d.valueOf()))return'时间未记录';return d.toLocaleString('zh-CN',short?{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}:{month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function setConnection(message,stale=false){$('syncStatus').replaceChildren(make('i'),document.createTextNode(message));$('syncStatus').classList.toggle('stale',stale);}
function setError(message){$('connectionError').textContent=message;$('connectionError').hidden=!message;}

function renderShelf(){
  $('bookshelf').replaceChildren();$('mobileBooks').replaceChildren();$('manageShelf').replaceChildren();$('shelfCount').textContent=state.shelf.length;
  for(const [i,book] of state.shelf.entries()){
    const b=make('button','book-item'+(state.active?.key===book.key?' active':''));b.type='button';b.setAttribute('aria-label','打开小说 '+book.title);
    const cover=make('span','book-spine',String(i+1).padStart(2,'0'));const text=make('span');text.append(make('strong','',book.title||book.name),make('small','',PLATFORM_NAMES[book.platform]||'尚未选择平台'));b.append(cover,text);b.addEventListener('click',()=>selectBook(book));$('bookshelf').append(b);
    const option=make('option','',book.title||book.name);option.value=book.key;option.selected=book.key===state.active?.key;$('mobileBooks').append(option);
    const row=make('div','manage-row');row.append(make('span','',book.title||book.name));const remove=make('button','','从书架移除');remove.type='button';remove.addEventListener('click',()=>removeBook(book.key));row.append(remove);$('manageShelf').append(row);
  }
}
function removeBook(key){state.shelf=state.shelf.filter(x=>x.key!==key);if(state.active?.key===key){syncGate.cancel();chapterGate.cancel();exportGate.cancel();state.active=null;state.manifest=null;state.chapter=null;state.text=null;if(state.shelf.length)selectBook(state.shelf[0]);else{resetBook();setConnection('未连接');setError('书架为空。添加一个小说仓库开始阅读。');}}persist();renderShelf();toast('已从本机书架移除，GitHub 仓库没有改动。');}
function resetBook(){
  $('exportAll').replaceChildren(icon('download'),document.createTextNode('导出整本 TXT'));
  $('bookTitle').textContent=state.active?.title||'每一个故事，都值得写完。';$('bookSubtitle').textContent='正在读取仓库的最新进度。';$('chapterCount').textContent='0';$('hanCount').replaceChildren(document.createTextNode('0'),make('span','',' 字'));$('chapterBadge').textContent='0';$('bookProgress').style.width='0%';$('currentPlatform').textContent='待选择';$('taskTitle').textContent='正在读取最新进度';$('taskDetail').textContent='请稍候。';$('nextAction').textContent='读取当前正式任务。';$('historyList').replaceChildren(make('p','empty-inline','等待同步。'));$('chapterList').replaceChildren();$('readerContent').hidden=true;$('readerEmpty').hidden=false;$('topicList').replaceChildren(make('div','empty-topics','等待研究结果。'));$('researchSummary').hidden=true;$('exportAll').disabled=true;$('copyContinue').disabled=true;
}
async function selectBook(book,preloaded=null){
  if(state.active?.key===book.key&&state.manifest&&!preloaded){setTab('dashboard');return;}
  syncGate.cancel();chapterGate.cancel();exportGate.cancel();state.active=book;state.manifest=null;state.chapter=null;state.text=null;state.lastChecked=null;state.cooldownUntil=0;setError('');resetBook();persist();renderShelf();setTab('dashboard');
  $('repoLink').href=book.url;$('repoName').textContent=book.owner+' / '+book.name;
  if(preloaded){applyManifest(preloaded);setConnection('已连接');return;}
  await refresh();
}
async function refresh(){
  if(!state.active)return;const repo=state.active;const ticket=syncGate.start();$('refresh').disabled=true;$('refresh').classList.add('spin');setConnection('同步中');
  try{
    const data=await fetchManifest(repo,{signal:ticket.signal});if(!ticket.current()||state.active?.key!==repo.key)return;
    const oldHash=state.chapter?.sha256;const currentNumber=state.chapter?.number;applyManifest(data);setError('');setConnection('已连接');state.cooldownUntil=0;
    if(currentNumber){const updated=data.chapters.find(x=>x.number===currentNumber);if(!updated){state.chapter=null;state.text=null;$('readerEmpty').hidden=false;$('readerContent').hidden=true;}else if(updated.sha256!==oldHash)await openChapter(updated);else{state.chapter=updated;$('pendingNotice').hidden=!updated.needs_revalidation;}}
  }catch(error){if(!ticket.current()||error.name==='AbortError')return;setConnection(state.manifest?'同步失败 · 保留上次数据':'连接失败',true);setError(error.message||'网络连接失败，请稍后刷新。');if(error.code==='rate_limit')state.cooldownUntil=Date.now()+300000;}
  finally{if(ticket.current()){$('refresh').disabled=false;$('refresh').classList.remove('spin');}}
}
function applyManifest(data){
  state.manifest=data;state.lastChecked=new Date();state.platform=data.book.platform||state.platform;
  const shelf=state.shelf.find(x=>x.key===state.active.key);if(shelf){shelf.title=data.book.title;shelf.platform=data.book.platform;}persist();renderShelf();
  $('bookTitle').textContent=data.book.title;$('bookSubtitle').textContent=data.initialized===false?'初始化包已就位，复制启动指令交给 GPT。':(data.book.platform?`${PLATFORM_NAMES[data.book.platform]||data.book.platform} · ${data.progress.completed?'这部故事已写完':'按自己的节奏，把故事写下去。'}`:'还没有选定平台，故事正准备出发。');
  $('chapterCount').textContent=data.progress.adopted_chapters;$('targetChapters').textContent=data.progress.target_chapters;$('chapterBadge').textContent=data.chapters.length;
  $('hanCount').replaceChildren(document.createTextNode(formatHan(data.progress.actual_han)),make('span','',' 字'));$('targetHan').textContent='目标约'+formatHan(data.progress.target_han)+'字';$('currentPlatform').textContent=PLATFORM_NAMES[data.book.platform]||'待选择';
  const percent=Math.min(100,Math.round(data.progress.adopted_chapters/data.progress.target_chapters*100));$('bookProgress').style.width=percent+'%';$('progressPercent').textContent=percent?`已完成 ${percent}%`:'尚未开始正文';
  $('sourceUpdate').textContent='进度更新 '+prettyDate(data.source.generated_at,true);$('lastChecked').textContent='刚刚核对 · 每60秒刷新';
  $('taskPill').textContent=data.progress.completed?'已完成':data.task.phase.startsWith('WAITING')?'等待你选择':'当前阶段';$('taskTitle').textContent=PHASE_LABELS[data.task.phase]||data.task.label||'查看当前任务';$('taskDetail').textContent=data.task.detail||'等待下一项任务。';$('nextAction').textContent=data.task.next_action||'读取仓库后继续。';
  $('copyContinue').disabled=false;$('exportAll').disabled=!data.chapters.length||!!data.progress.needs_revalidation?.length;
  $('dataVersion').textContent=`数据版本 ${data.source.state_revision??0} · ${data.source.workflow_revision??0}`;
  renderJourney();renderHistory();renderChapters();renderPlatforms();renderTopics();
}
function renderJourney(){
  const phase=state.manifest?.task.phase;let stage=0;if(phase==='RESEARCH_PLATFORM'||phase==='SCANNING_RANKINGS')stage=1;else if(phase==='WAITING_TOPIC')stage=2;else if(phase==='PLANNING')stage=3;else if(['WRITING','REVIEWING','REVISING','CHECKPOINT','SAVING','BLOCKED'].includes(phase))stage=state.manifest?.book.selected_topic_id?4:1;else if(phase==='COMPLETE')stage=5;
  $('journey').replaceChildren();for(const [i,label] of ['选择平台','规则与扫榜','选定题材','长篇规划','逐章创作','完整收束'].entries()){const li=make('li',i===stage?'active':i<stage?'done':'');const mark=make('span');if(i<stage)mark.append(icon('check'));else mark.textContent=String(i+1).padStart(2,'0');li.append(mark,document.createTextNode(label));$('journey').append(li);}
}
function renderHistory(){
  const rows=[...(state.manifest?.history||[])].reverse();$('historyList').replaceChildren();if(!rows.length){$('historyList').append(make('p','empty-inline','初始化后，这里会记录重要进度。'));return;}
  for(const row of rows){const div=make('div','history-row');const words=make('div');words.append(make('strong','',PHASE_LABELS[row.phase]||row.label||'更新进度'),make('p','',row.detail||''));const time=make('time','',prettyDate(row.at,true));div.append(make('span','history-dot'),words,time);$('historyList').append(div);}
}
function setTab(tab){state.tab=tab;for(const item of document.querySelectorAll('[data-tab]')){const active=item.dataset.tab===tab;item.classList.toggle('active',active);if(active)item.setAttribute('aria-current','page');else item.removeAttribute('aria-current');}for(const name of ['dashboard','chapters','topics'])$(name+'Panel').hidden=name!==tab;}
function renderChapters(){
  const query=$('chapterSearch').value.trim().toLowerCase();const rows=(state.manifest?.chapters||[]).filter(x=>!query||x.title.toLowerCase().includes(query)||String(x.number).includes(query));$('chapterList').replaceChildren();
  if(!rows.length){$('chapterList').append(make('p','list-empty',query?'没有匹配的章节。':'还没有采用章节。'));return;}
  for(const row of rows){const button=make('button','chapter-item'+(state.chapter?.number===row.number?' active':''));button.type='button';button.append(make('small','',`CHAPTER ${String(row.number).padStart(3,'0')}`),make('strong','',row.title),make('span','',`${row.han_count.toLocaleString()} 字${row.needs_revalidation?' · 待重新核对':''}`));button.addEventListener('click',()=>openChapter(row));$('chapterList').append(button);}
}
async function openChapter(chapter){
  if(!state.active)return;const repo=state.active;const ticket=chapterGate.start();state.chapter=chapter;state.text=null;setTab('chapters');renderChapters();$('readerEmpty').hidden=true;$('readerContent').hidden=false;$('readingNumber').textContent='CHAPTER '+String(chapter.number).padStart(3,'0');$('readingTitle').textContent=chapter.title;$('readingCount').textContent=chapter.han_count.toLocaleString()+' 字';$('hashVerified').textContent='正在读取并核对正文';$('chapterText').textContent='正在加载正文……';$('copyChapter').disabled=true;$('exportChapter').disabled=true;$('pendingNotice').hidden=!chapter.needs_revalidation;
  const i=state.manifest.chapters.findIndex(x=>x.number===chapter.number);$('prevChapter').disabled=i<=0;$('nextChapter').disabled=i<0||i>=state.manifest.chapters.length-1;
  try{const text=await fetchChapter(repo,chapter,{signal:ticket.signal});if(!ticket.current()||state.active?.key!==repo.key)return;state.text=text;$('chapterText').textContent=text;$('hashVerified').textContent='正文版本已核对';$('copyChapter').disabled=false;$('exportChapter').disabled=chapter.needs_revalidation;storage.setItem('novel-last-'+repo.key,String(chapter.number));}
  catch(error){if(!ticket.current()||error.name==='AbortError')return;$('chapterText').textContent=error.message||'正文读取失败。';$('hashVerified').textContent='读取未完成';toast(error.message||'读取失败');}
}
function renderPlatforms(){
  const notes={qidian:'起点中文网',fanqie:'番茄小说',qimao:'七猫中文网',jinjiang:'晋江文学城'};$('platformGrid').replaceChildren();
  for(const [id,name] of Object.entries(notes)){const button=make('button','platform-card'+(state.platform===id?' selected':''));button.type='button';button.setAttribute('aria-pressed',state.platform===id?'true':'false');button.append(make('span','selection-dot'),make('span','platform-symbol',name[0]),make('strong','',name),make('small','',state.manifest?.book.platform===id?'仓库当前平台':'规则 · 榜单 · 原创选题'));button.addEventListener('click',()=>{state.platform=id;renderPlatforms();});$('platformGrid').append(button);}
  $('platformActionHint').textContent=`准备研究${PLATFORM_NAMES[state.platform]}。复制指令交给 GPT，结果保存到仓库后自动显示。`;
}
function renderTopics(){
  const data=state.manifest;const topics=data?.topics||[];$('topicList').replaceChildren();$('topicCount').textContent=topics.length?`${topics.length} 个方向 · 等你决定`:'等待研究结果';
  if(!topics.length){const box=make('div','empty-topics');box.append(make('span','','还没有候选题材'),make('p','','研究完成并提交到仓库后，这里会显示候选的卖点、长篇动力和风险。'));$('topicList').append(box);}
  for(const topic of topics){const chosen=data.book.selected_topic_id===topic.id;const card=make('article','topic-card'+(chosen?' selected':''));card.append(make('span','topic-id',topic.id+' / '+topic.genre),make('h3','',topic.title),make('p','',topic.pitch));const dl=make('dl');for(const [label,key] of [['读者承诺','reader_promise'],['长篇动力','long_form_engine'],['阶段变化','stage_variation'],['需要注意','risk']])dl.append(make('dt','',label),make('dd','',topic[key]||'研究记录待补充'));card.append(dl);const button=make('button','button '+(chosen?'subtle':'outline')+' small',chosen?'已选定':'复制选题指令');button.disabled=!!data.book.selected_topic_id;button.addEventListener('click',()=>copyText(`读取GitHub仓库 ${state.active.url} 的当前正式状态。我选择选题 ${topic.id}《${topic.title}》。请记录我的选择，完成长篇规划、人物与结局条件，再更新online/index.json。先不写正文。`,'选题指令已复制，发送给 GPT 即可。'));card.append(button);$('topicList').append(card);}
  $('researchSummary').hidden=!data?.research;if(data?.research){const report=data.research;const box=$('researchSummary');box.replaceChildren(make('h3','',report.coverage_status==='verified'?'平台研究已保存':'平台研究已保存，部分信息待核实'),make('p','',`核查时间：${prettyDate(report.checked_at)} · 榜单样本 ${report.rank_samples?.length||0} 个`));if(report.gaps?.length)box.append(make('p','','未核实：'+report.gaps.join('；')));for(const source of (report.sources||[]).slice(0,8)){try{const url=new URL(source.url);if(url.protocol!=='https:')continue;const a=make('a','source-link',source.title);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';box.append(a);}catch{}}}
}
async function copyText(text,message='已复制'){if(!text){toast('暂时没有可复制的内容。');return false;}try{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(text);else throw new Error('fallback');toast(message);return true;}catch{const input=make('textarea');input.value=text;input.style.cssText='position:fixed;left:-9999px;top:0';document.body.append(input);input.focus();input.select();let ok=false;try{ok=document.execCommand('copy');}catch{}input.remove();toast(ok?message:'浏览器未允许复制，请选择正文后长按复制。');return ok;}}
function download(text,name){const blob=new Blob(['\uFEFF',text],{type:'text/plain;charset=utf-8'});const url=URL.createObjectURL(blob);const a=make('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),20000);}
async function exportAll(){
  if(!state.active||!state.manifest)return;const repo=state.active;const manifest=state.manifest;const ticket=exportGate.start();const button=$('exportAll');button.disabled=true;button.replaceChildren(icon('download'),document.createTextNode('准备导出…'));
  try{const result=await exportBook(repo,manifest,{signal:ticket.signal,onProgress:(n,total)=>{if(ticket.current())button.replaceChildren(icon('download'),document.createTextNode(`导出 ${n}/${total}`));}});if(!ticket.current()||state.active?.key!==repo.key)return;download(result.text,result.filename);toast(`已生成 ${result.chapters} 章 TXT，请在下载中查看。`);}
  catch(error){if(ticket.current()&&error.name!=='AbortError')toast(error.message||'导出失败，未生成不完整文件。');}
  finally{if(ticket.current()){button.replaceChildren(icon('download'),document.createTextNode('导出整本 TXT'));button.disabled=!state.manifest?.chapters.length||!!state.manifest?.progress.needs_revalidation?.length;}}
}
function showDialog(){$('formError').hidden=true;$('repoDialog').showModal();renderShelf();setTimeout(()=>$('repoInput').focus(),0);}
function closeDialog(){formGate.cancel();$('repoDialog').close();$('connectButton').disabled=false;}
$('repoForm').addEventListener('submit',async event=>{event.preventDefault();$('formError').hidden=true;let repo;try{repo=parseRepository($('repoInput').value);}catch(error){$('formError').textContent=error.message;$('formError').hidden=false;return;}const ticket=formGate.start();$('connectButton').disabled=true;
  try{const data=await fetchManifest(repo,{signal:ticket.signal});if(!ticket.current())return;const existing=state.shelf.find(x=>x.key===repo.key);const book=existing||{...repo,title:data.book.title,platform:data.book.platform};if(!existing)state.shelf.push(book);$('repoInput').value='';closeDialog();await selectBook(book,data);toast(existing?'已切换到这部小说。':'小说已添加到书架。');}
  catch(error){if(!ticket.current()||error.name==='AbortError')return;$('formError').textContent=error.message||'连接失败，请检查仓库链接。';$('formError').hidden=false;}
  finally{if(ticket.current())$('connectButton').disabled=false;}
});
$('addBook').addEventListener('click',showDialog);$('mobileAdd').addEventListener('click',showDialog);$('closeDialog').addEventListener('click',closeDialog);$('repoDialog').addEventListener('cancel',()=>formGate.cancel());$('mobileBooks').addEventListener('change',event=>{const b=state.shelf.find(x=>x.key===event.target.value);if(b)selectBook(b);});
$('refresh').addEventListener('click',refresh);$('exportAll').addEventListener('click',exportAll);document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>setTab(b.dataset.tab)));$('goTopics').addEventListener('click',()=>setTab('topics'));$('chapterSearch').addEventListener('input',renderChapters);$('backToContents').addEventListener('click',()=>$('chapterList').scrollIntoView({behavior:'smooth',block:'start'}));
$('copyStartup').addEventListener('click',()=>state.active&&copyText(startupPrompt(state.active),'启动指令已复制，发送给 GPT 即可。'));$('copyResearch').addEventListener('click',()=>state.active&&copyText(researchPrompt(state.active,state.platform),'平台研究指令已复制，发送给 GPT 即可。'));
$('copyContinue').addEventListener('click',()=>{if(!state.active||!state.manifest)return;const data=state.manifest;if(data.task.phase==='WAITING_PLATFORM'||data.initialized===false){copyText(startupPrompt(state.active),'启动指令已复制。');return;}copyText(`读取GitHub小说仓库 ${state.active.url} 的AGENTS.md与最新正式状态，接着当前任务执行。当前显示：${data.task.label}。下一步：${data.task.next_action}。复用已有产物，遵守三轮修订上限，完成后更新online/index.json并写回仓库。`,'继续任务指令已复制。');});
$('copyChapter').addEventListener('click',()=>{if(state.chapter&&state.text!==null)copyText(`第${state.chapter.number}章 ${state.chapter.title}\n\n${state.text}`,'本章已复制。');});$('exportChapter').addEventListener('click',()=>{if(state.chapter&&state.text!==null&&!state.chapter.needs_revalidation)download(`第${state.chapter.number}章 ${state.chapter.title}\n\n${state.text}`,safeFilename(`第${state.chapter.number}章_${state.chapter.title}`));});
for(const [id,step] of [['prevChapter',-1],['nextChapter',1]])$(id).addEventListener('click',()=>{if(!state.chapter||!state.manifest)return;const index=state.manifest.chapters.findIndex(x=>x.number===state.chapter.number);const next=state.manifest.chapters[index+step];if(next){openChapter(next);$('readerContent').scrollIntoView({behavior:'smooth',block:'start'});}});
function applyPreferences(){document.documentElement.style.setProperty('--reader-font',state.font+'px');document.body.dataset.paper=state.paper;storage.setItem('novel-font',String(state.font));storage.setItem('novel-paper',state.paper);}
$('smallerFont').addEventListener('click',()=>{state.font=Math.max(16,state.font-1);applyPreferences();});$('largerFont').addEventListener('click',()=>{state.font=Math.min(28,state.font+1);applyPreferences();});$('togglePaper').addEventListener('click',()=>{state.paper=state.paper?'':'warm';applyPreferences();});
setInterval(()=>{if(!document.hidden&&state.active&&Date.now()>state.cooldownUntil)refresh();},60000);document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.active&&Date.now()>state.cooldownUntil&&(!state.lastChecked||Date.now()-state.lastChecked.valueOf()>45000))refresh();});window.addEventListener('online',()=>state.active&&refresh());window.addEventListener('offline',()=>{setConnection('当前离线 · 保留已读内容',true);});
applyPreferences();let initial=null;try{const requested=new URLSearchParams(location.search).get('repo');if(requested){const parsed=parseRepository(requested);initial=state.shelf.find(x=>x.key===parsed.key)||{...parsed,title:parsed.name};if(!state.shelf.some(x=>x.key===parsed.key))state.shelf.push(initial);}}catch(error){toast(error.message);}
if(!state.shelf.length){const repo=parseRepository(DEFAULT_REPO);state.shelf.push({...repo,title:'未命名小说',platform:null});}initial||=state.shelf.find(x=>x.key===storage.getItem('novel-v4-active'))||state.shelf[0];renderShelf();renderPlatforms();renderJourney();selectBook(initial);
