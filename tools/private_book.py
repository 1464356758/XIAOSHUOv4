#!/usr/bin/env python3
"""加密保存整部书的正文、记忆及历史；公开进度不含正文引文。"""
from __future__ import annotations
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

import novel_state as ns

FORMAT = 'xiaoshuo-private-book-v1'
KEY_FORMAT = 'xiaoshuo-reader-key-v1'
MAX_PLAIN = 80_000_000

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def sha(data):
    return hashlib.sha256(data).hexdigest()

def valid_relative(value):
    return (isinstance(value, str) and bool(value) and '\\' not in value
            and not PurePosixPath(value).is_absolute()
            and all(x not in ('', '.', '..') for x in value.split('/')))

def aes():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ns.WorkspaceError('解锁或封存需安装Python cryptography；只读公开进度无需此依赖。') from exc
    return AESGCM

def read_key(path, book_id):
    value = ns.read_json(path)
    ns.require(value.get('format') == KEY_FORMAT and value.get('book_id') == book_id, '钥匙不属于这部书。')
    try:
        key = base64.b64decode(value['key_base64'], validate=True)
    except (ValueError, KeyError) as exc:
        raise ns.WorkspaceError('钥匙格式错误。') from exc
    ns.require(len(key) == 32 and sha(key) == value.get('key_id'), '钥匙格式错误。')
    return key

def make_key(path, book_id):
    path = Path(path)
    ns.require(not path.exists(), '不覆盖已有钥匙。')
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    value = dict(format=KEY_FORMAT, book_id=book_id, key_id=sha(key),
                 key_base64=base64.b64encode(key).decode(),
                 instruction='这是本书私密阅读与续写钥匙。网页导入此文件；GPT用此文件解锁仓库book/private_storage.json所指的最新加密稿。不要上传到公开仓库。')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return {'created': True, 'book_id': book_id, 'key_id': value['key_id']}

def projection(root):
    state = ns.load_state(root)
    ns.verify_files(root, state)
    status = ns.status(root)
    status['unresolved_reviews'] = [{**{k: r[k] for k in ('from', 'to', 'decision')},
                                      'conclusion': '具体问题位于私密回看记录，请解锁。'}
                                     for r in status['unresolved_reviews']]
    return dict(book_id=state['book_id'], title=state['title'], revision=state['revision'],
                state_sha256=ns.digest((Path(root) / 'state.json').read_text(encoding='utf-8')),
                config={k: state['config'][k] for k in ('target_chapters', 'target_han')},
                inputs={k: {f: v[f] for f in ('kind', 'path', 'sha256')} for k, v in state['inputs'].items()},
                chapters=[dict(number=int(n), **item) for n, item in sorted(state['chapters'].items(), key=lambda x: int(x[0]))],
                progress={k: status[k] for k in ('adopted_chapters', 'actual_han', 'completed', 'needs_revalidation',
                                                'reviewed_until', 'window_review_due', 'unresolved_reviews')})

def aad(envelope):
    return (FORMAT + '\0' + canonical({k: envelope[k] for k in
            ('format', 'public', 'key_id', 'plain_sha256', 'plain_bytes', 'payload_codec')})).encode()

def read_public(project):
    project = Path(project)
    pointer = ns.read_json(project / 'book/private_storage.json')
    ns.require(pointer.get('format') == FORMAT and re.fullmatch(r'vault/book_[a-f0-9]{64}\.json', pointer.get('path', '')), '私密稿指针无效。')
    path = ns.inside(project, pointer['path'])
    data = path.read_bytes()
    ns.require(sha(data) == pointer.get('sha256'), '加密稿文件哈希不符。')
    value = json.loads(data)
    ns.require(value.get('format') == FORMAT and value.get('payload_codec') == 'gzip-utf8-json', '加密稿格式无效。')
    public = value['public']
    ns.require(public['book_id'] == pointer.get('book_id') and public['revision'] == pointer.get('revision'), '加密稿版本身份不符。')
    ns.require(value['key_id'] == pointer.get('key_id') and re.fullmatch('[a-f0-9]{64}', value['key_id']), '加密稿钥匙标识不符。')
    chapters = public['chapters']
    ns.require([c['number'] for c in chapters] == list(range(1, len(chapters) + 1)), '私密稿章节不连续。')
    ns.require(public['progress']['adopted_chapters'] == len(chapters)
               and public['progress']['actual_han'] == sum(c['han_count'] for c in chapters), '公开进度与加密稿目录不符。')
    return pointer, value

def pack(project, source, key_file):
    project, source = Path(project).resolve(), Path(source).resolve()
    ns.require(not source.is_relative_to(project) and not Path(key_file).resolve().is_relative_to(project), '私密工作副本和钥匙必须位于公开项目目录之外。')
    ns.require(not (source / 'private_storage.json').exists(), '先解锁实际工作副本，不能封存公开占位目录。')
    public = projection(source)
    key = read_key(key_file, public['book_id'])
    current = project / 'book/private_storage.json'
    if current.exists():
        old, previous = read_public(project)
        ns.require(old['book_id'] == public['book_id'] and old['key_id'] == sha(key), '不能替换其他书或静默换钥匙。')
        ns.require(public['revision'] >= old['revision'], '不能用旧工作副本覆盖新的加密稿。')
        parent = source / 'private_parent.json'
        ns.require(parent.exists() and ns.read_json(parent).get('vault_sha256') == old['sha256'], '工作副本不是从当前加密稿恢复的，先核对最新版本。')
    else:
        baseline = ns.load_state(project / 'book')
        ns.require(baseline['book_id'] == public['book_id'] and not baseline['chapters'], '首次封存应从本书未公开正文的状态迁入。')
    files = {}
    for file in sorted(source.rglob('*')):
        if file.is_file():
            relative = file.relative_to(source).as_posix()
            if relative == 'private_parent.json':
                continue
            ns.require(valid_relative(relative) and not file.is_symlink(), '不接受符号链接或越界路径。')
            if relative.endswith('.lock') or file.name.startswith('.write-lock'):
                raise ns.WorkspaceError('封存时存在写入锁；先确认写入任务已结束。')
            files[relative] = file.read_text(encoding='utf-8')
    plain = canonical({'format': FORMAT, 'files': files}).encode()
    ns.require(len(plain) <= MAX_PLAIN, '私密稿超过当前容量限制。')
    value = dict(format=FORMAT, public=public, key_id=sha(key), plain_sha256=sha(plain),
                 plain_bytes=len(plain), payload_codec='gzip-utf8-json')
    nonce = os.urandom(12)
    value['nonce_base64'] = base64.b64encode(nonce).decode()
    value['ciphertext_base64'] = base64.b64encode(aes()(key).encrypt(nonce, gzip.compress(plain, mtime=0), aad(value))).decode()
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    digest = ns.digest(encoded)
    path = f'vault/book_{digest}.json'
    ns.immutable_text(ns.inside(project, path), encoded)
    pointer = dict(format=FORMAT, path=path, sha256=digest, book_id=public['book_id'],
                   revision=public['revision'], key_id=value['key_id'])
    ns.atomic_json(project / 'book/private_storage.json', pointer)
    ns.atomic_json(source / 'private_parent.json', {'vault_sha256': digest})
    return {'sealed': True, **pointer, 'chapters': len(public['chapters']), 'actual_han': public['progress']['actual_han']}

def decrypt(project, key_file):
    pointer, envelope = read_public(project)
    key = read_key(key_file, pointer['book_id'])
    ns.require(sha(key) == envelope['key_id'], '钥匙不匹配。')
    try:
        zipped = aes()(key).decrypt(base64.b64decode(envelope['nonce_base64'], validate=True),
                                   base64.b64decode(envelope['ciphertext_base64'], validate=True), aad(envelope))
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(zipped)) as f:
            plain = f.read(MAX_PLAIN + 1)
    except Exception as exc:
        raise ns.WorkspaceError('私密稿认证或解压失败；钥匙错误或文件被改动。') from exc
    ns.require(len(plain) == envelope['plain_bytes'] <= MAX_PLAIN and sha(plain) == envelope['plain_sha256'], '私密稿内容哈希不符。')
    body = json.loads(plain)
    ns.require(body.get('format') == FORMAT and isinstance(body.get('files'), dict), '私密稿内容无效。')
    ns.require(all(valid_relative(k) and isinstance(v, str) for k, v in body['files'].items()), '私密稿包含越界路径。')
    return envelope, body['files']

def unlock(project, key_file, out):
    out = Path(out).resolve()
    ns.require(not out.exists(), '解锁只写入一个新的私密目录，避免覆盖工作。')
    envelope, files = decrypt(project, key_file)
    # 先在临时目录核对全部正文/记录引用，成功后才交付工作副本。
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='novel-verify-', dir=out.parent) as tmp:
        root = Path(tmp)
        for name, text in files.items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
            p.chmod(0o600)
        ns.require(projection(root) == envelope['public'], '私密状态与公开目录不一致。')
        import shutil
        shutil.copytree(root, out)
        out.chmod(0o700)
    ns.atomic_json(out / 'private_parent.json', {'vault_sha256': ns.read_json(Path(project) / 'book/private_storage.json')['sha256']})
    return {'unlocked': True, 'book_id': envelope['public']['book_id'], 'revision': envelope['public']['revision'], 'chapters': len(envelope['public']['chapters'])}

def publish_plain(project, key_file, author_request):
    """按作者实际指令恢复直接读取；从当前密文认证恢复，保留身份和全部版本。"""
    ns.require(bool(author_request.strip()), '需要记录作者取消阅读密钥的实际指令。')
    project = Path(project).resolve()
    root = project / 'book'
    with ns.writer_lock(root):
        pointer, _ = read_public(project)
        envelope, files = decrypt(project, key_file)
        ns.require(envelope['public']['book_id'] == pointer['book_id']
                   and envelope['public']['revision'] == pointer['revision'], '迁移期间加密稿改变，请重读。')
        secret = base64.b64encode(read_key(key_file, pointer['book_id'])).decode()
        ns.require(all(secret not in text and '"key_base64"' not in text for text in files.values()),
                   '书稿夹有密钥文件，不能发布。')
        ns.require('private_parent.json' not in files and 'private_storage.json' not in files,
                   '书稿夹有存储控制指针，不能直接发布。')
        with tempfile.TemporaryDirectory(prefix='novel-public-verify-') as tmp:
            stage = Path(tmp)
            for name, text in files.items():
                ns.atomic_text(stage / name, text)
            ns.require(projection(stage) == envelope['public'], '恢复的正式状态与密文目录不符。')
        # 先检查已有不可变文件，避免用本地遗留物覆盖正式版本。
        mutable = {'state.json', '接管记录.md'}
        for name, text in files.items():
            target = ns.inside(root, name)
            if name not in mutable and name != 'workflow.json' and target.exists():
                ns.require(target.read_text(encoding='utf-8') == text, '已有正式产物冲突：' + name)
        for name, text in files.items():
            if name in ('state.json', 'workflow.json'):
                continue
            target = ns.inside(root, name)
            if name in mutable:
                ns.atomic_text(target, text)
            else:
                ns.immutable_text(target, text)
        ns.require(read_public(project)[0] == pointer, '迁移期间加密稿改变，请重读。')
        ns.atomic_text(root / 'state.json', files['state.json'])
        ns.atomic_json(root / 'storage_migration.json', dict(
            mode='plain_files', requires_reader_key=False,
            author_request=author_request.strip(), book_id=pointer['book_id'],
            revision=pointer['revision'], previous_vault_path=pointer['path'],
            previous_vault_sha256=pointer['sha256'],
            note='现行来源是book/state.json及其引用文件。历史vault仅保留作旧版本档案，不是当前阅读入口。'))
        (root / 'private_storage.json').unlink()
        result = ns.status(root)
        ns.require(result['revision'] == envelope['public']['revision']
                   and result['adopted_chapters'] == envelope['public']['progress']['adopted_chapters'],
                   '恢复后的进度不一致。')
    return dict(published_plain=True, book_id=result['book_id'], revision=result['revision'],
                chapters=result['adopted_chapters'], actual_han=result['actual_han'],
                requires_reader_key=False)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', default='.')
    s = p.add_subparsers(dest='command', required=True)
    a = s.add_parser('keygen'); a.add_argument('--out', required=True); a.add_argument('--book-id', required=True)
    s.add_parser('status')
    a = s.add_parser('pack'); a.add_argument('--source', required=True); a.add_argument('--key-file', required=True)
    a = s.add_parser('unlock'); a.add_argument('--key-file', required=True); a.add_argument('--out', required=True)
    a = s.add_parser('publish-plain'); a.add_argument('--key-file', required=True); a.add_argument('--author-request', required=True)
    args = p.parse_args()
    try:
        if args.command == 'keygen': result = make_key(args.out, args.book_id)
        elif args.command == 'pack': result = pack(args.project, args.source, args.key_file)
        elif args.command == 'unlock': result = unlock(args.project, args.key_file, args.out)
        elif args.command == 'publish-plain': result = publish_plain(args.project, args.key_file, args.author_request)
        elif not (Path(args.project) / 'book/private_storage.json').exists():
            result = dict(ns.status(Path(args.project) / 'book'), encrypted=False,
                          note='正文和记录可直接读取，不需要阅读密钥。')
        else:
            pointer, data = read_public(args.project)
            result = dict(data['public']['progress'], book_id=pointer['book_id'], revision=pointer['revision'], encrypted=True,
                          note='这是经密文哈希核对的公开进度；阅读全文与继续写作需要私密钥匙。')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ns.WorkspaceError, OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f'未完成：{exc}\n')

if __name__ == '__main__': main()
