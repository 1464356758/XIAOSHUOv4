import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto,createHash,randomBytes} from 'node:crypto';
import {gzipSync} from 'node:zlib';
import {canonicalJSON,decryptPrivateBook,validateReaderKey,portableSha256} from '../dist/core.mjs';
import {parseRepository,rawURL,validateManifest,fetchManifest,fetchChapter,sha256,exportBook,readShelf,RequestGate,researchPrompt,startupPrompt,continuationPrompt,safeFilename} from '../dist/core.mjs';
if(!globalThis.crypto)globalThis.crypto=webcrypto;
const repo=parseRepository('https://github.com/example/novel');
async function fixture(count=1){
  const text='这是测试正文，不是正式小说。';const hash=await sha256(text);
  return {text,data:{schema_version:1,initialized:true,book:{id:'test',title:'测试作品',platform:null},source:{repository_url:repo.url,branch:'main'},progress:{adopted_chapters:count,actual_han:count*12,target_chapters:100,target_han:900000,needs_revalidation:[]},task:{phase:'WRITING'},topics:[],chapters:Array.from({length:count},(_,i)=>({number:i+1,title:'测试章节'+(i+1),han_count:12,sha256:hash,text_path:`book/chapters/CH${String(i+1).padStart(3,'0')}_${hash}.txt`,needs_revalidation:false}))}};
}
const response=(text,status=200)=>new Response(text,{status,headers:{'content-type':'text/plain;charset=utf-8'}});

test('repository URL and owner/repo normalize to same identity',()=>{assert.equal(parseRepository('example/novel').key,repo.key);assert.equal(parseRepository(repo.url+'.git/').key,repo.key);});
test('reject credentials, lookalike hosts, file URLs, query and traversal',()=>{for(const value of ['javascript:alert(1)','https://github.com.evil/a/b','https://user:token@github.com/a/b','https://github.com/a/b/tree/main','https://github.com/a/b?token=secret','https://github.com/a/../b'])assert.throws(()=>parseRepository(value));});
test('raw file reads use GitHub default branch HEAD',()=>{assert.match(rawURL(repo,'online/index.json'),/\/HEAD\/online\/index\.json$/);});
test('reject unsafe content paths',()=>{for(const value of ['../secret','/absolute','book/../secret','book\\x','book//x'])assert.throws(()=>rawURL(repo,value));});
test('manifest must match the selected repository',async()=>{const {data}=await fixture();data.source.repository_url='https://github.com/example/other';assert.throws(()=>validateManifest(data,repo),/另一部小说/);});
test('manifest requires contiguous adopted chapters',async()=>{const {data}=await fixture(2);data.chapters[1].number=3;assert.throws(()=>validateManifest(data,repo));});
test('manifest refuses arbitrary URLs and unsafe chapter locations',async()=>{const {data}=await fixture();data.chapters[0].text_path='https://evil.test/file';assert.throws(()=>validateManifest(data,repo));});
test('empty initialization manifest is a valid empty state',async()=>{const {data}=await fixture(0);data.initialized=false;data.book.id=null;data.source.repository_url=null;assert.equal(validateManifest(data,repo).chapters.length,0);});
test('manifest fetch requests live raw data with no-store and omits credentials',async()=>{const {data}=await fixture();let calls=0;const result=await fetchManifest(repo,{fetcher:async(url,options)=>{calls++;assert.match(url,/raw\.githubusercontent\.com/);assert.match(url,/_refresh=/);assert.equal(options.cache,'no-store');assert.equal(options.credentials,'omit');return response(JSON.stringify(data));}});assert.equal(calls,1);assert.equal(result.book.title,'测试作品');});
test('manual fresh read decodes the current Contents response without credentials',async()=>{const {data}=await fixture();const result=await fetchManifest(repo,{forceFresh:true,fetcher:async(url,options)=>{assert.match(url,/api\.github\.com\/repos\/example\/novel\/contents\/online\/index\.json/);assert.equal(options.credentials,'omit');return response(JSON.stringify({encoding:'base64',content:Buffer.from(JSON.stringify(data)).toString('base64')}));}});assert.equal(result.book.title,data.book.title);});
test('404 is an explicit initialization or access error',async()=>{await assert.rejects(fetchManifest(repo,{fetcher:async()=>response('no',404)}),error=>error.code==='not_found');});
test('rate limits are explicit and never appear as empty success',async()=>{await assert.rejects(fetchManifest(repo,{fetcher:async()=>response('slow',429)}),error=>error.code==='rate_limit');});
test('malformed JSON is rejected',async()=>{await assert.rejects(fetchManifest(repo,{fetcher:async()=>response('<html>bad</html>')}),error=>error.code==='invalid_manifest');});
test('oversized response is rejected',async()=>{await assert.rejects(fetchManifest(repo,{maxBytes:5,fetcher:async()=>response('123456789')}),error=>error.code==='too_large');});
test('chapter text must match adopted SHA-256',async()=>{const {text,data}=await fixture();assert.equal(await fetchChapter(repo,data.chapters[0],{fetcher:async()=>response(text)}),text);});
test('portable SHA-256 matches native SHA on Unicode, padding boundaries and chapter-sized content',()=>{for(const data of [Buffer.from(''),Buffer.from('abc'),Buffer.from('中文正文🌙\n换行'),...Array.from([1,55,56,63,64,65,119,120,127,128,129,32768],n=>randomBytes(n)),Buffer.from('验'.repeat(9000))])assert.equal(portableSha256(data),createHash('sha256').update(data).digest('hex'));});
test('chapter tampering stops reading and export',async()=>{const {data}=await fixture();await assert.rejects(fetchChapter(repo,data.chapters[0],{fetcher:async()=>response('被改过的文字')}),error=>error.code==='hash_mismatch');});
test('CRLF normalizes the same way as the Python state tool',async()=>{const text='第一段\n第二段';const hash=await sha256(text);const {data}=await fixture();const chapter={...data.chapters[0],sha256:hash,text_path:`book/chapters/CH001_${hash}.txt`};assert.equal(await fetchChapter(repo,chapter,{fetcher:async()=>response('第一段\r\n第二段')}),text);});
test('full export preserves chapter order while loading at most three at once',async()=>{const {text,data}=await fixture(8);let active=0,max=0;const done=[];const result=await exportBook(repo,data,{fetcher:async()=>{active++;max=Math.max(max,active);await new Promise(resolve=>setTimeout(resolve,2));active--;return response(text);},onProgress:n=>done.push(n)});assert.equal(result.chapters,8);assert.ok(max<=3);assert.equal(done.length,8);assert.ok(result.text.indexOf('第1章')<result.text.indexOf('第8章'));});
test('empty book does not export a fake chapter',async()=>{const {data}=await fixture(0);await assert.rejects(exportBook(repo,data),error=>error.code==='empty');});
test('pending revalidation blocks whole-book export',async()=>{const {data}=await fixture();data.progress.needs_revalidation=[1];await assert.rejects(exportBook(repo,data),error=>error.code==='pending');});
test('one failed chapter prevents a partial successful export',async()=>{const {text,data}=await fixture(4);await assert.rejects(exportBook(repo,data,{fetcher:async url=>response(url.includes('CH003_')?'错误文字':text)}),error=>error.code==='hash_mismatch');});
test('cancelling an export does not produce a download result',async()=>{const {data}=await fixture();const controller=new AbortController();controller.abort();await assert.rejects(exportBook(repo,data,{signal:controller.signal}),error=>error.name==='AbortError');});
test('request gate makes previous novel responses stale and aborts them',()=>{const gate=new RequestGate();const first=gate.start();const next=gate.start();assert.equal(first.current(),false);assert.equal(first.signal.aborted,true);assert.equal(next.current(),true);gate.cancel();assert.equal(next.current(),false);});
test('shelf deduplicates repositories and ignores unsafe saved URLs',()=>{const storage={getItem:()=>JSON.stringify([{url:repo.url,title:'甲'},{url:repo.url,title:'乙'},{url:'javascript:bad'}])};const shelf=readShelf(storage);assert.equal(shelf.length,1);assert.equal(shelf[0].title,'乙');});
test('corrupt or unavailable storage returns a safe empty shelf',()=>{assert.deepEqual(readShelf({getItem:()=>'{bad'}),[]);assert.deepEqual(readShelf({getItem:()=>{throw new Error('denied');}}),[]);});
test('prompts carry selected novel and stop before automatic topic selection',()=>{const prompt=researchPrompt(repo,'fanqie');assert.ok(prompt.includes(repo.url));assert.ok(prompt.includes('番茄小说'));assert.ok(prompt.includes('5—10'));assert.ok(prompt.includes('等待我选题'));assert.ok(startupPrompt(repo).includes('当前先不写正文'));});
test('continuation instructions read current files and preserve the current task',async()=>{const {data}=await fixture();data.task={phase:'WAITING_NEXT',label:'等待下一章',next_action:'执行合成第六章任务'};const prompt=continuationPrompt(repo,data);for(const value of [repo.url,'book/state.json','book/接管记录.md','章后记录','执行合成第六章任务','无需阅读密钥'])assert.ok(prompt.includes(value));assert.ok(!prompt.includes('导入本书钥匙'));});
test('export filename excludes path and reserved characters',()=>{assert.equal(safeFilename('A/B:C?'), 'ABC.txt');});
test('100 chapters can be assembled from a consistent 900000-character fixture',async()=>{const text='验'.repeat(9000);const hash=await sha256(text);const {data}=await fixture(100);for(const chapter of data.chapters){chapter.sha256=hash;chapter.text_path=`book/chapters/CH${String(chapter.number).padStart(3,'0')}_${hash}.txt`;chapter.han_count=9000;}const result=await exportBook(repo,data,{fetcher:async()=>response(text)});assert.equal(result.chapters,100);assert.equal((result.text.match(/验/g)||[]).length,900000);});

async function privateFixture(count=1){
  const {text,data}=await fixture(count);const bytes=crypto.getRandomValues(new Uint8Array(32));
  const keyHash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
  const key={format:'xiaoshuo-reader-key-v1',book_id:data.book.id,key_id:keyHash,key_base64:Buffer.from(bytes).toString('base64')};
  const body={format:'xiaoshuo-private-book-v1',files:Object.fromEntries(data.chapters.map(c=>[c.text_path.slice(5),text]))};
  const plain=canonicalJSON(body);const value={format:body.format,public:{book_id:data.book.id},key_id:keyHash,plain_sha256:await sha256(plain),plain_bytes:Buffer.byteLength(plain),payload_codec:'gzip-utf8-json'};
  const nonce=crypto.getRandomValues(new Uint8Array(12));const ck=await crypto.subtle.importKey('raw',bytes,'AES-GCM',false,['encrypt']);
  const cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce,additionalData:new TextEncoder().encode(value.format+'\0'+canonicalJSON(value)),tagLength:128},ck,gzipSync(plain));
  value.nonce_base64=Buffer.from(nonce).toString('base64');value.ciphertext_base64=Buffer.from(cipher).toString('base64');
  const encoded=JSON.stringify(value);const hash=await sha256(encoded);
  for(const c of data.chapters)Object.assign(c,{storage:'encrypted_book_v1',vault_path:`vault/book_${hash}.json`,vault_sha256:hash,book_id:data.book.id,key_id:keyHash});
  return {text,data,key,encoded,value};
}
test('locked chapters never request ciphertext or plaintext',async()=>{const f=await privateFixture();let calls=0;await assert.rejects(fetchChapter(repo,f.data.chapters[0],{fetcher:async()=>{calls++;return response(f.encoded);}}),e=>e.code==='key_required');assert.equal(calls,0);});
test('private chapter decrypts locally and fetch options never contain key',async()=>{const f=await privateFixture();assert.equal(await fetchChapter(repo,f.data.chapters[0],{key:f.key,fetcher:async(url,options)=>{assert.match(url,/\/vault\/book_/);assert.equal(options.key,undefined);assert.ok(!JSON.stringify(options).includes(f.key.key_base64));return response(f.encoded);}}),f.text);});
test('a key from another novel is rejected',async()=>{const f=await privateFixture();await assert.rejects(validateReaderKey(f.key,'other-book'),e=>e.code==='invalid_key');});
test('private ciphertext tampering is rejected before display',async()=>{const f=await privateFixture();await assert.rejects(decryptPrivateBook(f.encoded+' ',f.key,f.data.chapters[0]),e=>e.code==='hash_mismatch');});
test('authenticated header cannot be edited by recomputing public SHA',async()=>{const f=await privateFixture();f.value.public.title='篡改';const encoded=JSON.stringify(f.value);const c={...f.data.chapters[0],vault_sha256:await sha256(encoded)};await assert.rejects(decryptPrivateBook(encoded,f.key,c),e=>e.code==='decrypt_failed');});
test('private whole-book export decrypts each vault once and preserves order',async()=>{const f=await privateFixture(7);let calls=0;const exported=await exportBook(repo,f.data,{key:f.key,fetcher:async()=>{calls++;return response(f.encoded);}});assert.equal(calls,1);assert.equal(exported.chapters,7);assert.ok(exported.text.indexOf('第1章')<exported.text.indexOf('第7章'));});
test('private manifest refuses arbitrary vault URLs',async()=>{const f=await privateFixture();f.data.chapters[0].vault_path='https://evil.test/data';assert.throws(()=>validateManifest(f.data,repo));});
