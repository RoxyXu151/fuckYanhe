/* Persistent course library, cross-course batches and task navigation. */
function savedCourse(id){return (state.library||[]).find(c=>String(c.id)===String(id));}
// A job is a processing attempt; only a course is a report entry.
function reportEntries(){
  const groups=new Map(),entries=[];
  for(const job of state.jobs){
    const courseId=job.source==='yanhe'&&job.request?.course_id!=null?String(job.request.course_id):null;
    if(courseId){if(!groups.has(courseId))groups.set(courseId,[]);groups.get(courseId).push(job);}
    else entries.push({...job,entry_id:'job:'+job.id,course_id:null,course:'',job_id:job.id});
  }
  for(const [courseId,jobs] of groups){
    const course=savedCourse(courseId),recordings=new Map();
    for(const job of [...jobs].sort((a,b)=>b.updated_at-a.updated_at)){
      const key=JSON.stringify([String(job.request.session_id),String(job.request.video_id)]);
      const current=recordings.get(key);
      if(!current||current.status!=='completed'&&job.status==='completed')recordings.set(key,job);
    }
    const selected=[...recordings.values()],done=selected.filter(j=>j.status==='completed').length;
    const total=Math.max(course?.total_sessions||0,selected.length),active=jobs.some(j=>['running','queued'].includes(j.status));
    const status=total&&done===total?'completed':active?'running':done||selected.some(j=>j.status==='partial')?'partial':selected[0]?.status||'queued';
    entries.push({entry_id:'course:'+courseId,id:'course:'+courseId,course_id:courseId,job_id:null,
      title:course?.title||`${ui.t('课程')} ${courseId}`,course:'',status,
      completed_sessions:done,total_sessions:total,event_count:selected.reduce((n,j)=>n+(j.event_count||0),0),
      message:`${done} / ${total} ${ui.t('个课次已完成')}`,
      search_text:jobs.map(j=>`${j.title} ${j.message}`).join(' '),
      updated_at:Math.max(...jobs.map(j=>j.updated_at||0)),created_at:Math.min(...jobs.map(j=>j.created_at||0))});
  }
  return entries.sort((a,b)=>(b.updated_at||0)-(a.updated_at||0));
}
function openReportEntry(id){const entry=reportEntries().find(e=>e.entry_id===id);if(entry?.course_id)return openCourseReport(entry.course_id);if(entry)return openJob(entry.job_id);}
function reportEntryAttribute(entry){return `data-report-entry="${esc(entry.entry_id)}"`;}
function bindReportLinks(){document.querySelectorAll('[data-report-entry]').forEach(b=>b.onclick=()=>openReportEntry(b.dataset.reportEntry));document.querySelectorAll('[data-course-report]').forEach(b=>b.onclick=()=>openCourseReport(b.dataset.courseReport));}
function filteredCourses(){
  const query=state.libraryQuery.toLowerCase();
  return (state.library||[]).filter(c=>(state.libraryFilter==='archived'?c.archived:!c.archived)
    &&(state.libraryFilter!=='completed'||c.completed_sessions>0)
    &&(state.libraryFilter!=='pending'||c.pending_sessions>0)
    &&(state.libraryFilter!=='starred'||c.starred)
    &&`${c.title} ${c.professors.join(' ')} ${c.notes}`.toLowerCase().includes(query));
}
async function loadLibrary(){
  const data=await api('/api/library');state.library=data.items;
  if(state.view==='library')renderLibrary();
}
function bindJobLinks(){document.querySelectorAll('[data-job]').forEach(b=>b.onclick=()=>openJob(b.dataset.job));}
function renderLibrary(){
  writeRoute('library');
  if(state.savedCourse){renderSavedCourse();return;}
  const courses=state.library||[],done=courses.reduce((n,c)=>n+c.completed_sessions,0);
  const active=state.jobs.filter(j=>!terminal.has(j.status)).length;
  $('#main-content').innerHTML=heading((ui.t("课程库")),(state.features.remote_storage?ui.t("录像与缓存归档到远程服务器；课程索引、字幕和报告同时留在本机。课次数量以最近同步为准。"):ui.t("课程、已完成课次和报告保存在本机。课次数量以已保存或最近同步的列表为准。")),
    (`<button class="button secondary" data-action="classroom">${ui.t("添加 / 同步课程")}</button><button class="button secondary" data-action="local">${ui.t("导入本地文件")}</button>`))+
    `<div class="workspace-stats"><section><strong>${courses.filter(c=>!c.archived).length}</strong><span>${ui.t("在用课程")}</span></section><section><strong>${done}</strong><span>${ui.t("已完成课次")}</span></section><section><strong>${active}</strong><span>${ui.t("排队 / 处理中")}</span></section></div>
    ${!state.auth.connected?(`<p class="inline-notice">${ui.t("当前未连接延河课堂。已有报告可离线查看；同步录播或批量执行前，请先连接课堂。")}</p>`):''}
    <div class="toolbar"><label class="search-field">${ui.t("搜索课程")}<input id="library-search" placeholder="${ui.t("课程名称、教师或备注")}" value="${esc(state.libraryQuery)}"></label><label>${ui.t("课程筛选")}<select id="library-filter"><option value="active">${ui.t("在用课程")}</option><option value="pending">${ui.t("还有课次未完成")}</option><option value="completed">${ui.t("有已完成报告")}</option><option value="starred">${ui.t("已置顶")}</option><option value="archived">${ui.t("已归档")}</option></select></label></div>
    <section class="panel batch-bar"><label class="checkbox-label"><input id="library-select-all" type="checkbox">${ui.t("选择筛选结果")}</label><span id="batch-selected">${ui.t("已选")} ${state.selectedCourses.size} ${ui.t("门")}</span><label class="checkbox-label"><input id="skip-completed" type="checkbox" checked>${ui.t("跳过已完成课次")}</label><button class="button primary" id="batch-start" ${!state.auth.connected||!state.selectedCourses.size||state.batchBusy?'disabled':''}>${state.batchBusy?(ui.t("正在加入队列…")):(ui.t("一键分析所选课程"))}</button><button class="text-button" id="clear-course-selection">${ui.t("清空选择")}</button></section>
    <p class="option-hint">${ui.t("批量执行会刷新所选课程的录播列表，自动优先选择屏幕信号、使用视频原音轨，按顺序处理；一次最多 100 个课次。取消“跳过已完成”会重新处理并调用模型。需要独立话筒或画面辅助时，请进入单门课程选择。")}</p>
    <div id="library-results"></div><div id="batch-feedback" role="status" aria-live="polite"></div>`;
  bindActions();$('#library-filter').value=state.libraryFilter;
  $('#library-search').oninput=e=>{state.libraryQuery=e.target.value;renderLibraryRows();};
  $('#library-filter').onchange=e=>{state.libraryFilter=e.target.value;renderLibraryRows();};
  $('#library-select-all').onchange=e=>{filteredCourses().forEach(c=>e.target.checked?state.selectedCourses.add(c.id):state.selectedCourses.delete(c.id));renderLibraryRows();};
  $('#clear-course-selection').onclick=()=>{state.selectedCourses.clear();renderLibraryRows();};
  $('#batch-start').onclick=startCourseBatch;renderLibraryRows();
}
function renderLibraryRows(){
  const courses=filteredCourses(),target=$('#library-results');if(!target)return;
  target.innerHTML=courses.length?`<div class="table-wrap panel"><table class="library-table"><thead><tr><th>${ui.t("选择")}</th><th>${ui.t("课程")}</th><th>${ui.t("课次进度")}</th><th>${ui.t("任务")}</th><th>${ui.t("操作")}</th></tr></thead><tbody>${courses.map(c=>`<tr><td><input type="checkbox" data-course-select="${esc(c.id)}" aria-label="${ui.t("选择课程")} ${esc(c.title)}" ${state.selectedCourses.has(c.id)?'checked':''}></td><td><strong>${c.starred?'★ ':''}${esc(c.title)}</strong><small>${esc(c.professors.join(' · ')||(ui.t("教师信息未同步")))}</small></td><td><strong>${c.completed_sessions} / ${c.total_sessions} ${ui.t("已完成")}</strong><small>${c.pending_sessions} ${ui.t("个未完成课次 ·")} ${c.event_count} ${ui.t("个候选事项")}</small></td><td>${c.active_jobs?`<span class="badge neutral">${c.active_jobs} ${ui.t("个处理中")}</span>`:'—'}</td><td><div class="row-actions"><button class="button secondary" data-saved-course="${esc(c.id)}">${ui.t("查看课次与报告")}</button><button class="text-button" data-star-course="${esc(c.id)}">${c.starred?(ui.t("取消置顶")):(ui.t("置顶"))}</button><button class="text-button" data-archive-course="${esc(c.id)}">${c.archived?(ui.t("恢复")):(ui.t("归档"))}</button></div></td></tr>`).join('')}</tbody></table></div>`:
    (`<div class="empty"><h3>${ui.t("暂无符合条件的课程")}</h3><p>${ui.t("连接课堂后浏览课程会自动保存。已完成课次及报告也会自动归入课程库。")}</p><button class="button secondary" data-action="classroom">${ui.t("添加 / 同步课程")}</button></div>`);
  bindActions();document.querySelectorAll('[data-course-select]').forEach(input=>input.onchange=()=>{input.checked?state.selectedCourses.add(input.dataset.courseSelect):state.selectedCourses.delete(input.dataset.courseSelect);updateBatchSelection();});
  document.querySelectorAll('[data-saved-course]').forEach(b=>b.onclick=()=>{state.savedCourse=b.dataset.savedCourse;renderLibrary();});
  document.querySelectorAll('[data-star-course]').forEach(b=>b.onclick=()=>patchCourse(b.dataset.starCourse,{starred:!savedCourse(b.dataset.starCourse).starred}));
  document.querySelectorAll('[data-archive-course]').forEach(b=>b.onclick=()=>patchCourse(b.dataset.archiveCourse,{archived:!savedCourse(b.dataset.archiveCourse).archived}));
  updateBatchSelection();
}
function updateBatchSelection(){
  if(!$('#batch-selected'))return;
  $('#batch-selected').textContent=`${ui.t("已选")} ${state.selectedCourses.size} ${ui.t("门")}`;
  $('#batch-start').disabled=!state.auth.connected||!state.selectedCourses.size||state.batchBusy;
  const visible=filteredCourses(),all=$('#library-select-all');
  all.checked=visible.length>0&&visible.every(c=>state.selectedCourses.has(c.id));
  all.indeterminate=visible.some(c=>state.selectedCourses.has(c.id))&&!all.checked;
}
async function patchCourse(id,body){
  try{const data=await api(`/api/library/${encodeURIComponent(id)}`,{method:'PATCH',body});state.library=data.items;
    if(body.archived)state.selectedCourses.delete(id);renderLibrary();toast((ui.t("课程信息已保存到本机")));
  }catch(e){toast(e.message,true);}
}
function renderSavedCourse(){
  const course=savedCourse(state.savedCourse);if(!course){state.savedCourse=null;renderLibrary();return;}
  $('#main-content').innerHTML=(`<button class="back-button" id="back-library">${ui.t("← 返回课程库")}</button>`)+heading(course.title,
    `${course.professors.join(' · ')} · ${course.completed_sessions}/${course.total_sessions} ${ui.t("个课次已完成")}`,
    (`<button class="button primary" data-course-report="${esc(course.id)}">${ui.t("查看课程报告")}</button><button class="button secondary" id="refresh-saved-course">${ui.t("刷新官方课次 / 选择分析")}</button>`))+
    `<section class="panel course-notes"><label>${ui.t("课程备注")}<textarea id="course-notes" maxlength="2000" placeholder="${ui.t("记录课程安排、复核进度等")}">${esc(course.notes)}</textarea></label><button class="button secondary" id="save-course-notes">${ui.t("保存备注")}</button></section>
    <div class="table-wrap panel"><table class="library-table"><thead><tr><th>${ui.t("课次")}</th><th>${ui.t("处理状态")}</th><th>${ui.t("操作")}</th></tr></thead><tbody>${course.sessions.map(s=>`<tr><td><strong>${esc(s.title)}</strong><small>${esc(dateLabel(s.started_at))}</small></td><td>${s.status?jobBadge({status:s.status}):(`<span class="badge neutral">${ui.t("尚未处理")}</span>`)}${s.completed_job_id&&s.status!=='completed'?(`<small>${ui.t("保留有已完成版本")}</small>`):''}</td><td>${s.latest_job_id?`<button class="button secondary" data-task-job="${esc(s.latest_job_id)}">${ui.t("查看进度")}</button>`:'—'}${s.latest_job_id&&state.features.study?`<button class="text-button" data-study-job="${esc(s.completed_job_id||s.latest_job_id)}">${ui.t('听课')}</button>`:''}</td></tr>`).join('')||(`<tr><td colspan="3">${ui.t("尚未同步课次，请连接课堂后刷新。")}</td></tr>`)}</tbody></table></div>`;
  writeRoute('library');$('#back-library').onclick=()=>{state.savedCourse=null;renderLibrary();};
  $('#refresh-saved-course').onclick=()=>openCourse(course.id);
  $('#save-course-notes').onclick=()=>patchCourse(course.id,{notes:$('#course-notes').value});bindJobLinks();bindReportLinks();bindStudyLinks();bindTaskLinks();
}
async function startCourseBatch(){
  if(state.batchBusy)return;
  if(!configured()){openSettings();toast((ui.t("请先配置模型服务，再执行批量分析")));return;}
  const skip=$('#skip-completed').checked;state.batchBusy=true;updateBatchSelection();$('#batch-start').textContent=(ui.t("正在读取课程并加入队列…"));
  try{
    const result=await api('/api/batches',{method:'POST',body:{course_ids:[...state.selectedCourses],skip_completed:skip}});
    state.jobs=[...result.jobs,...state.jobs.filter(j=>!result.jobs.some(n=>n.id===j.id))];
    state.batchFeedback=`${ui.t("已加入")} ${result.jobs.length} ${ui.t("个课次，跳过")} ${result.skipped.length} ${ui.t("个已完成课次，")}${result.reused.length} ${ui.t("个课次已在队列。")}`;
    state.batchErrors=result.errors.map(e=>`${savedCourse(e.course_id)?.title||e.course_id}：${e.message}`);state.selectedCourses.clear();
    await loadLibrary();showView('queue');toast(state.batchFeedback);
  }catch(e){toast(e.message,true);}finally{state.batchBusy=false;if(state.view==='library')renderLibrary();}
}
function renderQueue(){
  const queueRank=j=>j.status==='running'?0:j.stage==='waiting_analysis'?1:2;
  const active=state.jobs.filter(j=>!terminal.has(j.status)).sort((a,b)=>queueRank(a)-queueRank(b)),incomplete=state.jobs.filter(j=>terminal.has(j.status)&&j.status!=='completed');
  $('#main-content').innerHTML=heading((ui.t("任务队列")),`${active.length} ${ui.t("个任务排队或处理中，")}${incomplete.length} ${ui.t("个任务可继续。")}`,
    (`<button class="button secondary" data-action="library">${ui.t("从课程库批量执行")}</button>`))+
    `<p class="muted">${ui.t("下载与分析独立排队，可同时运行；继续任务时仅补失败片段。")}</p>`+
    (state.batchFeedback?`<p class="inline-notice" role="status">${esc(state.batchFeedback)}</p>`:'')+
    (state.batchErrors?.length?`<div class="risk-note">${state.batchErrors.map(esc).join('<br>')}</div>`:'')+
    `<div class="queue-actions"><button class="button secondary" id="stop-queue" ${!active.length?'disabled':''}>${ui.t("停止全部待处理任务")}</button><button class="text-button" data-action="history">${ui.t("查看全部报告与记录")}</button></div><div class="job-list">${[...active,...incomplete].map(j=>`<section class="panel queue-card"><div><strong>${esc(j.title)}</strong>${jobBadge(j)}</div><p>${esc(stages[j.stage]||j.message)} ${j.total?esc(progressLabel(j)):''}</p><div class="row-actions"><button class="button secondary" data-job="${esc(j.id)}">${ui.t("查看")}${terminal.has(j.status)?(ui.t("报告")):(ui.t("进度"))}</button><button class="text-button" data-task-action="${esc(j.id)}">${terminal.has(j.status)?(ui.t("继续处理")):(ui.t("停止"))}</button></div></section>`).join('')||(`<div class="empty"><h3>${ui.t("目前没有待处理任务")}</h3><p>${ui.t("在课程库勾选多门课程，即可一键分析未完成课次。")}</p></div>`)}</div>`;
  bindActions();bindJobLinks();document.querySelectorAll('[data-task-action]').forEach(b=>b.onclick=async()=>{try{const job=state.jobs.find(j=>j.id===b.dataset.taskAction);const data=await api(`/api/jobs/${job.id}/${terminal.has(job.status)?'retry':'cancel'}`,{method:'POST'});state.jobs=state.jobs.map(j=>j.id===job.id?data:j);renderQueue();}catch(e){toast(e.message,true);}});
  if(state.features.product)enhanceQueue();$('#stop-queue').onclick=async()=>{try{await Promise.all(active.map(j=>api(`/api/jobs/${j.id}/cancel`,{method:'POST'})));toast((ui.t("正在停止任务，成功缓存会保留")));}catch(e){toast(e.message,true);}};
}
function renderHistoryWorkspace(){
  $('#main-content').innerHTML=heading((ui.t("课程报告")),(ui.t("每门课程一份报告，自动汇总各课次；重试结果更新到原报告。")),(`<button class="button secondary" data-action="library">${ui.t("返回课程库")}</button>`))+
    `<div class="toolbar"><label class="search-field">${ui.t("搜索报告")}<input id="history-search" placeholder="${ui.t("课次名称或处理信息")}" value="${esc(state.historyQuery)}"></label><label>${ui.t("状态")}<select id="history-status"><option value="all">${ui.t("全部状态")}</option>${Object.entries(statusNames).map(([key,title])=>`<option value="${key}">${title}</option>`).join('')}</select></label><label>${ui.t("课程")}<select id="history-course"><option value="all">${ui.t("全部课程 / 本地文件")}</option>${(state.library||[]).map(c=>`<option value="${esc(c.id)}">${esc(c.title)}</option>`).join('')}</select></label></div><div id="history-results"></div>`;
  bindActions();$('#history-status').value=state.historyStatus;$('#history-course').value=state.historyCourse;
  $('#history-search').oninput=e=>{state.historyQuery=e.target.value;renderHistoryRows();};
  $('#history-status').onchange=e=>{state.historyStatus=e.target.value;renderHistoryRows();};
  $('#history-course').onchange=e=>{state.historyCourse=e.target.value;renderHistoryRows();};renderHistoryRows();
}
function renderHistoryRows(){
  const query=state.historyQuery.toLowerCase();const entries=reportEntries().filter(j=>(state.historyStatus==='all'||j.status===state.historyStatus)
    &&(state.historyCourse==='all'||j.course_id===state.historyCourse)&&`${j.title} ${j.message} ${j.search_text||''}`.toLowerCase().includes(query));
  $('#history-results').innerHTML=`<p class="option-hint">${entries.length} ${ui.t("份报告")}</p><div class="job-list">${entries.map(j=>`<button class="job-card" ${reportEntryAttribute(j)}><div class="job-info"><h3>${esc(j.title)}</h3><p>${esc(j.course_id?ui.t("课程报告"):j.source==='demo'?ui.t("合成演示"):ui.t("本地文件"))} · ${new Date(j.updated_at*1000).toLocaleString(ui.language)} · ${j.event_count||0} ${ui.t("个候选事项")}</p><p>${esc(ui.message(j.message))}</p></div>${jobBadge(j)}</button>`).join('')||(`<div class="empty">${ui.t("没有符合条件的处理记录。")}</div>`)}</div>`;bindReportLinks();
}

const courseReportState={id:null,data:null,error:null,loading:false,request:0,filter:'all',query:'',signature:null,expanded:null};
function courseReportSignature(){return JSON.stringify([savedCourse(courseReportState.id)?.total_sessions,state.jobs.filter(j=>j.source==='yanhe'&&String(j.request?.course_id)===courseReportState.id).map(j=>[j.id,j.status,j.event_count])]);}
function bindTaskLinks(){document.querySelectorAll('[data-task-job]').forEach(b=>b.onclick=()=>openJob(b.dataset.taskJob,{task:true}));}
async function openCourseReport(id){
  rememberDetailOrigin();
  Object.assign(courseReportState,{id:String(id),data:null,error:null,filter:'all',query:'',signature:null,expanded:null});
  state.currentJob=null;showView('course-report');await loadCourseReport();
}
async function loadCourseReport(){
  const id=courseReportState.id,sequence=++courseReportState.request,signature=courseReportSignature();
  courseReportState.loading=true;
  try{
    const data=await api(`/api/library/${encodeURIComponent(id)}/report`);
    if(sequence!==courseReportState.request||courseReportState.id!==id)return;
    courseReportState.data=data;courseReportState.error=null;courseReportState.signature=signature;
  }catch(error){if(sequence!==courseReportState.request)return;courseReportState.error=error.message;}
  finally{if(sequence===courseReportState.request){courseReportState.loading=false;if(state.view==='course-report')renderCourseReport();}}
}
function renderCourseReport(){
  const data=courseReportState.data;
  if(!data){$('#main-content').innerHTML=`<button class="back-button" data-action="detail-back">${detailBackLabel()}</button>`+
    heading(savedCourse(courseReportState.id)?.title||ui.t('课程报告'),ui.t('每门课程一份报告，自动汇总各课次；重试结果更新到原报告。'))+
    `<div class="empty" role="status">${courseReportState.error?`<p>${esc(courseReportState.error)}</p><button class="button secondary" id="reload-course-report">${ui.t('重新读取')}</button>`:ui.t('正在读取处理记录…')}</div>`;
    bindActions();const retry=$('#reload-course-report');if(retry)retry.onclick=loadCourseReport;return;}
  const back=detailBackLabel(),id=encodeURIComponent(data.course_id);
  $('#main-content').innerHTML=`<button class="back-button" data-action="detail-back">${back}</button>`+
    heading(data.title,`${data.professors.join(' · ')} · ${ui.t('课程报告')}`,
      `${jobBadge(data)}<button class="button secondary" id="reload-course-report">${ui.t('刷新报告')}</button><a class="button secondary" href="/api/library/${id}/report?format=markdown">${ui.t('↓ 导出报告')}</a>`)+
    `<p class="inline-notice">${ui.t('每门课程一份报告，自动汇总各课次；重试结果更新到原报告。')}</p>
    ${data.status!=='completed'?`<p class="risk-note">${ui.t('部分课次尚未完成，以下报告仅覆盖已生成的结果。')}</p>`:''}
    ${courseReportState.error?`<p class="risk-note" role="status">${esc(courseReportState.error)}</p>`:''}
    <div class="workspace-stats"><section><strong>${data.completed_sessions} / ${data.total_sessions}</strong><span>${ui.t('已完成课次')}</span></section><section><strong>${data.event_count}</strong><span>${ui.t('个候选事项')}</span></section><section><strong>${data.available_sessions}</strong><span>${ui.t('已有结果的课次')}</span></section></div>
    <div class="toolbar"><label class="search-field">${ui.t('搜索报告')}<input id="course-report-search" value="${esc(courseReportState.query)}" placeholder="${ui.t('事项、原文或课次名称')}"></label><label>${ui.t('课堂重要事项')}<select id="course-report-filter"><option value="all">${ui.t('全部')}</option><option value="actionable">${ui.t('需行动')}</option><option value="review">${ui.t('待核验')}</option>${Object.entries(typeNames).map(([key,label])=>`<option value="${key}">${esc(label)}</option>`).join('')}</select></label></div>
    <div class="report-layout"><div id="course-report-lectures"></div><aside class="report-side"><section class="panel summary-card"><h3>${ui.t('原录播')}</h3><video id="course-report-player" controls preload="none" hidden></video><p id="course-report-player-caption" class="option-hint">${ui.t('点击事项时间，可切换到对应课次的原录播。')}</p></section><p class="option-hint">${ui.t('未检出不代表课堂上未发生。录播之外的通知、识别漏句和短暂画面仍可能遗漏，请按原文与原录播核对。')}</p></aside></div>`;
  bindActions();$('#reload-course-report').onclick=loadCourseReport;
  $('#course-report-filter').value=courseReportState.filter;
  $('#course-report-filter').onchange=e=>{courseReportState.filter=e.target.value;renderCourseLectures();};
  $('#course-report-search').oninput=e=>{courseReportState.query=e.target.value;renderCourseLectures();};renderCourseLectures();
}
function renderCourseLectures(){
  const data=courseReportState.data,query=courseReportState.query.toLowerCase(),filter=courseReportState.filter;
  const matches=event=>filter==='all'||filter==='review'&&event.needs_review||filter==='actionable'&&event.status==='actionable'||event.type===filter;
  const lectures=data.lectures.map((lecture,index)=>{
    const result=lecture.result,report=result?.report;
    const key=JSON.stringify([lecture.session_id,lecture.video_id]);
    const events=(report?.events||[]).filter(e=>matches(e)&&`${lecture.title} ${e.summary} ${e.action||''} ${e.sources.map(s=>s.quote).join(' ')}`.toLowerCase().includes(query));
    if((query||filter!=='all')&&!events.length)return '';
    const open=query||filter!=='all'||(courseReportState.expanded?courseReportState.expanded.has(key):index===0);
    return `<details class="panel course-report-lecture" data-recording="${esc(key)}" ${open?'open':''}><summary><span><strong>${esc(lecture.title)}</strong><small>${esc(dateLabel(lecture.started_at))} · ${events.length} ${ui.t('个候选事项')}</small></span>${jobBadge({status:lecture.report_status==='unavailable'?(lecture.status==='not_started'?'queued':lecture.status):lecture.report_status})}</summary><div class="course-report-body">
      <div class="row-actions">${result&&state.features.study?`<button class="button secondary" data-study-job="${esc(result.job_id)}">${ui.t('听课 / 整理讲义')}</button>`:''}${result?.links.subtitles?`<a class="text-button" href="${esc(result.links.subtitles)}" download>${ui.t('导出字幕')}</a>`:''}${result?`<a class="text-button" href="/api/jobs/${esc(result.job_id)}/export">${ui.t('导出课次证据')}</a>`:''}${lecture.latest_job_id?`<button class="text-button" data-task-job="${esc(lecture.latest_job_id)}">${ui.t('查看进度')}</button>`:''}</div>
      ${!result?`<p class="inline-notice">${ui.t('这个课次的报告尚未生成或无法读取，请在任务队列继续处理。')}</p>`:`
      ${report.simulated?`<p class="risk-note">${ui.t('✦ 这是固定合成样例演示，不是真实课程或真实模型的检测结果。')}</p>`:''}
      ${result.job_id!==lecture.latest_job_id?`<p class="option-hint">${ui.t('本课次采用最近的已完成结果，重试不会覆盖已有完整内容。')}</p>`:''}
      <details class="quality-details"><summary>${ui.t('覆盖范围与质量提示（')}${report.warnings.length}${ui.language==='en'?')':'）'}</summary><p>${ui.t('已生成字幕的分析')} ${report.analyzed_segment_ids.length} / ${report.total_segments} ${ui.t('段')}${report.media?` · ${ui.t('音频转写')} ${report.media.completed_chunk_ids.length} / ${report.media.total_chunks} ${ui.t('块')}`:''}</p><ul>${report.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>
      ${events.map(event=>courseEventCard(lecture,event)).join('')||`<p class="option-hint">${ui.t('所提供内容中未检出事项')} · ${ui.t('请同时查看录播覆盖与质量提示，不把无检出理解为保证没有。')}</p>`}`}
    </div></details>`;
  }).filter(Boolean);
  $('#course-report-lectures').innerHTML=lectures.join('')||`<div class="empty">${ui.t('没有符合条件的处理记录。')}</div>`;
  document.querySelectorAll('[data-recording]').forEach(element=>element.ontoggle=()=>{courseReportState.expanded=new Set([...document.querySelectorAll('[data-recording][open]')].map(item=>item.dataset.recording));});
  bindStudyLinks();bindTaskLinks();
  document.querySelectorAll('[data-course-seek]').forEach(b=>b.onclick=()=>{
    const lecture=data.lectures.find(l=>l.result?.job_id===b.dataset.courseSeek),url=lecture?.result.links.video,player=$('#course-report-player');
    if(!url||/\.(srt|vtt|txt)$/i.test(url)){toast(ui.t('此演示没有关联视频，请根据时间位置核对原录播'));return;}
    const seconds=Number(b.dataset.seconds);
    if(player.getAttribute('src')!==url){player.onloadedmetadata=()=>{player.currentTime=seconds;player.play().catch(()=>{});};player.src=url;}
    else{player.currentTime=seconds;player.play().catch(()=>{});}
    player.hidden=false;$('#course-report-player-caption').textContent=lecture.title;player.scrollIntoView({behavior:'smooth',block:'center'});
  });
  document.querySelectorAll('[data-course-review]').forEach(input=>input.onchange=async()=>{
    const lecture=data.lectures.find(l=>l.result?.job_id===input.dataset.courseReview),event=input.dataset.event,checked=input.checked;
    input.disabled=true;
    try{await api(`/api/jobs/${lecture.result.job_id}/reviews/${encodeURIComponent(event)}`,{method:'PUT',body:{reviewed:checked}});lecture.result.reviews[event]=checked;input.parentElement.classList.toggle('checked',checked);}
    catch(error){input.checked=!checked;toast(error.message,true);}finally{input.disabled=false;}
  });
}
function courseEventCard(lecture,event){
  const result=lecture.result,links=result.event_links[event.event_id]||{};
  return `<article class="event-card"><div class="event-meta"><span class="badge neutral">${esc(typeNames[event.type]||event.type)}</span><span class="badge neutral">${esc(ui.t({actionable:'需行动',needs_review:'待核验',informational:'仅供知悉',canceled:'已取消'}[event.status]||event.status))}</span><button class="event-time" data-course-seek="${esc(result.job_id)}" data-seconds="${event.start_seconds??0}">◷ ${timestamp(event.start_seconds)} ↗</button></div><h3>${esc(event.summary)}</h3>${event.action?`<p class="event-action">${esc(event.action)}</p>`:''}${event.due_text?`<p class="due-text">${ui.t('◷ 截止原文：')}${esc(event.due_text)}${ui.t('（需核验具体日期）')}</p>`:''}
    ${event.sources.map(source=>`<div class="quote-box">${esc(source.quote||ui.t('画面未读出文字，请查看截图。'))}</div><p class="source-caption">${ui.t(source.kind==='frame'?'画面模型识别文字 · 尚未自动核验':'原字幕')} · ${source.segment_ids.join(', ')}</p>`).join('')}
    ${(links.frames||[]).filter(Boolean).map(url=>`<a href="${esc(url)}" target="_blank" rel="noopener"><img class="event-frame" loading="lazy" src="${esc(url)}" alt="${ui.t('事项证据截图')}"></a>`).join('')}
    <details class="source-caption"><summary>${ui.t('为何需要核验')}</summary><p>${esc(event.review_reason)}</p></details><label class="review-label ${result.reviews[event.event_id]?'checked':''}"><input type="checkbox" data-course-review="${esc(result.job_id)}" data-event="${esc(event.event_id)}" ${result.reviews[event.event_id]?'checked':''}>${ui.t('我已核对这项通知')}</label>${links.clip?`<audio class="event-audio" controls preload="none" src="${esc(links.clip)}"></audio>`:''}</article>`;
}
