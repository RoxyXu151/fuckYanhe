"""Exercise edits made during an asynchronous save and task-selection recovery."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_product_async_edits_and_progress_guards():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for frontend regression checks')
    source = Path(__file__).resolve().parents[1] / 'src/yanhe_sentinel/web/static/product.js'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const elements={};
for(const key of ['#study-personal','#personal-note','#save-personal-note','#note-save-state','#bookmark-current'])elements[key]={value:''};
const state={features:{product:true},jobs:[]},studyState={jobId:'lecture-a'};
let resolveSave,calls=[];
const context=vm.createContext({state,studyState,
 window:{addEventListener(){}},document:{querySelectorAll:()=>[]},
 $:selector=>elements[selector],ui:{t:value=>value},esc:value=>value,timestamp:String,
 terminal:new Set(['completed','failed','canceled']),toast(){},
 api:async(path,options)=>{calls.push({path,options});if(path.endsWith('study-note'))return new Promise(resolve=>{resolveSave=resolve;});return {position:42,speed:1.5};}
});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8')+`;this.testProduct={productState,renderStudyRecords,enhanceQueue,saveStudyProgress};`,context);
const p=context.testProduct;
(async()=>{
 p.productState.records={position:0,note:'original',bookmarks:[]};p.productState.recordJob='lecture-a';
 p.renderStudyRecords();
 elements['#personal-note'].value='first edit';elements['#personal-note'].oninput({target:{value:'first edit'}});
 const pending=elements['#save-personal-note'].onclick();
 elements['#personal-note'].value='newer edit';elements['#personal-note'].oninput({target:{value:'newer edit'}});
 resolveSave({note:'first edit'});await pending;
 assert.equal(p.productState.drafts['lecture-a'].text,'newer edit');
 assert.equal(p.productState.drafts['lecture-a'].dirty,true);
 assert.equal(elements['#note-save-state'].textContent,'尚未保存');
 p.productState.retrySelection=new Set(['still-failed','already-done','removed']);
 state.jobs=[{id:'still-failed',status:'failed'},{id:'already-done',status:'completed'}];
 p.enhanceQueue();assert.deepEqual([...p.productState.retrySelection],['still-failed']);
 elements['#study-player']={currentTime:42,playbackRate:1.5};
 p.saveStudyProgress(true);assert.equal(calls.length,1,'opening a player must not reset saved progress');
 p.productState.playbackTouchedJob='lecture-a';p.saveStudyProgress(true);await Promise.resolve();
 assert.equal(calls.length,2);assert.equal(calls[1].options.body.position,42);assert.equal(calls[1].options.keepalive,true);
 let resolveGeneration;
 const studyContext=vm.createContext({state:{view:'study'},configured:()=>true,
   api:()=>new Promise(resolve=>{resolveGeneration=resolve;}),ui:{t:value=>value},toast(){}});
 vm.runInContext(fs.readFileSync(process.argv[1].replace('product.js','study.js'),'utf8')+
   ';renderStudyStatus=()=>{};renderStudyContent=()=>{};this.testStudy={studyState,generateStudy};',studyContext);
 const study=studyContext.testStudy;
 study.studyState.jobId='lecture-a';study.studyState.data={status:'idle'};
 const generation=study.generateStudy();
 study.studyState.jobId='lecture-b';study.studyState.data={status:'completed',title:'keep lecture b'};
 resolveGeneration({status:'queued',title:'lecture a'});await generation;
 assert.equal(study.studyState.data.title,'keep lecture b','late generation response must not overwrite another lecture');
 console.log('Concurrent editing, stale selections and playback save guards passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script, str(source)], capture_output=True,
                            text=True, encoding='utf8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
