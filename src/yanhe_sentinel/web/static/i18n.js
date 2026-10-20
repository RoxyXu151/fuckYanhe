/* UI-only localization. Course titles, notes, transcripts and evidence are never translated. */
'use strict';
const ui = (() => {
  const read = (key, fallback) => {try{return localStorage.getItem(key)||fallback;}catch{return fallback;}};
  const write = (key,value) => {try{localStorage.setItem(key,value);}catch{/* Storage may be disabled. */}};
  let language=read('yanhe.language', navigator.language.startsWith('zh')?'zh-CN':'en');
  if(!['zh-CN','en'].includes(language))language='zh-CN';
  let theme=read('yanhe.theme','system');if(!['light','dark','system'].includes(theme))theme='system';
  const media=matchMedia('(prefers-color-scheme: dark)');
  let dictionary={}, reverse={};const staticText=[],staticAttrs=[];
  function t(key){const canonical=reverse[key]||key;return language==='en'?(dictionary[canonical]||key):canonical;}
  function message(value){
    if(language!=='en')return t(value);
    const found=String(value??'').match(/^发现 (\d+) 个需留意的事项$/);
    if(found)return `Found ${found[1]} candidate notices`;
    if(String(value).startsWith('请先在模型设置中配置：')){let fields=String(value).slice('请先在模型设置中配置：'.length);for(const key of ['事项分析地址、模型与密钥','音频转写地址与模型','音频转写密钥'])fields=fields.replaceAll(key,t(key));return 'Configure in Model settings: '+fields.replaceAll('、',', ');}
    return t(value);
  }
  function applyTheme(){const actual=theme==='system'?(media.matches?'dark':'light'):theme;document.documentElement.dataset.theme=actual;document.documentElement.style.colorScheme=actual;}
  applyTheme();media.addEventListener('change',()=>{if(theme==='system')applyTheme();});
  function translateStatic(){document.documentElement.lang=language;for(const [node,text]of staticText)node.textContent=text.replace(text.trim(),t(text.trim()));for(const [node,name,text]of staticAttrs)node.setAttribute(name,t(text));}
  const ready=fetch('/static/en.json').then(r=>{if(!r.ok)throw Error('Translation catalogue unavailable');return r.json();}).then(data=>{dictionary=data;reverse=Object.fromEntries(Object.entries(data).filter(([zh,en])=>zh!==en).map(([zh,en])=>[en,zh]));translateStatic();}).catch(()=>{language='zh-CN';translateStatic();});
  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  while(walker.nextNode()){const node=walker.currentNode;if(/[\u3400-\u9fff]/.test(node.textContent)&&!node.parentElement.closest('script,style'))staticText.push([node,node.textContent]);}
  staticText.push([document.querySelector('title').firstChild,document.title]);
  document.querySelectorAll('[placeholder],[aria-label],[title]').forEach(node=>{for(const name of ['placeholder','aria-label','title']){const value=node.getAttribute(name);if(value&&/[\u3400-\u9fff]/.test(value))staticAttrs.push([node,name,value]);}});
  const languageSelect=document.querySelector('#ui-language'),themeSelect=document.querySelector('#ui-theme');
  languageSelect.value=language;themeSelect.value=theme;
  languageSelect.onchange=()=>{language=languageSelect.value;write('yanhe.language',language);translateStatic();ui.onLanguageChange?.();};
  themeSelect.onchange=()=>{theme=themeSelect.value;write('yanhe.theme',theme);applyTheme();};
  return {t,message,ready,get language(){return language;},labels:object=>new Proxy(object,{get(target,key){const value=target[key];return typeof value==='string'?t(value):value;}})};
})();
