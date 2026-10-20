import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

STATIC=Path(__file__).resolve().parents[1]/'src/yanhe_sentinel/web/static'


def test_ui_catalog_covers_explicit_translation_keys():
    dictionary=json.loads((STATIC/'en.json').read_text('utf8'))
    keys=set()
    for name in ('app.js','workspace.js','study.js'):
        source=(STATIC/name).read_text('utf8')
        keys.update(json.loads(match) for match in re.findall(r'ui\.t\(("(?:[^"\\]|\\.)*")\)',source))
        keys.update(re.findall(r"ui\.t\('([^']*)'\)",source))
    assert keys-set(dictionary)==set()
    assert all(isinstance(value,str) and value for value in dictionary.values())


def test_language_theme_storage_and_live_labels():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the frontend preference regression test')
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const root=process.argv[1],source=fs.readFileSync(root+'/i18n.js','utf8'),dictionary=JSON.parse(fs.readFileSync(root+'/en.json','utf8'));
const storage=new Map();let denied=false;
function launch(){
 const text={textContent:'课程库',parentElement:{closest:()=>null}},title={textContent:'延河课堂 · 重要事项雷达'};
 const language={},theme={},placeholder={attributes:{placeholder:'选择文件'},getAttribute(n){return this.attributes[n];},setAttribute(n,v){this.attributes[n]=v;}};
 let walked=false;const media={matches:false,addEventListener(event,callback){this.changed=callback;}};
 const document={body:{},documentElement:{dataset:{},style:{}},title:title.textContent,
 createTreeWalker(){return {nextNode(){if(walked)return false;walked=true;this.currentNode=text;return true;}};},
 querySelector(s){return s==='title'?{firstChild:title}:s==='#ui-language'?language:theme;},querySelectorAll(){return [placeholder];}};
 const context=vm.createContext({document,NodeFilter:{SHOW_TEXT:4},navigator:{language:'zh-CN'},
 localStorage:{getItem(k){if(denied)throw Error('disabled');return storage.get(k);},setItem(k,v){if(denied)throw Error('disabled');storage.set(k,v);}},
 matchMedia:()=>media,fetch:async()=>({ok:true,json:async()=>dictionary})});
 vm.runInContext(source+';this.testUI=ui;',context);return {ui:context.testUI,document,language,theme,text,media,placeholder};
}
(async()=>{
 let page=launch();await page.ui.ready;
 const labels=page.ui.labels({done:'已完成'});assert.equal(labels.done,'已完成');
 page.language.value='en';page.language.onchange();assert.equal(page.text.textContent,'Course library');
 assert.equal(page.placeholder.attributes.placeholder,'Choose file');assert.equal(labels.done,'Completed');assert.equal(page.document.documentElement.lang,'en');
 page.theme.value='dark';page.theme.onchange();assert.equal(page.document.documentElement.dataset.theme,'dark');
 page.media.matches=false;page.media.changed();assert.equal(page.document.documentElement.dataset.theme,'dark');
 page=launch();await page.ui.ready;assert.equal(page.text.textContent,'Course library');assert.equal(page.theme.value,'dark');
 page.theme.value='system';page.theme.onchange();page.media.matches=true;page.media.changed();assert.equal(page.document.documentElement.dataset.theme,'dark');
 page.media.matches=false;page.media.changed();assert.equal(page.document.documentElement.dataset.theme,'light');
 page.language.value='zh-CN';page.language.onchange();assert.equal(page.text.textContent,'课程库');assert.equal(page.placeholder.attributes.placeholder,'选择文件');
 assert.equal(page.ui.t('私有课程原文'),'私有课程原文');
 denied=true;page=launch();await page.ui.ready;page.theme.value='light';page.theme.onchange();assert.equal(page.document.documentElement.dataset.theme,'light');
 console.log('Language, live labels, reversible placeholders, theme, persistence and blocked-storage checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script,str(STATIC)],capture_output=True,text=True,encoding='utf8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
