/* Product navigation, readiness, reversible task recovery, and saved study records. */
'use strict';
const productState={health:null,offline:false,restoring:false,records:null,recordJob:null,drafts:{},lastProgress:0,playbackTouchedJob:null,studyOpenRequest:0,retrySelection:new Set(),retryBusy:false};
const connectionLabels={available:'服务可达，模型可用',model_missing:'服务可达，但未找到此模型',auth_error:'鉴权失败，请检查密钥',rate_limited:'服务限流，请稍后重试',unsupported:'服务未提供模型列表，需实际任务验证',redirect:'服务地址发生重定向，请核对地址',unavailable:'服务暂不可用',unreachable:'无法连接，请检查服务或网络',incomplete:'请补全服务配置'};
function writeRoute(view=state.view){
  if(productState.restoring)return;
  let path=view;
  if(view==='study'&&studyState.jobId)path+='/' + studyState.jobId;
  if(view==='report'&&state.currentJob)path+='/' + state.currentJob;
  if(view==='library'&&state.savedCourse)path='course/'+encodeURIComponent(state.savedCourse);
  if(['study','report'].includes(view)&&path.includes('/')&&productState.detailOrigin)path+='?from='+encodeURIComponent(productState.detailOrigin);
  if(location.hash!=='#/'+path)history.pushState(null,'','#/'+path);
}
async function restoreRoute(){
  productState.restoring=true;
  try{
    const [path,query='']=location.hash.replace(/^#\//,'').split('?');
    const parts=path.split('/'),view=parts[0],id=parts[1];
    if(['study','report'].includes(view)&&id&&state.jobs.some(job=>job.id===id)){
      productState.detailOrigin=validDetailOrigin(new URLSearchParams(query).get('from'));
      if(view==='study')await openStudy(id);else await openJob(id);
    }else if(view==='course'&&id&&savedCourse(decodeURIComponent(id))){state.savedCourse=decodeURIComponent(id);showView('library');}
    else if(['home','library','study','queue','history','classroom','local'].includes(view)&&parts.length===1){
      if(view==='library')state.savedCourse=null;
      if(view==='study'){saveStudyProgress(true);productState.studyOpenRequest++;studyState.jobId=null;studyState.data=null;}
      showView(view);
    }
    else{showView('home');history.replaceState(null,'','#/home');}
  }catch(error){showView('home');history.replaceState(null,'','#/home');toast(error.message,true);}
  finally{productState.restoring=false;}
}
function validDetailOrigin(origin){
  if(['home','library','study','queue','history'].includes(origin))return origin;
  if(origin?.startsWith('course/')&&savedCourse(decodeURIComponent(origin.slice(7))))return origin;
  return null;
}
function rememberDetailOrigin(id){
  if(productState.restoring)return;
  if(['report','study'].includes(state.view)&&(state.view!=='study'||studyState.jobId))return;
  productState.detailOrigin=state.view==='library'&&state.savedCourse
    ?'course/'+encodeURIComponent(state.savedCourse):validDetailOrigin(state.view);
}
function detailBackLabel(){
  return ui.t(productState.detailOrigin?.startsWith('course/')?'← 返回课程课次':
    productState.detailOrigin==='queue'?'← 返回任务队列':
    productState.detailOrigin==='home'?'← 返回工作台':
    productState.detailOrigin==='study'?'选择其他课次':
    productState.detailOrigin==='library'?'← 返回课程库':'← 返回处理记录');
}
function returnFromDetail(){
  const origin=validDetailOrigin(productState.detailOrigin);
  if(origin?.startsWith('course/')){state.savedCourse=decodeURIComponent(origin.slice(7));showView('library');return;}
  if(origin==='library')state.savedCourse=null;
  if(origin==='study'){openStudyPicker();return;}
  showView(origin||'history');
}
function openStudyPicker(){
  saveStudyProgress(true);productState.studyOpenRequest++;studyState.jobId=null;studyState.data=null;
  productState.detailOrigin=null;showView('study');
}
window.addEventListener('popstate',()=>restoreRoute());
window.addEventListener('beforeunload',event=>{if(Object.values(productState.drafts).some(d=>d.dirty)){event.preventDefault();event.returnValue='';}});
function connectionState(offline){
  productState.offline=offline;
  const banner=$('#connection-banner');if(banner)banner.hidden=!offline;
}
async function loadHealth(){
  try{productState.health=await api('/api/health');if(state.view==='home')renderHome();}
  catch(error){toast(error.message,true);}
}
function renderHome(){
  const s=state.settings,health=productState.health,active=state.jobs.filter(job=>!terminal.has(job.status)),unfinished=state.jobs.filter(job=>terminal.has(job.status)&&job.status!=='completed');
  const checks=[{label:'选择模型服务',ok:configured(true),action:'settings',hint:'自建模型或线上 API，可分别配置转写与分析'},
    {label:'连接课程或导入文件',ok:state.auth.connected||state.jobs.length>0,action:'classroom',hint:'校园登录只用于新录播；已有报告可离线查看'},
    {label:'回看结果与开始复习',ok:state.jobs.some(j=>j.status==='completed'),action:'study',hint:'通知报告、文字稿和讲义集中保存在本机'}];
  if(window.fuckYanheUI){window.fuckYanheUI.renderHome($('#main-content'),{
    t:ui.t,connected:state.auth.connected,configured:configured(true),model:s.event_model,
    courses:state.library.filter(c=>!c.archived).length,completed:state.jobs.filter(j=>j.status==='completed'&&j.source!=='demo').length,
    pending:active.length+unfinished.length+(state.studyTasks||[]).filter(task=>task.status!=='completed').length,
    jobs:state.jobs.map(j=>({...j,course:savedCourse(j.request?.course_id)?.title||''})),health,checks,onJob:openJob,
    onAction:async action=>{if(action==='settings')return openSettings();if(action==='refresh')return loadHealth();if(action==='local')return $('#local-file').click();if(action==='study')return openStudyPicker();if(action==='demo'){try{const job=await api('/api/demo',{method:'POST'});state.jobs=[job,...state.jobs.filter(j=>j.id!==job.id)];renderAccount();await openJob(job.id);}catch(error){toast(error.message,true);}return;}showView(action);}
  });return;}
  $('#main-content').innerHTML=`<div class="bento-dashboard">
    <section class="bento-hero"><div class="hero-kicker"><span class="hero-logomark">◎</span> fuckYanhe <span class="hero-edition">${ui.t('学习工作台')}</span></div>
      <h1>${ui.t('把课堂里的重要信息，留在眼前。')}</h1><p>${ui.t('从录播到通知与讲义，接着上次的进度继续。')}</p>
      <div class="page-actions"><button class="button primary" data-action="classroom">${ui.t('添加课程')} <span aria-hidden="true">↗</span></button><button class="button secondary" data-action="local">${ui.t('导入本地文件')}</button></div>
      <div class="hero-footer"><span><i class="connection-dot ${state.auth.connected?'connected':''}"></i>${ui.t(state.auth.connected?'已连接延河课堂':'尚未连接延河课堂')}</span><span>${ui.t('本机保存 · 自选模型服务')}</span></div></section>
    <div class="bento-stats workspace-stats"><section><div class="stat-heading"><span>${ui.t('在用课程')}</span><span aria-hidden="true">▦</span></div><strong>${state.library.filter(c=>!c.archived).length}<small>${ui.t('门')}</small></strong><span>${ui.t('课程与历史记录，集中保存')}</span></section><section><div class="stat-heading"><span>${ui.t('已完成课次')}</span><span aria-hidden="true">✓</span></div><strong>${state.jobs.filter(j=>j.status==='completed'&&j.source!=='demo').length}<small>${ui.t('段可用录播')}</small></strong><span>${ui.t('随时回看字幕与事项报告')}</span></section><section><div class="stat-heading"><span>${ui.t('进行中 / 待继续')}</span><span aria-hidden="true">◷</span></div><strong>${active.length+unfinished.length+(state.studyTasks||[]).filter(task=>task.status!=='completed').length}</strong><button class="text-button" data-action="queue">${ui.t('查看待继续任务')} ↗</button></section></div>
    <section class="panel readiness"><div class="section-title"><h2>${ui.t('开始使用')}</h2><button class="text-button" data-action="demo">${ui.t('查看演示报告')} ↗</button></div><div class="readiness-grid">${checks.map((item,index)=>`<button data-action="${item.action}" class="readiness-step"><span class="check-state">${item.ok?'✓':String(index+1).padStart(2,'0')}</span><strong>${ui.t(item.label)}</strong><p>${ui.t(item.hint)}</p><span class="tile-arrow" aria-hidden="true">↗</span></button>`).join('')}</div></section>
    <section class="panel health-panel"><div class="section-title"><h2>${ui.t('本机与服务状态')}</h2><span class="health-orbit" aria-hidden="true">◎</span></div><div class="health-items"><span><small>${ui.t('校园平台')}</small><strong>${ui.t(state.auth.connected?'已连接延河课堂':'尚未连接延河课堂')}</strong></span><span><small>${ui.t('分析模型')}</small><strong>${configured(true)?esc(s.event_model):ui.t('尚未配置')}</strong></span><span><small>${ui.t('媒体工具')}</small><strong>${health?ui.t(health.media_ready?'可用':'待安装'):ui.t('正在检查')}</strong></span><span><small>${ui.t('本机存储')}</small><strong>${health?ui.t(health.storage_ready?'可写':'不可写'):ui.t('正在检查')}</strong>${health?.free_bytes!=null?`<small>${(health.free_bytes/1073741824).toFixed(1)} GB ${ui.t('可用空间')}</small>`:''}</span></div><div class="row-actions"><button class="text-button" id="refresh-health">${ui.t('刷新状态')} ↻</button><button class="text-button" data-action="settings">${ui.t('配置与检测模型')} ↗</button></div></section>
    <section class="panel recent-work"><div class="section-title"><h2>${ui.t('最近处理')}</h2><button class="text-button" data-action="history">${ui.t('查看全部报告与记录')} ↗</button></div><div class="job-list">${state.jobs.slice(0,5).map(job=>`<button class="job-card" data-job="${esc(job.id)}"><span class="recent-icon" aria-hidden="true">▤</span><div class="job-info"><h3>${esc(job.title)}</h3><p>${esc(savedCourse(job.request?.course_id)?.title||ui.message(job.message))}</p></div>${jobBadge(job)}<span class="recent-arrow" aria-hidden="true">↗</span></button>`).join('')||`<div class="empty"><p>${ui.t('还没有处理记录，可以先查看演示或导入自己的字幕。')}</p></div>`}</div></section></div>`;
  bindActions();bindJobLinks();$('#refresh-health').onclick=loadHealth;
}
function modelFormValues(){
  const values=Object.fromEntries(new FormData($('#settings-form')));delete values.remember_defaults;
  return values;
}
async function checkModel(kind){
  const button=$('#test-'+kind),output=$('#'+kind+'-connection-result');button.disabled=true;output.textContent=ui.t('正在检查');
  try{const result=await api('/api/settings/check/'+kind,{method:'POST',body:modelFormValues()});output.textContent=ui.t(connectionLabels[result.status]||'服务暂不可用');}
  catch(error){output.textContent=ui.message(error.message);}finally{button.disabled=false;}
}
async function retrySelectedTasks(){
  const selected=[...document.querySelectorAll('[data-retry-select]:checked')].map(input=>input.dataset.retrySelect);
  if(!selected.length)return;
  const button=$('#retry-selected');productState.retryBusy=true;button.disabled=true;
  try{const result=await api('/api/jobs/retry-many',{method:'POST',body:{job_ids:selected}});
    state.jobs=[...result.jobs,...state.jobs.filter(j=>!result.jobs.some(n=>n.id===j.id))];
    result.jobs.forEach(job=>productState.retrySelection.delete(job.id));productState.retryResult=result;renderQueue();renderAccount();
    toast(`${ui.t('已继续')} ${result.jobs.length} ${ui.t('个任务')} · ${ui.t('需要处理')} ${result.errors.length}`);
  }catch(error){toast(error.message,true);}finally{productState.retryBusy=false;if($('#retry-selected'))$('#retry-selected').disabled=!productState.retrySelection.size;}
}
function enhanceQueue(){
  const unfinished=state.jobs.filter(job=>terminal.has(job.status)&&job.status!=='completed');
  const eligible=new Set(unfinished.map(job=>job.id));
  productState.retrySelection=new Set([...productState.retrySelection].filter(id=>eligible.has(id)));
  const actions=$('.queue-actions');if(!actions)return;
  actions.insertAdjacentHTML('beforeend',`<button class="button primary" id="retry-selected" ${!productState.retrySelection.size||productState.retryBusy?'disabled':''}>${ui.t('继续所选任务')}</button><label class="checkbox-label"><input id="select-retry-all" type="checkbox">${ui.t('选择可继续任务')}</label>`);
  document.querySelectorAll('[data-task-action]').forEach(button=>{const job=state.jobs.find(j=>j.id===button.dataset.taskAction);if(!terminal.has(job.status))return;
    button.parentElement.insertAdjacentHTML('afterbegin',`<label class="checkbox-label"><input type="checkbox" data-retry-select="${esc(job.id)}" ${productState.retrySelection.has(job.id)?'checked':''}>${ui.t('选择')}</label>`);
    if(job.source==='yanhe'&&!state.auth.connected)button.parentElement.insertAdjacentHTML('beforeend',`<small>${ui.t('需要重新连接校园平台')}</small>`);
  });
  document.querySelectorAll('[data-retry-select]').forEach(input=>input.onchange=()=>{input.checked?productState.retrySelection.add(input.dataset.retrySelect):productState.retrySelection.delete(input.dataset.retrySelect);$('#retry-selected').disabled=!productState.retrySelection.size||productState.retryBusy;});
  $('#select-retry-all').checked=unfinished.length>0&&unfinished.every(job=>productState.retrySelection.has(job.id));$('#select-retry-all').onchange=event=>document.querySelectorAll('[data-retry-select]').forEach(input=>{input.checked=event.target.checked;input.onchange();});
  $('#retry-selected').onclick=retrySelectedTasks;
  const studies=(state.studyTasks||[]).filter(task=>task.status!=='completed');if(studies.length){$('#main-content').insertAdjacentHTML('beforeend',`<section class="recent-work"><h2>${ui.t('讲义整理任务')}</h2><div class="job-list">${studies.map(task=>`<button class="job-card" data-study-job="${esc(task.id)}"><div class="job-info"><h3>${esc(task.title)}</h3><p>${task.current}/${task.total} · ${ui.t('查看讲义进度或继续整理')}</p></div>${jobBadge(task)}</button>`).join('')}</div></section>`);bindStudyLinks();}
  if(productState.retryResult?.errors.length)actions.insertAdjacentHTML('afterend',`<p class="inline-notice">${ui.t('部分任务未继续，请连接校园平台，或检查模型和原始文件后重试。')}</p>`);
}
async function loadStudyRecords(id){
  if(!state.features.product)return;
  try{const data=await api(`/api/jobs/${id}/study-records`);if(studyState.jobId!==id)return;productState.records=data;productState.recordJob=id;renderStudyRecords();}
  catch(error){toast(error.message,true);}
}
function renderStudyRecords(){
  const target=$('#study-personal');if(!target||productState.recordJob!==studyState.jobId)return;
  const records=productState.records,draft=productState.drafts[studyState.jobId];
  target.innerHTML=`<details open class="study-personal"><summary>${ui.t('我的笔记与书签')}</summary>
    ${records.position>0&&!records.progress_stale?`<button class="button secondary" id="resume-study">${ui.t('继续上次听课')} · ${timestamp(records.position)}</button>`:''}
    ${records.progress_stale?`<p class="option-hint">${ui.t('文字稿已更新，旧进度不自动定位；笔记仍保留。')}</p>`:''}
    <label for="personal-note">${ui.t('我的复习笔记')}</label><textarea id="personal-note" maxlength="20000" placeholder="${ui.t('记录理解、疑问或对 AI 讲义的修正，仅保存在本机。')}">${esc(draft?.dirty?draft.text:records.note)}</textarea>
    <div class="row-actions"><button class="button secondary" id="save-personal-note">${ui.t('保存笔记')}</button><span id="note-save-state" role="status">${ui.t(draft?.dirty?'尚未保存':'已保存到本机')}</span><button class="text-button" id="bookmark-current" ${!$('#study-player')?'disabled':''}>${ui.t('收藏当前播放位置')}</button></div>
    <div class="bookmark-list">${records.bookmarks.map(mark=>`<article class="bookmark"><button class="text-button" data-bookmark-seek="${mark.position??''}" ${mark.stale||mark.position==null?'disabled':''}>${timestamp(mark.position)}${mark.segment_id?' · #'+mark.segment_id:''}</button><p>${esc(mark.quote||mark.note||ui.t('播放位置书签'))}</p>${mark.stale?`<small>${ui.t('原文已更新，请重新核对书签')}</small>`:''}<button class="text-button" data-remove-bookmark="${esc(mark.id)}">${ui.t('移除书签')}</button></article>`).join('')||`<p class="option-hint">${ui.t('可收藏播放位置，或在文字稿旁点击收藏原文。')}</p>`}</div></details>`;
  $('#personal-note').oninput=event=>{productState.drafts[studyState.jobId]={text:event.target.value,dirty:true};$('#note-save-state').textContent=ui.t('尚未保存');};
  $('#save-personal-note').onclick=async()=>{try{const id=studyState.jobId,text=$('#personal-note').value,data=await api(`/api/jobs/${id}/study-note`,{method:'PUT',body:{text}});const changed=productState.drafts[id]?.dirty&&productState.drafts[id].text!==text;if(!changed)productState.drafts[id]={text,dirty:false};if(studyState.jobId!==id)return;productState.records=data;$('#note-save-state').textContent=ui.t(changed?'尚未保存':'已保存到本机');}catch(error){toast(error.message,true);}};
  if($('#resume-study'))$('#resume-study').onclick=()=>{const player=$('#study-player');if(player){player.playbackRate=records.speed||1;$('#study-speed').value=String(player.playbackRate);}seekStudy(records.position);};
  $('#bookmark-current').onclick=()=>addStudyBookmark({position:$('#study-player').currentTime});
  document.querySelectorAll('[data-bookmark-seek]').forEach(button=>button.onclick=()=>seekStudy(button.dataset.bookmarkSeek));
  document.querySelectorAll('[data-remove-bookmark]').forEach(button=>button.onclick=async()=>{try{const id=studyState.jobId,data=await api(`/api/jobs/${id}/bookmarks/${button.dataset.removeBookmark}`,{method:'DELETE'});if(studyState.jobId!==id)return;productState.records=data;renderStudyRecords();}catch(error){toast(error.message,true);}});
}
async function addStudyBookmark(body){
  try{const id=studyState.jobId,data=await api(`/api/jobs/${id}/bookmarks`,{method:'POST',body});if(studyState.jobId!==id)return;productState.records=data;renderStudyRecords();toast(ui.t('书签已保存'));}
  catch(error){toast(error.message,true);}
}
function saveStudyProgress(force=false){
  const player=$('#study-player');if(!player||!state.features.product||!studyState.jobId||productState.playbackTouchedJob!==studyState.jobId)return;
  const now=Date.now();if(!force&&now-productState.lastProgress<15000)return;
  productState.lastProgress=now;
  const id=studyState.jobId;
  api(`/api/jobs/${id}/study-progress`,{method:'PUT',keepalive:force,body:{position:player.currentTime,speed:player.playbackRate}}).then(data=>{if(studyState.jobId===id){productState.records=data;productState.recordJob=id;}}).catch(()=>{});
}
