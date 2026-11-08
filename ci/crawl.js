// Doorloopt de app en klikt elk scherm en elke knop aan. Faalt bij scriptfouten, NaN/undefined in de tekst of horizontaal scrollen.
// Zonder PF_PW test hij de weergave "Verder zonder bedragen"; met PF_PW ook de versleutelde schermen.
const {chromium}=require('playwright');
const BASE=process.env.BASE||'http://localhost:8080',PW=process.env.PF_PW;
(async()=>{const b=await chromium.launch(),p=await b.newPage({viewport:{width:400,height:900}});const errs=[];let cur='start';
p.on('pageerror',x=>errs.push('PAGE '+x.message+' @ '+cur));p.on('console',m=>{if(m.type()==='error'&&!/fonts|ERR_|Failed to load|net::/.test(m.text()))errs.push('CON '+m.text().slice(0,80)+' @ '+cur)});
if(PW)await p.addInitScript(pw=>{localStorage.setItem('pf_pw',pw)},PW);
await p.goto(BASE+'/index.html');
if(!PW){await p.waitForSelector('text=Verder zonder bedragen',{timeout:30000});await p.click('text=Verder zonder bedragen')}
await p.waitForSelector('.nav button',{timeout:30000});await p.waitForTimeout(2000);
const BAD=/NaN|undefined|\[object|Infinity|null%|\bnull\b/,seen=new Set(),issues=[],SKIP=/impWis|data-del|alr|nsave|data-nx|readAll|id="bk"/;let clicks=0;
async function scan(l){const t=await p.evaluate(()=>document.body.innerText),m=t.match(BAD);if(m&&!seen.has(l+m[0])){seen.add(l+m[0]);const i=t.indexOf(m[0]);issues.push(l+': '+m[0]+' … '+t.slice(Math.max(0,i-50),i+30).replace(/\n/g,' '))}
 if(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2)&&!seen.has(l+'w')){seen.add(l+'w');issues.push(l+': horizontaal scrollen')}}
const tabs=await p.evaluate(()=>[...document.querySelectorAll('.nav button')].map(b=>b.dataset.t));
for(const t of tabs){cur='tab '+t;await p.evaluate(t=>{tab=t;det=null;sub=null;render()},t);await scan('tab '+t);
 const n=await p.evaluate(()=>document.querySelectorAll('#app button, #sheet button').length);
 for(let i=0;i<Math.min(n,80);i++){const info=await p.evaluate(i=>{const b=[...document.querySelectorAll('#app button, #sheet button')][i];return b?b.outerHTML.slice(0,90):null},i);if(!info||SKIP.test(info))continue;
  cur='tab '+t+' > '+info.replace(/\s+/g,' ').slice(0,60);await p.evaluate(i=>{const b=[...document.querySelectorAll('#app button, #sheet button')][i];b&&b.click()},i);clicks++;await p.waitForTimeout(40);await scan('tab '+t+' > '+info.replace(/\s+/g,' ').slice(0,50));
  await p.evaluate(()=>{const k=document.getElementById('bk');if(k)k.click()});if(await p.evaluate(()=>tab)!==t)await p.evaluate(t=>{tab=t;det=null;sub=null;render()},t)}}
const tks=await p.evaluate(()=>rows.map(r=>r.tk));
for(const tk of tks)for(const dt of ['analyse','moat','nieuws','dividend']){cur='bedrijf '+tk+' '+dt;await p.evaluate(([tk,dt])=>{tab='kand';det=tk;dtab=dt;render()},[tk,dt]);await scan('bedrijf '+tk+' '+dt)}
console.log('klikken',clicks,'bedrijven',tks.length,'problemen',issues.length,'fouten',[...new Set(errs)].length);issues.slice(0,30).forEach(x=>console.log(' -',x));[...new Set(errs)].slice(0,10).forEach(x=>console.log(' !',x));
await b.close();process.exit(issues.length||errs.length?1:0)})().catch(e=>{console.error(e);process.exit(2)});
