'use strict';
const $ = id => document.getElementById(id);
const fields = ['sql','etl_date','mode','repo','model_name','target','keys','output_columns','variables','scope_mode','target_filter','scope_confirmed'];
let current = null, active = 0, requestVersion = 0;
function invalidate() {
  requestVersion++;
  current = null;
  $('copy').disabled = $('download').disabled = $('download_all').disabled = true;
  if (!$('results').hidden) $('feedback').textContent = 'Đầu vào đã thay đổi. Generate lại để cập nhật query.';
}
for (const id of fields) $(id).addEventListener('input', invalidate);
$('scope_mode').addEventListener('change', () => {
  $('filter_label').hidden = $('scope_mode').value !== 'custom';
  $('scope_confirmed').checked = false;
});
$('etl_date').addEventListener('change', () => { $('scope_confirmed').checked = false; });
$('clear').addEventListener('click', () => { $('sql').value = ''; invalidate(); $('sql').focus(); });
function save(text, filename) {
  const url = URL.createObjectURL(new Blob([text], {type:'text/plain;charset=utf-8'}));
  const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function filename(suffix) { return `${(current.job || 'model').replace(/[^a-z0-9_-]/gi,'_')}_${suffix}.sql`; }
function renderTest(index) {
  active = index;
  if (!current) return;
  const test = current.tests[index];
  for (let i=0; i<3; i++) { $(`tab${i}`).setAttribute('aria-selected',String(i===index)); $(`tab${i}`).tabIndex = i===index ? 0 : -1; }
  $('test_panel').setAttribute('aria-labelledby',`tab${index}`);
  $('test_title').textContent = `${test.id} · ${test.type}`;
  $('expected').textContent = `Expected: ${test.expected}`;
  $('test_sql').value = test.sql;
  $('test_sql').hidden = !test.ready;
  $('test_issue').hidden = test.ready;
  $('test_issue').textContent = test.issues.join(' ');
  $('copy').disabled = $('download').disabled = !test.ready;
  $('test_meta').replaceChildren();
  for (const [key,value] of Object.entries(test.metadata)) {
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = key; dd.textContent = value === '' ? '—' : value;
    $('test_meta').append(dt,dd);
  }
}
for (let i=0;i<3;i++) {
  $(`tab${i}`).addEventListener('click',() => renderTest(i));
  $(`tab${i}`).addEventListener('keydown',e => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) return;
    e.preventDefault();
    const n = e.key==='Home' ? 0 : e.key==='End' ? 2 : (i+(e.key==='ArrowRight'?1:2))%3;
    renderTest(n); $(`tab${n}`).focus();
  });
}
$('copy').addEventListener('click',async () => {
  if (!current?.tests[active].ready) return;
  try { await navigator.clipboard.writeText(current.tests[active].sql); $('copy').textContent = 'Đã copy'; setTimeout(() => $('copy').textContent='Copy SQL',1300); }
  catch { $('test_sql').focus(); $('test_sql').select(); $('feedback').textContent = 'Clipboard chưa được cấp quyền. Nhấn Ctrl+C để copy phần đã chọn.'; }
});
$('download').addEventListener('click',() => { if (current?.tests[active].ready) save(current.tests[active].sql,filename(current.tests[active].id)); });
$('download_all').addEventListener('click',() => { if (current?.tests.every(t=>t.ready)) save(current.tests.map(t=>t.sql).join('\n\n'),filename('all_tests')); });
function render(result) {
  current = result; $('results').hidden = false;
  const ready = result.tests.filter(t=>t.ready).length;
  $('feedback').textContent = `${ready}/3 query sẵn sàng · Chưa thực thi trên Athena`;
  $('result_title').textContent = result.job || 'Cần bổ sung thông tin';
  $('summary').textContent = `DEV · Target: ${result.target || 'chưa xác định'} · ${result.columns.length} cột · WHERE ${result.scope || 'chưa xác định'} · Khóa: ${result.keys.join(', ') || 'chưa xác nhận'}`;
  $('download_all').disabled = ready !== 3;
  $('issues').replaceChildren();
  for (const [items,cls] of [[result.errors,'error'],[result.input_issues,'error'],[result.key_problem ? [result.key_problem]:[],'error'],[result.warnings,'warning']]) {
    if (!items?.length) continue;
    const ul = document.createElement('ul'); ul.className=cls;
    for (const item of items) { const li=document.createElement('li'); li.textContent=item; ul.append(li); }
    $('issues').append(ul);
  }
  $('key_choices').replaceChildren();
  for (const candidate of result.key_candidates || []) {
    const b=document.createElement('button'); b.type='button'; b.className='secondary';
    b.textContent=`Dùng khóa: ${candidate.columns.join(', ')}`; b.title=candidate.evidence;
    b.addEventListener('click',() => { $('keys').value=candidate.columns.join(', '); invalidate(); $('form').requestSubmit(); });
    $('key_choices').append(b);
  }
  $('converted').value=result.sql || '';
  $('mappings').replaceChildren();
  for (const mapping of result.mappings || []) {
    const tr=document.createElement('tr');
    for (const value of [mapping.reference,mapping.table,mapping.evidence]) { const td=document.createElement('td'); td.textContent=value; tr.append(td); }
    $('mappings').append(tr);
  }
  $('notes').replaceChildren();
  for (const note of result.notes || []) { const li=document.createElement('li'); li.textContent=note; $('notes').append(li); }
  renderTest(active);
}
$('form').addEventListener('submit',async e => {
  e.preventDefault(); invalidate();
  const version = requestVersion;
  const payload=Object.fromEntries(fields.filter(f=>!['variables','scope_confirmed'].includes(f)).map(f=>[f,$(f).value]));
  payload.scope_confirmed=$('scope_confirmed').checked;
  try {
    payload.variables=JSON.parse($('variables').value || '{}');
    if (!payload.variables || Array.isArray(payload.variables) || typeof payload.variables!=='object') throw new Error('Biến dbt phải là JSON object.');
    $('generate').disabled=true; $('feedback').textContent='Đang resolve metadata và sinh query…';
    const response=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result=await response.json();
    if (version !== requestVersion) return;
    if (!response.ok) throw new Error(result.error || 'Không thể sinh query.');
    render(result);
  } catch(error) { if (version===requestVersion) $('feedback').textContent=`Lỗi: ${error.message}`; }
  finally { $('generate').disabled=false; }
});
fetch('/api/health').then(async response => {
  const health=await response.json();
  if (!response.ok) throw new Error(health.error);
  if (!$('repo').value) $('repo').value=health.repo;
  $('health').textContent=`${health.metadata.models} models · ${health.metadata.macros} macros`;
  $('feedback').textContent='Sẵn sàng · Metadata local; catalog Athena chưa được kiểm chứng';
}).catch(error => { $('feedback').textContent=`Không đọc được server local: ${error.message}`; });
