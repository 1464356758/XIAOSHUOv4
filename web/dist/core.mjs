export const PLATFORM_NAMES = {qidian:'起点中文网',fanqie:'番茄小说',qimao:'七猫中文网',jinjiang:'晋江文学城'};
export const PHASE_LABELS = {WAITING_PLATFORM:'等待选择平台',RESEARCH_PLATFORM:'核查平台规则',SCANNING_RANKINGS:'研究榜单',WAITING_TOPIC:'等待选择题材',PLANNING:'长篇规划',WRITING:'撰写正文',REVIEWING:'检查本章',REVISING:'局部修订',CHECKPOINT:'阶段回看',SAVING:'保存进度',BLOCKED:'需要处理问题',WAITING_NEXT:'等待下一章',COMPLETE:'全书完成',NOT_INITIALIZED:'等待初始化'};

export class ReaderError extends Error { constructor(message,code='error'){super(message);this.name='ReaderError';this.code=code;} }

export function parseRepository(value){
  let input=String(value||'').trim();
  if (/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(input)) input='https://github.com/'+input;
  let url;try{url=new URL(input);}catch{throw new ReaderError('请填写完整的 GitHub 仓库链接。','invalid_repo');}
  if(url.protocol!=='https:'||url.hostname.toLowerCase()!=='github.com'||url.username||url.password||url.port||url.search||url.hash)throw new ReaderError('仅支持 https://github.com/用户名/仓库名。','invalid_repo');
  const parts=url.pathname.replace(/^\/+|\/+$/g,'').replace(/\.git$/,'').split('/');
  if(parts.length!==2||parts.some(x=>!x||x==='.'||x==='..'||!/^[A-Za-z0-9_.-]+$/.test(x)))throw new ReaderError('请填写仓库首页链接，不是文件或分支链接。','invalid_repo');
  return {owner:parts[0],name:parts[1],key:parts.join('/').toLowerCase(),url:'https://github.com/'+parts.join('/')};
}

export function rawURL(repo,path,{fresh=false}={}){
  if(typeof path!=='string'||path.startsWith('/')||path.includes('\\')||path.split('/').some(x=>x==='..'||x==='.'||!x))throw new ReaderError('仓库文件路径无效。','invalid_path');
  return `https://raw.githubusercontent.com/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}/HEAD/${path.split('/').map(encodeURIComponent).join('/')}${fresh?'?_refresh='+Date.now():''}`;
}

export function validateManifest(data,repo){
  const fail=()=>{throw new ReaderError('仓库中的阅读索引格式不完整，请让 GPT 更新 online/index.json。','invalid_manifest');};
  if(!data||data.schema_version!==1||!data.book||!data.progress||!data.task||!data.source||!Array.isArray(data.chapters))fail();
  if(data.source.repository_url&&parseRepository(data.source.repository_url).key!==repo.key)throw new ReaderError('这个仓库仍保留另一部小说的身份，请用空初始化包重新建立作品。','identity_mismatch');
  if(typeof data.book.title!=='string'||data.book.title.length>300||!Number.isInteger(data.progress.adopted_chapters)||!Number.isFinite(data.progress.actual_han)||data.progress.actual_han<0||!Number.isFinite(data.progress.target_han)||data.progress.target_han<=0||!Number.isInteger(data.progress.target_chapters)||data.progress.target_chapters<1)fail();
  if(data.chapters.length>2000||data.chapters.length!==data.progress.adopted_chapters)fail();
  for(const [i,chapter] of data.chapters.entries()){
    if(chapter.number!==i+1||typeof chapter.title!=='string'||!Number.isInteger(chapter.han_count)||chapter.han_count<0||!/^[a-f0-9]{64}$/.test(chapter.sha256)||!/^book\/chapters\/CH\d{3,6}_[a-f0-9]{64}\.txt$/.test(chapter.text_path))fail();
    if(!chapter.text_path.endsWith('_'+chapter.sha256+'.txt'))fail();
    if(chapter.storage!==undefined && (chapter.storage!=='encrypted_book_v1'||!/^vault\/book_[a-f0-9]{64}\.json$/.test(chapter.vault_path)||!/^[a-f0-9]{64}$/.test(chapter.vault_sha256)||!chapter.vault_path.endsWith('_'+chapter.vault_sha256+'.json')||chapter.book_id!==data.book.id||!/^[a-f0-9]{64}$/.test(chapter.key_id)))fail();
  }
  if(data.topics!==undefined&&(!Array.isArray(data.topics)||data.topics.length>10))fail();
  return data;
}

async function getBytes(url,{fetcher=fetch,signal,maxBytes=3000000}={}){
  const controller=new AbortController();let timedOut=false;
  const abort=()=>controller.abort();if(signal?.aborted)controller.abort();else signal?.addEventListener('abort',abort,{once:true});
  const timer=setTimeout(()=>{timedOut=true;controller.abort();},25000);
  try{
  const response=await fetcher(url,{cache:'no-store',signal:controller.signal,credentials:'omit',referrerPolicy:'no-referrer'});
  if(!response.ok){
    if(response.status===404)throw new ReaderError('未找到阅读数据：请确认仓库公开，并已使用初始化包生成 online/index.json。','not_found');
    if(response.status===403||response.status===429)throw new ReaderError('GitHub 暂时限制访问，请稍后手动刷新。','rate_limit');
    throw new ReaderError('读取 GitHub 失败（'+response.status+'），已保留当前内容。','network');
  }
  if(Number(response.headers?.get?.('content-length'))>maxBytes)throw new ReaderError('仓库文件过大，暂时无法读取。','too_large');
  const bytes=new Uint8Array(await response.arrayBuffer());
  if(bytes.length>maxBytes)throw new ReaderError('仓库文件过大，暂时无法读取。','too_large');
  try{return new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes).replace(/\r\n?/g,'\n');}
  catch{throw new ReaderError('文件不是有效 UTF-8 文本。','encoding');}
  }catch(error){if(timedOut)throw new ReaderError('GitHub 连接超时，请稍后刷新。','timeout');throw error;}
  finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);}
}

export async function fetchManifest(repo,options={}){
  let text;
  if(options.forceFresh){
    // 手动刷新和首次连接使用Contents接口，避开raw默认分支缓存。
    const url=`https://api.github.com/repos/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}/contents/online/index.json?_refresh=${Date.now()}`;
    const encoded=await getBytes(url,options);
    let resource;try{resource=JSON.parse(encoded);if(resource.encoding!=='base64'||typeof resource.content!=='string')throw new Error();text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(from64(resource.content.replace(/\s/g,''))).replace(/\r\n?/g,'\n');}
    catch{throw new ReaderError('GitHub最新索引无法解析，请稍后刷新。','invalid_manifest');}
  }else text=await getBytes(rawURL(repo,'online/index.json',{fresh:true}),options);
  let data;try{data=JSON.parse(text.replace(/^\uFEFF/,''));}catch{throw new ReaderError('阅读索引无法解析，请让 GPT 重新生成。','invalid_manifest');}
  return validateManifest(data,repo);
}

export async function sha256(text){
  if(!globalThis.crypto?.subtle)throw new ReaderError('当前浏览器无法校验正文，请在安全的 HTTPS 网页打开。','crypto_unavailable');
  const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));
  return [...new Uint8Array(hash)].map(x=>x.toString(16).padStart(2,'0')).join('');
}

export function canonicalJSON(value){
  if(Array.isArray(value))return '['+value.map(canonicalJSON).join(',')+']';
  if(value!==null&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonicalJSON(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
const from64=value=>{try{return Uint8Array.from(atob(value),c=>c.charCodeAt(0));}catch{throw new ReaderError('私密文件格式错误。','invalid_key');}};
export async function validateReaderKey(value,bookId){
  if(value?.format!=='xiaoshuo-reader-key-v1'||value.book_id!==bookId||!value.key_base64)throw new ReaderError('这份钥匙不属于当前小说。','invalid_key');
  const bytes=from64(value.key_base64);
  if(bytes.length!==32)throw new ReaderError('钥匙格式错误。','invalid_key');
  const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
  if(digest!==value.key_id)throw new ReaderError('钥匙校验失败。','invalid_key');
  return value;
}
export async function decryptPrivateBook(encoded,key,chapter){
  if(!key)throw new ReaderError('正文已加密。请先导入本书的私密阅读钥匙。','key_required');
  await validateReaderKey(key,chapter.book_id);
  if(key.key_id!==chapter.key_id)throw new ReaderError('钥匙与当前稿件不匹配。','invalid_key');
  if(await sha256(encoded)!==chapter.vault_sha256)throw new ReaderError('加密稿版本校验失败，请刷新。','hash_mismatch');
  let value;try{value=JSON.parse(encoded);}catch{throw new ReaderError('加密稿格式错误。','invalid_vault');}
  if(value.format!=='xiaoshuo-private-book-v1'||value.payload_codec!=='gzip-utf8-json'||value.key_id!==key.key_id||value.public?.book_id!==chapter.book_id||!Number.isInteger(value.plain_bytes)||value.plain_bytes<1||value.plain_bytes>80000000)throw new ReaderError('加密稿身份或容量无效。','invalid_vault');
  const header={};for(const field of ['format','public','key_id','plain_sha256','plain_bytes','payload_codec'])header[field]=value[field];
  let plain;
  try{
    const cryptoKey=await crypto.subtle.importKey('raw',from64(key.key_base64),'AES-GCM',false,['decrypt']);
    const zipped=await crypto.subtle.decrypt({name:'AES-GCM',iv:from64(value.nonce_base64),additionalData:new TextEncoder().encode(value.format+'\0'+canonicalJSON(header)),tagLength:128},cryptoKey,from64(value.ciphertext_base64));
    if(!globalThis.DecompressionStream)throw new ReaderError('请使用支持解压的新版浏览器。','browser_old');
    const reader=new Blob([zipped]).stream().pipeThrough(new DecompressionStream('gzip')).getReader();
    const chunks=[];let size=0;
    while(true){const next=await reader.read();if(next.done)break;size+=next.value.byteLength;if(size>value.plain_bytes){await reader.cancel();throw new Error('size');}chunks.push(next.value);}
    if(size!==value.plain_bytes)throw new Error('size');
    const bytes=new Uint8Array(size);let pos=0;for(const chunk of chunks){bytes.set(chunk,pos);pos+=chunk.byteLength;}
    plain=new TextDecoder('utf-8',{fatal:true}).decode(bytes);
  }catch(error){if(error instanceof ReaderError)throw error;throw new ReaderError('解密未通过：钥匙错误或加密稿被改动。','decrypt_failed');}
  if(await sha256(plain)!==value.plain_sha256)throw new ReaderError('私密稿内容校验失败。','hash_mismatch');
  let body;try{body=JSON.parse(plain);}catch{throw new ReaderError('私密稿内容格式错误。','invalid_vault');}
  if(body.format!==value.format||!body.files||typeof body.files!=='object')throw new ReaderError('私密稿内容无效。','invalid_vault');
  return body.files;
}
export async function fetchChapter(repo,chapter,options={}){
  let text;
  if(chapter.storage==='encrypted_book_v1'){
    if(!options.key)throw new ReaderError('正文已加密。请先导入本书的私密阅读钥匙。','key_required');
    const cacheKey=repo.key+':'+chapter.vault_sha256+':'+options.key.key_id;
    let work=options.vaultCache?.get(cacheKey);
    if(!work){work=(async()=>{const encoded=await getBytes(rawURL(repo,chapter.vault_path),{...options,maxBytes:30000000});return decryptPrivateBook(encoded,options.key,chapter);})();options.vaultCache?.set(cacheKey,work);}
    const files=await work;
    text=files[chapter.text_path.replace(/^book\//,'')];
    if(typeof text!=='string')throw new ReaderError('私密稿缺少这个章节。','invalid_vault');
  }else text=await getBytes(rawURL(repo,chapter.text_path),options);
  if(await sha256(text)!==chapter.sha256)throw new ReaderError('正文与当前采用版本不符，已停止复制和导出。请刷新后重试。','hash_mismatch');
  return text;
}

export function safeFilename(value){return (String(value||'小说').replace(/[<>:"/\\|?*\x00-\x1f]/g,'').trim().slice(0,70)||'小说')+'.txt';}
export function formatHan(value){return value>=10000?`${(value/10000).toFixed(value%10000===0?0:1)}万`:Number(value||0).toLocaleString('zh-CN');}

export async function exportBook(repo,manifest,{onProgress=()=>{},...options}={}){
  options.vaultCache=new Map();
  if(!manifest.chapters.length)throw new ReaderError('还没有可以导出的采用章节。','empty');
  if(manifest.progress.needs_revalidation?.length||manifest.chapters.some(c=>c.needs_revalidation))throw new ReaderError('前文改动后仍有章节待核对，完成核对后再导出整本。','pending');
  const output=new Array(manifest.chapters.length);let cursor=0,completed=0;
  async function worker(){
    while(cursor<manifest.chapters.length){
      if(options.signal?.aborted)throw new DOMException('已取消','AbortError');
      const i=cursor++;const chapter=manifest.chapters[i];
      const text=await fetchChapter(repo,chapter,options);
      output[i]=`第${chapter.number}章 ${chapter.title}\n\n${text}`;
      onProgress(++completed,manifest.chapters.length);
    }
  }
  await Promise.all(Array.from({length:Math.min(3,manifest.chapters.length)},worker));
  if(options.signal?.aborted)throw new DOMException('已取消','AbortError');
  return {text:manifest.book.title+'\n\n'+output.join('\n\n'),filename:safeFilename(manifest.book.title),chapters:output.length};
}

export function researchPrompt(repo,platform){
  const name=PLATFORM_NAMES[platform]||'由我指定的平台';
  return `读取GitHub小说仓库 ${repo.url}，遵循AGENTS.md与启动新书.txt，先核对正式状态。\n我选择${name}。请核查现行官方创作、投稿、签约、更新和AI辅助使用规则，记录来源、日期与未核实项；研究该平台当前公开榜单及同类型作品，区分榜单时间、类别和可见指标。根据研究提出5—10个原创选题，每个说明卖点、人物矛盾、90万字展开动力、阶段变化、结局方向和风险。把研究与候选写回仓库并更新online/index.json，停在等待我选题。不要替我选定，也不要开始正文。`;
}
export function startupPrompt(repo){return `接管GitHub小说仓库 ${repo.url}，读取AGENTS.md和启动新书.txt。若尚未初始化，使用空母版建立本书身份；已有进度则继续当前任务。先让我选择平台，再查官方规则和榜单，提出5—10个原创选题供我选择。每次重要进度写回GitHub并更新online/index.json，当前先不写正文。`;}

export function readShelf(storage){
  try{
    const data=JSON.parse(storage.getItem('novel-v4-shelf')||'[]');
    if(!Array.isArray(data))return [];
    const unique=new Map();for(const item of data.slice(0,50)){try{const repo=parseRepository(item.url);unique.set(repo.key,{...repo,title:typeof item.title==='string'?item.title.slice(0,300):repo.name,platform:item.platform||null});}catch{}}
    return [...unique.values()];
  }catch{return [];}
}

export class RequestGate{
  constructor(){this.serial=0;this.controller=null;}
  start(){this.controller?.abort();this.controller=new AbortController();const serial=++this.serial;return {signal:this.controller.signal,current:()=>serial===this.serial};}
  cancel(){this.controller?.abort();this.serial++;}
}
