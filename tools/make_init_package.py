#!/usr/bin/env python3
"""生成空的新书母版；绝不打包本书身份、进度或正文。"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def build(root=ROOT,out=None):
    root=Path(root).resolve();out=Path(out or root/'初始化系统包.zip').resolve()
    names=['AGENTS.md','README.md','启动新书.txt','每日一章指令.txt','docs/连载冻结与发布前检查.md','执行规程.md','工具使用与数据格式.md','平台研究与选题.md','system_config.json','platforms.json','.gitignore','docs/线上同步协议.md','docs/正文直接读取.md','docs/私密稿保存.md','requirements-private.txt']
    files={name:(root/name).read_bytes() for name in names}
    for folder in ('tools','templates','tests'):
        for path in sorted((root/folder).glob('*')):
            if path.is_file() and path.suffix in ('.py','.json','.md','.txt'):
                files[path.relative_to(root).as_posix()]=path.read_bytes()
    config=json.loads((root/'project_config.json').read_text())
    config['project_name']='新小说项目';config['authority']={'mode':'github','repository_url':'','branch':'main'}
    config['writing']['platform']=None;config['writing']['genre']=None
    config['storage']={'mode':'plain_files','requires_reader_key':False}
    files['project_config.json']=(json.dumps(config,ensure_ascii=False,indent=2)+'\n').encode()
    files['README.md']='''# 新小说初始化母版\n\n本包只是一套空系统，没有上一部小说的身份、进度、正文或选题。\n\n1. 将包内文件复制到新GitHub仓库根目录。\n2. 把新仓库链接与《启动新书.txt》发送给具备GitHub读写和代码执行能力的GPT。\n3. 先选择平台，再看规则、榜单和5—10个原创选题。选定后规划和写作。\n4. 在同一个小说阅读器里添加新仓库链接，即可切换查看。\n5. 新书默认采用即冻结；每章先完成发布前检查和到期全文回看。\n6. 之后将《每日一章指令.txt》发给GPT，每次只完成一章；由作者实际发布，回执单独登记。\n\n已有作品请使用迁移方案，不用本包覆盖清空。\n'''.encode()
    files['book/README.md']='# 空工作区\n\n由GPT运行bootstrap生成这本书的新身份。正文与记录直接保存，无需阅读密钥。不要复制旧书state.json。\n'.encode()
    files['research/README.md']='# 尚未研究\n\n选定平台后重新核查官方规则和榜单。\n'.encode()
    empty={'schema_version':1,'system_version':'4.2-serial','initialized':False,'book':{'id':None,'title':'新小说（待初始化）','platform':None,'selected_topic_id':None},'source':{'repository_url':None,'branch':'main','generated_at':None,'state_revision':0,'workflow_revision':0},'progress':{'target_chapters':100,'adopted_chapters':0,'target_han':900000,'actual_han':0,'completed':False,'needs_revalidation':[]},'task':{'phase':'NOT_INITIALIZED','label':'等待初始化','chapter':1,'detail':'复制启动指令交给GPT，建立这本书的新身份。','next_action':'读取启动新书.txt，填入新仓库链接后bootstrap。','updated_at':None},'chapters':[],'topics':[],'research':None,'history':[]}
    files['online/index.json']=(json.dumps(empty,ensure_ascii=False,indent=2)+'\n').encode()
    if (root/'.github/workflows/validate.yml').exists():files['.github/workflows/validate.yml']=(root/'.github/workflows/validate.yml').read_bytes()
    assert not any(name.startswith(('book/chapters/','book/records/','book/candidates/')) or name in ('book/state.json','book/workflow.json') for name in files)
    manifest={'template_version':'4.2-serial','contains_novel_data':False,'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    files['INIT_MANIFEST.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
    out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):archive.writestr(name,data)
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        for name,sha in manifest['files'].items():assert hashlib.sha256(archive.read(name)).hexdigest()==sha
    return {'path':str(out),'files':len(files),'contains_novel_data':False,'bytes':out.stat().st_size}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out');args=p.parse_args();print(json.dumps(build(out=args.out),ensure_ascii=False,indent=2))
