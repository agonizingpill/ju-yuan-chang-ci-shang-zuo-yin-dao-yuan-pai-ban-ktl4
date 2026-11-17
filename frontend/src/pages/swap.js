import{esc,pretty}from'../utils/format.js';import{empty,notify}from'../components/shared.js';import{send}from'../api.js';import{SWAP_BATCH_STATES,SWAP_ITEM_STATES}from'../constants.js';

// 全部剧目的已确认点名单及实名班表（换班可跨场、跨剧目整批登记）
function roster(s){
 const perfs=s.performances;
 const calls=s.calls.filter(c=>c.status==='confirmed');
 return calls.map(c=>({call:c,perf:perfs.find(p=>p.id===c.performance_id),
   prod:s.productions.find(p=>p.id===perfs.find(x=>x.id===c.performance_id).production_id),
   rows:s.assignments.filter(a=>a.crew_call_id===c.id)}));
}
const openItem=x=>x.state==='waiting'||x.state==='arrived';

export function render(s){
 const lists=roster(s),writer=s.user.role!=='viewer',admin=s.user.role==='admin';
 const flat=lists.flatMap(g=>g.rows);
 const openAssignmentIds=new Set(s.swapBatches.filter(b=>b.state==='pending').flatMap(b=>b.items.filter(openItem).map(i=>i.assignment_id)));
 return `<div class="pagehead"><div><div class="eyebrow">TEMP SWAP / 10</div><h1>临时换班，整批交接。</h1><p class="subtitle">经理把多条实名班表作为一批调换，支持两人跨场互换；接班未齐由原班值守，全部到岗一次性切换。</p></div><span class="mono">ALL-OR-NOTHING</span></div>
 <div class="columns"><section class="section"><h2>换班批次与接班进度</h2>${s.swapBatches.length?s.swapBatches.map(b=>`<article class="handover"><div class="eyebrow">${esc(b.batch_ref)} · ${SWAP_BATCH_STATES[b.state]} · ${b.arrived}/${b.total} 到岗</div><h2>${b.reason?esc(b.reason):'（未填写换班原因）'}</h2><div class="strip"><div>登记<b>${esc(b.created_by)}</b></div><div>生效<b>${b.effective_by?esc(b.effective_by):'待全部到岗'}</b></div><div>时间<b>${pretty(b.effective_at||b.created_at)}</b></div></div><div class="tablewrap"><table><thead><tr><th>原班（换出）</th><th>接班</th><th>状态 / 接班签到</th><th>责任交接</th></tr></thead><tbody>${b.items.map(i=>`<tr><td><b>${esc(i.from_view.name)}</b> <small class="mono">${esc(i.from_view.code)}</small>${i.from_view.checked_in?`<br><small>原签到 ${pretty(i.from_view.observed_at)}</small>`:''}</td><td><b>${esc(i.to_view.name)}</b> <small class="mono">${esc(i.to_view.code)}</small></td><td><span class="status ${i.state==='swapped'?'confirmed':'proposed'}">${SWAP_ITEM_STATES[i.state]}</span><br><small class="mono">${esc(i.production_code)} · ${i.date}</small><br><small>${i.observed_at?('接班签到 '+pretty(i.observed_at)):'待到岗（点名±15分钟）'}</small></td><td>${i.handovers?.length?i.handovers.map(h=>`${h.kind==='zone_post'?'入口岗位':'事件'}：${esc(h.from_staff_code)}→${esc(h.to_staff_code)}${h.ref_state==='closed'?'（已结，责任人保留）':''}`).join('<br>'):esc(i.handover_note||'—')}</td></tr>`).join('')}</tbody></table></div>${b.state==='pending'&&admin?`<div class="actions"><button data-cancel-swap="${b.id}">取消批次（原班继续值守）</button></div>`:''}</article>`).join(''):empty('登记批次后，这里显示每条换班的接班进度、签到证据与前后责任人。')}</section>
 <aside class="section"><h2>登记整批换班</h2>${admin?`<form id="swap-form"><label>批次号（重试请用同一号码，不会重复换班）</label><input name="batch_ref" minlength="3" maxlength="60" placeholder="SW-20261120-01" required><label>换班原因</label><textarea name="reason" rows="2" maxlength="2000" placeholder="临时调岗说明（可选）"></textarea><div id="swap-lines"><h3>换班条目</h3>${[0].map(()=>swapLine(s,lists,openAssignmentIds)).join('')}</div><button type="button" id="swap-add-line" ${flat.length?'':'disabled'}>再加一条（跨场互换）</button><div class="actions"><button class="primary" ${flat.length?'':'disabled'}>提交换班批次</button></div></form><p class="panelnote">提交后原班表不变，接班者凭此页面逐条重新签到；系统按调换后的完整班表核对时段重叠与每日上限。交接草稿或归档场次拒绝换班。</p>`:empty('仅值班经理可以登记或取消换班批次；领班可在下方为接班者登记到岗。')}
 <h2 style="margin-top:24px">接班签到</h2>${(()=>{const waiting=s.swapBatches.filter(b=>b.state==='pending').flatMap(b=>b.items.filter(openItem).map(i=>({...i,batch_ref:b.batch_ref})));return waiting.length?`<div class="staff-directory">${waiting.map(i=>`<form class="checkin swap-checkin" data-item="${i.id}"><div><b>${esc(i.to_view.name)}</b> <small class="mono">${esc(i.to_view.code)}</small><br><small>接替 ${esc(i.from_view.code)} · ${esc(i.production_code)} ${i.date} ${pretty(i.call_time)}点名</small></div><input name="observed_at" type="datetime-local" value="${(i.observed_at||i.call_time).slice(0,16)}" ${i.state==='arrived'?'disabled':''} ${!writer?'disabled':''}><button ${!writer||i.state==='arrived'?'disabled':''}>${i.state==='arrived'?'已到岗，等其他人':'接班签到'}</button></form>`).join('')}</div>`:empty('没有待接班的条目。')})()}</aside></div>`;
}

function swapLine(s,lists,openAssignmentIds){
 const groups=lists.map(g=>`<optgroup label="${esc(g.prod.code+' '+g.prod.title)} · ${g.perf.date} ${esc(g.perf.curtain)} / ${pretty(g.call.call_time)}点名">${g.rows.map(a=>`<option value="${a.id}" ${openAssignmentIds.has(a.id)?'disabled':''}>${esc(a.staff_code+' '+a.staff_name)}${openAssignmentIds.has(a.id)?'（换班中）':''}</option>`).join('')}</optgroup>`).join('');
 return `<div class="swap-line"><label>换出的实名班表</label><select name="assignment_id">${groups}</select><label>接班人员</label><select name="to_staff_id">${s.staff.filter(x=>x.active).map(x=>`<option value="${x.id}">${esc(x.code+' '+x.name)} / 每日≤${x.max_daily_calls}场</option>`).join('')}</select></div>`;
}

export function bind(s,reload){
 const admin=s.user.role==='admin';
 const form=document.querySelector('#swap-form');
 if(form){
  const lists=roster(s);
  const openIds=new Set(s.swapBatches.filter(b=>b.state==='pending').flatMap(b=>b.items.filter(openItem).map(i=>i.assignment_id)));
  const add=document.querySelector('#swap-add-line');
  add.onclick=()=>{const wrap=document.querySelector('#swap-lines');const t=document.createElement('template');t.innerHTML=swapLine(s,lists,openIds);wrap.appendChild(t.content.firstElementChild)};
  form.onsubmit=async e=>{e.preventDefault();const fd=new FormData(form);const assignment_ids=fd.getAll('assignment_id').map(Number);const to=fd.getAll('to_staff_id').map(Number);try{await send('/swap-batches',{batch_ref:String(fd.get('batch_ref')).trim(),reason:String(fd.get('reason')||''),items:assignment_ids.map((id,k)=>({assignment_id:id,to_staff_id:to[k]}))});await reload();notify('换班批次已登记，等待接班者到岗')}catch(x){notify(x.message)}};
 }
 document.querySelectorAll('[data-cancel-swap]').forEach(b=>b.onclick=async()=>{try{await send('/swap-batches/'+b.dataset.cancelSwap+'/cancel',{},'POST');await reload();notify('批次已取消，原班继续值守')}catch(x){notify(x.message)}});
 document.querySelectorAll('.swap-checkin').forEach(f=>f.onsubmit=async e=>{e.preventDefault();try{await send('/swap-items/'+f.dataset.item+'/check-in',{observed_at:new FormData(f).get('observed_at')});await reload();notify('接班签到已留档；全部到岗后整批生效')}catch(x){notify(x.message)}});
}
