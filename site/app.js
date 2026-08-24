/* Hydrosat pilot dashboard — static, no backend */
"use strict";

const CUL_COLOR = {Corn:"#e8c468", Alfalfa:"#6fc79b", Beat:"#c98bd6", Beans:"#6ebbd6", Cotton:"#f0f0e8"};
const CUL_RU = {Corn:"Кукуруза", Alfalfa:"Люцерна", Beat:"Свёкла", Beans:"Фасоль", Cotton:"Хлопок"};
const GRAD = {
  RdYlGn: "linear-gradient(90deg,#a50026,#d73027,#f46d43,#fdae61,#fee08b,#d9ef8b,#a6d96a,#66bd63,#1a9850,#006837)",
  BrBG:   "linear-gradient(90deg,#8c510a,#bf812d,#dfc27d,#f6e8c3,#c7eae5,#80cdc1,#35978f,#01665e)"
};
const LAYER_ORDER = ["ndvi","ndvi_contrast","ndmi","false","natural"];

let META, FIELDS, SERIES, CHIPS;
let map, baseLayers = {}, chipLayers = [], fieldLayer, fieldHalo;
let curLayer = "ndvi", curDate = null, selField = null, chipOpacity = .96;
let dateList = [];
const histCache = {};
let hChart, hSeason, sChart, sCv;

const $ = id => document.getElementById(id);
const fmtD = d => d ? d.slice(8,10)+"."+d.slice(5,7)+"."+d.slice(0,4) : "—";

async function boot(){
  [META, FIELDS, SERIES, CHIPS] = await Promise.all([
    fetch("data/meta.json").then(r=>r.json()),
    fetch("data/fields.geojson").then(r=>r.json()),
    fetch("data/series.json").then(r=>r.json()),
    fetch("data/chips_index.json").then(r=>r.json()),
  ]);
  $("updated").textContent = "данные: " + META.period[0] + " … " + META.period[1];
  const ds = new Set();
  Object.values(CHIPS).forEach(c => c.dates.forEach(d => ds.add(d)));
  dateList = [...ds].sort();
  curDate = dateList[dateList.length-1];
  initMap(); initLayersPanel(); initDates(); initFieldList();
  initTabs(); initHist(); initSeries();
  refreshChips(); refreshLegend();
}

/* ── карта ── */
function initMap(){
  map = L.map("map",{zoomControl:false});
  L.control.zoom({position:"bottomleft"}).addTo(map);
  baseLayers.esri = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{maxZoom:19,attribution:"Esri World Imagery"});
  baseLayers.osm  = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"© OpenStreetMap"});
  baseLayers.esri.addTo(map);
  // тёмное «гало» под контуром — граница читается на любой подложке
  fieldHalo = L.geoJSON(FIELDS,{interactive:false,
    style: f => ({color:"#0b1113", weight: selField===f.properties.field_id ? 8 : 5,
                  opacity:.55, fill:false})}).addTo(map);
  fieldLayer = L.geoJSON(FIELDS,{
    style: fieldStyle,
    onEachFeature: (f,l)=>{
      const p = f.properties;
      l.bindTooltip(`№${p.field_id} · ${CUL_RU[p.Culture]||p.Culture} · ${p.Area_ha} га`,
                    {sticky:true, className:"fld-tip"});
      l.on("click", ()=>selectField(p.field_id));   // клик по полю: выбрать
                                                     // + значение пикселя (map click)
    }
  }).addTo(map);
  map.fitBounds(fieldLayer.getBounds().pad(0.08));
  document.querySelectorAll("#base-btns .chip").forEach(b=>b.onclick=()=>setBase(b.dataset.base,b));
  loadWayback();
  map.on("click", e=>{
    const r = samplePixel(e.latlng);
    if(r) L.popup({className:"px-pop", closeButton:false, offset:[0,-4]})
           .setLatLng(e.latlng).setContent(r.html).openOn(map);
  });
  const op = $("opa");
  op.oninput = ()=>{ chipOpacity = op.value/100; $("opa-v").textContent = op.value+"%";
                     chipLayers.forEach(l=>l.setOpacity(chipOpacity)); };
}

function setBase(name, btn){
  document.querySelectorAll("#base-btns .chip").forEach(x=>x.classList.remove("active"));
  btn.classList.add("active");
  Object.values(baseLayers).forEach(l=>map.removeLayer(l));
  $("wayback-sel").classList.toggle("hidden", name!=="wayback");
  if(name==="wayback" && baseLayers.wayback) baseLayers.wayback.addTo(map);
  else if(baseLayers[name]) baseLayers[name].addTo(map);
  else baseLayers.esri.addTo(map);
  if(fieldLayer) fieldLayer.bringToFront();
}

async function loadWayback(){
  try{
    const cfg = await fetch("https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json").then(r=>r.json());
    const items = Object.entries(cfg).map(([num,v])=>({num,label:(v.itemTitle||"").replace("World Imagery (Wayback ","").replace(")","")}))
      .filter(x=>x.label).sort((a,b)=>b.label.localeCompare(a.label));
    const sel = $("wayback-sel");
    items.forEach(it=>{const o=document.createElement("option");o.value=it.num;o.textContent="Wayback "+it.label;sel.appendChild(o);});
    const mk = num => L.tileLayer(`https://wayback.maptiles.arcgis.com/arcgis/rest/services/world_imagery/mapserver/tile/${num}/{z}/{y}/{x}`,{maxZoom:19,attribution:"Esri Wayback"});
    baseLayers.wayback = mk(items[0].num);
    sel.onchange = ()=>{ const on = map.hasLayer(baseLayers.wayback);
      if(on) map.removeLayer(baseLayers.wayback);
      baseLayers.wayback = mk(sel.value);
      if(on){ baseLayers.wayback.addTo(map); fieldLayer.bringToFront(); } };
  }catch(e){ document.querySelector('[data-base="wayback"]').style.display="none"; }
}

/* ── слои-чипы ── */
function chipDateFor(fid){
  const c = CHIPS[String(fid)]; if(!c) return null;
  if(c.dates.includes(curDate)) return curDate;
  const prior = c.dates.filter(d=>d<curDate);
  if(!prior.length) return null;
  const d = prior[prior.length-1];
  return (new Date(curDate)-new Date(d))/864e5 <= 7 ? d : null;
}
function refreshChips(){
  chipLayers.forEach(l=>map.removeLayer(l)); chipLayers=[];
  Object.entries(CHIPS).forEach(([fid,c])=>{
    const d = chipDateFor(+fid); if(!d) return;
    const url = `data/chips/${fid}/${d}_${curLayer}.png`;
    const b = c.bounds; // [s,w,n,e]
    const ov = L.imageOverlay(url, [[b[0],b[1]],[b[2],b[3]]], {opacity:chipOpacity, interactive:false});
    ov.fid = +fid; ov.chipDate = d;
    ov.addTo(map); chipLayers.push(ov);
  });
  fieldLayer.bringToFront();
  updateFieldListValues();
  if(selField) showCard(selField);
}

/* значение пикселя по клику (ВП-14): цвет из PNG → значение через палитру */
function nearestLut(lut, r, g, b){
  let bi=0, bd=1e9;
  for(let i=0;i<lut.length;i++){
    const d=(lut[i][0]-r)**2+(lut[i][1]-g)**2+(lut[i][2]-b)**2;
    if(d<bd){bd=d;bi=i;}
  }
  return {i:bi, dist:Math.sqrt(bd)};
}
function samplePixel(latlng){
  const lm = META.layers[curLayer];
  for(let k=chipLayers.length-1;k>=0;k--){
    const ov = chipLayers[k], b = ov.getBounds();
    if(!b.contains(latlng)) continue;
    const img = ov.getElement();
    if(!img || !img.complete || !img.naturalWidth) continue;
    const w=img.naturalWidth, h=img.naturalHeight;
    const x=Math.floor((latlng.lng-b.getWest())/(b.getEast()-b.getWest())*w);
    const y=Math.floor((b.getNorth()-latlng.lat)/(b.getNorth()-b.getSouth())*h);
    if(x<0||y<0||x>=w||y>=h) continue;
    const c=document.createElement("canvas"); c.width=w; c.height=h;
    const ctx=c.getContext("2d",{willReadFrequently:true}); ctx.drawImage(img,0,0);
    const p=ctx.getImageData(x,y,1,1).data;
    if(p[3]===0) continue;                       // вне границы поля
    const f=FIELDS.features.find(f=>f.properties.field_id===ov.fid);
    const nm=f?`Поле №${ov.fid} · ${CUL_RU[f.properties.Culture]||f.properties.Culture}`:`Поле №${ov.fid}`;
    if(!lm.legend){                              // RGB-композит — значения нет
      return {html:`<b>${nm}</b><div class="sub">${lm.name} · ${fmtD(ov.chipDate)} · RGB ${p[0]},${p[1]},${p[2]}</div>`};
    }
    const lut=META.cmaps[lm.legend.cmap];
    const {i,dist}=nearestLut(lut,p[0],p[1],p[2]);
    let vmin=lm.legend.min, vmax=lm.legend.max, note="";
    if(vmin===null){ vmin=0; vmax=1; note=" (доля диапазона окна)"; }
    const val=vmin+(vmax-vmin)*i/255;
    const label=curLayer==="ndmi"?"NDMI":"NDVI";
    return {html:`<b>${label} ${val.toFixed(3)}</b><div class="sub">${nm}<br>${fmtD(ov.chipDate)} · пиксель 10 м${note}${dist>12?" · цвет приблизительный":""}</div>`};
  }
  return null;
}
function refreshLegend(){
  const lm = META.layers[curLayer], lg = $("legend");
  if(lm.legend && lm.legend.min!==null){
    lg.innerHTML = `<div class="bar" style="background:${GRAD[lm.legend.cmap]}"></div>
      <div class="lab"><span>${lm.legend.min}</span><span>${lm.legend.max}</span></div>`;
  } else if(lm.legend){
    lg.innerHTML = `<div class="bar" style="background:${GRAD[lm.legend.cmap]}"></div>
      <div class="lab"><span>мин окна</span><span>макс окна</span></div>`;
  } else lg.innerHTML = "";
  $("interp").textContent = lm.interp;
}

/* ── панели ── */
function initLayersPanel(){
  const box = $("layer-btns");
  LAYER_ORDER.forEach(k=>{
    const b = document.createElement("button");
    b.className = "chip"+(k===curLayer?" active":""); b.textContent = META.layers[k].name;
    b.onclick = ()=>{ curLayer=k;
      box.querySelectorAll(".chip").forEach(x=>x.classList.remove("active"));
      b.classList.add("active"); refreshChips(); refreshLegend(); };
    box.appendChild(b);
  });
}
function initDates(){
  const sel = $("d-select");
  dateList.forEach(d=>{const o=document.createElement("option");o.value=d;o.textContent=fmtD(d);sel.appendChild(o);});
  sel.value = curDate;
  sel.onchange = ()=>{curDate=sel.value; refreshChips();};
  $("d-prev").onclick = ()=>stepDate(-1);
  $("d-next").onclick = ()=>stepDate(1);
}
function stepDate(k){
  const i = dateList.indexOf(curDate)+k;
  if(i<0||i>=dateList.length) return;
  curDate = dateList[i]; $("d-select").value=curDate; refreshChips();
}
function valAt(fid, idx, date){
  const s = SERIES[String(fid)]; if(!s) return null;
  let i = s.dates.indexOf(date);
  if(i<0){ const prior = s.dates.filter(d=>d<=date); if(!prior.length) return null;
           i = s.dates.indexOf(prior[prior.length-1]); }
  return {v:s[idx].mean[i], cv:s[idx].cv?s[idx].cv[i]:null, date:s.dates[i], clear:s.clear_pct[i]};
}
function initFieldList(){
  const list = $("f-list");
  $("f-count").textContent = "· " + FIELDS.features.length;
  FIELDS.features.forEach(f=>{
    const p = f.properties;
    const el = document.createElement("div");
    el.className = "f-item"; el.id = "fi-"+p.field_id;
    const flag = META.implausible.some(x=>x.field_id===p.field_id) ? `<span class="flag" title="есть даты с физически неправдоподобными значениями — проверить границу">⚠</span>` : "";
    el.innerHTML = `<span class="cul" style="background:${CUL_COLOR[p.Culture]||'#888'}"></span>
      <span class="nm">№${p.field_id} ${CUL_RU[p.Culture]||p.Culture} · ${p.Area_ha} га</span>${flag}
      <span class="v" data-fid="${p.field_id}"></span>`;
    el.onclick = ()=>{ selectField(p.field_id, true); };
    list.appendChild(el);
  });
  updateFieldListValues();
}
function listIndex(){ return curLayer==="ndmi" ? "NDMI" : "NDVI"; }
function updateFieldListValues(){
  const idx = listIndex();
  $("f-count").textContent = `· ${FIELDS.features.length} · ${idx}`;
  document.querySelectorAll(".f-item .v").forEach(sp=>{
    const r = valAt(+sp.dataset.fid, idx, curDate);
    sp.textContent = r && r.v!=null ? r.v.toFixed(2) : "·";
    sp.title = r ? `${idx} на ${fmtD(r.date)} · чисто ${r.clear}%` : "нет данных на эту дату";
  });
}
function fieldStyle(f){
  const on = selField===f.properties.field_id;
  return {color: on ? "#7ef0bd" : "#ffffff", weight: on ? 4 : 2.2,
          opacity: on ? 1 : .8, dashArray: on ? "9 5" : null,
          fill:true, fillColor:"#7ef0bd", fillOpacity: on ? .14 : 0,
          className: on ? "fld-sel" : ""};
}
function selectField(fid, zoom){
  selField = fid;
  document.querySelectorAll(".f-item").forEach(x=>x.classList.remove("sel"));
  const it = $("fi-"+fid); if(it) it.classList.add("sel");
  fieldLayer.setStyle(fieldStyle);
  fieldHalo.setStyle(f=>({color:"#0b1113", weight:f.properties.field_id===fid?8:5,
                          opacity:.55, fill:false}));
  // className в setStyle Leaflet не применяет — класс анимации ставим вручную
  fieldLayer.eachLayer(l=>{
    const el = l.getElement();
    if(el) el.classList.toggle("fld-sel", l.feature.properties.field_id===fid);
  });
  fieldLayer.eachLayer(l=>{ if(l.feature.properties.field_id===fid) l.bringToFront(); });
  if(zoom){
    const lyr = fieldLayer.getLayers().find(l=>l.feature.properties.field_id===fid);
    if(lyr) map.fitBounds(lyr.getBounds().pad(0.6));
  }
  showCard(fid);
}
function showCard(fid){
  const f = FIELDS.features.find(x=>x.properties.field_id===fid); if(!f) return;
  const p = f.properties;
  const nd = valAt(fid,"NDVI",curDate), nm = valAt(fid,"NDMI",curDate);
  const impl = META.implausible.filter(x=>x.field_id===fid);
  const cd = chipDateFor(fid);
  const card = $("f-card");
  card.classList.remove("hidden");
  card.innerHTML = `
    <div class="head"><b>Поле №${fid}</b>
      <span style="color:var(--ink2)">${CUL_RU[p.Culture]||p.Culture} · ${p.Area_ha} га · ${p.Cad_number}</span>
      <button class="x" title="Закрыть">×</button></div>
    <div class="kv">
      <div class="cell"><div class="l">NDVI</div><div class="v">${nd&&nd.v!=null?nd.v.toFixed(3):"—"}</div></div>
      <div class="cell"><div class="l">NDMI</div><div class="v">${nm&&nm.v!=null?nm.v.toFixed(3):"—"}</div></div>
      <div class="cell"><div class="l">CV равном.</div><div class="v">${nd&&nd.cv!=null?nd.cv.toFixed(2):"—"}</div></div>
      <div class="cell"><div class="l">дата · чисто</div><div class="v" style="font-size:12px">${nd?fmtD(nd.date):"—"} · ${nd?nd.clear+"%":""}</div></div>
    </div>
    ${cd&&cd!==curDate?`<div class="chip-date">снимок на карте: ${fmtD(cd)} (на ${fmtD(curDate)} поле закрыто облаком)</div>`:""}
    ${impl.length?`<div class="warnbox">⚠ ${impl.length} дат с неправдоподобными значениями (${[...new Set(impl.map(x=>x.implausible))].join("; ")}) — вероятно, граница поля лежит не на посеве.</div>`:""}
    <div class="links">
      <button id="go-hist">Гистограммы поля →</button>
      <button id="go-series">Ряды поля →</button>
    </div>`;
  card.querySelector(".x").onclick = ()=>card.classList.add("hidden");
  $("go-hist").onclick = ()=>{ $("h-field").value=fid; switchTab("hist"); onHistField(); };
  $("go-series").onclick = ()=>{ $("s-field").value=fid; switchTab("series"); drawSeries(); };
}

/* ── вкладки ── */
function initTabs(){
  document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
}
function switchTab(name){
  document.querySelectorAll(".tab").forEach(b=>b.classList.toggle("active",b.dataset.tab===name));
  ["map","hist","series"].forEach(v=>$("view-"+v).classList.toggle("hidden",v!==name));
  if(name==="map") setTimeout(()=>map.invalidateSize(),50);
  if(name==="hist") onHistField();
  if(name==="series") drawSeries();
}

/* ── гистограммы ── */
function fillFieldSelect(sel){
  FIELDS.features.forEach(f=>{const p=f.properties;
    const o=document.createElement("option");o.value=p.field_id;
    o.textContent=`№${p.field_id} · ${CUL_RU[p.Culture]||p.Culture} · ${p.Area_ha} га`;sel.appendChild(o);});
}
function fillIndexSelect(sel){
  META.indices.forEach(i=>{const o=document.createElement("option");o.value=i;o.textContent=i;sel.appendChild(o);});
}
async function getHist(fid){
  if(!histCache[fid]) histCache[fid] = await fetch(`data/hist/${fid}.json`).then(r=>r.json());
  return histCache[fid];
}
function initHist(){
  fillFieldSelect($("h-field")); fillIndexSelect($("h-index"));
  $("h-field").onchange = onHistField;
  $("h-index").onchange = onHistIndex;
  $("h-date").onchange = drawHist;
  $("h-compare").onchange = drawHist;
  $("h-prev").onclick = ()=>stepHistDate(-1);
  $("h-next").onclick = ()=>stepHistDate(1);
}
async function onHistField(){ await onHistIndex(); }
async function onHistIndex(){
  const fid=$("h-field").value, idx=$("h-index").value;
  const h = await getHist(fid);
  const sel = $("h-date"); sel.innerHTML="";
  const dates = h[idx] ? Object.keys(h[idx].dates).sort() : [];
  dates.forEach(d=>{const o=document.createElement("option");o.value=d;o.textContent=fmtD(d);sel.appendChild(o);});
  if(dates.length) sel.value = dates[dates.length-1];
  $("h-interp").textContent = META.interp[idx] || "";
  drawHist(); drawSeason();
}
function stepHistDate(k){
  const sel=$("h-date"); const i=sel.selectedIndex+k;
  if(i<0||i>=sel.options.length) return;
  sel.selectedIndex=i; drawHist();
}
async function drawHist(){
  const fid=$("h-field").value, idx=$("h-index").value, date=$("h-date").value;
  const h = await getHist(fid);
  if(!h[idx] || !date){ if(hChart) hChart.destroy(); $("h-meta").textContent="нет гистограммы (мало пикселей)"; return; }
  const e = h[idx]; const counts = e.dates[date]||[];
  const labels = counts.map((_,i)=>(e.lo+e.step*(i+0.5)).toFixed(3));
  const total = counts.reduce((a,b)=>a+b,0)||1;
  const freq = counts.map(c=>+(100*c/total).toFixed(2));
  const dsets = [{label:fmtD(date),data:freq,backgroundColor:"#6fc79bcc",borderRadius:2,barPercentage:1,categoryPercentage:.95}];
  if($("h-compare").checked){
    const ds = Object.keys(e.dates).sort(); const i=ds.indexOf(date);
    if(i>0){ const pc=e.dates[ds[i-1]]; const pt=pc.reduce((a,b)=>a+b,0)||1;
      dsets.push({label:fmtD(ds[i-1]),data:pc.map(c=>+(100*c/pt).toFixed(2)),
        backgroundColor:"#6ebbd680",borderRadius:2,barPercentage:1,categoryPercentage:.95});}
  }
  const s = SERIES[fid], si = s.dates.indexOf(date);
  const stat = si>=0 && s[idx] ? ` · среднее <b>${s[idx].mean[si]}</b> · CV <b>${s[idx].cv&&s[idx].cv[si]!=null?s[idx].cv[si]:"—"}</b>` : "";
  $("h-meta").innerHTML = `${idx} · сетка ${e.grid_m} м · пикселей ${total}${stat}`;
  if(hChart) hChart.destroy();
  hChart = new Chart($("h-chart"),{type:"bar",
    data:{labels,datasets:dsets},
    options:{animation:false,plugins:{legend:{labels:{color:"#b3beb8"}}},
      scales:{x:{ticks:{color:"#7f8c86",maxTicksLimit:14},grid:{display:false},title:{display:true,text:idx,color:"#7f8c86"}},
              y:{ticks:{color:"#7f8c86"},grid:{color:"#2c363b"},title:{display:true,text:"% пикселей поля",color:"#7f8c86"}}}}});
}
function drawSeason(){
  const fid=$("h-field").value, idx=$("h-index").value;
  const s=SERIES[fid]; if(!s||!s[idx]){if(hSeason)hSeason.destroy();return;}
  const L=s.dates.map(fmtD);
  const mk=(k,color,fill,lbl)=>({label:lbl,data:s[idx][k]||[],borderColor:color,backgroundColor:color+"33",
    pointRadius:0,borderWidth:k==="p50"?2:1,fill,tension:.25,spanGaps:true});
  if(hSeason) hSeason.destroy();
  hSeason = new Chart($("h-season"),{type:"line",
    data:{labels:L,datasets:[mk("p05","#3e4a44",false,"p05"),mk("p95","#3e4a44","-1","p05–p95"),
      mk("p25","#4a7a63",false,"p25"),mk("p75","#4a7a63","-1","p25–p75"),mk("p50","#6fc79b",false,"медиана")]},
    options:{animation:false,plugins:{legend:{labels:{color:"#b3beb8",filter:i=>["p05–p95","p25–p75","медиана"].includes(i.text)}}},
      scales:{x:{ticks:{color:"#7f8c86",maxTicksLimit:12},grid:{display:false}},
              y:{ticks:{color:"#7f8c86"},grid:{color:"#2c363b"}}}}});
}

/* ── ряды ── */
function initSeries(){
  fillFieldSelect($("s-field")); fillIndexSelect($("s-index"));
  $("s-field").onchange = drawSeries; $("s-index").onchange = drawSeries;
}
function drawSeries(){
  const fid=$("s-field").value, idx=$("s-index").value;
  const s=SERIES[fid]; if(!s||!s[idx]) return;
  const L=s.dates.map(fmtD);
  if(sChart) sChart.destroy();
  sChart = new Chart($("s-chart"),{type:"line",
    data:{labels:L,datasets:[
      {label:"p25",data:s[idx].p25||[],borderColor:"#4a7a63",pointRadius:0,borderWidth:1,fill:false,tension:.25,spanGaps:true},
      {label:"p25–p75",data:s[idx].p75||[],borderColor:"#4a7a63",backgroundColor:"#4a7a6340",pointRadius:0,borderWidth:1,fill:"-1",tension:.25,spanGaps:true},
      {label:"среднее "+idx,data:s[idx].mean,borderColor:"#6fc79b",pointRadius:2.5,pointBackgroundColor:"#6fc79b",borderWidth:2,fill:false,tension:.25,spanGaps:true}]},
    options:{animation:false,plugins:{legend:{labels:{color:"#b3beb8",filter:i=>i.text!=="p25"}}},
      scales:{x:{ticks:{color:"#7f8c86",maxTicksLimit:14},grid:{display:false}},
              y:{ticks:{color:"#7f8c86"},grid:{color:"#2c363b"}}}}});
  if(sCv) sCv.destroy();
  sCv = new Chart($("s-cv"),{type:"line",
    data:{labels:L,datasets:[{label:"CV "+idx,data:s[idx].cv||[],borderColor:"#d8a755",
      backgroundColor:"#d8a75522",pointRadius:2,borderWidth:1.5,fill:true,tension:.25,spanGaps:true}]},
    options:{animation:false,plugins:{legend:{labels:{color:"#b3beb8"}}},
      scales:{x:{ticks:{color:"#7f8c86",maxTicksLimit:14},grid:{display:false}},
              y:{ticks:{color:"#7f8c86"},grid:{color:"#2c363b"}}}}});
}

boot();
