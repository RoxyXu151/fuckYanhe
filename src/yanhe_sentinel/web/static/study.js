/* Study workspace. Model summaries and original transcript evidence remain separate. */
'use strict';
const studyState={jobId:null,data:null,transcript:null,chapter:null,mode:'notes',query:'',offset:0,language:'zh-CN',request:0,generating:false};
function bindStudyLinks(){document.querySelectorAll('[data-study-job]').forEach(button=>button.onclick=()=>openStudy(button.dataset.studyJob));}
async function openStudy(id){
  try{const data=await api(`/api/jobs/${encodeURIComponent(id)}/learning`);
    Object.assign(studyState,{jobId:id,data,transcript:null,chapter:null,mode:'notes',query:'',offset:0,language:data.model?data.language:ui.language});
    showView('study');await loadStudyTranscript();
  }catch(error){toast(error.message,true);}
}
function renderStudy(){
  if(!studyState.jobId){
    const available=state.jobs.filter(job=>terminal.has(job.status));
    $('#main-content').innerHTML=heading(ui.t('听课'),ui.t('用已有文字稿复习：搜索原文、跳转录播、整理章节与讲义。'))+
      `<div class="job-list">${available.map(job=>`<button class="job-card" data-study-job="${esc(job.id)}"><div class="job-info"><h3>${esc(job.title)}</h3><p>${esc(job.source==='demo'?ui.t('合成演示'):savedCourse(job.request.course_id)?.title||ui.t('本地文件'))}</p></div>${jobBadge(job)}<span>▷</span></button>`).join('')||`<div class="empty"><h3>${ui.t('先准备一份文字稿')}</h3><p>${ui.t('完成录播转写或导入字幕后，即可开始听课。')}</p></div>`}</div>`;
    bindStudyLinks();return;
  }
  const d=studyState.data;
  $('#main-content').innerHTML=`<button class="back-button" id="study-back">← ${ui.t('选择其他课次')}</button>`+
    heading(d.title,ui.t('文字稿可直接查看；生成讲义使用当前事项分析模型，不重新转写音频。'))+
    `<p class="inline-notice">${ui.t('AI 讲义仅覆盖已有字幕。漏句、公式识别和跨分段主题可能影响结果，请对照原录播核验。')}</p>
    <div class="study-layout"><aside class="panel study-outline"><h2>${ui.t('章节目录')}</h2><div id="study-chapters"></div></aside>
    <section class="study-main">${d.video?`<div class="study-player"><video id="study-player" controls preload="metadata" src="${esc(d.video)}"></video><label>${ui.t('播放速度')}<select id="study-speed"><option value="0.75">0.75×</option><option value="1" selected>1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label></div>`:`<p class="inline-notice">${ui.t(d.has_timestamps?'此文字稿未关联可播放录播，仍可按时间阅读。':'此文字稿没有时间戳，章节和引用按字幕编号定位。')}</p>`}
    <section class="panel study-generation"><div id="study-status" role="status" aria-live="polite"></div></section>
    <div class="study-tabs"><button class="tab" id="study-notes-tab">${ui.t('讲义与知识点')}</button><button class="tab" id="study-transcript-tab">${ui.t('原始文字稿')}</button><button class="text-button" id="study-report">${ui.t('查看通知报告')}</button></div>
    <div id="study-notes"></div><section id="study-transcript" class="panel"><div class="study-search"><label for="study-query">${ui.t('搜索文字稿')}</label><input id="study-query" placeholder="${ui.t('输入原文关键词')}" value="${esc(studyState.query)}"><button class="button secondary" id="study-search-button">${ui.t('搜索')}</button></div><div id="study-transcript-rows"></div></section>
    </section></div>`;
  $('#study-back').onclick=()=>{studyState.jobId=null;studyState.data=null;renderStudy();};
  $('#study-notes-tab').onclick=()=>{studyState.mode='notes';renderStudyContent();};
  $('#study-transcript-tab').onclick=()=>{studyState.mode='transcript';renderStudyContent();};
  $('#study-report').onclick=()=>openJob(studyState.jobId);
  $('#study-search-button').onclick=()=>{studyState.query=$('#study-query').value;studyState.offset=0;studyState.mode='transcript';loadStudyTranscript();};
  $('#study-query').onkeydown=event=>{if(event.key==='Enter')$('#study-search-button').click();};
  const player=$('#study-player');if(player){$('#study-speed').onchange=()=>player.playbackRate=Number($('#study-speed').value);player.ontimeupdate=updateStudyPlayback;}
  renderStudyStatus();renderStudyContent();
}
function renderStudyStatus(){
  const target=$('#study-status');if(!target)return;
  const d=studyState.data,busy=studyState.generating||['queued','running'].includes(d.status);
  const names={idle:'尚未生成讲义',queued:'排队中',running:'正在整理讲义',completed:'讲义已生成',failed:'讲义整理未完成',interrupted:'待继续',outdated:'文字稿已更新，请重新整理'};
  target.innerHTML=`<strong>${ui.t(names[d.status]||'尚未生成讲义')}</strong><p>${d.current||0} / ${d.total||0} ${ui.t('文字稿分段')} · ${d.total_segments||0} ${ui.t('段字幕')}${d.model?' · '+esc(d.model):''}</p>
    ${d.error?`<p class="risk-note">${ui.t('讲义整理未完成，请检查模型服务后继续；成功分段已缓存。')}</p>`:''}
    <div class="row-actions"><label>${ui.t('讲义语言')}<select id="study-language"><option value="zh-CN">中文</option><option value="en">English</option></select></label>
    <button class="button primary" id="study-generate" ${busy||!d.total_segments?'disabled':''}>${ui.t(d.status==='completed'?'重新生成讲义':['failed','interrupted'].includes(d.status)?'继续整理讲义':'生成章节与讲义')}</button>
    ${busy?`<button class="text-button" id="study-stop">${ui.t('停止整理（保留缓存）')}</button>`:''}
    ${d.export?`<a class="button secondary" href="${esc(d.export)}" download="study-notes.md">${ui.t('导出讲义 Markdown')}</a>`:''}</div>
    <small>${ui.t('仅点击生成才会调用模型；线上服务可能计费。停止会在当前请求结束后生效。')}</small>`;
  $('#study-language').value=studyState.language;$('#study-language').onchange=e=>studyState.language=e.target.value;
  $('#study-generate').onclick=generateStudy;
  if($('#study-stop'))$('#study-stop').onclick=async()=>{try{await api(`/api/jobs/${studyState.jobId}/learning/cancel`,{method:'POST'});toast(ui.t('已请求停止，当前请求结束后保留已成功分段。'));}catch(e){toast(e.message,true);}};
}
async function generateStudy(){
  if(studyState.generating)return;
  if(!configured(true)){openSettings();toast(ui.t('请先配置事项分析模型'));return;}
  studyState.generating=true;renderStudyStatus();
  try{const response=await api(`/api/jobs/${studyState.jobId}/learning`,{method:'POST',body:{language:studyState.language,force:studyState.data.status==='completed'}});
    studyState.data={...studyState.data,...response};studyState.chapter=null;studyState.offset=0;renderStudyContent();
  }catch(error){toast(error.message,true);}finally{studyState.generating=false;renderStudyStatus();}
}
async function refreshStudy(){
  if(!studyState.jobId||!['queued','running'].includes(studyState.data?.status))return;
  const id=studyState.jobId,data=await api(`/api/jobs/${id}/learning`);
  if(studyState.jobId!==id||state.view!=='study')return;
  const changed=JSON.stringify([data.status,data.current,data.error])!==JSON.stringify([studyState.data.status,studyState.data.current,studyState.data.error]);
  studyState.data=data;if(changed){renderStudyStatus();renderStudyContent();}
}
function renderStudyContent(){
  if(!$('#study-chapters'))return;
  const d=studyState.data,chapters=d.chapters||[];
  $('#study-chapters').innerHTML=`<button class="study-chapter ${studyState.chapter===null?'active':''}" data-study-chapter="all">${ui.t('全部章节')}</button>`+
    chapters.map((chapter,index)=>`<button class="study-chapter ${studyState.chapter===index?'active':''}" data-study-chapter="${index}"><strong>${index+1}. ${esc(chapter.title)}</strong><small>${timestamp(chapter.start)} – ${timestamp(chapter.end)}</small></button>`).join('')+
    (!chapters.length?`<p>${ui.t('生成后在这里按主题导航。文字稿无需生成即可查看。')}</p>`:'');
  document.querySelectorAll('[data-study-chapter]').forEach(button=>button.onclick=()=>{
    studyState.chapter=button.dataset.studyChapter==='all'?null:Number(button.dataset.studyChapter);studyState.offset=0;
    if(studyState.chapter!==null)seekStudy(chapters[studyState.chapter].start);
    renderStudyContent();loadStudyTranscript();
  });
  const selected=studyState.chapter===null?chapters:chapters.filter((_,index)=>index===studyState.chapter);
  $('#study-notes').innerHTML=selected.map(chapter=>`<article class="panel study-note"><h2>${esc(chapter.title)}</h2><p class="study-summary">${esc(chapter.summary)}</p>
    ${chapter.points.map(point=>`<section class="study-point"><p>${esc(point.text)}</p><details><summary>${ui.t('查看原文依据')}</summary>${point.sources.map(source=>`<blockquote><button class="text-button" data-study-seek="${source.start??''}" ${source.start==null?'disabled':''}>${timestamp(source.start)} · #${source.id}</button><p>${esc(source.text)}</p></blockquote>`).join('')}</details></section>`).join('')}</article>`).join('')||`<div class="panel empty"><h3>${ui.t('先阅读文字稿，或生成一份讲义')}</h3><p>${ui.t('章节标题和知识点由模型整理，原文依据保持不变。')}</p><button class="button secondary" id="study-read-transcript">${ui.t('阅读文字稿')}</button></div>`;
  if($('#study-read-transcript'))$('#study-read-transcript').onclick=()=>{studyState.mode='transcript';renderStudyContent();};
  $('#study-notes').hidden=studyState.mode!=='notes';$('#study-transcript').hidden=studyState.mode!=='transcript';
  $('#study-notes-tab').classList.toggle('active',studyState.mode==='notes');$('#study-transcript-tab').classList.toggle('active',studyState.mode==='transcript');
  bindStudySeek();renderStudyTranscript();
}
function seekStudy(seconds){
  if(seconds==null||seconds==='')return;
  const player=$('#study-player');if(!player)return;
  player.currentTime=Number(seconds);player.play().catch(()=>{});
}
function bindStudySeek(){document.querySelectorAll('[data-study-seek]').forEach(button=>button.onclick=()=>seekStudy(button.dataset.studySeek));}
async function loadStudyTranscript(){
  if(!studyState.jobId)return;
  const request=++studyState.request,id=studyState.jobId;
  try{const parameters=new URLSearchParams({offset:studyState.offset,limit:80,q:studyState.query});
    if(studyState.chapter!==null)parameters.set('chapter',studyState.chapter);
    const data=await api(`/api/jobs/${id}/transcript?${parameters}`);
    if(studyState.request!==request||studyState.jobId!==id)return;
    studyState.transcript=data;renderStudyTranscript();
  }catch(error){toast(error.message,true);}
}
function renderStudyTranscript(){
  const target=$('#study-transcript-rows');if(!target)return;
  const data=studyState.transcript;if(!data){target.innerHTML=`<p>${ui.t('正在读取文字稿…')}</p>`;return;}
  target.innerHTML=`<p class="study-result-count">${data.total} ${ui.t('段字幕')} · ${ui.t('仅显示当前章节与搜索结果')}</p>`+
    data.items.map(segment=>`<div class="transcript-row" data-segment-start="${segment.start??''}" data-segment-end="${segment.end??''}"><button class="text-button" data-study-seek="${segment.start??''}" ${segment.start==null?'disabled':''}>${timestamp(segment.start)} <small>#${segment.id}</small></button><p>${esc(segment.text)}</p></div>`).join('')+
    (!data.items.length?`<p class="empty">${ui.t('没有符合条件的文字稿片段')}</p>`:'')+
    `<div class="pagination"><button class="button secondary" id="study-prev" ${data.offset<=0?'disabled':''}>${ui.t('← 上一页')}</button><span>${Math.floor(data.offset/data.limit)+1} / ${Math.max(1,Math.ceil(data.total/data.limit))}</span><button class="button secondary" id="study-next" ${data.offset+data.limit>=data.total?'disabled':''}>${ui.t('下一页 →')}</button></div>`;
  $('#study-prev').onclick=()=>{studyState.offset=Math.max(0,studyState.offset-data.limit);loadStudyTranscript();};
  $('#study-next').onclick=()=>{studyState.offset+=data.limit;loadStudyTranscript();};bindStudySeek();updateStudyPlayback();
}
function updateStudyPlayback(){
  const player=$('#study-player');if(!player)return;
  document.querySelectorAll('[data-segment-start]').forEach(row=>{const active=row.dataset.segmentStart!==''&&Number(row.dataset.segmentStart)<=player.currentTime&&Number(row.dataset.segmentEnd)>=player.currentTime;row.classList.toggle('playing',active);if(active)row.setAttribute('aria-current','true');else row.removeAttribute('aria-current');});
}
