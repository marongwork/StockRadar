// 选股雷达实验室 - 推广期公开渲染逻辑
const finiteNumber = v => { const n=Number(v); return Number.isFinite(n)?n:null; };
const fmt = v => finiteNumber(v) == null ? '-' : finiteNumber(v).toFixed(2);
const fmtWan = v => { const n=finiteNumber(v); return n==null?'-':(Math.abs(n)>=1e8?(n/1e8).toFixed(1)+'亿':(Math.abs(n)>=1e4?(n/1e4).toFixed(1)+'万':n.toFixed(0))); };
const cls = v => (finiteNumber(v) || 0) >= 0 ? 'pos' : 'neg';
const chgText = v => { const n=finiteNumber(v); return n==null?'-':((n>=0?'+':'')+n.toFixed(2)+'%'); };
const strategyName = v => ({all:'选股雷达整体', trend:'趋势事件策略', limit_up:'涨停短线策略', agent_council:'Agent 共识策略'}[v] || v || '-');
const strategyDesc = v => ({all:'所有信号合并后的真实表现', trend:'非涨停的事件/资金确认票', limit_up:'涨停或连板观察票，只用于次日竞价验证', agent_council:'仅保留多 Agent 看多且风险未否决的信号'}[v] || '策略回测');
const exitName = v => ({target:'止盈', stop:'止损', time:'到期退出', open:'持仓中'}[v] || v || '-');
const fundText = p => {
  const parts = [];
  parts.push('资金' + (p.fund_grade || '-') + '档' + (p.fund_score != null ? ' ' + p.fund_score + '分' : ''));
  if (p.main_net != null) parts.push('主力 ' + fmtWan(p.main_net));
  if (p.super_net != null) parts.push('超大单 ' + fmtWan(p.super_net));
  if (p.lobby_net != null) parts.push('龙虎榜机构 ' + fmtWan(p.lobby_net));
  if (p.seal_amount != null) parts.push('封单 ' + fmtWan(p.seal_amount));
  if (p.break_count != null) parts.push('炸板 ' + Number(p.break_count).toFixed(0) + '次');
  return parts.join(' / ');
};
const boardTerms = p => [p.industry, p.concepts, p.theme].filter(Boolean).flatMap(v => String(v).split(/[、,，;；|/]+/)).map(v=>v.trim()).filter(Boolean);
const boardText = p => [p.industry, p.concepts].filter(Boolean).join(' · ') || p.theme || '-';
function setupConceptFilter() {
  const select = document.getElementById('conceptFilter');
  if (!select || select.options.length > 1) return;
  [...new Set(SIGNALS.flatMap(boardTerms))].sort((a,b)=>a.localeCompare(b,'zh-CN')).forEach(name => {
    const option = document.createElement('option'); option.value = name; option.textContent = name; select.appendChild(option);
  });
}
const passRate = r => Number(r && r.qualified || 0) === 1 || (Number(r && r.trades || 0) >= 30 && Number(r && r.entry_days || 0) >= 20 && Number(r && r.expectancy || 0) > 0 && Number(r && r.max_drawdown || 0) >= -30);
const benchmarksOf = r => {
  try { const arr = JSON.parse(r.benchmarks_json || '[]'); if (Array.isArray(arr) && arr.length) return arr; } catch(e) {}
  return r.benchmark_name ? [{name:r.benchmark_name, return:r.benchmark_return, excess:r.excess_return}] : [];
};
const periodKey = r => [r.start_date||'全部历史', r.end_date||'最新', r.fee_bps ?? '-', r.slippage_bps ?? '-'].join('|');
const periodDays = r => {
  if (!r.start_date || !r.end_date) return 99999;
  const a = new Date(r.start_date + 'T00:00:00');
  const b = new Date(r.end_date + 'T00:00:00');
  const d = Math.round((b - a) / 86400000);
  return Number.isFinite(d) && d > 0 ? d : 99999;
};
let SELECTED_BT_PERIOD = '';
const periodRuns = () => {
  const key = SELECTED_BT_PERIOD || (RUNS[0] ? periodKey(RUNS[0]) : '');
  return RUNS.filter(r => periodKey(r) === key);
};
const sharedBenchmarks = () => {
  const runs = periodRuns();
  const run = runs.find(r => benchmarksOf(r).length >= 3) || runs[0] || {};
  return benchmarksOf(run);
};
const benchmarkPeriod = () => {
  const runs = periodRuns();
  const run = runs.find(r => benchmarksOf(r).length >= 3) || runs[0] || {};
  return (run.start_date || '-') + ' 至 ' + (run.end_date || '-');
};
const benchmarkRun = () => {
  const runs = periodRuns();
  return runs.find(r => benchmarksOf(r).length >= 3) || runs[0] || {};
};
function setupBacktestPeriods() {
  const sel = document.getElementById('backtestPeriod');
  if (!sel) return;
  const seen = new Set();
  const opts = [];
  RUNS.forEach(r => {
    if (!r.start_date || !r.end_date) return;
    const key = periodKey(r);
    if (seen.has(key)) return;
    seen.add(key);
    const days = periodDays(r);
    const label = (days < 99999 ? '近' + days + '天 · ' : '') + r.start_date + ' 至 ' + r.end_date + ' · 费用 ' + fmt(r.fee_bps) + 'bp / 滑点 ' + fmt(r.slippage_bps) + 'bp';
    opts.push({key,label,days});
  });
  opts.sort((a,b) => a.days - b.days);
  if (!SELECTED_BT_PERIOD && opts[0]) SELECTED_BT_PERIOD = opts[0].key;
  sel.innerHTML = opts.map(o => '<option value="'+o.key+'" '+(o.key===SELECTED_BT_PERIOD?'selected':'')+'>'+o.label+'</option>').join('');
  sel.onchange = () => { SELECTED_BT_PERIOD = sel.value; render(); };
}
const excessSummary = r => {
  const xs = benchmarksOf(r).filter(b => b.excess != null).map(b => Number(b.excess)).filter(Number.isFinite);
  if (!xs.length) return '-';
  return '超额 ' + fmt(Math.min(...xs)) + '% ~ ' + fmt(Math.max(...xs)) + '%';
};
const sectorValue = (row, words) => {
  for (const [key, raw] of Object.entries(row || {})) {
    if (!words.some(word => key.includes(word)) || raw == null || raw === '' || raw === '-') continue;
    if (typeof raw === 'number') return raw;
    let text = String(raw).replace(/,/g,'');
    let scale = text.includes('亿') ? 1e8 : (text.includes('万') ? 1e4 : 1);
    const value = Number(text.replace(/[^\d.+-]/g,''));
    if (Number.isFinite(value)) return value * scale;
  }
  return null;
};
const sectorName = row => {
  for (const word of ['板块名称','指数简称','概念名称','名称']) {
    const hit = Object.entries(row || {}).find(([key,value]) => key.includes(word) && value);
    if (hit) return String(hit[1]);
  }
  return '';
};
let sectorFlowChart = null;
let sectorFlowType = '概念';
let sectorFlowDate = '';
let sectorFlowRefreshTimer = null;
const sectorFlowColors = ['#00a67e','#1677ff','#e3a008','#ef476f','#7c3aed','#00a6c7','#d85b12','#537188','#13a10e','#b42318'];
function renderSectorFlowLine() {
  const chartEl = document.getElementById('sectorFlowChart');
  if (!chartEl || !window.echarts || !Array.isArray(SECTOR_FLOW_HISTORY)) return;
  const inTradingSession = frame => {
    const time=String(frame.snapshot_at || '').slice(11,16);
    if (!/^\d{2}:\d{2}$/.test(time)) return false;
    const [hour,minute]=time.split(':').map(Number); const value=hour*60+minute;
    return (value>=570 && value<=690) || (value>=780 && value<=900);
  };
  const allFrames = SECTOR_FLOW_HISTORY.filter(inTradingSession).slice().reverse();
  const dates=[...new Set(allFrames.map(frame=>String(frame.snapshot_at || '').slice(0,10)).filter(Boolean))].sort().reverse();
  if (!sectorFlowDate || !dates.includes(sectorFlowDate)) sectorFlowDate=dates[0] || '';
  const dateSelect=document.getElementById('sectorFlowDate');
  if (dateSelect && dateSelect.dataset.dates!==dates.join(',')) {
    dateSelect.innerHTML=dates.map(date=>'<option value="'+date+'">'+date+'</option>').join('');
    dateSelect.dataset.dates=dates.join(','); dateSelect.value=sectorFlowDate;
  }
  const frames = allFrames.filter(frame=>String(frame.snapshot_at || '').startsWith(sectorFlowDate)).map(frame=>({
    time:String(frame.snapshot_at || '').slice(11,16),
    rows:(frame.sectors || []).filter(row=>String(row['板块类型'] || '')===sectorFlowType)
  }));
  const availableRows = (frames[frames.length-1]?.rows || []).map(row=>({
    name:sectorName(row), flow:sectorValue(row,['主力资金净流入','主力资金流向']), chg:sectorValue(row,['涨跌幅'])
  })).filter(row=>row.name && Number.isFinite(row.flow));
  const inflowRows=availableRows.filter(row=>row.flow>=0).sort((a,b)=>b.flow-a.flow).slice(0,4);
  const outflowRows=availableRows.filter(row=>row.flow<0).sort((a,b)=>a.flow-b.flow).slice(0,4);
  const latestRows=[...inflowRows,...outflowRows];
  const names = latestRows.map(row=>row.name);
  const series = names.map((name,index)=>{
    const data=frames.map(frame=>{
      const row=frame.rows.find(item=>sectorName(item)===name);
      const value=row?sectorValue(row,['主力资金净流入','主力资金流向']):null;
      return Number.isFinite(value)?Number((value/1e8).toFixed(2)):null;
    });
    const pointCount=data.filter(Number.isFinite).length;
    const color=index<4?sectorFlowColors[index]:['#e5484d','#d85b12','#b42318','#8c3b3b'][index-4];
    return {name,type:'line',smooth:.22,showSymbol:frames.length<4||pointCount<=2,symbolSize:pointCount<=2?9:7,connectNulls:true,
      animationDuration:900,animationDurationUpdate:900,lineStyle:{width:index<4?3:2,color,type:pointCount<=2?'dashed':'solid'},
      itemStyle:{color},areaStyle:index<2?{opacity:.045}:undefined,data};
  });
  if (!sectorFlowChart) sectorFlowChart=echarts.init(chartEl,null,{renderer:'canvas'});
  sectorFlowChart.setOption({
    color:sectorFlowColors, animation:true,
    tooltip:{trigger:'axis',backgroundColor:'rgba(255,255,255,.96)',borderColor:'#cbd5df',textStyle:{color:'#14243b'},valueFormatter:value=>(Number(value)>=0?'+':'')+Number(value).toFixed(2)+'亿'},
    legend:{type:'scroll',top:4,left:8,right:8,textStyle:{color:'#536176',fontSize:12},pageTextStyle:{color:'#536176'}},
    grid:{left:62,right:22,top:54,bottom:42},
    xAxis:{type:'category',boundaryGap:false,data:frames.map(frame=>frame.time),axisLine:{lineStyle:{color:'#b9c6d2'}},axisLabel:{color:'#6b7889'},axisTick:{show:false}},
    yAxis:{type:'value',name:'亿元',nameTextStyle:{color:'#6b7889'},axisLabel:{color:'#6b7889',formatter:value=>(value>0?'+':'')+value},splitLine:{lineStyle:{color:'rgba(91,110,128,.14)',type:'dashed'}}},
    series
  },true);
  const isDailyOnly=frames.length===1 && frames[0].time==='15:00';
  document.getElementById('sectorFlowTime').textContent = sectorFlowDate || '-';
  document.getElementById('sectorFlowState').textContent = (isDailyOnly?'日级收盘快照':frames.length+' 个分钟快照')+' · 流入'+inflowRows.length+' / 流出'+outflowRows.length;
  document.getElementById('sectorFlowLatest').innerHTML = latestRows.map((row,index)=>'<span><i style="background:'+(index<4?sectorFlowColors[index]:['#e5484d','#d85b12','#b42318','#8c3b3b'][index-4])+'"></i><b>'+row.name+'</b><strong class="'+cls(row.flow)+'">'+fmtWan(row.flow)+'</strong><em class="'+cls(row.chg)+'">'+chgText(row.chg)+'</em></span>').join('') || '<span>暂无'+sectorFlowType+'板块资金数据</span>';
  renderSectorPreopen();
}
function renderSectorPreopen() {
  const target=document.getElementById('sectorPreopenReview');
  if (!target || !Array.isArray(SECTOR_FLOW_HISTORY)) return;
  const frames=SECTOR_FLOW_HISTORY.filter(frame=>{
    const time=String(frame.snapshot_at || '').slice(11,16);
    return String(frame.snapshot_at || '').startsWith(sectorFlowDate) && (frame.phase==='preopen' || (time>='09:15' && time<='09:25'));
  }).slice().reverse();
  if (!frames.length) {
    target.innerHTML='<div><b>盘前竞价复盘</b><span>今日暂无 09:15 / 09:25 板块快照；盘前数据不会接入盘中曲线。</span></div>';
    return;
  }
  const latest=frames[frames.length-1]; const previous=frames.length>1?frames[frames.length-2]:null;
  const previousMap=new Map((previous?.sectors || []).filter(row=>row['板块类型']===sectorFlowType).map(row=>[sectorName(row),sectorValue(row,['主力资金净流入','主力资金流向'])]));
  const rows=(latest.sectors || []).filter(row=>row['板块类型']===sectorFlowType).map(row=>({name:sectorName(row),flow:sectorValue(row,['主力资金净流入','主力资金流向'])})).filter(row=>row.name && Number.isFinite(row.flow)).sort((a,b)=>Math.abs(b.flow)-Math.abs(a.flow)).slice(0,5);
  target.innerHTML='<div><b>盘前竞价复盘</b><span>'+frames.map(frame=>String(frame.snapshot_at).slice(11,16)).join(' → ')+' · 独立观察，不接入盘中折线</span></div><section>'+rows.map(row=>{const old=previousMap.get(row.name);const delta=Number.isFinite(old)?row.flow-old:null;return '<span><b>'+row.name+'</b><strong class="'+cls(row.flow)+'">'+fmtWan(row.flow)+'</strong><em class="'+cls(delta)+'">'+(delta==null?'首帧':'变化 '+fmtWan(delta))+'</em></span>';}).join('')+'</section>';
}
async function refreshSectorFlowData() {
  try {
    const response=await fetch('/invest/sector-flow.json?t='+Date.now(),{cache:'no-store'});
    const payload=await response.json();
    if (Array.isArray(payload.history) && payload.history.length) SECTOR_FLOW_HISTORY=payload.history;
    renderSectorFlowLine();
  } catch (_) {}
}
function initSectorFlowMotion() {
  const chartEl=document.getElementById('sectorFlowChart');
  if (!chartEl || chartEl.dataset.ready==='1') return;
  chartEl.dataset.ready='1';
  document.querySelectorAll('[data-sector-type]').forEach(button=>button.addEventListener('click',()=>{
    document.querySelectorAll('[data-sector-type]').forEach(item=>item.classList.toggle('active',item===button));
    sectorFlowType=button.dataset.sectorType; renderSectorFlowLine();
  }));
  document.getElementById('sectorFlowDate')?.addEventListener('change',event=>{sectorFlowDate=event.target.value;renderSectorFlowLine();});
  renderSectorFlowLine();
  window.addEventListener('resize',()=>sectorFlowChart?.resize());
  clearInterval(sectorFlowRefreshTimer);
  sectorFlowRefreshTimer=setInterval(refreshSectorFlowData,60000);
}

function initLimitPoolFilters() {
  const toolbar=document.querySelector('.limit-pool-toolbar');
  if (!toolbar || toolbar.dataset.bound==='1') return;
  toolbar.dataset.bound='1';
  const rows=[...document.querySelectorAll('.limit-stock-row')];
  toolbar.addEventListener('click',event=>{
    const button=event.target.closest('[data-limit-filter]');
    if (!button) return;
    const filter=button.dataset.limitFilter;
    toolbar.querySelectorAll('[data-limit-filter]').forEach(item=>item.classList.toggle('active',item===button));
    let visible=0;
    rows.forEach(row=>{
      const show=filter==='all'||String(row.dataset.limitTags||'').split(' ').includes(filter);
      row.hidden=!show; if(show) visible+=1;
    });
    const count=document.getElementById('limitPoolCount'); if(count) count.textContent=visible+' 只';
  });
}
function initRadarMotion() {
  if (window.__radarMotionReady || !window.gsap) return;
  window.__radarMotionReady = true;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) {
    document.body.classList.add('motion-disabled');
    return;
  }
  if (window.ScrollTrigger) gsap.registerPlugin(ScrollTrigger);
  gsap.defaults({ ease: 'power3.out', duration: .72, overwrite: 'auto' });
  document.querySelectorAll('.decision-card,.market-tape,.signal-card').forEach(el => el.classList.add('gsap-layer'));
  const intro = gsap.timeline({ defaults: { ease: 'power3.out' } });
  intro.from('.top', { y: -24, autoAlpha: 0, duration: .55 })
    .from('.market-tape', { y: -10, autoAlpha: 0, duration: .42 }, '<.12')
    .from('.decision-card', { y: 18, autoAlpha: .7, stagger: { each: .05, from: 'start' }, duration: .42 }, '<.08')
    .from('.hero .panel', { y: 18, stagger: .06, duration: .48 }, '<.12');
  if (window.ScrollTrigger) {
    ScrollTrigger.batch('.panel:not(.hero .panel), .signal-card', {
      start: 'top 86%',
      once: true,
      interval: .08,
      batchMax: 5,
      onEnter: batch => gsap.fromTo(batch, { y: 18 }, { y: 0, stagger: .05, duration: .45, clearProps: 'transform' })
    });
  }
}
function refreshRadarMotion() {
  if (!window.gsap) return;
  if (!window.__radarMotionReady) initRadarMotion();
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) return;
  const cards = document.querySelectorAll('#pickCards .signal-card');
  if (cards.length) gsap.fromTo(cards, { y: 18, autoAlpha: .72 }, { y: 0, autoAlpha: 1, stagger: { each: .025, from: 'random' }, duration: .38, clearProps: 'transform,opacity,visibility' });
  if (window.ScrollTrigger) ScrollTrigger.refresh();
}
const quoteUrl = code => { const c=String(code||'').match(/\\d{6}/); const s=c?c[0]:String(code||''); const m=/^(600|601|603|605|688|689)/.test(s)?'sh':'sz'; return 'https://quote.eastmoney.com/'+m+s+'.html'; };
const detailUrl = p => p && p.id ? ('stock-lab/signals/'+p.id+'.html') : ('stock-lab/'+String(p.code||'').replace(/\\D/g,'').slice(0,6)+'.html');
function signalAt(p) { return (p.picked_at || ((p.picked_date||'-') + ' ' + (p.picked_time||'--:--'))).slice(0,16); }
function scanTimeText(p) {
  const at=signalAt(p), time=at.slice(11,16);
  return time >= '15:00' ? at.slice(0,10)+' 收盘复盘 · '+time+'生成' : at+'生成';
}
function quoteTimeText(p) { return p.quote_as_of || (signalAt(p).slice(11,16) >= '15:00' ? signalAt(p).slice(0,10)+' 15:00' : signalAt(p)); }
function hhmm(value) { return value ? String(value).slice(11,16) : '-'; }
function limitTimeText(p) {
  if (!p.first_limit_at && !p.final_limit_at) return '';
  return '首封 '+hhmm(p.first_limit_at)+' · 最终封板 '+hhmm(p.final_limit_at);
}
function slotInfo(p) {
  const t = signalAt(p).slice(11,16);
  if (t >= '09:00' && t < '11:30') return {cls:'slot-0945', label:'09:45 开盘'};
  if (t >= '11:30' && t < '13:30') return {cls:'slot-1245', label:'12:45 午间'};
  if (t >= '13:30' && t < '15:30') return {cls:'slot-1430', label:'14:30 尾盘'};
  return {cls:'slot-1610', label:'盘后复盘'};
}
function actionOf(p) {
  const r = p.reason || '';
  if (p.buy_point != null && Number(p.position_pct || 0) > 0) return ['可执行', 'buy'];
  if (r.includes('涨停短线') || String(p.theme || '').includes('涨停板')) return ['待次日竞价', 'short'];
  return ['观察', 'watch'];
}
function tagsOf(p) { return String(p.reason||'').split('+').filter(Boolean).slice(0,5).map(t=>'<span>'+t+'</span>').join(''); }
function councilOf(p) {
  try { const value=JSON.parse(p.agent_reviews_json||'{}'); return value && typeof value==='object'?value:{}; } catch (_) { return {}; }
}
function councilBand(p) {
  const review=councilOf(p); const opinions=Array.isArray(review.opinions)?review.opinions:[];
  if (!opinions.length) return '';
  const labels={buy:'看多',hold:'中性',sell:'看空'}; const consensus=review.consensus||'hold';
  const agents=opinions.map(item=>'<span class="agent-vote '+(item.signal||'hold')+'" title="'+(item.evidence||[]).join('；')+'"><i></i><b>'+item.label+'</b><em>'+labels[item.signal||'hold']+'</em></span>').join('');
  const matched=Array.isArray(review.matched_strategies)?review.matched_strategies:[];
  return '<div class="agent-council"><div class="agent-council-head"><span>AGENT 议会</span><b class="'+consensus+'">'+labels[consensus]+' · '+Math.round(Number(review.confidence||0)*100)+'%</b>'+(review.data_quality==='partial'?'<small>数据部分缺失</small>':'')+'<em>分歧 '+fmt(review.disagreement)+'</em>'+(review.risk_veto?'<strong>风险否决</strong>':'')+'</div><div class="strategy-match"><span>命中策略</span><b>'+(matched.join(' / ')||'暂无')+'</b></div><div class="agent-votes">'+agents+'</div></div>';
}
function numOr(v, fallback=-999999) { const n=Number(v); return Number.isFinite(n) ? n : fallback; }
function dedupeDaily(rows) {
  const groups = new Map();
  rows.forEach(p => {
    const code = String(p.code || '').replace(/\\D/g,'').slice(0,6) || String(p.code || '');
    const key = (p.picked_date || signalAt(p).slice(0,10) || '-') + '|' + code;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  });
  return [...groups.values()].map(items => {
    items.sort((a,b) => signalAt(b).localeCompare(signalAt(a)) || numOr(b.score) - numOr(a.score));
    const latest = {...items[0]};
    latest._dup_count = items.length;
    latest._first_signal_at = signalAt(items[items.length - 1]);
    latest._last_signal_at = signalAt(items[0]);
    latest._run_ids = [...new Set(items.map(x=>x.run_id).filter(Boolean))];
    latest._best_score = Math.max(...items.map(x=>numOr(x.score)));
    latest._best_rank = Math.min(...items.map(x=>numOr(x.rank, 999999)));
    return latest;
  });
}
function filteredSignals() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const date = document.getElementById('dateFilter').value;
  const action = document.getElementById('actionFilter').value;
  const agent = document.getElementById('agentFilter').value;
  const concept = document.getElementById('conceptFilter').value;
  const run = document.getElementById('runFilter').value;
  const sortBy = document.getElementById('sortBy').value;
  const uniqueMode = document.getElementById('uniqueMode').value;
  let rows = SIGNALS.filter(p => {
    const a = actionOf(p)[1];
    const hay = [p.code,p.name,p.theme,p.industry,p.concepts,p.reason,p.run_id,signalAt(p)].join(' ').toLowerCase();
    const agentMatch = !agent || (agent === 'veto' ? Number(p.risk_veto||0) === 1 : p.agent_consensus === agent);
    return (!q || hay.includes(q)) && (!date || p.picked_date === date) && (!action || a === action) && agentMatch && (!concept || boardTerms(p).includes(concept)) && (!run || p.run_id === run);
  });
  if (uniqueMode === 'daily' && !run) rows = dedupeDaily(rows);
  rows.sort((a,b) => {
    if (sortBy === 'time_asc') return signalAt(a).localeCompare(signalAt(b));
    if (sortBy === 'score_desc') return numOr(b.score) - numOr(a.score);
    if (sortBy === 'chg_desc') return numOr(b.chg_pct) - numOr(a.chg_pct);
    if (sortBy === 'chg_asc') return numOr(a.chg_pct) - numOr(b.chg_pct);
    return signalAt(b).localeCompare(signalAt(a));
  });
  return rows;
}
function render() {
  setupConceptFilter();
  setupBacktestPeriods();
  const latestRun = RUNS[0] || {};
  const runSelect = document.getElementById('runFilter');
  if (runSelect && runSelect.options.length === 1) {
    [...new Set(SIGNALS.map(p=>p.run_id).filter(Boolean))].forEach(r => {
      const opt = document.createElement('option'); opt.value = r; opt.textContent = r; runSelect.appendChild(opt);
    });
  }
  const rows = filteredSignals();
  const rawFiltered = SIGNALS.filter(p => {
    const a = actionOf(p)[1];
    const q = document.getElementById('q').value.trim().toLowerCase();
    const date = document.getElementById('dateFilter').value;
    const action = document.getElementById('actionFilter').value;
    const agent = document.getElementById('agentFilter').value;
    const concept = document.getElementById('conceptFilter').value;
    const run = document.getElementById('runFilter').value;
    const hay = [p.code,p.name,p.theme,p.industry,p.concepts,p.reason,p.run_id,signalAt(p)].join(' ').toLowerCase();
    const agentMatch = !agent || (agent === 'veto' ? Number(p.risk_veto||0) === 1 : p.agent_consensus === agent);
    return (!q || hay.includes(q)) && (!date || p.picked_date === date) && (!action || a === action) && agentMatch && (!concept || boardTerms(p).includes(concept)) && (!run || p.run_id === run);
  });
  const hiddenDup = Math.max(0, rawFiltered.length - rows.length);
  const runOk = passRate(latestRun);
  const trades = Number(latestRun.trades || 0);
  const entryDays = Number(latestRun.entry_days || 0);
  const runLabel = (trades < 30 || entryDays < 20) ? '样本不足，谨慎试验' : (runOk ? '验收通过' : (Number(latestRun.expectancy || 0) <= 0 ? '负期望，暂停执行' : '回撤超限，暂停执行'));
  const newest = rows[0] || SIGNALS[0] || {};
  const currentBatch = SIGNALS.filter(p => newest.run_id && p.run_id === newest.run_id);
  const actionable = currentBatch.filter(p => actionOf(p)[1] === 'buy').length;
  const shortCount = currentBatch.filter(p => actionOf(p)[1] === 'short').length;
  const statusClass = (trades < 30 || entryDays < 20) ? 'status-warn' : (runOk ? 'status-good' : 'status-bad');
  document.getElementById('decisionStrip').innerHTML =
    '<div class="decision-card primary"><div class="decision-label">策略状态</div><div class="decision-value '+statusClass+'">'+runLabel+'</div><div class="decision-note">样本、期望值和回撤共同决定是否允许交易。</div></div>' +
    '<div class="decision-card"><div class="decision-label">最新扫描结果</div><div class="decision-value">'+(newest.name || '-')+'</div><div class="decision-note">'+scanTimeText(newest)+' · '+(limitTimeText(newest)||newest.theme||'-')+'</div></div>' +
    '<div class="decision-card"><div class="decision-label">执行队列</div><div class="decision-value status-warn">'+actionable+'</div><div class="decision-note">可执行 '+actionable+' · 等竞价确认 '+shortCount+'</div></div>' +
    '<div class="decision-card"><div class="decision-label">回测验收</div><div class="decision-value">'+fmt(latestRun.expectancy)+'%</div><div class="decision-note">期望值 · 胜率 '+fmt(latestRun.win_rate)+'% · 回撤 '+fmt(latestRun.max_drawdown)+'%</div></div>';
  document.getElementById('summary').innerHTML =
    '<div class="summary-grid"><div><span>可执行</span><b class="status-good">'+actionable+'</b></div><div><span>待次日竞价</span><b>'+shortCount+'</b></div><div><span>当前展示</span><b>'+rows.length+'</b></div><div><span>合并重复</span><b>'+hiddenDup+'</b></div></div>' +
    '<div class="summary-note"><b>'+runLabel+'</b><span>'+(latestRun.trades||0)+' 笔 · '+entryDays+'个入场日 · 胜率 '+fmt(latestRun.win_rate)+'% · 期望值 '+fmt(latestRun.expectancy)+'% · 盈亏比 '+fmt(latestRun.payoff_ratio)+'</span></div>';
  document.getElementById('pickMeta').textContent = rows[0] ? ('最新 ' + signalAt(rows[0])) : '无匹配信号';
  const cardRows = rows.slice(0, 24);
  document.getElementById('pickCards').innerHTML = (cardRows.map(p => {
    const a=actionOf(p); const slot=slotInfo(p);
    const dup=p._dup_count>1 ? '<span title=\"同一股票当天多次入选\">当日出现 '+p._dup_count+' 次</span>' : '';
    // 反馈文档 P0：关注区间 / 支撑压力 / 仓位建议（image6 投顾推荐卡格式）
    const focusBand = (p.focus_low!=null && p.focus_high!=null)
      ? '<div class="focus-band"><span>关注区间</span><b class="focus-price">¥'+p.focus_low+' — ¥'+p.focus_high+'</b></div>'
      : '<div class="focus-band muted"><span>关注区间</span><b>—</b></div>';
    const levels = '<div class="plan">'
      + '<div><span>支撑位</span><b class="pos">'+(p.support_price!=null?'¥'+p.support_price:(p.stop_loss!=null?'¥'+p.stop_loss:'-'))+'</b></div>'
      + '<div><span>压力位</span><b class="neg">'+(p.resistance_price!=null?'¥'+p.resistance_price:(p.target!=null?'¥'+p.target:'-'))+'</b></div>'
      + '<div><span>建议仓位</span><b class="'+(p.position_pct>0?'pos':'neg')+'">'+(p.position_pct!=null?p.position_pct+'%':'仅观察')+'</b></div>'
      + '</div>';
    const posBadge = p.position_pct!=null && p.position_pct>0
      ? '<span class="badge pos-badge">仓位 '+p.position_pct+'%</span>' : '';
    return '<div class="card signal-card '+a[1]+' '+slot.cls+'">'
      + '<div class="card-top"><div><div class="name"><a href="'+detailUrl(p)+'">'+p.name+'</a> <span class="dim">'+p.code+'</span></div>'
      + '<div class="dim">'+scanTimeText(p)+' · 行情截至 '+quoteTimeText(p)+' · '+p.theme+'</div>'+(limitTimeText(p)?'<div class="limit-time-line">'+limitTimeText(p)+'</div>':'')+'</div>'
      + '<div style="display:flex;gap:6px;align-items:flex-start;flex-wrap:wrap;justify-content:flex-end"><span class="slot-pill">'+slot.label+'</span><span class="badge '+a[1]+'">'+a[0]+'</span>'+posBadge+'</div></div>'
      + focusBand
      + '<div class="sector-band"><span>A股板块 / 概念</span><b>'+boardText(p)+'</b>'+(p.sector_chg!=null?'<em class="'+cls(p.sector_chg)+'">'+chgText(p.sector_chg)+'</em>':'')+'</div>'
      + '<div class="fund-band"><span>资金确认 / 游资痕迹</span><b>'+fundText(p)+'</b></div>'
      + councilBand(p)
      + '<div class="metrics"><div class="metric"><span>评分</span><b>'+p.score+'</b></div><div class="metric"><span>涨跌</span><b class="'+cls(p.chg_pct)+'">'+chgText(p.chg_pct)+'</b></div><div class="metric"><span>市值</span><b>'+fmtWan(p.cap)+'</b></div><div class="metric"><span>排名</span><b>#'+(p.rank||'-')+'</b></div></div>'
      + levels
      + '<div class="tags">'+tagsOf(p)+dup+'</div>'
      + '<div><a class="dim" href="'+detailUrl(p)+'">完整分析 ↗</a></div></div>';
  }).join('') || '<div class="dim">暂无候选</div>');
  document.getElementById('pickTable').innerHTML = '<thead><tr><th>扫描生成</th><th>行情/封板时间</th><th>批次</th><th>重复</th><th>#</th><th>代码</th><th>名称</th><th>动作</th><th>主题</th><th>评分</th><th>资金档</th><th>涨跌</th><th>市值</th><th>买点</th><th>止损</th><th>目标</th><th>分析</th><th>理由</th></tr></thead><tbody>'+rows.map(p=>{const a=actionOf(p); return '<tr><td>'+scanTimeText(p)+'</td><td>截至 '+quoteTimeText(p)+(limitTimeText(p)?'<br>'+limitTimeText(p):'')+'</td><td>'+p.run_id+'</td><td>'+(p._dup_count>1 ? '当日'+p._dup_count+'次' : '-')+'</td><td>'+p.rank+'</td><td>'+p.code+'</td><td><b>'+p.name+'</b></td><td><span class="badge '+a[1]+'">'+a[0]+'</span></td><td>'+p.theme+'</td><td>'+p.score+'</td><td>'+fundText(p)+'</td><td class="'+cls(p.chg_pct)+'">'+chgText(p.chg_pct)+'</td><td>'+fmtWan(p.cap)+'</td><td>'+ (p.buy_point ?? '-') +'</td><td class="neg">'+(p.stop_loss ?? '-')+'</td><td class="pos">'+(p.target ?? '-')+'</td><td><a href="'+detailUrl(p)+'">详情</a></td><td class="dim">'+(p.reason||'-')+'</td></tr>';}).join('')+'</tbody>';
  document.getElementById('historyTable').innerHTML = '<thead><tr><th>日期</th><th>批次</th><th>开始时间</th><th>结束时间</th><th>候选</th><th>已评估</th><th>成功</th><th>止损</th><th>胜率</th><th>平均收益</th></tr></thead><tbody>'+HISTORY.map(h=>{const e=h.evaluated||0; const win=e?((h.success||0)/e*100).toFixed(0)+'%':'待回测'; return '<tr><td>'+h.picked_date+'</td><td>'+h.run_id+'</td><td>'+(h.first_signal_at||'-')+'</td><td>'+(h.last_signal_at||'-')+'</td><td>'+h.picks+'</td><td>'+e+'</td><td class="pos">'+(h.success||0)+'</td><td class="neg">'+(h.stopped||0)+'</td><td>'+win+'</td><td class="'+cls(h.avg_return)+'">'+(h.avg_return==null?'-':fmt(h.avg_return)+'%')+'</td></tr>';}).join('')+'</tbody>';
  const br = benchmarkRun();
  const selectedRuns = periodRuns();
  const selectedRunIds = new Set(selectedRuns.map(r => Number(r.id)).filter(Number.isFinite));
  const shownTrades = TRADES.filter(t => selectedRunIds.has(Number(t.run_id))).slice(0,80);
  const actualPeriod = (br.actual_start_date || '-') + ' 至 ' + (br.actual_end_date || '-') + ' · ' + (br.entry_days || 0) + '个入场日';
  document.getElementById('backtestMeta').innerHTML = '<div><span>参数周期</span><b>'+benchmarkPeriod()+'</b></div><div><span>真实交易覆盖</span><b>'+actualPeriod+'</b></div><div><span>最新验收</span><b>'+(br.run_at||'-')+'</b></div><div><span>交易成本</span><b>'+fmt(br.fee_bps)+'bp / '+fmt(br.slippage_bps)+'bp</b></div>';
  document.getElementById('benchmarkStrip').innerHTML = sharedBenchmarks().length ? sharedBenchmarks().map(b=>'<div><span>'+b.name+'</span><b class="'+cls(b.return)+'">'+fmt(b.return)+'%</b></div>').join('') : '<div class="dim">暂无同期大盘基准</div>';
  document.getElementById('runCards').innerHTML = selectedRuns.length ? '<div class="scroll bt-matrix"><table><thead><tr><th>策略</th><th>状态</th><th>样本 / 覆盖</th><th>胜率 / 盈亏比</th><th>期望值</th><th>策略总收益</th><th>相对大盘</th><th>最大回撤</th><th>夏普</th><th>连亏</th></tr></thead><tbody>'+selectedRuns.slice(0,3).map(r=>{const ok=passRate(r); return '<tr><td class="bt-name"><b>'+strategyName(r.strategy)+'</b><small>'+strategyDesc(r.strategy)+' · '+r.run_at+'</small></td><td><span class="bt-verdict '+(ok?'ok':'stop')+'">'+(ok?'允许执行':'样本不足/暂停')+'</span></td><td><span class="bt-num">'+(r.trades||0)+' 笔 · '+(r.entry_days||0)+'日</span><span class="bt-sub">'+(r.actual_start_date||'-')+' 至 '+(r.actual_end_date||'-')+'</span></td><td><span class="bt-num">'+fmt(r.win_rate)+'% / '+fmt(r.payoff_ratio)+'</span></td><td><span class="bt-num '+cls(r.expectancy)+'">'+fmt(r.expectancy)+'%</span></td><td><span class="bt-num '+cls(r.total_return)+'">'+fmt(r.total_return)+'%</span></td><td><span class="bt-num '+cls((benchmarksOf(r)[0]||{}).excess)+'">'+excessSummary(r)+'</span></td><td><span class="bt-num neg">'+fmt(r.max_drawdown)+'%</span></td><td><span class="bt-num '+cls(r.sharpe_ratio)+'">'+fmt(r.sharpe_ratio)+'</span></td><td><span class="bt-num neg">'+(r.max_consec_loss||'-')+'</span></td></tr>';}).join('')+'</tbody></table></div>' : '<div class="dim">暂无该周期回测</div>';
  document.getElementById('tradeTable').innerHTML = '<thead><tr><th>信号日</th><th>代码</th><th>名称</th><th>策略</th><th>买入</th><th>退出</th><th>收益</th><th>退出原因</th></tr></thead><tbody>'+shownTrades.map(t=>'<tr><td>'+t.signal_date+'</td><td>'+t.code+'</td><td><b>'+t.name+'</b></td><td>'+strategyName(t.strategy)+'</td><td>'+t.entry_date+'</td><td>'+t.exit_date+'</td><td class="'+cls(t.return_pct)+'">'+fmt(t.return_pct)+'%</td><td>'+exitName(t.exit_reason)+'</td></tr>').join('')+'</tbody>';
  requestAnimationFrame(refreshRadarMotion);
  requestAnimationFrame(initSectorFlowMotion);
  requestAnimationFrame(initLimitPoolFilters);
  // 内容淡入效果
  document.querySelectorAll('#pickCards,#runCards,#historyTable,#tradeTable').forEach(el => {
    el.classList.remove('skeleton');
    el.classList.add('content-loaded');
  });
}
render();
['q','dateFilter','actionFilter','agentFilter','conceptFilter','sortBy','uniqueMode','runFilter'].forEach(id => document.getElementById(id).addEventListener('input', render));
