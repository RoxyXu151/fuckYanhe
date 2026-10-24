const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../src/yanhe_sentinel/web/static/workspace.js'),'utf8');
function app(){
  const items=[{id:'old',source:'yanhe',request:{course_id:100,session_id:1,video_id:1},status:'completed',event_count:3,updated_at:1,created_at:1},
    {id:'retry',source:'yanhe',request:{course_id:'100',session_id:'1',video_id:'1'},status:'failed',event_count:1,updated_at:3,created_at:3},
    {id:'second',source:'yanhe',request:{course_id:'100',session_id:'2',video_id:'1'},status:'completed',event_count:2,updated_at:2,created_at:2},
    {id:'other-course',source:'yanhe',request:{course_id:'101',session_id:'2',video_id:'1'},status:'partial',event_count:1,updated_at:4,created_at:4},
    {id:'local',source:'local',request:{},status:'completed',event_count:1,updated_at:5,created_at:5}];
  const calls=[],output={innerHTML:''};
  const ctx=vm.createContext({state:{jobs:items,library:[{id:'100',title:'同名课程',total_sessions:2},{id:'101',title:'同名课程',total_sessions:1}],historyQuery:'',historyStatus:'all',historyCourse:'all'},
    ui:{t:s=>s,message:s=>s,language:'zh-CN'},$:()=>output,esc:s=>String(s??''),
    jobBadge:j=>j.status,document:{querySelectorAll:()=>[]},
    openJob:id=>calls.push(['job',id]),calls,output});
  vm.runInContext(source+';openCourseReport=id=>calls.push(["course",id]);',ctx);
  return ctx;
}
test('one course entry across recordings, retries, numeric IDs; completed results win',()=>{
  const ctx=app(),entries=vm.runInContext('reportEntries()',ctx);
  assert.equal(entries.length,3);
  const course=entries.find(e=>e.course_id==='100');
  assert.equal(course.event_count,5);assert.equal(course.completed_sessions,2);assert.equal(course.status,'completed');
  assert.equal(entries.filter(e=>e.title==='同名课程').length,2,'different official IDs must not merge');
});
test('history lists course reports once and routes to the canonical course report',()=>{
  const ctx=app();vm.runInContext('renderHistoryRows();openReportEntry("course:100");openReportEntry("job:local")',ctx);
  assert.equal((ctx.output.innerHTML.match(/data-report-entry="course:100"/g)||[]).length,1);
  assert(!ctx.output.innerHTML.includes('data-job="retry"'));
  assert.equal(JSON.stringify(ctx.calls),JSON.stringify([['course','100'],['job','local']]));
  vm.runInContext('state.historyCourse="100";renderHistoryRows()',ctx);
  assert(!ctx.output.innerHTML.includes('course:101'));
});
