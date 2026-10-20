/* Persistent course library, cross-course batches and task navigation. */
function savedCourse(id){return (state.library||[]).find(c=>c.id===id);}
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
  if(state.savedCourse){renderSavedCourse();return;}
  const courses=state.library||[],done=courses.reduce((n,c)=>n+c.completed_sessions,0);
  const active=state.jobs.filter(j=>!terminal.has(j.status)).length;
  $('#main-content').innerHTML=heading((ui.t("课程库")),(ui.t("课程、已完成课次和报告保存在本机。课次数量以已保存或最近同步的列表为准。")),
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
    (`<button class="button secondary" id="refresh-saved-course">${ui.t("刷新官方课次 / 选择分析")}</button>`))+
    `<section class="panel course-notes"><label>${ui.t("课程备注")}<textarea id="course-notes" maxlength="2000" placeholder="${ui.t("记录课程安排、复核进度等")}">${esc(course.notes)}</textarea></label><button class="button secondary" id="save-course-notes">${ui.t("保存备注")}</button></section>
    <div class="table-wrap panel"><table class="library-table"><thead><tr><th>${ui.t("课次")}</th><th>${ui.t("处理状态")}</th><th>${ui.t("报告")}</th></tr></thead><tbody>${course.sessions.map(s=>`<tr><td><strong>${esc(s.title)}</strong><small>${esc(dateLabel(s.started_at))}</small></td><td>${s.status?jobBadge({status:s.status}):(`<span class="badge neutral">${ui.t("尚未处理")}</span>`)}${s.completed_job_id&&s.status!=='completed'?(`<small>${ui.t("保留有已完成版本")}</small>`):''}</td><td>${s.latest_job_id?`<button class="button secondary" data-job="${esc(s.latest_job_id)}">${['running','queued'].includes(s.status)?(ui.t("查看进度")):(ui.t("查看报告"))}</button>`:'—'}${s.completed_job_id&&s.completed_job_id!==s.latest_job_id?`<button class="text-button" data-job="${esc(s.completed_job_id)}">${ui.t("已完成版本")}</button>`:''}</td></tr>`).join('')||(`<tr><td colspan="3">${ui.t("尚未同步课次，请连接课堂后刷新。")}</td></tr>`)}</tbody></table></div>`;
  $('#back-library').onclick=()=>{state.savedCourse=null;renderLibrary();};
  $('#refresh-saved-course').onclick=()=>openCourse(course.id);
  $('#save-course-notes').onclick=()=>patchCourse(course.id,{notes:$('#course-notes').value});bindJobLinks();
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
  const active=state.jobs.filter(j=>!terminal.has(j.status)),incomplete=state.jobs.filter(j=>terminal.has(j.status)&&j.status!=='completed');
  $('#main-content').innerHTML=heading((ui.t("任务队列")),`${active.length} ${ui.t("个任务排队或处理中，")}${incomplete.length} ${ui.t("个任务可继续。")}`,
    (`<button class="button secondary" data-action="library">${ui.t("从课程库批量执行")}</button>`))+
    (state.batchFeedback?`<p class="inline-notice" role="status">${esc(state.batchFeedback)}</p>`:'')+
    (state.batchErrors?.length?`<div class="risk-note">${state.batchErrors.map(esc).join('<br>')}</div>`:'')+
    `<div class="queue-actions"><button class="button secondary" id="stop-queue" ${!active.length?'disabled':''}>${ui.t("停止全部待处理任务")}</button><button class="text-button" data-action="history">${ui.t("查看全部报告与记录")}</button></div><div class="job-list">${[...active,...incomplete].map(j=>`<section class="panel queue-card"><div><strong>${esc(j.title)}</strong>${jobBadge(j)}</div><p>${esc(stages[j.stage]||j.message)} ${j.total?esc(progressLabel(j)):''}</p><div class="row-actions"><button class="button secondary" data-job="${esc(j.id)}">${ui.t("查看")}${terminal.has(j.status)?(ui.t("报告")):(ui.t("进度"))}</button><button class="text-button" data-task-action="${esc(j.id)}">${terminal.has(j.status)?(ui.t("继续处理")):(ui.t("停止"))}</button></div></section>`).join('')||(`<div class="empty"><h3>${ui.t("目前没有待处理任务")}</h3><p>${ui.t("在课程库勾选多门课程，即可一键分析未完成课次。")}</p></div>`)}</div>`;
  bindActions();bindJobLinks();document.querySelectorAll('[data-task-action]').forEach(b=>b.onclick=async()=>{try{const job=state.jobs.find(j=>j.id===b.dataset.taskAction);const data=await api(`/api/jobs/${job.id}/${terminal.has(job.status)?'retry':'cancel'}`,{method:'POST'});state.jobs=state.jobs.map(j=>j.id===job.id?data:j);renderQueue();}catch(e){toast(e.message,true);}});
  $('#stop-queue').onclick=async()=>{try{await Promise.all(active.map(j=>api(`/api/jobs/${j.id}/cancel`,{method:'POST'})));toast((ui.t("正在停止任务，成功缓存会保留")));}catch(e){toast(e.message,true);}};
}
function renderHistoryWorkspace(){
  $('#main-content').innerHTML=heading((ui.t("报告与处理记录")),(ui.t("按课程、状态或关键词查找，历史版本会保留。")),(`<button class="button secondary" data-action="library">${ui.t("返回课程库")}</button>`))+
    `<div class="toolbar"><label class="search-field">${ui.t("搜索报告")}<input id="history-search" placeholder="${ui.t("课次名称或处理信息")}" value="${esc(state.historyQuery)}"></label><label>${ui.t("状态")}<select id="history-status"><option value="all">${ui.t("全部状态")}</option>${Object.entries(statusNames).map(([key,title])=>`<option value="${key}">${title}</option>`).join('')}</select></label><label>${ui.t("课程")}<select id="history-course"><option value="all">${ui.t("全部课程 / 本地文件")}</option>${(state.library||[]).map(c=>`<option value="${esc(c.id)}">${esc(c.title)}</option>`).join('')}</select></label></div><div id="history-results"></div>`;
  bindActions();$('#history-status').value=state.historyStatus;$('#history-course').value=state.historyCourse;
  $('#history-search').oninput=e=>{state.historyQuery=e.target.value;renderHistoryRows();};
  $('#history-status').onchange=e=>{state.historyStatus=e.target.value;renderHistoryRows();};
  $('#history-course').onchange=e=>{state.historyCourse=e.target.value;renderHistoryRows();};renderHistoryRows();
}
function renderHistoryRows(){
  const query=state.historyQuery.toLowerCase();const jobs=state.jobs.filter(j=>(state.historyStatus==='all'||j.status===state.historyStatus)
    &&(state.historyCourse==='all'||j.request.course_id===state.historyCourse)&&`${j.title} ${j.message}`.toLowerCase().includes(query));
  $('#history-results').innerHTML=`<p class="option-hint">${jobs.length} ${ui.t("份记录")}</p><div class="job-list">${jobs.map(j=>`<button class="job-card" data-job="${esc(j.id)}"><div class="job-info"><h3>${esc(j.title)}</h3><p>${esc(savedCourse(j.request.course_id)?.title|| (j.source==='demo'?(ui.t("合成演示")):(ui.t("本地文件"))))} · ${new Date(j.created_at*1000).toLocaleString(ui.language)} · ${j.event_count||0} ${ui.t("个候选事项")}</p><p>${esc(ui.message(j.message))}</p></div>${jobBadge(j)}</button>`).join('')||(`<div class="empty">${ui.t("没有符合条件的处理记录。")}</div>`)}</div>`;bindJobLinks();
}
