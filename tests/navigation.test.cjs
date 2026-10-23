const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function app(){
  const ctx=vm.createContext({URLSearchParams,window:{addEventListener(){}},
    state:{view:'library',savedCourse:'os',jobs:[{id:'job'}]},studyState:{jobId:'old',data:{}},
    location:{hash:'#/library'},savedCourse:id=>['os','language'].includes(id)?{id}:null,
    ui:{t:s=>s},$:()=>null,saveStudyProgress(){},toast(){}});
  vm.runInContext(`function showView(v){state.view=v;writeRoute(v);}
    async function openJob(id){state.currentJob=id;showView('report');}
    async function openStudy(id){studyState.jobId=id;showView('study');}
    const history={pushState(a,b,p){location.hash=p;},replaceState(a,b,p){location.hash=p;}};`,ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/yanhe_sentinel/web/static/product.js'),'utf8'),ctx);
  return ctx;
}
test('browser back to course list clears old course selection',async()=>{
  const c=app();await vm.runInContext('restoreRoute()',c);
  assert.equal(c.state.savedCourse,null);assert.equal(c.state.view,'library');
});
test('report returns to entry course even after another course was selected',()=>{
  const c=app();vm.runInContext(`state.savedCourse='language';rememberDetailOrigin('job');
    state.view='report';state.savedCourse='os';returnFromDetail();`,c);
  assert.equal(c.state.savedCourse,'language');assert.equal(c.location.hash,'#/course/language');
});
test('study return context survives refresh',async()=>{
  const c=app();c.location.hash='#/study/job?from=course%2Flanguage';
  await vm.runInContext('restoreRoute()',c);vm.runInContext('returnFromDetail()',c);
  assert.equal(c.state.savedCourse,'language');assert.equal(c.state.view,'library');
});
test('generic study entry clears previously opened course',()=>{
  const c=app();vm.runInContext('openStudyPicker()',c);
  assert.equal(c.studyState.jobId,null);assert.equal(c.location.hash,'#/study');
});
test('study root restores a list rather than a stale lecture',async()=>{
  const c=app();c.location.hash='#/study';await vm.runInContext('restoreRoute()',c);
  assert.equal(c.studyState.jobId,null);assert.equal(c.state.view,'study');
});
test('history origin is retained across report to study switch',()=>{
  const c=app();vm.runInContext(`state.view='history';rememberDetailOrigin('job');
    state.view='report';rememberDetailOrigin('job');state.view='study';studyState.jobId='job';
    writeRoute('study');`,c);
  assert.equal(c.location.hash,'#/study/job?from=history');
  vm.runInContext('returnFromDetail()',c);assert.equal(c.state.view,'history');
});
