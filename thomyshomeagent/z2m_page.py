#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
z2m_page.py — Zigbee-Oberfläche für ThomysHomeAgent (Seite /zigbee)
====================================================================
Baut die wichtigsten Ansichten des Zigbee2MQTT-Frontends (WindFront) in
ThomysHomeAgent nach — gespeist ausschliesslich aus /api/z2m/* desselben Servers,
das Handy spricht also nie direkt mit Zigbee2MQTT oder dem MQTT-Broker:

  Geräte      Karten wie im Frontend: Name, Beschreibung, Linkqualität, Erreichbarkeit,
              an/aus, Helligkeit, Farbtemperatur, Farbe (je nach Fähigkeiten), Sensorwerte
  Aktivität   „Aktuelle Aktivität“: Zustandsänderungen je Gerät (linkquality: 184 → 190 …)
  Gruppen     Zigbee-Gruppen mit an/aus
  Bridge      Version, Koordinator, Netzwerk, Health, Warnungen, „Beitritt erlauben“

Einbau: siehe z2m_api.Z2MApi.page() bzw. README („Zigbee-Oberfläche in ThomysHomeAgent“).
"""

PAGE_PATH = "/zigbee"

HTML = r"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zigbee · ThomysHomeAgent</title>
%%HEAD%%
<style>
:root{--bg:#15171c;--card:#1f232b;--line:#2c313b;--fg:#e8eaf0;--mut:#9aa3b2;--ok:#4cd27a;--warn:#ffb020;--bad:#ff5c5c;--acc:#3fa9f5}
*{box-sizing:border-box;min-width:0}html,body{max-width:100%;overflow-x:hidden}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:2;background:#111318;border-bottom:1px solid var(--line);padding:.6em .9em;display:flex;gap:.5em;align-items:center;flex-wrap:wrap}header a.b{text-decoration:none;font-size:.9em}
header h1{font-size:1.05em;margin:0;flex:1 1 auto}#status{font-size:.85em;color:var(--mut);flex-basis:100%}
nav{display:flex;border-bottom:1px solid var(--line);background:#111318}nav button{flex:1;background:none;border:0;color:var(--mut);padding:.7em .4em;font:inherit;border-bottom:2px solid transparent}
nav button.on{color:var(--fg);border-bottom-color:var(--acc)}nav button{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}main{padding:.8em;max-width:980px;margin:0 auto}section[hidden]{display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(290px,100%),1fr));gap:.7em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:.8em}
.card h3{margin:0 0 .15em;font-size:1em}.desc{color:var(--mut);font-size:.82em;margin-bottom:.5em}
.badges{display:flex;gap:.4em;flex-wrap:wrap;margin:.3em 0 .5em}.badge{font-size:.75em;padding:.15em .5em;border-radius:999px;background:#2a2f39;color:var(--mut)}
.badge.ok{color:var(--ok)}.badge.bad{color:var(--bad)}.badge.warn{color:var(--warn)}
.lqi{display:inline-flex;gap:2px;align-items:flex-end;height:12px;margin-right:.3em;vertical-align:-1px}.lqi i{width:3px;background:#3a4150;display:block}.lqi i.on{background:var(--ok)}
.row{display:flex;align-items:center;gap:.6em;margin:.35em 0;flex-wrap:wrap}.row label{width:6.5em;flex:none;color:var(--mut);font-size:.85em}.row input[type=range]{flex:1 1 120px;min-width:80px;accent-color:var(--acc)}
button.b{background:#2a2f39;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:.45em .8em;font:inherit}button.b.on{background:var(--ok);color:#0b2213;border-color:var(--ok)}
button.b.acc{background:var(--acc);color:#04111c;border-color:var(--acc)}button.b.warn{background:var(--warn);color:#211600;border-color:var(--warn)}
.sw{display:inline-block;width:26px;height:26px;border-radius:50%;border:2px solid #0006;margin-right:.3em;cursor:pointer}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.15em .8em;font-size:.85em}.kv b{color:var(--mut);font-weight:500}
ul.act{list-style:none;padding:0;margin:0}ul.act li{padding:.45em .2em;border-bottom:1px solid var(--line);font-size:.88em}ul.act time{color:var(--mut);margin-right:.5em;font-variant-numeric:tabular-nums}
.muted{color:var(--mut)}.err{color:var(--bad)}a{color:var(--acc)}
</style></head><body>
<header><h1>Zigbee</h1>
<button class="b warn" id="join">Beitritt erlauben</button>
<button class="b" id="refresh" title="Aktualisieren">⟳</button>
<a class="b" id="fe" href="#" target="_blank" rel="noopener" hidden>WindFront</a>
<div id="status">lade …</div></header>
<nav><button class="on" data-tab="devices">Geräte</button><button data-tab="activity">Aktivität</button><button data-tab="groups">Gruppen</button><button data-tab="bridge">Bridge</button></nav>
<main>
<section id="devices"><div class="grid" id="dev"></div></section>
<section id="activity" hidden><ul class="act" id="act"></ul></section>
<section id="groups" hidden><div class="grid" id="grp"></div></section>
<section id="bridge" hidden><div class="card"><div class="kv" id="br"></div></div><div class="card" style="margin-top:.7em"><h3>Letzte Warnungen</h3><ul class="act" id="warn"></ul></div></section>
</main>
<script>
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,m){try{const r=await fetch(p,{method:m||'GET'});return await r.json();}catch(e){return {ok:false,error:String(e)};}}
const SW=['#FF1010','#FF6A00','#FFDF00','#00C000','#0033FF','#8A2BE2','#FF1493','#FFFFFF'];
let lastActivityKey='';
function lqi(v){if(v==null)return '';const n=Math.max(0,Math.min(4,Math.round(v/64+0.5)));return `<span class="lqi">${[1,2,3,4].map(i=>`<i class="${i<=n?'on':''}" style="height:${3*i}px"></i>`).join('')}</span>${v}`;}
function pct(b,max){return b==null?null:Math.round(b/(max||254)*100);}
function devCard(d,light){
  const st=d.state||{};const av=d.available;
  const badges=[lqi(st.linkquality!=null?st.linkquality:light?.linkquality),
    av==='offline'?'<span class="badge bad">offline</span>':av==='online'?'<span class="badge ok">online</span>':'',
    d.power_source==='Battery'?`<span class="badge">🔋 ${st.battery!=null?st.battery+'%':'Batterie'}</span>`:'',
    d.type==='Router'?'<span class="badge">Router</span>':d.type==='EndDevice'?'<span class="badge">Endgerät</span>':'',
    d.interview_state&&d.interview_state!=='SUCCESSFUL'?`<span class="badge warn">${esc(d.interview_state)}</span>`:''].filter(Boolean).join('');
  let body='';
  if(light){
    const n=encodeURIComponent(light.friendly_name);const on=light.state==='ON';
    body+=`<div class="row"><button class="b ${on?'on':''}" data-act="toggle" data-name="${esc(light.friendly_name)}">${on?'AN':'AUS'}</button>
      ${light.brightness!=null?`<span class="muted">${pct(light.brightness,light.brightness_max)} %</span>`:''}</div>`;
    if(light.features.includes('brightness'))body+=`<div class="row"><label>Helligkeit</label><input type="range" min="1" max="100" value="${pct(light.brightness,light.brightness_max)??50}" data-act="brightness" data-name="${esc(light.friendly_name)}"></div>`;
    if(light.color_temp_range)body+=`<div class="row"><label>Farbtemp.</label><input type="range" min="${light.color_temp_range[0]}" max="${light.color_temp_range[1]}" value="${light.color_temp??Math.round((light.color_temp_range[0]+light.color_temp_range[1])/2)}" data-act="ct" data-name="${esc(light.friendly_name)}" title="links warm, rechts kalt"></div>`;
    if(light.color)body+=`<div class="row"><label>Farbe</label><div>${SW.map(h=>`<span class="sw" style="background:${h}" data-act="color" data-hex="${h}" data-name="${esc(light.friendly_name)}"></span>`).join('')}</div></div>`;
  }else{
    const keys=Object.keys(st).filter(k=>!['linkquality','last_seen','update','battery','voltage'].includes(k)).slice(0,6);
    if(keys.length)body+=`<div class="kv">${keys.map(k=>`<b>${esc(k)}</b><span>${esc(typeof st[k]==='object'?JSON.stringify(st[k]):st[k])}</span>`).join('')}</div>`;
  }
  return `<div class="card"><h3>${esc(d.friendly_name)}</h3><div class="desc">${esc([d.vendor,d.model].filter(Boolean).join(' '))}${d.description?' · '+esc(d.description):''}</div><div class="badges">${badges}</div>${body}</div>`;
}
async function refresh(){
  const [info,devs,lights,groups,act]=await Promise.all([api('/api/z2m/info'),api('/api/z2m/devices'),api('/api/z2m/lights'),api('/api/z2m/groups'),api('/api/z2m/activity')]);
  if(!info.ok){$('#status').innerHTML=`<span class="err">Zigbee2MQTT nicht erreichbar: ${esc(info.error)}</span>`;return;}
  const s=info.result;
  $('#status').innerHTML=`${s.online?'<span class="badge ok">online</span>':'<span class="badge bad">'+esc(s.bridge_state||'getrennt')+'</span>'} Zigbee2MQTT ${esc(s.version??'?')} · ${s.devices_total} Geräte (${s.lights_total} Leuchten)${s.permit_join?` · <span class="badge warn">Beitritt offen${s.permit_join_remaining_sec!=null?' '+s.permit_join_remaining_sec+' s':''}</span>`:''}${s.mqtt_error?' · <span class="err">'+esc(s.mqtt_error)+'</span>':''}`;
  $('#join').textContent=s.permit_join?'Beitritt sperren':'Beitritt erlauben';$('#join').dataset.open=s.permit_join?'1':'';
  if(s.frontend_url){$('#fe').href=s.frontend_url;$('#fe').hidden=false;}
  const lm={};(lights.result||[]).forEach(l=>lm[l.friendly_name]=l);
  const ds=(devs.result||[]).filter(d=>d.type!=='Coordinator').sort((a,b)=>(lm[b.friendly_name]?1:0)-(lm[a.friendly_name]?1:0)||a.friendly_name.localeCompare(b.friendly_name));
  if(document.activeElement&&document.activeElement.type==='range')return;   // nicht unter dem Finger neu zeichnen
  $('#dev').innerHTML=ds.map(d=>devCard(d,lm[d.friendly_name])).join('')||'<p class="muted">Noch keine Geräte von Zigbee2MQTT empfangen.</p>';
  $('#grp').innerHTML=(groups.result||[]).map(g=>`<div class="card"><h3>${esc(g.friendly_name)}</h3><div class="desc">Gruppe ${g.id} · ${(g.members||[]).length} Mitglieder${(g.scenes||[]).length?' · Szenen: '+g.scenes.map(x=>esc(x.name)).join(', '):''}</div>
    <div class="row"><button class="b" data-act="on" data-name="${esc(g.friendly_name)}">AN</button><button class="b" data-act="off" data-name="${esc(g.friendly_name)}">AUS</button></div></div>`).join('')||'<p class="muted">Keine Gruppen.</p>';
  const a=act.result||[];const key=a.length?a[0].time+a[0].name:'';
  if(key!==lastActivityKey){lastActivityKey=key;$('#act').innerHTML=a.map(e=>`<li><time>${new Date(e.time*1000).toLocaleTimeString()}</time><b>${esc(e.name)}</b> — ${Object.entries(e.changes).map(([k,[o,n]])=>`${esc(k)}: ${esc(o??'∅')} → ${esc(n??'∅')}`).join(', ')}</li>`).join('')||'<li class="muted">Noch keine Aktivität.</li>';}
  const h=s.health||{};
  $('#br').innerHTML=[['Version',`${s.version??'?'} (Commit ${s.commit??'?'}, erwartet ${s.expected_version})`],['Koordinator',`${s.coordinator.type??'?'} · ${s.coordinator.ieee_address??'?'} · Rev. ${s.coordinator.revision??'?'}`],
    ['zigbee-herdsman',`${s.zigbee_herdsman??'?'} / converters ${s.zigbee_herdsman_converters??'?'}`],['Netzwerk',`Kanal ${s.network.channel??'?'}, PAN-ID ${s.network.pan_id??'?'}`],
    ['Maschine',`${s.os??'?'} · ${s.cpus??'?'} · ${s.memory_mb??'?'} MB · Node ${s.node_version??'?'}`],['MQTT',`${s.z2m_mqtt_server??'?'} · unsere Seite ${s.mqtt_host} (${s.mqtt_connected?'verbunden':'getrennt'})`],
    ['Health',h.uptime_sec!=null?`Laufzeit ${Math.round(h.uptime_sec/60)} min · Z2M ${h.process_memory_mb} MB · RAM ${h.os_memory_percent} % · Load ${JSON.stringify(h.os_load_average)}`:'noch kein Health-Check'],
    ['Neustart nötig',s.restart_required?'ja':'nein'],['Offline',(s.devices_offline||[]).join(', ')||'–']].map(([k,v])=>`<b>${k}</b><span>${esc(v)}</span>`).join('');
  $('#warn').innerHTML=(s.last_warnings||[]).map(w=>`<li><time>${new Date(w.time*1000).toLocaleTimeString()}</time>[${esc(w.level)}] ${esc(w.message)}</li>`).join('')||'<li class="muted">keine</li>';
}
document.querySelector('nav').addEventListener('click',ev=>{const b=ev.target.closest('button[data-tab]');if(!b)return;
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));document.querySelectorAll('main section').forEach(s=>s.hidden=s.id!==b.dataset.tab);});
let timer=null;
document.querySelector('main').addEventListener('click',ev=>{const b=ev.target.closest('[data-act]');if(!b||b.type==='range')return;const n=encodeURIComponent(b.dataset.name);
  const url={toggle:`/api/z2m/toggle?name=${n}&transition=1`,on:`/api/z2m/set?name=${n}&state=ON&transition=1`,off:`/api/z2m/set?name=${n}&state=OFF&transition=1`,color:`/api/z2m/set?name=${n}&hex=${encodeURIComponent(b.dataset.hex||'')}&transition=1`}[b.dataset.act];
  if(url)api(url,'POST').then(()=>setTimeout(refresh,600));});
document.querySelector('main').addEventListener('change',ev=>{const r=ev.target;if(!r.matches('input[type=range][data-act]'))return;const n=encodeURIComponent(r.dataset.name);
  const url=r.dataset.act==='brightness'?`/api/z2m/set?name=${n}&brightness=${r.value}&transition=1`:`/api/z2m/set?name=${n}&color_temp=${r.value}&transition=1`;api(url,'POST').then(()=>setTimeout(refresh,800));});
$('#join').addEventListener('click',()=>{const open=$('#join').dataset.open==='1';if(!open&&!confirm('Zigbee-Netz 2 Minuten für neue Geräte öffnen?'))return;api(`/api/z2m/permit_join?time=${open?0:120}`,'POST').then(refresh);});
$('#refresh').addEventListener('click',refresh);
refresh();timer=setInterval(refresh,5000);
</script></body></html>
"""


def html(head_extra=""):
    """Fertige Seite; head_extra z. B. pwa.head_tags() für die Handy-App."""
    return HTML.replace("%%HEAD%%", head_extra or "")
